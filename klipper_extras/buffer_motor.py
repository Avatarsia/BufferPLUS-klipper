"""Motor lifecycle and printer-wide disable transactions.

Live motion/motor fields remain on BufferFeeder. These helpers own the
ordering, not a second copy of state or a separate queue.
"""
import logging


class DisableCoordinator:
    def __init__(self, stepper_enable):
        self.stepper_enable = stepper_enable
        # Keep the established private registry/raw-call references visible.
        stepper_enable._buffer_disable_owners = []
        stepper_enable._buffer_set_motors_enable = stepper_enable.set_motors_enable
        stepper_enable.set_motors_enable = self.set_motors_enable

    @classmethod
    def register(cls, stepper_enable, owner):
        coordinator = getattr(stepper_enable, '_buffer_disable_coordinator', None)
        if coordinator is None:
            coordinator = cls(stepper_enable)
            stepper_enable._buffer_disable_coordinator = coordinator
        if owner not in stepper_enable._buffer_disable_owners:
            stepper_enable._buffer_disable_owners.append(owner)
        return coordinator

    def set_motors_enable(self, stepper_names, enable):
        se = self.stepper_enable
        names = list(stepper_names)
        owners = [owner for owner in se._buffer_disable_owners
                  if owner.stepper.get_name() in names]
        if enable or not owners:
            return se._buffer_set_motors_enable(names, enable)
        previous = [(owner, owner._global_disable_in_progress)
                    for owner in owners]
        for owner in owners:
            owner._global_disable_in_progress = True
        try:
            result = se._buffer_set_motors_enable(names, enable)
            for owner in owners:
                # A deliberate global motor-off also cancels unsent work;
                # otherwise a later idle flush can re-enable an old prime.
                owner._halt_motion()
                owner._needs_overflow_prime = False
            return result
        finally:
            for owner, was_disabling in previous:
                owner._global_disable_in_progress = was_disabling


class MotorLifecycle:
    def __init__(self, owner):
        self.owner = owner

    def schedule_toggle_time(self):
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
        owner = self.owner
        mcu = owner.stepper.get_mcu()
        mcu_now = mcu.estimated_print_time(owner.reactor.monotonic())
        pt = max(mcu_now + owner.lead_time,
                 owner._last_move_end_time + owner.lead_time,
                 owner._last_enable_schedule_time + owner.lead_time)
        owner._last_enable_schedule_time = pt
        return pt

    def schedule_disable(self):
        """Disable stepper, deferring to tick if a move is in flight.

        Calling motor_disable while steps are still unprocessed in the
        trapq causes Klipper to register an add_active_callback that
        fires motor_enable(past_time) when the step-generator processes
        those steps.  set_digital(past_time, 1) then causes the MCU to
        raise 'Timer too close'.  Deferring until flight=False lets the
        step-generator finish before motor_disable touches the callbacks.
        """
        owner = self.owner
        if owner._stepper_synced_to is not None:
            # Defense-in-depth (Codex 2026-07-13): der Stepper haengt
            # an der Extruder-Trapq — der Deferral-Guard unten sieht
            # nur own-trapq-Moves (_current_move) und wuerde ein
            # motor_disable mitten in fremde in-flight Steps feuern
            # ("Timer too close"). Kein Disable waehrend Sync; die
            # Unsync-/Cleanup-Pfade disablen danach regulaer.
            return
        if owner._move_in_flight():
            owner._pending_disable = True
        else:
            owner._disable_stepper()

    def enable(self):
        owner = self.owner
        if (getattr(owner, '_stepper_disable_in_progress', False)
                or getattr(owner, '_global_disable_in_progress', False)):
            return
        if owner._stepper_enable is None:
            return
        owner._pending_disable = False   # cancel any deferred disable
        # Neue Aktivitaet: Silent-Idle-Disable-Latch re-armen, damit
        # die naechste Ruhephase wieder genau einmal disabled
        # (idle_anchor_mode='silent').
        owner._silent_idle_disabled = False
        try:
            pt = owner._schedule_time_for_enable_toggle()
            owner._stepper_enable.motor_enable(pt)
        except Exception:
            logging.exception("buffer_feeder: enable_stepper failed")

    def guard_disable(self, print_time):
        owner = self.owner
        if getattr(owner, '_stepper_disable_in_progress', False):
            return
        owner._stepper_disable_in_progress = True
        try:
            move_end = (owner._current_move['end_time']
                        if owner._current_move is not None else 0.0)
            if owner.motion_queuing.last_step_gen_time < move_end:
                # A flush callback can append a prime beyond the snapshot
                # used by Klipper's global flush_all_steps(). Drain once more
                # with every buffer submit path blocked, before rearming
                # the automatic activity callback in motor_disable().
                owner.motion_queuing.flush_all_steps()
                if owner.motion_queuing.last_step_gen_time < move_end:
                    raise owner._cmd_error(
                        'Buffer motor disable: incomplete step generation')
                print_time = max(print_time,
                                 owner._schedule_time_for_enable_toggle())
            owner._last_enable_schedule_time = max(
                owner._last_enable_schedule_time, print_time)
            result = owner._raw_motor_disable(print_time)
            owner._stepcompress_primed = False
            owner._pending_disable = False
            return result
        finally:
            owner._stepper_disable_in_progress = False

    def disable(self):
        owner = self.owner
        # MCU time can pass the move end before a delayed background
        # flush has generated all steps. Disabling then re-arms Klipper's
        # activity callback, which may enable again at the old move start.
        # Use the completed generator cursor, not the next flush target.
        # With no submitted move, lme may only be a startup/recovery anchor.
        move_end = (owner._current_move['end_time']
                    if owner._current_move is not None
                    else 0.0)
        if owner.motion_queuing.last_step_gen_time < move_end:
            owner._pending_disable = True
            return
        owner._pending_disable = False
        # Nach Disable ist der Stepcompress-Cursor nicht mehr synchron —
        # beim naechsten Re-Enable muss set_position() aufgerufen werden
        # (partieller Reprime ohne flush_step_generation). Flag VOR dem
        # early-return setzen damit es auch ohne stepper_enable wirkt.
        owner._stepcompress_primed = False
        if owner._stepper_enable is None:
            return
        try:
            pt = owner._schedule_time_for_enable_toggle()
            owner._stepper_enable.motor_disable(pt)
        except Exception:
            logging.exception("buffer_feeder: disable_stepper failed")

    def install_guard(self, stepper_enable=None):
        owner = self.owner
        handle = owner._stepper_enable
        if getattr(owner, '_guarded_stepper_enable', None) is not handle:
            owner._raw_motor_disable = handle.motor_disable
            owner._guarded_stepper_enable = handle
            handle.motor_disable = owner._guard_motor_disable
        if (stepper_enable is None
                or not callable(getattr(stepper_enable, 'set_motors_enable', None))):
            return
        DisableCoordinator.register(stepper_enable, owner)
