import logging
from contextlib import contextmanager
from enum import Enum

from ._buffer_common import STATE_AUTO, STATE_IDLE, STATE_JAM, STATE_OVERFLOW


class CleanupPolicy(Enum):
    STRICT = 'strict'
    BEST_EFFORT = 'best_effort'


class CleanupCoordinator:
    def __init__(self, owner):
        self.owner = owner

    def run_cleanup(self, actions, *, policy):
        """Attempt every cleanup; strict reports the first failure afterwards."""
        first_error = None
        for action in actions:
            try:
                action()
            except Exception as exc:
                logging.exception("buffer_feeder: operation cleanup failed")
                if first_error is None:
                    first_error = exc
        if first_error is not None and policy is CleanupPolicy.STRICT:
            raise first_error
        return first_error is None

    @contextmanager
    def on_exit(self, actions, *, policy):
        try:
            yield
        except BaseException:
            # The original exception remains primary even if cleanup fails.
            self.run_cleanup(actions, policy=CleanupPolicy.BEST_EFFORT)
            raise
        else:
            self.run_cleanup(actions, policy=policy)

    def save_gcode_state(self, from_command=False):
        self.owner._gcode_run_script_checked(
            "SAVE_GCODE_STATE NAME=buffer_feeder_op",
            from_command=from_command)
        self.owner._macro_state_saved = True

    def restore_gcode_state(self, from_command=False):
        owner = self.owner
        if not owner._macro_state_saved:
            return False
        owner._gcode_run_script_checked(
            "RESTORE_GCODE_STATE NAME=buffer_feeder_op MOVE=0",
            from_command=from_command)
        owner._macro_state_saved = False
        return True

    def try_restore_gcode_state(self, from_command=False):
        if not self.owner._macro_state_saved:
            return False
        return self.run_cleanup(
            [lambda: self.restore_gcode_state(from_command=from_command)],
            policy=CleanupPolicy.BEST_EFFORT)

    @contextmanager
    def gcode_state_scope(self, *, policy, adopt_existing=False):
        # Adoption preserves the original E-mode saved by the LOAD macro.
        state_saved = False

        def restore():
            if state_saved:
                self.restore_gcode_state(from_command=True)

        with self.on_exit([restore], policy=policy):
            if not (adopt_existing and self.owner._macro_state_saved):
                self.save_gcode_state(from_command=True)
            state_saved = True
            self.owner._gcode_run_script_checked("M83", from_command=True)
            yield

    @contextmanager
    def sync_scope(self, sync_action=None, *, policy):
        # Install cleanup before SYNC can partially mutate the queue binding.
        with self.on_exit([self.owner._unsync_if_synced], policy=policy):
            if sync_action is not None:
                sync_action()
            yield

    def full_reset_to_idle(self, options):
        owner = self.owner
        if owner._unsync_if_synced():
            owner._respond(options.label + " — also unsynced from extruder")
        owner._continuous_feed = False
        owner._halt_motion()
        if options.full:
            owner.pending_stream.finish()
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
        owner._halt_requested = options.set_halt_requested
        owner._post_load_overflow_grace = False
        if options.preserve_lockout and owner._state in (STATE_OVERFLOW, STATE_JAM):
            return
        owner._set_state(STATE_IDLE)
        if options.full:
            self.try_restore_gcode_state(from_command=True)

    def clear_jam(self):
        owner = self.owner
        # Auch latched _jam_active ohne STATE_JAM akzeptieren (Review
        # 2026-07-09): Abbruch-Pfade (PHASE1-except, UNLOAD-Overshoot,
        # OVERFLOW-Overwrite) koennen state=IDLE bei gesetztem Latch
        # hinterlassen — der dokumentierte Recovery-Befehl muss den
        # Latch trotzdem loesen koennen.
        if owner._state != STATE_JAM and not owner._jam_active:
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
