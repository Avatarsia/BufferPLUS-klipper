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
# STATE_LOAD_PHASE_2 = "LOAD_PHASE_2"  # P7-55b: entfernt mit cmd_BUFFER_LOAD_PHASE2
STATE_LOAD_PHASE_3   = "LOAD_PHASE_3"
STATE_UNLOAD_PHASE_3 = "UNLOAD_PHASE_3"
STATE_OVERFLOW       = "OVERFLOW"
STATE_RUNOUT         = "RUNOUT"
STATE_JAM            = "JAM"

# (UNLOAD_PHASE_1 und UNLOAD_PHASE_2 wurden in P7-20 durch SYNC_TO_EXTRUDER
# ersetzt und mit P7-23/P7-27 vollstaendig entfernt.)

# States where LOAD/UNLOAD is active — override commands
# (BUFFER_FEED/RETRACT/AUTO_ON/FORCE_BUFFER_FILL) must refuse.
# UNLOAD_PHASE_1/_2 wurden mit P7-20 obsolet (sync-mode Macro nutzt
# nur noch UNLOAD_PHASE_3 fuer den Buffer-allein-Retract).
BUSY_PHASE_STATES = {STATE_INITIAL_GRIP,
                     STATE_LOAD_PHASE_1, STATE_LOAD_PHASE_3,
                     STATE_UNLOAD_PHASE_3}

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
# Stable-Tracking Drop-Toleranz (P7-11): kurze Sensor-Flicker waehrend
# LOAD_PHASE_3 stable-exit tracking werden bis zu dieser Dauer
# toleriert. Sobald der Sensor innerhalb der Toleranz wieder aktiv ist,
# laeuft die Stable-Uhr weiter. Erst nach N Sekunden komplett-aus
# zaehlt das als echter Reset.
STABLE_DROP_GRACE  = 0.5             # s

# Triple-click action kinds.
CLICK_SINGLE = 1
CLICK_DOUBLE = 2
CLICK_TRIPLE = 3

BUTTON_FEED    = "feed"
BUTTON_RETRACT = "retract"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

class HallSensorMonitor:
    def __init__(self, owner, config):
        self.owner = owner
        self.printer = owner.printer
        self.reactor = owner.reactor

        # ----- Sensor pin state -----
        #
        # Polarity convention (matches the old [gcode_button]-based config):
        #
        # The Mellow LLL Plus uses a single arm that swings through tilt
        # positions based on buffer fill level. Each HALL is a photo-
        # interrupter that is BLOCKED by the arm in its associated tilt
        # position:
        #   HALL3 blocked -> buffer empty
        #   HALL2 blocked -> buffer full
        #   HALL1 blocked -> overflow
        #
        # Electrical: arm-blocked -> phototransistor OFF -> pullup holds
        # pin HIGH. Klipper config uses `^!` (pullup + invert), so the
        # Klipper button callback delivers state=False when pin is HIGH.
        #   -> arm blocked (threshold active)  <=>  state=False
        #   -> arm not blocking (threshold idle) <=>  state=True
        #
        # The extension then inverts ONCE more below (polarity_flip=True)
        # so `hall_empty`/`hall_full`/`hall_overflow` return True when
        # the corresponding threshold is active. This is NOT a double
        # invert bug - the config `!` handles the PHYSICAL polarity,
        # the Python flip handles the "button-language vs threshold-
        # language" semantic shift.
        #
        # Entrance switch uses `^!` with state=True = filament present
        # (standard filament_switch_sensor wiring).
        # Buttons use `^!` with state=True = pressed.
        self._pin_raw_state = {}
        self._pin_change_time = {}
        self._pin_stable_state = {}
        self._pin_polarity_flip = {
            'hall_empty': True,
            'hall_full': True,
            'hall_overflow': True,
            'entrance': False,
            'feed_button': False,
            'retract_button': False,
        }
        # Initial stable-state defaults - SAFETY-FIRST assumption:
        # Klipper's buttons.register_buttons only fires initial callbacks
        # for pins whose logical state != 0 at boot (last_button starts
        # at 0, changed = new XOR 0). For pins that are in their "idle"
        # logical state at boot, NO callback is delivered until the
        # state actually changes.
        #
        # Consequence: if we defaulted to "inactive" and a HALL sensor
        # was already physically active (e.g. HALL2 blocked because the
        # buffer is already full at Klipper restart), we would never hear
        # about it - and bang-bang would happily keep filling until
        # overflow / safety-timeout.
        #
        # Fix: default HALLs to "active" (stable=False -> semantic=True),
        # triggering OVERFLOW lockout at boot. As soon as Klipper
        # delivers an initial callback for pins that are actually idle
        # (the common case for HALL1), we transition out of lockout.
        # Entrance and buttons default to "not present / not pressed"
        # - that is the actual idle state for those switches, and
        # initial-insert events are further suppressed by the
        # _startup_grace_done gate below.
        for name, flip in self._pin_polarity_flip.items():
            if flip:
                idle_raw = False
            else:
                idle_raw = False
            self._pin_raw_state[name] = idle_raw
            self._pin_change_time[name] = 0.0
            self._pin_stable_state[name] = idle_raw

        # ----- Register pins via buttons module -----
        buttons = self.printer.load_object(config, 'buttons')
        self.register_pin(buttons, config, 'hall_empty_pin', 'hall_empty')
        self.register_pin(buttons, config, 'hall_full_pin', 'hall_full')
        self.register_pin(buttons, config, 'hall_overflow_pin', 'hall_overflow')
        self.register_pin(buttons, config, 'entrance_pin', 'entrance')
        self.register_pin(buttons, config, 'feed_button_pin', 'feed_button')
        self.register_pin(buttons, config, 'retract_button_pin', 'retract_button')

        # ----- Click detection state -----
        self._click_count = {BUTTON_FEED: 0, BUTTON_RETRACT: 0}
        self._last_click_time = {BUTTON_FEED: 0.0, BUTTON_RETRACT: 0.0}
        self._button_held = {BUTTON_FEED: False, BUTTON_RETRACT: False}
        # Deferred click summary: ein einziger _respond pro Click-Window,
        # statt einer Meldung pro Tastendruck. Aktionen feuern weiterhin
        # sofort (responsives UX), aber die Summary kommt erst nach dem
        # triple_click_window-Settling - so sieht der User bei einem
        # Triple-Klick "Triple-Burst" statt "Dauerlauf / Puls / Burst".
        self._pending_click_msg = {BUTTON_FEED: None, BUTTON_RETRACT: None}
        self._click_settle_timer = {BUTTON_FEED: None, BUTTON_RETRACT: None}

    def register_pin(self, buttons, config, config_key, logical_name):
        pin = config.get(config_key)

        def _callback(eventtime, raw_state, _ln=logical_name):
            self.on_pin_raw_change(eventtime, _ln, bool(raw_state))

        buttons.register_buttons([pin], _callback)

    def on_pin_raw_change(self, eventtime, name, raw_state):
        if raw_state == self._pin_raw_state[name]:
            return
        self._pin_raw_state[name] = raw_state
        self._pin_change_time[name] = eventtime

    def check_debounce(self, eventtime):
        threshold = self.owner.hall_debounce_ms / 1000.0
        for name, raw in self._pin_raw_state.items():
            stable = self._pin_stable_state[name]
            if stable == raw:
                continue
            if (eventtime - self._pin_change_time[name]) >= threshold:
                self._pin_stable_state[name] = raw
                self.on_stable_sensor_change(eventtime, name, raw)

    def semantic_state(self, name):
        raw = self._pin_stable_state[name]
        return (not raw) if self._pin_polarity_flip[name] else raw

    @property
    def hall_empty(self):
        return self.semantic_state('hall_empty')

    @property
    def hall_full(self):
        return self.semantic_state('hall_full')

    @property
    def hall_overflow(self):
        return self.semantic_state('hall_overflow')

    @property
    def entrance_detected(self):
        return self.semantic_state('entrance')

    @property
    def feed_button_pressed(self):
        return self.semantic_state('feed_button')

    @property
    def retract_button_pressed(self):
        return self.semantic_state('retract_button')

    def on_stable_sensor_change(self, eventtime, name, raw_state):
        del raw_state
        owner = self.owner
        if not owner._startup_grace_done:
            return
        if name == 'hall_overflow':
            if owner._is_hall1_active('sensor_callback'):
                owner._enter_overflow()
            else:
                # P7-46 (Issue #16): clear post-LOAD grace on HALL1-fall.
                # Buffer-Arm has dropped, normal sensor regime resumes.
                owner._post_load_overflow_grace = False
                owner._exit_overflow()
        elif name == 'hall_full':
            pass
        elif name == 'hall_empty':
            pass
        elif name == 'entrance':
            if owner.entrance_detected:
                owner._on_entrance_insert(eventtime)
            else:
                owner._on_entrance_runout(eventtime)
        elif name == 'feed_button':
            owner._on_button_change(BUTTON_FEED, owner.feed_button_pressed, eventtime)
        elif name == 'retract_button':
            owner._on_button_change(BUTTON_RETRACT, owner.retract_button_pressed, eventtime)

    def on_entrance_insert(self, eventtime):
        owner = self.owner
        owner._respond("Filament at entrance detected")
        if owner._runout_follow_active:
            owner._runout_follow_active = False
            owner._runout_filament_ref = None
            owner._respond("Runout-follow cancelled (filament re-inserted)")
        will_auto_grip = (owner._state == STATE_IDLE
                          and not owner._bang_bang_suspended
                          and not owner._auto_off_by_user
                          and not owner._halt_requested
                          and owner._entrance_was_empty)
        owner._entrance_was_empty = False
        if owner._state == STATE_RUNOUT:
            owner._set_state(STATE_IDLE)
            owner._runout_recovery_pending = True
            owner._respond("Reinsert during RUNOUT — cleared. Call "
                          "RESUME to continue (grip + fill runs "
                          "automatically), or BUFFER_AUTO_OFF + "
                          "FORCE_BUFFER_FILL for manual refill first.")
            return
        if owner._bang_bang_suspended:
            owner._respond("Reinsert during paused print — auto-grip suppressed. "
                          "Use FORCE_BUFFER_FILL to trigger manually after RESUME.")
            return
        if owner._auto_off_by_user:
            owner._respond("Reinsert while AUTO is off (operator-disabled) — "
                          "auto-grip suppressed. Use FORCE_BUFFER_FILL to trigger.")
            return
        if will_auto_grip:
            owner._start_initial_grip(eventtime)
        else:
            owner._respond("Entrance already had filament at boot — "
                          "no auto-grip. Use FORCE_BUFFER_FILL to "
                          "fill the buffer manually.")

    def on_entrance_runout(self, eventtime):
        owner = self.owner
        owner._entrance_was_empty = True
        if owner._state in (STATE_LOAD_PHASE_1, STATE_LOAD_PHASE_3,
                            STATE_UNLOAD_PHASE_3, STATE_MANUAL_FEED,
                            STATE_MANUAL_RETRACT):
            return

        if not owner._print_running:
            owner._respond("Entrance runout outside print — stepper off")
            owner._continuous_feed = False
            owner._halt_motion()
            owner._set_state(STATE_IDLE)
            return

        if owner.runout_pause:
            owner._respond("Runout during print — PAUSE (runout_pause=1)")
            owner._continuous_feed = False
            owner._halt_motion()
            owner._schedule_stepper_disable()
            owner._set_state(STATE_RUNOUT)
            owner._gcode_run_script("PAUSE")
        else:
            owner._respond("Runout — external sensor mode, %dmm follow"
                           % int(owner.runout_follow_mm))
            try:
                ps = owner.printer.lookup_object('print_stats')
                owner._runout_filament_ref = ps.get_status(eventtime).get('filament_used', 0.0)
            except Exception:
                owner._runout_filament_ref = 0.0
            owner._runout_follow_active = True

    def on_button_change(self, button_name, pressed, eventtime):
        if pressed:
            self._button_held[button_name] = True
            self.on_button_press(button_name, eventtime)
        else:
            was_held = self._button_held[button_name]
            self._button_held[button_name] = False
            if was_held:
                self.on_button_release(button_name, eventtime)

    def ensure_click_settle_timer(self, button_name):
        if self._click_settle_timer[button_name] is None:
            cb = lambda et, b=button_name: self.click_settle_fire(b, et)
            self._click_settle_timer[button_name] = self.reactor.register_timer(cb)

    def set_pending_click_msg(self, button_name, msg):
        self._pending_click_msg[button_name] = msg
        self.ensure_click_settle_timer(button_name)
        fire_time = self.reactor.monotonic() + self.owner.triple_click_window
        self.reactor.update_timer(self._click_settle_timer[button_name], fire_time)

    def click_settle_fire(self, button_name, eventtime):
        del eventtime
        msg = self._pending_click_msg[button_name]
        self._pending_click_msg[button_name] = None
        if msg is not None:
            self.owner._respond(msg)
        return self.reactor.NEVER

    def on_button_press(self, button_name, eventtime):
        owner = self.owner
        retract_overflow_override = (
            button_name == BUTTON_RETRACT
            and (owner._state == STATE_OVERFLOW or owner.hall_overflow)
            and owner._state != STATE_JAM
        )

        block_states = (STATE_LOAD_PHASE_1, STATE_LOAD_PHASE_3,
                        STATE_UNLOAD_PHASE_3, STATE_OVERFLOW, STATE_JAM,
                        STATE_INITIAL_GRIP)
        if owner._state in block_states and not retract_overflow_override:
            hint = ""
            if owner._state == STATE_JAM:
                hint = " — fix the cause, then BUFFER_CLEAR_JAM"
            elif owner._state == STATE_OVERFLOW:
                hint = " — clear HALL1 (lockout releases automatically); retract button is allowed"
            elif owner._state in (STATE_LOAD_PHASE_1,
                                  STATE_LOAD_PHASE_3, STATE_UNLOAD_PHASE_3):
                hint = " — wait for LOAD/UNLOAD to finish, or BUFFER_HALT"
            elif owner._state == STATE_INITIAL_GRIP:
                hint = " — wait for grip to finish, or STOP_BUFFER_FILL"
            self.set_pending_click_msg(
                button_name,
                "Button ignored — state=%s%s" % (owner._state, hint))
            return
        if owner.hall_overflow and not retract_overflow_override:
            self.set_pending_click_msg(
                button_name,
                "Button ignored — HALL1 overflow physically active "
                "(retract button still works to recover)")
            return

        if button_name == BUTTON_FEED and owner._measure_load_active:
            if owner._measure_feeding:
                owner._measure_feeding = False
                owner._continuous_feed = False
                owner._halt_motion()
                owner._measure_report()
                owner._measure_load_active = False
                owner._set_state(STATE_IDLE)
            else:
                owner._measure_feeding = True
                owner._measure_load_distance = 0.0
                owner._submit_move(owner.max_feed_distance, owner.manual_speed)
                owner._set_state(STATE_MANUAL_FEED)
                owner._respond("MEASURE_LOAD: feeder running — click again to stop")
            return

        now = eventtime
        if (now - self._last_click_time[button_name]) > owner.triple_click_window:
            self._click_count[button_name] = 1
        else:
            self._click_count[button_name] += 1
        self._last_click_time[button_name] = now

        cnt = self._click_count[button_name]
        if cnt == CLICK_SINGLE:
            owner._action_manual_start(button_name)
        elif cnt == CLICK_DOUBLE:
            owner._continuous_feed = False
            owner._halt_motion()
            owner._action_manual_pulse(button_name)
        elif cnt >= CLICK_TRIPLE:
            self._click_count[button_name] = 0
            owner._continuous_feed = False
            owner._halt_motion()
            if button_name == BUTTON_FEED and not owner.feed_burst_enabled:
                owner._action_manual_start(button_name)
            else:
                owner._action_burst(button_name)

    def on_button_release(self, button_name, eventtime):
        del eventtime
        owner = self.owner
        if owner._continuous_feed:
            desired_dir = +1 if button_name == BUTTON_FEED else -1
            if (owner._continuous_feed_direction == desired_dir
                    and not owner._measure_load_active):
                owner._continuous_feed = False
                owner._halt_motion()
                if owner._state in (STATE_MANUAL_FEED, STATE_MANUAL_RETRACT):
                    owner._start_cooldown()


class SyncCoordinator:
    def __init__(self, owner):
        self.owner = owner
        self.printer = owner.printer
        self.reactor = owner.reactor
        self.motion_queuing = None
        self.trapq = None
        self.trapq_append = None
        self._stepper_synced_to = None

    def setup_trapq(self, config):
        self.motion_queuing = self.printer.load_object(config, 'motion_queuing')
        self.trapq = self.motion_queuing.allocate_trapq()
        self.trapq_append = self.motion_queuing.lookup_trapq_append()

    def anchor_step(self):
        owner = self.owner
        owner._enable_stepper()
        anchor_dir = -1.0 if owner.hall_overflow else 1.0
        owner._submit_move(anchor_dir * 0.05, 10.0)
        owner._wait_for_move_done(direction=int(anchor_dir))
        owner._respond("Stepcompress anchor primed (boot %s 0.05mm)"
                      % ("retract" if anchor_dir < 0 else "feed"))

    def sync_to_extruder(self, extruder_name):
        owner = self.owner
        extruder = owner.printer.lookup_object(extruder_name)
        if not hasattr(extruder, 'get_trapq'):
            raise owner._cmd_error(
                "Object '%s' is not an extruder (no get_trapq method)"
                % extruder_name)
        # P7-46 (Issue #16): Gap-Reprime BEFORE the trapq-swap. The
        # own-trapq path in _submit_single_trapezoid (~Z. 2148) has a
        # gap-reprime check, but sync_to_extruder didn't — so if no
        # buffer-stepper move ran for > CLOCK_DIFF_MAX (~16.7s), the
        # first extruder-step after the swap would land at a
        # print_time-clock far ahead of the stale stepcompress cursor
        # → 'Invalid sequence' crash. Hardware-test 2026-04-26 saw
        # this with a 19.9s gap between LOAD-macro abort and UNLOAD's
        # SYNC (Issue #16 Re-Test). Refresh the cursor with a tiny
        # anchor-step (0.05mm, direction follows HALL1 to avoid
        # forward-feed when buffer is overfilled) so the swap finds
        # an up-to-date last_step_clock.
        REPRIME_GAP = 5.0
        mcu = owner.stepper.get_mcu()
        mcu_now = mcu.estimated_print_time(owner.reactor.monotonic())
        gap = mcu_now - owner._last_move_end_time
        if gap > REPRIME_GAP:
            owner._enable_stepper()
            anchor_dir = -1.0 if owner.hall_overflow else 1.0
            owner._submit_move(anchor_dir * 0.05, 10.0)
            owner._wait_for_move_done(direction=int(anchor_dir))
            owner._respond("Sync prep: own-trapq cursor refreshed "
                          "(idle %.1fs, anchor %s 0.05mm)"
                          % (gap, "retract" if anchor_dir < 0 else "feed"))
        toolhead = owner.printer.lookup_object('toolhead')
        try:
            toolhead.flush_step_generation()
            # P7-47: Feeder-Startposition auf die tatsächliche commanded_pos
            # des Extruder-Steppers setzen, nicht auf (0,0,0). Nach LOAD steht
            # der Extruder-Stepper intern bei z.B. 180mm; set_position((0,0,0))
            # würde den Feeder auf 0 setzen während itersolve Schritte ab 180mm
            # berechnet → alle Schritte landen bei t=0 → {i=0,c=N} Invalid
            # sequence. extruder.last_position ist die physikalische
            # commanded_pos des Extruder-Steppers (unabhängig von G92-Offsets)
            # — selbes Pattern wie Klipper's ExtruderStepper.sync_to_extruder
            # (extruder.py:968ff) und _read_extruder_position() in dieser Datei.
            _ext_pos = extruder.last_position
            owner.stepper.set_position((_ext_pos, 0., 0.))
            owner.stepper.set_trapq(extruder.get_trapq())
            self.motion_queuing.check_step_generation_scan_windows()
        except Exception:
            # P7-36: best-effort rollback so the finally-cleanup of any
            # caller (e.g. cmd_BUFFER_UNLOAD_FILAMENT) cannot mistake a
            # half-mutated stepper for a clean state. Clear the arming
            # flag last so a recursive failure inside the rollback still
            # leaves the cleanup guard disarmed (unsync_if_synced returns
            # False), avoiding double-rollback attempts.
            try:
                owner.stepper.set_trapq(self.trapq)
                self.motion_queuing.check_step_generation_scan_windows()
            except Exception:
                pass
            self._stepper_synced_to = None
            raise
        self._stepper_synced_to = extruder_name
        owner._stepcompress_primed = True
        owner._enable_stepper()
        owner._respond("Buffer-Feeder synced to '%s' — follows extruder moves"
                      % extruder_name)

    def unsync_if_synced(self):
        if self._stepper_synced_to is None:
            return False
        owner = self.owner
        toolhead = owner.printer.lookup_object('toolhead')
        toolhead.flush_step_generation()
        owner.stepper.set_position((0., 0., 0.))
        owner.stepper.set_trapq(self.trapq)
        self.motion_queuing.check_step_generation_scan_windows()
        self._stepper_synced_to = None
        owner._commanded_pos = 0.0
        mcu = owner.stepper.get_mcu()
        now_pt = mcu.estimated_print_time(owner.reactor.monotonic())
        owner._last_move_end_time = max(owner._last_move_end_time,
                                        now_pt + owner.lead_time)
        # P7-45 (Issue #16): catch deferred _exit_overflow. If HALL1
        # fell while we were synced, _exit_overflow short-circuited
        # to avoid stranding the stepper on extruder_trapq during a
        # state-transition. Now that the sync binding is released,
        # run the deferred exit so state-machine catches up.
        if (owner._state == STATE_OVERFLOW
                and not owner.hall_overflow):
            owner._exit_overflow()
        return True


class FaultManager:
    def __init__(self, owner):
        self.owner = owner
        self.printer = owner.printer
        self.reactor = owner.reactor
        self._overflow_interrupted_follow = False
        self._overflow_resume_mm = 0.0
        self._overflow_resume_dir = 0
        self._overflow_resume_spd = 0.0
        self._overflow_interrupted_state = None
        self._jam_active = False
        self._hall2_start_time = None
        self._hall2_start_extruder_pos = 0.0
        self._hall3_start_time = None

    def is_hall1_active(self, context):
        owner = self.owner
        if not owner.hall_overflow:
            return False

        phase3_overflow_ok = (owner._state == STATE_LOAD_PHASE_3
                              and owner._load_phase3_overflow_ok)
        if context == 'sensor_callback':
            if phase3_overflow_ok:
                return False
            if owner._state in (STATE_UNLOAD_PHASE_3, STATE_MANUAL_RETRACT):
                return False
            if owner._stepper_synced_to is not None:
                return False
            return True
        if context == 'main_tick':
            if owner._state in (STATE_OVERFLOW, STATE_MANUAL_RETRACT,
                                STATE_UNLOAD_PHASE_3):
                return False
            if phase3_overflow_ok:
                return False
            if owner._stepper_synced_to is not None:
                return False
            # P7-35 fault-overlay: skip re-trigger while overlay flag is
            # set (state stays LOAD_PHASE_3 in overlay mode, but the
            # main_tick poll would otherwise re-enter _enter_overflow on
            # every cycle).
            if owner.use_fault_overlay and owner._fault_overflow:
                return False
            # P7-46 (Issue #16): post-LOAD HALL1-grace. After Phase 3
            # exit via stable HALL1, the buffer is legitimately full —
            # main_tick must not bounce back to STATE_OVERFLOW. Cleared
            # when HALL1 actually falls (sensor_callback path) or via
            # operator cleanup.
            if owner._post_load_overflow_grace:
                return False
            return True
        if context == 'submit_move':
            return not phase3_overflow_ok
        if context in ('auto_on', 'phase3_entry'):
            return True
        raise ValueError("Unknown HALL1 context: %s" % (context,))

    def clear_recovery_flags(self):
        self._jam_active = False
        self._hall2_start_time = None
        self._hall3_start_time = None

    def resume_after_overflow(self):
        owner = self.owner
        # P7-45 (Issue #16): refuse to promote out of OVERFLOW while
        # SYNC is still active. _exit_overflow already short-circuits,
        # but direct callers (fault-overlay branch, future re-entry)
        # could land here with the sync binding intact. Promotion to
        # STATE_AUTO/STATE_LOAD_PHASE_1/STATE_INITIAL_GRIP would let
        # bang-bang or _submit_move queue moves to own_trapq while
        # the stepper is on extruder_trapq → moves go live at next
        # unsync, corrupting the stepcompress cursor.
        if owner._stepper_synced_to is not None:
            return
        interrupted = self._overflow_interrupted_state
        self._overflow_interrupted_state = None

        if (interrupted == STATE_INITIAL_GRIP
                and self._overflow_interrupted_follow):
            self._overflow_interrupted_follow = False
            if self._overflow_resume_mm > 0:
                owner._grip_follow_active = True
                owner._enable_stepper()
                owner._set_state(STATE_INITIAL_GRIP)
                owner._submit_move(
                    self._overflow_resume_dir * self._overflow_resume_mm,
                    self._overflow_resume_spd)
                self._overflow_resume_mm = 0.0
                return
            self._overflow_resume_mm = 0.0
            if owner.auto_load_after_follow:
                if owner._hotend_warm():
                    owner._schedule_gcode_script("LOAD_FILAMENT")
                else:
                    owner._respond(
                        "Auto-Load übersprungen: Hotend zu kalt"
                        " (%.0f/%.0f °C)" % (
                            owner._hotend_temp(), owner.min_temp))
            return

        if interrupted == STATE_LOAD_PHASE_1 and self._overflow_resume_mm > 0:
            owner._enable_stepper()
            owner._set_state(STATE_LOAD_PHASE_1)
            owner._pending_remaining_mm = self._overflow_resume_mm
            owner._pending_direction = self._overflow_resume_dir
            owner._pending_speed = self._overflow_resume_spd
            self._overflow_resume_mm = 0.0
            return

        # P7-36: in fault-overlay mode the cmd_BUFFER_LOAD_PHASE3 while-
        # loop is still spinning — keep _state=LOAD_PHASE_3 so the loop
        # continues feeding instead of falling through to STATE_AUTO and
        # silently returning success on an aborted phase 3.
        if (interrupted == STATE_LOAD_PHASE_3
                and owner.use_fault_overlay
                and owner._state == STATE_LOAD_PHASE_3):
            self._overflow_resume_mm = 0.0
            owner._enable_stepper()
            return

        self._overflow_resume_mm = 0.0
        if (owner.entrance_detected
                and not owner._auto_off_by_user
                and not owner._bang_bang_suspended
                and not owner._halt_requested):
            owner._enable_stepper()
            owner._set_state(STATE_AUTO)

    def check_auto_ready(self, allow_jam=False):
        owner = self.owner
        if self.is_hall1_active('auto_on') or owner._state == STATE_OVERFLOW:
            return "HALL1 overflow active"
        if not allow_jam and (owner._state == STATE_JAM or self._jam_active):
            return ("JAM active — inspect and call BUFFER_CLEAR_JAM, "
                    "or BUFFER_AUTO_OFF first.")
        if owner._state in BUSY_PHASE_STATES:
            return ("LOAD/UNLOAD in progress (state=%s) — call "
                    "STOP_BUFFER_FILL to abort first." % owner._state)
        if owner._bang_bang_suspended:
            return ("print is paused (bang-bang suspended). RESUME the "
                    "print — bang-bang re-engages automatically. If the "
                    "print is already finished, use BUFFER_AUTO_OFF first.")
        return None


# ---------------------------------------------------------------------------
# BufferFeeder
# ---------------------------------------------------------------------------

class BufferFeeder:
    # ----------------------------------------------------------------------
    # TODO(P7-30+): Fault-Overlay Migration (siehe Issue #15 Phase 4)
    # ----------------------------------------------------------------------
    # Aktuell sind OVERFLOW, RUNOUT und JAM exklusive _state-Werte. Das ist
    # eine flache State-Maschine mit Fault-States. Industriestandard fuer
    # Fault-Handling ist HSM mit Fault-Overlay-Flags: der "normale" State
    # bleibt erhalten (FILLING/FEEDING/IDLE), Faults sind orthogonale
    # Overlay-Flags (_fault_overflow, _fault_runout, _fault_jam) plus
    # Guard-Bedingungen.
    #
    # Vorteil: ein Resume-Pfad statt drei separate Mechanismen.
    #
    # Migration ist mehrere Tage Arbeit pro State und braucht parallele
    # Test-Coverage. Status der schrittweisen Umsetzung:
    #
    #   P7-30: ✅ Flag use_fault_overlay + Overlay-Felder eingefuehrt
    #   P7-35: ✅ cmd_BUFFER_LOAD_PHASE3 OVERFLOW-Behandlung migriert
    #          (_enter_overflow laesst state=LOAD_PHASE_3 statt
    #           STATE_OVERFLOW zu kippen wenn use_fault_overlay=1)
    #   P7-?? offen: _enter_overflow / _exit_overflow fuer alle anderen
    #          States auf Overlay umstellen
    #   P7-?? offen: _trigger_jam / BUFFER_CLEAR_JAM auf Overlay
    #   P7-?? offen: RUNOUT-Pfade migrieren
    #   P7-?? offen: LOAD_PHASE_1/2/3 zu LOAD-Substate kollabieren
    #
    # Mit use_fault_overlay=1 ist NUR der LOAD_PHASE_3-Pfad migriert.
    # Alle anderen Fault-Pfade (OVERFLOW ausserhalb Phase 3, RUNOUT,
    # JAM) laufen weiterhin als state-flip. Migration paused — wird
    # bei Bedarf einzeln wieder aufgenommen.
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
        # unload_phase3_speed: Geschwindigkeit fuer BUFFER_UNLOAD_PHASE3 (Buffer
        # allein zieht Filament rueckwaerts bis Eingang frei). Separat von
        # unload_fast_speed, damit der synced G1 E-{sync_dist} Move langsam
        # bleiben kann (Extruder-Blockierung bei hoher Geschwindigkeit) waehrend
        # PHASE3 wieder schnell laeuft. Default = unload_fast_speed (rueckwaerts-
        # kompatibel: wer keinen Wert setzt, behalt das bisherige Verhalten).
        self.unload_phase3_speed = config.getfloat('unload_phase3_speed',
                                                   self.unload_fast_speed,
                                                   above=0.)
        self.grip_speed         = config.getfloat('grip_speed',         55., above=0.)
        self.accel              = config.getfloat('accel',            1000., above=0.)

        # ----- Config: distances / durations -----
        self.manual_chunk_distance = config.getfloat('manual_chunk_distance', 10.,  above=0.)
        self.burst_distance        = config.getfloat('burst_distance',       1300., above=0.)
        self.grip_duration         = config.getfloat('grip_duration',          10., above=0.)
        self.grip_follow_distance  = config.getfloat('grip_follow_distance',   0., minval=0.)
        self.grip_follow_speed     = config.getfloat('grip_follow_speed',      30., above=0.)
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

        # ----- Config: behaviour -----
        self.auto_load_after_follow = config.getboolean('auto_load_after_follow', False)
        # Bang-bang kommt mit Print-Start automatisch hoch, wenn Filament
        # am Eingang ist und kein Operator-Lockout aktiv. Auf False setzen,
        # um Bang-bang nur ueber explizites BUFFER_AUTO_ON zu starten.
        self.auto_engage_on_print_start = config.getboolean('auto_engage_on_print_start', True)
        # AUTO direkt beim Klipper-Boot engagen wenn Filament am Eingang
        # da ist und kein Overflow aktiv. Damit reagiert der Buffer auch
        # auf manuelle Mainsail-Extrusionen ohne aktiven Print — sonst
        # wuerde nach ~30 mm manueller Extrusion der Buffer leer laufen
        # und das Filament im Hauptextruder grinden. Auf False setzen
        # falls der Buffer beim Boot trotz Filament im IDLE bleiben soll.
        self.auto_engage_on_boot = config.getboolean('auto_engage_on_boot', True)
        self.min_temp               = config.getfloat('min_temp', 180., minval=0.)
        self.use_python_unload      = config.getint('use_python_unload', 0)
        self.use_fault_overlay      = config.getboolean('use_fault_overlay', False)
        # P7-52: Flush-driven bang-bang. When enabled, bang-bang feed
        # decisions ride on Klipper's MCU flush cycle (motion_queuing.
        # register_flush_callback) rather than the 50ms reactor tick.
        # Move-submits anchor at step_gen_time + lead_time which is
        # safe by Klipper-mainline contract — no toolhead-anker race,
        # no cursor-decay. Default off until hardware-validated.
        self.use_flush_callback_bang_bang = config.getboolean(
            'use_flush_callback_bang_bang', False)

        # ----- Stepper + trapq -----
        self.sync = SyncCoordinator(self)
        self._setup_trapq(config)
        self.motion_queuing = self.sync.motion_queuing
        self.trapq = self.sync.trapq
        self.trapq_append = self.sync.trapq_append

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
        # P7-20: sync-to-extruder state. Wenn != None ist der Buffer-
        # Stepper an einen externen Extruder-Trapq gebunden (folgt
        # G1 E Moves 1:1). BUFFER_SYNC_TO_EXTRUDER setzt, BUFFER_UNSYNC
        # cleart. Anwendungsfall: UNLOAD-Tip-Forming, Filament-Fluss
        # durch den Buffer ohne Sensor-Trigger.
        self._stepper_synced_to = None
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

        # ----- Sensors + buttons -----
        self.sensors = HallSensorMonitor(self, config)
        self._pin_raw_state = self.sensors._pin_raw_state
        self._pin_change_time = self.sensors._pin_change_time
        self._pin_stable_state = self.sensors._pin_stable_state
        self._pin_polarity_flip = self.sensors._pin_polarity_flip
        self._click_count = self.sensors._click_count
        self._last_click_time = self.sensors._last_click_time
        self._button_held = self.sensors._button_held
        self._pending_click_msg = self.sensors._pending_click_msg
        self._click_settle_timer = self.sensors._click_settle_timer

        # ----- Operation flags -----
        self.fault = FaultManager(self)
        self._state = STATE_INIT
        # Bang-bang is paused while the printer is in a paused/ended
        # print context (idle_timeout != printing). The flag is armed
        # on idle_timeout:ready/idle only if we were actively printing,
        # and cleared on idle_timeout:printing. Manual BUFFER_AUTO_ON
        # outside a print stays active — the flag never gets armed.
        self._bang_bang_suspended = False
        self._initial_grip_end_time = None
        self._grip_follow_active = False
        # P7-55: redundant overflow-resume init removed — FaultManager
        # already initializes _overflow_interrupted_follow,
        # _overflow_resume_mm/dir/spd, _overflow_interrupted_state in
        # its own __init__ (Z. 566-570). Property setters here would
        # have just re-set the same backing fields to the same defaults.
        self._load_phase3_distance = 0.0
        self._load_phase3_max_distance = 0.0
        self._load_phase3_speed = 0.0       # per-call feed speed in phase 3
        # Stable-exit-Tracking fuer Phase 3 (P7-8). STABLE_TIMEOUT=N
        # bedeutet: HALL2 (oder HALL1 wenn OVERFLOW_OK=1) muss N Sekunden
        # KONTINUIERLICH aktiv sein, bevor Phase 3 sauber beendet wird.
        # Reset bei Trigger-Loss faengt das Bowden-Widerstand-Zucken ab,
        # wo der Arm kurz gegen HALL1/HALL2 schlaegt aber sofort
        # zurueckfaellt — solche Spikes setzen die Stoppuhr zurueck.
        # STABLE_TIMEOUT=0 = altes Verhalten (Instant-Exit beim ersten
        # Trigger). OVERFLOW_OK=1 = HALL1-Stable als legitimer Exit
        # (Buffer ist ueberfuellt → Filament ist da → Phase 2 fertig).
        self._load_phase3_stable_timeout = 0.0
        self._load_phase3_overflow_ok = False
        self._load_phase3_chunk_distance = 10.0
        self._load_phase3_hall_full_since = None
        self._load_phase3_hall_overflow_since = None
        # Drop-Toleranz (P7-11): kurze Sensor-Flicker (Bowden-Spring zieht
        # den Arm fuer wenige ms zurueck) sollen die Stable-Stoppuhr
        # nicht hart resetten. Stattdessen Grace-Period — wenn der Sensor
        # innerhalb der Toleranz wieder triggered, faengt die alte Uhr an
        # weiterzulaufen. Erst wenn er N Sekunden komplett aus bleibt,
        # zaehlt das als echter Reset.
        self._load_phase3_hall_full_drop_since = None
        self._load_phase3_hall_overflow_drop_since = None

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
        self._pending_disable = False       # deferred stepper disable (while move in flight)
        self._feed_deadline_time = None     # max feed deadline (reactor time)
        self._measure_load_active = False
        self._measure_load_distance = 0.0
        # Explicit toggle tracking so the first button click after
        # MEASURE_LOAD_START always starts the feed, regardless of
        # whatever _continuous_feed was doing in AUTO before.
        self._measure_feeding = False
        self._print_running = False
        self._jam_active = False
        # Fault-overlay migration (P7-30 roadmap, partially completed):
        # use_fault_overlay=1 enables the LOAD_PHASE_3 overflow overlay
        # (P7-35 — _enter_overflow leaves state=LOAD_PHASE_3 instead of
        # flipping to STATE_OVERFLOW). RUNOUT and JAM paths remain on
        # the legacy state-flip pattern; their overlay-fields below
        # stay as scaffolding for a future migration step.
        self._fault_overflow = False
        self._fault_runout = False
        self._fault_jam = False
        self._fault_pre_overflow_state = None
        # P7-46 (Issue #16): Post-LOAD HALL1-bounce-suppression. Set
        # when LOAD_PHASE_3 with overflow_ok=1 exits via stable HALL1
        # (treating as full). Buffer is legitimately overfilled — the
        # main_tick path would otherwise re-trigger _enter_overflow on
        # the next cycle and bounce state IDLE/AUTO → OVERFLOW. Cleared
        # when HALL1 actually falls (sensor_callback) or via operator
        # cleanup (BUFFER_AUTO_OFF, STOP_BUFFER_FILL, BUFFER_HALT).
        self._post_load_overflow_grace = False

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
        self._retract_burst_done = False  # go IDLE after retract burst, not AUTO

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

        # P7-52: Flush-driven bang-bang. Registered unconditionally so
        # we can toggle the feature flag at runtime; the callback
        # itself is a no-op when the flag is off, falling through to
        # the legacy reactor-tick path. Mainline Klipper API:
        # klippy/extras/motion_queuing.py:106 register_flush_callback
        # fires synchronously inside the MCU flush cycle with
        # signature (flush_time, step_gen_time). Anchoring submits at
        # step_gen_time + lead_time bypasses the toolhead.get_last_
        # move_time anchor that breaks reactive bang-bang during
        # mid-toolhead-move (P7-50 lesson learned the hard way).
        if hasattr(self.motion_queuing, 'register_flush_callback'):
            self.motion_queuing.register_flush_callback(
                self._on_mcu_flush, can_add_trapq=True)

        # ----- GCode registrations -----
        # P7-40: Migration register_command -> register_mux_command.
        # Mux-Key 'BUFFER' folgt der Klipper-Mainline-Konvention fuer
        # load_config_prefix-Module mit Single-Type-Identifier. User-Aufruf:
        #   BUFFER_AUTO_ON BUFFER=mellow
        # Mux-Value = self.name (bei aktuellem Setup "mellow", aus
        # [buffer_feeder mellow]). Mehrere Instanzen koennen denselben
        # Command-Namen registrieren, der Dispatcher waehlt via BUFFER=...
        gcode = self.printer.lookup_object('gcode')
        gcode.register_mux_command('BUFFER_FEED', 'BUFFER', self.name,
                                   self.cmd_BUFFER_FEED,
                                   desc=self.cmd_BUFFER_FEED_help)
        gcode.register_mux_command('BUFFER_RETRACT', 'BUFFER', self.name,
                                   self.cmd_BUFFER_RETRACT,
                                   desc=self.cmd_BUFFER_RETRACT_help)
        gcode.register_mux_command('BUFFER_HALT', 'BUFFER', self.name,
                                   self.cmd_BUFFER_HALT,
                                   desc=self.cmd_BUFFER_HALT_help)
        gcode.register_mux_command('BUFFER_AUTO_ON', 'BUFFER', self.name,
                                   self.cmd_BUFFER_AUTO_ON,
                                   desc=self.cmd_BUFFER_AUTO_ON_help)
        gcode.register_mux_command('BUFFER_AUTO_ON_IF_READY', 'BUFFER', self.name,
                                   self.cmd_BUFFER_AUTO_ON_IF_READY,
                                   desc=self.cmd_BUFFER_AUTO_ON_IF_READY_help)
        gcode.register_mux_command('BUFFER_AUTO_OFF', 'BUFFER', self.name,
                                   self.cmd_BUFFER_AUTO_OFF,
                                   desc=self.cmd_BUFFER_AUTO_OFF_help)
        gcode.register_mux_command('BUFFER_WAIT_IDLE', 'BUFFER', self.name,
                                   self.cmd_BUFFER_WAIT_IDLE,
                                   desc=self.cmd_BUFFER_WAIT_IDLE_help)
        gcode.register_mux_command('BUFFER_LOAD_PHASE1', 'BUFFER', self.name,
                                   self.cmd_BUFFER_LOAD_PHASE1,
                                   desc=self.cmd_BUFFER_LOAD_PHASE1_help)
        # P7-55b: BUFFER_LOAD_PHASE2 entfernt — durch SYNC_TO_EXTRUDER
        # ersetzt. Siehe Kommentar bei cmd_BUFFER_LOAD_PHASE3.
        gcode.register_mux_command('BUFFER_LOAD_PHASE3', 'BUFFER', self.name,
                                   self.cmd_BUFFER_LOAD_PHASE3,
                                   desc=self.cmd_BUFFER_LOAD_PHASE3_help)
        gcode.register_mux_command('BUFFER_UNLOAD_FILAMENT', 'BUFFER', self.name,
                                   self.cmd_BUFFER_UNLOAD_FILAMENT,
                                   desc=self.cmd_BUFFER_UNLOAD_FILAMENT_help)
        gcode.register_mux_command('BUFFER_UNLOAD_PHASE3', 'BUFFER', self.name,
                                   self.cmd_BUFFER_UNLOAD_PHASE3,
                                   desc=self.cmd_BUFFER_UNLOAD_PHASE3_help)
        gcode.register_mux_command('BUFFER_SYNC_TO_EXTRUDER', 'BUFFER', self.name,
                                   self.cmd_BUFFER_SYNC_TO_EXTRUDER,
                                   desc=self.cmd_BUFFER_SYNC_TO_EXTRUDER_help)
        gcode.register_mux_command('BUFFER_UNSYNC', 'BUFFER', self.name,
                                   self.cmd_BUFFER_UNSYNC,
                                   desc=self.cmd_BUFFER_UNSYNC_help)
        gcode.register_mux_command('FORCE_BUFFER_FILL', 'BUFFER', self.name,
                                   self.cmd_FORCE_BUFFER_FILL,
                                   desc=self.cmd_FORCE_BUFFER_FILL_help)
        gcode.register_mux_command('STOP_BUFFER_FILL', 'BUFFER', self.name,
                                   self.cmd_STOP_BUFFER_FILL,
                                   desc=self.cmd_STOP_BUFFER_FILL_help)
        gcode.register_mux_command('BUFFER_STATE_DUMP', 'BUFFER', self.name,
                                   self.cmd_BUFFER_STATE_DUMP,
                                   desc=self.cmd_BUFFER_STATE_DUMP_help)
        gcode.register_mux_command('CALIBRATE_FEEDER_SYNC', 'BUFFER', self.name,
                                   self.cmd_CALIBRATE_FEEDER_SYNC,
                                   desc=self.cmd_CALIBRATE_FEEDER_SYNC_help)
        gcode.register_mux_command('MEASURE_LOAD_START', 'BUFFER', self.name,
                                   self.cmd_MEASURE_LOAD_START,
                                   desc=self.cmd_MEASURE_LOAD_START_help)
        gcode.register_mux_command('MEASURE_LOAD_STOP', 'BUFFER', self.name,
                                   self.cmd_MEASURE_LOAD_STOP,
                                   desc=self.cmd_MEASURE_LOAD_STOP_help)
        gcode.register_mux_command('ENABLE_RUNOUT_SENSOR', 'BUFFER', self.name,
                                   self.cmd_ENABLE_RUNOUT_SENSOR,
                                   desc="Set print_running=1 — enable runout PAUSE")
        gcode.register_mux_command('DISABLE_RUNOUT_SENSOR', 'BUFFER', self.name,
                                   self.cmd_DISABLE_RUNOUT_SENSOR,
                                   desc="Set print_running=0 — disable runout PAUSE")
        gcode.register_mux_command('BUFFER_CLEAR_JAM', 'BUFFER', self.name,
                                   self.cmd_BUFFER_CLEAR_JAM,
                                   desc="Clear JAM state after operator intervention")
        gcode.register_mux_command('BUFFER_RESTORE_STATE', 'BUFFER', self.name,
                                   self.cmd_BUFFER_RESTORE_STATE,
                                   desc="Best-effort restore of gcode-state saved by a failed LOAD/UNLOAD")
        gcode.register_mux_command('BUFFER_SAVE_MACRO_STATE', 'BUFFER', self.name,
                                   self.cmd_BUFFER_SAVE_MACRO_STATE,
                                   desc="Internal: mark gcode-state as saved (used by _SAVE_E_MODE)")
        gcode.register_mux_command('BUFFER_RESTORE_MACRO_STATE', 'BUFFER', self.name,
                                   self.cmd_BUFFER_RESTORE_MACRO_STATE,
                                   desc="Internal: restore + clear gcode-state save (used by _RESTORE_E_MODE)")

        logging.info("buffer_feeder '%s' initialised", self.name)

    # -----------------------------------------------------------------------
    # Pin registration helper
    # -----------------------------------------------------------------------

    def _register_pin(self, buttons, config, config_key, logical_name):
        """Read pin from config and register a raw-state callback."""
        return self.sensors.register_pin(buttons, config, config_key, logical_name)

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
        # If no filament is present at the entrance on boot, arm the
        # edge-detect flag so the first real insert triggers auto-grip
        # without requiring a pull-and-reinsert cycle. Without this,
        # _entrance_was_empty stays False (its init value) and the first
        # insert is silently ignored because no "empty" edge was ever seen.
        if not self.entrance_detected:
            self._entrance_was_empty = True
        # Log the settled sensor picture so operators can sanity-check
        # polarity against physical reality on a fresh boot.
        self._respond(
            "Startup grace done — hall_empty=%s hall_full=%s "
            "hall_overflow=%s entrance=%s"
            % (self.hall_empty, self.hall_full,
               self.hall_overflow, self.entrance_detected))
        # P7-18/19: Anchor-Step beim Boot — etabliert stepcompress
        # last_step_clock auf einen echten Wert. Hintergrund: ohne
        # ersten Step seit Klipper-Boot bleibt last_step_clock=0
        # (allocator init-state). Spaeter, wenn der erste echte Move
        # (z.B. UNLOAD Phase 2 retract oder Bang-Bang feed) >17s nach
        # Boot kommt, scheitert das Re-Prime via flush+set_position
        # zuverlaessig zurueckzusetzen → "stepcompress Invalid sequence"
        # im flush_handler. Solange wir den ersten Step beim Boot
        # machen (MCU-Clock noch klein, last_step_clock=0 → gap klein),
        # laeuft der Move ohne Crash und last_step_clock ist etabliert.
        # 0.05mm = ~250 Steps, physisch kaum spuerbar. Forward (Filament-
        # Foerderrichtung) bei normalem Boot — entspricht der User-
        # Erwartung "Buffer fettet kurz an". Bei HALL1-Boot retract,
        # weil _submit_move forward-rejects bei aktivem hall_overflow.
        try:
            self._anchor_step()
        except Exception:
            logging.exception("buffer_feeder: boot anchor failed")
        # Drop into normal operation. If HALL1 is currently active,
        # main_tick will immediately transition to OVERFLOW.
        # P7-15: optional direkt zu AUTO wenn Filament da ist — manuelle
        # Mainsail-Extrusionen brauchen Bang-Bang um nicht nach ~30 mm
        # leer zu laufen. Bei aktivem Overflow oder fehlender Filament-
        # Praesenz fallen wir auf IDLE zurueck (uebliches Verhalten).
        if (self.auto_engage_on_boot
                and self.entrance_detected
                and not self._is_hall1_active('auto_on')):
            self._set_state(STATE_AUTO)
            self._respond("AUTO engaged on boot — filament at entrance, "
                          "buffer follows extruder demand")
        else:
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
        # Klipper fires idle_timeout:printing during MCU init even without
        # an active print (print_stats state = 'standby'). Guard against
        # this boot artifact so _print_running is only armed for real prints.
        try:
            ps = self.printer.lookup_object('print_stats', None)
            if ps is not None:
                if ps.get_status(self.reactor.monotonic()).get(
                        'state', '') == 'standby':
                    return
        except Exception:
            pass
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
            self._clear_recovery_flags()
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
            return

        # Auto-engage Bang-bang beim Print-Start.
        #
        # Wir wollen, dass der Buffer im Druck mitlaeuft, ohne dass der
        # User das Boot-autostart-Feature pflegen oder BUFFER_AUTO_ON in
        # PRINT_START selbst eintragen muss. Bedingung: Filament am
        # Eingang, State ist IDLE, kein Operator-Lockout aktiv.
        # Konfigurierbar via auto_engage_on_print_start (default True).
        if (self.auto_engage_on_print_start
                and self._state == STATE_IDLE
                and self.entrance_detected
                and not self._auto_off_by_user
                and not self._halt_requested
                and not self._is_hall1_active('auto_on')):
            self._respond("Print start — engaging AUTO")
            self._enable_stepper()
            self._set_state(STATE_AUTO)

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
        #
        # Guard: Klipper fires idle_timeout:printing then :ready during
        # MCU init, which would set _print_running=True and then arm
        # _bang_bang_suspended before any real print has started.
        # Ignore all idle_timeout events until the startup grace is done.
        if not self._startup_grace_done:
            self._print_running = False
            return
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
        return self.sensors.on_pin_raw_change(eventtime, name, raw_state)

    def _check_debounce(self, eventtime):
        """Promote raw->stable after hall_debounce_ms."""
        return self.sensors.check_debounce(eventtime)

    def _semantic_state(self, name):
        """Return semantic 'active' bool from stable raw state."""
        return self.sensors.semantic_state(name)

    # Convenience accessors (always up-to-date with debounced state).
    @property
    def hall_empty(self):
        return self.sensors.hall_empty

    @property
    def hall_full(self):
        return self.sensors.hall_full

    @property
    def hall_overflow(self):
        return self.sensors.hall_overflow

    @property
    def entrance_detected(self):
        return self.sensors.entrance_detected

    @property
    def feed_button_pressed(self):
        return self.sensors.feed_button_pressed

    @property
    def retract_button_pressed(self):
        return self.sensors.retract_button_pressed

    @property
    def _stepper_synced_to(self):
        return self.sync._stepper_synced_to

    @_stepper_synced_to.setter
    def _stepper_synced_to(self, value):
        self.sync._stepper_synced_to = value

    @property
    def _jam_active(self):
        return self.fault._jam_active

    @_jam_active.setter
    def _jam_active(self, value):
        self.fault._jam_active = value

    @property
    def _hall2_start_time(self):
        return self.fault._hall2_start_time

    @_hall2_start_time.setter
    def _hall2_start_time(self, value):
        self.fault._hall2_start_time = value

    @property
    def _hall2_start_extruder_pos(self):
        return self.fault._hall2_start_extruder_pos

    @_hall2_start_extruder_pos.setter
    def _hall2_start_extruder_pos(self, value):
        self.fault._hall2_start_extruder_pos = value

    @property
    def _hall3_start_time(self):
        return self.fault._hall3_start_time

    @_hall3_start_time.setter
    def _hall3_start_time(self, value):
        self.fault._hall3_start_time = value

    @property
    def _overflow_interrupted_follow(self):
        return self.fault._overflow_interrupted_follow

    @_overflow_interrupted_follow.setter
    def _overflow_interrupted_follow(self, value):
        self.fault._overflow_interrupted_follow = value

    @property
    def _overflow_resume_mm(self):
        return self.fault._overflow_resume_mm

    @_overflow_resume_mm.setter
    def _overflow_resume_mm(self, value):
        self.fault._overflow_resume_mm = value

    @property
    def _overflow_resume_dir(self):
        return self.fault._overflow_resume_dir

    @_overflow_resume_dir.setter
    def _overflow_resume_dir(self, value):
        self.fault._overflow_resume_dir = value

    @property
    def _overflow_resume_spd(self):
        return self.fault._overflow_resume_spd

    @_overflow_resume_spd.setter
    def _overflow_resume_spd(self, value):
        self.fault._overflow_resume_spd = value

    @property
    def _overflow_interrupted_state(self):
        return self.fault._overflow_interrupted_state

    @_overflow_interrupted_state.setter
    def _overflow_interrupted_state(self, value):
        self.fault._overflow_interrupted_state = value

    def _is_hall1_active(self, context):
        return self.fault.is_hall1_active(context)

    def _on_stable_sensor_change(self, eventtime, name, raw_state):
        """Dispatch stable sensor change to the right handler."""
        return self.sensors.on_stable_sensor_change(eventtime, name, raw_state)

    # -----------------------------------------------------------------------
    # Overflow (HALL1) — hard priority
    # -----------------------------------------------------------------------

    def _enter_overflow(self):
        self._respond("*** HALL1 OVERFLOW — Feeder disabled, lockout engaged ***")
        self._continuous_feed = False
        # Save the interrupted state and pending distance BEFORE
        # _halt_motion() zeroes _pending_remaining_mm, so _exit_overflow
        # can resume the move after HALL1 clears.
        self._overflow_interrupted_state = self._state
        self._overflow_resume_mm  = self._pending_remaining_mm
        self._overflow_resume_dir = self._pending_direction
        self._overflow_resume_spd = self._pending_speed
        self._halt_motion()
        self._schedule_stepper_disable()
        if self._grip_follow_active:
            self._overflow_interrupted_follow = True
            self._grip_follow_active = False
            self._initial_grip_end_time = None
        # P7-35 fault-overlay: in overlay mode for LOAD_PHASE_3, keep
        # _state=LOAD_PHASE_3 and only set the overlay flag. The phase3
        # cmd loop terminates via fault_overflow check, postcheck raises
        # like the legacy STATE_OVERFLOW path.
        self._fault_overflow = True
        self._fault_pre_overflow_state = self._state
        if self.use_fault_overlay and self._state == STATE_LOAD_PHASE_3:
            return
        self._set_state(STATE_OVERFLOW)

    def _clear_recovery_flags(self):
        """Clear jam-related recovery flags reused by cleanup paths."""
        return self.fault.clear_recovery_flags()

    def _resume_after_overflow(self):
        """Restore the pre-overflow workflow if it is still resumable."""
        return self.fault.resume_after_overflow()

    def _exit_overflow(self):
        # P7-45 (Issue #16): defer state-transition while SYNC is active.
        # Otherwise we'd transition OVERFLOW → IDLE → AUTO while the
        # stepper is still bound to extruder_trapq. The next bang-bang
        # tick or _submit_move would then queue moves to own_trapq —
        # those moves go live at the next BUFFER_UNSYNC and corrupt
        # the stepcompress cursor (see Eifel-Joe's hardware log: SYNC
        # #1 → HALL1-fall → OVERFLOW→IDLE→AUTO → UNSYNC → SYNC#2 →
        # 'stepcompress Invalid sequence'). The macro will call
        # BUFFER_UNSYNC itself; SyncCoordinator.unsync_if_synced
        # re-runs this method once the sync binding is released.
        if self._stepper_synced_to is not None:
            return
        # P7-35 fault-overlay: clear overlay flag without state change
        # when overlay path is active. _resume_after_overflow handles
        # restarting the interrupted phase3 move via _overflow_resume_*.
        if (self.use_fault_overlay
                and self._fault_overflow
                and self._state == STATE_LOAD_PHASE_3):
            self._fault_overflow = False
            self._fault_pre_overflow_state = None
            self._respond("HALL1 cleared — overflow lockout released (overlay)")
            self._resume_after_overflow()
            return
        if self._state != STATE_OVERFLOW:
            return
        self._respond("HALL1 cleared — overflow lockout released")
        # Go to IDLE (the _set_state hook calls _halt_motion + stepper-disable).
        self._set_state(STATE_IDLE)
        self._fault_overflow = False
        self._fault_pre_overflow_state = None
        self._resume_after_overflow()

    # -----------------------------------------------------------------------
    # Entrance (buffer_entrance) events
    # -----------------------------------------------------------------------

    def _on_entrance_insert(self, eventtime):
        return self.sensors.on_entrance_insert(eventtime)

    def _on_entrance_runout(self, eventtime):
        return self.sensors.on_entrance_runout(eventtime)

    # -----------------------------------------------------------------------
    # Button events
    # -----------------------------------------------------------------------

    def _on_button_change(self, button_name, pressed, eventtime):
        return self.sensors.on_button_change(button_name, pressed, eventtime)

    def _ensure_click_settle_timer(self, button_name):
        return self.sensors.ensure_click_settle_timer(button_name)

    def _set_pending_click_msg(self, button_name, msg):
        return self.sensors.set_pending_click_msg(button_name, msg)

    def _click_settle_fire(self, button_name, eventtime):
        return self.sensors.click_settle_fire(button_name, eventtime)

    def _on_button_press(self, button_name, eventtime):
        return self.sensors.on_button_press(button_name, eventtime)

    def _on_button_release(self, button_name, eventtime):
        return self.sensors.on_button_release(button_name, eventtime)

    def _action_manual_start(self, button_name):
        direction = +1 if button_name == BUTTON_FEED else -1
        target_state = STATE_MANUAL_FEED if button_name == BUTTON_FEED else STATE_MANUAL_RETRACT
        self._start_continuous_motion(direction, self.manual_speed, None)
        self._set_state(target_state)
        self._set_pending_click_msg(button_name, "%s: Dauerlauf" % button_name)

    def _action_manual_pulse(self, button_name):
        direction = +1 if button_name == BUTTON_FEED else -1
        target_state = STATE_MANUAL_FEED if button_name == BUTTON_FEED else STATE_MANUAL_RETRACT
        self._set_state(target_state)
        self._submit_move(direction * self.manual_chunk_distance, self.manual_speed)
        self._schedule_return_to_auto_after_move()
        self._set_pending_click_msg(button_name,
            "%s: %d mm Puls" % (button_name, self.manual_chunk_distance))

    def _action_burst(self, button_name):
        direction = +1 if button_name == BUTTON_FEED else -1
        target_state = STATE_MANUAL_FEED if button_name == BUTTON_FEED else STATE_MANUAL_RETRACT
        self._set_state(target_state)
        self._submit_move(direction * self.burst_distance, self.burst_speed)
        if direction < 0:
            # Retract burst: operator is deliberately pulling filament back.
            # Stay IDLE afterwards — jam timer must not race against an empty
            # buffer. Operator calls BUFFER_AUTO_ON to re-engage.
            self._retract_burst_done = True
        self._schedule_return_to_auto_after_move(cooldown=self.reenable_cooldown_fast)
        self._set_pending_click_msg(button_name,
            "%s: Triple-Burst %d mm @ %d mm/s"
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

            # HALL1 has absolute priority — AUSSER bei aktivem Manual-
            # Retract oder einer UNLOAD-Phase: dann lassen wir den
            # Operator/das Macro den Buffer entlasten. Sobald die
            # Retract-Sequenz endet, greift der Reassert wieder normal.
            # OVERFLOW_OK=1 in Phase 3 (P7-8): unterdrueckt den Auto-
            # Transition zu STATE_OVERFLOW, damit _load_phase3_tick das
            # HALL1-Stable-Tracking selbst auswerten kann. Ohne diesen
            # Skip wuerde die State-Machine den Stepper sofort beim
            # ersten HALL1-Spike lockouten — aber wir wollen ja gerade
            # den Spike vom dauerhaften "Buffer wirklich ueberfuellt"
            # unterscheiden. _is_hall1_active('main_tick') kapselt
            # diese caller-spezifischen Bypasses.
            if self._is_hall1_active('main_tick'):
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
                    "max_feed_time %ds reached without HALL2 — motor stall, "
                    "empty spool, or value too low for setup (typical 2m "
                    "bowden+buffer fill at %dmm/s needs ~90s; bump "
                    "max_feed_time in lll.cfg if first-fill is legit)"
                    % (int(self.max_feed_time), int(self.feed_speed)))

            # max_feed_distance is a forward-feed safety only. Manual
            # retract (Retract-Taster Dauerlauf, BUFFER_RETRACT without
            # DISTANCE) legitimately accumulates large distances in the
            # opposite direction; tripping a JAM on those is a bug.
            if (self._continuous_feed
                    and self._continuous_feed_direction == 1
                    and self._feed_distance_accumulator >= self.max_feed_distance):
                self._trigger_jam(
                    "SAFETY_DISTANCE",
                    "max_feed_distance %dmm reached without HALL2 — slipping "
                    "drive gear, kinked filament, or value too low for setup "
                    "(bowden+buffer path; bump max_feed_distance in lll.cfg "
                    "if first-fill is legit)"
                    % int(self.max_feed_distance))

            # Deferred disable: motor_disable must not be called while steps
            # are unprocessed in the trapq (step-gen fires motor_enable with
            # a past time via add_active_callback → Timer too close).
            if self._pending_disable and not self._move_in_flight():
                self._pending_disable = False
                self._disable_stepper()

            # Cooldown end: back to AUTO if entrance present AND the
            # operator hasn't explicitly disabled AUTO.
            if self._cooldown_deadline is not None and eventtime >= self._cooldown_deadline:
                if self._state in (STATE_MANUAL_FEED, STATE_MANUAL_RETRACT, STATE_INITIAL_GRIP):
                    # Guard: estimate may fire early (per-chunk gap not accounted
                    # for). Only transition once the move is truly done.
                    if self._move_in_flight() or self._pending_remaining_mm > 0:
                        self._cooldown_deadline = eventtime + 0.05
                    else:
                        self._cooldown_deadline = None
                        if (self.entrance_detected
                                and not self._is_hall1_active('auto_on')
                                and not self._auto_off_by_user
                                and not self._bang_bang_suspended
                                and not self._retract_burst_done):
                            self._set_state(STATE_AUTO)
                        else:
                            self._retract_burst_done = False
                            self._set_state(STATE_IDLE)
                else:
                    self._cooldown_deadline = None

            # Initial grip done: start follow-feed (if configured) or go IDLE.
            if self._state == STATE_INITIAL_GRIP and self._initial_grip_end_time is not None:
                mcu = self.stepper.get_mcu()
                now_pt = mcu.estimated_print_time(eventtime)
                if now_pt >= self._initial_grip_end_time:
                    self._initial_grip_end_time = None
                    if self._auto_off_by_user or self._bang_bang_suspended:
                        self._set_state(STATE_IDLE)
                        self._respond("Initial grip done — staying IDLE "
                                      "(AUTO off by operator or print paused)")
                    elif self.grip_follow_distance > 0:
                        self._grip_follow_active = True
                        self._respond(
                            "Initial grip done — follow feed: %.0f mm @ %.0f mm/s"
                            % (self.grip_follow_distance, self.grip_follow_speed))
                        self._submit_move(self.grip_follow_distance,
                                          self.grip_follow_speed)
                        # State stays STATE_INITIAL_GRIP; pending streaming
                        # handles chunk queuing. Completion detected below.
                    else:
                        self._set_state(STATE_IDLE)
                        self._respond("Initial grip done — IDLE")
                        if self.auto_load_after_follow:
                            if self._hotend_warm():
                                self._schedule_gcode_script("LOAD_FILAMENT")
                            else:
                                self._respond(
                                    "Auto-Load übersprungen: Hotend zu kalt"
                                    " (%.0f/%.0f °C)" % (
                                        self._hotend_temp(), self.min_temp))

            # Follow-feed completion: grip + follow done, drop to IDLE.
            if (self._state == STATE_INITIAL_GRIP
                    and self._grip_follow_active
                    and self._initial_grip_end_time is None
                    and not self._move_in_flight()
                    and self._pending_remaining_mm <= 0):
                self._grip_follow_active = False
                self._set_state(STATE_IDLE)
                self._respond("Grip follow done — IDLE")
                if self.auto_load_after_follow:
                    if self._hotend_warm():
                        self._schedule_gcode_script("LOAD_FILAMENT")
                    else:
                        self._respond(
                            "Auto-Load übersprungen: Hotend zu kalt"
                            " (%.0f/%.0f °C)" % (
                                self._hotend_temp(), self.min_temp))

            # Bang-bang nur in AUTO. (P7-16 erweiterte das auf
            # UNLOAD_PHASE_1, aber P7-20 hat den Tip-Forming-Pfad
            # auf SYNC_TO_EXTRUDER umgestellt — UNLOAD_PHASE_1 wird
            # nicht mehr betreten.)
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
                        self._runout_filament_ref = None
                        self._runout_follow_active = False
                        self._set_state(STATE_IDLE)  # calls _schedule_stepper_disable
                except Exception:
                    pass

            # LOAD Phase 3 — feed until HALL2 or max distance.
            if self._state == STATE_LOAD_PHASE_3:
                self._load_phase3_tick(eventtime)

            # P7-55b: ehemaliger LOAD_PHASE_2 Auto-IDLE-Block entfernt.
            # cmd_BUFFER_LOAD_PHASE2 ist seit P7-44 nicht mehr im
            # LOAD_FILAMENT-Macro genutzt (durch SYNC_TO_EXTRUDER
            # ersetzt) und wurde mit P7-55b komplett aus der API
            # entfernt. UNLOAD_PHASE_2 wurde schon in P7-20 durch
            # SYNC_TO_EXTRUDER abgeloest.

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
                elif (self._pending_direction < 0
                        and self._state == STATE_MANUAL_RETRACT
                        and not self.entrance_detected):
                    self._halt_motion()
                    self._respond("Retract-Burst gestoppt — Filament am Eingang weg")
                elif self._pending_speed > 0:
                    chunk_duration = (self.max_move_chunk_mm
                                      / self._pending_speed)
                    mcu = self.stepper.get_mcu()
                    now_pt = mcu.estimated_print_time(eventtime)
                    gap = self._last_move_end_time - now_pt
                    # Submit next chunk when <= half-a-chunk remains
                    # in the currently-queued move, so next trapezoid
                    # starts right at the prior one's end_time.
                    if gap <= chunk_duration * 0.5:
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
        """HALL-based bang-bang with hysteresis. Reactor-tick driven —
        anchors submits via toolhead.get_last_move_time which fights
        against active toolhead-moves (lag during manual G1 E50). The
        flush-callback path (_on_mcu_flush, P7-52) is the preferred
        replacement when use_flush_callback_bang_bang is enabled."""
        if self._bang_bang_suspended:
            # Print is paused — do nothing until idle_timeout:printing.
            return
        # P7-52: when flush-callback bang-bang is active, the
        # reactor-tick path becomes a no-op so we don't double-submit.
        # Stop-on-HALL2 is still needed via flush-callback path itself.
        if self.use_flush_callback_bang_bang:
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

    def _on_mcu_flush(self, flush_time, step_gen_time):
        """P7-52: Flush-callback driven bang-bang. Klipper's motion_
        queuing module fires this synchronously inside the MCU flush
        cycle (klippy/extras/motion_queuing.py:155). We have two
        timing parameters from the caller:

          flush_time     — last time steps were sent to the MCU
          step_gen_time  — last time Klipper generated steps (>= flush_time)

        Anchoring our submit at step_gen_time + lead_time guarantees
        the move lands in the very next flush iteration without
        racing against any toolhead-anchor or stale stepcompress
        cursor. This was the architectural fix needed to make
        bang-bang reactive during mid-toolhead-moves (P7-50 attempted
        the same goal via mcu_now-anchor in _submit_single_trapezoid
        and crashed; the flush-callback path is the safe equivalent
        because Klipper itself dictates the anchor time).
        """
        if not self.use_flush_callback_bang_bang:
            return
        if self._bang_bang_suspended:
            return
        if self._state != STATE_AUTO:
            # Macros and operator commands own non-AUTO states. Bang-
            # bang only acts in AUTO. LOAD/UNLOAD-driven SYNC paths
            # are handled by their own macros, not by us.
            return
        if self._stepper_synced_to is not None:
            # An explicit BUFFER_SYNC_TO_EXTRUDER (macro path) is in
            # effect. Stay out of the way — submitting our own move
            # while synced would queue trapezoids on the wrong trapq.
            return

        # Hard-safety: HALL1 forward-reject still applies. Without
        # this, an overfilled buffer would keep getting fed.
        if self._is_hall1_active('submit_move'):
            return

        if self.hall_full:
            # Buffer voll: stop. Pending streamed chunks drain via
            # _last_move_end_time naturally; clearing the streaming
            # flag prevents new chunks from being requested.
            if self._continuous_feed:
                self._continuous_feed = False
        elif self.hall_empty:
            # Buffer leer: feed. step_gen_time + lead_time is the
            # safe anchor — Klipper just told us the cursor is here.
            if not self._continuous_feed:
                anchor = step_gen_time + self.lead_time
                self._submit_move(self.max_move_chunk_mm,
                                   self.feed_speed,
                                   forced_t0=anchor)
                self._continuous_feed = True
                self._continuous_feed_direction = 1
                self._continuous_feed_speed = self.feed_speed
        else:
            # Zwischen-Zone: hysteresis.
            pass

    def _load_phase3_tick(self, eventtime):
        threshold = self._load_phase3_stable_timeout
        # HALL2 (full) Stabilitaets-Tracking mit Drop-Toleranz (P7-11):
        # kurze False-Edges (<STABLE_DROP_GRACE) lassen die Stable-Uhr
        # weiterlaufen — der Bowden-Spring drueckt den Arm zurueck, der
        # Stepper foerdert weiter, der Arm geht wieder hoch. Hard-Reset
        # nur wenn der Sensor laenger als die Grace komplett aus bleibt.
        if self.hall_full:
            self._load_phase3_hall_full_drop_since = None
            if self._load_phase3_hall_full_since is None:
                self._load_phase3_hall_full_since = eventtime
            full_dwell = eventtime - self._load_phase3_hall_full_since
            if full_dwell >= threshold:
                self._continuous_feed = False
                self._halt_motion()
                self._load_phase3_hall_full_since = None
                self._load_phase3_hall_overflow_since = None
                self._load_phase3_hall_full_drop_since = None
                self._load_phase3_hall_overflow_drop_since = None
                if threshold > 0:
                    self._respond("LOAD Phase 3: HALL2 stable %.1fs, "
                                  "buffer full" % full_dwell)
                else:
                    self._respond("LOAD Phase 3: HALL2 reached, buffer full")
                # P7-49 (Hardware-Test 2026-04-27): AUTO ist nach
                # einem deliberate LOAD das erwartete State —
                # unabhaengig davon ob ein Print laeuft. Frueher hat
                # der _print_running-Guard das auf IDLE gezwungen, mit
                # der Begruendung "spontane Toolhead-Pulls duerfen
                # nicht bang-bang triggern". Aber: nach einem
                # erfolgreichen LOAD ist der naechste Schritt fast
                # immer eine Extrusion (manuell oder Print) — und ohne
                # AUTO bekommt das den Buffer nicht nachgefuettert.
                # _post_load_overflow_grace haelt _main_tick davon ab,
                # zurueck nach OVERFLOW zu kippen waehrend HALL1
                # asserted ist; bei HALL1-fall wird der grace gecleart.
                if (self.entrance_detected
                        and not self._auto_off_by_user
                        and not self._halt_requested):
                    self._set_state(STATE_AUTO)
                else:
                    self._set_state(STATE_IDLE)
                return
        elif self._load_phase3_hall_full_since is not None:
            # Sensor gerade abgefallen — Grace-Window starten/checken.
            if self._load_phase3_hall_full_drop_since is None:
                self._load_phase3_hall_full_drop_since = eventtime
            elif (eventtime - self._load_phase3_hall_full_drop_since
                  >= STABLE_DROP_GRACE):
                self._load_phase3_hall_full_since = None
                self._load_phase3_hall_full_drop_since = None
        # HALL1 (overflow) Stabilitaets-Tracking — nur wenn OVERFLOW_OK=1
        # gesetzt wurde. Sonst ist der HALL1-Pfad weiterhin via
        # _main_tick → _enter_overflow → state=OVERFLOW abgewickelt
        # (alter Pfad, raised im cmd_BUFFER_LOAD_PHASE3-Postcheck).
        if self._load_phase3_overflow_ok:
            if self.hall_overflow:
                self._load_phase3_hall_overflow_drop_since = None
                if self._load_phase3_hall_overflow_since is None:
                    self._load_phase3_hall_overflow_since = eventtime
                overflow_dwell = eventtime - self._load_phase3_hall_overflow_since
                if overflow_dwell >= threshold:
                    self._continuous_feed = False
                    self._halt_motion()
                    self._load_phase3_hall_full_since = None
                    self._load_phase3_hall_overflow_since = None
                    self._load_phase3_hall_full_drop_since = None
                    self._load_phase3_hall_overflow_drop_since = None
                    self._respond("LOAD Phase 3: HALL1 stable %.1fs, "
                                  "buffer overfilled (treating as full)"
                                  % overflow_dwell)
                    # P7-46 (Issue #16): suppress _main_tick from
                    # re-triggering _enter_overflow now that we have
                    # legitimately accepted the overfilled buffer as
                    # the LOAD-success exit. Cleared on HALL1-fall.
                    self._post_load_overflow_grace = True
                    # P7-49: see HALL2-Exit branch above — AUTO is the
                    # natural post-LOAD state regardless of print state.
                    if (self.entrance_detected
                            and not self._auto_off_by_user
                            and not self._halt_requested):
                        self._set_state(STATE_AUTO)
                    else:
                        self._set_state(STATE_IDLE)
                    return
            elif self._load_phase3_hall_overflow_since is not None:
                # Same drop-tolerance pattern wie bei HALL2.
                if self._load_phase3_hall_overflow_drop_since is None:
                    self._load_phase3_hall_overflow_drop_since = eventtime
                elif (eventtime - self._load_phase3_hall_overflow_drop_since
                      >= STABLE_DROP_GRACE):
                    self._load_phase3_hall_overflow_since = None
                    self._load_phase3_hall_overflow_drop_since = None
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
            # P7-35 fault-overlay: pause move submission while overlay
            # flag is set. _enter_overflow already halted current motion;
            # re-submitting here would immediately re-saturate HALL1.
            if self.use_fault_overlay and self._fault_overflow:
                return
            # P7-48 (Hardware-Test 2026-04-27): if HALL1 is asserted —
            # buffer is already full, the stable-timer is what we want
            # to elapse, NOT more filament-push. Submitting another
            # chunk while HALL1 is on stuffs filament against the
            # extruder clamp via the bowden, which then bleeds through
            # the heatbreak into the nozzle (visible as filament
            # squirting from the nozzle pre-extrude). Hold the chunk
            # stream while the timer counts up; HALL1-fall (drop-grace)
            # naturally re-arms the submit path.
            if self.hall_overflow:
                return
            # Clip chunk so the per-call MAX_DISTANCE is a hard cap.
            remaining = self._load_phase3_max_distance - self._load_phase3_distance
            chunk = min(self._load_phase3_chunk_distance, remaining)
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

            # P7-53 (Hardware-Test 2026-04-27): Jam-detection is a
            # PRINT-only safety. The CLOG detector triggers when HALL2
            # stays active while the extruder accumulates extrusion —
            # but during manual workflows (PA-tuning's
            # _CLIENT_LINEAR_MOVE E=50 F=480, manual purge, BUFFER_FEED
            # tests) HALL2 is naturally active for many seconds AND
            # the extruder is moving. That's normal behaviour, not a
            # clog. Only run jam-detection when idle_timeout signals
            # an active print.
            if not self._print_running:
                self._hall2_start_time = None
                self._hall3_start_time = None
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
        self._respond("*** JAM %s: %s ***" % (kind, message))
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
        """Pick a safe print_time for the next motor_enable/disable."""
        mcu = self.stepper.get_mcu()
        mcu_now = mcu.estimated_print_time(self.reactor.monotonic())
        toolhead = self.printer.lookup_object('toolhead')
        th_now = toolhead.get_last_move_time()
        pt = max(mcu_now + self.lead_time,
                 th_now + self.lead_time,
                 self._last_move_end_time + self.lead_time,
                 self._last_enable_schedule_time + self.lead_time)
        self._last_enable_schedule_time = pt
        return pt

    def _schedule_stepper_disable(self):
        """Disable stepper, deferring to tick if a move is in flight.

        Calling motor_disable while steps are still unprocessed in the
        trapq causes Klipper to register an add_active_callback that
        fires motor_enable(past_time) when the step-generator processes
        those steps.  set_digital(past_time, 1) then causes the MCU to
        raise 'Timer too close'.  Deferring until flight=False lets the
        step-generator finish before motor_disable touches the callbacks.
        """
        if self._move_in_flight():
            self._pending_disable = True
        else:
            self._disable_stepper()

    def _enable_stepper(self):
        if self._stepper_enable is None:
            return
        self._pending_disable = False   # cancel any deferred disable
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

    def _submit_move(self, signed_distance, speed, forced_t0=None):
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
        # OVERFLOW: nur Forward-Submits ablehnen. Retract (signed_distance < 0)
        # ist die einzige Recovery-Bewegung, die einen überfüllten Buffer
        # entlasten kann — sonst sitzt der User in der Sackgasse.
        # Ausnahme (P7-8): LOAD_PHASE_3 mit OVERFLOW_OK=1 darf weiterfeeden
        # waehrend HALL1 aktiv — sonst koennte das Stable-Tracking nie
        # die Schwelle erreichen, weil der Arm bei jedem Reject zurueck-
        # faellt und HALL1 deaktiviert. _load_phase3_tick beendet die
        # Phase sauber sobald HALL1 stable lange genug ist.
        if self._is_hall1_active('submit_move') and signed_distance > 0:
            logging.warning("buffer_feeder: forward move rejected — HALL1 active "
                            "(distance=%.1f speed=%.1f)", signed_distance, speed)
            self._continuous_feed = False
            self._pending_remaining_mm = 0.0
            return

        # Cancel any previously-streaming sequence before starting new.
        self._pending_remaining_mm = 0.0

        # Ensure the first chunk starts no earlier than the last enable/disable
        # toggle scheduled on the MCU. Without this guard, a move submitted
        # right after an enable (e.g. LOAD_PHASE1 after IDLE→disable) would
        # send a trapezoid with t0 < enable_time → MCU "Timer too close".
        self._last_move_end_time = max(self._last_move_end_time,
                                       self._last_enable_schedule_time)

        distance_abs = abs(signed_distance)
        direction = 1.0 if signed_distance > 0 else -1.0

        first_chunk = min(distance_abs, self.max_move_chunk_mm)
        self._submit_single_trapezoid(direction * first_chunk, speed,
                                       forced_t0=forced_t0)
        remaining = distance_abs - first_chunk
        if remaining > 0:
            self._pending_remaining_mm = remaining
            self._pending_direction = direction
            self._pending_speed = speed

    def _submit_single_trapezoid(self, signed_distance, speed,
                                  forced_t0=None):
        """Append one trapezoid to our trapq. Low-level primitive.

        forced_t0 (P7-52): when not None, overrides the t0 anchor with
        the explicit value. Used by the flush-callback bang-bang path
        which receives step_gen_time from Klipper and can compute a
        race-free anchor at step_gen_time + lead_time. The default
        (None) keeps the existing toolhead-anchor logic for the
        legacy reactor-tick path.
        """
        # Prime/re-prime stepcompress nach Idle-Pause die laenger ist als
        # CLOCK_DIFF_MAX (Klipper: 3<<28 ticks = ~16.7s @ 48MHz). Dahinter
        # laeuft compress_bisect_add in degenerierte Sequenzen ein
        # ("stepcompress o=X i=0 c=N a=0: Invalid sequence" → MCU shutdown).
        #
        # Loesung: toolhead.flush_step_generation() drainiert alle pending
        # Toolhead-Bewegungen und syncted last_step_gen_time fuer ALLE
        # Syncemitter (inkl. unserem Buffer-Feeder-Stepper). set_position
        # danach setzt _commanded_pos und itersolve's commanded_pos auf 0,
        # damit der naechste trapq_append einen sauberen start_pos_x hat.
        #
        # KEIN _print_running-Guard: der Re-Prime muss IMMER laufen, auch
        # mid-print. Drei Iterationen Detach/Reattach (P7-2 bis P7-5)
        # haben versucht das mid-print elegant zu vermeiden, aber alle
        # crashten an genau dem Pfad wo _print_running=True war (UNLOAD
        # mid-print, Bang-bang nach Heater-Warmup). Mid-print-Stall durch
        # flush_step_generation ist ein paar Millisekunden, bei UNLOAD/
        # erstem Buffer-Move kaum spuerbar — Crash-Vermeidung wiegt schwerer.
        REPRIME_GAP = 5.0
        mcu = self.stepper.get_mcu()
        mcu_now = mcu.estimated_print_time(self.reactor.monotonic())
        gap = mcu_now - self._last_move_end_time
        need_reprime = (not self._stepcompress_primed) or (gap > REPRIME_GAP)
        if need_reprime:
            try:
                toolhead = self.printer.lookup_object('toolhead')
                toolhead.flush_step_generation()
                logging.info("buffer_feeder: stepcompress re-primed via "
                             "flush_step_generation (gap=%.1fs)", gap)
            except Exception:
                logging.exception("buffer_feeder: flush_step_generation failed")
            self.stepper.set_position((0., 0., 0.))
            self._commanded_pos = 0.0
            self._stepcompress_primed = True

        self._enable_stepper()

        # Time base selection:
        # For streaming chunks (previous chunk still in the future):
        #   use _last_move_end_time directly so chunks abut without
        #   gap. Calling get_last_move_time() here would include queued
        #   toolhead/extruder moves (e.g. LOAD Phase 2 G1 E180) which
        #   push t0 tens of seconds into the future — feeder stops mid-
        #   phase while the extruder runs alone.
        # For the first chunk after idle (or gap recovery):
        #   use toolhead.get_last_move_time() to anchor to the MCU's
        #   step-gen cursor. Without this, estimated_print_time drifts
        #   during long idle and the first step lands at a clock the MCU
        #   has no baseline for → "Invalid sequence" shutdown.
        # mcu/mcu_now sind oben fuer den Re-Prime-Gap-Check schon
        # berechnet — wiederverwenden statt neu fetchen.
        if forced_t0 is not None:
            # P7-52 flush-callback path: caller provides a step_gen_
            # time-based anchor that is race-free against Klipper's
            # MCU flush cycle. Honor _last_move_end_time as the floor
            # so streaming chunks still abut without gap.
            t0 = max(forced_t0, self._last_move_end_time)
        elif self._last_move_end_time > mcu_now + self.lead_time:
            # Streaming: previous chunk is still in the future — abut.
            t0 = self._last_move_end_time
        else:
            # First chunk / gap: anchor to toolhead print_time.
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
            self._halt_motion()
            self._schedule_stepper_disable()
            # P7-36: ensure overlay flag is not stale across an abort
            # path that bypasses _exit_overflow (STOP_BUFFER_FILL,
            # BUFFER_HALT, BUFFER_AUTO_OFF). _exit_overflow only fires
            # on HALL1 fall-edge — direct state transitions to IDLE
            # while HALL1 is still asserted would otherwise leave
            # _fault_overflow=True, blocking later main_tick re-entry.
            self._fault_overflow = False
            self._fault_pre_overflow_state = None

    # -----------------------------------------------------------------------
    # Helper: gcode interactions
    # -----------------------------------------------------------------------

    def _schedule_gcode_script(self, script):
        """Run a gcode script from a deferred one-shot reactor timer.

        gc.run_script() blocks until the script finishes. Calling it
        directly from a reactor timer callback (like _main_tick) prevents
        that timer from returning — so _main_tick never reschedules
        itself, and the pending-chunk streaming block starves. Deferring
        by 1ms lets _main_tick return first, so it fires normally on its
        20ms cadence while the gcode script runs in a separate timer.
        """
        def _cb(eventtime):
            self._gcode_run_script(script)
            return self.reactor.NEVER
        self.reactor.register_timer(_cb, self.reactor.monotonic() + 0.001)

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

    def _gcode_run_script_checked(self, script, from_command=False):
        """Run a gcode script and propagate failures to the caller."""
        gc = self.printer.lookup_object('gcode')
        if from_command:
            gc.run_script_from_command(script)
        else:
            gc.run_script(script)

    def _respond(self, message):
        # Log + console echo. M117 wird hier bewusst NICHT emittiert:
        # _respond wird sowohl aus reactor-event handlers als auch
        # aus gcode command handlers gerufen, und gc.run_script
        # re-acquired die gcode mutex. Aus einem command handler
        # (wo die Mutex bereits gehalten wird) wuerde der Aufruf
        # Klippers ganze gcode pipeline deadlocken.
        logging.info("buffer_feeder: %s", message)
        try:
            gc = self.printer.lookup_object('gcode')
            gc.respond_info("BufferFeeder: %s" % message)
        except Exception:
            pass

    def _hotend_temp(self):
        try:
            ex = self.printer.lookup_object('extruder')
            return ex.get_heater().get_temp(self.reactor.monotonic())[0]
        except Exception:
            return 0.0

    def _hotend_warm(self):
        return self._hotend_temp() >= self.min_temp

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
        # P7-24: falls Sync-to-Extruder gerade aktiv ist (HALT mitten
        # in UNLOAD_FILAMENT zwischen BUFFER_SYNC_TO_EXTRUDER und
        # BUFFER_UNSYNC), den Stepper sofort vom Extruder-Trapq abkoppeln
        # bevor wir den lokalen State manipulieren.
        if self._unsync_if_synced():
            self._respond("HALT — also unsynced from extruder")
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
        # P7-46: clear post-LOAD grace on operator HALT.
        self._post_load_overflow_grace = False
        # Preserve safety-lockout states (OVERFLOW / JAM); any other
        # state drops to IDLE. Our _set_state(STATE_IDLE) hook also
        # disables the stepper.
        if self._state not in (STATE_OVERFLOW, STATE_JAM):
            self._set_state(STATE_IDLE)
        # Arm the abort flag so any pending WAIT_IDLE in a macro
        # propagates the halt as a Klipper error.
        self._halt_requested = True
        self._respond("HALT — workflow will abort at next wait")

    def _check_auto_ready(self, allow_jam=False):
        """Pruefe Voraussetzungen fuer AUTO-Eintritt. Liefert None wenn OK,
        sonst eine User-faced Fehlermeldung. allow_jam=True wird von
        BUFFER_CLEAR_JAM genutzt, das den JAM-Lockout selbst bereits
        aufloest und nur die anderen Guards weiter abfragen will.
        """
        return self.fault.check_auto_ready(allow_jam=allow_jam)

    cmd_BUFFER_AUTO_ON_help = "Enable bang-bang auto mode"
    def cmd_BUFFER_AUTO_ON(self, gcmd):
        reason = self._check_auto_ready()
        if reason is not None:
            raise self._cmd_error("Cannot enable AUTO while " + reason)
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

    cmd_BUFFER_AUTO_ON_IF_READY_help = ("Enable bang-bang auto mode if precondition guard "
                                         "passes. Otherwise log skip-reason and return without "
                                         "raising. Used by macros where the AUTO call follows a "
                                         "LOAD/UNLOAD that may legitimately leave HALL1 active.")
    def cmd_BUFFER_AUTO_ON_IF_READY(self, gcmd):
        # P7-46 (Issue #16): macro-render-time vs runtime fix.
        # Klipper-Jinja-macros render the whole macro body once at
        # macro-start. A `{% if bf.hall_overflow %}`-guard around
        # BUFFER_AUTO_ON evaluates the snapshot from macro-start —
        # the actual sensor reading at the time AUTO is reached can
        # be different (e.g. LOAD Phase 3 ends with HALL1 active).
        # This command does the runtime-check in Python, returning
        # quietly if the guard rejects, so the macro continues.
        reason = self._check_auto_ready()
        # P7-49: Phase 3 stable-HALL1-Exit setzt _post_load_overflow_
        # grace=True, signalisiert "HALL1 active ist legitim, gerade
        # erfolgreich beendet". Akzeptiere AUTO-engage trotz HALL1
        # in genau diesem Fenster — _main_tick respektiert grace
        # separat und laesst kein _enter_overflow durch. Bei
        # HALL1-fall (Filament durch Extruder gepullt) wird grace
        # via sensor_callback geclearet, normales Regime resumed.
        if (reason is not None
                and "HALL1 overflow active" in reason
                and self._post_load_overflow_grace):
            reason = None
        if reason is not None:
            self._respond("AUTO not engaged: " + reason)
            return
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
        # P7-24: Stepper auch vom Extruder-Trapq abkoppeln falls
        # ein Macro-Abbruch ihn dort haengen lies.
        if self._unsync_if_synced():
            self._respond("AUTO_OFF — also unsynced from extruder")
        self._continuous_feed = False
        self._halt_motion()
        self._pending_remaining_mm = 0.0
        self._clear_recovery_flags()
        self._runout_follow_active = False
        self._runout_filament_ref = None
        self._measure_load_active = False
        self._measure_feeding = False
        self._cooldown_deadline = None
        self._bang_bang_suspended = False  # operator overrides PAUSE-suspend
        self._auto_off_by_user = True      # but reinsert auto-grip stays blocked
        self._runout_recovery_pending = False
        self._halt_requested = True
        # P7-46: operator-explicit AUTO_OFF clears the post-LOAD grace
        # so HALL1 returns to its normal lockout regime.
        self._post_load_overflow_grace = False
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

    def _wait_for_move_done(self, gcmd=None, direction=+1,
                            allow_overflow=False):
        """Internal: block until both in-flight and pending-stream
        moves are done, OR an emergency condition trips.

        Used by blocking phase commands that legitimately hold the
        busy-phase state during the wait. External callers should
        use cmd_BUFFER_WAIT_IDLE instead, which additionally waits
        for the busy-phase state to be vacated.

        Early-exits on HALT / OVERFLOW / JAM because at that point
        the motor has already been disabled (or is about to be) and
        waiting out the nominal trapq end_time is pointless.

        direction=-1 (UNLOAD/Retract): OVERFLOW/JAM blockieren nicht
        — Retract ist Recovery. Nur HALT bricht ab.

        allow_overflow=True (P7-12): forward-direction wait der den
        HALL1-Overflow-Check skipt. Genutzt von LOAD_PHASE_3 mit
        OVERFLOW_OK=1 — die Stable-Exit-Logik haendelt HALL1 selbst,
        und der Standard _raise_if_locked_out wuerde den Wait am
        Ende mit "HALL1 OVERFLOW active — aborting" raisen, bevor
        die Stable-Logik je laufen kann. JAM bleibt absolut.
        """
        while self._move_in_flight() or self._pending_remaining_mm > 0:
            if self._abort_signalled():
                break
            self.reactor.pause(self.reactor.monotonic() + 0.05)
        if gcmd is not None:
            if allow_overflow:
                if self._state == STATE_JAM or self._jam_active:
                    raise self._cmd_error(
                        "BufferFeeder: JAM active — aborting. "
                        "Use BUFFER_CLEAR_JAM after inspection. "
                        "(UNLOAD is allowed.)")
            else:
                self._raise_if_locked_out(gcmd, direction=direction)

    def _wait_for_move_done_resume_on_overflow(self, gcmd=None):
        """Like _wait_for_move_done but waits out HALL1 overflow instead of
        aborting. Used in LOAD_PHASE_1 so a HALL1 event mid-phase pauses the
        feeder and resumes automatically after HALL1 clears (_exit_overflow
        restores _pending_remaining_mm so streaming continues).
        Only hard aborts (HALT, JAM) terminate the wait early.
        """
        while (self._move_in_flight()
               or self._pending_remaining_mm > 0
               or self.hall_overflow
               or self._state == STATE_OVERFLOW):
            if self._halt_requested or self._jam_active or self._state == STATE_JAM:
                break
            self.reactor.pause(self.reactor.monotonic() + 0.1)
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

    def _check_phase_entry(self, cmd_name, allowed_states):
        """Reject a phase command if the current state isn't in the
        allow-list. Callers pass exactly the states from which a legit
        progression (or idempotent re-entry) is permitted — e.g. each
        phase command accepts its own STATE_* for retry idempotence,
        plus IDLE/AUTO/RUNOUT for the normal entry path. UNLOAD-phase
        commands also accept OVERFLOW/JAM because UNLOAD is the
        recovery operation for those lockouts.
        """
        if self._state in allowed_states:
            return
        raise self._cmd_error(
            "%s rejected — wrong state (state=%s, expected one of %s). "
            "Use BUFFER_HALT or BUFFER_CLEAR_JAM/BUFFER_AUTO_OFF to clear, "
            "or BUFFER_STATE_DUMP to inspect."
            % (cmd_name, self._state, sorted(allowed_states)))

    cmd_BUFFER_LOAD_PHASE1_help = "LOAD Phase 1 — feeder alone fast to toolhead. DISTANCE=mm"
    def cmd_BUFFER_LOAD_PHASE1(self, gcmd):
        self._halt_requested = False    # ack any stale console HALT
        self._raise_if_locked_out(gcmd)
        self._check_phase_entry('LOAD_PHASE1', {
            STATE_IDLE, STATE_AUTO, STATE_RUNOUT, STATE_LOAD_PHASE_1,
        })
        distance = gcmd.get_float('DISTANCE', self.load_fast_distance, above=0.)
        speed    = gcmd.get_float('SPEED',    self.load_fast_speed,    above=0.)
        # Stop any inherited bang-bang / manual dauerfeed and drain
        # any in-flight chunk so residual motion doesn't extend Phase 1.
        self._continuous_feed = False
        self._wait_for_move_done(gcmd)
        self._set_state(STATE_LOAD_PHASE_1)
        self._enable_stepper()
        self._submit_move(+distance, speed)
        # Blocking: wait for move done, but pause-and-resume on HALL1 overflow
        # instead of aborting — _exit_overflow restores pending state so
        # streaming continues naturally. BUFFER_WAIT_IDLE would deadlock
        # because it also waits for state != busy-phase.
        try:
            self._wait_for_move_done_resume_on_overflow(gcmd)
        except Exception:
            # Release the phase state on error so it doesn't stay sticky.
            self._set_state(STATE_IDLE)
            raise
        self._set_state(STATE_IDLE)

    # P7-55b: cmd_BUFFER_LOAD_PHASE2 entfernt. Das parallele Feeder+
    # Extruder-Pattern wurde durch SYNC_TO_EXTRUDER abgeloest (P7-44 in
    # LOAD_FILAMENT Phase 3/3, P7-20 in UNLOAD-Tip-Forming). Der alte
    # Befehl war seit dem nicht mehr in lll.cfg / tests / Macros
    # aufgerufen, nur als Public-G-Code-Endpoint registriert. Externe
    # Custom-Macros, die `BUFFER_LOAD_PHASE2` direkt aufgerufen haben,
    # muessen auf `BUFFER_SYNC_TO_EXTRUDER` + `G1 E` + `BUFFER_UNSYNC`
    # umgestellt werden.

    cmd_BUFFER_LOAD_PHASE3_help = ("LOAD Phase 3 — feed until HALL2 or MAX_DISTANCE. "
                                    "Optional: STABLE_TIMEOUT (s, 0=instant), "
                                    "OVERFLOW_OK (0/1, treat stable HALL1 as success), "
                                    "CHUNK_DISTANCE (mm per tick chunk).")
    def cmd_BUFFER_LOAD_PHASE3(self, gcmd):
        self._halt_requested = False
        # Parameter ZUERST parsen — overflow_ok beeinflusst den Lockout-
        # Check (P7-8/9). Wenn der Standard _raise_if_locked_out vorher
        # liefe, wuerde HALL1-aktiv sofort raisen, bevor wir den
        # OVERFLOW_OK=1 Modus auswerten koennen.
        max_distance = gcmd.get_float('MAX_DISTANCE', self.load_buffer_max, above=0.)
        speed        = gcmd.get_float('SPEED',        self.feed_speed,      above=0.)
        # Stable-Exit-Optionen (P7-8): Sensoren muessen N Sekunden
        # KONTINUIERLICH aktiv sein bevor Phase 3 abbricht. Default 0
        # = altes Verhalten (Instant-Exit beim ersten HALL2-Trigger).
        # OVERFLOW_OK=1 → stable HALL1 ist auch ein legitimer Exit
        # (Buffer ueberfuellt → Filament ist da → Phase 2 fertig).
        # CHUNK_DISTANCE konfiguriert die Foerder-Chunkgroesse pro Tick
        # (Default 10mm — fuer LOAD-Wiederholung kann der Macro auf 50+
        # mm hochsetzen, weniger viele kleine Submits).
        stable_timeout = gcmd.get_float('STABLE_TIMEOUT', 0.0, minval=0.)
        overflow_ok    = bool(gcmd.get_int('OVERFLOW_OK', 0, minval=0, maxval=1))
        chunk_distance = gcmd.get_float('CHUNK_DISTANCE', 10.0, above=0.)
        # Lockout-Check: JAM ist immer absolut. OVERFLOW nur wenn nicht
        # OVERFLOW_OK gesetzt — sonst handhabt _load_phase3_tick den
        # HALL1-Stable-Exit selbst.
        if self._state == STATE_JAM or self._jam_active:
            raise self._cmd_error(
                "BufferFeeder: JAM active — aborting. "
                "Use BUFFER_CLEAR_JAM after inspection. (UNLOAD is allowed.)")
        if not overflow_ok:
            if (self._state == STATE_OVERFLOW
                    or self._is_hall1_active('phase3_entry')):
                raise self._cmd_error(
                    "BufferFeeder: HALL1 OVERFLOW active — aborting. "
                    "Clear overflow, then retry. (UNLOAD is allowed; "
                    "use OVERFLOW_OK=1 for stable-exit semantics.)")
        # Phase Entry: bei overflow_ok ist STATE_OVERFLOW ein legitimer
        # Vorgaenger-State (das aufrufende Macro hat das vorher
        # abgesichert via Status-Check).
        allowed_states = {STATE_IDLE, STATE_AUTO, STATE_RUNOUT,
                          STATE_LOAD_PHASE_3}
        if overflow_ok:
            allowed_states.add(STATE_OVERFLOW)
        self._check_phase_entry('LOAD_PHASE3', allowed_states)
        # Clean start: stop any inherited continuous feed and wait for
        # any in-flight manual move to finish before we begin chunk
        # streaming. Prevents residual motion from tacking onto Phase 3.
        # allow_overflow=overflow_ok: bei OVERFLOW_OK=1 darf der Wait
        # nicht am internen _raise_if_locked_out kippen — sonst rueckt
        # die Stable-Logic nie zur Geltung. JAM raised weiterhin (P7-12).
        self._continuous_feed = False
        self._wait_for_move_done(gcmd, allow_overflow=overflow_ok)
        self._load_phase3_distance = 0.0
        self._load_phase3_max_distance = max_distance
        self._load_phase3_speed = speed
        self._load_phase3_stable_timeout = stable_timeout
        self._load_phase3_overflow_ok = overflow_ok
        self._load_phase3_chunk_distance = chunk_distance
        self._load_phase3_hall_full_since = None
        self._load_phase3_hall_overflow_since = None
        self._load_phase3_hall_full_drop_since = None
        self._load_phase3_hall_overflow_drop_since = None
        # Wenn wir aus STATE_OVERFLOW heraus eintreten (overflow_ok=1),
        # die _overflow_interrupted_*-Felder clearen — sonst wuerde ein
        # spaeteres _exit_overflow versuchen, einen "interrupted" Move
        # zu resumen, der gar nicht mehr passt.
        if overflow_ok:
            self._overflow_interrupted_state = None
            self._overflow_resume_mm = 0.0
            self._overflow_resume_dir = 0
            self._overflow_resume_spd = 0.0
            self._overflow_interrupted_follow = False
        logging.info("buffer_feeder: P3 start threshold=%.1fs overflow_ok=%s "
                     "chunk=%.1f hall1=%s hall2=%s state=%s",
                     stable_timeout, overflow_ok, chunk_distance,
                     self.hall_overflow, self.hall_full, self._state)
        self._enable_stepper()
        self._set_state(STATE_LOAD_PHASE_3)
        self._start_continuous_motion(+1, speed, self.max_feed_time)
        # Block until the tick-driven state machine exits STATE_LOAD_PHASE_3.
        # P7-35 fault-overlay: in overlay mode HALL1 sets _fault_overflow
        # without state change, so the overlay flag is an additional exit
        # condition. _exit_overflow clears it; postcheck below raises if
        # HALL1 is still asserted.
        while (self._state == STATE_LOAD_PHASE_3
               and not (self.use_fault_overlay and self._fault_overflow)):
            self.reactor.pause(self.reactor.monotonic() + 0.1)
        # Postcheck: JAM bleibt absolut. Bei overflow_ok haben wir den
        # HALL1-Stable-Exit selbst gemacht — sonst alte Lockout-Logik.
        if self._state == STATE_JAM or self._jam_active:
            raise self._cmd_error(
                "BufferFeeder: JAM active — aborting. "
                "Use BUFFER_CLEAR_JAM after inspection. (UNLOAD is allowed.)")
        if not overflow_ok:
            self._raise_if_locked_out(gcmd)

    # P7-23: cmd_BUFFER_UNLOAD_PHASE1 und cmd_BUFFER_UNLOAD_PHASE2 entfernt.
    # P7-20 hat das UNLOAD_FILAMENT-Macro auf SYNC_TO_EXTRUDER umgestellt —
    # Tip-Forming und parallele sync-distance laufen jetzt im Macro selbst
    # via G1 E waehrend BUFFER_SYNC_TO_EXTRUDER aktiv ist.

    cmd_BUFFER_UNLOAD_FILAMENT_help = "UNLOAD_FILAMENT als Python-Workflow mit garantiertem Cleanup"
    def cmd_BUFFER_UNLOAD_FILAMENT(self, gcmd):
        tip_cycles = gcmd.get_int('TIP_CYCLES', 4, minval=0)
        tip_push = gcmd.get_float('TIP_PUSH', 8.0, above=0.)
        tip_pull = gcmd.get_float('TIP_PULL', 10.0, above=0.)
        tip_speed = gcmd.get_float('TIP_SPEED', 20.0, above=0.)
        tip_final_retract = gcmd.get_float('TIP_FINAL_RETRACT', 25.0, above=0.)
        tip_final_speed = gcmd.get_float('TIP_FINAL_SPEED', 50.0, above=0.)
        sync_dist = gcmd.get_float('SYNC_DIST', self.unload_sync_distance, above=0.)
        fast_spd = gcmd.get_float('FAST_SPD', self.unload_fast_speed, above=0.)
        max_distance = gcmd.get_float('MAX_DISTANCE', self.unload_fast_max, above=0.)
        heat_to = gcmd.get_float('AUTO_HEAT_TARGET', 250.0, above=0.)
        extruder_name = gcmd.get('EXTRUDER', 'extruder')

        temp = self._hotend_temp()
        if temp < self.min_temp:
            self._gcode_run_script_checked(
                "M118 Hotend zu kalt (%d/%d C) - heize automatisch auf %d C\n"
                "M109 S%d"
                % (int(temp), int(self.min_temp), int(heat_to), int(heat_to)),
                from_command=True)

        state_saved = False
        try:
            self._gcode_run_script_checked(
                "SAVE_GCODE_STATE NAME=buffer_feeder_op",
                from_command=True)
            self._macro_state_saved = True
            state_saved = True
            self._gcode_run_script_checked("M83", from_command=True)

            try:
                # P7-32: kein sync_active-Flag mehr. _unsync_if_synced
                # ist idempotent (early-return wenn _stepper_synced_to
                # is None). Damit greift der Cleanup auch wenn die
                # Exception INNERHALB des sync-Aufrufs raised — vorher
                # haette sync_active=False den finally-Cleanup
                # uebersprungen, obwohl das Sync-Command schon teilweise
                # mutiert hatte.
                self._gcode_run_script_checked(
                    "BUFFER_SYNC_TO_EXTRUDER BUFFER=%s EXTRUDER=%s"
                    % (self.name, extruder_name),
                    from_command=True)

                tip_speed_f = int(tip_speed * 60)
                fast_spd_f = int(fast_spd * 60)
                moves = []
                for _ in range(tip_cycles):
                    moves.append("G1 E%g F%d" % (tip_push, tip_speed_f))
                    moves.append("G1 E-%g F%d" % (tip_pull, tip_speed_f))
                moves.append("G1 E-%g F%d" % (tip_final_retract,
                                             int(tip_final_speed * 60)))
                moves.append("G1 E-%g F%d" % (sync_dist, fast_spd_f))
                moves.append("M400")
                self._gcode_run_script_checked("\n".join(moves), from_command=True)
            finally:
                self._unsync_if_synced()

            self._gcode_run_script_checked(
                "BUFFER_UNLOAD_PHASE3 BUFFER=%s MAX_DISTANCE=%g SPEED=%g"
                % (self.name, max_distance, self.unload_phase3_speed),
                from_command=True)
        finally:
            if state_saved and self._macro_state_saved:
                self._gcode_run_script_checked(
                    "RESTORE_GCODE_STATE NAME=buffer_feeder_op MOVE=0",
                    from_command=True)
                self._macro_state_saved = False

        self._respond("UNLOAD abgeschlossen (Python workflow)")

    cmd_BUFFER_UNLOAD_PHASE3_help = "UNLOAD Phase 3 — chunked retract until entrance free"
    def cmd_BUFFER_UNLOAD_PHASE3(self, gcmd):
        self._halt_requested = False
        self._raise_if_locked_out(gcmd, direction=-1)
        # OVERFLOW/JAM erlaubt fuer Retract-Recovery.
        self._check_phase_entry('UNLOAD_PHASE3', {
            STATE_IDLE, STATE_AUTO, STATE_RUNOUT, STATE_UNLOAD_PHASE_3,
            STATE_OVERFLOW, STATE_JAM,
        })
        max_distance = gcmd.get_float('MAX_DISTANCE', self.unload_fast_max, above=0.)
        speed        = gcmd.get_float('SPEED',        self.unload_phase3_speed, above=0.)
        nominal_chunk = 50.0
        # Clean start: cancel any inherited continuous feed and drain
        # any in-flight move so residual motion doesn't join the retract.
        self._continuous_feed = False
        self._wait_for_move_done(gcmd, direction=-1)
        self._set_state(STATE_UNLOAD_PHASE_3)
        self._enable_stepper()
        retracted = 0.0
        overshoot = False
        while retracted < max_distance:
            # Abort immediately on HALT. OVERFLOW/JAM duerfen weiterlaufen
            # (UNLOAD ist Recovery, direction=-1).
            self._raise_if_locked_out(gcmd, direction=-1)
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
            # exit the loop. UNLOAD ist Retract → OVERFLOW/JAM erlaubt.
            self._wait_for_move_done(gcmd, direction=-1)
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
        # P7-22: UNLOAD ist semantisch der JAM-/OVERFLOW-Recovery-Pfad —
        # bei erfolgreichem Exit auch sticky Lockout-Flags clearen.
        # Sonst raised der naechste LOAD_FILAMENT mit "JAM active" weil
        # _jam_active=True von einem frueheren LOAD_TIMEOUT haengt.
        # _set_state(STATE_IDLE) oben cleart nur _state, nicht die
        # Begleit-Flags. (P7-31 review: nutzt jetzt _clear_recovery_flags
        # konsistent mit den anderen vier Recovery-Pfaden.)
        self._clear_recovery_flags()

    def _setup_trapq(self, config):
        return self.sync.setup_trapq(config)

    def _anchor_step(self):
        return self.sync.anchor_step()

    def _sync_to_extruder(self, extruder_name):
        return self.sync.sync_to_extruder(extruder_name)

    cmd_BUFFER_SYNC_TO_EXTRUDER_help = ("Sync buffer-feeder stepper to the named "
                                         "extruder's trapq for parallel motion. "
                                         "Optional: EXTRUDER=<name> (default: extruder)")
    def cmd_BUFFER_SYNC_TO_EXTRUDER(self, gcmd):
        # P7-20: bindet den Buffer-Feeder-Stepper an den Trapq eines
        # anderen Extruder-Steppers, sodass jeder G1 E Move den Buffer-
        # Stepper synchron mitzieht. Pattern aus
        # klippy/kinematics/extruder.py:ExtruderStepper.sync_to_extruder
        # (extruder.py:968-995).
        # Anwendungsfall: UNLOAD-Tip-Forming. Der Hauptextruder pusht/pullt
        # Filament durchs Hotend, der Buffer-Stepper folgt mit derselben
        # Geschwindigkeit — Filament-Strang fliesst durch den Buffer ohne
        # Stau (HALL2/HALL1) oder Leerlauf (HALL3). Auch loest die
        # Stepcompress-Cursor-Etablierung: solange der gemeinsame Trapq
        # aktiv ist, ist der Buffer-Stepper-last_step_clock immer frisch.
        extruder_name = gcmd.get('EXTRUDER', 'extruder')
        self._sync_to_extruder(extruder_name)

    def _unsync_if_synced(self):
        """Idempotent unsync helper. Cleanup-Pfade (BUFFER_HALT,
        BUFFER_AUTO_OFF, STOP_BUFFER_FILL) rufen das auf damit ein
        zwischen SYNC_TO_EXTRUDER und UNSYNC abgebrochenes Macro nicht
        den Stepper am Extruder-Trapq zurueck laesst (P7-24).
        """
        return self.sync.unsync_if_synced()

    cmd_BUFFER_UNSYNC_help = "Unsync buffer-feeder stepper back to its own trapq"
    def cmd_BUFFER_UNSYNC(self, gcmd):
        # P7-20: kehrt SYNC_TO_EXTRUDER um — Stepper bekommt seinen
        # eigenen Trapq zurueck. Buffer-eigene Move-Logik (cmd_BUFFER_FEED,
        # BUFFER_UNLOAD_PHASE3 etc.) laeuft danach sauber weiter.
        if self._unsync_if_synced():
            self._respond("Buffer-Feeder unsynced — own trapq active")
        else:
            self._respond("Buffer-Feeder is not synced — no-op")

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
        # P7-24: Stepper auch vom Extruder-Trapq abkoppeln falls
        # ein Macro-Abbruch ihn dort haengen lies.
        if self._unsync_if_synced():
            self._respond("STOP_BUFFER_FILL — also unsynced from extruder")
        self._continuous_feed = False
        self._halt_motion()
        self._pending_remaining_mm = 0.0
        self._initial_grip_end_time = None
        self._grip_follow_active = False
        self._load_phase3_distance = 0.0
        self._measure_load_active = False
        self._measure_feeding = False
        self._clear_recovery_flags()
        self._runout_follow_active = False
        self._runout_filament_ref = None
        self._cooldown_deadline = None
        self._bang_bang_suspended = False
        self._auto_off_by_user = True
        self._runout_recovery_pending = False
        self._halt_requested = True
        # P7-46: clear post-LOAD grace on operator stop.
        self._post_load_overflow_grace = False
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
            "overlay flags     = overflow=%s runout=%s jam=%s (use=%s)" % (
                self._fault_overflow, self._fault_runout,
                self._fault_jam, self.use_fault_overlay),
            "runout_follow      = %s ref=%s" % (self._runout_follow_active,
                                                self._runout_filament_ref),
            "runout_recov_pending= %s (RESUME will grip+fill if armed)" % self._runout_recovery_pending,
            "macro_state_saved  = %s (buffer_feeder_op slot consumable)" % self._macro_state_saved,
            "synced_to_extruder = %s" % self._stepper_synced_to,
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
        self._clear_recovery_flags()
        self._halt_requested = False
        # Best-effort restore of any LOAD/UNLOAD gcode-state that was
        # saved before the jam fired. Otherwise the operator would
        # end up back in AUTO with the E-mode still flipped to M83
        # from the failed macro.
        self._try_restore_gcode_state(from_command=True)
        # P7-24: vor dem Auto-Sprung dieselben Guards pruefen, die
        # BUFFER_AUTO_ON anwendet (HALL1, Pause-Suspend, Busy-Phase).
        # Ohne diese Angleichung konnte CLEAR_JAM AUTO aktivieren in
        # Situationen, wo AUTO_ON verweigert (z.B. Print pausiert).
        # Bei verweigerten Voraussetzungen bleibt der Buffer in IDLE —
        # der Operator/RESUME engagiert dann selbst AUTO.
        target = STATE_IDLE
        if self.entrance_detected:
            block_reason = self._check_auto_ready(allow_jam=True)
            if block_reason is None and not self._auto_off_by_user:
                target = STATE_AUTO
            elif block_reason is not None:
                self._respond("JAM cleared — staying IDLE: " + block_reason)
        self._set_state(target)
        self._respond("JAM cleared — state=%s" % self._state)

    # -----------------------------------------------------------------------
    # Utilities
    # -----------------------------------------------------------------------

    def _cmd_error(self, msg):
        gc = self.printer.lookup_object('gcode')
        return gc.error(msg)

    def _raise_if_locked_out(self, gcmd=None, direction=+1):
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
        # Forward-Operationen (LOAD/feed) werden bei OVERFLOW/JAM
        # geblockt. UNLOAD ist Retract — die einzige sinnvolle Recovery
        # bei Overflow oder Jam. Daher direction=-1 fuer Retract-Pfade,
        # die Lockout durchbrechen duerfen. HALT bleibt absolut.
        if direction > 0:
            # hall_overflow direkt pruefen, nicht nur state — catched
            # die Race, wenn AUTO_OFF / STOP_BUFFER_FILL state schon nach
            # IDLE gesetzt hat, der naechste main_tick aber erst noch
            # OVERFLOW reasserten wird.
            if self._state == STATE_OVERFLOW or self.hall_overflow:
                raise self._cmd_error(
                    "BufferFeeder: HALL1 OVERFLOW active — aborting. "
                    "Clear overflow, then retry. (UNLOAD is allowed.)")
            if self._state == STATE_JAM or self._jam_active:
                raise self._cmd_error(
                    "BufferFeeder: JAM active — aborting. "
                    "Use BUFFER_CLEAR_JAM after inspection. (UNLOAD is allowed.)")

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
            'fault_overflow':           self._fault_overflow,
            'fault_runout':             self._fault_runout,
            'fault_jam':                self._fault_jam,
            'post_load_overflow_grace': self._post_load_overflow_grace,
            'bang_bang_suspended':      self._bang_bang_suspended,
            'halt_requested':           self._halt_requested,
            'runout_follow_active':     self._runout_follow_active,
            'runout_recovery_pending':  self._runout_recovery_pending,
            'measure_load_active':      self._measure_load_active,
            'measure_load_distance_mm': self._measure_load_distance,
            'macro_state_saved':        self._macro_state_saved,
            'synced_to_extruder':       self._stepper_synced_to,
            # Config values (exposed so LOAD/UNLOAD macros don't hardcode)
            'feed_speed':               self.feed_speed,
            'manual_speed':             self.manual_speed,
            'burst_speed':              self.burst_speed,
            'load_fast_speed':          self.load_fast_speed,
            'load_slow_speed':          self.load_slow_speed,
            'unload_fast_speed':        self.unload_fast_speed,
            'unload_phase3_speed':      self.unload_phase3_speed,
            'load_fast_distance':       self.load_fast_distance,
            'load_slow_distance':       self.load_slow_distance,
            'load_buffer_max':          self.load_buffer_max,
            'unload_sync_distance':     self.unload_sync_distance,
            'unload_fast_max':          self.unload_fast_max,
            'min_temp':                 self.min_temp,
            'use_python_unload':        self.use_python_unload,
            'use_fault_overlay':        self.use_fault_overlay,
            'accel':                    self.accel,
        }


# ---------------------------------------------------------------------------
# Config hook
# ---------------------------------------------------------------------------

def load_config_prefix(config):
    return BufferFeeder(config)
