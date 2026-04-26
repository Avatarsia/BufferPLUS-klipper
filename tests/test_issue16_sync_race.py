"""Issue #16 — race when HALL1 falls while SYNC is active.

Sequence from Eifel-Joe's Hardware-Log:

  LOAD Phase 3: HALL1 stable 10s, treating as full   → state=IDLE
  *** HALL1 OVERFLOW ***                              → _enter_overflow → state=OVERFLOW
  Buffer-Feeder synced to 'extruder'                  → SYNC active, state=OVERFLOW
  HALL1 cleared                                       → sensor_callback → _exit_overflow
  OVERFLOW → IDLE                                     → _exit_overflow legacy branch
  IDLE → AUTO                                         → _resume_after_overflow
                                                        BUT: _stepper_synced_to still='extruder'!
  ...next bang-bang or _submit_move would append to own_trapq while
     stepper is on extruder_trapq → moves become live at next unsync
     → 'stepcompress Invalid sequence' crash on next BUFFER_SYNC_TO_EXTRUDER.

Codex audit (2026-04-26) confirmed Cause 1 is plausible and reachable
in code. The invariant we want to enforce:

  IF _state in (STATE_AUTO, STATE_IDLE) THEN _stepper_synced_to is None

i.e. bang-bang/auto state is never entered while SYNC is still active —
the SYNC must be properly released first (or _stepper_synced_to must
be cleared via the unsync path BEFORE state-transition to AUTO).
"""

from fakes_klipper import FakeConfig, FakePrinter
from klipper_extras import buffer_feeder


def make_feeder(values=None):
    printer = FakePrinter()
    config = FakeConfig(printer=printer, values=values)
    feeder = buffer_feeder.BufferFeeder(config)
    return printer, feeder


def set_sensor_active(feeder, sensor_name, active):
    polarity_flip = feeder._pin_polarity_flip[sensor_name]
    feeder._pin_stable_state[sensor_name] = (not active) if polarity_flip else active


# ---------------------------------------------------------------------------
# Invariant: state=AUTO must imply _stepper_synced_to is None
# ---------------------------------------------------------------------------

def test_exit_overflow_must_not_leave_synced_state_in_auto():
    """Reproduce the Issue #16 race: HALL1 falls while SYNC is active
    after _enter_overflow. _exit_overflow → _resume_after_overflow path
    promotes to STATE_AUTO without clearing the SYNC binding.

    This is a characterization test — currently FAILS on python-ansatz
    HEAD (eddd1c8). After the fix it must PASS.
    """
    _, feeder = make_feeder()

    # Setup: matches the Issue #16 hardware log step-by-step.
    # Phase 3 just ended (state went IDLE), HALL1 still active so
    # main_tick triggered _enter_overflow saving _overflow_interrupted_
    # state=IDLE. Then macro called BUFFER_SYNC_TO_EXTRUDER, setting
    # _stepper_synced_to='extruder'. Now HALL1 falls.
    feeder._state = buffer_feeder.STATE_OVERFLOW
    feeder.fault._overflow_interrupted_state = buffer_feeder.STATE_IDLE
    feeder._stepper_synced_to = 'extruder'
    set_sensor_active(feeder, 'hall_overflow', False)
    set_sensor_active(feeder, 'entrance', True)

    # Trigger: HALL1-fall path (sensor_callback → _exit_overflow).
    feeder._exit_overflow()

    # Invariant check: bang-bang state with active SYNC is the race.
    # Either state stays out of AUTO/IDLE, OR _stepper_synced_to is
    # cleared during the transition.
    in_bang_bang_state = feeder._state in (buffer_feeder.STATE_AUTO,
                                           buffer_feeder.STATE_IDLE)
    sync_active = feeder._stepper_synced_to is not None
    assert not (in_bang_bang_state and sync_active), (
        "Race detected: state=%s + _stepper_synced_to=%r. "
        "_bang_bang_tick or _submit_move could now queue moves to "
        "own_trapq while the stepper is bound to extruder_trapq. "
        "Next BUFFER_UNSYNC reattaches own_trapq and the queued "
        "moves become live; the next BUFFER_SYNC_TO_EXTRUDER then "
        "crashes with 'stepcompress Invalid sequence'."
        % (feeder._state, feeder._stepper_synced_to))


def test_resume_after_overflow_does_not_strand_synced_in_auto():
    """Same race, exercised via _resume_after_overflow directly. The
    fault-overlay exit branch jumps straight to the resume helper, and
    a future caller could too — verify the synced-guard short-circuits
    BEFORE any state-promotion happens.

    Setup uses STATE_OVERFLOW (post _enter_overflow, pre _exit_overflow)
    so a successful state-promotion to STATE_AUTO would be observable
    as a race. With the P7-45 guard, resume_after_overflow returns
    early and state stays in STATE_OVERFLOW until UNSYNC fires."""
    _, feeder = make_feeder()
    feeder._state = buffer_feeder.STATE_OVERFLOW
    feeder.fault._overflow_interrupted_state = buffer_feeder.STATE_IDLE
    feeder._stepper_synced_to = 'extruder'
    set_sensor_active(feeder, 'entrance', True)

    feeder._resume_after_overflow()

    # No state-promotion while synced.
    assert feeder._state == buffer_feeder.STATE_OVERFLOW, (
        "resume_after_overflow promoted state to %s while sync still "
        "active (_stepper_synced_to=%r)"
        % (feeder._state, feeder._stepper_synced_to))


# ---------------------------------------------------------------------------
# Concrete trapq divergence: track move appends, fail if any move lands
# on own_trapq while the stepper is bound to a different trapq.
# ---------------------------------------------------------------------------

def test_no_own_trapq_append_while_synced_during_overflow_recovery():
    """The concrete crash mechanism: while _stepper_synced_to='extruder',
    the stepper is bound to extruder_trapq. Any trapq_append to
    feeder.trapq (own_trapq) during that window queues moves that
    later become live at unsync, corrupting the stepcompress cursor."""
    printer, feeder = make_feeder()
    motion_q = printer.lookup_object('motion_queuing')

    # Snapshot existing append_calls so we only count the new ones from
    # the recovery path.
    baseline_appends = len(motion_q.append_calls)

    feeder._state = buffer_feeder.STATE_OVERFLOW
    feeder.fault._overflow_interrupted_state = buffer_feeder.STATE_IDLE
    feeder._stepper_synced_to = 'extruder'
    set_sensor_active(feeder, 'hall_overflow', False)
    set_sensor_active(feeder, 'entrance', True)
    set_sensor_active(feeder, 'hall_empty', True)

    feeder._exit_overflow()

    # If the recovery path moved us to STATE_AUTO without unsync, then
    # the next main_tick / submit_move would inject own_trapq appends.
    # Verify: any appends that DID happen during exit_overflow must
    # NOT be on own_trapq while sync is still active.
    new_appends = motion_q.append_calls[baseline_appends:]
    for call_args in new_appends:
        appended_trapq = call_args[0]
        if feeder._stepper_synced_to is not None:
            assert appended_trapq is not feeder.trapq, (
                "trapq divergence: move appended to own_trapq while "
                "_stepper_synced_to=%r (stepper is bound to extruder_"
                "trapq). t0=%s" % (feeder._stepper_synced_to,
                                    call_args[1] if len(call_args) > 1 else "?"))


# ---------------------------------------------------------------------------
# Deferred _exit_overflow runs at unsync — the sequel to test #1
# ---------------------------------------------------------------------------

def test_unsync_runs_deferred_exit_overflow_when_hall1_already_clear():
    """After P7-45 _exit_overflow short-circuits while synced. The
    state-machine catches up at unsync time: SyncCoordinator.
    unsync_if_synced sees state=OVERFLOW + hall_overflow=False and
    re-runs _exit_overflow → state finally goes to IDLE/AUTO.

    This is the full LOAD-Phase 3 happy path under the new architecture:
      Phase 3 → IDLE → main_tick → OVERFLOW → SYNC → HALL1-fall →
      _exit_overflow short-circuit (still OVERFLOW) → G1 E run → UNSYNC
      → unsync_if_synced re-runs _exit_overflow → state=IDLE/AUTO."""
    _, feeder = make_feeder()
    feeder._state = buffer_feeder.STATE_OVERFLOW
    feeder.fault._overflow_interrupted_state = buffer_feeder.STATE_IDLE
    feeder._stepper_synced_to = 'extruder'
    set_sensor_active(feeder, 'entrance', True)

    # HALL1-fall during sync — _exit_overflow short-circuits.
    set_sensor_active(feeder, 'hall_overflow', False)
    feeder._exit_overflow()
    assert feeder._state == buffer_feeder.STATE_OVERFLOW, (
        "deferred _exit_overflow expected — got %s" % feeder._state)
    assert feeder._stepper_synced_to == 'extruder'

    # Macro now calls BUFFER_UNSYNC. Deferred exit must run.
    feeder._unsync_if_synced()

    assert feeder._stepper_synced_to is None
    assert feeder._state in (buffer_feeder.STATE_AUTO,
                             buffer_feeder.STATE_IDLE), (
        "deferred _exit_overflow at unsync did not resolve state — "
        "got %s" % feeder._state)


def test_unsync_does_not_re_enter_exit_overflow_if_hall1_still_active():
    """If HALL1 is still asserted when unsync runs, the deferred-exit
    hook must NOT fire — _exit_overflow would no-op anyway, but we
    verify state=OVERFLOW is preserved so the operator sees the real
    hardware condition."""
    _, feeder = make_feeder()
    feeder._state = buffer_feeder.STATE_OVERFLOW
    feeder.fault._overflow_interrupted_state = buffer_feeder.STATE_IDLE
    feeder._stepper_synced_to = 'extruder'
    set_sensor_active(feeder, 'hall_overflow', True)
    set_sensor_active(feeder, 'entrance', True)

    feeder._unsync_if_synced()

    assert feeder._stepper_synced_to is None
    # HALL1 still on — state must stay OVERFLOW.
    assert feeder._state == buffer_feeder.STATE_OVERFLOW
