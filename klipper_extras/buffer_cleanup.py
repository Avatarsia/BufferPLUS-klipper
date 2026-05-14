import logging

from ._buffer_common import STATE_AUTO, STATE_IDLE, STATE_JAM, STATE_OVERFLOW


class CleanupCoordinator:
    def __init__(self, owner):
        self.owner = owner

    def try_restore_gcode_state(self, from_command=False):
        owner = self.owner
        if not owner._macro_state_saved:
            return False
        try:
            owner._gcode_run_script(
                "RESTORE_GCODE_STATE NAME=buffer_feeder_op MOVE=0",
                from_command=from_command)
            owner._macro_state_saved = False
            return True
        except Exception:
            logging.exception("buffer_feeder: gcode-state restore failed")
            return False

    def full_reset_to_idle(self, options):
        owner = self.owner
        if owner._unsync_if_synced():
            owner._respond(options.label + " — also unsynced from extruder")
        owner._continuous_feed = False
        owner._halt_motion()
        if options.full:
            owner._pending_remaining_mm = 0.0
            owner._clear_recovery_flags()
        owner._runout_follow_active = False
        owner._runout_filament_ref = None
        if options.full:
            owner._measure_load_active = False
            owner._measure_feeding = False
        owner._cooldown_deadline = None
        if options.full:
            owner._bang_bang_suspended = False
        if options.sticky_auto_off:
            owner._auto_off_by_user = True
        owner._runout_recovery_pending = False
        owner._halt_requested = True
        owner._post_load_overflow_grace = False
        if options.preserve_lockout and owner._state in (STATE_OVERFLOW, STATE_JAM):
            return
        owner._set_state(STATE_IDLE)
        if options.full:
            self.try_restore_gcode_state(from_command=True)

    def clear_jam(self):
        owner = self.owner
        if owner._state != STATE_JAM:
            raise owner._cmd_error("Not in JAM state (state=%s)" % owner._state)
        owner._clear_recovery_flags()
        owner._prepare_post_jam_recovery()
        owner._halt_requested = False
        self.try_restore_gcode_state(from_command=True)
        target = STATE_IDLE
        if owner.entrance_detected:
            block_reason = owner._check_auto_ready(allow_jam=True)
            if block_reason is None and not owner._auto_off_by_user:
                target = STATE_AUTO
            elif block_reason is not None:
                owner._respond("JAM cleared — staying IDLE: " + block_reason)
        owner._set_state(target)
        owner._respond("JAM cleared — state=%s" % owner._state)
