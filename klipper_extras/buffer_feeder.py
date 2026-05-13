# buffer_feeder.py — Klipper extension for the Mellow LLL Plus Filament Buffer
#
# Architecture: Variante 3 (Python-Ansatz).
#
# Owns a single extruder-stepper-compatible stepper via its own trapq,
# independent of the main toolhead motion queue. Sensor-driven bang-bang
# control (HALL-based hysteresis) + explicit GCode commands for manual,
# LOAD, UNLOAD, and calibration flows.
#
# Time-base for normal feed submits is toolhead.get_last_move_time() +
# lead_time (anchored against the MCU step-gen cursor — same safeguard
# Klipper's manual_stepper uses for first-step-after-idle). The
# flush-callback bang-bang path (use_flush_callback_bang_bang=1) uses
# step_gen_time + lead_time directly from motion_queuing's flush
# notification.
# flush_step_generation() is called explicitly in three places:
#   - sync_to_extruder / unsync_if_synced (trapq-binding swaps must
#     drain pending generation before the swap),
#   - REPRIME path in _submit_single_trapezoid when the feeder has been
#     idle longer than CLOCK_DIFF_MAX (~17s) so the stepcompress cursor
#     wouldn't overflow on the next move.
# It is NOT called per-move during normal feed streaming.
#
# See docs/superpowers/specs/2026-04-23-python-ansatz-design.md for the
# full design rationale and feature mapping.

import collections
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
                # C-cont T5: In STATE_AUTO defer immediate _enter_overflow
                # to _main_tick (which checks for hall1_persist_timeout).
                # In other states (LOAD, MANUAL, UNLOAD) keep the immediate
                # trigger — those paths have their own safety semantics and
                # need synchronous overflow-handling.
                if owner._state == STATE_AUTO:
                    owner._mark_hall1_active()
                else:
                    owner._enter_overflow()
            else:
                # P7-46 (Issue #16): clear post-LOAD grace on HALL1-fall.
                # Buffer-Arm has dropped, normal sensor regime resumes.
                owner._post_load_overflow_grace = False
                owner._mark_hall1_cleared()
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
        # P7-56f: heal sticky suspended-flag from PAUSE → CANCEL/ERROR
        # path before we evaluate the auto-grip guards. Recompute
        # will_auto_grip after the potential clear so this insert
        # actually grips instead of falling into the suppressed branch.
        owner._clear_stale_suspend_if_print_inactive(eventtime)
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
            # Defer PAUSE via 1ms timer so we don't block this sensor
            # callback for the entire macro (tool-park etc.). Direct
            # run_script() would freeze _main_tick + bang-bang for the
            # full PAUSE duration (P7-56b).
            owner._schedule_gcode_script("PAUSE")
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

    def _submit_anchor_move(self, *, forced_t0=None):
        """Submit a 0.05mm anchor-step in the safe direction. Returns
        the direction sign so the caller can format its respond message
        (boot anchor vs. pre-sync REPRIME use different wording but the
        underlying motion + direction-policy is identical).

        P7-78v2 (Codex-Verify Finding zu P7-78 v1): keyword-only
        `forced_t0` routet den Submit durch den forced_t0!=None
        Branch in _submit_single_trapezoid (Z.3203 ff.). Der ist
        NICHT vom P7-77 B SKIP-statt-Clamp Guard (else-Branch
        Z.3275) betroffen. Caller im Watchdog-Print-Block-Override-
        Pfad (_main_tick P7-78) MUESSEN `forced_t0=mcu_now +
        lead_time` uebergeben, damit der Anchor auch bei aktiv
        gefuellter Toolhead-Queue (toolhead.get_last_move_time()
        far-future, typisch waehrend Print) tatsaechlich gesubmittet
        wird statt geskippt zu werden. Default (None) erhaelt das
        bisherige Verhalten fuer alle anderen Caller (boot anchor,
        pre-sync REPRIME, P7-77 C Nicht-Print-Watchdog)."""
        owner = self.owner
        owner._enable_stepper()
        anchor_dir = -1.0 if owner.hall_overflow else 1.0
        owner._submit_move(anchor_dir * 0.05, 10.0, forced_t0=forced_t0)
        owner._wait_for_move_done(direction=int(anchor_dir))
        return anchor_dir

    def anchor_step(self):
        anchor_dir = self._submit_anchor_move()
        self.owner._respond("Stepcompress anchor primed (boot %s 0.05mm)"
                            % ("retract" if anchor_dir < 0 else "feed"))

    def sync_to_extruder(self, extruder_name):
        owner = self.owner
        extruder = owner.printer.lookup_object(extruder_name)
        if not hasattr(extruder, 'get_trapq'):
            raise owner._cmd_error(
                "Object '%s' is not an extruder (no get_trapq method)"
                % extruder_name)
        # P7-46 (Issue #16): Gap-Reprime BEFORE the trapq-swap. The
        # own-trapq path in _submit_single_trapezoid has a
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
            anchor_dir = self._submit_anchor_move()
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
            # (klippy/kinematics/extruder.py: ExtruderStepper.sync_to_
            # extruder) und _read_extruder_position() in dieser Datei.
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
        self._hall3_drop_since = None

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
        self._hall3_drop_since = None

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
            owner._maybe_auto_load()
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
            # P7-76 B: Defensiver Watchdog-Gate-Reset beim
            # OVERFLOW→IDLE→AUTO-Recovery. Falls _continuous_feed nach
            # OVERFLOW-Cycling haengend geblieben ist (rapid HALL1-
            # Flicker, race in _enter_overflow vs. _on_mcu_flush oder
            # _bang_bang_tick), wuerde der _main_tick-Watchdog 56s+
            # nicht feuern, obwohl AUTO mit Quiescent-Phasen laeuft
            # (Eifel-Joe Hardware Crash #3, klippy.log Z 30556-30669,
            # DWELL-SA3 Diagnose). _enter_overflow + _halt_motion +
            # _set_state(IDLE) sollten bereits alle drei Flags clearen
            # — dieser Reset ist Defense-in-Depth gegen unbekannte
            # Race-Pfade. Schadet nichts wenn die Flags bereits clean
            # sind (no-op). hall_empty / hall_full / _stepper_synced_to
            # bleiben unangetastet — die sind sensor- bzw. architektur-
            # getrieben und kein "stuck flag"-Risiko.
            if owner._continuous_feed:
                logging.warning(
                    "buffer_feeder: stuck _continuous_feed cleared "
                    "at OVERFLOW→AUTO transition (P7-76 B)")
                owner._continuous_feed = False
                owner._continuous_feed_direction = 0
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
            # P7-56f: heal stale suspend before rejecting. RESUME the
            # print path doesn't fire idle_timeout:ready a second time
            # if a PAUSE was cancelled instead.
            owner._clear_stale_suspend_if_print_inactive(
                owner.reactor.monotonic())
        if owner._bang_bang_suspended:
            return ("print is paused (bang-bang suspended). RESUME the "
                    "print — bang-bang re-engages automatically. If the "
                    "print is already finished, use BUFFER_AUTO_OFF first.")
        return None


# ---------------------------------------------------------------------------
# ExtruderVelocityTracker
# ---------------------------------------------------------------------------

class ExtruderVelocityTracker:
    """Read-only passive tracker for extruder velocity.

    Uses extruder.get_status(eventtime)['position'] — no flush_step_
    generation, no SYNC, no lockstep with toolhead pipeline. Pure
    observer pattern. Output drives C-cont SpeedModulator and C-pred
    safety-factor override.
    """

    def __init__(self, owner, printer, *,
                 sample_interval=0.025,
                 window_size=0.3,
                 filament_diameter=1.75):
        self.owner = owner
        self.printer = printer
        self.sample_interval = sample_interval
        self.window_size = window_size
        self._cross_section = math.pi * (filament_diameter / 2.0) ** 2
        # round() avoids float-precision truncation (e.g. 0.3 / 0.025
        # = 11.999... -> int() would yield 11 instead of expected 12).
        self._max_samples = max(2, int(round(window_size / sample_interval)))
        self._samples = collections.deque(maxlen=self._max_samples)
        self._extruder = None
        self._last_sample_time = None

    def _get_extruder(self):
        if self._extruder is not None:
            return self._extruder
        self._extruder = self.printer.lookup_object('extruder', None)
        return self._extruder

    def tick(self, eventtime):
        """Call from _main_tick (50Hz reactor). Throttles internally
        to sample_interval (default 25ms / 40Hz). Uses 1us tolerance
        to absorb float-accumulation drift in periodic callers."""
        if (self._last_sample_time is not None
                and (eventtime - self._last_sample_time
                     < self.sample_interval - 1e-6)):
            return
        ext = self._get_extruder()
        if ext is None:
            return
        try:
            status = ext.get_status(eventtime)
        except Exception:
            return
        position = status.get('position', 0.0) if isinstance(
            status, dict) else 0.0
        self._samples.append((eventtime, position))
        self._last_sample_time = eventtime

    def get_velocity(self):
        """Returns linear filament velocity (mm/s, non-negative).
        0.0 if fewer than 2 samples or negative dp."""
        if len(self._samples) < 2:
            return 0.0
        (t0, p0), (t1, p1) = self._samples[0], self._samples[-1]
        dt = t1 - t0
        if dt < 1e-6:
            return 0.0
        return max(0.0, (p1 - p0) / dt)

    def get_volumetric_flow(self):
        """Returns volumetric flow (mm^3/s)."""
        return self.get_velocity() * self._cross_section

    def is_ready(self):
        """True after sliding window has filled (window_size seconds
        of samples accumulated)."""
        return len(self._samples) == self._max_samples

    def reset(self):
        """Clear all samples. Call on klippy:disconnect / BUFFER_RESET."""
        self._samples.clear()
        self._last_sample_time = None


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
    #   P7-?? offen: LOAD_PHASE_1 + LOAD_PHASE_3 zu LOAD-Substate kollabieren
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
        self.unload_sync_distance  = config.getfloat('unload_sync_distance',  400., above=0.)
        self.unload_fast_max       = config.getfloat('unload_fast_max',      5000., above=0.)

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
        # Flush-Callback-Pfad benutzt einen eigenen, kleineren Chunk damit
        # der Feeder den HALL1-Overflow-Bereich nicht ueberschiesst.
        # Default 15.0mm = HALL3->HALL2-Pufferweg am Mellow LLL Plus.
        self.flush_callback_chunk_mm = config.getfloat('flush_callback_chunk_mm', 15.0, above=0.)
        # P7-66b: Maximum size of a SINGLE submitted trapezoid in the
        # AUTO bang-bang streaming path. When flush_callback_chunk_mm
        # exceeds this cap, the chunk is split into sub-chunks via
        # _pending_remaining_mm so HALL2/HALL1 can abort between
        # sub-chunks (move-splitting interrupt). Default 9.0 mm gives
        # ~128 ms sub-chunk duration at 70 mm/s — bounded overshoot.
        # Hardware-test 2026-05-12: 45 mm without a cap → grinding.
        # Cap <= max_move_chunk_mm.
        self.interrupt_chunk_mm = config.getfloat(
            'interrupt_chunk_mm', 9.0, above=0.)
        if self.interrupt_chunk_mm > self.max_move_chunk_mm:
            self.interrupt_chunk_mm = self.max_move_chunk_mm
        # C-cont T3: max_feed_speed = Cap fuer SpeedModulator-Output.
        # Bei extruder_velocity * factor sollte das Stepper-Hardware-Limit
        # nicht ueberschritten werden. Default 100 mm/s (deutlich ueber
        # Default feed_speed=30, lll.cfg-Wert=70).
        self.max_feed_speed = config.getfloat(
            'max_feed_speed', 100.0, above=0.)
        if self.max_feed_speed < self.feed_speed:
            raise config.error(
                "max_feed_speed (%.1f) must be >= feed_speed (%.1f)"
                % (self.max_feed_speed, self.feed_speed))
        # C-cont T3: HALL1-Persist-Timeout. HALL1 (Ueberlast) im STATE_AUTO
        # loest erst nach diesem Timeout den echten OVERFLOW-State aus.
        # In der Zwischenzeit setzt SpeedModulator nur target_speed=0.
        self.hall1_persist_timeout = config.getfloat(
            'hall1_persist_timeout', 2.0, above=0.)
        # C-cont T3: Diagnostik-Logs (Buffer-Metrics alle 1s, Per-Submit-
        # DEBUG). Default off fuer Production.
        self.buffer_debug_metrics = config.getboolean(
            'buffer_debug_metrics', False)
        # P7-70 (Issue #12): Interval after which an IDLE stepper gets a
        # micro-anchor move to refresh stepcompress's last_step_clock.
        # Background: STATE_IDLE has no periodic move activity. After
        # UNLOAD → IDLE the stepcompress cursor freezes. Once Klipper's
        # background flush_handler fires more than CLOCK_DIFF_MAX (~17s
        # @48MHz) after _last_move_end_time, compress_bisect_add hits a
        # degenerate sequence ("stepcompress o=X i=0 c=N a=0: Invalid
        # sequence") and the MCU shuts down. This watchdog matches the
        # well-tested boot-anchor / SYNC-gap-anchor pattern (a 0.05 mm
        # move in the safe direction is enough to keep last_step_clock
        # current). Set above 0 — too small wastes motor wear, too large
        # risks crossing the 17 s threshold. Default 10 s leaves a
        # comfortable safety margin.
        self.idle_anchor_gap = config.getfloat(
            'idle_anchor_gap', 10.0, above=0.)

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
        # uses. Flag gates the FIRST boot-prime; it is also reset by
        # _submit_single_trapezoid's REPRIME path whenever the feeder
        # has been idle longer than REPRIME_GAP (~5s), so a fresh
        # flush+set_position runs at every long-idle wakeup.
        # P7-47 added a separate but related fix in SyncCoordinator.
        # sync_to_extruder: when binding the stepper to the extruder
        # trapq, we now seed the position with extruder.last_position
        # (matching klippy/kinematics/extruder.py: ExtruderStepper.
        # sync_to_extruder) instead of (0,0,0)
        # — otherwise itersolve sees a 180mm phase mismatch on the
        # first synced step and crashes with i=0/c=N invalid sequence.
        # The flag here is the first-move boot prime; the cursor-fresh
        # logic for ongoing operation lives in
        # _submit_single_trapezoid's REPRIME_GAP path.
        self._stepcompress_primed = False
        # P7-20: sync-to-extruder state lebt im SyncCoordinator
        # (self.sync._stepper_synced_to). BufferFeeder exponiert es als
        # Bridge-Property weiter unten, damit interner self._stepper_-
        # synced_to Zugriff weiterhin funktioniert.
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
        # P7-66b: per-sub-chunk submit cap propagated to _tick_pending_-
        # chunk so the HALL-interruptible streaming uses the same small
        # trapezoid size as the first submit. Default None = legacy
        # max_move_chunk_mm. AUTO+streaming sets this to
        # interrupt_chunk_mm; LOAD/UNLOAD/MANUAL paths leave it None.
        self._pending_submit_chunk_cap = None
        self._continuous_feed = False       # True = keep submitting moves while active
        self._continuous_feed_direction = 0 # +1 or -1
        self._continuous_feed_speed = 0.0
        self._auto_between_since = None     # sustained HALL3-leave in AUTO
        self._pending_disable = False       # deferred stepper disable (while move in flight)
        # P7-70 (Issue #12): timestamp of the last idle-watchdog anchor.
        # Gates the watchdog so it fires at most every idle_anchor_gap
        # seconds (not on every reactor tick once gap > threshold). Used
        # by _main_tick. 0.0 means "never fired yet" — first valid trip
        # happens once mcu_now - _last_move_end_time > idle_anchor_gap.
        self._last_idle_anchor_time = 0.0
        # P7-78 (Issue #29): Timestamp wann _on_mcu_flush zuletzt
        # aufgerufen wurde (MCU print-time). Genutzt vom P7-77 A
        # Print-Block-Override: bei print_stats.state == 'printing'
        # wird der Watchdog hart geblockt, aber wenn _on_mcu_flush
        # messbar still ist (HALL2-Hysterese-Zwischenzone in
        # STATE_AUTO), muss der Watchdog feuern damit stepcompress.
        # last_step_clock nicht altert und der erste Bang-Bang-Submit
        # nach Stille nicht c=7 Invalid sequence wirft. 0.0 = "noch nie
        # ein Flush gesehen" (Boot-Schutz, kein Override moeglich).
        self._last_mcu_flush_time = 0.0
        # C-cont T5: HALL1-Persist-Tracking. In STATE_AUTO loest HALL1-Edge
        # nicht mehr direkt _enter_overflow aus, sondern setzt nur den
        # Timestamp. _main_tick prueft Persist > hall1_persist_timeout und
        # eskaliert dann zu echtem OVERFLOW (Hardware-Safety). None = HALL1
        # inactive, float = reactor.monotonic() zum Edge-Time.
        self._hall1_active_since = None
        # C-cont T2: ExtruderVelocityTracker fuer SpeedModulator.
        # Read-only passiver Observer ueber extruder.get_status. Kein
        # flush_step_generation, kein SYNC -> kein Druckkopf-Pause-
        # Risiko. Tick-Driver ist _main_tick (50Hz), Tracker drosselt
        # intern auf sample_interval=0.025s (40Hz).
        self.velocity_tracker = ExtruderVelocityTracker(
            owner=self, printer=self.printer,
            sample_interval=0.025,
            window_size=0.3,
            filament_diameter=config.getfloat(
                'filament_diameter', 1.75, above=0.))
        # P7-54: After OVERFLOW → IDLE → AUTO the stepcompress cursor
        # is out of sync. _main_tick handles the safe resync (forced_t0=None
        # path → flush_step_generation allowed). _on_mcu_flush skips while
        # this flag is set to prevent submitting on an unsynced cursor.
        self._needs_overflow_prime = False
        self._feed_deadline_time = None     # max feed deadline (reactor time)
        self._measure_load_active = False
        self._measure_load_distance = 0.0
        # Explicit toggle tracking so the first button click after
        # MEASURE_LOAD_START always starts the feed, regardless of
        # whatever _continuous_feed was doing in AUTO before.
        self._measure_feeding = False
        self._print_running = False
        # _jam_active lebt im FaultManager (FaultManager.__init__
        # initialisiert es); Bridge-Property exponiert self._jam_active.
        # Fault-overlay migration (P7-30 roadmap, partially completed):
        # use_fault_overlay=1 enables the LOAD_PHASE_3 overflow overlay
        # (P7-35 — _enter_overflow leaves state=LOAD_PHASE_3 instead of
        # flipping to STATE_OVERFLOW). RUNOUT and JAM paths remain on
        # the legacy state-flip pattern; their overlay-fields below
        # stay as scaffolding for a future migration step.
        self._fault_overflow = False
        self._fault_runout = False
        self._fault_jam = False
        # P7-46 (Issue #16): Post-LOAD HALL1-bounce-suppression. Set
        # when LOAD_PHASE_3 with overflow_ok=1 exits via stable HALL1
        # (treating as full). Buffer is legitimately overfilled — the
        # main_tick path would otherwise re-trigger _enter_overflow on
        # the next cycle and bounce state IDLE/AUTO → OVERFLOW. Cleared
        # when HALL1 actually falls (sensor_callback) or via operator
        # cleanup (BUFFER_AUTO_OFF, STOP_BUFFER_FILL, BUFFER_HALT).
        self._post_load_overflow_grace = False

        # ----- Jam detection state -----
        # _hall2_start_time / _hall2_start_extruder_pos / _hall3_start_-
        # time leben im FaultManager (FaultManager.__init__ setzt sie
        # auf None/0.0). BufferFeeder hat passende Bridge-Properties.

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
        # klippy/extras/motion_queuing.py register_flush_callback
        # fires synchronously inside the MCU flush cycle with
        # signature (flush_time, step_gen_time). Anchoring submits at
        # step_gen_time + lead_time bypasses the toolhead.get_last_
        # move_time anchor that breaks reactive bang-bang during
        # mid-toolhead-move (P7-50 lesson learned the hard way).
        if hasattr(self.motion_queuing, 'register_flush_callback'):
            self.motion_queuing.register_flush_callback(
                self._on_mcu_flush, can_add_trapq=True)

        self._register_gcode_commands()

        logging.info("buffer_feeder '%s' initialised", self.name)

    def _register_gcode_commands(self):
        """Register all gcode commands as mux-commands on key 'BUFFER'.

        P7-40: register_command → register_mux_command. Mux-Key 'BUFFER'
        folgt der Klipper-Mainline-Konvention fuer load_config_prefix-
        Module mit Single-Type-Identifier. User-Aufruf:
          BUFFER_AUTO_ON BUFFER=mellow
        Mux-Value = self.name (z.B. "mellow" aus [buffer_feeder mellow]).
        Mehrere Instanzen koennen denselben Command-Namen registrieren,
        Dispatcher waehlt via BUFFER=...

        P7-62: Beim Single-Instance-Setup (was die uebliche Konfiguration
        ist) registriert _handle_ready zusaetzlich den BUFFER=None-
        default fuer JEDEN command, sodass der User die Befehle ohne
        BUFFER=mellow aufrufen kann (z.B. einfach "BUFFER_FEED").
        Multi-Instance-Setups behalten den Pflicht-Mux-Key.
        """
        gcode = self.printer.lookup_object('gcode')
        # (gcode_name, handler, help_text). help_text=None zieht den
        # *_help-Class-Attr; sonst inline-String wenn der Befehl keinen
        # _help hat.
        commands = [
            ('BUFFER_FEED',                 self.cmd_BUFFER_FEED,                 None),
            ('BUFFER_RETRACT',              self.cmd_BUFFER_RETRACT,              None),
            ('BUFFER_HALT',                 self.cmd_BUFFER_HALT,                 None),
            ('BUFFER_AUTO_ON',              self.cmd_BUFFER_AUTO_ON,              None),
            ('BUFFER_AUTO_ON_IF_READY',     self.cmd_BUFFER_AUTO_ON_IF_READY,     None),
            ('BUFFER_AUTO_OFF',             self.cmd_BUFFER_AUTO_OFF,             None),
            ('BUFFER_WAIT_IDLE',            self.cmd_BUFFER_WAIT_IDLE,            None),
            ('BUFFER_LOAD_PHASE1',          self.cmd_BUFFER_LOAD_PHASE1,          None),
            # P7-55b: BUFFER_LOAD_PHASE2 entfernt (durch SYNC_TO_EXTRUDER ersetzt)
            ('BUFFER_LOAD_PHASE3',          self.cmd_BUFFER_LOAD_PHASE3,          None),
            ('BUFFER_UNLOAD_FILAMENT',      self.cmd_BUFFER_UNLOAD_FILAMENT,      None),
            ('BUFFER_UNLOAD_PHASE3',        self.cmd_BUFFER_UNLOAD_PHASE3,        None),
            ('BUFFER_SYNC_TO_EXTRUDER',     self.cmd_BUFFER_SYNC_TO_EXTRUDER,     None),
            ('BUFFER_UNSYNC',               self.cmd_BUFFER_UNSYNC,               None),
            ('FORCE_BUFFER_FILL',           self.cmd_FORCE_BUFFER_FILL,           None),
            ('STOP_BUFFER_FILL',            self.cmd_STOP_BUFFER_FILL,            None),
            ('BUFFER_STATE_DUMP',           self.cmd_BUFFER_STATE_DUMP,           None),
            ('BUFFER_SET',                  self.cmd_BUFFER_SET,                  None),
            ('CALIBRATE_FEEDER_SYNC',       self.cmd_CALIBRATE_FEEDER_SYNC,       None),
            ('MEASURE_LOAD_START',          self.cmd_MEASURE_LOAD_START,          None),
            ('MEASURE_LOAD_STOP',           self.cmd_MEASURE_LOAD_STOP,           None),
            ('ENABLE_RUNOUT_SENSOR',        self.cmd_ENABLE_RUNOUT_SENSOR,
                "Set print_running=1 — enable runout PAUSE"),
            ('DISABLE_RUNOUT_SENSOR',       self.cmd_DISABLE_RUNOUT_SENSOR,
                "Set print_running=0 — disable runout PAUSE"),
            ('BUFFER_CLEAR_JAM',            self.cmd_BUFFER_CLEAR_JAM,
                "Clear JAM state after operator intervention"),
            ('BUFFER_RESTORE_STATE',        self.cmd_BUFFER_RESTORE_STATE,
                "Best-effort restore of gcode-state saved by a failed LOAD/UNLOAD"),
            ('BUFFER_SAVE_MACRO_STATE',     self.cmd_BUFFER_SAVE_MACRO_STATE,
                "Internal: mark gcode-state as saved (used by _SAVE_E_MODE)"),
            ('BUFFER_RESTORE_MACRO_STATE',  self.cmd_BUFFER_RESTORE_MACRO_STATE,
                "Internal: restore + clear gcode-state save (used by _RESTORE_E_MODE)"),
        ]
        # Save the table so _handle_ready can register a default-mux
        # fallback (BUFFER=None) when this is the only buffer_feeder
        # instance. Resolve help_text inline so the second-pass
        # registration uses identical descriptions.
        self._command_table = []
        for name, handler, help_text in commands:
            if help_text is None:
                help_text = getattr(self, 'cmd_' + name + '_help', None)
            self._command_table.append((name, handler, help_text))
            gcode.register_mux_command(name, 'BUFFER', self.name,
                                       handler, desc=help_text)

    def _register_default_mux_if_only_instance(self):
        """P7-62: When this is the only [buffer_feeder ...] section,
        register every command a SECOND time with BUFFER=None as the
        default-fallback. The user can then call commands without the
        BUFFER=mellow argument:
            BUFFER_FEED                  (no mux)
            BUFFER_FEED BUFFER=mellow    (explicit, also works)

        Multi-instance setups keep the mandatory mux-key (calling
        BUFFER_FEED without BUFFER= would be ambiguous).

        Called from _handle_ready so all instances have already
        registered their __init__ via Klipper's load_config_prefix.
        """
        instances = [obj for name, obj in self.printer.lookup_objects()
                     if name.startswith('buffer_feeder ')]
        if len(instances) != 1:
            return
        gcode = self.printer.lookup_object('gcode')
        for name, handler, help_text in self._command_table:
            try:
                gcode.register_mux_command(name, 'BUFFER', None,
                                           handler, desc=help_text)
            except Exception:
                # Already registered or other error — log + continue.
                # Not fatal: explicit BUFFER=name still works.
                logging.exception(
                    "buffer_feeder: default-mux register failed for %s",
                    name)

    # -----------------------------------------------------------------------
    # Pin registration helper
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
        # P7-62: Optional default-mux fallback so single-instance
        # setups can use commands without BUFFER=<name>. Done here
        # (not in __init__) because all buffer_feeder sections must
        # have completed their __init__ before we count instances.
        self._register_default_mux_if_only_instance()

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

    def _clear_stale_suspend_if_print_inactive(self, eventtime):
        """Lazy stale-suspend recovery (P7-56f follow-up).

        idle_timeout:ready only fires once per printing→ready transition.
        The PAUSE → CANCEL pathway is therefore stuck:
          1. PAUSE → :ready fires → _bang_bang_suspended=True
          2. CANCEL_PRINT runs → print_stats.state='cancelled', no new
             :ready event (we're already in ready)
          3. _bang_bang_suspended stays True forever
        Same trap exists for PAUSE → ERROR.

        This helper polls print_stats.state at decision points (entrance
        insert, _check_auto_ready) and clears the stale flag when the
        print is no longer paused/running. Returns True if it cleared
        something so the caller can re-evaluate any guard that depends
        on the flag."""
        if not self._bang_bang_suspended:
            return False
        try:
            ps = self.printer.lookup_object('print_stats')
            ps_state = ps.get_status(eventtime).get('state')
        except Exception:
            return False
        if ps_state in ('printing', 'paused'):
            return False
        # Print is no longer pause-recoverable (complete / cancelled /
        # error / standby). Clear the stale lock so next entrance-
        # insert / AUTO_ON_IF_READY proceeds.
        self._bang_bang_suspended = False
        self._respond("Stale bang-bang-suspend cleared "
                      "(print state=%s)" % ps_state)
        return True

    def _on_idle_ready(self, *args):
        # idle_timeout:ready fires for BOTH a manual PAUSE during a
        # print (RESUME erwartet) AND for the natural end of a print
        # job ("Done printing file" → no RESUME ever). P7-56f: read
        # print_stats.state so we differentiate. Pre-fix the buffer
        # would stay _bang_bang_suspended=True after a clean print
        # end, blocking auto-grip on the next entrance-insert and
        # forcing the operator to run BUFFER_AUTO_OFF + AUTO_ON or
        # FORCE_BUFFER_FILL just to load fresh filament.
        #
        # Guard: Klipper fires idle_timeout:printing then :ready during
        # MCU init, which would set _print_running=True and then arm
        # _bang_bang_suspended before any real print has started.
        # Ignore all idle_timeout events until the startup grace is done.
        if not self._startup_grace_done:
            self._print_running = False
            return
        if self._print_running:
            ps_state = None
            try:
                ps = self.printer.lookup_object('print_stats')
                ps_state = ps.get_status(self.reactor.monotonic()).get('state')
            except Exception:
                pass
            if ps_state == 'paused':
                # Real PAUSE — RESUME is expected, suspend bang-bang
                # so a queued G1 E in the resumed file doesn't fire
                # an unexpected feed before the print actually resumes.
                self._bang_bang_suspended = True
                if self._continuous_feed:
                    self._continuous_feed = False
                    self._halt_motion()
                self._respond("Print paused — bang-bang suspended until RESUME")
            else:
                # Print ended normally (state=complete/standby/None).
                # Buffer stays available for manual workflow + reinsert
                # auto-grip. _bang_bang_suspended stays whatever it
                # was (operator may have set it explicitly via
                # BUFFER_AUTO_OFF; we don't override).
                self._respond("Print ended — buffer ready for next "
                              "filament change or print")
        self._print_running = False

    # -----------------------------------------------------------------------
    # Sensor: raw pin change + debounce
    # -----------------------------------------------------------------------

    def _check_debounce(self, eventtime):
        """Promote raw->stable after hall_debounce_ms."""
        return self.sensors.check_debounce(eventtime)

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
    def _hall3_drop_since(self):
        return self.fault._hall3_drop_since

    @_hall3_drop_since.setter
    def _hall3_drop_since(self, value):
        self.fault._hall3_drop_since = value

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

    def _mark_hall1_active(self):
        """C-cont T5: HALL1-Edge im STATE_AUTO — defer state-transition,
        nur Timestamp setzen. _main_tick prueft Persist >
        hall1_persist_timeout fuer echten OVERFLOW-Safety-Trigger.

        Idempotent: bereits gesetzten Timestamp NICHT ueberschreiben,
        damit Persist-Dauer korrekt akkumuliert."""
        if self._hall1_active_since is None:
            self._hall1_active_since = self.reactor.monotonic()

    def _mark_hall1_cleared(self):
        """C-cont T5: HALL1 falling-edge — Timestamp loeschen, Persist-
        Counter zurueck auf None. Auch bei _exit_overflow muss diese
        Methode gerufen werden damit ein neuer HALL1-Edge sauber tracked
        wird."""
        self._hall1_active_since = None

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
            self._respond("HALL1 cleared — overflow lockout released (overlay)")
            self._resume_after_overflow()
            return
        if self._state != STATE_OVERFLOW:
            return
        self._respond("HALL1 cleared — overflow lockout released")
        # P7-54: Mark cursor resync pending. _main_tick will submit a
        # 0.05mm anchor with forced_t0=None (safe reactor context) so the
        # stepcompress cursor is synchronised before the first fill-move.
        # _on_mcu_flush skips while this flag is set (see below).
        self._needs_overflow_prime = True
        # Go to IDLE (the _set_state hook calls _halt_motion + stepper-disable).
        self._set_state(STATE_IDLE)
        self._fault_overflow = False
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
        """Reactor-tick dispatcher. Order matters — HALL1 lockout has
        absolute priority, then safety-timers fire jam-detection, then
        late-disable, then state-completion handlers, then submit
        helpers (bang-bang / phase3 / continuous / pending-chunks)."""
        try:
            # C-cont T2: Velocity-Tracker tick (50Hz, intern throttled
            # auf sample_interval=0.025s = 40Hz). Read-only passiver
            # Observer ueber extruder.get_status — kein flush, kein
            # SYNC, kein Side-Effect auf den Druckkopf.
            self.velocity_tracker.tick(eventtime)

            self._check_debounce(eventtime)

            # During startup grace, only sensor polling runs. No state
            # transitions, no bang-bang, no continuous feed — we wait
            # for Klipper to deliver initial sensor callbacks so we
            # learn the real hardware picture.
            if not self._startup_grace_done:
                return eventtime + MAIN_TICK_INTERVAL

            # C-cont T6: HALL1-Persist-Check. In STATE_AUTO loest HALL1-
            # Edge nicht mehr direkt _enter_overflow aus (siehe T5). Erst
            # wenn HALL1 laenger als hall1_persist_timeout aktiv ist,
            # eskaliere zu echtem _enter_overflow (Hardware-Safety-State).
            # In der Zwischenzeit setzt SpeedModulator (T4) bereits
            # target_speed=0, der Stepper foerdert nicht. Damit ist HALL1
            # nicht mehr ein 'instant-State-Wechsel' aber bleibt eine
            # harte Safety-Eskalation bei mechanisch stuck buffer.
            if (self._state == STATE_AUTO
                    and self._hall1_active_since is not None):
                persist_duration = (
                    self.reactor.monotonic() - self._hall1_active_since)
                if persist_duration >= self.hall1_persist_timeout:
                    if self.buffer_debug_metrics:
                        logging.info(
                            "buffer_feeder: HALL1-Persist %.2fs >= "
                            "%.2fs threshold — entering OVERFLOW state "
                            "(C-cont T6)",
                            persist_duration, self.hall1_persist_timeout)
                    self._enter_overflow()
                    return eventtime + MAIN_TICK_INTERVAL
                # Persist innerhalb Timeout: kein Hard-Trigger noetig,
                # der Hard-Pfad unten greift in STATE_AUTO ohnehin nicht
                # (siehe T6-cleanup-Guard). Fall-through zum naechsten
                # Tick.

            # HALL1 has absolute priority — AUSSER bei aktivem Manual-
            # Retract oder einer UNLOAD-Phase: dann lassen wir den
            # Operator/das Macro den Buffer entlasten. Sobald die
            # Retract-Sequenz endet, greift der Reassert wieder normal.
            # OVERFLOW_OK=1 in Phase 3 (P7-8): _is_hall1_active kapselt
            # die caller-spezifischen Bypasses (siehe FaultManager).
            # C-cont T6 cleanup: HALL1-Hard-Trigger nur noch fuer nicht-
            # AUTO-States (LOAD/MANUAL/UNLOAD/etc.). In STATE_AUTO
            # uebernimmt der Persist-Check (Z.~2049ff.) die HALL1-
            # Behandlung mit Soft-Timer-Eskalation. Toter Code (AUTO-
            # Pfad) sichtbar entfernt.
            if (self._state != STATE_AUTO
                    and self._is_hall1_active('main_tick')):
                self._enter_overflow()
                return eventtime + MAIN_TICK_INTERVAL

            self._tick_safety_timeouts(eventtime)

            # Deferred disable: motor_disable must not be called while
            # steps are unprocessed in the trapq (step-gen fires
            # motor_enable with a past time via add_active_callback →
            # Timer too close).
            if self._pending_disable and not self._move_in_flight():
                self._pending_disable = False
                self._disable_stepper()

            # P7-70 (Issue #12): Idle-Watchdog.
            # In STATE_IDLE neither the bang-bang flush-callback nor any
            # other periodic move-submit runs. _last_move_end_time freezes
            # at the time the last queued move ended. Once Klipper's
            # background flush_handler fires more than CLOCK_DIFF_MAX
            # (~17s @ 48 MHz) after that anchor, compress_bisect_add
            # degenerates into an "Invalid sequence" → MCU shutdown.
            # The reactive REPRIME path in _submit_single_trapezoid runs
            # only on the NEXT submit, which never comes in IDLE.
            #
            # P7-75 (Issue #31, Eifel-Joe Hardware-Test 2026-05-12):
            # Same stale-cursor pathology hits in STATE_AUTO when the
            # buffer sits in the bang-bang hysteresis dead-zone (neither
            # hall_full nor hall_empty). _on_mcu_flush does nothing
            # there; _bang_bang_tick does nothing there. last_step_clock
            # ages from the boot anchor until the first hall_empty
            # finally arms a submit — by then queue_step interval has
            # blown past int32 (P7-73 clamps far-future forced_t0 but
            # cannot heal the past-end). Extend the watchdog gate to
            # STATE_AUTO with extra sub-gates so it never collides with
            # an active bang-bang session.
            #
            # Fix: fire a 0.05 mm anchor (boot-anchor / SYNC-gap-anchor
            # pattern, see SyncCoordinator._submit_anchor_move) whenever
            # idle_anchor_gap seconds elapsed since the last move and
            # the last watchdog-anchor. The anchor refreshes
            # last_step_clock and re-arms _last_move_end_time, so the
            # next background flush stays inside CLOCK_DIFF_MAX.
            #
            # Gates:
            #   - state in (IDLE, AUTO): IDLE handled by P7-70; AUTO is
            #     the bang-bang dead-zone case from P7-75/Issue #31.
            #     MANUAL/LOAD/UNLOAD have their own move cadence +
            #     dedicated reprime paths and stay out.
            #   - not synced: when bound to extruder trapq, moves come
            #     from the extruder side and we must not inject our own.
            #   - not _move_in_flight / not pending: no overlap with a
            #     drain-in-progress (defense in depth; in IDLE these are
            #     typically False already).
            #   - _last_idle_anchor_time gating: a second tick right
            #     after firing must NOT submit another anchor — the same
            #     idle_anchor_gap window applies between anchors.
            #   - AUTO-specific sub-gates (P7-75) keep the watchdog out
            #     of any active bang-bang flow:
            #       * not _continuous_feed     — bang-bang inactive
            #       * not hall_empty           — no open feed request
            #       * not _needs_overflow_prime — no pending prime
            #       * not hall_full            — buffer already full;
            #         further forward anchors would push toward HALL1
            #         overflow (P7-75b Codex-Verify finding: ~18mm/h
            #         drift without this gate at default idle_anchor_gap=10s)
            # P7-76 C: Diagnostic-Logging fuer Watchdog-Blocks.
            # Wenn die "harten" Move-/Sync-Gates clean sind aber ein
            # Sub-Gate (continuous_feed/hall_empty/hall_full/needs_
            # overflow_prime) den Anchor blockiert, log das aktive
            # Sub-Gate. Hilft kuenftige Issue-#32-Klassen ohne weitere
            # Hardware-Repros zu diagnostizieren (DWELL-SA3 Eifel-Joe
            # Crash #3: 56.6s ohne Anchor trotz scheinbar quiescentem
            # AUTO). Rate-limit: einmal pro idle_anchor_gap-Fenster.
            if (self._state in (STATE_IDLE, STATE_AUTO)
                    and not self._stepper_synced_to
                    and not self._pending_disable
                    and not self._move_in_flight()
                    and self._pending_remaining_mm == 0.0):
                _mcu = self.stepper.get_mcu()
                _mcu_now = _mcu.estimated_print_time(
                    self.reactor.monotonic())
                _gap_moves_diag = _mcu_now - self._last_move_end_time
                if _gap_moves_diag > self.idle_anchor_gap * 1.5:
                    _blocking = []
                    if self._continuous_feed:
                        _blocking.append("_continuous_feed")
                    if self.hall_empty:
                        _blocking.append("hall_empty")
                    if self.hall_full:
                        _blocking.append("hall_full")
                    if self._needs_overflow_prime:
                        _blocking.append("_needs_overflow_prime")
                    # P7-76 C: separate watermark for log-rate (not
                    # _last_idle_anchor_time — that is only updated when
                    # an anchor actually fires; in the blocked path no
                    # anchor fires so using it for rate-limiting would
                    # spam every tick).
                    _last_skip_log = getattr(
                        self, '_last_watchdog_skip_log_time', 0.0)
                    if _blocking and (
                            _mcu_now - _last_skip_log
                            > self.idle_anchor_gap):
                        logging.debug(
                            "buffer_feeder: watchdog skip "
                            "(state=%s gap=%.1fs > %.1fs threshold) "
                            "blocked by: %s (P7-76 C diagnostic)",
                            self._state, _gap_moves_diag,
                            self.idle_anchor_gap,
                            ",".join(_blocking))
                        self._last_watchdog_skip_log_time = _mcu_now

            # P7-77 A (Issue #32 Crash unter P7-76, Eifel-Joe Hardware-
            # Log 2026-05-12 klippy.log "(2).txt"): Watchdog HARD-block
            # waehrend aktivem Print. Diagnose:
            #   1. Watchdog-Anchor laeuft legitim (gap > threshold),
            #      schiebt stepcompress.last_step_clock auf ~551.18s.
            #   2. 4 nachfolgende Bang-Bang-Tick-Submits (continuous_-
            #      feed-streaming, forced_t0=None Pfad) clampen t0 via
            #      P7-76 A auf mcu_now + lead_time = ~551.13s.
            #   3. ABER: last_step_clock = 551.18 vom legitimen Anchor
            #      -> interval = 551.13 - 551.18 = -10.4ms -> negativer
            #      interval -> stepcompress-Crash (i=-500471).
            # Architektonisch ist `t0 = max(forced_t0, lme, en, mcu_-
            # now)` blind gegen `last_step_clock`. Waehrend eines aktiven
            # Prints uebernimmt _on_mcu_flush + P7-73 (forced_t0-Pfad)
            # die Cursor-Pflege; Watchdog ist konzeptionell nur fuer
            # echtes IDLE/Standby. -> Print-Stats-Check skipt Watchdog
            # bei state == 'printing'. paused/complete/cancelled/standby
            # zaehlen NICHT als active print (paused: User-Halt, kein
            # ongoing flush; complete: lookahead leer; standby/cancelled:
            # kein Print).
            #
            # P7-78 (Issue #29 Crash unter P7-77, Eifel-Joe Hardware-
            # Log 2026-05-13): Der P7-77 A Hard-Block ist zu strikt.
            # In der HALL2-Hysterese-Zwischenzone laeuft _on_mcu_flush
            # minutenlang nicht — Klipper's motion_queuing.flush_handler
            # ruft den Callback nur synchron mit Step-Generation; ohne
            # Steps kein Callback. stepcompress.last_step_clock altert,
            # und der erste Bang-Bang-Submit nach Stille wirft c=7
            # Invalid sequence. Eifel-Joe Beleg: 163.2s Funkstille
            # zwischen IDLE->AUTO (Z.8706 @ 1063.5s) und Crash (Z.9895
            # @ 1226.7s). Loesung: Print-Block-Override — wenn _on_mcu_-
            # flush messbar laenger als idle_anchor_gap nicht gerufen
            # wurde, weichen wir den Hard-Block auf und lassen den
            # Watchdog feuern. Boot-Schutz: _last_mcu_flush_time == 0.0
            # zaehlt nicht (frischer Boot, noch nie ein Flush).
            _print_active = False
            try:
                _ps = self.printer.lookup_object('print_stats', None)
                if _ps is not None:
                    _ps_status = _ps.get_status(eventtime)
                    _print_active = (
                        _ps_status.get('state') == 'printing')
            except Exception:
                _print_active = False

            # P7-78 Print-Block-Stale-Override: nur evaluieren wenn
            # ueberhaupt geblockt waere und mindestens ein Flush
            # bereits gesehen wurde (Boot-Schutz). Strict > damit
            # Stille == idle_anchor_gap noch geblockt bleibt.
            #
            # P7-78v2 (Codex-Verify Finding): _p778_override Flag
            # markiert den Override-Pfad, damit der innere Anchor-
            # Submit `forced_t0=mcu_now + lead_time` uebergibt und
            # den P7-77 B SKIP-statt-Clamp im else-Branch umgeht.
            # Ohne den Flag wuerde der Override zwar feuern, aber
            # `_submit_anchor_move()` (ohne kwarg) faellt in den
            # forced_t0==None else-Branch -> th_time = aktive
            # Toolhead-Queue (far-future) -> P7-77 B SKIP ->
            # silent return ohne realen Submit -> Bug wirkungslos.
            _p778_override = False
            if _print_active and self._last_mcu_flush_time > 0.0:
                _mcu_p778 = self.stepper.get_mcu()
                _mcu_now_p778 = _mcu_p778.estimated_print_time(
                    self.reactor.monotonic())
                _flush_silence = (
                    _mcu_now_p778 - self._last_mcu_flush_time)
                if _flush_silence > self.idle_anchor_gap:
                    logging.info(
                        "buffer_feeder: print-block stale override "
                        "(flush silent for %.1fs > %.1fs threshold, "
                        "P7-78)",
                        _flush_silence, self.idle_anchor_gap)
                    _print_active = False
                    _p778_override = True

            if (self._state in (STATE_IDLE, STATE_AUTO)
                    and not self._stepper_synced_to
                    and not self._pending_disable
                    and not self._move_in_flight()
                    and self._pending_remaining_mm == 0.0
                    and not self._continuous_feed
                    and not self.hall_empty
                    and not self.hall_full
                    and not self._needs_overflow_prime
                    and not _print_active):  # P7-77 A + P7-78 Override
                mcu = self.stepper.get_mcu()
                mcu_now = mcu.estimated_print_time(
                    self.reactor.monotonic())
                gap_moves = mcu_now - self._last_move_end_time
                gap_anchors = mcu_now - self._last_idle_anchor_time
                if (gap_moves > self.idle_anchor_gap
                        and gap_anchors > self.idle_anchor_gap):
                    # P7-77 C: lme-clamp NUR direkt vor dem Anchor-
                    # Submit, nicht bei jedem Tick. P7-76 D rollte lme
                    # unconditional bei jedem Tick zurueck — das
                    # radierte den Anchor-Effekt fuer alle nachfolgenden
                    # Bang-Bang-Ticks (sie sahen lme=mcu_now statt
                    # lme=anchor_end_time und produzierten t0-Werte
                    # zurueck unter last_step_clock). Inside des Submit-
                    # Branches: clamp greift nur einmal pro Watchdog-
                    # Anchor, danach setzt _submit_anchor_move lme
                    # konsistent in die Zukunft.
                    if self._last_move_end_time > mcu_now:
                        logging.debug(
                            "buffer_feeder: pre-anchor lme-clamp "
                            "(was %.3fs ahead of mcu_now, P7-77 C; "
                            "ex-P7-76 D, scope reduced)",
                            self._last_move_end_time - mcu_now)
                        self._last_move_end_time = mcu_now
                    try:
                        if _p778_override:
                            # P7-78v2: aktiver Print hat typisch weit-
                            # zukuenftige toolhead.get_last_move_time().
                            # Ohne forced_t0 wuerde der Anchor-Submit in
                            # den forced_t0==None else-Branch fallen
                            # (Z.3248) und durch P7-77 B SKIP (Z.3275)
                            # silent abgebrochen. Mit forced_t0=mcu_now+
                            # lead_time geht der Submit in den forced_t0
                            # !=None Branch (Z.3203), der NICHT vom
                            # P7-77 B Skip betroffen ist.
                            #
                            # P7-78v3 (Codex-Verify MEDIUM): lead_time
                            # mit min(..., MAX_FORCED_T0_LOOKAHEAD) cap,
                            # damit ein via BUFFER_SET ungewoehnlich
                            # gesetzter lead_time > 2.0s den forced_t0
                            # nicht in den P7-73 Clamp-Pfad zieht. Im
                            # Default-Fall (lead_time=0.3s) no-op.
                            _MAX_FORCED_T0_LOOKAHEAD = 2.0  # s
                            _p778_forced_t0 = (
                                mcu_now
                                + min(self.lead_time,
                                      _MAX_FORCED_T0_LOOKAHEAD))
                            self.sync._submit_anchor_move(
                                forced_t0=_p778_forced_t0)
                        else:
                            self.sync._submit_anchor_move()
                        self._last_idle_anchor_time = mcu_now
                        # In IDLE we re-defer the disable so IDLE
                        # semantics (stopped AND disabled) are restored
                        # once the anchor-move drains. In AUTO we must
                        # NOT disable — bang-bang owns the next submit
                        # and a disable here would race with it.
                        if self._state == STATE_IDLE:
                            self._schedule_stepper_disable()
                        logging.info(
                            "buffer_feeder: %s anchor fired "
                            "(gap=%.1fs, threshold=%.1fs)",
                            self._state.lower(),
                            gap_moves, self.idle_anchor_gap)
                    except Exception:
                        logging.exception(
                            "buffer_feeder: idle/auto anchor failed")

            self._tick_cooldown_end(eventtime)
            self._tick_grip_completion(eventtime)

            # Bang-bang nur in AUTO. (P7-16 erweiterte das auf
            # UNLOAD_PHASE_1, aber P7-20 hat den Tip-Forming-Pfad
            # auf SYNC_TO_EXTRUDER umgestellt — UNLOAD_PHASE_1 wird
            # nicht mehr betreten.)
            if self._state == STATE_AUTO:
                # P7-54: Post-OVERFLOW cursor resync. After OVERFLOW →
                # IDLE → AUTO the stepcompress cursor is stale.
                # P7-60: when use_flush_callback_bang_bang is active,
                # the prime-anchor MUST go through _on_mcu_flush so it
                # gets a race-free step_gen_time anchor. The legacy
                # forced_t0=None path here calls flush_step_generation
                # mid-print + set_position, which rips itersolve under
                # in-flight steps if _stepcompress_primed=False (which
                # it is post-OVERFLOW because of deferred-disable).
                # Hardware-Crash 2026-04-29 (klippy.log #6: c=13,
                # gap=-0.6s) reproduced that exact path.
                if self._needs_overflow_prime:
                    if not self.use_flush_callback_bang_bang:
                        self._needs_overflow_prime = False
                        self._submit_move(0.05, self.feed_speed,
                                          forced_t0=None)
                    # else: leave the flag set — _on_mcu_flush picks
                    # it up on the next flush-cycle and submits with
                    # forced_t0=step_gen_time+lead_time.
                self._bang_bang_tick(eventtime)

            self._tick_runout_follow(eventtime)

            # LOAD Phase 3 — feed until HALL2 or max distance.
            if self._state == STATE_LOAD_PHASE_3:
                self._load_phase3_tick(eventtime)

            # Continuous feed: keep chunks streaming, but only in
            # states where continuous motion is the intended behavior
            # (CONTINUOUS_FEED_STATES). Otherwise stale _continuous_feed
            # would leak into LOAD_PHASE_1 single-shot moves.
            #
            # P7-59: When flush_callback_bang_bang is active and we're
            # in STATE_AUTO, _on_mcu_flush owns chunk submission with
            # race-free step_gen_time anchors. Streaming a parallel
            # chunk here with forced_t0=None races against the flush-
            # callback anchor — the result is a negative gap (last_-
            # move_end_time > mcu_now) plus a stale _stepcompress_-
            # primed flag, which triggers a mid-print flush_step_-
            # generation() + set_position((0,0,0)) and rips itersolve
            # under in-flight steps → "Invalid sequence" MCU shutdown.
            # Hardware-Crash 2026-04-29 (klippy.log #5: c=6, gap=-0.6s).
            # Manual + LOAD/UNLOAD phases keep using this reactor-tick
            # streaming path because _on_mcu_flush bails on non-AUTO.
            if (self._continuous_feed
                    and self._state in CONTINUOUS_FEED_STATES
                    and not (self.use_flush_callback_bang_bang
                             and self._state == STATE_AUTO)
                    and not self._move_in_flight()):
                chunk_dist = max(self.manual_chunk_distance,
                                 self._continuous_feed_speed * 0.5)
                self._submit_move(self._continuous_feed_direction * chunk_dist,
                                  self._continuous_feed_speed)

            self._tick_pending_chunk(eventtime)

        except Exception:
            logging.exception("buffer_feeder main_tick error")

        return eventtime + MAIN_TICK_INTERVAL

    def _tick_safety_timeouts(self, eventtime):
        """Hard-safety aborts route through _trigger_jam: phase
        commands raise via WAIT_IDLE, recovery requires explicit
        BUFFER_CLEAR_JAM / BUFFER_AUTO_OFF / STOP_BUFFER_FILL."""
        if (self._feed_deadline_time is not None
                and eventtime >= self._feed_deadline_time):
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
        #
        # P7-63: In STATE_AUTO with use_flush_callback_bang_bang, the
        # buffer arm can rest near the hall_empty threshold for long
        # stretches at high print flow without ever triggering hall_full.
        # The accumulator then grows past max_feed_distance and trips a
        # false JAM_SAFETY_DISTANCE while the system is operating
        # correctly (Issue #26). SUPPLY_JAM (via _jam_tick,
        # jam_supply_dwell_time) is the correct detector for genuine
        # mechanical jams in this mode. SAFETY_DISTANCE remains active
        # for LOAD/MANUAL phases and for legacy AUTO without bang-bang.
        if (self._continuous_feed
                and self._continuous_feed_direction == 1
                and not (self.use_flush_callback_bang_bang
                         and self._state == STATE_AUTO)
                and self._feed_distance_accumulator >= self.max_feed_distance):
            self._trigger_jam(
                "SAFETY_DISTANCE",
                "max_feed_distance %dmm reached without HALL2 — slipping "
                "drive gear, kinked filament, or value too low for setup "
                "(bowden+buffer path; bump max_feed_distance in lll.cfg "
                "if first-fill is legit)"
                % int(self.max_feed_distance))

    def _tick_cooldown_end(self, eventtime):
        """Cooldown end: back to AUTO if entrance present AND the
        operator hasn't explicitly disabled AUTO."""
        if self._cooldown_deadline is None or eventtime < self._cooldown_deadline:
            return
        if self._state in (STATE_MANUAL_FEED, STATE_MANUAL_RETRACT,
                           STATE_INITIAL_GRIP):
            # Guard: estimate may fire early (per-chunk gap not
            # accounted for). Only transition once the move is truly
            # done.
            if self._move_in_flight() or self._pending_remaining_mm > 0:
                self._cooldown_deadline = eventtime + 0.05
                return
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

    def _tick_grip_completion(self, eventtime):
        """Initial grip done → follow-feed (if configured) or IDLE.
        Follow-feed done → IDLE. Both branches optionally schedule
        LOAD_FILAMENT via _maybe_auto_load."""
        if (self._state == STATE_INITIAL_GRIP
                and self._initial_grip_end_time is not None):
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
                    self._maybe_auto_load()

        # Follow-feed completion: grip + follow done, drop to IDLE.
        if (self._state == STATE_INITIAL_GRIP
                and self._grip_follow_active
                and self._initial_grip_end_time is None
                and not self._move_in_flight()
                and self._pending_remaining_mm <= 0):
            self._grip_follow_active = False
            self._set_state(STATE_IDLE)
            self._respond("Grip follow done — IDLE")
            self._maybe_auto_load()

    def _tick_runout_follow(self, eventtime):
        """RUNOUT-follow (runout_pause=0 mode): bang-bang keeps
        running in AUTO; we just track extruder distance here."""
        if not (self._runout_follow_active
                and self._runout_filament_ref is not None):
            return
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

    def _tick_pending_chunk(self, eventtime):
        """Pending-chunk streaming for long single-shot moves.
        Schedule the next chunk when the current one is within
        half-a-chunk-duration of ending, so chunks abut without a
        visible gap in motion. Abort signals zero out the pending
        counter — already-queued trapezoids drain on the MCU."""
        if self._pending_remaining_mm <= 0:
            return
        if self._abort_signalled():
            self._pending_remaining_mm = 0.0
            return
        # P7-66b: HALL2 (buffer full) MUST abort a forward streaming
        # sequence. _abort_signalled covers HALL1 (overflow) but not
        # the bang-bang stop-on-full case. Without this clamp the
        # sub-chunks of a 45mm chunk would keep flowing into a full
        # buffer until the original distance was exhausted — exactly
        # the overshoot the hardware-test 2026-05-12 hit.
        # Only forward direction + AUTO state — retract / UNLOAD must
        # still drain pending distance regardless of HALL2 (it pulls
        # filament back, doesn't push into the buffer).
        if (self._pending_direction > 0
                and self._state == STATE_AUTO
                and self.hall_full):
            self._pending_remaining_mm = 0.0
            self._continuous_feed = False
            return
        if (self._pending_direction < 0
                and self._state == STATE_MANUAL_RETRACT
                and not self.entrance_detected):
            self._halt_motion()
            self._respond("Retract-Burst gestoppt — Filament am Eingang weg")
            return
        if self._pending_speed <= 0:
            return
        # P7-66b: honour the sub-chunk cap if the active stream was
        # opened with one. AUTO+streaming sets cap=interrupt_chunk_mm
        # so HALL-interrupt latency stays bounded; legacy paths
        # (LOAD/UNLOAD/MANUAL) leave _pending_submit_chunk_cap=None and
        # fall back to max_move_chunk_mm exactly as before.
        cap = self._pending_submit_chunk_cap
        if cap is None or cap > self.max_move_chunk_mm:
            cap = self.max_move_chunk_mm
        chunk_duration = cap / self._pending_speed
        mcu = self.stepper.get_mcu()
        now_pt = mcu.estimated_print_time(eventtime)
        gap = self._last_move_end_time - now_pt
        # Submit next chunk when <= half-a-chunk remains in the
        # currently-queued move, so next trapezoid starts right at
        # the prior one's end_time.
        if gap <= chunk_duration * 0.5:
            chunk = min(self._pending_remaining_mm, cap)
            # P7-66 R1: this is the streaming continuation of an
            # already-running burst. Pass streaming=True so the
            # _enable_stepper() and _last_enable_schedule_time floor
            # are skipped — same rationale as the lookahead branch in
            # _on_mcu_flush. _move_in_flight() is implicit here: the
            # gap-check above only fires while the previous trapezoid
            # is still in the future.
            self._submit_single_trapezoid(
                self._pending_direction * chunk, self._pending_speed,
                streaming=True)
            self._pending_remaining_mm -= chunk
            if self._pending_remaining_mm <= 0:
                # Drop the cap so a subsequent unrelated _submit_move
                # call does not inherit it.
                self._pending_submit_chunk_cap = None

    def _bang_bang_tick(self, eventtime):
        """HALL-based bang-bang with hysteresis. Reactor-tick driven —
        anchors submits via toolhead.get_last_move_time which fights
        against active toolhead-moves (lag during manual G1 E50). The
        flush-callback path (_on_mcu_flush, P7-52) is the preferred
        replacement when use_flush_callback_bang_bang is enabled."""
        if self._bang_bang_suspended:
            # Print is paused — do nothing until idle_timeout:printing.
            return
        # P7-69 (Issue #18): An explicit BUFFER_SYNC_TO_EXTRUDER (macro
        # path) has bound the stepper to the extruder trapq. Submitting
        # any move via the reactor-tick path while synced would queue
        # trapezoids on the wrong trapq AND can trip the gap>5s reprime
        # in _submit_single_trapezoid → mid-print toolhead.flush_step_-
        # generation() → extruder stop. Mirror the guard from
        # _on_mcu_flush (the legacy reactor-tick path was missing it).
        if self._stepper_synced_to is not None:
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

    def _compute_target_feed_speed(self):
        """C-cont T4: SpeedModulator.

        HALL-Sensoren + ExtruderVelocity -> target feed_speed (mm/s)
        fuer den naechsten Submit. Returns 0.0 als Notbremse (HALL1).

        Logik:
          HALL1 (overflow)  -> 0.0 (Notbremse, ohne State-Wechsel)
          HALL3 (empty)     -> max_feed_speed (Buffer auffuellen)
          HALL2 (full)      -> 0.5 * extruder_velocity (langsam)
          Zwischenzone      -> extruder_velocity (Balance)
          Tracker not_ready -> config feed_speed (Fallback)
        """
        if self.hall_overflow:
            return 0.0
        if self.hall_empty:
            return self.max_feed_speed
        if not self.velocity_tracker.is_ready():
            return self.feed_speed
        extruder_vel = self.velocity_tracker.get_velocity()
        if self.hall_full:
            return 0.5 * extruder_vel
        return extruder_vel

    def _on_mcu_flush(self, flush_time, step_gen_time):
        """P7-52: Flush-callback driven bang-bang. Klipper's motion_
        queuing module fires this synchronously inside the MCU flush
        cycle (klippy/extras/motion_queuing.py callback dispatch loop
        in flush_handler). We have two
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
        # P7-78 (Issue #29): Track flush-callback activity for Print-
        # Block-Override (Watchdog-Stale-Detection). Set BEFORE early-
        # returns so even filtered ticks (state != AUTO, suspended,
        # use_flush_callback_bang_bang=False) keep the timestamp fresh
        # — what matters is that the LLL_PLUS MCU is generating steps
        # somewhere, not whether we decided to act on this tick.
        self._last_mcu_flush_time = flush_time
        if not self.use_flush_callback_bang_bang:
            return
        if self._bang_bang_suspended:
            return
        if self._state != STATE_AUTO:
            # Macros and operator commands own non-AUTO states. Bang-
            # bang only acts in AUTO. LOAD/UNLOAD-driven SYNC paths
            # are handled by their own macros, not by us.
            return
        # P7-79 (Issue #29 Eifel-Joe 2026-05-13 c=14 i=0 Crash):
        # Defer flush-callback submits when a post-disable reprime
        # would race with itersolve still processing the pre-disable
        # move. Crash-Pfad (verifiziert gegen Source):
        #   1. M1 (z.B. 9mm Streaming-Chunk) submitted, lme = T+0.13s.
        #   2. HALL1 OVERFLOW -> _enter_overflow -> _halt_motion +
        #      _schedule_stepper_disable (Z.1719-1720). Deferred weil
        #      move in flight; _pending_disable=True.
        #   3. _main_tick mit M1 zeit-basiert vorbei (now > end) ruft
        #      _disable_stepper -> _stepcompress_primed=False (Z.1915-
        #      1917 + Z.2949).
        #   4. KERN: _move_in_flight() ist zeit-basiert (Z.3398:
        #      now_pt < end_time). Itersolve laeuft jedoch hinterher
        #      (step_gen_time < lme), pending Steps fuer M1 sind noch
        #      nicht generiert.
        #   5. HALL1 cleared -> _resume_after_overflow ->
        #      _needs_overflow_prime=True (Z.1772).
        #   6. Naechster _on_mcu_flush mit step_gen_time < lme(M1)
        #      feuert _submit_move(0.05, forced_t0=step_gen_time+
        #      lead_time). In _submit_single_trapezoid Z.3138:
        #      need_reprime=True (not primed). Forced_t0!=None-Pfad
        #      ueberspringt flush_step_generation(), ruft aber
        #      stepper.set_position((0,0,0)) (Z.3148) -> itersolve_pos
        #      = 0 (reset). _commanded_pos = 0.0.
        #   7. trapq_append(M2 prime: start=0, end=0.05, t0=anchor).
        #   8. Gleicher _advance_flush_time-Call: itersolve_gen_steps
        #      prozessiert pending M1 (start=0, end=9) -> itersolve_-
        #      pos=9, dann M2 (start=0, end=0.05) -> catch-up REVERSE
        #      9->0 = ~14 Steps auf demselben Clock -> c=14 i=0
        #      Invalid sequence (Eifel-Hardware-Log 24 mm^3/s,
        #      print_time=817.590s).
        # Fix: solange itersolve den pre-disable Move noch nicht
        # ausgespielt hat (itersolve_end > step_gen_time) UND der
        # Cursor durch _disable_stepper geclearrt wurde (not primed),
        # MUSS der naechste Submit deferren — sonst rasen
        # set_position(0) und das pending M1-Step-Generation
        # gegeneinander.
        # Position: vor dem _needs_overflow_prime-Block damit auch
        # der Overflow-Prime-Submit (Z.2517) deferred wird — das ist
        # genau der Crash-Pfad. Der _needs_overflow_prime-Flag bleibt
        # gesetzt, der naechste Tick mit advanced step_gen_time
        # uebernimmt den Submit. Worst-case Defer ~lead_time +
        # chunk_duration ~0.5-0.8s, akzeptabel fuer Overflow-
        # Recovery.
        # P7-79b (Codex-Verify v1 HIGH 2026-05-13): Anker MUSS
        # `_current_move['end_time']` sein, NICHT `_last_move_end_-
        # time`. Begruendung: P7-74 (`_halt_motion`, Z.3513-3514)
        # clampt `_last_move_end_time` auf `mcu_now` waehrend mid-
        # flight Overflow, laesst aber `_current_move` intakt
        # (Z.3452: explizite Doc-Garantie). Wenn nach dem Clamp
        # ein _on_mcu_flush mit step_gen_time zwischen mcu_now
        # (= geclamptes lme) und der echten Move-Ende-Zeit
        # (_current_move['end_time']) feuert, wuerde der alte
        # Check `_last_move_end_time > step_gen_time` False
        # liefern und den Defer umgehen — obwohl M1-Steps in
        # itersolve noch pending sind. Fallback `_last_move_end_-
        # time` greift wenn `_current_move is None` (nach Move-
        # Done aber vor neuem Submit / Boot-Anchor / Pre-Sync-
        # REPRIME-Pfade, die ebenfalls `_on_mcu_flush` mit
        # `_current_move=None` triggern).
        itersolve_end = (self._current_move['end_time']
                         if self._current_move is not None
                         else self._last_move_end_time)
        if (not self._stepcompress_primed
                and itersolve_end > step_gen_time):
            return
        if self._needs_overflow_prime:
            # P7-60: Post-OVERFLOW prime via flush-callback path.
            # _main_tick skips the prime when flush-callback bang-bang
            # is active so we own the anchor here. step_gen_time +
            # lead_time is the race-free reference point Klipper just
            # gave us — submitting the 0.05mm prime-move with this
            # anchor refreshes our stepcompress cursor without ever
            # calling flush_step_generation (which would mid-print
            # drain the toolhead and was the original P7-54 reason
            # to go via _main_tick). The follow-up bang-bang fills
            # then ride on a synchronised cursor.
            self._needs_overflow_prime = False
            anchor = step_gen_time + self.lead_time
            self._submit_move(0.05, self.feed_speed, forced_t0=anchor)
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

        # C-cont T7: Continuous-Streaming-Submit ersetzt den frueheren
        # Bang-Bang-Block (hall_full/hall_empty/else mit STABLE_DROP_-
        # GRACE). Jede flush-callback bestimmt die target_speed via
        # SpeedModulator (_compute_target_feed_speed) aus HALL-Sensor-
        # Kombination + ExtruderVelocityTracker. Submit nur wenn
        # target_speed > 0; sonst kein Move (Notbremse oder Modulator
        # entscheidet, dass aktuell kein Foerder-Bedarf besteht).
        #
        # Lookahead-Pipeline (P7-66 Streaming-Pattern) bleibt: ein
        # neuer Chunk wird erst eingereiht, wenn der laufende Move
        # weniger als lead_time Restzeit hat; sub-chunk-Pipeline
        # (_pending_remaining_mm) gates weiter wie bisher (HALL2-
        # Interrupt via move-splitting).
        target_speed = self._compute_target_feed_speed()
        if target_speed <= 0.0:
            if self.buffer_debug_metrics:
                logging.debug(
                    "buffer_feeder: target_speed=0 - kein Submit "
                    "(hall1=%s ready=%s)",
                    self.hall_overflow,
                    self.velocity_tracker.is_ready())
            return

        # Lookahead-Check: laeuft noch ein Move? P7-66 Streaming-Pattern.
        move_active = self._move_in_flight()
        if move_active:
            remaining = self._last_move_end_time - step_gen_time
            if remaining > self.lead_time:
                return  # noch frueh, kein neuer Chunk
        if self._pending_remaining_mm > 0:
            return  # Sub-Chunk-Pipeline laeuft

        # Anchor wie in Bang-Bang (P7-66 Pattern).
        if move_active:
            anchor = self._last_move_end_time
        else:
            anchor = step_gen_time + self.lead_time

        # C-cont T7: continuous_feed bleibt strukturell True im Stream-
        # Mode. Reset nur bei echtem Uebergang inactive->active
        # (P7-57: Safety-Distance-Accumulator-Reset; P7-61b:
        # _feed_deadline_time-Reset).
        if not self._continuous_feed:
            self._feed_distance_accumulator = 0.0
            self._feed_deadline_time = None
            self._continuous_feed = True
            self._continuous_feed_direction = 1
        self._continuous_feed_speed = target_speed

        self._submit_move(
            self.flush_callback_chunk_mm,
            target_speed,
            forced_t0=anchor,
            streaming=move_active,
            submit_chunk_cap=self.interrupt_chunk_mm)

    def _exit_phase3_stable(self, *, set_grace, respond_text):
        """Common exit-sequence when HALL2 (buffer full) or HALL1 (with
        OVERFLOW_OK=1) has been stable for the configured dwell.

        Halts streaming, clears all four Phase 3 trackers, optionally
        arms _post_load_overflow_grace (P7-46 bounce-suppression for
        HALL1-stable exit), and chooses STATE_AUTO when entrance is
        present + operator hasn't issued AUTO_OFF/HALT, else IDLE
        (P7-49: AUTO is the natural post-LOAD state — bang-bang
        re-fills on next extrusion regardless of print_running)."""
        self._continuous_feed = False
        self._halt_motion()
        self._load_phase3_hall_full_since = None
        self._load_phase3_hall_overflow_since = None
        self._load_phase3_hall_full_drop_since = None
        self._load_phase3_hall_overflow_drop_since = None
        self._respond(respond_text)
        if set_grace:
            self._post_load_overflow_grace = True
        if (self.entrance_detected
                and not self._auto_off_by_user
                and not self._halt_requested):
            self._set_state(STATE_AUTO)
        else:
            self._set_state(STATE_IDLE)

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
                if threshold > 0:
                    msg = ("LOAD Phase 3: HALL2 stable %.1fs, buffer full"
                           % full_dwell)
                else:
                    msg = "LOAD Phase 3: HALL2 reached, buffer full"
                self._exit_phase3_stable(set_grace=False, respond_text=msg)
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
        # _main_tick → _enter_overflow abgewickelt: legacy-Pfad
        # state=OVERFLOW + raise im cmd_BUFFER_LOAD_PHASE3-Postcheck;
        # use_fault_overlay=1-Pfad belaesst state=LOAD_PHASE_3 und setzt
        # nur _fault_overflow=True, raise dann im selben Postcheck via
        # fault_overflow-Flag.
        if self._load_phase3_overflow_ok:
            if self.hall_overflow:
                self._load_phase3_hall_overflow_drop_since = None
                if self._load_phase3_hall_overflow_since is None:
                    self._load_phase3_hall_overflow_since = eventtime
                overflow_dwell = eventtime - self._load_phase3_hall_overflow_since
                if overflow_dwell >= threshold:
                    msg = ("LOAD Phase 3: HALL1 stable %.1fs, "
                           "buffer overfilled (treating as full)"
                           % overflow_dwell)
                    # set_grace=True: P7-46 bounce-suppression — _main_tick
                    # would otherwise re-trigger _enter_overflow on the
                    # next cycle since HALL1 stays asserted.
                    self._exit_phase3_stable(set_grace=True, respond_text=msg)
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
                self._hall3_drop_since = None
                return eventtime + JAM_TICK_INTERVAL

            if self._state not in JAM_WATCH_STATES:
                # Reset trackers.
                self._hall2_start_time = None
                self._hall3_start_time = None
                self._hall3_drop_since = None
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
            # P7-63 stelle 7: HALL3-Drop-Grace. Without it, brief bouncing
            # flicker (30-500ms HALL3 false-edges, mechanical normal at high
            # flow) would permanently reset _hall3_start_time before
            # jam_supply_dwell_time elapses. Same STABLE_DROP_GRACE pattern
            # as _load_phase3_tick uses for HALL1/HALL2. Required because
            # SUPPLY_JAM is now the sole backstop for AUTO+bang-bang
            # (SAFETY_DISTANCE bypassed there by stelle 6).
            feeder_running_fwd = self._continuous_feed and self._continuous_feed_direction == 1
            if self.hall_empty and feeder_running_fwd:
                self._hall3_drop_since = None
                if self._hall3_start_time is None:
                    self._hall3_start_time = eventtime
                else:
                    dwell = eventtime - self._hall3_start_time
                    if dwell >= self.jam_supply_dwell_time:
                        self._trigger_jam("SUPPLY",
                            "HALL3 active %.0fs with feeder running — spool/supply jam suspected"
                            % dwell)
            else:
                if self._hall3_start_time is None:
                    self._hall3_drop_since = None
                elif self._hall3_drop_since is None:
                    self._hall3_drop_since = eventtime
                elif eventtime - self._hall3_drop_since >= STABLE_DROP_GRACE:
                    self._hall3_start_time = None
                    self._hall3_drop_since = None
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
            # Defer jam_action via 1ms timer — _trigger_jam runs from
            # _jam_tick (reactor timer). Direct run_script() would block
            # the reactor for the full macro duration (P7-56b).
            self._schedule_gcode_script(self.jam_action)

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

        P7-58: Removed the toolhead.get_last_move_time() lookup that
        used to feed a 4th max() argument. The buffer stepper runs in
        own_trapq — toolhead's last move time has no bearing on our
        own enable scheduling, and the lookup synchronously runs the
        toolhead lookahead pipeline (_process_lookahead /
        _flush_lookahead in mainline klippy/toolhead.py) on every
        call. _enable_stepper() runs before every chunk submit, so
        each bang-bang feed forced the toolhead through a lookahead
        flush → brief extruder pause (host-side planning work) →
        visible gaps in the print.

        The remaining three floors (mcu_now, _last_move_end_time,
        _last_enable_schedule_time) are sufficient to keep the
        Buffer-Stepper's own enable→step→disable ordering correct
        and preserve the P7-56 'Timer too close' fix. The real
        toolhead-anchor for the first step lives in _submit_single_-
        trapezoid (forced_t0 / th_time path), which still uses
        toolhead.get_last_move_time() once per chunk-stream — but
        only in the gap/first-chunk path, not for every enable.
        """
        mcu = self.stepper.get_mcu()
        mcu_now = mcu.estimated_print_time(self.reactor.monotonic())
        pt = max(mcu_now + self.lead_time,
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
        # Nach Disable ist der Stepcompress-Cursor nicht mehr synchron —
        # beim naechsten Re-Enable muss set_position() aufgerufen werden
        # (partieller Reprime ohne flush_step_generation). Flag VOR dem
        # early-return setzen damit es auch ohne stepper_enable wirkt.
        self._stepcompress_primed = False
        if self._stepper_enable is None:
            return
        try:
            pt = self._schedule_time_for_enable_toggle()
            self._stepper_enable.motor_disable(pt)
        except Exception:
            logging.exception("buffer_feeder: disable_stepper failed")

    def _submit_move(self, signed_distance, speed, forced_t0=None,
                     streaming=False, submit_chunk_cap=None):
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

        streaming (P7-66): set by _on_mcu_flush lookahead-submits when
        a move is still in-flight. Skips _enable_stepper() (motor is
        already energised from the in-flight chunk) and removes the
        _last_enable_schedule_time floor on t0. Without this, the
        enable-floor pushes the streaming-anchor forward by lead_time
        → inter-chunk gap reopens.

        submit_chunk_cap (P7-66b): caps the size of the FIRST submitted
        trapezoid for hardware-safe HALL-interrupt latency. The full
        signed_distance is honoured — anything beyond the cap is queued
        into _pending_remaining_mm and streamed by _tick_pending_chunk,
        which re-checks HALL2/HALL1 between sub-chunks. Default None
        falls back to max_move_chunk_mm (legacy behaviour).
        """
        if signed_distance == 0 or speed <= 0:
            return
        # P7-69 (Issue #18): defense-in-depth sync guard. _bang_bang_tick
        # and _on_mcu_flush already guard, but _submit_move is reachable
        # via several other call-sites (LOAD/UNLOAD phases, manual cmds,
        # _tick_grip_completion). None of those should fire while a
        # macro-driven SYNC has the stepper bound to the extruder trapq
        # — submitting on own_trapq during that window would queue moves
        # on the wrong trapq AND can trip the gap>5s reprime in
        # _submit_single_trapezoid → toolhead.flush_step_generation()
        # mid-print → extruder stops.
        if self._stepper_synced_to is not None:
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
        # P7-66 R1: only apply this floor when NOT streaming. In the
        # streaming-lookahead path the stepper is already enabled and
        # _last_enable_schedule_time is stale — pushing _last_move_end_-
        # time forward would break the abuttend-anchor.
        if not streaming:
            self._last_move_end_time = max(self._last_move_end_time,
                                           self._last_enable_schedule_time)

        distance_abs = abs(signed_distance)
        direction = 1.0 if signed_distance > 0 else -1.0

        # P7-66b: hardware-safe sub-chunking. submit_chunk_cap (typ.
        # interrupt_chunk_mm=9) limits the first trapezoid to a size
        # that lets HALL2 abort within one sub-chunk's duration. The
        # rest streams via _pending_remaining_mm with per-sub-chunk
        # HALL re-checks in _tick_pending_chunk. Falls back to
        # max_move_chunk_mm when caller does not request sub-chunking.
        chunk_cap = submit_chunk_cap if submit_chunk_cap is not None \
            else self.max_move_chunk_mm
        if chunk_cap > self.max_move_chunk_mm:
            chunk_cap = self.max_move_chunk_mm
        first_chunk = min(distance_abs, chunk_cap)
        self._submit_single_trapezoid(direction * first_chunk, speed,
                                       forced_t0=forced_t0,
                                       streaming=streaming)
        remaining = distance_abs - first_chunk
        if remaining > 0:
            self._pending_remaining_mm = remaining
            self._pending_direction = direction
            self._pending_speed = speed
            # P7-66b: propagate the cap so _tick_pending_chunk uses
            # the same sub-chunk size for the streaming continuation.
            self._pending_submit_chunk_cap = chunk_cap

    def _submit_single_trapezoid(self, signed_distance, speed,
                                  forced_t0=None, streaming=False):
        """Append one trapezoid to our trapq. Low-level primitive.

        forced_t0 (P7-52): when not None, overrides the t0 anchor with
        the explicit value. Used by the flush-callback bang-bang path
        which receives step_gen_time from Klipper and can compute a
        race-free anchor at step_gen_time + lead_time. The default
        (None) keeps the existing toolhead-anchor logic for the
        legacy reactor-tick path.

        streaming (P7-66 R1): set by lookahead-submits during a still-
        in-flight previous chunk. Suppresses _enable_stepper() (motor
        is already on) and drops the _last_enable_schedule_time floor
        from the t0 max(). Without this, _enable_stepper() bumps
        _last_enable_schedule_time forward by lead_time → en becomes
        the binding floor → abuttend-anchor breaks → inter-chunk gap
        regrows to lead_time despite the lookahead path firing.
        """
        # P7-69 (Issue #18): innermost defense-in-depth sync guard.
        # _bang_bang_tick / _on_mcu_flush / _submit_move already guard,
        # but this primitive is the actual site of the dangerous side-
        # effects flagged in Issue #18: when forced_t0 is None AND
        # gap > REPRIME_GAP, the legacy reprime path calls
        # toolhead.flush_step_generation() + stepper.set_position(0)
        # mid-print, which drains the toolhead queue and stops the
        # extruder while it is actively driving the synced stepper.
        # _tick_pending_chunk (P7-66 streaming) and other future call-
        # sites would bypass the upstream guards; this final gate makes
        # the invariant "no own-trapq submit while synced" robust.
        if self._stepper_synced_to is not None:
            return
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
        # Reprime-Logik unterscheidet zwei Pfade:
        #
        # Alter Pfad (forced_t0=None, Reactor-Tick):
        #   flush_step_generation() + set_position() bei not-primed ODER gap>5s.
        #
        # Flush-Callback-Pfad (forced_t0 gesetzt):
        #   - flush_step_generation() NIEMALS aufrufen (ReactorError, weil
        #     reactor.pause() innerhalb von assert_no_pause verboten ist).
        #   - set_position() NUR wenn not-primed (d.h. nach Stepper-Disable,
        #     z.B. nach OVERFLOW). Einmaliger Cursor-Reset ist sicher und
        #     noetig. Bei primed=True: kein Aufruf (Schutz gegen rapide
        #     SET_VELOCITY_LIMIT-Flush-Callbacks die Cursor korrumpieren).
        #   - Gap-basierter Reprime entfaellt: step_gen_time ist der Anker.
        # P7-67: snapshot the primed-flag BEFORE the reprime block
        # rewrites it. The en-floor decision further down depends on
        # whether the stepcompress cursor was primed on ENTRY, not on
        # whether the reprime path just set it True. Without this
        # snapshot, the post-reprime read of _stepcompress_primed
        # would always be True and the new en-floor branch would
        # never trigger — defeating the point of the patch.
        was_primed = self._stepcompress_primed
        if forced_t0 is None:
            need_reprime = (not self._stepcompress_primed) or (gap > REPRIME_GAP)
        else:
            need_reprime = not self._stepcompress_primed
        if need_reprime:
            if forced_t0 is None:
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

        # P7-66 R1: skip _enable_stepper() in streaming-lookahead path.
        # The previous (still in-flight) chunk has already enabled the
        # motor and pushed _last_enable_schedule_time forward by lead_-
        # time. A re-enable here would push it ANOTHER lead_time into
        # the future → en floor wins below → abuttend-anchor broken.
        # Safe to skip: streaming=True only when _move_in_flight() was
        # True on entry, so the motor is energised through end_time of
        # the prior chunk, which equals our t0 floor.
        if not streaming:
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
        # _enable_stepper() oben hat _last_enable_schedule_time via
        # _schedule_time_for_enable_toggle() weiter in die Zukunft
        # geschoben. t0 muss in ALLEN Pfaden >= diesem neuen Wert sein,
        # damit der Motor-Enable vor dem ersten Step feuert.
        # Ohne diesen Floor: t0 = _last_move_end_time < enable_pt →
        # enable NACH Steps → "Invalid sequence" MCU-Shutdown (Hardware
        # 2026-04-29, c=22 i=0).
        # P7-66 R1: when streaming, en floor is DROPPED — see comment
        # at the _enable_stepper() guard above.
        # P7-67 (post-overflow resume primed=False edge case): the R1
        # drop-en-floor optimisation is only safe when the stepcompress
        # cursor was already primed on entry. If was_primed=False
        # (typical resume path: _disable_stepper → _enable_stepper
        # zyklus cleared _stepcompress_primed), the reprime above just
        # rewrote stepcompress with set_position((0,0,0)) and the
        # regular en floor must keep the lead_time margin — otherwise
        # t0 lands at _last_move_end_time with no enable-vs-step lead
        # → MCU "stepcompress Invalid sequence" shutdown (Issue #29,
        # LOAD_PHASE_1 post-OVERFLOW regression of P7-66).
        #
        # P7-71 (Issue #29 Eifel-Joe Update — AUTO-Rapid-Cycle):
        # P7-67 only covers the SLOW-cycle path where _disable_stepper
        # actually ran and flipped _stepcompress_primed=False between
        # OVERFLOW and resume. The RAPID-cycle bypasses that: HALL1
        # flickers so fast that _schedule_stepper_disable only ever
        # sets _pending_disable=True (move-in-flight), and the
        # subsequent _resume_after_overflow → _enable_stepper cancels
        # _pending_disable=False (line 2697). _disable_stepper NEVER
        # runs → _stepcompress_primed stays True over the entire
        # cycle → was_primed=True on the next submit. Same scenario
        # via a different path: any forced_t0=None submit after
        # gap > REPRIME_GAP (5s) with primed=True triggers the
        # reprime branch (need_reprime=True). In BOTH cases the
        # reprime block has just called stepper.set_position((0,0,0))
        # — which makes _last_move_end_time semantically dead
        # (cursor reset to zero, old end_time no longer consistent
        # with the new stepcompress base). Without en-floor t0
        # would land at the stale _last_move_end_time, again with
        # no enable-vs-step lead → "Invalid sequence" shutdown
        # (rapid-cycle reproduction from Eifel-Joe hardware log).
        #
        # Third guard `not need_reprime`: en-floor is dropped ONLY if
        # the cursor was primed on entry AND the reprime block did
        # not just rewrite it. When need_reprime=True the reprime ran
        # → en-floor is mandatory, even if was_primed=True.
        #
        # P7-72 (Issue #29 Eifel-Joe — defense-in-depth stale-anchor
        # guard): the three existing guards above
        # (`streaming` + `was_primed` + `not need_reprime`) all check
        # state at submit-entry. They do NOT see whether
        # `_last_move_end_time` itself is still a meaningful anchor.
        # _enter_overflow → _halt_motion mid-flight does not touch
        # `_last_move_end_time` — it stays parked at the in-flight
        # chunk's end_time. If the rapid-cycle then drains that chunk
        # without queuing a successor (HALL1 flicker, HALT, JAM
        # recovery, etc.) and wall-clock advances past it, the next
        # streaming-lookahead submit would carry a `_last_move_end_-
        # time` that lies in the past relative to mcu_now. The
        # `forced_t0`-branch below already floors against mcu_now,
        # but the legacy `forced_t0=None + streaming + was_primed +
        # gap <= REPRIME_GAP` corner combines a stale anchor with the
        # dropped en-floor — t0 lands at the stale `_last_move_end_-
        # time`, no enable-vs-step lead, MCU "Invalid sequence"
        # shutdown (Eifel-Joe 22-cycle AUTO-Bang-Bang reproduction,
        # follow-up to P7-71 which only covered the gap>5s reprime
        # corner). Marker `mcu_now >= _last_move_end_time` catches
        # exactly this dead-anchor case — when a real streaming
        # chunk is in flight, `_last_move_end_time` is strictly in
        # the future (chunk end_time > mcu_now) so the guard is
        # inert. Perf-impact: zero on healthy streaming, the guard
        # only triggers when the abuttment anchor is already dead.
        stale_anchor = (self._last_move_end_time <= mcu_now)
        en = (0.0
              if (streaming and was_primed and not need_reprime
                  and not stale_anchor)
              else self._last_enable_schedule_time)
        if forced_t0 is not None:
            # P7-52 flush-callback path: caller provides a step_gen_
            # time-based anchor that is race-free against Klipper's
            # MCU flush cycle. Honor _last_move_end_time as the floor
            # so streaming chunks still abut without gap.
            # P7-66 R1b: mcu_now as additional floor — if a previous
            # streamed move ended slightly before step_gen_time (clock
            # drift, queue underrun race), forced_t0=_last_move_end_-
            # time would land in the past → "Timer too close". mcu_now
            # is the safe rebase anchor for that degenerate case.
            #
            # P7-73 (Issue #31): defensive clamp gegen far-future
            # forced_t0. motion_queuing.flush_all_steps() kann beim
            # Print-Start einen `step_gen_time = need_step_gen_time`
            # (= Toolhead-Queue-Ende, 60-100s weit in der Zukunft) an
            # die Flush-Callbacks durchreichen. `_on_mcu_flush` baut
            # daraus `anchor = step_gen_time + lead_time` und reicht
            # das als forced_t0 weiter; im max() unten dominiert es
            # _last_move_end_time/en/mcu_now. last_step_clock im
            # Stepcompress (aus Boot-Anchor, ~7s alt) bleibt zurück,
            # queue_step-Intervall wächst auf 60-100s und überschreitet
            # int32 signed (44.7s @ 48 MHz signed) bzw. uint32 (89.5s)
            # → MCU "Timer too close"-Shutdown. Hardware-Reproduktion
            # Eifel-Joe 2026-05-12: P7-70 interval=48.76s, P7-71
            # interval=86.8s — beides far-future-anchor-Crashes vom
            # Print-Start mit print_time-Spitze 59-100s.
            #
            # Cap auf mcu_now + MAX_FORCED_T0_LOOKAHEAD: im gesunden
            # flush-callback-Betrieb ist step_gen_time ≈ mcu_now +
            # ~0.25s, anchor ≈ mcu_now + 0.55s — also weit unterhalb
            # des Cap. Greift NUR im degenerate far-future Print-Start
            # Fall. Fallback-Anker = mcu_now + lead_time entspricht
            # dem normalen "Erst-Chunk"-Anker (else-Branch unten).
            MAX_FORCED_T0_LOOKAHEAD = 2.0  # s
            if forced_t0 > mcu_now + MAX_FORCED_T0_LOOKAHEAD:
                logging.warning(
                    "buffer_feeder: forced_t0 clamped — was %.2fs "
                    "ahead of mcu_now (P7-73 guard, Issue #31 "
                    "far-future flush)", forced_t0 - mcu_now)
                forced_t0 = mcu_now + self.lead_time
            t0 = max(forced_t0, self._last_move_end_time, en, mcu_now)
        elif self._last_move_end_time > mcu_now + self.lead_time:
            # Streaming: previous chunk is still in the future — abut.
            t0 = max(self._last_move_end_time, en)
        else:
            # First chunk / gap: anchor to toolhead print_time.
            toolhead = self.printer.lookup_object('toolhead')
            th_time = toolhead.get_last_move_time()
            t0 = max(th_time + self.lead_time, self._last_move_end_time, en)
            # P7-77 B (Issue #32 Crash unter P7-76, Eifel-Joe 2026-05-12
            # klippy.log "(2).txt"): wenn th_time strukturell weit voraus
            # ist (aktiver Print mit gefuellter Toolhead-Queue), MUSS
            # der Submit SKIP statt CLAMP. Begruendung:
            #   - P7-76 A clampte t0 auf mcu_now + lead_time
            #   - Aber stepcompress.last_step_clock wurde durch einen
            #     vorigen Submit/Anchor bereits weiter vorgerueckt (z.B.
            #     ein legitimer Watchdog-Anchor auf 551.18s)
            #   - Geclampte t0 ~ mcu_now + 0.3 = 551.13s < last_step_clock
            #     -> interval = -10.4ms -> stepcompress-Crash i=-500471
            # Einen Step bei `mcu_now + lead_time` zu submitten waehrend
            # `last_step_clock` schon weiter vorgerueckt ist MUSS zu
            # negativem interval fuehren. Sicheres Verhalten: nicht
            # submitten. Setze `_last_idle_anchor_time = mcu_now` damit
            # der Watchdog-Rate-Limit weiterhin greift; der naechste
            # _on_mcu_flush (forced_t0!=None Pfad) liefert ohnehin einen
            # race-freien step_gen_time-Anchor und uebernimmt die
            # Cursor-Pflege.
            # Komplementaer zu P7-77 A (Watchdog-Print-Block im _main_-
            # tick) — wenn Patch A wegen einer Race / paused / einer
            # nicht-printing-State greift, faengt B den degenerate Submit
            # noch in _submit_single_trapezoid ab.
            MAX_T0_LOOKAHEAD = 2.0  # s
            if t0 > mcu_now + MAX_T0_LOOKAHEAD:
                logging.warning(
                    "buffer_feeder: anchor skipped — th_time %.2fs "
                    "ahead, would corrupt last_step_clock (P7-77 B; "
                    "th_time=%.3f lme=%.3f en=%.3f mcu_now=%.3f)",
                    t0 - mcu_now, th_time, self._last_move_end_time,
                    en, mcu_now)
                # Rate-limit watchdog so the skip doesn't spin once per
                # tick — analog zum erfolgreichen Anchor-Submit, der
                # `_last_idle_anchor_time = mcu_now` setzt.
                self._last_idle_anchor_time = mcu_now
                # _last_move_end_time mitziehen falls stale-future,
                # damit nachfolgende Submits nicht erneut den ELSE-
                # Branch durch falschen abut-Check verfehlen.
                if self._last_move_end_time > mcu_now + MAX_T0_LOOKAHEAD:
                    self._last_move_end_time = mcu_now
                return

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
        # P7-63: Reset accumulator on every halt. After a halt the
        # accumulator is stale — leaving it set would cause a false
        # JAM_SAFETY_DISTANCE on the very first chunk of the next
        # session if _on_mcu_flush hasn't yet reset it (it does at
        # session start, but defense in depth covers all stop paths
        # including JAM/RUNOUT/PAUSE/CLEAR_JAM).
        self._feed_distance_accumulator = 0.0
        self._auto_between_since = None
        self._pending_remaining_mm = 0.0
        # P7-66b: drop the streaming sub-chunk cap so a subsequent
        # LOAD/UNLOAD/MANUAL pending-stream uses its own (max_move_-
        # chunk_mm) sizing without inheriting a stale AUTO cap.
        self._pending_submit_chunk_cap = None
        self._feed_deadline_time = None
        # P7-74 (Issue #29 Eifel-Joe Hypothese 2026-05-12): clamp
        # `_last_move_end_time` to mcu_now when it sits in the
        # future. _halt_motion in the AUTO-Streaming-Cycling-Pfad
        # is called mid-flight (HALL1 fires bei ~9mm gefahren in
        # einem 45mm-Chunk → _enter_overflow → _halt_motion). Pre-
        # P7-74 ließ das `_last_move_end_time` auf dem geplanten
        # Chunk-Ende stehen (mcu_now + 0.55s), obwohl der Stepper
        # tatsächlich nur 9mm gefahren ist. Der NÄCHSTE Streaming-
        # Submit (nach HALL1-Bounce-Clear, _stepcompress_primed
        # bleibt True) ankert dann via `t0 = max(forced_t0,
        # _last_move_end_time, en, mcu_now)` auf dieser Fake-Future
        # — stepcompress.last_step_clock ist aber auf dem echten
        # letzten Step (innerhalb der ersten 9mm). MCU sieht
        # last_step_clock → t0-Sprung als inkonsistent → "Invalid
        # sequence c=29" Shutdown (Eifel-Joe Hardware-Log 21:52
        # UTC, Issue #29 Kommentar).
        #
        # P7-72 stale_anchor=(_last_move_end_time <= mcu_now) fängt
        # NUR den Past-Anchor-Fall. Hier liegt der Anker in der
        # falschen ZUKUNFT, P7-72 würde stale_anchor=False sagen
        # und en-Floor droppen → Fake-Future wird abuttment-Anker.
        #
        # Clamp ist einseitig: nur `> mcu_now` wird auf mcu_now
        # heruntergesetzt. Damit ist die tatsächliche Halt-Position
        # konsistent reflektiert, und P7-72 stale_anchor wird beim
        # nächsten Submit True (weil `<=`) → en-Floor aktiv → safe.
        mcu = self.stepper.get_mcu()
        mcu_now = mcu.estimated_print_time(self.reactor.monotonic())
        if self._last_move_end_time > mcu_now:
            self._last_move_end_time = mcu_now

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
            self._hall3_drop_since = None
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

    def _maybe_auto_load(self):
        """If auto_load_after_follow=1 + hotend warm, schedule
        LOAD_FILAMENT via deferred timer. Otherwise log skip-message
        with actual/min temp. Called from grip/follow completion
        and from _resume_after_overflow."""
        if not self.auto_load_after_follow:
            return
        if self._hotend_warm():
            self._schedule_gcode_script("LOAD_FILAMENT")
        else:
            self._respond(
                "Auto-Load übersprungen: Hotend zu kalt"
                " (%.0f/%.0f °C)" % (self._hotend_temp(), self.min_temp))

    def _full_reset_to_idle(self, *, label,
                            full=False,
                            sticky_auto_off=False,
                            preserve_lockout=False):
        """Common cleanup pattern shared by HALT / AUTO_OFF / STOP_BUFFER_FILL.

        Always done: unsync (with caller-prefixed respond), halt motion,
        clear runout-follow + cooldown + post_load_grace, set
        _halt_requested so any pending WAIT_IDLE in a macro propagates
        the abort.

        label: prefix for the unsync respond message ("HALT", "AUTO_OFF" ...)
        full: AUTO_OFF/STOP-style — also clears _clear_recovery_flags,
              measure flags, _pending_remaining_mm, _bang_bang_suspended;
              calls _try_restore_gcode_state at end (E-mode-recovery for
              failed LOAD/UNLOAD).
        sticky_auto_off: sets _auto_off_by_user=True so a later auto-engage
              hook stays blocked until the operator explicitly re-enables.
        preserve_lockout: HALT-style — skip the STATE_IDLE transition if
              state is OVERFLOW/JAM (safety lockout supersedes user halt).
        """
        if self._unsync_if_synced():
            self._respond(label + " — also unsynced from extruder")
        self._continuous_feed = False
        self._halt_motion()
        if full:
            self._pending_remaining_mm = 0.0
            self._clear_recovery_flags()
        self._runout_follow_active = False
        self._runout_filament_ref = None
        if full:
            self._measure_load_active = False
            self._measure_feeding = False
        self._cooldown_deadline = None
        if full:
            self._bang_bang_suspended = False
        if sticky_auto_off:
            self._auto_off_by_user = True
        self._runout_recovery_pending = False
        self._halt_requested = True
        self._post_load_overflow_grace = False
        if preserve_lockout and self._state in (STATE_OVERFLOW, STATE_JAM):
            return
        self._set_state(STATE_IDLE)
        if full:
            self._try_restore_gcode_state(from_command=True)

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
        # preserve_lockout=True keeps OVERFLOW/JAM intact (safety
        # supersedes user halt), no full-reset (no recovery-flag clear,
        # no E-mode-restore — operator may want to inspect state).
        self._full_reset_to_idle(label="HALT", preserve_lockout=True)
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
        # fired :printing again). sticky_auto_off=True blocks
        # reinsert auto-grip until an explicit BUFFER_AUTO_ON.
        self._full_reset_to_idle(label="AUTO_OFF",
                                 full=True,
                                 sticky_auto_off=True)
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
                self._raise_if_jam()
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
        self._raise_if_jam()
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
        self._raise_if_jam()
        if not overflow_ok:
            self._raise_if_locked_out(gcmd)

    # P7-23: cmd_BUFFER_UNLOAD_PHASE1 und cmd_BUFFER_UNLOAD_PHASE2 entfernt.
    # P7-20 hat das UNLOAD_FILAMENT-Macro auf SYNC_TO_EXTRUDER umgestellt —
    # Tip-Forming und parallele sync-distance laufen jetzt im Macro selbst
    # via G1 E waehrend BUFFER_SYNC_TO_EXTRUDER aktiv ist.

    cmd_BUFFER_UNLOAD_FILAMENT_help = "UNLOAD_FILAMENT als Python-Workflow mit garantiertem Cleanup"
    def cmd_BUFFER_UNLOAD_FILAMENT(self, gcmd):
        tip_cycles = gcmd.get_int('TIP_CYCLES', 6, minval=0)
        tip_push = gcmd.get_float('TIP_PUSH', 8.0, above=0.)
        tip_pull = gcmd.get_float('TIP_PULL', 14.0, above=0.)
        tip_speed = gcmd.get_float('TIP_SPEED', 20.0, above=0.)
        tip_final_retract = gcmd.get_float('TIP_FINAL_RETRACT', 50.0, above=0.)
        tip_final_speed = gcmd.get_float('TIP_FINAL_SPEED', 50.0, above=0.)
        use_cooling_move = gcmd.get_int('USE_COOLING_MOVE', 1, minval=0, maxval=1)
        cool_temp = gcmd.get_float('COOL_TEMP', 150.0, above=0.)
        cool_temp_max = gcmd.get_float('COOL_TEMP_MAX', cool_temp + 10.0, above=cool_temp)
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
                # P7-64: Tip-Forming in zwei Phasen, dazwischen optional
                # Cooling-Move (M104 + TEMPERATURE_WAIT). Cooling haertet
                # die Filament-Spitze, damit sie sauber durch die Haupt-
                # extruder-Zaehne (BMG/Sherpa/Orbiter) passt und der
                # Buffer-Stepper das Ende anschliessend frei aus dem
                # Bowden ziehen kann.
                pre_cool_moves = []
                for _ in range(tip_cycles):
                    pre_cool_moves.append("G1 E%g F%d" % (tip_push, tip_speed_f))
                    pre_cool_moves.append("G1 E-%g F%d" % (tip_pull, tip_speed_f))
                if pre_cool_moves:
                    self._gcode_run_script_checked("\n".join(pre_cool_moves),
                                                    from_command=True)

                if use_cooling_move:
                    self._gcode_run_script_checked(
                        "M118 UNLOAD Cooling-Move: heize runter auf %d C\n"
                        "M104 S%d\n"
                        "TEMPERATURE_WAIT SENSOR=%s MAXIMUM=%d"
                        % (int(cool_temp), int(cool_temp), extruder_name, int(cool_temp_max)),
                        from_command=True)

                post_cool_moves = []
                post_cool_moves.append("G1 E-%g F%d" % (tip_final_retract,
                                                       int(tip_final_speed * 60)))
                post_cool_moves.append("G1 E-%g F%d" % (sync_dist, fast_spd_f))
                post_cool_moves.append("M400")
                self._gcode_run_script_checked("\n".join(post_cool_moves),
                                                from_command=True)
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
        nominal_chunk = self.max_move_chunk_mm
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
        # (klippy/kinematics/extruder.py: ExtruderStepper.sync_to_
        # extruder).
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
        # stale HALT flag AND the AUTO_OFF-by-user flag. Initial-grip
        # itself drops to STATE_IDLE on completion (or stays in
        # INITIAL_GRIP for the optional follow-feed if grip_follow_
        # distance > 0); STATE_AUTO is then engaged automatically by
        # the auto-engage hooks (auto_engage_on_print_start /
        # on_entrance_insert) — but ONLY if _auto_off_by_user is
        # cleared. Without that clear, BUFFER_AUTO_OFF →
        # FORCE_BUFFER_FILL would grip 10s and then stay IDLE
        # forever, the "fill" part never running.
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
        # Like BUFFER_AUTO_OFF (full reset, sticky auto-off), but
        # additionally clears phase-specific scratch state for the
        # active grip / follow / phase3 workflow that AUTO_OFF leaves
        # alone. STOP_BUFFER_FILL is "abort the current fill cycle".
        self._full_reset_to_idle(label="STOP_BUFFER_FILL",
                                 full=True,
                                 sticky_auto_off=True)
        # Phase-spezifische Cleanup-Flags NACH dem Helper, weil
        # _unsync_if_synced (im Helper) ueber _exit_overflow →
        # _resume_after_overflow im Edge-Case _grip_follow_active=True
        # setzen kann. Inverse Reihenfolge wuerde das wieder ueber-
        # schreiben (verifiziert von Sonnet/Codex Phase D Review).
        self._initial_grip_end_time = None
        self._grip_follow_active = False
        self._load_phase3_distance = 0.0
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

    # ---- P7-68: runtime parameter tuning (Issue #28) ----
    # Hot-swap setter for the five hardware-discovery parameters. All
    # writes are picked up by the next read on the streaming/auto path
    # (no Klipper restart). NO persistence: the operator manually
    # transfers a confirmed value to lll.cfg.
    cmd_BUFFER_SET_help = (
        "Live-tune buffer parameters without restart. All args optional:\n"
        "  CHUNK_MM              flush_callback_chunk_mm (mm,  default 15, lll.cfg 45)\n"
        "  SPEED                 feed_speed              (mm/s, default 30, lll.cfg 70)\n"
        "  INTERRUPT_CHUNK_MM    interrupt_chunk_mm      (mm,  default 9, cap <= MAX_MOVE_CHUNK_MM)\n"
        "  LEAD_TIME             lead_time               (s,   default 0.3, lll.cfg 0.12; warn outside 0.05..1.0)\n"
        "  MAX_MOVE_CHUNK_MM     max_move_chunk_mm       (mm,  default 50)\n"
        "Without args: prints current values. No persistence — copy "
        "the final value into lll.cfg manually."
    )
    def cmd_BUFFER_SET(self, gcmd):
        # All args optional. above=0. ensures only positive values.
        new_chunk      = gcmd.get_float('CHUNK_MM',           None, above=0.)
        new_speed      = gcmd.get_float('SPEED',              None, above=0.)
        new_interrupt  = gcmd.get_float('INTERRUPT_CHUNK_MM', None, above=0.)
        new_lead       = gcmd.get_float('LEAD_TIME',          None, above=0.)
        new_max_move   = gcmd.get_float('MAX_MOVE_CHUNK_MM',  None, above=0.)

        gc = self.printer.lookup_object('gcode')
        changed = False

        # Apply MAX_MOVE_CHUNK_MM first so INTERRUPT_CHUNK_MM cap below
        # sees the new ceiling (operator can raise both in one call).
        if new_max_move is not None:
            old = self.max_move_chunk_mm
            self.max_move_chunk_mm = new_max_move
            gc.respond_info("BUFFER_SET: max_move_chunk_mm  %.3f -> %.3f mm"
                            % (old, new_max_move))
            # Existing interrupt-chunk might now violate the new cap;
            # never raised, only lowered (mirrors __init__ cap-on-init).
            if self.interrupt_chunk_mm > self.max_move_chunk_mm:
                old_ic = self.interrupt_chunk_mm
                self.interrupt_chunk_mm = self.max_move_chunk_mm
                gc.respond_info(
                    "BUFFER_SET: interrupt_chunk_mm capped %.3f -> %.3f mm "
                    "(<= max_move_chunk_mm=%.3f)"
                    % (old_ic, self.interrupt_chunk_mm,
                       self.max_move_chunk_mm))
            changed = True

        if new_interrupt is not None:
            old = self.interrupt_chunk_mm
            # Cap at max_move_chunk_mm — mirrors the init-time check at
            # buffer_feeder.py:826 (P7-66b). We CAP (not raise) so the
            # operator can issue a single command without micromanaging
            # ordering against MAX_MOVE_CHUNK_MM.
            capped = new_interrupt
            if capped > self.max_move_chunk_mm:
                gc.respond_info(
                    "BUFFER_SET: INTERRUPT_CHUNK_MM=%.3f exceeds "
                    "max_move_chunk_mm=%.3f — capping"
                    % (new_interrupt, self.max_move_chunk_mm))
                capped = self.max_move_chunk_mm
            self.interrupt_chunk_mm = capped
            gc.respond_info("BUFFER_SET: interrupt_chunk_mm  %.3f -> %.3f mm"
                            % (old, capped))
            changed = True

        if new_chunk is not None:
            old = self.flush_callback_chunk_mm
            self.flush_callback_chunk_mm = new_chunk
            gc.respond_info(
                "BUFFER_SET: flush_callback_chunk_mm  %.3f -> %.3f mm"
                % (old, new_chunk))
            changed = True

        if new_speed is not None:
            old = self.feed_speed
            self.feed_speed = new_speed
            gc.respond_info("BUFFER_SET: feed_speed  %.3f -> %.3f mm/s"
                            % (old, new_speed))
            changed = True

        if new_lead is not None:
            old = self.lead_time
            self.lead_time = new_lead
            gc.respond_info("BUFFER_SET: lead_time  %.4f -> %.4f s"
                            % (old, new_lead))
            if new_lead > 1.0 or new_lead < 0.05:
                gc.respond_info(
                    "BUFFER_SET: WARNING lead_time=%.4f outside typical "
                    "hardware range 0.05..1.0 s — proceed with caution"
                    % new_lead)
            changed = True

        if not changed:
            # No-op: dump current values so the operator can read the
            # live picture without a separate command.
            gc.respond_info("BUFFER_SET: no args — current values:")
            gc.respond_info("  flush_callback_chunk_mm = %.3f mm"
                            % self.flush_callback_chunk_mm)
            gc.respond_info("  feed_speed              = %.3f mm/s"
                            % self.feed_speed)
            gc.respond_info("  interrupt_chunk_mm      = %.3f mm"
                            % self.interrupt_chunk_mm)
            gc.respond_info("  lead_time               = %.4f s"
                            % self.lead_time)
            gc.respond_info("  max_move_chunk_mm       = %.3f mm"
                            % self.max_move_chunk_mm)

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

    def _raise_if_jam(self):
        """Hard JAM lockout: raise gcmd_error if state==JAM or _jam_active.
        Used by entry-checks that allow OVERFLOW (UNLOAD-recovery,
        LOAD_PHASE_3 with OVERFLOW_OK) but never JAM."""
        if self._state == STATE_JAM or self._jam_active:
            raise self._cmd_error(
                "BufferFeeder: JAM active — aborting. "
                "Use BUFFER_CLEAR_JAM after inspection. (UNLOAD is allowed.)")

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
            self._raise_if_jam()

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
            'use_fault_overlay':        self.use_fault_overlay,
            'accel':                    self.accel,
            'max_move_chunk_mm':        self.max_move_chunk_mm,
            'flush_callback_chunk_mm':  self.flush_callback_chunk_mm,
            'interrupt_chunk_mm':       self.interrupt_chunk_mm,
        }


# ---------------------------------------------------------------------------
# Config hook
# ---------------------------------------------------------------------------

def load_config_prefix(config):
    return BufferFeeder(config)
