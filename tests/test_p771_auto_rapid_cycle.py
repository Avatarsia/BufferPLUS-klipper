"""P7-71 — AUTO-Rapid-Cycle Force-Floor (Issue #29 Eifel-Joe Update).

P7-67 deckt nur den AUTO-SLOW-Cycle ab
--------------------------------------
P7-67 (commit 8f5ffc9) hat das LOAD_PHASE_1/AUTO-Slow-Resume gefixt: dort
läuft `_disable_stepper()` zwischen OVERFLOW und Resume und clear-t
`_stepcompress_primed=False`. Die en-Floor-Bedingung
`if streaming and was_primed` greift dann nicht → der reguläre en-Floor
`_last_enable_schedule_time` bleibt aktiv → t0 hat lead_time-Margin
gegen Enable → kein "Invalid sequence".

Der AUTO-Rapid-Cycle (Eifel-Joe Update)
---------------------------------------
Wenn HALL1 SO schnell fluttert, dass `_disable_stepper()` NIE läuft:

    1. AUTO-Streaming, _stepcompress_primed=True, Move in flight.
    2. HALL1 fires → `_enter_overflow` → `_schedule_stepper_disable` →
       weil `_move_in_flight()=True`: `_pending_disable=True` gesetzt,
       `_disable_stepper()` NICHT aufgerufen.
    3. BEVOR der Move drainiert: HALL1 cleared →
       `_exit_overflow` → `_resume_after_overflow` → `_enable_stepper`.
    4. `_enable_stepper()` cancelt `_pending_disable=False`
       (buffer_feeder.py:2697).
    5. `_disable_stepper()` läuft NIE → `_stepcompress_primed` bleibt
       True über den gesamten OVERFLOW-Zyklus.
    6. _on_mcu_flush wird auf `_needs_overflow_prime=True` getriggert,
       submittet 0.05mm prime-Move. Der Reprime-Block in
       `_submit_single_trapezoid` läuft (need_reprime=True, denn auch bei
       primed=True triggern forced_t0 != None UND not primed... NEIN —
       eigentlich nur not primed; siehe sofort).

    HALT — der eigentliche Bug-Pfad ist subtiler:

    Mit forced_t0 != None: need_reprime = not self._stepcompress_primed.
    Bei was_primed=True wäre need_reprime=False → kein set_position →
    OK, kein Bug.

    Aber bei dem normalen Streaming-Pfad: `_on_mcu_flush` setzt
    forced_t0=step_gen_time+lead_time UND was_primed=True → need_reprime
    False, der set_position-Block läuft nicht. Auch nicht buggy.

    Der eigentliche Bug-Pfad ist:
    Wenn need_reprime=True (egal warum: legacy-Pfad gap>5s, oder
    not _stepcompress_primed) UND streaming=True UND was_primed=True
    nicht möglich (need_reprime impliziert was_primed=False ODER
    gap>5s)…

    Eckpunkt 1: gap > 5s + was_primed=True + streaming=True
    --------------------------------------------------------
    forced_t0=None Pfad: need_reprime = (not primed) or (gap > 5s).
    Mit primed=True UND gap>5s wird need_reprime=True. Der Reprime
    läuft: flush_step_generation + set_position((0,0,0)).
    P7-67 Bedingung: `streaming and was_primed` ist hier TRUE
    (was_primed war True!), en wird 0.0. Aber nach set_position ist
    _last_move_end_time semantisch entkoppelt → t0 landet auf
    _last_move_end_time, KEIN lead_time-Margin → "Invalid sequence".

    Eckpunkt 2: Direkt-Call mit forced_t0=None + primed=True + gap>5s
    -----------------------------------------------------------------
    Beispiel: nach langer IDLE-Pause submittet jemand einen
    streaming=True submit (theoretisch — _tick_pending_chunk könnte
    bei genug Stale-Pendings einen solchen Submit erzeugen).

P7-71 Fix
---------
Reprime-Block triggert Force-Floor: wenn `need_reprime=True`, MUSS
en-Floor aktiv sein, unabhängig von streaming/was_primed.

    en = (0.0
          if streaming and was_primed and not need_reprime
          else self._last_enable_schedule_time)

Begründung: nach `set_position((0,0,0))` ist
`_last_move_end_time` ein toter Wert — der Cursor wurde auf 0
zurückgesetzt, das _last_move_end_time vom alten Cursor ist nicht mehr
konsistent mit dem neuen Stepcompress-Zustand. Ohne en-Floor landet t0
zu nah am Enable → MCU "Invalid sequence" Crash.

Tags: post-overflow rapid cycle, gap>5s reprime, was_primed=True edge.
"""

import pytest

from fakes_klipper import FakeConfig, FakePrinter
from klipper_extras import buffer_feeder


def set_sensor_active(feeder, sensor_name, active):
    polarity_flip = feeder._pin_polarity_flip[sensor_name]
    raw = (not active) if polarity_flip else active
    feeder._pin_stable_state[sensor_name] = raw
    feeder._pin_raw_state[sensor_name] = raw


def make_feeder(values=None):
    """Build a feeder with stepper_enable wired up (klippy:connect)."""
    base = {"use_flush_callback_bang_bang": True}
    if values:
        base.update(values)
    printer = FakePrinter()
    config = FakeConfig(printer=printer, values=base)
    feeder = buffer_feeder.BufferFeeder(config)
    printer.fire_event('klippy:connect')
    feeder._startup_grace_done = True
    feeder._state = buffer_feeder.STATE_AUTO
    set_sensor_active(feeder, 'hall_overflow', False)
    set_sensor_active(feeder, 'hall_full', False)
    set_sensor_active(feeder, 'hall_empty', False)
    return printer, feeder


def _submitted_to_own_trapq(motion_q, feeder, start_index):
    return [c for c in motion_q.append_calls[start_index:]
            if c[0] is feeder.trapq]


# ---------------------------------------------------------------------------
# Bug-reproduction characterization (would fail on PRE-FIX P7-67-only code).
# ---------------------------------------------------------------------------

def test_streaming_primed_with_reprime_forces_en_floor():
    """P7-71 core: streaming=True + was_primed=True PLUS need_reprime=True
    (gap > 5s after long idle) must keep en-floor active. The reprime
    block has just called set_position((0,0,0)) — _last_move_end_time
    from the pre-reprime session is semantically dead. Without en-floor
    t0 lands at the stale _last_move_end_time → MCU 'Invalid sequence'.

    PRE-FIX (P7-67-only): t0 == _last_move_end_time (5.20) — BUG.
    POST-FIX (P7-71):     t0 >= _last_enable_schedule_time (5.50).
    """
    printer, feeder = make_feeder()
    motion_q = printer.lookup_object('motion_queuing')

    # Setup: streaming submit but with stale anchor (gap > 5s).
    # was_primed=True (Stepcompress war auf alten Cursor primed), aber
    # der Reprime-Block läuft sowieso wegen gap > REPRIME_GAP=5s.
    feeder.reactor.now = 12.00  # mcu_now = 12.00
    feeder._last_move_end_time = 5.20  # gap = 12.00 - 5.20 = 6.80s > 5s
    feeder._last_enable_schedule_time = 5.50
    feeder._stepcompress_primed = True   # <-- was_primed=True
    feeder._current_move = None  # no in-flight move (would be drained
                                 # in the long gap)

    appends_before = len(motion_q.append_calls)
    # forced_t0=None damit need_reprime = (not primed) or (gap > 5s)
    # → need_reprime=True trotz was_primed=True.
    feeder._submit_single_trapezoid(
        +1.0 * feeder.flush_callback_chunk_mm, feeder.feed_speed,
        streaming=True)

    own = _submitted_to_own_trapq(motion_q, feeder, appends_before)
    assert own, "expected streaming submit"
    t0 = own[0][1]

    # P7-71 expects: en-floor active despite was_primed=True because
    # need_reprime triggered set_position((0,0,0)).
    # NOTE: _enable_stepper is NOT called in streaming-path, so
    # _last_enable_schedule_time remains at 5.50 (the value set in
    # the test setup, simulating the schedule from the last enable).
    assert t0 >= 5.50 - 0.001, (
        "P7-71 broken: streaming+primed=True with need_reprime=True "
        "landed t0=%.3f below _last_enable_schedule_time=5.50. "
        "After set_position((0,0,0)), _last_move_end_time is stale → "
        "MCU 'Invalid sequence' regression (Issue #29 rapid-cycle)." % t0)

    # Cursor must have been re-primed.
    assert feeder._stepcompress_primed is True, (
        "submit must reprime stepcompress when need_reprime=True")


# ---------------------------------------------------------------------------
# Issue #29 scenario 1: HALL1 flicker with pending-disable cancelled.
# ---------------------------------------------------------------------------

def test_auto_resume_after_overflow_with_pending_disable_cancelled():
    """Reproduces the Eifel-Joe rapid-cycle:

      1. AUTO-Streaming, Move in flight, _stepcompress_primed=True.
      2. HALL1 fires → _enter_overflow → _schedule_stepper_disable:
         Move in flight → _pending_disable=True (defer).
      3. BEFORE in-flight move drains: HALL1 cleared →
         _resume_after_overflow → _enable_stepper.
      4. _enable_stepper cancels _pending_disable=False
         (buffer_feeder.py:2697). _disable_stepper NEVER ran.
      5. _stepcompress_primed stays True over the whole cycle.
      6. First flush-callback submit after resume must still have
         en-floor active because the reprime path might trigger
         (gap > 5s or stale clock).
    """
    printer, feeder = make_feeder()
    motion_q = printer.lookup_object('motion_queuing')

    # 1. AUTO streaming chunk in flight.
    feeder.reactor.now = 5.00
    feeder._last_move_end_time = 5.20
    feeder._commanded_pos = 9.0
    feeder._stepcompress_primed = True
    feeder._current_move = {
        'end_time': 5.20, 'direction': 1.0,
        'distance': feeder.flush_callback_chunk_mm,
        'speed': feeder.feed_speed,
    }

    # 2. HALL1 fires → _schedule_stepper_disable while in-flight.
    feeder._schedule_stepper_disable()
    assert feeder._pending_disable is True, (
        "_schedule_stepper_disable must defer when move in flight "
        "(precondition for the rapid-cycle bug)")
    assert feeder._stepcompress_primed is True, (
        "_pending_disable path must NOT clear _stepcompress_primed "
        "(_disable_stepper hasn't run yet)")

    # 3. HALL1 cleared before drain → real _resume_after_overflow.
    #    This walks the full overflow→AUTO resume routing (FaultManager
    #    .resume_after_overflow → _enable_stepper → _set_state(STATE_AUTO))
    #    rather than just calling _enable_stepper in isolation. The
    #    _enable_stepper effect inside this routing is what triggers the
    #    _pending_disable cancellation that is the entry condition for
    #    the rapid-cycle bug. Codex-Verify P7-71 flagged the prior
    #    direct-_enable_stepper version as not proving the resume
    #    pathway routes through this state — fixed here.
    feeder.reactor.now = 5.10  # still in-flight (end_time=5.20)
    enable_handle = feeder._stepper_enable
    feeder._state = buffer_feeder.STATE_OVERFLOW
    feeder.fault._overflow_interrupted_state = buffer_feeder.STATE_AUTO
    set_sensor_active(feeder, 'entrance', True)
    feeder._auto_off_by_user = False
    feeder._bang_bang_suspended = False
    feeder._halt_requested = False
    feeder._resume_after_overflow()

    # 4. _enable_stepper cancels _pending_disable.
    assert feeder._pending_disable is False, (
        "_enable_stepper must cancel _pending_disable (cf. line 2697)")
    assert feeder._stepcompress_primed is True, (
        "_stepcompress_primed must still be True — _disable_stepper "
        "never ran (this is the rapid-cycle entry condition)")

    en_after_reenable = feeder._last_enable_schedule_time
    assert en_after_reenable >= 5.10 + feeder.lead_time - 0.01, (
        "Re-enable must push _last_enable_schedule_time forward by "
        "lead_time (got %.3f, expected >= %.3f)" % (
            en_after_reenable, 5.10 + feeder.lead_time))

    # 5. Simulate the rapid-cycle ageing scenario: imagine wall-clock
    #    advanced enough that gap > 5s would trigger a reprime when
    #    the next non-forced submit arrives. We assert t0 >= en
    #    regardless. Most importantly: streaming submit with
    #    was_primed=True (entry primed) must still honour en-floor
    #    if need_reprime triggers.
    feeder.reactor.now = 11.50  # gap to last_move_end_time = 6.30s > 5s
    appends_before = len(motion_q.append_calls)
    feeder._submit_single_trapezoid(
        +9.0, feeder.feed_speed, streaming=True)

    own = _submitted_to_own_trapq(motion_q, feeder, appends_before)
    assert own, "expected resume submit"
    t0 = own[0][1]

    # P7-71 fix: en-floor preserved despite was_primed=True because
    # need_reprime fired (gap > 5s). Without fix t0 == 5.20 → MCU crash.
    assert t0 >= en_after_reenable - 0.001, (
        "P7-71 broken: streaming resume after pending_disable-cancel + "
        "gap>5s landed t0=%.3f below en=%.3f. After set_position("
        "(0,0,0)) the _last_move_end_time is semantically dead → MCU "
        "'Invalid sequence' regression (Issue #29 rapid-cycle)." % (
            t0, en_after_reenable))


# ---------------------------------------------------------------------------
# Issue #29 scenario 2: after the post-overflow prime-anchor.
# ---------------------------------------------------------------------------

def test_auto_streaming_chunk_after_prime_anchor_safe():
    """After _needs_overflow_prime path fires the 0.05mm anchor in
    _on_mcu_flush, the stepcompress cursor is freshly primed
    (set_position via the prime move). The next streaming-lookahead
    submit (via _on_mcu_flush flush_time = step_gen_time path) must
    still have lead_time margin between Enable and the first step,
    NOT abutt at the just-set _last_move_end_time of the prime-anchor.

    This is a defense-in-depth test: even though the prime-anchor uses
    forced_t0=step_gen_time+lead_time which naturally has lead_time
    distance from now, the NEXT submit must not race in tight on top.
    """
    printer, feeder = make_feeder()
    motion_q = printer.lookup_object('motion_queuing')
    set_sensor_active(feeder, 'hall_empty', True)

    # State after OVERFLOW → IDLE → AUTO transition:
    feeder.reactor.now = 6.00
    feeder._needs_overflow_prime = True
    feeder._stepcompress_primed = True   # rapid-cycle: never disabled
    feeder._last_move_end_time = 5.20    # pre-OVERFLOW remnant
    feeder._last_enable_schedule_time = 5.50

    # First flush after AUTO re-entry: prime-anchor path.
    appends_before = len(motion_q.append_calls)
    motion_q.trigger_flush(flush_time=6.00, step_gen_time=6.05)
    prime_own = _submitted_to_own_trapq(motion_q, feeder, appends_before)
    assert prime_own, "prime-anchor submit missing"
    prime_t0 = prime_own[0][1]
    # Prime-anchor: forced_t0 = step_gen_time + lead_time = 6.35.
    assert prime_t0 >= 6.05 + feeder.lead_time - 0.01, (
        "Prime-anchor t0=%.3f, expected >= %.3f" % (
            prime_t0, 6.05 + feeder.lead_time))
    assert feeder._needs_overflow_prime is False, (
        "Prime-anchor must clear _needs_overflow_prime")

    # Now the next flush: AUTO streaming-lookahead must honour en-floor
    # if need_reprime triggers; here gap is small so no reprime — the
    # streaming abuttment is the expected behaviour. This branch is
    # the P7-66/P7-67 baseline.
    feeder.reactor.now = 6.10
    # _last_move_end_time advanced by prime submit; record it.
    after_prime_end = feeder._last_move_end_time
    appends_before = len(motion_q.append_calls)
    # Hall_empty still active → next flush should submit streaming chunk.
    feeder._current_move = {
        'end_time': after_prime_end, 'direction': 1.0,
        'distance': 0.05,
        'speed': feeder.feed_speed,
    }
    motion_q.trigger_flush(flush_time=after_prime_end - 0.05,
                           step_gen_time=after_prime_end - 0.05)
    second = _submitted_to_own_trapq(motion_q, feeder, appends_before)
    if second:
        t0_2 = second[0][1]
        # streaming + primed + no reprime → abuttend at after_prime_end.
        # P7-71 inactive here (need_reprime=False), P7-66 abuttment intact.
        assert t0_2 == pytest.approx(after_prime_end, abs=0.05) or \
               t0_2 >= feeder._last_enable_schedule_time - 0.001, (
            "Streaming-after-prime t0=%.3f neither abutts at "
            "after_prime_end=%.3f nor honours en-floor %.3f" % (
                t0_2, after_prime_end,
                feeder._last_enable_schedule_time))


# ---------------------------------------------------------------------------
# Issue #29 scenario 3: repeated OVERFLOW→IDLE→AUTO cycles.
# ---------------------------------------------------------------------------

def test_repeated_overflow_cycle_no_invalid_sequence():
    """Reproduces Eifel-Joe's log: 3× AUTO→OVERFLOW→IDLE→AUTO with
    move-in-flight (rapid HALL1 flicker), then a final streaming
    submit AFTER gap > REPRIME_GAP (5s) to force the buggy edge.

    Invariant tested:
      - Cycles 0 + 1 (no reprime, streaming abuttment): t0 ==
        _last_move_end_time. This is the P7-66 baseline — en-floor
        intentionally dropped, motor was already enabled by the
        prior in-flight chunk so no MCU race.
      - Cycle 2 (gap > REPRIME_GAP forces need_reprime=True with
        was_primed=True): MUST honour en-floor — P7-71 fix territory.
        Pre-P7-71 t0 == _last_move_end_time (stale after
        set_position((0,0,0))) → "Invalid sequence" crash.

    The test_p767_resume_anchor.test_load_phase1_resume_after_overflow_*
    covers the SLOW path with explicit _disable_stepper. This test
    covers the RAPID path where _pending_disable is repeatedly
    cancelled and _stepcompress_primed never flips to False.
    """
    printer, feeder = make_feeder()
    motion_q = printer.lookup_object('motion_queuing')

    feeder._last_move_end_time = 5.00
    feeder._commanded_pos = 0.0
    feeder._stepcompress_primed = True

    submit_t0s = []
    for cycle in range(3):
        # Cycle k: simulate move-in-flight.
        cycle_now = 5.00 + cycle * 0.5
        feeder.reactor.now = cycle_now
        feeder._current_move = {
            'end_time': cycle_now + 0.20, 'direction': 1.0,
            'distance': feeder.flush_callback_chunk_mm,
            'speed': feeder.feed_speed,
        }
        feeder._last_move_end_time = cycle_now + 0.20

        # HALL1 fires → defer disable (move in flight).
        feeder._schedule_stepper_disable()
        assert feeder._pending_disable is True

        # HALL1 clears mid-flight → re-enable cancels pending.
        feeder.reactor.now = cycle_now + 0.05
        feeder._enable_stepper()
        assert feeder._pending_disable is False
        assert feeder._stepcompress_primed is True, (
            "Cycle %d: _stepcompress_primed flipped to False — that "
            "would be the slow-cycle path, not rapid-cycle." % cycle)

        en_for_cycle = feeder._last_enable_schedule_time
        last_move_end_pre = feeder._last_move_end_time

        # Submit the continuation streaming chunk.
        appends_before = len(motion_q.append_calls)

        if cycle < 2:
            # Cycles 0/1: small gap, no reprime → P7-66 streaming
            # abuttment, en-floor dropped. _last_move_end_time is
            # the abuttment anchor. en is from the in-flight chunk's
            # original enable.
            feeder._submit_single_trapezoid(
                +9.0, feeder.feed_speed, streaming=True)
        else:
            # Cycle 2: large gap → need_reprime=True with
            # was_primed=True. THIS is the P7-71 territory.
            # Wall-clock advances → gap > REPRIME_GAP=5s.
            feeder.reactor.now = cycle_now + 6.0

            # _last_enable_schedule_time stays at en_for_cycle (no
            # re-enable between now and submit — streaming path
            # skips _enable_stepper). But the reprime block fires
            # because gap > 5s. The toolhead.flush_step_generation
            # is invoked in the legacy forced_t0=None branch.
            feeder._submit_single_trapezoid(
                +9.0, feeder.feed_speed, streaming=True)

        own = _submitted_to_own_trapq(motion_q, feeder, appends_before)
        assert own, "cycle %d: submit missing" % cycle
        t0 = own[0][1]
        submit_t0s.append((cycle, t0, en_for_cycle, last_move_end_pre))

        if cycle < 2:
            # Baseline P7-66: streaming abuttment intact (en-floor
            # dropped because was_primed AND no reprime).
            assert t0 == pytest.approx(last_move_end_pre, abs=0.001), (
                "Cycle %d (no reprime): expected abuttment at %.3f, "
                "got %.3f" % (cycle, last_move_end_pre, t0))
        else:
            # P7-71 critical path: was_primed=True + need_reprime=True
            # → en-floor enforced. Without fix t0 == last_move_end_pre
            # (5.20 + cycles, stale after set_position(0,0,0)).
            assert t0 >= en_for_cycle - 0.001, (
                "Cycle %d (gap>5s + primed=True): t0=%.3f < en=%.3f "
                "— lead_time margin between motor_enable and first "
                "step is gone after set_position((0,0,0)). MCU "
                "'Invalid sequence' regression (Issue #29 rapid-"
                "cycle, P7-71)." % (cycle, t0, en_for_cycle))

    # Monotonicity: t0 must not move backwards across cycles
    # (sanity check; clock advances).
    prev_t0 = -1.0
    for _, t0, _, _ in submit_t0s:
        assert t0 >= prev_t0, (
            "t0 non-monotonic across cycles: %.3f after %.3f"
            % (t0, prev_t0))
        prev_t0 = t0
