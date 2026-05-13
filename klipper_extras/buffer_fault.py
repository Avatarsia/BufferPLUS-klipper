# buffer_fault.py — Fault/Overflow/Jam state-machine for the buffer.
#
# Sub-module of buffer_feeder; kein Klipper-load_config-Eintrypoint.
# State (jam-flags, overflow-resume) lives on the owner BufferFeeder;
# this class holds only logic.

import logging

from ._buffer_common import (
    BUSY_PHASE_STATES,
    STATE_AUTO, STATE_INITIAL_GRIP, STATE_JAM, STATE_LOAD_PHASE_1,
    STATE_LOAD_PHASE_3, STATE_MANUAL_RETRACT, STATE_OVERFLOW,
    STATE_UNLOAD_PHASE_3,
)


class FaultManager:
    def __init__(self, owner):
        self.owner = owner
        self.printer = owner.printer
        self.reactor = owner.reactor

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
            # Fault-overlay: skip re-trigger while overlay flag is set
            # (state stays LOAD_PHASE_3 in overlay mode, but the main_tick
            # poll would otherwise re-enter _enter_overflow on every
            # cycle).
            if owner.use_fault_overlay and owner._fault_overflow:
                return False
            # Post-LOAD HALL1-grace: after Phase 3 exit via stable HALL1
            # the buffer is legitimately full — main_tick must not bounce
            # back to STATE_OVERFLOW. Cleared when HALL1 actually falls
            # (sensor_callback path) or via operator cleanup.
            if owner._post_load_overflow_grace:
                return False
            return True
        if context == 'submit_move':
            return not phase3_overflow_ok
        if context in ('auto_on', 'phase3_entry'):
            return True
        raise ValueError("Unknown HALL1 context: %s" % (context,))

    def clear_recovery_flags(self):
        self.owner._jam_active = False
        self.owner._hall2_start_time = None
        self.owner._hall3_start_time = None
        self.owner._hall3_drop_since = None

    def resume_after_overflow(self):
        owner = self.owner
        # Refuse to promote out of OVERFLOW while SYNC is still active.
        # _exit_overflow already short-circuits, but direct callers
        # (fault-overlay branch, future re-entry) could land here with
        # the sync binding intact. Promotion to STATE_AUTO/STATE_LOAD_-
        # PHASE_1/STATE_INITIAL_GRIP would let bang-bang or
        # _submit_move queue moves to own_trapq while the stepper is on
        # extruder_trapq → moves go live at next unsync, corrupting the
        # stepcompress cursor.
        if owner._stepper_synced_to is not None:
            return
        interrupted = owner._overflow_interrupted_state
        owner._overflow_interrupted_state = None

        if (interrupted == STATE_INITIAL_GRIP
                and owner._overflow_interrupted_follow):
            owner._overflow_interrupted_follow = False
            if owner._overflow_resume_mm > 0:
                owner._grip_follow_active = True
                owner._enable_stepper()
                owner._set_state(STATE_INITIAL_GRIP)
                owner._submit_move(
                    owner._overflow_resume_dir * owner._overflow_resume_mm,
                    owner._overflow_resume_spd)
                owner._overflow_resume_mm = 0.0
                return
            owner._overflow_resume_mm = 0.0
            owner._maybe_auto_load()
            return

        if interrupted == STATE_LOAD_PHASE_1 and owner._overflow_resume_mm > 0:
            owner._enable_stepper()
            owner._set_state(STATE_LOAD_PHASE_1)
            owner._pending_remaining_mm = owner._overflow_resume_mm
            owner._pending_direction = owner._overflow_resume_dir
            owner._pending_speed = owner._overflow_resume_spd
            owner._overflow_resume_mm = 0.0
            return

        # In fault-overlay mode the cmd_BUFFER_LOAD_PHASE3 while-loop is
        # still spinning — keep _state=LOAD_PHASE_3 so the loop continues
        # feeding instead of falling through to STATE_AUTO and silently
        # returning success on an aborted phase 3.
        if (interrupted == STATE_LOAD_PHASE_3
                and owner.use_fault_overlay
                and owner._state == STATE_LOAD_PHASE_3):
            owner._overflow_resume_mm = 0.0
            owner._enable_stepper()
            return

        owner._overflow_resume_mm = 0.0
        if (owner.entrance_detected
                and not owner._auto_off_by_user
                and not owner._bang_bang_suspended
                and not owner._halt_requested):
            # Defensive watchdog-gate-reset at OVERFLOW→AUTO recovery.
            # If _continuous_feed got stuck during OVERFLOW cycling
            # (rapid HALL1-flicker race between _enter_overflow and
            # _on_mcu_flush / _bang_bang_tick), the _main_tick watchdog
            # would not fire for 56s+ even while AUTO has quiescent
            # phases. _enter_overflow + _halt_motion + _set_state(IDLE)
            # should already clear all three flags — this reset is
            # defense-in-depth against unknown race paths. No-op when
            # the flags are already clean. hall_empty/hall_full/
            # _stepper_synced_to are left alone — they are sensor- /
            # architecture-driven, not stuck-flag risks.
            if owner._continuous_feed:
                logging.warning(
                    "buffer_feeder: stuck _continuous_feed cleared "
                    "at OVERFLOW→AUTO transition")
                owner._continuous_feed = False
                owner._continuous_feed_direction = 0
            owner._enable_stepper()
            owner._set_state(STATE_AUTO)

    def check_auto_ready(self, allow_jam=False):
        owner = self.owner
        if self.is_hall1_active('auto_on') or owner._state == STATE_OVERFLOW:
            return "HALL1 overflow active"
        if not allow_jam and (owner._state == STATE_JAM or owner._jam_active):
            return ("JAM active — inspect and call BUFFER_CLEAR_JAM, "
                    "or BUFFER_AUTO_OFF first.")
        if owner._state in BUSY_PHASE_STATES:
            return ("LOAD/UNLOAD in progress (state=%s) — call "
                    "STOP_BUFFER_FILL to abort first." % owner._state)
        if owner._bang_bang_suspended:
            # Heal stale suspend before rejecting. RESUME the print
            # path doesn't fire idle_timeout:ready a second time if a
            # PAUSE was cancelled instead.
            owner._clear_stale_suspend_if_print_inactive(
                owner.reactor.monotonic())
        if owner._bang_bang_suspended:
            return ("print is paused (bang-bang suspended). RESUME the "
                    "print — bang-bang re-engages automatically. If the "
                    "print is already finished, use BUFFER_AUTO_OFF first.")
        return None
