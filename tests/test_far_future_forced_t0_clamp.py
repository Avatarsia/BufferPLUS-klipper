"""P7-73 — Far-future forced_t0 clamp (Issue #31 Eifel-Joe Print-Start).

Reproduziert Eifel-Joes "Timer too close beim ersten Bang-Bang-Submit
nach Print-Start"-Crash und beweist, dass der defensive Clamp im
forced_t0-Branch von `_submit_single_trapezoid` das far-future anchor
unschädlich macht.

Pre-Fix-Pfad (P7-72-Stand)
--------------------------
1. Klipper-Boot → BufferFeeder._anchor_step erzeugt 0.05mm-Step bei
   MCU-Boot-Clock, `_stepcompress_primed=True`, `_last_move_end_time
   ≈ 0.5s` (Boot-Anchor-Ende), `last_step_clock` etabliert.
2. ~7s später User startet Druck via virtual_sdcard.
3. Toolhead füllt seine GCode-Queue mit Heizmoves/PRINT_START-Macros
   → `print_time` schiebt sich auf 60-100s in die Zukunft.
4. motion_queuing.flush_all_steps() ruft Flush-Callbacks mit
   `step_gen_time = need_step_gen_time` (≈ Toolhead-Queue-Ende) auf.
5. `_on_mcu_flush` baut `anchor = step_gen_time + lead_time` → 60-100s
   ahead, reicht das als forced_t0 an `_submit_single_trapezoid`.
6. Dort gewinnt forced_t0 im `t0 = max(forced_t0, _last_move_end_time,
   en, mcu_now)` und trapq_append landet 60-100s in der Zukunft.
7. last_step_clock vom Boot-Anchor bleibt zurück → erstes queue_step-
   Intervall wird 60-100s × 48 MHz = ~3-5 Mrd Ticks → überschreitet
   uint32 (~89.5s @ 48 MHz) bzw. signed int32 (~44.7s) → MCU
   "Timer too close"-Shutdown auf LLL_PLUS.

Smoking-Gun aus Eifel-Joes Hardware-Log:
  - P7-70 (lead_time=0.30): queue_step interval=2340612897 = 48.76s
  - P7-71 (lead_time=0.12): queue_step interval=4166339230 = 86.8s

P7-73 Fix
---------
Defensiver Upper-Bound-Clamp im forced_t0-Branch:

    MAX_FORCED_T0_LOOKAHEAD = 2.0  # s
    if forced_t0 > mcu_now + MAX_FORCED_T0_LOOKAHEAD:
        logging.warning(...)
        forced_t0 = mcu_now + self.lead_time

Im gesunden Betrieb ist `step_gen_time ≈ mcu_now + ~0.25s`, also
`anchor = step_gen_time + lead_time ≈ mcu_now + 0.55s` — weit unter
dem 2s-Cap. Der Clamp ist inert. NUR im degenerate Print-Start-Fall
(Toolhead-Queue 60-100s voraus) greift er, ersetzt das far-future
forced_t0 durch einen sicheren `mcu_now + lead_time`-Anker.

Tags: Issue #31, Eifel-Joe Print-Start, far-future flush, int32 timer
overflow, defensive guard.
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
# Test 1: Far-future forced_t0 — Eifel-Joe Print-Start reproduction
# ---------------------------------------------------------------------------

def test_far_future_forced_t0_clamped():
    """Eifel-Joe Issue #31 Pfad: ~7s nach Boot startet ein Druck, der
    Toolhead füllt 60-100s Heizmoves in seine Queue, motion_queuing
    reicht `step_gen_time = need_step_gen_time` (Toolhead-Queue-Ende)
    durch, `_on_mcu_flush` baut forced_t0 ≈ 90s. Ohne Clamp landet
    trapq_append 83s ahead of mcu_now, queue_step-Intervall >>
    int32 → MCU Shutdown.

    P7-73 erwartet: forced_t0 wird auf mcu_now + lead_time geklemmt,
    t0 landet ~7.3s (= mcu_now=7.0 + lead_time=0.3).
    """
    printer, feeder = make_feeder()
    motion_q = printer.lookup_object('motion_queuing')

    # Setup: 7s nach Boot, Boot-Anchor noch _last_move_end_time≈0.5,
    # Stepcompress primed, kein Move in flight.
    feeder.reactor.now = 7.0  # mcu_now = 7.0s
    feeder._last_move_end_time = 0.5  # Boot-Anchor-Ende
    feeder._last_enable_schedule_time = 0.0
    feeder._stepcompress_primed = True
    feeder._current_move = None

    appends_before = len(motion_q.append_calls)
    # Print-Start-Szenario: forced_t0 = 90.0 = 83s in der Zukunft.
    # = simulates anchor = step_gen_time(~89.7) + lead_time(0.3)
    # bei print_time-Spitze ~90s aus Heiz-/PRINT_START-Macros.
    feeder._submit_single_trapezoid(
        15.0, feeder.feed_speed, forced_t0=90.0)

    own = _submitted_to_own_trapq(motion_q, feeder, appends_before)
    assert own, "expected submit"
    t0 = own[0][1]

    # P7-73 expects: forced_t0 geklemmt → t0 ≤ mcu_now + lead_time + ε
    # (Cap-Fallback ist mcu_now + lead_time = 7.3).
    mcu_now = 7.0
    assert t0 <= mcu_now + feeder.lead_time + 0.01, (
        "P7-73 broken: far-future forced_t0=90.0 not clamped — "
        "t0=%.3f, mcu_now=%.3f, expected t0 <= %.3f. Pre-fix this "
        "would trip MCU 'Timer too close' (Issue #31)." % (
            t0, mcu_now, mcu_now + feeder.lead_time))


# ---------------------------------------------------------------------------
# Test 2: Healthy near-future forced_t0 — NOT clamped (regression guard)
# ---------------------------------------------------------------------------

def test_healthy_near_future_forced_t0_not_clamped():
    """Im normalen flush-callback-Betrieb liegt step_gen_time nur
    ~0.25s über mcu_now → anchor = step_gen_time + lead_time ≈
    mcu_now + 0.55s. Das muss UNGEKLEMMT durchgehen, sonst zerstört
    P7-73 den P7-52 race-free-Anker.
    """
    printer, feeder = make_feeder()
    motion_q = printer.lookup_object('motion_queuing')

    feeder.reactor.now = 5.0  # mcu_now = 5.0
    feeder._last_move_end_time = 0.5
    feeder._last_enable_schedule_time = 0.0
    feeder._stepcompress_primed = True
    feeder._current_move = None

    appends_before = len(motion_q.append_calls)
    # Healthy anchor: mcu_now + 0.55s = 5.55 (= step_gen_time + lead_time
    # bei step_gen_time = mcu_now + 0.25).
    feeder._submit_single_trapezoid(
        15.0, feeder.feed_speed, forced_t0=5.55)

    own = _submitted_to_own_trapq(motion_q, feeder, appends_before)
    assert own, "expected submit"
    t0 = own[0][1]

    # Must NOT be clamped — forced_t0 wins in the max().
    assert t0 == pytest.approx(5.55, abs=0.001), (
        "P7-73 regression: healthy forced_t0=5.55 (mcu_now+0.55) "
        "should pass through unchanged, got t0=%.3f" % t0)


# ---------------------------------------------------------------------------
# Test 3: Boundary case — forced_t0 == mcu_now + cap (NOT clamped)
# ---------------------------------------------------------------------------

def test_boundary_forced_t0_at_cap_not_clamped():
    """Boundary: forced_t0 = mcu_now + MAX_FORCED_T0_LOOKAHEAD (2.0).
    Strict ">"-Check im Clamp → diese Stelle bleibt UNGEKLEMMT.
    Stellt sicher dass der Cap exklusiv ist und legitime Edge-Cases
    nicht unnötig clampen.
    """
    printer, feeder = make_feeder()
    motion_q = printer.lookup_object('motion_queuing')

    feeder.reactor.now = 5.0
    feeder._last_move_end_time = 0.5
    feeder._last_enable_schedule_time = 0.0
    feeder._stepcompress_primed = True
    feeder._current_move = None

    appends_before = len(motion_q.append_calls)
    # Exactly at cap: mcu_now + 2.0 = 7.0.
    feeder._submit_single_trapezoid(
        15.0, feeder.feed_speed, forced_t0=7.0)

    own = _submitted_to_own_trapq(motion_q, feeder, appends_before)
    assert own, "expected submit"
    t0 = own[0][1]

    # Must NOT be clamped (strict ">"-Check).
    assert t0 == pytest.approx(7.0, abs=0.001), (
        "P7-73 boundary broken: forced_t0=mcu_now+cap should pass "
        "through (strict '>'), got t0=%.3f" % t0)


# ---------------------------------------------------------------------------
# Test 4: Just-past-cap — IS clamped
# ---------------------------------------------------------------------------

def test_just_past_cap_forced_t0_clamped():
    """forced_t0 = mcu_now + 2.5 (knapp über Cap=2.0) MUSS geklemmt
    werden. Stellt sicher dass der Cap konsistent greift, sobald die
    Schwelle überschritten ist.
    """
    printer, feeder = make_feeder()
    motion_q = printer.lookup_object('motion_queuing')

    feeder.reactor.now = 5.0
    feeder._last_move_end_time = 0.5
    feeder._last_enable_schedule_time = 0.0
    feeder._stepcompress_primed = True
    feeder._current_move = None

    appends_before = len(motion_q.append_calls)
    # Just past cap: mcu_now + 2.5 = 7.5.
    feeder._submit_single_trapezoid(
        15.0, feeder.feed_speed, forced_t0=7.5)

    own = _submitted_to_own_trapq(motion_q, feeder, appends_before)
    assert own, "expected submit"
    t0 = own[0][1]

    # Must be clamped: forced_t0 → mcu_now + lead_time = 5.3.
    mcu_now = 5.0
    assert t0 <= mcu_now + feeder.lead_time + 0.01, (
        "P7-73 broken: forced_t0=7.5 (mcu_now+2.5, past cap) not "
        "clamped — t0=%.3f, expected <= %.3f" % (
            t0, mcu_now + feeder.lead_time))


# ---------------------------------------------------------------------------
# Test 5: Stale future floors must not override a healthy forced_t0
# ---------------------------------------------------------------------------

def test_stale_future_floors_do_not_override_healthy_forced_t0():
    """A healthy flush anchor must still win when the internal floors
    are stale and far in the future.

    This is the gap left by the original P7-73 guard: clamping
    `forced_t0` alone is not enough if `_last_move_end_time` or
    `_last_enable_schedule_time` are already corrupted and no move is
    actually in flight.
    """
    printer, feeder = make_feeder()
    motion_q = printer.lookup_object('motion_queuing')

    feeder.reactor.now = 5.0  # mcu_now
    feeder._last_move_end_time = 25.0
    feeder._last_enable_schedule_time = 26.0
    feeder._stepcompress_primed = True
    feeder._current_move = None

    appends_before = len(motion_q.append_calls)
    feeder._submit_single_trapezoid(
        15.0, feeder.feed_speed, forced_t0=5.55)

    own = _submitted_to_own_trapq(motion_q, feeder, appends_before)
    assert own, "expected submit"
    t0 = own[0][1]

    assert t0 == pytest.approx(5.55, abs=0.01), (
        "forced_t0 guard broken: stale future floors overrode a "
        "healthy forced_t0=5.55. Got t0=%.3f." % t0)
    assert feeder._last_enable_schedule_time <= 5.0 + feeder.lead_time + 0.01, (
        "forced_t0 guard broken: stale _last_enable_schedule_time was "
        "not sanitized before enable_stepper. Got %.3f." % (
            feeder._last_enable_schedule_time))


# ---------------------------------------------------------------------------
# Test 6: Post-clamp queue_step interval is within int32 timer range
# ---------------------------------------------------------------------------

def test_post_clamp_queue_step_interval_within_int32():
    """Beweist dass der Clamp das eigentliche MCU-Symptom verhindert:
    queue_step-Intervall (= delta zwischen last_step_clock und
    erstem Step im trapq_append) muss nach dem Clamp deutlich unter
    int32-signed (44.7s @ 48 MHz = ~2.14 Mrd Ticks) liegen.

    Smoking-Gun aus Issue #31 Log:
      P7-71 (lead_time=0.12): interval=4166339230 = 86.8s @ 48 MHz
      → out of uint32 signed.
    """
    printer, feeder = make_feeder()
    motion_q = printer.lookup_object('motion_queuing')

    # Boot-Anchor-Setup: last_step_clock entspricht
    # _last_move_end_time = 0.5 (Boot-Anchor-Ende). mcu_now = 7.0
    # (7s nach Boot, Print-Start passiert jetzt).
    feeder.reactor.now = 7.0
    feeder._last_move_end_time = 0.5
    feeder._last_enable_schedule_time = 0.0
    feeder._stepcompress_primed = True
    feeder._current_move = None

    appends_before = len(motion_q.append_calls)
    # Far-future anchor wie im Hardware-Log.
    feeder._submit_single_trapezoid(
        15.0, feeder.feed_speed, forced_t0=90.0)

    own = _submitted_to_own_trapq(motion_q, feeder, appends_before)
    assert own, "expected submit"
    t0 = own[0][1]

    # Differenz t0 - _last_move_end_time approximiert das queue_step-
    # Intervall (in Sekunden, vor Multiplikation mit MCU-Frequenz).
    # Bei 48 MHz Standard-LLL-Plus-Takt:
    MCU_FREQ_HZ = 48_000_000
    INT32_SIGNED_MAX = 2**31 - 1  # 2147483647

    # last_step_clock im Stepcompress entspricht Boot-Anchor-Ende
    # (0.5s in unserem Setup). Worst-case queue_step-Intervall =
    # (t0 - 0.5) * MCU_FREQ_HZ.
    interval_seconds = t0 - 0.5
    interval_ticks = int(interval_seconds * MCU_FREQ_HZ)

    assert interval_ticks < INT32_SIGNED_MAX, (
        "P7-73 broken: queue_step interval after clamp = %d ticks "
        "= %.2fs @ 48 MHz, exceeds int32 signed (%d). Pre-fix this "
        "was the exact MCU 'Timer too close' trigger (Issue #31)." % (
            interval_ticks, interval_seconds, INT32_SIGNED_MAX))
    # Sanity: well below cap (should be ~7s − 0.5s = ~6.5s post-clamp,
    # because Cap-Fallback = mcu_now + lead_time = 7.3, minus
    # _last_move_end_time=0.5 in the trapq base).
    assert interval_seconds < 10.0, (
        "P7-73 clamp didn't reduce interval enough: %.2fs" % interval_seconds)
