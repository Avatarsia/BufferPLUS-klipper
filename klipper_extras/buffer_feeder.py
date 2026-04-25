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

# States where the main_tick continuous-feed chunk-pump is allowed
# to run. In any other state, a stale _continuous_feed must NOT
# cause new chunks to be submitted — otherwise a previously-active
# bang-bang or manual dauerfeed leaks into subsequent phases.
CONTINUOUS_FEED_STATES = {STATE_AUTO, STATE_MANUAL_FEED,
                          STATE_MANUAL_RETRACT, STATE_LOAD_PHASE_3,
                          STATE_INITIAL_GRIP}

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
        # Caps pending trapq time for any single move-submit. Long
        # BUFFER_FEED DISTANCE=1000 style moves get chunked so that
        # HALT / OVERFLOW can take effect within one chunk instead
        # of waiting out the full nominal move duration.
        self.max_move_chunk_mm  = config.getfloat('max_move_chunk_mm',  50.0,  above=0.)

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
        # Make motion_queuing recompute step-generation scan windows
        # with our new stepper. Without this call, the internal
        # step-gen timing budget doesn't account for our stepper and
        # the first generated step lands past the MCU deadline,
        # triggering "Timer too close" on the LLL_PLUS MCU at boot.
        # Same pattern Klipper's kinematics/extruder.py uses in
        # sync_to_extruder after changing trapq bindings.
        self.motion_queuing.check_step_generation_scan_windows()

        # Stepper position tracking (mm)
        self._commanded_pos = 0.0          # head of planned moves (end)
        self._last_move_end_time = 0.0     # print_time at which current/last move ends
        self._current_move = None          # dict with end_time, direction, distance_left
        self._feed_distance_accumulator = 0.0  # for safety max_feed_distance
        self._feed_start_time = None       # reactor time when continuous feed started
        self._accumulated_feed_distance = 0.0  # lifetime counter
        # stepcompress for a stepper starts with last_step_clock=0 and stays
        # there until the first step. On a printer that idles for long
        # enough (>~17s — Klipper's CLOCK_DIFF_MAX), the first step's clock
        # exceeds uint32_t and stepcompress emits an invalid queue_step
        # ("stepcompress o=X i=... c=... a=...: Invalid sequence" → MCU
        # shutdown). Prime the stepper once before the first real move by
        # calling stepper.set_position — same pattern force_move.manual_move
        # uses. Flag gates it so we do it exactly once.
        self._stepcompress_primed = False
        # Monotonic clock tracker for motor_enable/disable scheduling.
        # queue_digital_out commands on the same pin MUST be scheduled
        # with strictly non-decreasing MCU clocks: a disable at clock A
        # followed by an enable at clock B < A makes the MCU re-schedule
        # the second toggle in the past ("Timer too close" → shutdown).
        # This happened in the 2026-04-24 HALL1-during-pause crash, where
        # _disable_stepper and a subsequent _enable_stepper used slightly
        # different time bases (est_print_time vs. toolhead print_time)
        # and clock-sync jitter during the stall caused a regression.
        self._last_enable_schedule_time = 0.0

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
        # Initial stable-state defaults — SAFETY-FIRST assumption:
        # Klipper's buttons.register_buttons only fires initial callbacks
        # for pins whose logical state != 0 at boot (last_button starts
        # at 0, changed = new XOR 0). For pins that are in their "idle"
        # logical state at boot, NO callback is delivered until the
        # state actually changes.
        #
        # Consequence: if we defaulted to "inactive" and a HALL sensor
        # was already physically active (e.g. HALL2 blocked because the
        # buffer is already full at Klipper restart), we would never hear
        # about it — and bang-bang would happily keep filling until
        # overflow / safety-timeout.
        #
        # Fix: default HALLs to "active" (stable=False → semantic=True),
        # triggering OVERFLOW lockout at boot. As soon as Klipper
        # delivers an initial callback for pins that are actually idle
        # (the common case for HALL1), we transition out of lockout.
        # Entrance and buttons default to "not present / not pressed"
        # — that is the actual idle state for those switches, and
        # initial-insert events are further suppressed by the
        # _startup_grace_done gate below.
        for name, flip in self._pin_polarity_flip.items():
            if flip:
                # HALL sensors: start "active" (raw=False → semantic=True)
                idle_raw = False
            else:
                # Entrance / buttons: start "not active" (raw=False → semantic=False)
                idle_raw = False
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

        # Pending-chunk streaming for single-shot moves larger than
        # max_move_chunk_mm. _submit_move submits the first chunk
        # synchronously and records the remaining distance here;
        # main_tick submits subsequent chunks as prior ones approach
        # completion. This keeps _last_move_end_time bounded to
        # roughly 1.5 chunks ahead, so HALT/OVERFLOW stop remaining
        # chunks from ever being queued to the MCU.
        self._pending_remaining_mm = 0.0
        self._pending_direction = 0.0
        self._pending_speed = 0.0
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
        # Armed when STATE_RUNOUT was cleared via entrance-reinsert
        # during a paused print. Lets _on_idle_printing distinguish
        # "RESUME after runout_pause=1 reinsert" (trigger grip+fill)
        # from "any other RESUME with IDLE state" (leave as-is).
        self._runout_recovery_pending = False

        # ----- Cooldown timer -----
        self._cooldown_deadline = None    # reactor time after which auto re-enables
        # When True, the cooldown-after-manual-move flow returns to
        # IDLE instead of AUTO. Set by BUFFER_AUTO_OFF / STOP_BUFFER_FILL,
        # cleared by BUFFER_AUTO_ON. Respects the user's explicit
        # "bang-bang off" choice even after manual calibration moves.
        self._auto_off_by_user = False

        # ----- Startup grace period -----
        # During the first _startup_grace_seconds after klippy:ready,
        # sensor callbacks silently update stable_states but do NOT
        # fire insert/overflow/etc. events. Prevents spurious OVERFLOW
        # if HALL1's "idle" callback arrives just after main_tick
        # already called _enter_overflow.
        self._startup_grace_seconds = 2.0
        self._startup_grace_done = False

        # Edge-triggered auto-grip on entrance insert.
        # Time-based grace alone is unreliable here because Klipper's
        # MCU button query for the entrance pin can land after our
        # 2s grace window. Instead we gate auto-grip on HAVING SEEN
        # the entrance-False state at some point first. Boot with
        # filament already present → only ever True callbacks arrive
        # → flag stays False → no grip. User pulls filament → False
        # → flag becomes True → re-insert → grip. Clean edge detect.
        self._entrance_was_empty = False

        # ----- Macro gcode-state tracking -----
        # Klipper's SAVE_GCODE_STATE writes a saved_states entry;
        # RESTORE_GCODE_STATE reads but does NOT delete. Without our
        # own flag, a successful LOAD/UNLOAD restore leaves a valid
        # slot behind, and the next _try_restore_gcode_state() (on a
        # completely unrelated AUTO_OFF or CLEAR_JAM) would re-apply
        # an ancient state. The flag is set by BUFFER_SAVE_MACRO_STATE
        # and cleared after any restore succeeds.
        self._macro_state_saved = False

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
        gcode.register_command('BUFFER_RESTORE_STATE',
                               self.cmd_BUFFER_RESTORE_STATE,
                               desc="Best-effort restore of gcode-state saved by a failed LOAD/UNLOAD")
        gcode.register_command('BUFFER_SAVE_MACRO_STATE',
                               self.cmd_BUFFER_SAVE_MACRO_STATE,
                               desc="Internal: mark gcode-state as saved (used by _SAVE_E_MODE)")
        gcode.register_command('BUFFER_RESTORE_MACRO_STATE',
                               self.cmd_BUFFER_RESTORE_MACRO_STATE,
                               desc="Internal: restore + clear gcode-state save (used by _RESTORE_E_MODE)")

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
        # Anchor _last_move_end_time to the toolhead's current print_time
        # rather than mcu.estimated_print_time. The two diverge after
        # long idle periods, and our submissions must live in the same
        # print-time space that stepcompress anchors against (which
        # only advances via toolhead-driven flushes).
        toolhead = self.printer.lookup_object('toolhead')
        self._last_move_end_time = toolhead.get_last_move_time() + self.lead_time

        # Stay in STATE_INIT during startup grace. Bang-bang, insert
        # handling and OVERFLOW transitions are all gated on the grace
        # period — the first 2s just passively accumulate sensor
        # callbacks so we learn the real hardware state without
        # acting on boot-time edges.

        # Start reactor timers. Main tick silently updates debounce;
        # all higher-level logic (bang-bang, phase ticks, continuous
        # feed, safety) early-exits while _startup_grace_done is False.
        self._main_timer = self.reactor.register_timer(self._main_tick,
                                                       self.reactor.NOW)
        self._jam_timer = self.reactor.register_timer(self._jam_tick,
                                                      self.reactor.NOW)
        # Schedule grace-period completion.
        self.reactor.register_callback(
            self._end_startup_grace,
            self.reactor.monotonic() + self._startup_grace_seconds)
        self._respond("BufferFeeder: ready — entering %.1fs sensor-settle grace" %
                      self._startup_grace_seconds)

    def _end_startup_grace(self, eventtime):
        self._startup_grace_done = True
        # Log the settled sensor picture so operators can sanity-check
        # polarity against physical reality on a fresh boot.
        self._respond(
            "Startup grace done — hall_empty=%s hall_full=%s "
            "hall_overflow=%s entrance=%s"
            % (self.hall_empty, self.hall_full,
               self.hall_overflow, self.entrance_detected),
            force_display=True)
        # Drop into normal operation. If HALL1 is currently active,
        # main_tick will immediately transition to OVERFLOW.
        self._set_state(STATE_IDLE)

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
        jam_recovery = self._state == STATE_JAM or self._jam_active
        if jam_recovery:
            self._respond("RESUME: clearing JAM lockout")
            self._jam_active = False
            self._hall2_start_time = None
            self._hall3_start_time = None
            # If the jam interrupted a LOAD/UNLOAD macro mid-flight,
            # the macro's SAVE_GCODE_STATE is still pending. Restore
            # it now so the user's E-mode isn't stuck on M83 after
            # RESUME. Parity with BUFFER_CLEAR_JAM's recovery path.
            self._try_restore_gcode_state()
            if self.hall_overflow:
                # Cannot resume while overflow physically present.
                self._enter_overflow()
            elif self.entrance_detected:
                self._enable_stepper()
                self._set_state(STATE_AUTO)
            else:
                self._set_state(STATE_IDLE)
            return

        # RUNOUT-recovery path (runout_pause=1 case):
        #   Runout → STATE_RUNOUT + PAUSE → idle_timeout:ready armed
        #       _bang_bang_suspended.
        #   Reinsert during RUNOUT → STATE_IDLE + _runout_recovery_pending.
        #   RESUME: if the flag is armed AND filament is still at the
        #       entrance AND no operator-control flag is set, run
        #       grip+fill so the buffer is full before the print
        #       resumes actual extrusion.
        #
        # Gated by _runout_recovery_pending so that RESUME for other
        # idle-state reasons (MEASURE_LOAD_STOP, an idle console
        # session, BUFFER_HALT drop-to-IDLE, AUTO_OFF) does NOT queue
        # surprise grip motion. Respects _halt_requested for the same
        # reason. Flag is consumed by the grip or by any subsequent
        # state change away from IDLE.
        if (self._runout_recovery_pending
                and self._state == STATE_IDLE
                and self.entrance_detected
                and not self._auto_off_by_user
                and not self._halt_requested
                and not self.hall_overflow):
            self._runout_recovery_pending = False
            self._respond("RESUME after runout-reinsert — starting grip + fill")
            self._start_initial_grip(self.reactor.monotonic())

    def _on_idle_ready(self, *args):
        # Treat any transition out of an active print (PAUSE, jam-PAUSE,
        # print end) as bang-bang-suspension. The flag is cleared on
        # idle_timeout:printing (next RESUME or new print). Condition
        # is independent of current state: during a jam we're in
        # STATE_JAM, and BUFFER_CLEAR_JAM would otherwise re-start
        # bang-bang while the print is still paused. Manual
        # BUFFER_AUTO_ON from an idle console never sees
        # _print_running=True, so this does not affect user-initiated
        # AUTO during idle.
        if self._print_running:
            self._bang_bang_suspended = True
            if self._continuous_feed:
                self._continuous_feed = False
                self._halt_motion()
            self._respond("Print paused — bang-bang suspended until RESUME")
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
        if not self._startup_grace_done:
            # During startup grace: state was already updated by
            # _check_debounce; swallow the event so we don't fire
            # insert / overflow / etc. handlers based on initial
            # sensor readouts. After grace, main_tick and regular
            # event paths handle the settled state normally.
            return
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
        # Go to IDLE first (the _set_state hook disables the stepper).
        self._set_state(STATE_IDLE)
        # Re-arm bang-bang ONLY if filament is there AND no operator-
        # control flag is active:
        #   - _auto_off_by_user: operator explicitly disabled AUTO
        #   - _bang_bang_suspended: print is paused
        #   - _halt_requested: operator issued HALT/STOP_BUFFER_FILL
        # A simple HALL1-clear must not silently override any of these.
        if (self.entrance_detected
                and not self._auto_off_by_user
                and not self._bang_bang_suspended
                and not self._halt_requested):
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
        # Edge-triggered auto-grip: only proceed with the default
        # IDLE→INITIAL_GRIP path if we've observed the entrance being
        # EMPTY at some point since boot. Boot with filament already
        # at the entrance → _entrance_was_empty stays False → no
        # silent auto-grip. User can still trigger manually via
        # FORCE_BUFFER_FILL. Explicit runout / user-pull events
        # flip the flag so the next real re-insert auto-grips.
        # RUNOUT/suspend/auto_off/halt path guards below still apply.
        will_auto_grip = (self._state == STATE_IDLE
                          and not self._bang_bang_suspended
                          and not self._auto_off_by_user
                          and not self._halt_requested
                          and self._entrance_was_empty)
        # Reset the flag now — we've consumed the edge.
        self._entrance_was_empty = False
        # RUNOUT (runout_pause=1): the print has been PAUSE'd. Spec §5
        # says a reinsert clears RUNOUT but does NOT auto-grip —
        # grip during a paused print would queue unexpected motion.
        # Arm _runout_recovery_pending so that the next RESUME
        # (_on_idle_printing) specifically triggers grip+fill for
        # THIS reinsert, not for any unrelated IDLE-state entering
        # printing. Operators who want a manual fill before RESUME
        # can call BUFFER_AUTO_OFF + FORCE_BUFFER_FILL.
        if self._state == STATE_RUNOUT:
            self._set_state(STATE_IDLE)
            self._runout_recovery_pending = True
            self._respond("Reinsert during RUNOUT — cleared. Call "
                          "RESUME to continue (grip + fill runs "
                          "automatically), or BUFFER_AUTO_OFF + "
                          "FORCE_BUFFER_FILL for manual refill first.")
            return
        # Suppress auto-grip while bang-bang is suspended (print-PAUSE).
        if self._bang_bang_suspended:
            self._respond("Reinsert during paused print — auto-grip suppressed. "
                          "Use FORCE_BUFFER_FILL to trigger manually after RESUME.")
            return
        # Respect explicit operator decision to keep AUTO off.
        # Reinsert after BUFFER_AUTO_OFF should NOT auto-grip —
        # the user specifically disabled the feeder.
        if self._auto_off_by_user:
            self._respond("Reinsert while AUTO is off (operator-disabled) — "
                          "auto-grip suppressed. Use FORCE_BUFFER_FILL to trigger.")
            return
        # Normal idle-state reinsert: kick off initial grip, but
        # only if we've actually seen an empty→present transition
        # (edge flag above). Boot with filament already present does
        # NOT trigger grip — operator runs FORCE_BUFFER_FILL if they
        # want a fresh fill.
        if will_auto_grip:
            self._start_initial_grip(eventtime)
        else:
            self._respond("Entrance already had filament at boot — "
                          "no auto-grip. Use FORCE_BUFFER_FILL to "
                          "fill the buffer manually.")

    def _on_entrance_runout(self, eventtime):
        # Arm the edge-detect flag: we've now seen the entrance go
        # empty. The NEXT entrance-True callback (re-insert) will be
        # treated as a genuine operator insert event and will
        # auto-grip (subject to the usual lockout / AUTO_OFF checks).
        self._entrance_was_empty = True
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
        # Also block if HALL1 is physically active — covers the race
        # where AUTO_OFF/STOP_BUFFER_FILL reset state to IDLE before
        # the next main_tick re-asserts OVERFLOW.
        if self._state in (STATE_LOAD_PHASE_1, STATE_LOAD_PHASE_2, STATE_LOAD_PHASE_3,
                           STATE_UNLOAD_PHASE_1, STATE_UNLOAD_PHASE_2,
                           STATE_UNLOAD_PHASE_3, STATE_OVERFLOW, STATE_JAM,
                           STATE_INITIAL_GRIP):
            self._respond("Button ignored — state=%s" % self._state)
            return
        if self.hall_overflow:
            self._respond("Button ignored — HALL1 overflow physically active")
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
        if direction < 0:
            # Retract burst: operator is deliberately pulling filament back.
            # Stay IDLE afterwards — jam timer must not race against an empty
            # buffer. Operator calls BUFFER_AUTO_ON to re-engage.
            self._auto_off_by_user = True
        self._schedule_return_to_auto_after_move(cooldown=self.reenable_cooldown_fast)
        self._respond("%s: Triple-Burst %d mm @ %d mm/s"
                      % (button_name, self.burst_distance, self.burst_speed))

    # -----------------------------------------------------------------------
    # Initial grip phase
    # -----------------------------------------------------------------------

    def _estimate_sequence_duration(self, distance, speed):
        """Upper-bound wall-time for an async-streamed distance.

        Each chunk in the streamer does accel → cruise → decel to 0,
        so the true chunk time is `2*accel_time + cruise_time`, not
        just `chunk_dist/speed`. Summing across all chunks yields a
        per-move overhead of `chunks * 2 * accel_time`. Returning
        this upper bound ensures callers that set a state-deadline
        (INITIAL_GRIP, cooldown) don't flip out of the phase while
        the final chunk is still playing.
        """
        if speed <= 0 or distance <= 0:
            return 0.0
        accel_time = speed / self.accel
        chunks = int(math.ceil(distance / self.max_move_chunk_mm))
        return distance / speed + chunks * 2.0 * accel_time

    def _start_initial_grip(self, eventtime):
        self._enable_stepper()
        self._set_state(STATE_INITIAL_GRIP)
        distance = self.grip_speed * self.grip_duration
        self._respond("Initial grip: %.0f mm @ %.0f mm/s"
                      % (distance, self.grip_speed))
        # Submit first, then compute end_time from the ACTUAL queued
        # chunk's end plus the pending-stream remainder. This accounts
        # for the case where _last_move_end_time was in the future
        # from a prior aborted move (trapq can't overwrite; new move
        # starts at max(now+lead, _last_move_end_time)).
        self._submit_move(distance, self.grip_speed)
        pending_duration = 0.0
        if self._pending_remaining_mm > 0 and self._pending_speed > 0:
            pending_duration = self._estimate_sequence_duration(
                self._pending_remaining_mm, self._pending_speed)
        self._initial_grip_end_time = self._last_move_end_time + pending_duration

    # -----------------------------------------------------------------------
    # Main tick — sensor debounce + bang-bang + state progression
    # -----------------------------------------------------------------------

    def _main_tick(self, eventtime):
        try:
            self._check_debounce(eventtime)

            # During startup grace, only sensor polling runs. No state
            # transitions, no bang-bang, no continuous feed — we wait
            # for Klipper to deliver initial sensor callbacks so we
            # learn the real hardware picture.
            if not self._startup_grace_done:
                return eventtime + MAIN_TICK_INTERVAL

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

            # Cooldown end: back to AUTO if entrance present AND the
            # operator hasn't explicitly disabled AUTO.
            if self._cooldown_deadline is not None and eventtime >= self._cooldown_deadline:
                self._cooldown_deadline = None
                if self._state in (STATE_MANUAL_FEED, STATE_MANUAL_RETRACT, STATE_INITIAL_GRIP):
                    if (self.entrance_detected
                            and not self.hall_overflow
                            and not self._auto_off_by_user
                            and not self._bang_bang_suspended):
                        self._set_state(STATE_AUTO)
                    else:
                        self._set_state(STATE_IDLE)

            # Initial grip done -> AUTO (or IDLE if operator-off).
            if self._state == STATE_INITIAL_GRIP and self._initial_grip_end_time is not None:
                mcu = self.stepper.get_mcu()
                now_pt = mcu.estimated_print_time(eventtime)
                if now_pt >= self._initial_grip_end_time:
                    self._initial_grip_end_time = None
                    # Respect explicit AUTO_OFF / print-pause — do not
                    # silently re-engage bang-bang.
                    if self._auto_off_by_user or self._bang_bang_suspended:
                        self._set_state(STATE_IDLE)
                        self._respond("Initial grip done — staying IDLE "
                                      "(AUTO off by operator or print paused)")
                    else:
                        self._set_state(STATE_AUTO)
                        self._respond("Initial grip done — AUTO engaged")
                        # Guarantee the spec's "initial fill sequence":
                        # bang-bang's deadband hysteresis would stop
                        # immediately if the arm landed between HALL3
                        # and HALL2 after the grip. Explicitly kick
                        # continuous feed so the buffer fills to HALL2
                        # before bang-bang's stop-on-full triggers.
                        if not self.hall_full:
                            self._start_continuous_motion(
                                +1, self.feed_speed, self.max_feed_time)
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
            # BUFFER_WAIT_IDLE and main_tick finalizes the state here.
            # Must wait for BOTH in-flight trapezoid AND pending-stream
            # to drain — with 180mm Phase 2 / 50mm chunks, the default
            # case streams 3-4 chunks; finalizing after the first one
            # releases the state mid-move.
            if (self._state in (STATE_LOAD_PHASE_2, STATE_UNLOAD_PHASE_2)
                    and not self._move_in_flight()
                    and self._pending_remaining_mm <= 0):
                self._set_state(STATE_IDLE)

            # Continuous feed: keep chunks streaming, but only in
            # states where continuous motion is the intended behavior.
            # Otherwise a stale _continuous_feed=True would leak into
            # LOAD_PHASE_1/2 / UNLOAD_PHASE_2 single-shot moves and
            # keep pumping extra chunks after the phase's own move.
            if (self._continuous_feed
                    and self._state in CONTINUOUS_FEED_STATES
                    and not self._move_in_flight()):
                chunk_dist = max(self.manual_chunk_distance,
                                 self._continuous_feed_speed * 0.5)
                self._submit_move(self._continuous_feed_direction * chunk_dist,
                                  self._continuous_feed_speed)

            # Pending-chunk streaming for long single-shot moves.
            # Schedule the next chunk when the current one is within
            # half-a-chunk-duration of ending, so chunks abut with
            # no visible gap in motion. Abort signals zero out the
            # pending counter — draining remaining chunks happens
            # only on the MCU for the already-queued trapezoid.
            if self._pending_remaining_mm > 0:
                if self._abort_signalled():
                    self._pending_remaining_mm = 0.0
                elif self._pending_speed > 0:
                    chunk_duration = (self.max_move_chunk_mm
                                      / self._pending_speed)
                    mcu = self.stepper.get_mcu()
                    now_pt = mcu.estimated_print_time(eventtime)
                    # Submit next chunk when <= half-a-chunk remains
                    # in the currently-queued move, so next trapezoid
                    # starts right at the prior one's end_time.
                    if (self._last_move_end_time - now_pt) <= chunk_duration * 0.5:
                        chunk = min(self._pending_remaining_mm,
                                    self.max_move_chunk_mm)
                        self._submit_single_trapezoid(
                            self._pending_direction * chunk,
                            self._pending_speed)
                        self._pending_remaining_mm -= chunk

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
            # Route through _trigger_jam so the blocking LOAD_PHASE3
            # command's post-loop _raise_if_locked_out raises and
            # aborts the LOAD_FILAMENT macro instead of letting it
            # print "LOAD abgeschlossen".
            self._trigger_jam(
                "LOAD_TIMEOUT",
                "LOAD Phase 3: max_distance %dmm reached without HALL2 — check sensor/buffer"
                % int(self._load_phase3_max_distance))
            return
        if not self._move_in_flight():
            # Clip chunk so the per-call MAX_DISTANCE is a hard cap.
            remaining = self._load_phase3_max_distance - self._load_phase3_distance
            chunk = min(10.0, remaining)
            if chunk > 0:
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

    def _schedule_time_for_enable_toggle(self):
        """Pick a safe print_time for the next motor_enable/disable.

        Must satisfy ALL of:
          - ≥ toolhead print_time + lead_time (avoids "in the past"
            relative to the MCU's step-gen cursor)
          - ≥ _last_move_end_time (so a disable never lands before a
            queued step, which would force the MCU to reorder)
          - ≥ previous enable/disable schedule time + ε (strictly
            monotonic — else the MCU sees an out-of-order toggle and
            crashes with "Timer too close").
        """
        toolhead = self.printer.lookup_object('toolhead')
        th_time = toolhead.get_last_move_time()
        pt = max(th_time + self.lead_time,
                 self._last_move_end_time,
                 self._last_enable_schedule_time + 1e-6)
        self._last_enable_schedule_time = pt
        return pt

    def _enable_stepper(self):
        if self._stepper_enable is None:
            return
        try:
            pt = self._schedule_time_for_enable_toggle()
            self._stepper_enable.motor_enable(pt)
        except Exception:
            logging.exception("buffer_feeder: enable_stepper failed")

    def _disable_stepper(self):
        if self._stepper_enable is None:
            return
        try:
            pt = self._schedule_time_for_enable_toggle()
            self._stepper_enable.motor_disable(pt)
        except Exception:
            logging.exception("buffer_feeder: disable_stepper failed")

    def _submit_move(self, signed_distance, speed):
        """Submit a move. Chunks long moves asynchronously.

        Flush-free. For distances ≤ max_move_chunk_mm this queues
        one trapezoid and returns. For longer distances it queues
        the first chunk only and records the remainder in
        _pending_remaining_mm; main_tick streams subsequent chunks
        as prior ones approach completion.

        The async streaming is what keeps HALT responsive. A
        synchronous loop would queue the whole sequence to the MCU
        at once — _last_move_end_time would land at the end of the
        full distance, and HALT could no longer prevent chunks that
        are already in the MCU step queue from playing out. By
        only ever holding ~1.5 chunks ahead in the trapq, HALT can
        zero out _pending_remaining_mm and let the in-flight chunk
        drain out — max latency one chunk duration.
        """
        if signed_distance == 0 or speed <= 0:
            return
        if self.hall_overflow:
            logging.warning("buffer_feeder: move rejected — HALL1 active "
                            "(distance=%.1f speed=%.1f)", signed_distance, speed)
            self._continuous_feed = False
            self._pending_remaining_mm = 0.0
            return

        # Cancel any previously-streaming sequence before starting new.
        self._pending_remaining_mm = 0.0

        distance_abs = abs(signed_distance)
        direction = 1.0 if signed_distance > 0 else -1.0

        first_chunk = min(distance_abs, self.max_move_chunk_mm)
        self._submit_single_trapezoid(direction * first_chunk, speed)
        remaining = distance_abs - first_chunk
        if remaining > 0:
            self._pending_remaining_mm = remaining
            self._pending_direction = direction
            self._pending_speed = speed

    def _submit_single_trapezoid(self, signed_distance, speed):
        """Append one trapezoid to our trapq. Low-level primitive."""
        # Prime stepcompress on the very first submission. Without
        # this, last_step_clock=0 and the first step (after minutes
        # or hours of idle) sits at clock N×10⁹, which exceeds
        # CLOCK_DIFF_MAX → stepcompress returns an invalid
        # queue_step → MCU shutdown. set_position anchors itersolve's
        # position state to (0,0,0); toolhead.get_last_move_time()
        # below ties our t0 to print-time that the MCU's step-gen
        # cursor can actually track.
        if not self._stepcompress_primed:
            self.stepper.set_position((0., 0., 0.))
            self._commanded_pos = 0.0
            self._stepcompress_primed = True

        self._enable_stepper()

        # Time base = toolhead.get_last_move_time(), NOT
        # mcu.estimated_print_time(reactor.monotonic()). The latter
        # drifts away from toolhead print_time during long idle
        # periods; submitting at t0 = est_print_time + lead would
        # land at a clock the MCU's step-gen cursor has no baseline
        # for, producing an "Invalid sequence" (see 2026-04-24 log).
        # get_last_move_time flushes only the lookahead planner —
        # it does NOT drain the MCU step queue, so it is cheap
        # enough to call from bang-bang/streaming paths during an
        # active print. This mirrors manual_stepper/force_move.
        toolhead = self.printer.lookup_object('toolhead')
        th_time = toolhead.get_last_move_time()
        t0 = max(th_time + self.lead_time, self._last_move_end_time)

        distance = abs(signed_distance)
        direction = 1.0 if signed_distance > 0 else -1.0

        accel = self.accel
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
        axes_r_x = direction

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

        if self._measure_load_active and direction > 0:
            self._measure_load_distance += distance

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
        cut motor power on the MCU-level.

        Clears `_pending_remaining_mm` so a long async-streamed move
        stops the moment halt_motion is called. Without this clear,
        OVERFLOW / JAM would suspend streaming only as long as
        _abort_signalled() returned True — a subsequent AUTO_ON or
        HALL1-release would re-enable streaming of the leftover
        distance mid-recovery.

        Also clears `_feed_deadline_time` so a deadline that was
        armed for a since-finished continuous feed does not later
        trip SAFETY_TIMEOUT on a quiescent feeder.
        """
        self._continuous_feed = False
        self._continuous_feed_direction = 0
        self._continuous_feed_speed = 0.0
        self._pending_remaining_mm = 0.0
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
        # Account for BOTH the already-queued trapezoid and any
        # pending chunks still to be streamed. Use the sequence
        # estimator for the pending part so per-chunk accel/decel
        # overhead is included — otherwise long manual/burst moves
        # would see the cooldown fire ~1.3s before the last chunk
        # finishes (1300mm burst at 50mm/s over 26 chunks).
        delay = 0.1 + cooldown
        if self._current_move is not None:
            mcu = self.stepper.get_mcu()
            now_pt = mcu.estimated_print_time(self.reactor.monotonic())
            remaining_current = max(0.0, self._current_move['end_time'] - now_pt)
            remaining_pending = 0.0
            if self._pending_remaining_mm > 0 and self._pending_speed > 0:
                remaining_pending = self._estimate_sequence_duration(
                    self._pending_remaining_mm, self._pending_speed)
            delay = remaining_current + remaining_pending + cooldown
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

    def _gcode_run_script(self, script, from_command=False):
        """Run a gcode script, choosing the mutex-safe variant.

        Inside a gcode command handler, we already hold the gcode
        mutex — `run_script_from_command` avoids re-acquire issues.
        Outside (reactor timer / event handler), use `run_script`
        which acquires the mutex.
        """
        try:
            gc = self.printer.lookup_object('gcode')
            if from_command:
                gc.run_script_from_command(script)
            else:
                gc.run_script(script)
        except Exception:
            logging.exception("buffer_feeder: gcode run_script failed (%s)", script)

    def _respond(self, message, force_display=False):
        # Log + console echo only. We deliberately do NOT emit M117
        # from here any more: _respond is called from both reactor-
        # event handlers AND gcode command handlers, and gc.run_script
        # re-acquires the gcode mutex. From a command handler (where
        # the mutex is already held), that call deadlocks Klipper's
        # entire gcode pipeline — all subsequent commands (including
        # print-start from Mainsail) queue up but never execute.
        # The `force_display` parameter is kept for call-site
        # backwards compatibility but is now a no-op.
        logging.info("buffer_feeder: %s", message)
        try:
            gc = self.printer.lookup_object('gcode')
            gc.respond_info("BufferFeeder: %s" % message)
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
        if self.hall_overflow:
            raise self._cmd_error("BufferFeeder: HALL1 overflow physically active — blocked")
        if self._state in BUSY_PHASE_STATES:
            raise self._cmd_error(
                "BufferFeeder: busy (state=%s) — call STOP_BUFFER_FILL "
                "or wait for LOAD/UNLOAD to finish" % self._state)
        # Fresh manual command = operator acknowledges any stale HALT
        # AND any pending runout-recovery auto-grip. Operator picked a
        # different recovery path (manual feed/retract) — RESUME should
        # not later queue a surprise grip.
        self._halt_requested = False
        self._runout_recovery_pending = False
        # Always start from a clean continuous-feed state — don't let
        # leftover bang-bang / old dauerfeed pump chunks into (or past)
        # this new command.
        self._continuous_feed = False
        target_state = STATE_MANUAL_FEED if direction > 0 else STATE_MANUAL_RETRACT
        if distance > 0:
            if distance > self.max_feed_distance:
                raise self._cmd_error("DISTANCE exceeds max_feed_distance=%.0f"
                                      % self.max_feed_distance)
            self._set_state(target_state)
            self._submit_move(direction * distance, speed)
            self._schedule_return_to_auto_after_move()
        else:
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
        # HALT supersedes a pending RUNOUT-recovery auto-grip.
        self._runout_recovery_pending = False
        # Wipe any cooldown timer so it can't later flip state.
        self._cooldown_deadline = None
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
        # Print-PAUSE suspension is owned by idle_timeout; user must
        # RESUME the print before bang-bang can re-engage. Allowing
        # AUTO_ON to clear this flag would defeat the documented
        # pause-until-RESUME semantic (spec §5).
        if self._bang_bang_suspended:
            raise self._cmd_error(
                "Cannot enable AUTO while print is paused (bang-bang "
                "suspended). RESUME the print — bang-bang re-engages "
                "automatically. If the print is already finished, "
                "use BUFFER_AUTO_OFF first to clear the suspension.")
        # Clear transient flags — user is explicitly starting fresh.
        # Also consume any pending RUNOUT-recovery: operator chose
        # to engage AUTO directly, so RESUME should not later insert
        # a grip on top.
        self._halt_requested = False
        self._auto_off_by_user = False
        self._runout_recovery_pending = False
        self._enable_stepper()
        self._set_state(STATE_AUTO)
        self._respond("AUTO engaged")

    cmd_BUFFER_AUTO_OFF_help = "Disable bang-bang auto mode (also clears JAM/runout-follow/pause-suspend)"
    def cmd_BUFFER_AUTO_OFF(self, gcmd):
        # Full-reset semantic: AUTO_OFF is the operator's "stop
        # everything and take control" lever. Clears recovery flags
        # AND the print-PAUSE suspension, so the user isn't stuck
        # (e.g. if the print ended uncleanly and idle_timeout never
        # fired :printing again). _auto_off_by_user is set to sticky-
        # block auto-grip on reinsert; re-engaging AUTO requires an
        # explicit BUFFER_AUTO_ON.
        # Also arms _halt_requested so any in-flight macro aborts
        # at its next wait-point (same contract as BUFFER_HALT).
        self._continuous_feed = False
        self._halt_motion()
        self._pending_remaining_mm = 0.0
        self._jam_active = False
        self._hall2_start_time = None
        self._hall3_start_time = None
        self._runout_follow_active = False
        self._runout_filament_ref = None
        self._measure_load_active = False
        self._measure_feeding = False
        self._cooldown_deadline = None
        self._bang_bang_suspended = False  # operator overrides PAUSE-suspend
        self._auto_off_by_user = True      # but reinsert auto-grip stays blocked
        self._runout_recovery_pending = False
        self._halt_requested = True
        self._set_state(STATE_IDLE)
        # Best-effort restore of any pending LOAD/UNLOAD gcode-state
        # (E-mode etc.) so AUTO_OFF cleanly recovers after a failed
        # LOAD/UNLOAD that couldn't reach its _RESTORE_E_MODE.
        self._try_restore_gcode_state(from_command=True)
        self._respond("AUTO off — workflow will abort at next wait; recovery flags cleared")

    def _abort_signalled(self):
        """True if a wait should cut short — HALT armed or safety lockout."""
        return (self._halt_requested
                or self._state == STATE_OVERFLOW
                or self._state == STATE_JAM
                or self._jam_active
                or self.hall_overflow)

    def _wait_for_move_done(self, gcmd=None):
        """Internal: block until both in-flight and pending-stream
        moves are done, OR an emergency condition trips.

        Used by blocking phase commands that legitimately hold the
        busy-phase state during the wait. External callers should
        use cmd_BUFFER_WAIT_IDLE instead, which additionally waits
        for the busy-phase state to be vacated.

        Early-exits on HALT / OVERFLOW / JAM because at that point
        the motor has already been disabled (or is about to be) and
        waiting out the nominal trapq end_time is pointless.
        """
        while self._move_in_flight() or self._pending_remaining_mm > 0:
            if self._abort_signalled():
                break
            self.reactor.pause(self.reactor.monotonic() + 0.05)
        if gcmd is not None:
            self._raise_if_locked_out(gcmd)

    cmd_BUFFER_WAIT_IDLE_help = ("Block until the feeder's current move is complete "
                                 "AND state has exited busy-phase (IDLE / AUTO / RUNOUT / lockout)")
    def cmd_BUFFER_WAIT_IDLE(self, gcmd):
        # Public contract (README / spec): wait for move-fertig AND
        # state=IDLE/AUTO. Also wait for pending-streamed chunks
        # to drain so the full logical move is done.
        # Early-exit on emergency conditions so abort propagates fast.
        while (self._move_in_flight()
               or self._pending_remaining_mm > 0
               or self._state in BUSY_PHASE_STATES):
            if self._abort_signalled():
                break
            self.reactor.pause(self.reactor.monotonic() + 0.05)
        self._raise_if_locked_out(gcmd)

    cmd_BUFFER_LOAD_PHASE1_help = "LOAD Phase 1 — feeder alone fast to toolhead. DISTANCE=mm"
    def cmd_BUFFER_LOAD_PHASE1(self, gcmd):
        self._halt_requested = False    # ack any stale console HALT
        self._raise_if_locked_out(gcmd)
        if self._state in BUSY_PHASE_STATES:
            raise self._cmd_error(
                "LOAD_PHASE1 rejected — another phase already active (state=%s)"
                % self._state)
        distance = gcmd.get_float('DISTANCE', self.load_fast_distance, above=0.)
        speed    = gcmd.get_float('SPEED',    self.load_fast_speed,    above=0.)
        # Stop any inherited bang-bang / manual dauerfeed and drain
        # any in-flight chunk so residual motion doesn't extend Phase 1.
        self._continuous_feed = False
        self._wait_for_move_done(gcmd)
        self._set_state(STATE_LOAD_PHASE_1)
        self._enable_stepper()
        self._submit_move(+distance, speed)
        # Blocking: wait for move done (state stays LOAD_PHASE_1 during
        # the wait, so we need the internal helper — BUFFER_WAIT_IDLE
        # would deadlock because it also waits for state != busy-phase).
        try:
            self._wait_for_move_done(gcmd)
        except Exception:
            # Release the phase state on error so it doesn't stay sticky.
            self._set_state(STATE_IDLE)
            raise
        self._set_state(STATE_IDLE)

    cmd_BUFFER_LOAD_PHASE2_help = "LOAD Phase 2 — feeder parallel to extruder. Non-blocking, use BUFFER_WAIT_IDLE"
    def cmd_BUFFER_LOAD_PHASE2(self, gcmd):
        self._halt_requested = False
        self._raise_if_locked_out(gcmd)
        if self._state in BUSY_PHASE_STATES:
            raise self._cmd_error(
                "LOAD_PHASE2 rejected — another phase already active (state=%s)"
                % self._state)
        distance = gcmd.get_float('DISTANCE', self.load_slow_distance, above=0.)
        speed    = gcmd.get_float('SPEED',    self.load_slow_speed,    above=0.)
        self._continuous_feed = False
        self._wait_for_move_done(gcmd)
        self._set_state(STATE_LOAD_PHASE_2)
        self._enable_stepper()
        self._submit_move(+distance, speed)

    cmd_BUFFER_LOAD_PHASE3_help = "LOAD Phase 3 — feed until HALL2 or MAX_DISTANCE"
    def cmd_BUFFER_LOAD_PHASE3(self, gcmd):
        self._halt_requested = False
        self._raise_if_locked_out(gcmd)
        if self._state in BUSY_PHASE_STATES:
            raise self._cmd_error(
                "LOAD_PHASE3 rejected — another phase already active (state=%s)"
                % self._state)
        max_distance = gcmd.get_float('MAX_DISTANCE', self.load_buffer_max, above=0.)
        speed        = gcmd.get_float('SPEED',        self.feed_speed,      above=0.)
        # Clean start: stop any inherited continuous feed and wait for
        # any in-flight manual move to finish before we begin chunk
        # streaming. Prevents residual motion from tacking onto Phase 3.
        self._continuous_feed = False
        self._wait_for_move_done(gcmd)
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
        if self._state in BUSY_PHASE_STATES:
            raise self._cmd_error(
                "UNLOAD_PHASE1 rejected — another phase already active (state=%s)"
                % self._state)
        # Tip-Forming runs on the extruder alone. Feeder must stand
        # still, and the state must NOT be IDLE — otherwise manual
        # buttons and FORCE_BUFFER_FILL would accept input and stomp
        # the unload sequence. STATE_UNLOAD_PHASE_1 is one of the
        # phase states blocked by the button handler.
        self._continuous_feed = False
        self._halt_motion()
        self._set_state(STATE_UNLOAD_PHASE_1)
        # Block until any in-flight chunk has finished (internal
        # helper — state stays UNLOAD_PHASE_1 during the wait, so
        # the public BUFFER_WAIT_IDLE would deadlock).
        try:
            self._wait_for_move_done(gcmd)
        except Exception:
            # On error (HALT/JAM/OVERFLOW raising), release the phase
            # state so it doesn't stay sticky. OVERFLOW/JAM will be
            # reasserted by the next main_tick if still active.
            self._set_state(STATE_IDLE)
            raise
        self._disable_stepper()
        self._respond("UNLOAD Phase 1: feeder halted for tip-forming")

    cmd_BUFFER_UNLOAD_PHASE2_help = "UNLOAD Phase 2 — feeder retract parallel to extruder"
    def cmd_BUFFER_UNLOAD_PHASE2(self, gcmd):
        self._halt_requested = False
        self._raise_if_locked_out(gcmd)
        if self._state in BUSY_PHASE_STATES:
            raise self._cmd_error(
                "UNLOAD_PHASE2 rejected — another phase already active (state=%s)"
                % self._state)
        distance = gcmd.get_float('DISTANCE', self.unload_sync_distance, above=0.)
        speed    = gcmd.get_float('SPEED',    self.unload_fast_speed,    above=0.)
        self._continuous_feed = False
        self._wait_for_move_done(gcmd)
        self._set_state(STATE_UNLOAD_PHASE_2)
        self._enable_stepper()
        self._submit_move(-distance, speed)

    cmd_BUFFER_UNLOAD_PHASE3_help = "UNLOAD Phase 3 — chunked retract until entrance free"
    def cmd_BUFFER_UNLOAD_PHASE3(self, gcmd):
        self._halt_requested = False
        self._raise_if_locked_out(gcmd)
        if self._state in BUSY_PHASE_STATES:
            raise self._cmd_error(
                "UNLOAD_PHASE3 rejected — another phase already active (state=%s)"
                % self._state)
        max_distance = gcmd.get_float('MAX_DISTANCE', self.unload_fast_max, above=0.)
        speed        = gcmd.get_float('SPEED',        self.unload_fast_speed, above=0.)
        nominal_chunk = 50.0
        # Clean start: cancel any inherited continuous feed and drain
        # any in-flight move so residual motion doesn't join the retract.
        self._continuous_feed = False
        self._wait_for_move_done(gcmd)
        self._set_state(STATE_UNLOAD_PHASE_3)
        self._enable_stepper()
        retracted = 0.0
        overshoot = False
        while retracted < max_distance:
            # Abort immediately on lockout.
            self._raise_if_locked_out(gcmd)
            if not self.entrance_detected:
                self._respond("UNLOAD Phase 3: entrance clear after %.0f mm" % retracted)
                break
            # Clip last chunk so MAX_DISTANCE is a HARD cap, not a
            # best-effort ceiling (previously could overshoot by up
            # to one full chunk).
            chunk = min(nominal_chunk, max_distance - retracted)
            if chunk <= 0:
                overshoot = True
                break
            self._submit_move(-chunk, speed)
            retracted += chunk
            # Wait on move-only; state stays UNLOAD_PHASE_3 until we
            # exit the loop. _wait_for_move_done raises on OVERFLOW/JAM.
            self._wait_for_move_done(gcmd)
        else:
            overshoot = True
        self._disable_stepper()
        self._set_state(STATE_IDLE)
        if overshoot:
            # Explicit failure — do not let UNLOAD_FILAMENT print
            # "UNLOAD abgeschlossen" after an unsuccessful retract.
            raise self._cmd_error(
                "UNLOAD Phase 3: MAX_DISTANCE %dmm reached without "
                "entrance clear — check buffer / filament path"
                % int(max_distance))

    cmd_FORCE_BUFFER_FILL_help = "Manually trigger initial grip + fill cycle"
    def cmd_FORCE_BUFFER_FILL(self, gcmd):
        if not self.entrance_detected:
            raise self._cmd_error("FORCE_BUFFER_FILL aborted: no filament at entrance")
        if self.hall_overflow or self._state == STATE_OVERFLOW:
            raise self._cmd_error("FORCE_BUFFER_FILL aborted: HALL1 overflow active")
        if self._state == STATE_JAM or self._jam_active:
            raise self._cmd_error("FORCE_BUFFER_FILL aborted: JAM active. Use BUFFER_CLEAR_JAM first.")
        # Refuse during print-PAUSE — FORCE_BUFFER_FILL is meant as a
        # full "initial grip + fill" cycle, and issuing a real grip
        # move while the printer is paused would queue unexpected
        # motion (same reason the entrance-insert handler suppresses
        # auto-grip during suspension).
        if self._bang_bang_suspended:
            raise self._cmd_error(
                "FORCE_BUFFER_FILL aborted: print is paused (bang-bang "
                "suspended). RESUME the print first, or use AUTO_OFF + "
                "AUTO_ON to take manual control.")
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
        # Operator explicitly invoked the full fill cycle. Clear the
        # stale HALT flag AND the AUTO_OFF-by-user flag, so the
        # initial-grip post-condition transitions to AUTO (bang-bang
        # continues until HALL2). Without clearing _auto_off_by_user
        # the grip would drop to IDLE and the "fill" part never runs —
        # BUFFER_AUTO_OFF → FORCE_BUFFER_FILL only grips 10s then stops.
        # Also consume any pending RUNOUT-recovery: this manual fill
        # IS the recovery, no need for RESUME to re-trigger.
        self._halt_requested = False
        self._auto_off_by_user = False
        self._runout_recovery_pending = False
        # Wait for any lingering in-flight chunk from a prior aborted
        # move to drain. Otherwise the initial-grip's end_time would
        # undershoot by the old chunk's remaining trapq duration
        # (since _halt_motion leaves _last_move_end_time intact).
        self._wait_for_move_done(gcmd)
        self._start_initial_grip(self.reactor.monotonic())

    cmd_STOP_BUFFER_FILL_help = "Abort any ongoing fill/grip/manual and return to IDLE"
    def cmd_STOP_BUFFER_FILL(self, gcmd):
        # Full-reset semantic: STOP_BUFFER_FILL aborts everything and
        # clears recovery flags so we land in a clean IDLE state.
        # Like BUFFER_AUTO_OFF, also clears _bang_bang_suspended so
        # the operator can re-engage AUTO without a missing RESUME.
        # _auto_off_by_user keeps reinsert-grip blocked.
        # Like BUFFER_HALT, arms _halt_requested so any macro waiting
        # on BUFFER_WAIT_IDLE raises and aborts rather than silently
        # continuing to the next phase.
        self._continuous_feed = False
        self._halt_motion()
        self._pending_remaining_mm = 0.0
        self._initial_grip_end_time = None
        self._load_phase3_distance = 0.0
        self._measure_load_active = False
        self._measure_feeding = False
        self._jam_active = False
        self._hall2_start_time = None
        self._hall3_start_time = None
        self._runout_follow_active = False
        self._runout_filament_ref = None
        self._cooldown_deadline = None
        self._bang_bang_suspended = False
        self._auto_off_by_user = True
        self._runout_recovery_pending = False
        self._halt_requested = True
        self._set_state(STATE_IDLE)
        # Best-effort gcode-state restore after a failed LOAD/UNLOAD.
        self._try_restore_gcode_state(from_command=True)
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
            "pending_remaining  = %.1f mm" % self._pending_remaining_mm,
            "feed_distance_acc  = %.1f mm" % self._feed_distance_accumulator,
            "accumulated total  = %.1f mm" % self._accumulated_feed_distance,
            "commanded_pos      = %.1f mm" % self._commanded_pos,
            "print_running      = %s" % self._print_running,
            "bang_bang_suspended= %s" % self._bang_bang_suspended,
            "auto_off_by_user   = %s" % self._auto_off_by_user,
            "cooldown_deadline  = %s" % (self._cooldown_deadline,),
            "halt_requested     = %s" % self._halt_requested,
            "jam_active         = %s" % self._jam_active,
            "runout_follow      = %s ref=%s" % (self._runout_follow_active,
                                                self._runout_filament_ref),
            "runout_recov_pending= %s (RESUME will grip+fill if armed)" % self._runout_recovery_pending,
            "macro_state_saved  = %s (buffer_feeder_op slot consumable)" % self._macro_state_saved,
            "measure_load       = active=%s feeding=%s dist=%.1f mm" % (
                self._measure_load_active, self._measure_feeding,
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
        # Operator is entering a distinct calibration workflow —
        # consume any pending RUNOUT-recovery so RESUME afterwards
        # doesn't surprise-grip on top of the measurement.
        self._runout_recovery_pending = False
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

    def _try_restore_gcode_state(self, from_command=False):
        """Best-effort: restore the 'buffer_feeder_op' gcode state if
        a LOAD/UNLOAD macro saved one and we haven't already consumed
        it. Klipper's RESTORE_GCODE_STATE doesn't delete the slot,
        so without our own _macro_state_saved flag any later call
        would re-apply a stale state. Idempotent across cleanup paths.
        """
        if not self._macro_state_saved:
            return False
        try:
            self._gcode_run_script(
                "RESTORE_GCODE_STATE NAME=buffer_feeder_op MOVE=0",
                from_command=from_command)
            self._macro_state_saved = False
            return True
        except Exception:
            logging.exception("buffer_feeder: gcode-state restore failed")
            return False

    def cmd_BUFFER_RESTORE_STATE(self, gcmd):
        if self._try_restore_gcode_state(from_command=True):
            self._respond("Restored gcode-state from 'buffer_feeder_op'")
        else:
            self._respond("No 'buffer_feeder_op' gcode-state to restore")

    def cmd_BUFFER_SAVE_MACRO_STATE(self, gcmd):
        """Invoked by the _SAVE_E_MODE macro. Saves gcode state AND
        marks it as valid-to-restore. Running again before a restore
        simply overwrites the slot."""
        self._gcode_run_script(
            "SAVE_GCODE_STATE NAME=buffer_feeder_op",
            from_command=True)
        self._macro_state_saved = True

    def cmd_BUFFER_RESTORE_MACRO_STATE(self, gcmd):
        """Invoked by the _RESTORE_E_MODE macro on the normal success
        path. Restores and clears the flag so later cleanup paths
        don't re-apply the same stale state."""
        if not self._macro_state_saved:
            # Normal success case: macro saved then restored exactly
            # once. Silent no-op if called without a save (defensive).
            return
        self._gcode_run_script(
            "RESTORE_GCODE_STATE NAME=buffer_feeder_op MOVE=0",
            from_command=True)
        self._macro_state_saved = False

    def cmd_BUFFER_CLEAR_JAM(self, gcmd):
        if self._state != STATE_JAM:
            raise self._cmd_error("Not in JAM state (state=%s)" % self._state)
        self._jam_active = False
        self._hall2_start_time = None
        self._hall3_start_time = None
        self._halt_requested = False
        # Best-effort restore of any LOAD/UNLOAD gcode-state that was
        # saved before the jam fired. Otherwise the operator would
        # end up back in AUTO with the E-mode still flipped to M83
        # from the failed macro.
        self._try_restore_gcode_state(from_command=True)
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
        # Check hall_overflow directly, not just state, to catch the
        # race where AUTO_OFF / STOP_BUFFER_FILL reset state to IDLE
        # while HALL1 is still physically active (the next main_tick
        # would transition back to OVERFLOW, but until then a
        # freshly-arriving motion command could sneak a move in).
        if self._state == STATE_OVERFLOW or self.hall_overflow:
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
            'runout_recovery_pending':  self._runout_recovery_pending,
            'measure_load_active':      self._measure_load_active,
            'measure_load_distance_mm': self._measure_load_distance,
            'macro_state_saved':        self._macro_state_saved,
            # Config values (exposed so LOAD/UNLOAD macros don't hardcode)
            'feed_speed':               self.feed_speed,
            'manual_speed':             self.manual_speed,
            'burst_speed':              self.burst_speed,
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
