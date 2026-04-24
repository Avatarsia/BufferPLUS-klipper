# buffer_feeder.py — Klipper extension for the Mellow LLL Plus Filament Buffer
#
# Architecture: Variante 3 (Python-Ansatz).
#
# Owns a single extruder-stepper-compatible stepper via its own trapq,
# independent of the main toolhead motion queue. Sensor-driven bang-bang
# control (HALL-based hysteresis) + explicit GCode commands for manual,
# LOAD, UNLOAD, and calibration flows.
#
# Key property: the feeder moves without ever calling
# toolhead.flush_step_generation() during print. Time-base for moves is
# mcu.estimated_print_time(reactor.monotonic()) + lead_time — decoupled
# from the toolhead lookahead.
#
# See docs/superpowers/specs/2026-04-23-python-ansatz-design.md for the
# full design rationale and feature mapping.

import logging
import math

import stepper


# ---------------------------------------------------------------------------
# State constants
# ---------------------------------------------------------------------------

STATE_INIT           = "INIT"
STATE_IDLE           = "IDLE"
STATE_INITIAL_GRIP   = "INITIAL_GRIP"
STATE_AUTO           = "AUTO"
STATE_MANUAL_FEED    = "MANUAL_FEED"
STATE_MANUAL_RETRACT = "MANUAL_RETRACT"
STATE_LOAD_PHASE_1   = "LOAD_PHASE_1"
STATE_LOAD_PHASE_2   = "LOAD_PHASE_2"
STATE_LOAD_PHASE_3   = "LOAD_PHASE_3"
STATE_UNLOAD_PHASE_1 = "UNLOAD_PHASE_1"
STATE_UNLOAD_PHASE_2 = "UNLOAD_PHASE_2"
STATE_UNLOAD_PHASE_3 = "UNLOAD_PHASE_3"
STATE_OVERFLOW       = "OVERFLOW"
STATE_RUNOUT         = "RUNOUT"
STATE_JAM            = "JAM"

# States where the feeder is allowed to carry on manual/auto motion
# (manual buttons are blocked during LOAD/UNLOAD/OVERFLOW/JAM).
USER_STATES = {STATE_IDLE, STATE_AUTO, STATE_MANUAL_FEED,
               STATE_MANUAL_RETRACT, STATE_RUNOUT}

# States where LOAD/UNLOAD is active — override commands
# (BUFFER_FEED/RETRACT/AUTO_ON/FORCE_BUFFER_FILL) must refuse.
BUSY_PHASE_STATES = {STATE_INITIAL_GRIP,
                     STATE_LOAD_PHASE_1, STATE_LOAD_PHASE_2, STATE_LOAD_PHASE_3,
                     STATE_UNLOAD_PHASE_1, STATE_UNLOAD_PHASE_2, STATE_UNLOAD_PHASE_3}

# States where jam-detection watches for HALL dwell anomalies.
JAM_WATCH_STATES = {STATE_AUTO, STATE_LOAD_PHASE_3}

# Main reactor tick interval (sensor polling, bang-bang decisions).
MAIN_TICK_INTERVAL = 0.02            # 50 Hz
JAM_TICK_INTERVAL  = 1.0             # 1 Hz

# Triple-click action kinds.
CLICK_SINGLE = 1
CLICK_DOUBLE = 2
CLICK_TRIPLE = 3

BUTTON_FEED    = "feed"
BUTTON_RETRACT = "retract"


# ---------------------------------------------------------------------------
# BufferFeeder
# ---------------------------------------------------------------------------

class BufferFeeder:

    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.name = config.get_name().split()[1]   # "mellow"

        # ----- Config: speeds -----
        self.feed_speed         = config.getfloat('feed_speed',         30., above=0.)
        self.manual_speed       = config.getfloat('manual_speed',       15., above=0.)
        self.burst_speed        = config.getfloat('burst_speed',        50., above=0.)
        self.load_fast_speed    = config.getfloat('load_fast_speed',    50., above=0.)
        self.load_slow_speed    = config.getfloat('load_slow_speed',     5., above=0.)
        self.unload_fast_speed  = config.getfloat('unload_fast_speed',  50., above=0.)
        self.grip_speed         = config.getfloat('grip_speed',         55., above=0.)
        self.accel              = config.getfloat('accel',            1000., above=0.)

        # ----- Config: distances / durations -----
        self.manual_chunk_distance = config.getfloat('manual_chunk_distance', 10.,  above=0.)
        self.burst_distance        = config.getfloat('burst_distance',       1300., above=0.)
        self.grip_duration         = config.getfloat('grip_duration',          10., above=0.)
        self.load_fast_distance    = config.getfloat('load_fast_distance',   1000., above=0.)
        self.load_slow_distance    = config.getfloat('load_slow_distance',    180., above=0.)
        self.load_buffer_max       = config.getfloat('load_buffer_max',      2000., above=0.)
        self.unload_sync_distance  = config.getfloat('unload_sync_distance',  180., above=0.)
        self.unload_fast_max       = config.getfloat('unload_fast_max',      2510., above=0.)

        # ----- Config: safety limits -----
        self.max_feed_time      = config.getfloat('max_feed_time',       60.,  above=0.)
        self.max_feed_distance  = config.getfloat('max_feed_distance',  3000., above=0.)
        self.hall_debounce_ms   = config.getint  ('hall_debounce_ms',    50,   minval=0)
        self.lead_time          = config.getfloat('lead_time',            0.3, above=0.)

        # ----- Config: jam detection -----
        self.jam_detection_enabled = config.getboolean('jam_detection_enabled', True)
        self.jam_clog_dwell_time   = config.getfloat('jam_clog_dwell_time',    60.,  above=0.)
        self.jam_clog_extrude_min  = config.getfloat('jam_clog_extrude_min',   30.,  above=0.)
        self.jam_supply_dwell_time = config.getfloat('jam_supply_dwell_time', 120.,  above=0.)
        self.jam_action            = config.get('jam_action', 'PAUSE').strip()

        # ----- Config: runout -----
        self.runout_pause       = config.getboolean('runout_pause', False)
        self.runout_follow_mm   = config.getfloat('runout_follow_mm', 100., minval=0.)

        # ----- Config: triple click / misc -----
        self.triple_click_window  = config.getfloat('triple_click_window',  1.5, above=0.)
        self.feed_burst_enabled   = config.getboolean('feed_burst_enabled', False)
        self.reenable_cooldown      = config.getfloat('reenable_cooldown',      1.0, minval=0.)
        self.reenable_cooldown_fast = config.getfloat('reenable_cooldown_fast', 0.5, minval=0.)

        # ----- Config: display / behaviour -----
        self.display_status_enabled = config.getboolean('display_status_enabled', True)
        self.auto_load_after_follow = config.getboolean('auto_load_after_follow', False)
        self.min_temp               = config.getfloat('min_temp', 180., minval=0.)

        # ----- Stepper + trapq -----
        self.motion_queuing = self.printer.load_object(config, 'motion_queuing')
        self.trapq = self.motion_queuing.allocate_trapq()
        self.trapq_append = self.motion_queuing.lookup_trapq_append()

        self.stepper = stepper.PrinterStepper(config, units_in_radians=False)
        self.stepper.setup_itersolve('cartesian_stepper_alloc', b'x')
        self.stepper.set_trapq(self.trapq)

        # Stepper position tracking (mm)
        self._commanded_pos = 0.0          # head of planned moves (end)
        self._last_move_end_time = 0.0     # print_time at which current/last move ends
        self._current_move = None          # dict with end_time, direction, distance_left
        self._feed_distance_accumulator = 0.0  # for safety max_feed_distance
        self._feed_start_time = None       # reactor time when continuous feed started
        self._accumulated_feed_distance = 0.0  # lifetime counter

        # Stepper enable handle (resolved at connect)
        self._stepper_enable = None

        # ----- Sensor pin state -----
        #
        # Polarity convention (matches the old [gcode_button]-based config):
        #
        # The Mellow LLL Plus uses a single arm that swings through tilt
        # positions based on buffer fill level. Each HALL is a photo-
        # interrupter that is BLOCKED by the arm in its associated tilt
        # position:
        #   HALL3 blocked → buffer empty
        #   HALL2 blocked → buffer full
        #   HALL1 blocked → overflow
        #
        # Electrical: arm-blocked → phototransistor OFF → pullup holds
        # pin HIGH. Klipper config uses `^!` (pullup + invert), so the
        # Klipper button callback delivers state=False when pin is HIGH.
        #   → arm blocked (threshold active)  ⇔  state=False
        #   → arm not blocking (threshold idle) ⇔  state=True
        #
        # The extension then inverts ONCE more below (polarity_flip=True)
        # so `hall_empty`/`hall_full`/`hall_overflow` return True when
        # the corresponding threshold is active. This is NOT a double
        # invert bug — the config `!` handles the PHYSICAL polarity,
        # the Python flip handles the "button-language vs threshold-
        # language" semantic shift.
        #
        # Entrance switch uses `^!` with state=True = filament present
        # (standard filament_switch_sensor wiring).
        # Buttons use `^!` with state=True = pressed.
        self._pin_raw_state     = {}   # name -> bool (callback state)
        self._pin_change_time   = {}   # name -> eventtime of last raw change
        self._pin_stable_state  = {}   # name -> debounced callback state
        self._pin_polarity_flip = {    # if True: semantic = not state
            'hall_empty':    True,   # HALL3 arm-blocked → state=False → semantic True
            'hall_full':     True,   # HALL2 arm-blocked → state=False → semantic True
            'hall_overflow': True,   # HALL1 arm-blocked → state=False → semantic True
            'entrance':      False,  # state=True already = filament present
            'feed_button':   False,  # state=True already = pressed
            'retract_button': False, # state=True already = pressed
        }
        # Initial raw state must correspond to "semantic inactive" so that
        # OVERFLOW doesn't trigger at boot before the first real callback.
        # For polarity_flip=True (HALLs): semantic = not raw → idle raw=True
        # For polarity_flip=False (entrance/buttons): semantic = raw → idle raw=False
        for name, flip in self._pin_polarity_flip.items():
            idle_raw = True if flip else False
            self._pin_raw_state[name] = idle_raw
            self._pin_change_time[name] = 0.0
            self._pin_stable_state[name] = idle_raw

        # ----- Register pins via buttons module -----
        buttons = self.printer.load_object(config, 'buttons')
        self._register_pin(buttons, config, 'hall_empty_pin',      'hall_empty')
        self._register_pin(buttons, config, 'hall_full_pin',       'hall_full')
        self._register_pin(buttons, config, 'hall_overflow_pin',   'hall_overflow')
        self._register_pin(buttons, config, 'entrance_pin',        'entrance')
        self._register_pin(buttons, config, 'feed_button_pin',     'feed_button')
        self._register_pin(buttons, config, 'retract_button_pin',  'retract_button')

        # ----- Click detection state -----
        self._click_count = {BUTTON_FEED: 0, BUTTON_RETRACT: 0}
        self._last_click_time = {BUTTON_FEED: 0.0, BUTTON_RETRACT: 0.0}
        self._button_held = {BUTTON_FEED: False, BUTTON_RETRACT: False}

        # ----- Operation flags -----
        self._state = STATE_INIT
        # Bang-bang is paused while the printer is in a paused/ended
        # print context (idle_timeout != printing). The flag is armed
        # on idle_timeout:ready/idle only if we were actively printing,
        # and cleared on idle_timeout:printing. Manual BUFFER_AUTO_ON
        # outside a print stays active — the flag never gets armed.
        self._bang_bang_suspended = False
        self._initial_grip_end_time = None
        self._load_phase3_target = None     # eventtime deadline for phase 3
        self._load_phase3_distance = 0.0
        self._load_phase3_max_distance = 0.0
        self._load_phase3_speed = 0.0       # per-call feed speed in phase 3
        self._continuous_feed = False       # True = keep submitting moves while active
        self._continuous_feed_direction = 0 # +1 or -1
        self._continuous_feed_speed = 0.0
        self._feed_deadline_time = None     # max feed deadline (reactor time)
        self._measure_load_active = False
        self._measure_load_distance = 0.0
        # Explicit toggle tracking so the first button click after
        # MEASURE_LOAD_START always starts the feed, regardless of
        # whatever _continuous_feed was doing in AUTO before.
        self._measure_feeding = False
        self._print_running = False
        self._jam_active = False

        # ----- Jam detection state -----
        self._hall2_start_time = None
        self._hall2_start_extruder_pos = 0.0
        self._hall3_start_time = None

        # ----- Runout follow state -----
        self._runout_filament_ref = None
        self._runout_follow_active = False

        # ----- Cooldown timer -----
        self._cooldown_deadline = None    # reactor time after which auto re-enables

        # ----- Abort signalling (set by HALT / STOP_BUFFER_FILL) -----
        # When True, _raise_if_locked_out() raises once, then auto-clears
        # the flag. Lets HALT propagate through any pending WAIT_IDLE
        # so calling macros abort cleanly.
        self._halt_requested = False

        # ----- Timers (created in _handle_ready) -----
        self._main_timer = None
        self._jam_timer = None

        # ----- Event handlers -----
        self.printer.register_event_handler('klippy:connect',  self._handle_connect)
        self.printer.register_event_handler('klippy:ready',    self._handle_ready)
        self.printer.register_event_handler('klippy:shutdown', self._handle_shutdown)

        # ----- GCode registrations -----
        gcode = self.printer.lookup_object('gcode')
        gcode.register_command('BUFFER_FEED',
                               self.cmd_BUFFER_FEED,
                               desc=self.cmd_BUFFER_FEED_help)
        gcode.register_command('BUFFER_RETRACT',
                               self.cmd_BUFFER_RETRACT,
                               desc=self.cmd_BUFFER_RETRACT_help)
        gcode.register_command('BUFFER_HALT',
                               self.cmd_BUFFER_HALT,
                               desc=self.cmd_BUFFER_HALT_help)
        gcode.register_command('BUFFER_AUTO_ON',
                               self.cmd_BUFFER_AUTO_ON,
                               desc=self.cmd_BUFFER_AUTO_ON_help)
        gcode.register_command('BUFFER_AUTO_OFF',
                               self.cmd_BUFFER_AUTO_OFF,
                               desc=self.cmd_BUFFER_AUTO_OFF_help)
        gcode.register_command('BUFFER_WAIT_IDLE',
                               self.cmd_BUFFER_WAIT_IDLE,
                               desc=self.cmd_BUFFER_WAIT_IDLE_help)
        gcode.register_command('BUFFER_LOAD_PHASE1',
                               self.cmd_BUFFER_LOAD_PHASE1,
                               desc=self.cmd_BUFFER_LOAD_PHASE1_help)
        gcode.register_command('BUFFER_LOAD_PHASE2',
                               self.cmd_BUFFER_LOAD_PHASE2,
                               desc=self.cmd_BUFFER_LOAD_PHASE2_help)
        gcode.register_command('BUFFER_LOAD_PHASE3',
                               self.cmd_BUFFER_LOAD_PHASE3,
                               desc=self.cmd_BUFFER_LOAD_PHASE3_help)
        gcode.register_command('BUFFER_UNLOAD_PHASE1',
                               self.cmd_BUFFER_UNLOAD_PHASE1,
                               desc=self.cmd_BUFFER_UNLOAD_PHASE1_help)
        gcode.register_command('BUFFER_UNLOAD_PHASE2',
                               self.cmd_BUFFER_UNLOAD_PHASE2,
                               desc=self.cmd_BUFFER_UNLOAD_PHASE2_help)
        gcode.register_command('BUFFER_UNLOAD_PHASE3',
                               self.cmd_BUFFER_UNLOAD_PHASE3,
                               desc=self.cmd_BUFFER_UNLOAD_PHASE3_help)
        gcode.register_command('FORCE_BUFFER_FILL',
                               self.cmd_FORCE_BUFFER_FILL,
                               desc=self.cmd_FORCE_BUFFER_FILL_help)
        gcode.register_command('STOP_BUFFER_FILL',
                               self.cmd_STOP_BUFFER_FILL,
                               desc=self.cmd_STOP_BUFFER_FILL_help)
        gcode.register_command('BUFFER_STATE_DUMP',
                               self.cmd_BUFFER_STATE_DUMP,
                               desc=self.cmd_BUFFER_STATE_DUMP_help)
        gcode.register_command('CALIBRATE_FEEDER_SYNC',
                               self.cmd_CALIBRATE_FEEDER_SYNC,
                               desc=self.cmd_CALIBRATE_FEEDER_SYNC_help)
        gcode.register_command('MEASURE_LOAD_START',
                               self.cmd_MEASURE_LOAD_START,
                               desc=self.cmd_MEASURE_LOAD_START_help)
        gcode.register_command('MEASURE_LOAD_STOP',
                               self.cmd_MEASURE_LOAD_STOP,
                               desc=self.cmd_MEASURE_LOAD_STOP_help)
        gcode.register_command('ENABLE_RUNOUT_SENSOR',
                               self.cmd_ENABLE_RUNOUT_SENSOR,
                               desc="Set print_running=1 — enable runout PAUSE")
        gcode.register_command('DISABLE_RUNOUT_SENSOR',
                               self.cmd_DISABLE_RUNOUT_SENSOR,
                               desc="Set print_running=0 — disable runout PAUSE")
        gcode.register_command('BUFFER_CLEAR_JAM',
                               self.cmd_BUFFER_CLEAR_JAM,
                               desc="Clear JAM state after operator intervention")

        logging.info("buffer_feeder '%s' initialised", self.name)

    # -----------------------------------------------------------------------
    # Pin registration helper
    # -----------------------------------------------------------------------

    def _register_pin(self, buttons, config, config_key, logical_name):
        """Read pin from config and register a raw-state callback."""
        pin = config.get(config_key)

        def _callback(eventtime, raw_state, _ln=logical_name):
            self._on_pin_raw_change(eventtime, _ln, bool(raw_state))

        buttons.register_buttons([pin], _callback)

    # -----------------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------------

    def _handle_connect(self):
        # Resolve stepper enable once all MCUs are connected.
        try:
            se = self.printer.lookup_object('stepper_enable')
            self._stepper_enable = se.lookup_enable(self.stepper.get_name())
        except Exception:
            logging.exception("buffer_feeder: could not look up stepper_enable")

        # Track print_running via idle_timeout events (best effort).
        try:
            self.printer.register_event_handler('idle_timeout:printing',
                                                self._on_idle_printing)
            self.printer.register_event_handler('idle_timeout:ready',
                                                self._on_idle_ready)
            self.printer.register_event_handler('idle_timeout:idle',
                                                self._on_idle_ready)
        except Exception:
            logging.exception("buffer_feeder: could not register idle events")

    def _handle_ready(self):
        # Set initial stepper position to zero in our frame.
        self.stepper.set_position([0., 0., 0.])
        # Populate "last_move_end_time" based on current MCU print_time.
        mcu = self.stepper.get_mcu()
        now_pt = mcu.estimated_print_time(self.reactor.monotonic())
        self._last_move_end_time = now_pt + self.lead_time

        # Transition to IDLE.
        self._set_state(STATE_IDLE)

        # Start reactor timers.
        self._main_timer = self.reactor.register_timer(self._main_tick,
                                                       self.reactor.NOW)
        self._jam_timer = self.reactor.register_timer(self._jam_tick,
                                                      self.reactor.NOW)
        self._respond("BufferFeeder: ready — state=%s" % self._state)

    def _handle_shutdown(self):
        # Stop timers and halt motion.
        if self._main_timer is not None:
            try:
                self.reactor.unregister_timer(self._main_timer)
            except Exception:
                pass
            self._main_timer = None
        if self._jam_timer is not None:
            try:
                self.reactor.unregister_timer(self._jam_timer)
            except Exception:
                pass
            self._jam_timer = None
        self._continuous_feed = False
        try:
            self._disable_stepper()
        except Exception:
            logging.exception("buffer_feeder: shutdown stepper disable failed")

    def _on_idle_printing(self, *args):
        self._print_running = True
        # RESUME / print-start: bang-bang resumes.
        self._bang_bang_suspended = False
        # Documented RESUME-clears-JAM path (spec §10, README §Jam).
        # When Klipper transitions back to 'printing' (typically after
        # a RESUME following our PAUSE-on-jam), drop the JAM lockout
        # so the feeder resumes AUTO. HALL1 is still respected — if
        # physical overflow is still present, we fall into OVERFLOW.
        if self._state == STATE_JAM or self._jam_active:
            self._respond("RESUME: clearing JAM lockout")
            self._jam_active = False
            self._hall2_start_time = None
            self._hall3_start_time = None
            if self.hall_overflow:
                # Cannot resume while overflow physically present.
                self._enter_overflow()
            elif self.entrance_detected:
                self._enable_stepper()
                self._set_state(STATE_AUTO)
            else:
                self._set_state(STATE_IDLE)

    def _on_idle_ready(self, *args):
        # If we were actively printing, treat this as print-PAUSE or
        # print-end: suspend bang-bang. Manual BUFFER_AUTO_ON from an
        # idle console never sees print_running=True, so this guard
        # keeps user-initiated AUTO active across idle events.
        if self._print_running and self._state == STATE_AUTO:
            self._bang_bang_suspended = True
            self._continuous_feed = False
            self._halt_motion()
            self._respond("Print paused — bang-bang suspended")
        self._print_running = False

    # -----------------------------------------------------------------------
    # Sensor: raw pin change + debounce
    # -----------------------------------------------------------------------

    def _on_pin_raw_change(self, eventtime, name, raw_state):
        """Callback from buttons.register_buttons. Debounce, then dispatch."""
        if raw_state == self._pin_raw_state[name]:
            return
        self._pin_raw_state[name] = raw_state
        self._pin_change_time[name] = eventtime
        # Debounce is evaluated on next main tick.

    def _check_debounce(self, eventtime):
        """Promote raw->stable after hall_debounce_ms."""
        threshold = self.hall_debounce_ms / 1000.0
        for name, raw in self._pin_raw_state.items():
            stable = self._pin_stable_state[name]
            if stable == raw:
                continue
            if (eventtime - self._pin_change_time[name]) >= threshold:
                self._pin_stable_state[name] = raw
                self._on_stable_sensor_change(eventtime, name, raw)

    def _semantic_state(self, name):
        """Return semantic 'active' bool from stable raw state."""
        raw = self._pin_stable_state[name]
        return (not raw) if self._pin_polarity_flip[name] else raw

    # Convenience accessors (always up-to-date with debounced state).
    @property
    def hall_empty(self):    return self._semantic_state('hall_empty')
    @property
    def hall_full(self):     return self._semantic_state('hall_full')
    @property
    def hall_overflow(self): return self._semantic_state('hall_overflow')
    @property
    def entrance_detected(self):
        return self._semantic_state('entrance')
    @property
    def feed_button_pressed(self):
        return self._semantic_state('feed_button')
    @property
    def retract_button_pressed(self):
        return self._semantic_state('retract_button')

    def _on_stable_sensor_change(self, eventtime, name, raw_state):
        """Dispatch stable sensor change to the right handler."""
        if name == 'hall_overflow':
            if self.hall_overflow:
                self._enter_overflow()
            else:
                self._exit_overflow()
        elif name == 'hall_full':
            # Bang-bang reacts in main tick, nothing to do here.
            pass
        elif name == 'hall_empty':
            pass
        elif name == 'entrance':
            if self.entrance_detected:
                self._on_entrance_insert(eventtime)
            else:
                self._on_entrance_runout(eventtime)
        elif name == 'feed_button':
            self._on_button_change(BUTTON_FEED, self.feed_button_pressed, eventtime)
        elif name == 'retract_button':
            self._on_button_change(BUTTON_RETRACT, self.retract_button_pressed, eventtime)

    # -----------------------------------------------------------------------
    # Overflow (HALL1) — hard priority
    # -----------------------------------------------------------------------

    def _enter_overflow(self):
        self._respond("*** HALL1 OVERFLOW — Feeder disabled, lockout engaged ***",
                      force_display=True)
        self._continuous_feed = False
        self._halt_motion()
        self._disable_stepper()
        self._set_state(STATE_OVERFLOW)

    def _exit_overflow(self):
        if self._state != STATE_OVERFLOW:
            return
        self._respond("HALL1 cleared — overflow lockout released")
        # Go to IDLE; user can call BUFFER_AUTO_ON or trigger via entrance re-insert.
        self._set_state(STATE_IDLE)
        if self.entrance_detected:
            # Re-arm bang-bang if filament is there.
            self._enable_stepper()
            self._set_state(STATE_AUTO)

    # -----------------------------------------------------------------------
    # Entrance (buffer_entrance) events
    # -----------------------------------------------------------------------

    def _on_entrance_insert(self, eventtime):
        self._respond("Filament at entrance detected")
        # If we were mid-runout-follow (externer Sensor-Modus), abbrechen.
        if self._runout_follow_active:
            self._runout_follow_active = False
            self._runout_filament_ref = None
            self._respond("Runout-follow cancelled (filament re-inserted)")
        # Only trigger initial grip if we're in a state that welcomes it.
        if self._state in (STATE_IDLE, STATE_RUNOUT):
            self._start_initial_grip(eventtime)

    def _on_entrance_runout(self, eventtime):
        # Planned filament exit: suppress during LOAD/UNLOAD/MANUAL.
        if self._state in (STATE_LOAD_PHASE_1, STATE_LOAD_PHASE_2, STATE_LOAD_PHASE_3,
                           STATE_UNLOAD_PHASE_1, STATE_UNLOAD_PHASE_2,
                           STATE_UNLOAD_PHASE_3, STATE_MANUAL_FEED,
                           STATE_MANUAL_RETRACT):
            return

        if not self._print_running:
            # Not printing: just stop feeder, return to IDLE.
            self._respond("Entrance runout outside print — stepper off")
            self._continuous_feed = False
            self._halt_motion()
            self._disable_stepper()
            self._set_state(STATE_IDLE)
            return

        # Printing: branch on runout_pause policy.
        if self.runout_pause:
            self._respond("Runout during print — PAUSE (runout_pause=1)",
                          force_display=True)
            self._continuous_feed = False
            self._halt_motion()
            self._disable_stepper()
            self._set_state(STATE_RUNOUT)
            self._gcode_run_script("PAUSE")
        else:
            # runout_pause=0: externer Sensor übernimmt PAUSE. Wir lassen
            # Bang-Bang in AUTO weiterlaufen (Feeder fördert noch den
            # Rest-Weg hinterher), aber zählen die Extruder-Distanz
            # mit. Nach runout_follow_mm Extrusion → Stepper aus.
            # State bleibt wie er ist (typisch AUTO); _runout_follow_active
            # Flag steuert die Distanz-Überwachung in _main_tick.
            self._respond("Runout — external sensor mode, %dmm follow" % int(self.runout_follow_mm))
            try:
                ps = self.printer.lookup_object('print_stats')
                self._runout_filament_ref = ps.get_status(eventtime).get('filament_used', 0.0)
            except Exception:
                self._runout_filament_ref = 0.0
            self._runout_follow_active = True

    # -----------------------------------------------------------------------
    # Button events
    # -----------------------------------------------------------------------

    def _on_button_change(self, button_name, pressed, eventtime):
        if pressed:
            self._button_held[button_name] = True
            self._on_button_press(button_name, eventtime)
        else:
            was_held = self._button_held[button_name]
            self._button_held[button_name] = False
            if was_held:
                self._on_button_release(button_name, eventtime)

    def _on_button_press(self, button_name, eventtime):
        # Block manual buttons during LOAD/UNLOAD/OVERFLOW/JAM.
        if self._state in (STATE_LOAD_PHASE_1, STATE_LOAD_PHASE_2, STATE_LOAD_PHASE_3,
                           STATE_UNLOAD_PHASE_1, STATE_UNLOAD_PHASE_2,
                           STATE_UNLOAD_PHASE_3, STATE_OVERFLOW, STATE_JAM,
                           STATE_INITIAL_GRIP):
            self._respond("Button ignored — state=%s" % self._state)
            return

        # MEASURE_LOAD toggle-mode overrides normal click logic on feed button.
        # _measure_feeding drives the toggle explicitly so a prior AUTO
        # bang-bang state does not pre-bias the first click.
        if button_name == BUTTON_FEED and self._measure_load_active:
            if self._measure_feeding:
                # 2nd click — stop.
                self._measure_feeding = False
                self._continuous_feed = False
                self._halt_motion()
                self._measure_report()
                self._measure_load_active = False
                self._set_state(STATE_IDLE)
            else:
                # 1st click — start.
                self._measure_feeding = True
                self._measure_load_distance = 0.0
                self._start_continuous_motion(+1, self.manual_speed, None)
                self._set_state(STATE_MANUAL_FEED)
                self._respond("MEASURE_LOAD: feeder running — click again to stop")
            return

        now = eventtime
        if (now - self._last_click_time[button_name]) > self.triple_click_window:
            self._click_count[button_name] = 1
        else:
            self._click_count[button_name] += 1
        self._last_click_time[button_name] = now

        cnt = self._click_count[button_name]
        if cnt == CLICK_SINGLE:
            self._action_manual_start(button_name)
        elif cnt == CLICK_DOUBLE:
            # Stop any ongoing manual, do a pulse.
            self._continuous_feed = False
            self._halt_motion()
            self._action_manual_pulse(button_name)
        elif cnt >= CLICK_TRIPLE:
            self._click_count[button_name] = 0
            self._continuous_feed = False
            self._halt_motion()
            if button_name == BUTTON_FEED and not self.feed_burst_enabled:
                # Feed burst disabled: restart manual run.
                self._action_manual_start(button_name)
            else:
                self._action_burst(button_name)

    def _on_button_release(self, button_name, eventtime):
        # Any release stops continuous motion (was started on single-click).
        if self._continuous_feed:
            desired_dir = +1 if button_name == BUTTON_FEED else -1
            if self._continuous_feed_direction == desired_dir and not self._measure_load_active:
                self._continuous_feed = False
                self._halt_motion()
                if self._state in (STATE_MANUAL_FEED, STATE_MANUAL_RETRACT):
                    self._start_cooldown()

    def _action_manual_start(self, button_name):
        direction = +1 if button_name == BUTTON_FEED else -1
        target_state = STATE_MANUAL_FEED if button_name == BUTTON_FEED else STATE_MANUAL_RETRACT
        self._start_continuous_motion(direction, self.manual_speed, None)
        self._set_state(target_state)
        self._respond("%s: Dauerlauf" % button_name)

    def _action_manual_pulse(self, button_name):
        direction = +1 if button_name == BUTTON_FEED else -1
        target_state = STATE_MANUAL_FEED if button_name == BUTTON_FEED else STATE_MANUAL_RETRACT
        self._set_state(target_state)
        self._submit_move(direction * self.manual_chunk_distance, self.manual_speed)
        self._schedule_return_to_auto_after_move()
        self._respond("%s: %d mm Puls" % (button_name, self.manual_chunk_distance))

    def _action_burst(self, button_name):
        direction = +1 if button_name == BUTTON_FEED else -1
        target_state = STATE_MANUAL_FEED if button_name == BUTTON_FEED else STATE_MANUAL_RETRACT
        self._set_state(target_state)
        self._submit_move(direction * self.burst_distance, self.burst_speed)
        self._schedule_return_to_auto_after_move(cooldown=self.reenable_cooldown_fast)
        self._respond("%s: Triple-Burst %d mm @ %d mm/s"
                      % (button_name, self.burst_distance, self.burst_speed))

    # -----------------------------------------------------------------------
    # Initial grip phase
    # -----------------------------------------------------------------------

    def _start_initial_grip(self, eventtime):
        self._enable_stepper()
        self._set_state(STATE_INITIAL_GRIP)
        distance = self.grip_speed * self.grip_duration
        self._respond("Initial grip: %.0f mm @ %.0f mm/s"
                      % (distance, self.grip_speed))
        self._submit_move(distance, self.grip_speed)
        # When move ends, main tick will transition to AUTO.
        self._initial_grip_end_time = self._last_move_end_time

    # -----------------------------------------------------------------------
    # Main tick — sensor debounce + bang-bang + state progression
    # -----------------------------------------------------------------------

    def _main_tick(self, eventtime):
        try:
            self._check_debounce(eventtime)

            # HALL1 has absolute priority.
            if self.hall_overflow and self._state != STATE_OVERFLOW:
                self._enter_overflow()
                return eventtime + MAIN_TICK_INTERVAL

            # Hard-safety aborts route through _trigger_jam so the
            # lockout is sticky: phase commands raise via WAIT_IDLE,
            # and recovery requires explicit BUFFER_CLEAR_JAM /
            # BUFFER_AUTO_OFF / STOP_BUFFER_FILL.
            if self._feed_deadline_time is not None and eventtime >= self._feed_deadline_time:
                self._feed_deadline_time = None
                self._trigger_jam(
                    "SAFETY_TIMEOUT",
                    "max_feed_time %ds reached without reaching HALL2" % int(self.max_feed_time))

            # max_feed_distance is a forward-feed safety only. Manual
            # retract (Retract-Taster Dauerlauf, BUFFER_RETRACT without
            # DISTANCE) legitimately accumulates large distances in the
            # opposite direction; tripping a JAM on those is a bug.
            if (self._continuous_feed
                    and self._continuous_feed_direction == 1
                    and self._feed_distance_accumulator >= self.max_feed_distance):
                self._trigger_jam(
                    "SAFETY_DISTANCE",
                    "max_feed_distance %dmm reached in one continuous feed" % int(self.max_feed_distance))

            # Cooldown end: back to AUTO if entrance present.
            if self._cooldown_deadline is not None and eventtime >= self._cooldown_deadline:
                self._cooldown_deadline = None
                if self._state in (STATE_MANUAL_FEED, STATE_MANUAL_RETRACT, STATE_INITIAL_GRIP):
                    if self.entrance_detected and not self.hall_overflow:
                        self._set_state(STATE_AUTO)
                    else:
                        self._set_state(STATE_IDLE)

            # Initial grip done -> AUTO.
            if self._state == STATE_INITIAL_GRIP and self._initial_grip_end_time is not None:
                mcu = self.stepper.get_mcu()
                now_pt = mcu.estimated_print_time(eventtime)
                if now_pt >= self._initial_grip_end_time:
                    self._initial_grip_end_time = None
                    self._set_state(STATE_AUTO)
                    self._respond("Initial grip done — AUTO engaged")
                    # Optional auto-LOAD if hotend warm.
                    if self.auto_load_after_follow and self._hotend_warm():
                        self._gcode_run_script("LOAD_FILAMENT")

            # Bang-bang in AUTO.
            if self._state == STATE_AUTO:
                self._bang_bang_tick(eventtime)

            # RUNOUT follow (runout_pause=0 mode): bang-bang keeps
            # running in AUTO; we just track extruder distance here.
            if self._runout_follow_active and self._runout_filament_ref is not None:
                try:
                    ps = self.printer.lookup_object('print_stats')
                    cur = ps.get_status(eventtime).get('filament_used', 0.0)
                    if cur - self._runout_filament_ref >= self.runout_follow_mm:
                        self._respond("Runout-follow %dmm reached — stepper off"
                                      % int(self.runout_follow_mm))
                        self._continuous_feed = False
                        self._halt_motion()
                        self._disable_stepper()
                        self._runout_filament_ref = None
                        self._runout_follow_active = False
                        self._set_state(STATE_IDLE)
                except Exception:
                    pass

            # LOAD Phase 3 — feed until HALL2 or max distance.
            if self._state == STATE_LOAD_PHASE_3:
                self._load_phase3_tick(eventtime)

            # Auto-return to IDLE after non-blocking phase moves end.
            # LOAD_PHASE_1 is synchronous and transitions itself.
            # LOAD/UNLOAD_PHASE_2 are non-blocking: the macro calls
            # BUFFER_WAIT_IDLE (which just waits on move_in_flight),
            # and main_tick finalizes the state here.
            if self._state in (STATE_LOAD_PHASE_2, STATE_UNLOAD_PHASE_2) and not self._move_in_flight():
                self._set_state(STATE_IDLE)

            # Continuous feed: keep chunks streaming.
            if self._continuous_feed and not self._move_in_flight():
                chunk_dist = max(self.manual_chunk_distance,
                                 self._continuous_feed_speed * 0.5)
                self._submit_move(self._continuous_feed_direction * chunk_dist,
                                  self._continuous_feed_speed)

        except Exception:
            logging.exception("buffer_feeder main_tick error")

        return eventtime + MAIN_TICK_INTERVAL

    def _bang_bang_tick(self, eventtime):
        """HALL-based bang-bang with hysteresis."""
        if self._bang_bang_suspended:
            # Print is paused — do nothing until idle_timeout:printing.
            return
        if self.hall_full:
            # Buffer voll: stop feeding.
            if self._continuous_feed:
                self._continuous_feed = False
                self._halt_motion()
        elif self.hall_empty:
            # Buffer leer: feed.
            if not self._continuous_feed:
                self._start_continuous_motion(+1, self.feed_speed, self.max_feed_time)
        else:
            # Zwischen-Zone: halte letzten Zustand (Hysterese).
            # Nichts tun — _continuous_feed bleibt wie es ist.
            pass

    def _load_phase3_tick(self, eventtime):
        if self.hall_full:
            self._continuous_feed = False
            self._halt_motion()
            self._respond("LOAD Phase 3: HALL2 reached, buffer full")
            self._set_state(STATE_AUTO)
            return
        if self._load_phase3_distance >= self._load_phase3_max_distance:
            self._continuous_feed = False
            self._halt_motion()
            self._respond("LOAD Phase 3: max_distance reached without HALL2 — check sensor",
                          force_display=True)
            self._set_state(STATE_AUTO)
            return
        if not self._move_in_flight():
            chunk = 10.0
            self._submit_move(chunk, self._load_phase3_speed)
            self._load_phase3_distance += chunk

    # -----------------------------------------------------------------------
    # Jam detection tick
    # -----------------------------------------------------------------------

    def _jam_tick(self, eventtime):
        try:
            if not self.jam_detection_enabled or self._jam_active:
                return eventtime + JAM_TICK_INTERVAL

            if self._state not in JAM_WATCH_STATES:
                # Reset trackers.
                self._hall2_start_time = None
                self._hall3_start_time = None
                return eventtime + JAM_TICK_INTERVAL

            # --- Jam-Typ 1: Nozzle-Clog (HALL2 stays active while extruding) ---
            if self.hall_full and not self.hall_empty:
                if self._hall2_start_time is None:
                    self._hall2_start_time = eventtime
                    self._hall2_start_extruder_pos = self._read_extruder_position()
                else:
                    dwell = eventtime - self._hall2_start_time
                    progress = self._read_extruder_position() - self._hall2_start_extruder_pos
                    if dwell >= self.jam_clog_dwell_time and progress >= self.jam_clog_extrude_min:
                        self._trigger_jam("CLOG",
                            "HALL2 active %.0fs, extruder +%.1fmm — nozzle clog suspected"
                            % (dwell, progress))
            else:
                self._hall2_start_time = None

            # --- Jam-Typ 2: Supply-Jam (HALL3 stays active while feeder running) ---
            feeder_running_fwd = self._continuous_feed and self._continuous_feed_direction == 1
            if self.hall_empty and feeder_running_fwd:
                if self._hall3_start_time is None:
                    self._hall3_start_time = eventtime
                else:
                    dwell = eventtime - self._hall3_start_time
                    if dwell >= self.jam_supply_dwell_time:
                        self._trigger_jam("SUPPLY",
                            "HALL3 active %.0fs with feeder running — spool/supply jam suspected"
                            % dwell)
            else:
                self._hall3_start_time = None
        except Exception:
            logging.exception("buffer_feeder jam_tick error")

        return eventtime + JAM_TICK_INTERVAL

    def _trigger_jam(self, kind, message):
        if self._jam_active:
            return
        self._jam_active = True
        self._respond("*** JAM %s: %s ***" % (kind, message), force_display=True)
        self._continuous_feed = False
        self._halt_motion()
        self._set_state(STATE_JAM)
        if self.jam_action:
            self._gcode_run_script(self.jam_action)

    def _read_extruder_position(self):
        try:
            ex = self.printer.lookup_object('extruder')
            return ex.last_position
        except Exception:
            return 0.0

    # -----------------------------------------------------------------------
    # Stepper control (flush-free move submit)
    # -----------------------------------------------------------------------

    def _enable_stepper(self):
        if self._stepper_enable is None:
            return
        try:
            mcu = self.stepper.get_mcu()
            now_pt = mcu.estimated_print_time(self.reactor.monotonic())
            self._stepper_enable.motor_enable(now_pt + self.lead_time)
        except Exception:
            logging.exception("buffer_feeder: enable_stepper failed")

    def _disable_stepper(self):
        if self._stepper_enable is None:
            return
        try:
            mcu = self.stepper.get_mcu()
            now_pt = mcu.estimated_print_time(self.reactor.monotonic())
            self._stepper_enable.motor_disable(now_pt + self.lead_time)
        except Exception:
            logging.exception("buffer_feeder: disable_stepper failed")

    def _submit_move(self, signed_distance, speed):
        """Submit a single trapezoidal move to our trapq. Flush-free."""
        if signed_distance == 0 or speed <= 0:
            return

        self._enable_stepper()

        mcu = self.stepper.get_mcu()
        now_pt = mcu.estimated_print_time(self.reactor.monotonic())
        t0 = max(now_pt + self.lead_time, self._last_move_end_time)

        distance = abs(signed_distance)
        direction = 1.0 if signed_distance > 0 else -1.0

        accel = self.accel
        # Trapezoidal profile: accel up to cruise_v, cruise, decel to 0.
        # accel_t = cruise_v / accel; accel_dist = 0.5 * accel_t * cruise_v
        cruise_v = speed
        accel_time = cruise_v / accel
        accel_dist = 0.5 * accel_time * cruise_v

        if distance < 2. * accel_dist:
            # Triangular profile — reduce peak velocity.
            cruise_v = math.sqrt(distance * accel)
            accel_time = cruise_v / accel
            accel_dist = 0.5 * accel_time * cruise_v
            cruise_time = 0.0
            decel_time = accel_time
            cruise_dist = 0.0
        else:
            cruise_dist = distance - 2. * accel_dist
            cruise_time = cruise_dist / cruise_v
            decel_time = accel_time

        start_pos_x = self._commanded_pos
        # Direction vector along our single axis 'x'
        axes_r_x = direction

        # trapq_append signature:
        # (trapq, print_time, accel_t, cruise_t, decel_t,
        #  start_x, start_y, start_z,
        #  axes_r_x, axes_r_y, axes_r_z,
        #  start_v, cruise_v, accel)
        self.trapq_append(self.trapq, t0,
                          accel_time, cruise_time, decel_time,
                          start_pos_x, 0., 0.,
                          axes_r_x, 0., 0.,
                          0., cruise_v, accel)

        total_time = accel_time + cruise_time + decel_time
        end_time = t0 + total_time
        self._last_move_end_time = end_time
        self._commanded_pos += direction * distance

        self._current_move = {
            'end_time': end_time,
            'direction': direction,
            'distance': distance,
            'speed': cruise_v,
        }

        self._feed_distance_accumulator += distance
        self._accumulated_feed_distance += distance

        # Measurement counter.
        if self._measure_load_active and direction > 0:
            self._measure_load_distance += distance

        # Kick background flusher.
        self.motion_queuing.note_mcu_movequeue_activity(end_time)

    def _move_in_flight(self):
        if self._current_move is None:
            return False
        mcu = self.stepper.get_mcu()
        now_pt = mcu.estimated_print_time(self.reactor.monotonic())
        return now_pt < self._current_move['end_time']

    def _halt_motion(self):
        """Stop the feeder at the next opportunity.

        We cannot abort a move in-flight on the trapq without a flush
        (which we refuse — that's the whole point of the architecture).
        Instead we: (a) stop submitting new chunks, (b) leave
        `_current_move` intact so `_move_in_flight` can still report
        accurately until the last submitted chunk plays out. For
        emergency stops, `_disable_stepper` is called separately to
        cut motor power on the MCU-level. Typical latency between
        halt request and actual motor-still: one chunk (≤0.5s at
        manual_speed) plus MCU step-queue depth (ms).

        Also clears `_feed_deadline_time` so a deadline that was
        armed for a since-finished continuous feed does not later
        trip SAFETY_TIMEOUT on a quiescent feeder.
        """
        self._continuous_feed = False
        self._feed_deadline_time = None

    def _start_continuous_motion(self, direction, speed, max_duration_s):
        self._continuous_feed = True
        self._continuous_feed_direction = direction
        self._continuous_feed_speed = speed
        self._feed_start_time = self.reactor.monotonic()
        self._feed_distance_accumulator = 0.0
        if max_duration_s is not None:
            self._feed_deadline_time = self.reactor.monotonic() + max_duration_s
        else:
            self._feed_deadline_time = None

    def _schedule_return_to_auto_after_move(self, cooldown=None):
        if cooldown is None:
            cooldown = self.reenable_cooldown
        # Use reactor-time-based cooldown starting after current move's end.
        # Approximation: now + move-duration + cooldown.
        delay = 0.1 + cooldown
        if self._current_move is not None:
            mcu = self.stepper.get_mcu()
            now_pt = mcu.estimated_print_time(self.reactor.monotonic())
            remaining = max(0.0, self._current_move['end_time'] - now_pt)
            delay = remaining + cooldown
        self._cooldown_deadline = self.reactor.monotonic() + delay

    def _start_cooldown(self):
        self._cooldown_deadline = self.reactor.monotonic() + self.reenable_cooldown

    # -----------------------------------------------------------------------
    # State management
    # -----------------------------------------------------------------------

    def _set_state(self, new_state):
        if new_state == self._state:
            return
        old = self._state
        self._state = new_state
        logging.info("buffer_feeder: %s -> %s", old, new_state)
        # Reset jam trackers on state exit.
        if old in JAM_WATCH_STATES and new_state not in JAM_WATCH_STATES:
            self._hall2_start_time = None
            self._hall3_start_time = None
        # IDLE semantic per spec/README: stopped AND disabled. Enforce.
        if new_state == STATE_IDLE:
            self._disable_stepper()

    # -----------------------------------------------------------------------
    # Helper: gcode interactions
    # -----------------------------------------------------------------------

    def _gcode_run_script(self, script):
        try:
            self.printer.lookup_object('gcode').run_script(script)
        except Exception:
            logging.exception("buffer_feeder: gcode run_script failed (%s)", script)

    def _respond(self, message, force_display=False):
        logging.info("buffer_feeder: %s", message)
        try:
            gc = self.printer.lookup_object('gcode')
            gc.respond_info("BufferFeeder: %s" % message)
            if self.display_status_enabled or force_display:
                gc.run_script("M117 %s" % message[:20])  # display is short
        except Exception:
            pass

    def _hotend_warm(self):
        try:
            ex = self.printer.lookup_object('extruder')
            return ex.get_heater().get_temp(self.reactor.monotonic())[0] >= self.min_temp
        except Exception:
            return False

    def _measure_report(self):
        self._respond("MEASURE_LOAD result: %.1f mm" % self._measure_load_distance)

    # -----------------------------------------------------------------------
    # GCode command implementations
    # -----------------------------------------------------------------------

    cmd_BUFFER_FEED_help = "Feed filament forward. DISTANCE=mm SPEED=mm/s TIMEOUT=s (no DISTANCE => continuous)"
    def cmd_BUFFER_FEED(self, gcmd):
        distance = gcmd.get_float('DISTANCE', 0., minval=0.)
        speed    = gcmd.get_float('SPEED',    self.manual_speed, above=0.)
        timeout  = gcmd.get_float('TIMEOUT',  self.max_feed_time, above=0.)
        self._cmd_feed_common(+1, distance, speed, timeout)

    cmd_BUFFER_RETRACT_help = "Retract filament. DISTANCE=mm SPEED=mm/s TIMEOUT=s"
    def cmd_BUFFER_RETRACT(self, gcmd):
        distance = gcmd.get_float('DISTANCE', 0., minval=0.)
        speed    = gcmd.get_float('SPEED',    self.manual_speed, above=0.)
        timeout  = gcmd.get_float('TIMEOUT',  self.max_feed_time, above=0.)
        self._cmd_feed_common(-1, distance, speed, timeout)

    def _cmd_feed_common(self, direction, distance, speed, timeout):
        if self._state in (STATE_OVERFLOW, STATE_JAM):
            raise self._cmd_error("BufferFeeder: state=%s blocks feed" % self._state)
        if self._state in BUSY_PHASE_STATES:
            raise self._cmd_error(
                "BufferFeeder: busy (state=%s) — call STOP_BUFFER_FILL "
                "or wait for LOAD/UNLOAD to finish" % self._state)
        # Fresh manual command = operator acknowledges any stale HALT.
        self._halt_requested = False
        if distance > 0:
            if distance > self.max_feed_distance:
                raise self._cmd_error("DISTANCE exceeds max_feed_distance=%.0f"
                                      % self.max_feed_distance)
            target_state = STATE_MANUAL_FEED if direction > 0 else STATE_MANUAL_RETRACT
            self._set_state(target_state)
            self._submit_move(direction * distance, speed)
            self._schedule_return_to_auto_after_move()
        else:
            target_state = STATE_MANUAL_FEED if direction > 0 else STATE_MANUAL_RETRACT
            self._set_state(target_state)
            self._start_continuous_motion(direction, speed, timeout)

    cmd_BUFFER_HALT_help = "Immediately stop any feeder motion (sticky — aborts active workflow)"
    def cmd_BUFFER_HALT(self, gcmd):
        # Halt must be sticky across AUTO / INITIAL_GRIP / LOAD_PHASE_3
        # (which would otherwise re-submit chunks from the tick loop)
        # AND across any non-locked state so an ongoing LOAD_FILAMENT /
        # UNLOAD_FILAMENT macro aborts instead of silently continuing.
        self._continuous_feed = False
        self._halt_motion()
        # Clear runout-follow so a lingering follow timer doesn't
        # later disable the stepper mid-operation after a new workflow
        # has already started.
        self._runout_follow_active = False
        self._runout_filament_ref = None
        # Preserve safety-lockout states (OVERFLOW / JAM); any other
        # state drops to IDLE. Our _set_state(STATE_IDLE) hook also
        # disables the stepper.
        if self._state not in (STATE_OVERFLOW, STATE_JAM):
            self._set_state(STATE_IDLE)
        # Arm the abort flag so any pending WAIT_IDLE in a macro
        # propagates the halt as a Klipper error.
        self._halt_requested = True
        self._respond("HALT — workflow will abort at next wait")

    cmd_BUFFER_AUTO_ON_help = "Enable bang-bang auto mode"
    def cmd_BUFFER_AUTO_ON(self, gcmd):
        if self.hall_overflow or self._state == STATE_OVERFLOW:
            raise self._cmd_error("Cannot enable AUTO while HALL1 overflow active")
        if self._state == STATE_JAM or self._jam_active:
            raise self._cmd_error(
                "Cannot enable AUTO while JAM active. Inspect and call "
                "BUFFER_CLEAR_JAM to resume, or BUFFER_AUTO_OFF first.")
        if self._state in BUSY_PHASE_STATES:
            raise self._cmd_error(
                "Cannot enable AUTO while LOAD/UNLOAD in progress (state=%s). "
                "Call STOP_BUFFER_FILL to abort first." % self._state)
        # Clear pending HALT flag + any print-pause suspension — user
        # is explicitly starting fresh.
        self._halt_requested = False
        self._bang_bang_suspended = False
        self._enable_stepper()
        self._set_state(STATE_AUTO)
        self._respond("AUTO engaged")

    cmd_BUFFER_AUTO_OFF_help = "Disable bang-bang auto mode (also clears JAM/runout-follow)"
    def cmd_BUFFER_AUTO_OFF(self, gcmd):
        # Full-reset semantic: AUTO_OFF is the operator's "stop
        # everything and acknowledge" lever. Clear ALL recovery and
        # modal flags so the system is in a clean IDLE state.
        self._continuous_feed = False
        self._halt_motion()
        self._jam_active = False
        self._hall2_start_time = None
        self._hall3_start_time = None
        self._runout_follow_active = False
        self._runout_filament_ref = None
        self._halt_requested = False
        self._bang_bang_suspended = False
        self._measure_load_active = False
        self._measure_feeding = False
        self._set_state(STATE_IDLE)
        self._respond("AUTO off (all recovery flags cleared)")

    cmd_BUFFER_WAIT_IDLE_help = "Block until the feeder's current move is complete"
    def cmd_BUFFER_WAIT_IDLE(self, gcmd):
        # Wait strictly for any in-flight feeder move to finish.
        # State transitions are the responsibility of the invoking
        # command (LOAD/UNLOAD phase commands transition back to IDLE
        # themselves after calling this).
        while self._move_in_flight():
            eventtime = self.reactor.monotonic()
            self.reactor.pause(eventtime + 0.05)
        # If a safety lockout tripped while we were waiting, abort the
        # caller. The Klipper error propagates up and halts the macro,
        # preventing subsequent phases from running.
        self._raise_if_locked_out(gcmd)

    cmd_BUFFER_LOAD_PHASE1_help = "LOAD Phase 1 — feeder alone fast to toolhead. DISTANCE=mm"
    def cmd_BUFFER_LOAD_PHASE1(self, gcmd):
        self._halt_requested = False    # ack any stale console HALT
        self._raise_if_locked_out(gcmd)
        distance = gcmd.get_float('DISTANCE', self.load_fast_distance, above=0.)
        speed    = gcmd.get_float('SPEED',    self.load_fast_speed,    above=0.)
        self._set_state(STATE_LOAD_PHASE_1)
        self._enable_stepper()
        self._submit_move(+distance, speed)
        # Blocking: wait until done. WAIT_IDLE raises on OVERFLOW/JAM.
        self.cmd_BUFFER_WAIT_IDLE(gcmd)
        self._set_state(STATE_IDLE)

    cmd_BUFFER_LOAD_PHASE2_help = "LOAD Phase 2 — feeder parallel to extruder. Non-blocking, use BUFFER_WAIT_IDLE"
    def cmd_BUFFER_LOAD_PHASE2(self, gcmd):
        self._halt_requested = False
        self._raise_if_locked_out(gcmd)
        distance = gcmd.get_float('DISTANCE', self.load_slow_distance, above=0.)
        speed    = gcmd.get_float('SPEED',    self.load_slow_speed,    above=0.)
        self._set_state(STATE_LOAD_PHASE_2)
        self._enable_stepper()
        self._submit_move(+distance, speed)

    cmd_BUFFER_LOAD_PHASE3_help = "LOAD Phase 3 — feed until HALL2 or MAX_DISTANCE"
    def cmd_BUFFER_LOAD_PHASE3(self, gcmd):
        self._halt_requested = False
        self._raise_if_locked_out(gcmd)
        max_distance = gcmd.get_float('MAX_DISTANCE', self.load_buffer_max, above=0.)
        speed        = gcmd.get_float('SPEED',        self.feed_speed,      above=0.)
        self._load_phase3_distance = 0.0
        self._load_phase3_max_distance = max_distance
        self._load_phase3_speed = speed
        self._enable_stepper()
        self._set_state(STATE_LOAD_PHASE_3)
        self._start_continuous_motion(+1, speed, self.max_feed_time)
        # Block until the tick-driven state machine exits STATE_LOAD_PHASE_3.
        while self._state == STATE_LOAD_PHASE_3:
            self.reactor.pause(self.reactor.monotonic() + 0.1)
        # Exit reason might be normal (AUTO) or safety lockout — check.
        self._raise_if_locked_out(gcmd)

    cmd_BUFFER_UNLOAD_PHASE1_help = ("UNLOAD Phase 1 — halt feeder and lock state for tip-forming "
                                     "(so buttons / FORCE_BUFFER_FILL don't interfere)")
    def cmd_BUFFER_UNLOAD_PHASE1(self, gcmd):
        self._halt_requested = False
        self._raise_if_locked_out(gcmd)
        # Tip-Forming runs on the extruder alone. Feeder must stand
        # still, and the state must NOT be IDLE — otherwise manual
        # buttons and FORCE_BUFFER_FILL would accept input and stomp
        # the unload sequence. STATE_UNLOAD_PHASE_1 is one of the
        # phase states blocked by the button handler.
        self._continuous_feed = False
        self._halt_motion()
        self._set_state(STATE_UNLOAD_PHASE_1)
        # Block until any in-flight chunk has finished. Otherwise
        # tip-forming Push/Pull on the extruder could overlap with
        # residual feeder motion (up to one chunk + MCU queue depth).
        # _halt_motion does NOT truncate the trapq; the last-submitted
        # chunk still plays out. BUFFER_WAIT_IDLE does the right
        # thing (and raises on OVERFLOW/JAM).
        self.cmd_BUFFER_WAIT_IDLE(gcmd)
        self._disable_stepper()
        self._respond("UNLOAD Phase 1: feeder halted for tip-forming")

    cmd_BUFFER_UNLOAD_PHASE2_help = "UNLOAD Phase 2 — feeder retract parallel to extruder"
    def cmd_BUFFER_UNLOAD_PHASE2(self, gcmd):
        self._halt_requested = False
        self._raise_if_locked_out(gcmd)
        distance = gcmd.get_float('DISTANCE', self.unload_sync_distance, above=0.)
        speed    = gcmd.get_float('SPEED',    self.unload_fast_speed,    above=0.)
        self._set_state(STATE_UNLOAD_PHASE_2)
        self._enable_stepper()
        self._submit_move(-distance, speed)

    cmd_BUFFER_UNLOAD_PHASE3_help = "UNLOAD Phase 3 — chunked retract until entrance free"
    def cmd_BUFFER_UNLOAD_PHASE3(self, gcmd):
        self._halt_requested = False
        self._raise_if_locked_out(gcmd)
        max_distance = gcmd.get_float('MAX_DISTANCE', self.unload_fast_max, above=0.)
        speed        = gcmd.get_float('SPEED',        self.unload_fast_speed, above=0.)
        chunk        = 50.0
        self._set_state(STATE_UNLOAD_PHASE_3)
        self._enable_stepper()
        retracted = 0.0
        while retracted < max_distance:
            # Abort immediately on lockout (WAIT_IDLE also catches, but
            # check before the next submit to avoid a race).
            self._raise_if_locked_out(gcmd)
            if not self.entrance_detected:
                self._respond("UNLOAD Phase 3: entrance clear after %.0f mm" % retracted)
                break
            self._submit_move(-chunk, speed)
            retracted += chunk
            # WAIT_IDLE raises if OVERFLOW/JAM happens during this chunk.
            self.cmd_BUFFER_WAIT_IDLE(gcmd)
        else:
            self._respond("UNLOAD Phase 3: MAX_DISTANCE reached without entrance clear",
                          force_display=True)
        self._disable_stepper()
        self._set_state(STATE_IDLE)

    cmd_FORCE_BUFFER_FILL_help = "Manually trigger initial grip + fill cycle"
    def cmd_FORCE_BUFFER_FILL(self, gcmd):
        if not self.entrance_detected:
            raise self._cmd_error("FORCE_BUFFER_FILL aborted: no filament at entrance")
        if self.hall_overflow or self._state == STATE_OVERFLOW:
            raise self._cmd_error("FORCE_BUFFER_FILL aborted: HALL1 overflow active")
        if self._state == STATE_JAM or self._jam_active:
            raise self._cmd_error("FORCE_BUFFER_FILL aborted: JAM active. Use BUFFER_CLEAR_JAM first.")
        # State guard per spec §5: only valid transition from IDLE
        # or RUNOUT into INITIAL_GRIP. Reject otherwise — accidentally
        # re-entering while AUTO / MANUAL_* / a LOAD-UNLOAD phase is
        # running would stomp over the active motion, and in
        # LOAD_PHASE_3 would pop the blocking caller out of its loop
        # into a surprise grip move.
        if self._state not in (STATE_IDLE, STATE_RUNOUT):
            raise self._cmd_error(
                "FORCE_BUFFER_FILL aborted: feeder busy (state=%s). "
                "Call STOP_BUFFER_FILL or BUFFER_AUTO_OFF first."
                % self._state)
        self._start_initial_grip(self.reactor.monotonic())

    cmd_STOP_BUFFER_FILL_help = "Abort any ongoing fill/grip/manual and return to IDLE"
    def cmd_STOP_BUFFER_FILL(self, gcmd):
        # Full-reset semantic: STOP_BUFFER_FILL aborts everything and
        # clears recovery flags so we land in a clean IDLE state.
        # Like BUFFER_HALT, this also arms _halt_requested so any
        # macro waiting on BUFFER_WAIT_IDLE raises and aborts rather
        # than silently continuing to the next phase.
        self._continuous_feed = False
        self._halt_motion()
        self._initial_grip_end_time = None
        self._load_phase3_distance = 0.0
        self._measure_load_active = False
        self._measure_feeding = False
        self._jam_active = False
        self._hall2_start_time = None
        self._hall3_start_time = None
        self._runout_follow_active = False
        self._runout_filament_ref = None
        self._bang_bang_suspended = False
        self._halt_requested = True
        self._set_state(STATE_IDLE)
        self._respond("All feed loops stopped (workflow will abort at next wait)")

    cmd_BUFFER_STATE_DUMP_help = "Dump full buffer_feeder state to console"
    def cmd_BUFFER_STATE_DUMP(self, gcmd):
        lines = [
            "---- BUFFER STATE ----",
            "state              = %s" % self._state,
            "hall_empty (HALL3) = %s" % self.hall_empty,
            "hall_full  (HALL2) = %s" % self.hall_full,
            "hall_overflow(HALL1)= %s" % self.hall_overflow,
            "entrance_detected  = %s" % self.entrance_detected,
            "feed_button        = %s" % self.feed_button_pressed,
            "retract_button     = %s" % self.retract_button_pressed,
            "continuous_feed    = %s dir=%d" % (self._continuous_feed,
                                                self._continuous_feed_direction),
            "feed_distance_acc  = %.1f mm" % self._feed_distance_accumulator,
            "accumulated total  = %.1f mm" % self._accumulated_feed_distance,
            "commanded_pos      = %.1f mm" % self._commanded_pos,
            "print_running      = %s" % self._print_running,
            "jam_active         = %s" % self._jam_active,
            "measure_load       = active=%s dist=%.1f mm" % (self._measure_load_active,
                                                              self._measure_load_distance),
            "click_count        = feed=%d retract=%d" % (self._click_count[BUTTON_FEED],
                                                         self._click_count[BUTTON_RETRACT]),
            "---- END STATE ----",
        ]
        gc = self.printer.lookup_object('gcode')
        for line in lines:
            gc.respond_info(line)

    cmd_CALIBRATE_FEEDER_SYNC_help = ("No-op under python-ansatz — feeder is not synced "
                                      "to extruder. Use MEASURE_LOAD_START for distance calibration.")
    def cmd_CALIBRATE_FEEDER_SYNC(self, gcmd):
        gc = self.printer.lookup_object('gcode')
        gc.respond_info(
            "CALIBRATE_FEEDER_SYNC: not applicable in python-ansatz.\n"
            "The feeder is decoupled from the extruder — no rotation_distance\n"
            "modulation to calibrate. For distance-per-revolution accuracy,\n"
            "use MEASURE_LOAD_START, feed a known amount, verify at the feeder."
        )

    cmd_MEASURE_LOAD_START_help = "Start MEASURE_LOAD toggle mode — feed button toggles feeder"
    def cmd_MEASURE_LOAD_START(self, gcmd):
        if self._state not in (STATE_IDLE, STATE_AUTO):
            raise self._cmd_error("MEASURE_LOAD_START requires IDLE or AUTO state")
        # If AUTO was already actively feeding (HALL3-triggered
        # bang-bang), stop it and reset to IDLE so the first button
        # press is unambiguously "start measurement feed".
        self._continuous_feed = False
        self._halt_motion()
        self._set_state(STATE_IDLE)
        self._measure_load_active = True
        self._measure_feeding = False
        self._measure_load_distance = 0.0
        self._respond("MEASURE_LOAD active — press feed button to start/stop")

    cmd_MEASURE_LOAD_STOP_help = "Stop MEASURE_LOAD mode and print distance"
    def cmd_MEASURE_LOAD_STOP(self, gcmd):
        self._continuous_feed = False
        self._halt_motion()
        self._measure_report()
        self._measure_load_active = False
        self._measure_feeding = False
        # Always return to IDLE — the operator can explicitly
        # BUFFER_AUTO_ON again if they want the bang-bang loop back.
        self._set_state(STATE_IDLE)

    def cmd_ENABLE_RUNOUT_SENSOR(self, gcmd):
        self._print_running = True
        self._respond("print_running=1 (runout PAUSE will fire)")

    def cmd_DISABLE_RUNOUT_SENSOR(self, gcmd):
        self._print_running = False
        self._respond("print_running=0 (runout PAUSE suppressed)")

    def cmd_BUFFER_CLEAR_JAM(self, gcmd):
        if self._state != STATE_JAM:
            raise self._cmd_error("Not in JAM state (state=%s)" % self._state)
        self._jam_active = False
        self._hall2_start_time = None
        self._hall3_start_time = None
        self._halt_requested = False
        self._set_state(STATE_IDLE if not self.entrance_detected else STATE_AUTO)
        self._respond("JAM cleared — state=%s" % self._state)

    # -----------------------------------------------------------------------
    # Utilities
    # -----------------------------------------------------------------------

    def _cmd_error(self, msg):
        gc = self.printer.lookup_object('gcode')
        return gc.error(msg)

    def _raise_if_locked_out(self, gcmd=None):
        """Abort a caller if the feeder is in a safety lockout.

        Called from blocking phase commands and from BUFFER_WAIT_IDLE so
        that OVERFLOW / JAM / user-abort events propagate out of macros
        as errors rather than silently letting the macro run into the
        next phase.

        _halt_requested auto-clears after raising so that the next
        command issued by the operator starts from a clean slate.
        """
        if self._halt_requested:
            self._halt_requested = False
            raise self._cmd_error("BufferFeeder: HALT requested — aborting workflow")
        if self._state == STATE_OVERFLOW:
            raise self._cmd_error("BufferFeeder: HALL1 OVERFLOW active — aborting. Clear overflow, then retry.")
        if self._state == STATE_JAM or self._jam_active:
            raise self._cmd_error("BufferFeeder: JAM active — aborting. Use BUFFER_CLEAR_JAM after inspection.")

    # -----------------------------------------------------------------------
    # Status API
    # -----------------------------------------------------------------------

    def get_status(self, eventtime):
        return {
            # Live state
            'state':                    self._state,
            'hall_empty':               self.hall_empty,
            'hall_full':                self.hall_full,
            'hall_overflow':            self.hall_overflow,
            'entrance_detected':        self.entrance_detected,
            'feed_button_pressed':      self.feed_button_pressed,
            'retract_button_pressed':   self.retract_button_pressed,
            'continuous_feed':          self._continuous_feed,
            'feed_direction':           self._continuous_feed_direction,
            'feed_distance_acc_mm':     self._feed_distance_accumulator,
            'total_accumulated_mm':     self._accumulated_feed_distance,
            'commanded_pos_mm':         self._commanded_pos,
            'print_running':            self._print_running,
            'jam_active':               self._jam_active,
            'bang_bang_suspended':      self._bang_bang_suspended,
            'halt_requested':           self._halt_requested,
            'runout_follow_active':     self._runout_follow_active,
            'measure_load_active':      self._measure_load_active,
            'measure_load_distance_mm': self._measure_load_distance,
            # Config values (exposed so LOAD/UNLOAD macros don't hardcode)
            'feed_speed':               self.feed_speed,
            'manual_speed':             self.manual_speed,
            'load_fast_speed':          self.load_fast_speed,
            'load_slow_speed':          self.load_slow_speed,
            'unload_fast_speed':        self.unload_fast_speed,
            'load_fast_distance':       self.load_fast_distance,
            'load_slow_distance':       self.load_slow_distance,
            'load_buffer_max':          self.load_buffer_max,
            'unload_sync_distance':     self.unload_sync_distance,
            'unload_fast_max':          self.unload_fast_max,
            'min_temp':                 self.min_temp,
            'accel':                    self.accel,
        }


# ---------------------------------------------------------------------------
# Config hook
# ---------------------------------------------------------------------------

def load_config_prefix(config):
    return BufferFeeder(config)
