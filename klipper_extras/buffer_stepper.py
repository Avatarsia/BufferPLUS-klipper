# buffer_stepper.py — Sync-Coordinator for the feeder-stepper trapq.
#
# Owns the binding between the feeder stepper and either its own trapq
# (default, autonomous bang-bang/streaming motion) or an extruder's
# trapq (for explicit SYNC_TO_EXTRUDER macros). Sub-module of
# buffer_feeder; kein Klipper-load_config-Eintrypoint.

from ._buffer_common import (
    ANCHOR_NUDGE_MM, REPRIME_GAP_S, STATE_OVERFLOW,
)


class SyncCoordinator:
    def __init__(self, owner):
        self.owner = owner
        self.printer = owner.printer
        self.reactor = owner.reactor
        self.motion_queuing = None
        self.trapq = None
        self.trapq_append = None

    def setup_trapq(self, config):
        try:
            self.motion_queuing = self.printer.load_object(config, 'motion_queuing')
        except Exception:
            raise config.error(
                "buffer_feeder requires Klipper's motion_queuing module. "
                "Install a recent mainline Klipper build that provides "
                "'motion_queuing' before loading [buffer_feeder].")
        required = (
            'allocate_trapq',
            'lookup_trapq_append',
            'check_step_generation_scan_windows',
            'note_mcu_movequeue_activity',
        )
        missing = [name for name in required
                   if not hasattr(self.motion_queuing, name)]
        if missing:
            raise config.error(
                "buffer_feeder requires a newer motion_queuing API. "
                "Missing: %s. Update Klipper mainline before loading "
                "[buffer_feeder]." % ", ".join(sorted(missing)))
        self.trapq = self.motion_queuing.allocate_trapq()
        self.trapq_append = self.motion_queuing.lookup_trapq_append()

    def _submit_anchor_move(self, *, forced_t0=None):
        """Submit a small anchor-step in the safe direction. Returns
        the direction sign so the caller can format its respond message
        (boot anchor vs. pre-sync REPRIME use different wording but the
        underlying motion + direction-policy is identical).

        Keyword-only `forced_t0` routes the submit through the
        forced_t0!=None branch in _submit_single_trapezoid. Callers in
        the watchdog-print-block-override path must pass `forced_t0=
        mcu_now + lead_time`, so the anchor is actually submitted even
        when the toolhead queue is far-future filled (typical during
        print). Default None preserves the existing behaviour for all
        other callers (boot anchor, pre-sync REPRIME, non-print
        watchdog).
        """
        owner = self.owner
        owner._enable_stepper()
        anchor_dir = -1.0 if owner.hall_overflow else 1.0
        owner._submit_move(anchor_dir * ANCHOR_NUDGE_MM, 10.0,
                           forced_t0=forced_t0)
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
        # Gap-Reprime BEFORE the trapq-swap. The own-trapq path in
        # _submit_single_trapezoid has a gap-reprime check, but
        # sync_to_extruder didn't — so if no buffer-stepper move ran for
        # > CLOCK_DIFF_MAX (~16.7s), the first extruder-step after the
        # swap would land at a print_time-clock far ahead of the stale
        # stepcompress cursor → 'Invalid sequence' crash. Refresh the
        # cursor with a tiny anchor-step (direction follows HALL1 to
        # avoid forward-feed when buffer is overfilled) so the swap
        # finds an up-to-date last_step_clock.
        mcu = owner.stepper.get_mcu()
        mcu_now = mcu.estimated_print_time(owner.reactor.monotonic())
        gap = mcu_now - owner._last_move_end_time
        if gap > REPRIME_GAP_S:
            anchor_dir = self._submit_anchor_move()
            owner._respond("Sync prep: own-trapq cursor refreshed "
                          "(idle %.1fs, anchor %s 0.05mm)"
                          % (gap, "retract" if anchor_dir < 0 else "feed"))
        toolhead = owner.printer.lookup_object('toolhead')
        try:
            toolhead.flush_step_generation()
            # Feeder-Startposition auf die tatsächliche commanded_pos
            # des Extruder-Steppers setzen, nicht auf (0,0,0). Nach LOAD
            # steht der Extruder-Stepper intern bei z.B. 180mm;
            # set_position((0,0,0)) würde den Feeder auf 0 setzen während
            # itersolve Schritte ab 180mm berechnet → alle Schritte landen
            # bei t=0 → {i=0,c=N} Invalid sequence. extruder.last_position
            # ist die physikalische commanded_pos des Extruder-Steppers
            # (unabhängig von G92-Offsets) — selbes Pattern wie Klipper's
            # ExtruderStepper.sync_to_extruder.
            _ext_pos = extruder.last_position
            owner.stepper.set_position((_ext_pos, 0., 0.))
            owner.stepper.set_trapq(extruder.get_trapq())
            self.motion_queuing.check_step_generation_scan_windows()
        except Exception:
            # Best-effort rollback so the finally-cleanup of any caller
            # (e.g. cmd_BUFFER_UNLOAD_FILAMENT) cannot mistake a
            # half-mutated stepper for a clean state. Clear the arming
            # flag last so a recursive failure inside the rollback still
            # leaves the cleanup guard disarmed (unsync_if_synced returns
            # False), avoiding double-rollback attempts.
            try:
                owner.stepper.set_trapq(self.trapq)
                self.motion_queuing.check_step_generation_scan_windows()
            except Exception:
                pass
            self.owner._stepper_synced_to = None
            raise
        self.owner._stepper_synced_to = extruder_name
        owner._stepcompress_primed = True
        owner._enable_stepper()
        owner._respond("Buffer-Feeder synced to '%s' — follows extruder moves"
                      % extruder_name)

    def unsync_if_synced(self):
        if self.owner._stepper_synced_to is None:
            return False
        owner = self.owner
        toolhead = owner.printer.lookup_object('toolhead')
        toolhead.flush_step_generation()
        owner.stepper.set_position((0., 0., 0.))
        owner.stepper.set_trapq(self.trapq)
        self.motion_queuing.check_step_generation_scan_windows()
        self.owner._stepper_synced_to = None
        owner._commanded_pos = 0.0
        mcu = owner.stepper.get_mcu()
        now_pt = mcu.estimated_print_time(owner.reactor.monotonic())
        owner._last_move_end_time = max(owner._last_move_end_time,
                                        now_pt + owner.lead_time)
        # Catch deferred _exit_overflow. If HALL1 fell while we were
        # synced, _exit_overflow short-circuited to avoid stranding the
        # stepper on extruder_trapq during a state-transition. Now that
        # the sync binding is released, run the deferred exit so the
        # state-machine catches up.
        if (owner._state == STATE_OVERFLOW
                and not owner.hall_overflow):
            owner._exit_overflow()
        return True
