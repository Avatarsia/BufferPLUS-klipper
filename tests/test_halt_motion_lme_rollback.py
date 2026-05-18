"""P7-74 — _halt_motion clamp `_last_move_end_time` to mcu_now
(Issue #29 Eifel-Joe Hypothese, Follow-up zu P7-72/P7-73).

Lücke die P7-72 nicht schließt
-------------------------------
P7-72 `stale_anchor=(_last_move_end_time <= mcu_now)` fängt nur den
Fall, in dem der Anker bereits in der Vergangenheit liegt. Im
AUTO-Streaming-Cycling-Pfad ist `_last_move_end_time` aber nach
einem mid-flight `_halt_motion` in der falschen Zukunft:

  1. AUTO-Streaming submittet 45mm-Chunk → trapq_append, end_time
     = mcu_now + 0.64s (45 mm / 70 mm s⁻¹). Stepcompress steppt los
     und schiebt last_step_clock vor.
  2. HALL1 fires bei ~9mm gefahren (~0.13s in den Chunk).
     `_enter_overflow` → `_halt_motion`. Pre-P7-74 clearisiert
     _halt_motion `_continuous_feed`, `_pending_remaining_mm`,
     `_pending_submit_chunk_cap`, `_feed_deadline_time` — aber
     NICHT `_last_move_end_time`. Der Wert bleibt auf dem geplanten
     Chunk-Ende stehen (mcu_now + 0.55s), obwohl der Stepper nur
     9mm gefahren ist.
  3. HALL1 rapid cleared (Bounce, ~50-200ms) → `_resume_after_-
     overflow` → nächster Submit. _stepcompress_primed bleibt True,
     _pending_disable wird vom _enable_stepper zurückgesetzt.
  4. Submit-Pfad: `t0 = max(forced_t0, _last_move_end_time, en,
     mcu_now)`. `_last_move_end_time` ist Fake-Future → gewinnt →
     t0 wird auf das geplante Ende eines Chunks gelegt, der NIE
     vollständig ausgespielt wurde.
  5. stepcompress.last_step_clock ist auf dem tatsächlich letzten
     Step (innerhalb der ersten 9mm) → MCU sieht einen Sprung von
     last_step_clock auf t0 = "Fake-Future" → MCU "Invalid
     sequence c=29" Shutdown.

P7-72 fängt das NICHT: dort ist `_last_move_end_time > mcu_now`
(Zukunft, nicht Vergangenheit) → `stale_anchor=False` → en-Floor
bleibt gedroppt → t0 = max(_last_move_end_time, 0, mcu_now) =
Fake-Future.

P7-74 Fix
---------
In `_halt_motion` zusätzlich `_last_move_end_time` auf `mcu_now`
clampen, wenn der Anker in der Zukunft liegt. Damit reflektiert
der Anker die tatsächlich gestoppte Position (Worst-Case: das
mid-flight gehaltene Chunk-Stück muss noch ausgespielt werden,
aber der Anker für den NÄCHSTEN Submit ist mcu_now — die echte
Halt-Position. Synergie mit P7-72: nach dem Clamp ist
`_last_move_end_time == mcu_now`, also `stale_anchor=True` →
en-Floor wird aktiv → safe Anker mit lead_time-Margin.

    mcu = self.stepper.get_mcu()
    mcu_now = mcu.estimated_print_time(self.reactor.monotonic())
    if self._last_move_end_time > mcu_now:
        self._last_move_end_time = mcu_now

Tags: Issue #29, Eifel-Joe Hypothese 2026-05-12, post-halt anchor,
Fake-Future, c=29 Invalid sequence.
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
# Test 1: Mid-flight halt → _last_move_end_time clamped to mcu_now
# ---------------------------------------------------------------------------

def test_halt_motion_clamps_future_anchor_to_mcu_now():
    """P7-74 core: when `_halt_motion` fires mid-flight (chunk
    end_time in the future relative to mcu_now), `_last_move_end_-
    time` must be clamped down to mcu_now. Otherwise the next
    streaming submit anchors on a Fake-Future that the stepcompress
    cursor never reached.
    """
    printer, feeder = make_feeder()

    # AUTO-Streaming setup: chunk in flight, end_time 0.55s in
    # future. mcu_now is mid-chunk.
    feeder.reactor.now = 5.20  # mcu_now
    feeder._last_move_end_time = 5.55  # planned chunk end (Fake-Future)
    feeder._current_move = {
        'end_time': 5.55, 'direction': 1.0,
        'distance': 45.0, 'speed': 70.0,
    }
    feeder._continuous_feed = True
    feeder._pending_remaining_mm = 30.0
    feeder._pending_submit_chunk_cap = 9.0

    # Pre-fix invariant: _last_move_end_time = 5.55 (Fake-Future).
    assert feeder._last_move_end_time == 5.55

    feeder._halt_motion()

    # P7-74: _last_move_end_time MUST be clamped to mcu_now (5.20).
    assert feeder._last_move_end_time == pytest.approx(5.20, abs=0.001), (
        "P7-74 broken: _halt_motion did not clamp _last_move_end_time "
        "to mcu_now. Got %.3f, expected 5.20 (mcu_now). Without clamp "
        "the next streaming submit anchors on Fake-Future → MCU "
        "'Invalid sequence c=29'." % feeder._last_move_end_time)

    # Existing _halt_motion side-effects unchanged.
    assert feeder._continuous_feed is False
    assert feeder._pending_remaining_mm == 0.0
    assert feeder._pending_submit_chunk_cap is None


# ---------------------------------------------------------------------------
# Test 2: Halt with past/equal anchor → unchanged (safe no-op)
# ---------------------------------------------------------------------------

def test_halt_motion_does_not_advance_past_anchor():
    """P7-74 must be a one-sided clamp: if `_last_move_end_time`
    already lies AT or BEFORE mcu_now (no chunk in flight, or chunk
    already drained), the value MUST NOT be advanced forward.
    Advancing it would create a phantom forward anchor and break
    P7-72's `stale_anchor` detection.
    """
    printer, feeder = make_feeder()

    feeder.reactor.now = 8.00
    feeder._last_move_end_time = 5.20  # already in the past
    feeder._current_move = None

    feeder._halt_motion()

    # P7-74: anchor stays at 5.20 (must not be pushed forward).
    assert feeder._last_move_end_time == pytest.approx(5.20, abs=0.001), (
        "P7-74 broke one-sided clamp: past anchor was advanced to "
        "%.3f. Must stay at 5.20 to preserve P7-72 stale_anchor "
        "detection." % feeder._last_move_end_time)


def test_halt_motion_anchor_equal_mcu_now_unchanged():
    """Edge case: _last_move_end_time == mcu_now → no change."""
    printer, feeder = make_feeder()

    feeder.reactor.now = 5.20
    feeder._last_move_end_time = 5.20

    feeder._halt_motion()

    assert feeder._last_move_end_time == pytest.approx(5.20, abs=0.001)


# ---------------------------------------------------------------------------
# Test 2b: Halt resets Schmitt-trigger hysteresis latch
# ---------------------------------------------------------------------------

def test_halt_motion_resets_modulator_hysteresis_latch():
    """A hard stop must clear the Schmitt-trigger feeding latch.

    Otherwise the next AUTO resume could inherit a stale "was
    feeding" state across OVERFLOW / JAM / PAUSE / CLEAR_JAM and use
    the stop-threshold path before the tracker/sensors have re-armed
    the modulator from live state.
    """
    printer, feeder = make_feeder()

    feeder._modulator_feeding = True
    feeder._post_full_bias_clamp = True
    feeder._post_full_h3_since = 12.34
    feeder._continuous_feed = True
    feeder._continuous_feed_direction = 1
    feeder._continuous_feed_speed = feeder.feed_speed
    feeder._pending_remaining_mm = 12.0

    feeder._halt_motion()

    assert feeder._modulator_feeding is False
    assert feeder._post_full_bias_clamp is False
    assert feeder._post_full_h3_since is None


# ---------------------------------------------------------------------------
# Test 3: Eifel-Joe rapid AUTO→OVERFLOW→AUTO cycle pattern
# ---------------------------------------------------------------------------

def test_rapid_auto_overflow_cycle_no_fake_future_anchor():
    """Reproduziert Eifels Cycling-Pattern (15 rapid AUTO→OVERFLOW
    →AUTO cycles mit mid-flight halt). Nach Patch sollten alle
    nachfolgenden t0-Anker konsistent sein (kein "Fake-Future").

    Pattern pro Cycle:
      1. AUTO-Streaming submittet 45mm-Chunk, end_time = mcu_now + 0.64
      2. ~9mm in den Chunk gefahren (~0.13s) → HALL1 → _halt_motion
      3. Wall-clock advances bis Chunk-Ende-Zeit überschritten
      4. HALL1 cleared → nächster Streaming-Submit
      5. t0 darf NIEMALS auf einer Fake-Future landen, die der
         Stepcompress-Cursor nicht erreicht hat.
    """
    printer, feeder = make_feeder()
    motion_q = printer.lookup_object('motion_queuing')

    feeder.reactor.now = 5.00
    feeder._last_move_end_time = 5.00
    feeder._commanded_pos = 0.0
    feeder._stepcompress_primed = True
    feeder._last_enable_schedule_time = 5.00

    fake_future_cycles = []

    for cycle in range(15):
        cycle_start = 5.00 + cycle * 1.0
        feeder.reactor.now = cycle_start

        # Streaming-Chunk in flight: 45mm @ 70mm/s = 0.64s ahead.
        planned_end = cycle_start + 0.64
        feeder._current_move = {
            'end_time': planned_end, 'direction': 1.0,
            'distance': 45.0, 'speed': 70.0,
        }
        feeder._last_move_end_time = planned_end
        feeder._continuous_feed = True

        # HALL1 fires mid-flight (~9mm = 0.13s in).
        feeder.reactor.now = cycle_start + 0.13
        mid_flight_mcu_now = feeder.reactor.now
        feeder._halt_motion()

        # P7-74 invariant: _last_move_end_time MUST be clamped down
        # from planned_end (Fake-Future) to mid_flight_mcu_now.
        assert feeder._last_move_end_time <= mid_flight_mcu_now + 0.001, (
            "Cycle %d: P7-74 broken — _last_move_end_time=%.3f "
            "remains in Fake-Future (mcu_now=%.3f, planned=%.3f)" % (
                cycle, feeder._last_move_end_time,
                mid_flight_mcu_now, planned_end))

        # Wall-clock advances past the planned-end (chunk drained on
        # the MCU even though we halted — no MCU-level cancel).
        feeder.reactor.now = planned_end + 0.05

        # Next streaming submit (after HALL1 cleared).
        appends_before = len(motion_q.append_calls)
        feeder._submit_single_trapezoid(
            +45.0, 70.0, streaming=True)

        own = _submitted_to_own_trapq(motion_q, feeder, appends_before)
        if not own:
            continue
        t0 = own[0][1]

        # P7-74 invariant: t0 must NOT land on the pre-halt Fake-
        # Future (planned_end). After the clamp + P7-72 stale_anchor
        # interaction, the en-Floor stays active → t0 >= en, which
        # itself is >= a sane mcu_now-based value.
        if t0 > planned_end + 0.001 and t0 < planned_end + 0.1:
            # Acceptable: future anchor from get_last_move_time path.
            # But t0 must NOT equal the stale planned_end exactly.
            pass
        # The crisp invariant: clamp leaves stale_anchor=True so
        # en-Floor kicks in. Without P7-74 t0 == planned_end exactly
        # (Fake-Future anchor abuttment).
        if abs(t0 - planned_end) < 0.001 and feeder.reactor.now > planned_end:
            fake_future_cycles.append((cycle, t0, planned_end))

    assert not fake_future_cycles, (
        "P7-74 broken: %d/15 cycles produced Fake-Future anchor at "
        "stale planned_end: %s" % (
            len(fake_future_cycles), fake_future_cycles[:3]))


# ---------------------------------------------------------------------------
# Test 4: Interaction with P7-72 stale_anchor guard
# ---------------------------------------------------------------------------

def test_p774_synergy_with_p772_stale_anchor():
    """P7-74 clamps `_last_move_end_time` to mcu_now. The very next
    submit then has `_last_move_end_time <= mcu_now` → P7-72's
    `stale_anchor=True` → en-Floor stays active → safe.
    """
    printer, feeder = make_feeder()
    motion_q = printer.lookup_object('motion_queuing')

    # AUTO-Streaming, mid-flight chunk:
    feeder.reactor.now = 5.20
    feeder._last_move_end_time = 5.55  # Fake-Future
    feeder._last_enable_schedule_time = 5.50
    feeder._stepcompress_primed = True
    feeder._current_move = {
        'end_time': 5.55, 'direction': 1.0,
        'distance': 45.0, 'speed': 70.0,
    }

    # Halt mid-flight → P7-74 clamp.
    feeder._halt_motion()

    assert feeder._last_move_end_time == pytest.approx(5.20, abs=0.001)

    # Next submit (still at same mcu_now): stale_anchor MUST be True
    # because _last_move_end_time (5.20) <= mcu_now (5.20).
    feeder._current_move = None  # halted, no chunk in flight

    appends_before = len(motion_q.append_calls)
    feeder._submit_single_trapezoid(
        +9.0, feeder.feed_speed, streaming=True)

    own = _submitted_to_own_trapq(motion_q, feeder, appends_before)
    assert own, "expected streaming submit"
    t0 = own[0][1]

    # en-Floor must be respected because stale_anchor=True triggered
    # by P7-74 clamp.
    assert t0 >= feeder._last_enable_schedule_time - 0.001, (
        "P7-74/P7-72 synergy broken: after halt clamp, stale_anchor "
        "should force en-Floor but t0=%.3f < en=%.3f." % (
            t0, feeder._last_enable_schedule_time))


# ---------------------------------------------------------------------------
# Test 5: Healthy streaming (no halt) — no regression
# ---------------------------------------------------------------------------

def test_healthy_streaming_no_regression():
    """P7-74 only fires when `_halt_motion` is called. Healthy
    streaming never calls it → no perf-impact.
    """
    printer, feeder = make_feeder()
    motion_q = printer.lookup_object('motion_queuing')

    feeder.reactor.now = 5.00
    feeder._last_move_end_time = 5.50  # chunk in flight, future
    feeder._last_enable_schedule_time = 5.30
    feeder._stepcompress_primed = True
    feeder._current_move = {
        'end_time': 5.50, 'direction': 1.0,
        'distance': 9.0, 'speed': feeder.feed_speed,
    }

    # No _halt_motion called: anchor stays at 5.50.
    appends_before = len(motion_q.append_calls)
    feeder._submit_single_trapezoid(
        +9.0, feeder.feed_speed, streaming=True)

    own = _submitted_to_own_trapq(motion_q, feeder, appends_before)
    assert own
    t0 = own[0][1]

    # P7-66 abuttment intact: t0 == _last_move_end_time = 5.50.
    assert t0 == pytest.approx(5.50, abs=0.001), (
        "P7-74 regression in healthy path: t0=%.3f, expected 5.50 "
        "(abuttment). P7-74 must only act inside _halt_motion." % t0)
