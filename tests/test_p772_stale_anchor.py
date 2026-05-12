"""P7-72 — Stale-anchor guard (Issue #29 Eifel-Joe 22-cycle Update).

Lücke die P7-71 strukturell nicht schließt
------------------------------------------
P7-71 (commit 723e3de) erweitert die en-Floor-Bedingung um
`not need_reprime` — der Force-Floor greift wenn der Reprime-Block
gerade `set_position((0,0,0))` aufgerufen hat. Damit ist der
"gap > REPRIME_GAP" plus "was_primed=True"-Pfad abgedeckt.

Aber: es gibt einen verwandten Pfad, in dem `need_reprime=False`
trotzdem mit einem stale `_last_move_end_time` kombiniert wird:

  1. AUTO-Streaming, Move in flight, `_stepcompress_primed=True`,
     `_last_move_end_time=5.20` (Chunk-Ende).
  2. HALL1 fires → `_enter_overflow → _halt_motion` mid-flight.
     `_halt_motion` setzt _continuous_feed=False + räumt pending_-
     remaining_mm — aber `_last_move_end_time` bleibt auf 5.20.
  3. `_schedule_stepper_disable` deferred (move in flight) →
     `_pending_disable=True`, `_disable_stepper` läuft NICHT.
  4. Chunk drained natürlich aus (kein Cancel im MCU möglich).
     `mcu_now` läuft jetzt über 5.20 hinaus.
  5. HALL1 cleared → `_resume_after_overflow → _enable_stepper` →
     `_pending_disable=False` → `_stepcompress_primed` bleibt
     True (Cancel-Race der Rapid-Cycle).
  6. Bevor `gap > REPRIME_GAP=5s` greift, kommt der nächste
     `_on_mcu_flush`-Submit. mcu_now=8.00, `_last_move_end_time=
     5.20`, gap=2.80s < 5s → `need_reprime=False`. P7-71 Force-Floor
     greift NICHT.
  7. `streaming=True`, `was_primed=True`, `need_reprime=False`
     → P7-71 en=0.0. t0 landet auf max(forced_t0, 5.20, 0.0,
     mcu_now=8.00) → t0=8.00. Im `forced_t0`-Branch schützt
     mcu_now-Floor zwar gegen "Past-Anchor", aber im
     `forced_t0=None`-Streaming-Submit (via `_tick_pending_chunk`
     → `_submit_single_trapezoid(streaming=True)`) gibt es nur
     `t0 = max(_last_move_end_time, en)` (Line 2998). Mit en=0 +
     stale `_last_move_end_time=5.20` und mcu_now=8.00 fällt es
     in den else-Branch (3000) `toolhead.get_last_move_time()` —
     aber der `_last_move_end_time > mcu_now + lead_time`-Check
     ist ja schon False für stale anchor.

Eigentliche Crash-Pfad: jeder Submit der mit `streaming=True` und
stalem `_last_move_end_time` kommt, würde ohne explizit gesetzten
mcu_now-Floor in einen unsicheren Anker driften. Im aktuellen Code
schützen mehrere Schichten (mcu_now-Floor im forced_t0-Branch,
get_last_move_time-Fallback im else-Branch), aber die en-Floor-
Drop-Optimierung beruht auf `_last_move_end_time` als gültigem
Anker. P7-72 macht das explizit:

P7-72 Fix
---------
Stale-Anchor erkennen über `_last_move_end_time <= mcu_now` —
wenn der Anker in der Vergangenheit liegt, ist er semantisch tot,
und die en-Floor-Drop-Optimierung (P7-66 R1) ist NICHT mehr safe.
en-Floor muss aktiv bleiben.

    stale_anchor = (self._last_move_end_time <= mcu_now)
    en = (0.0
          if streaming and was_primed and not need_reprime
              and not stale_anchor
          else self._last_enable_schedule_time)

Begründung: bei healthy streaming ist `_last_move_end_time` strikt
in der Zukunft (Chunk Ende > mcu_now), die Bedingung ist inert.
Sie greift nur in dem Edge-Case wo Anker tot ist.

Tags: Eifel-Joe 22-cycle, stale-anchor defense-in-depth, post-halt
streaming submit.
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
# Test 1: Stale-Anchor detected → en-Floor activated (basic)
# ---------------------------------------------------------------------------

def test_stale_anchor_forces_en_floor():
    """P7-72 core: streaming=True + was_primed=True + need_reprime=False
    aber `_last_move_end_time <= mcu_now` (Anker in der Vergangenheit) →
    en-Floor muss aktiv bleiben. P7-71-Drei-Guard-Bedingung würde sonst
    en=0.0 erlauben → t0 landet auf stalem `_last_move_end_time` →
    MCU 'Invalid sequence' Risiko.
    """
    printer, feeder = make_feeder()
    motion_q = printer.lookup_object('motion_queuing')

    # Setup:
    # - was_primed=True (Stepcompress war primed)
    # - gap = 2.80s < REPRIME_GAP=5s → need_reprime=False
    # - _last_move_end_time <= mcu_now → stale_anchor=True
    feeder.reactor.now = 8.00  # mcu_now = 8.00
    feeder._last_move_end_time = 5.20  # gap = 2.80s < 5s
    feeder._last_enable_schedule_time = 6.00
    feeder._stepcompress_primed = True
    feeder._current_move = None  # not in flight; chunk drained

    appends_before = len(motion_q.append_calls)
    # forced_t0=None + streaming=True; das ist der Edge-Case wo
    # P7-72 greift. need_reprime = (not primed) or (gap > 5s) = False.
    feeder._submit_single_trapezoid(
        +1.0 * feeder.flush_callback_chunk_mm, feeder.feed_speed,
        streaming=True)

    own = _submitted_to_own_trapq(motion_q, feeder, appends_before)
    assert own, "expected streaming submit"
    t0 = own[0][1]

    # P7-72 expects: stale_anchor=True → en-Floor active despite
    # streaming + was_primed + not need_reprime.
    # _last_enable_schedule_time = 6.00, mcu_now = 8.00.
    # t0 must respect en-floor (>= 6.00) — falls without P7-72 t0
    # would be max(5.20, 0.0) and fall to the toolhead-anchor path
    # which is also safe, but the explicit en-floor guarantees the
    # lead_time-margin against Enable regardless of submit-pfad.
    assert t0 >= feeder._last_enable_schedule_time - 0.001, (
        "P7-72 broken: streaming+primed=True with stale-anchor "
        "(last_move_end_time=%.3f <= mcu_now=%.3f) landed t0=%.3f "
        "below _last_enable_schedule_time=%.3f. en-Floor must "
        "stay active when anchor is semantically dead." % (
            feeder._last_move_end_time, 8.00, t0,
            feeder._last_enable_schedule_time))


# ---------------------------------------------------------------------------
# Test 2: Eifel-Joe 22-cycle rapid AUTO-Bang-Bang pattern
# ---------------------------------------------------------------------------

def test_eifel_joe_22_cycle_no_stale_anchor_corruption():
    """Simuliert Eifel-Joes 22-Cycle rapid AUTO-Bang-Bang.

    Pattern pro Cycle (≤ 1s):
      - AUTO-Streaming, move in flight.
      - HALL1 fires → _enter_overflow → _halt_motion + schedule_disable
        defer (move in flight, _pending_disable=True).
      - Move drains naturally (no MCU cancel).
      - HALL1 cleared → _resume_after_overflow → _enable_stepper →
        _pending_disable=False. _stepcompress_primed bleibt True.
      - Submit nächster Streaming-Chunk.

    Nach N=22 Cycles: `_last_move_end_time` ist evtl. stale
    (mcu_now > _last_move_end_time bevor neuer Submit kommt).
    Streaming-Submit MUSS dann en-Floor honorieren.
    """
    printer, feeder = make_feeder()
    motion_q = printer.lookup_object('motion_queuing')

    feeder.reactor.now = 5.00
    feeder._last_move_end_time = 5.00
    feeder._commanded_pos = 0.0
    feeder._stepcompress_primed = True

    crashed_cycles = []
    for cycle in range(22):
        cycle_start = 5.00 + cycle * 0.5
        feeder.reactor.now = cycle_start

        # Submit a streaming chunk that lands end_time=cycle_start+0.20.
        feeder._current_move = {
            'end_time': cycle_start + 0.20, 'direction': 1.0,
            'distance': 9.0, 'speed': feeder.feed_speed,
        }
        feeder._last_move_end_time = cycle_start + 0.20

        # HALL1 fires mid-flight → defer disable.
        feeder._schedule_stepper_disable()

        # Wall-clock advances PAST _last_move_end_time without a
        # successor submit (move drained, no chunk queued).
        feeder.reactor.now = cycle_start + 0.40  # > end_time=0.20

        # HALL1 cleared → _enable_stepper cancels _pending_disable.
        feeder._enable_stepper()
        assert feeder._pending_disable is False
        assert feeder._stepcompress_primed is True

        # Now: _last_move_end_time = cycle_start + 0.20 <= mcu_now
        # (cycle_start + 0.40). stale_anchor=True.
        mcu_now = feeder.reactor.now
        assert feeder._last_move_end_time <= mcu_now, (
            "Cycle %d: precondition broken — anchor not stale" % cycle)

        en_for_cycle = feeder._last_enable_schedule_time

        appends_before = len(motion_q.append_calls)
        # Streaming-Submit nach stale-Anker-Phase.
        feeder._submit_single_trapezoid(
            +9.0, feeder.feed_speed, streaming=True)

        own = _submitted_to_own_trapq(motion_q, feeder, appends_before)
        assert own, "cycle %d: submit missing" % cycle
        t0 = own[0][1]

        # P7-72 invariant: stale_anchor=True forces en-Floor.
        # gap = mcu_now - _last_move_end_time (pre-submit). For early
        # cycles gap is small (~0.20s); only at cycle 9-10+ would
        # gap exceed REPRIME_GAP=5s. Bis dahin schützt nur P7-72.
        if t0 < en_for_cycle - 0.001:
            crashed_cycles.append(
                (cycle, t0, en_for_cycle, feeder._last_move_end_time))

    assert not crashed_cycles, (
        "P7-72 broken: %d/22 cycles crashed with stale-anchor: %s"
        % (len(crashed_cycles), crashed_cycles[:3]))


# ---------------------------------------------------------------------------
# Test 3: Normal streaming (anchor in future) — P7-66 performance intact
# ---------------------------------------------------------------------------

def test_healthy_streaming_no_perf_regression():
    """P7-72 stale-anchor guard MUST NOT regress P7-66 streaming
    performance in the healthy case. When `_last_move_end_time` is
    strictly in the future (chunk in flight, abuttment intact), the
    en-Floor MUST stay dropped — abuttend-anchor at _last_move_end_-
    time, no lead_time-Gap.
    """
    printer, feeder = make_feeder()
    motion_q = printer.lookup_object('motion_queuing')

    # Healthy streaming setup:
    # - mcu_now = 5.00
    # - _last_move_end_time = 5.50 (chunk in flight, > mcu_now)
    # - was_primed=True, no reprime needed.
    feeder.reactor.now = 5.00
    feeder._last_move_end_time = 5.50  # > mcu_now → NOT stale
    feeder._last_enable_schedule_time = 5.30
    feeder._stepcompress_primed = True
    feeder._current_move = {
        'end_time': 5.50, 'direction': 1.0,
        'distance': 9.0, 'speed': feeder.feed_speed,
    }

    appends_before = len(motion_q.append_calls)
    feeder._submit_single_trapezoid(
        +9.0, feeder.feed_speed, streaming=True)

    own = _submitted_to_own_trapq(motion_q, feeder, appends_before)
    assert own, "expected streaming submit"
    t0 = own[0][1]

    # P7-66 expects: t0 == _last_move_end_time (abuttment). en-Floor
    # is dropped because healthy streaming + was_primed=True +
    # not need_reprime + NOT stale_anchor.
    assert t0 == pytest.approx(5.50, abs=0.001), (
        "P7-72 regression: healthy streaming should abutt at "
        "_last_move_end_time=5.50, got t0=%.3f. en-Floor active in "
        "healthy path → inter-chunk gap reopens, P7-66 broken." % t0)


# ---------------------------------------------------------------------------
# Test 4: Test 4: P7-71 path still works (regression guard)
# ---------------------------------------------------------------------------

def test_p771_path_still_works_with_p772():
    """P7-72 must not break P7-71. The gap>5s + was_primed=True path
    (need_reprime=True) still requires en-Floor — unchanged.
    """
    printer, feeder = make_feeder()
    motion_q = printer.lookup_object('motion_queuing')

    # gap > 5s → need_reprime=True
    feeder.reactor.now = 12.00
    feeder._last_move_end_time = 5.20
    feeder._last_enable_schedule_time = 5.50
    feeder._stepcompress_primed = True
    feeder._current_move = None

    appends_before = len(motion_q.append_calls)
    feeder._submit_single_trapezoid(
        +1.0 * feeder.flush_callback_chunk_mm, feeder.feed_speed,
        streaming=True)

    own = _submitted_to_own_trapq(motion_q, feeder, appends_before)
    assert own, "expected streaming submit"
    t0 = own[0][1]

    # en-Floor must stay active. Both P7-71 (need_reprime=True) AND
    # P7-72 (stale_anchor=True) would activate it here.
    assert t0 >= 5.50 - 0.001, (
        "P7-72 broke P7-71: gap>5s + primed=True needs en-Floor, "
        "got t0=%.3f < en=5.50" % t0)
