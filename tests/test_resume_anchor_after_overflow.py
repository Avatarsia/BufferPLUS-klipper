"""P7-67 — Resume-Anchor-Fix für post-OVERFLOW LOAD_PHASE_1 (Issue #29).

Regression von P7-66 R1
-----------------------
P7-66 R1 hat in `_submit_single_trapezoid` zwei Optimierungen für den
Streaming-Lookahead-Pfad eingeführt:

    if not streaming:
        self._enable_stepper()
    ...
    en = 0.0 if streaming else self._last_enable_schedule_time

Annahme R1: "Wenn streaming=True, ist der Motor bereits enabled (vom
laufenden Chunk in flight) → kein erneuter Enable nötig, und der
en-Floor würde nur den abuttend-Anker brechen."

Das stimmt im AUTO-Streaming-Pfad (`_on_mcu_flush` Lookahead +
`_tick_pending_chunk` ohne Disable-Zyklus). Es stimmt NICHT im
LOAD_PHASE_1/Resume-Pfad: zwischen Sub-Chunks läuft ein
`_disable_stepper()` → `_enable_stepper()`-Zyklus (z.B. bei
OVERFLOW-Recovery). `_disable_stepper` clear-t
`_stepcompress_primed=False`. Beim nächsten `_submit_single_trapezoid`
führt der Reprime-Block `flush_step_generation()` +
`set_position((0,0,0))` aus, der Stepcompress-Cursor wird komplett neu
aufgesetzt — und mit `en=0.0` landet `t0` direkt auf
`_last_move_end_time`. Das erste Trapezoid hat dann ein Clock-Delta
von ~0 vom letzten step_clock → MCU schreit "stepcompress … Invalid
sequence" und shutdown.

Fix P7-67 (Achse A)
-------------------
In `_submit_single_trapezoid` wird der en-Floor nur dann gedropt,
wenn `was_primed=True` (Snapshot VOR Reprime). Bei `was_primed=False`
greift der reguläre Floor `_last_enable_schedule_time`, der durch den
vorherigen `_enable_stepper()` mit ausreichendem `lead_time`-Margin
versorgt wurde.

Tags relevant: post-overflow resume, primed=False edge case,
stepcompress invalid sequence, LOAD_PHASE_1.
"""

import pytest

from fakes_klipper import FakeConfig, FakePrinter, FakePrintStats
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
    printer.objects["print_stats"] = FakePrintStats(state="printing")
    config = FakeConfig(printer=printer, values=base)
    feeder = buffer_feeder.BufferFeeder(config)
    # P7-66 R6: wire _stepper_enable via _handle_connect.
    printer.fire_event('klippy:connect')
    feeder._startup_grace_done = True
    feeder._state = buffer_feeder.STATE_AUTO
    set_sensor_active(feeder, 'hall_overflow', False)
    set_sensor_active(feeder, 'hall_full', False)
    set_sensor_active(feeder, 'hall_empty', False)
    return printer, feeder


def _submitted_to_own_trapq(motion_q, feeder, start_index):
    """Return only trapq_append calls targeting our own feeder trapq."""
    return [c for c in motion_q.append_calls[start_index:]
            if c[0] is feeder.trapq]


# ---------------------------------------------------------------------------
# CORE: en-floor selection vs. was_primed.
# ---------------------------------------------------------------------------

def test_streaming_primed_drops_en_floor():
    """P7-66 performance optimisation preserved: when streaming=True
    AND the stepcompress cursor was already primed on entry, the
    en-floor is dropped → t0 abuts at _last_move_end_time, no
    inter-chunk lead_time gap. This is the regular AUTO-streaming
    path."""
    printer, feeder = make_feeder()
    motion_q = printer.lookup_object('motion_queuing')

    # Setup: previous chunk in flight, cursor primed, en stale-in-future.
    feeder.reactor.now = 5.00
    feeder._last_move_end_time = 5.20
    feeder._last_enable_schedule_time = 5.50  # > _last_move_end_time
    feeder._stepcompress_primed = True
    feeder._current_move = {
        'end_time': 5.20, 'direction': 1.0,
        'distance': feeder.flush_callback_chunk_mm,
        'speed': feeder.feed_speed,
    }

    appends_before = len(motion_q.append_calls)
    feeder._submit_single_trapezoid(
        +1.0 * feeder.flush_callback_chunk_mm, feeder.feed_speed,
        streaming=True)

    own = _submitted_to_own_trapq(motion_q, feeder, appends_before)
    assert own, "expected streaming submit"
    t0 = own[0][1]
    # Without forced_t0 + _last_move_end_time > mcu_now+lead_time path:
    # streaming-abut branch → t0 = max(_last_move_end_time, en).
    # P7-66: en was dropped to 0.0 → t0 = 5.20. P7-67 keeps that for
    # primed=True.
    assert t0 == pytest.approx(5.20, abs=0.001), (
        "P7-67 streaming+primed=True: en-floor must be dropped → "
        "t0 == _last_move_end_time. Got t0=%.3f" % t0)


def test_streaming_not_primed_keeps_en_floor():
    """P7-67 fix: when streaming=True but stepcompress_primed=False
    on entry, the en-floor must be kept to guarantee lead_time margin
    after the reprime block."""
    printer, feeder = make_feeder()
    motion_q = printer.lookup_object('motion_queuing')

    # Resume-after-disable scenario: cursor un-primed, en is the
    # _last_enable_schedule_time set by the most recent
    # _enable_stepper() (after the prior _disable_stepper).
    # _last_move_end_time is stale (from the pre-disable session) —
    # using it directly without lead_time margin is the bug we fix.
    feeder.reactor.now = 5.00
    feeder._last_move_end_time = 5.20
    feeder._last_enable_schedule_time = 5.50  # = re-enable + lead_time
    feeder._stepcompress_primed = False  # <-- post-disable state
    feeder._current_move = None  # no in-flight move after disable

    appends_before = len(motion_q.append_calls)
    feeder._submit_single_trapezoid(
        +1.0 * feeder.flush_callback_chunk_mm, feeder.feed_speed,
        streaming=True)

    own = _submitted_to_own_trapq(motion_q, feeder, appends_before)
    assert own, "expected resume submit"
    t0 = own[0][1]
    # With en-floor kept: t0 >= en (5.50). Without fix: t0 = 5.20.
    assert t0 >= 5.50 - 0.001, (
        "P7-67 broken: streaming+primed=False landed t0=%.3f below "
        "_last_enable_schedule_time=5.50 — lead_time margin missing → "
        "stepcompress Invalid sequence regression." % t0)


def test_non_streaming_keeps_en_floor_unchanged():
    """Non-streaming (default) path must keep the en-floor regardless
    of primed state — this is the pre-P7-66 baseline behaviour. P7-67
    does not touch this path."""
    printer, feeder = make_feeder()
    motion_q = printer.lookup_object('motion_queuing')

    feeder.reactor.now = 5.00
    feeder._last_move_end_time = 5.20
    feeder._last_enable_schedule_time = 0.0  # will be bumped by _enable_stepper
    feeder._stepcompress_primed = True
    feeder._current_move = None

    appends_before = len(motion_q.append_calls)
    feeder._submit_single_trapezoid(
        +10.0, feeder.feed_speed, streaming=False)

    own = _submitted_to_own_trapq(motion_q, feeder, appends_before)
    assert own, "expected non-streaming submit"
    t0 = own[0][1]
    # Non-streaming path calls _enable_stepper(), which pushes
    # _last_enable_schedule_time to >= mcu_now + lead_time. t0 must
    # honour that floor.
    assert t0 >= feeder._last_enable_schedule_time - 0.001, (
        "Non-streaming path violated en-floor: t0=%.3f < "
        "_last_enable_schedule_time=%.3f" % (
            t0, feeder._last_enable_schedule_time))


# ---------------------------------------------------------------------------
# Direct simulation of the Issue #29 LOAD_PHASE_1 sequence.
# ---------------------------------------------------------------------------

def test_load_phase1_resume_after_overflow_keeps_lead_time_margin():
    """Reproduces Issue #29 sequence:

      1. AUTO-streaming chunk in flight.
      2. Mid-chunk HALL1 → OVERFLOW → _schedule_stepper_disable() →
         _disable_stepper() (since not in flight in test setup) →
         _stepcompress_primed=False.
      3. HALL1 cleared → _enable_stepper() bumps
         _last_enable_schedule_time forward by lead_time.
      4. Subsequent _submit_single_trapezoid(streaming=True) for the
         continuation of the pending move.

    PRE-FIX (P7-66 R1): t0 == _last_move_end_time, no margin → MCU
                        shutdown "stepcompress … Invalid sequence".
    POST-FIX (P7-67):   t0 >= _last_enable_schedule_time, margin
                        preserved.
    """
    printer, feeder = make_feeder()
    motion_q = printer.lookup_object('motion_queuing')

    # 1. Simulate a prior streaming chunk that already ran.
    feeder._last_move_end_time = 5.20
    feeder._commanded_pos = 9.0  # one sub-chunk in
    feeder._stepcompress_primed = True

    # 2. OVERFLOW → disable. Use the public path to mirror reality.
    feeder.reactor.now = 5.21
    feeder._current_move = None  # in-flight chunk finished playing out
    feeder._disable_stepper()
    assert feeder._stepcompress_primed is False, (
        "_disable_stepper must clear _stepcompress_primed (precondition "
        "for the bug — see line ~2618 in buffer_feeder.py)")

    # 3. HALL1 cleared → re-enable. _enable_stepper bumps
    #    _last_enable_schedule_time = max(... + lead_time).
    feeder.reactor.now = 6.00  # later wall-clock after overflow lockout
    enable_handle = feeder._stepper_enable
    enables_before = len(enable_handle.enables)
    feeder._enable_stepper()
    assert len(enable_handle.enables) == enables_before + 1, (
        "Re-enable failed in test setup — check _stepper_enable wiring")
    en_after_reenable = feeder._last_enable_schedule_time
    assert en_after_reenable >= 6.0 + feeder.lead_time - 0.01, (
        "Re-enable did not push _last_enable_schedule_time forward "
        "by lead_time (got %.3f, expected >= %.3f)" % (
            en_after_reenable, 6.0 + feeder.lead_time))

    # 4. Resume submit: streaming=True (continuation of pending chunk),
    #    primed=False (we just disabled+re-enabled, no submit between).
    appends_before = len(motion_q.append_calls)
    feeder._submit_single_trapezoid(
        +9.0, feeder.feed_speed, streaming=True)

    own = _submitted_to_own_trapq(motion_q, feeder, appends_before)
    assert own, "expected resume submit"
    t0 = own[0][1]

    # P7-67 assertion: t0 must be >= the re-enabled schedule time.
    # Without the fix t0 would equal _last_move_end_time = 5.20 (stale,
    # well below 6.0 + lead_time = 6.3) → MCU "Invalid sequence".
    assert t0 >= en_after_reenable - 0.001, (
        "P7-67 broken: LOAD_PHASE_1 resume after OVERFLOW landed "
        "t0=%.3f below the re-enable schedule time %.3f. The "
        "lead_time margin between motor_enable and the first step "
        "is gone → MCU 'stepcompress Invalid sequence' regression "
        "(Issue #29)." % (t0, en_after_reenable))

    # Cursor must have been re-primed by the submit.
    assert feeder._stepcompress_primed is True, (
        "submit must reprime stepcompress on primed=False entry")


# ---------------------------------------------------------------------------
# Guard: P7-66 streaming performance preserved when primed.
# ---------------------------------------------------------------------------

def test_p7_66_streaming_performance_preserved_with_primed_true():
    """Guarantees P7-67 does not regress the P7-66 streaming gap fix
    for the normal AUTO+streaming path. Same setup as
    test_streaming_anchor_unaffected_by_stale_enable_schedule_time
    from test_p7_66_streaming.py but explicitly asserts the t0 ==
    _last_move_end_time outcome remains intact when was_primed=True."""
    printer, feeder = make_feeder()
    motion_q = printer.lookup_object('motion_queuing')
    set_sensor_active(feeder, 'hall_empty', True)

    feeder.reactor.now = 5.00
    feeder._continuous_feed = True
    feeder._continuous_feed_direction = 1
    feeder._continuous_feed_speed = feeder.feed_speed
    feeder._last_move_end_time = 5.20
    feeder._last_enable_schedule_time = 5.20 + feeder.lead_time
    feeder._stepcompress_primed = True  # <- key guard
    feeder._current_move = {
        'end_time': 5.20, 'direction': 1.0,
        'distance': feeder.flush_callback_chunk_mm,
        'speed': feeder.feed_speed,
    }

    appends_before = len(motion_q.append_calls)
    motion_q.trigger_flush(flush_time=5.00, step_gen_time=5.05)
    own = _submitted_to_own_trapq(motion_q, feeder, appends_before)
    assert own, "lookahead submit missing"
    t0 = own[0][1]
    # With was_primed=True: en floor dropped → t0 abuts at 5.20.
    # P7-66 inter-chunk gap fix stays intact.
    assert t0 == pytest.approx(5.20, abs=0.001), (
        "P7-66 streaming abuttment regressed under P7-67: t0=%.3f, "
        "expected 5.20. Lead_time gap re-opened despite "
        "primed=True." % t0)
