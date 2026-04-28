"""P7-36 fixes verifying the post-review hardening.

Three findings fixed:
1. Codex MEDIUM: SyncCoordinator.sync_to_extruder set _stepper_synced_to
   AFTER trapq mutations — finally-cleanup skipped on mid-mutation raise.
2. Reviewer #2 F5: resume_after_overflow had no LOAD_PHASE_3 branch in
   overlay mode → silent success on aborted phase 3.
3. Reviewer #2 F2: stale _fault_overflow=True if state was bypassed to
   IDLE via STOP_BUFFER_FILL / BUFFER_HALT before HALL1 fall-edge.
"""

from fakes_klipper import FakeConfig, FakePrinter
from klipper_extras import buffer_feeder


def make_feeder(values=None):
    printer = FakePrinter()
    config = FakeConfig(printer=printer, values=values)
    feeder = buffer_feeder.BufferFeeder(config)
    return printer, feeder


def test_sync_to_extruder_rolls_back_on_mid_mutation_failure():
    """If a trapq mutation raises mid-sync, the caller's finally-cleanup
    must NOT see a half-mutated state with _stepper_synced_to=name. The
    rollback path leaves the flag at None so unsync_if_synced returns
    False (no double-rollback) while still best-effort reattaching the
    stepper to our own trapq."""
    _, feeder = make_feeder()

    # Stub motion_queuing.check_step_generation_scan_windows to raise on
    # the FIRST call (the sync attempt) and succeed on the SECOND
    # (rollback's reattach). Models a transient mid-sync failure where
    # flush_step_generation and set_trapq succeed but the scan-window
    # recompute trips.
    motion_q = feeder.printer.lookup_object('motion_queuing')
    original_check = motion_q.check_step_generation_scan_windows
    call_log = {"n": 0}

    def flaky():
        call_log["n"] += 1
        if call_log["n"] == 1:
            raise RuntimeError("simulated scan-window failure mid-sync")
        return original_check()

    motion_q.check_step_generation_scan_windows = flaky

    try:
        feeder._sync_to_extruder('extruder')
    except RuntimeError:
        pass

    # Arming flag cleared, no half-sync state for finally-cleanup.
    assert feeder._stepper_synced_to is None
    # Rollback re-attached us to our own trapq.
    assert feeder.stepper.last_trapq_set is feeder.trapq
    assert call_log["n"] >= 2, "rollback did try to recompute scan windows"


def test_resume_after_overflow_phase3_overlay_branch():
    """Overlay mode: resume_after_overflow must keep _state=LOAD_PHASE_3
    so the cmd_BUFFER_LOAD_PHASE3 while-loop continues spinning, instead
    of falling through to STATE_AUTO and silently returning success."""
    _, feeder = make_feeder(values={"use_fault_overlay": True})
    feeder._state = buffer_feeder.STATE_LOAD_PHASE_3
    feeder.fault._overflow_interrupted_state = buffer_feeder.STATE_LOAD_PHASE_3
    feeder.fault._overflow_resume_mm = 50.0
    feeder._entrance_pin_polarity_flip = False
    feeder._pin_stable_state['entrance'] = True

    feeder._resume_after_overflow()

    assert feeder._state == buffer_feeder.STATE_LOAD_PHASE_3
    assert feeder.fault._overflow_resume_mm == 0.0


def test_resume_after_overflow_legacy_phase3_falls_to_auto():
    """Legacy mode (use_fault_overlay=0): the LOAD_PHASE_3 branch must
    NOT trigger — old default-fallthrough path stays untouched."""
    _, feeder = make_feeder(values={"use_fault_overlay": False})
    feeder._state = buffer_feeder.STATE_IDLE
    feeder.fault._overflow_interrupted_state = buffer_feeder.STATE_LOAD_PHASE_3
    feeder.fault._overflow_resume_mm = 50.0
    feeder._pin_stable_state['entrance'] = True

    feeder._resume_after_overflow()

    # Default fallthrough sets STATE_AUTO when entrance_detected.
    assert feeder._state == buffer_feeder.STATE_AUTO


def test_set_state_idle_clears_overlay_flag():
    """STOP_BUFFER_FILL / BUFFER_HALT take state→IDLE while HALL1 is
    still asserted. The fault_overflow overlay flag must not leak."""
    _, feeder = make_feeder(values={"use_fault_overlay": True})
    feeder._state = buffer_feeder.STATE_LOAD_PHASE_3
    feeder._fault_overflow = True

    feeder._set_state(buffer_feeder.STATE_IDLE)

    assert feeder._fault_overflow is False


def test_set_state_idle_clears_overlay_flag_legacy_mode():
    """Even with use_fault_overlay=0 we clear the shadow-tracking flag
    on IDLE so observers (get_status, BUFFER_STATE_DUMP) stay clean."""
    _, feeder = make_feeder(values={"use_fault_overlay": False})
    feeder._state = buffer_feeder.STATE_OVERFLOW
    feeder._fault_overflow = True

    feeder._set_state(buffer_feeder.STATE_IDLE)

    assert feeder._fault_overflow is False
