# buffer_sensors.py — HALL/Entrance/Button monitor for the LLL Plus buffer.
#
# Sub-module of buffer_feeder (kein Klipper-load_config-Eintrypoint).

from ._buffer_common import (
    BUTTON_FEED, BUTTON_RETRACT,
    CLICK_DOUBLE, CLICK_SINGLE, CLICK_TRIPLE,
    STATE_AUTO, STATE_IDLE, STATE_INITIAL_GRIP, STATE_JAM,
    STATE_LOAD_PHASE_1, STATE_LOAD_PHASE_3, STATE_MANUAL_FEED,
    STATE_MANUAL_RETRACT, STATE_OVERFLOW, STATE_RUNOUT,
    STATE_UNLOAD_PHASE_3,
)


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
                # In STATE_AUTO defer immediate _enter_overflow to
                # _main_tick (which checks for hall1_persist_timeout).
                # In other states (LOAD, MANUAL, UNLOAD) keep the immediate
                # trigger — those paths have their own safety semantics and
                # need synchronous overflow-handling.
                if owner._state == STATE_AUTO:
                    owner._mark_hall1_active()
                else:
                    owner._enter_overflow()
            else:
                # Clear post-LOAD grace on HALL1-fall. Buffer-Arm has
                # dropped, normal sensor regime resumes.
                owner._post_load_overflow_grace = False
                owner._mark_hall1_cleared()
                owner._exit_overflow()
        elif name == 'hall_full':
            if owner.hall_full:
                # HALL2 rising edge: in continuous-streaming HALL2 is
                # only a speed-modulator input (no state transition),
                # so the accumulator reset must be surfaced here as an
                # explicit edge event. Without it
                # _feed_distance_accumulator grows monotonically over
                # long sessions and trips a false JAM_SAFETY_DISTANCE
                # in legacy paths.
                owner._feed_distance_accumulator = 0.0
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
        # Heal sticky suspended-flag from PAUSE → CANCEL/ERROR path
        # before evaluating the auto-grip guards. Recompute
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
            # full PAUSE duration.
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
