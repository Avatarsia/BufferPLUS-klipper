"""P7-70 — Idle-Watchdog reprimes stepcompress before CLOCK_DIFF_MAX hits.

Fixes Issue #12: "stepcompress Invalid sequence" MCU shutdown after
UNLOAD → IDLE when the printer sits idle longer than ~17 s.

Root cause (verified at code level before the fix):
- `_submit_single_trapezoid` carries a REPRIME-on-gap branch
  (`gap > REPRIME_GAP=5.0`) but it only runs when a NEW move is
  submitted. After UNLOAD drains to STATE_IDLE the buffer-feeder
  emits no further moves. `_last_move_end_time` freezes.
- Klipper's background `flush_handler` keeps firing on the
  syncemitter. Once it crosses ~17 s past `_last_move_end_time`
  (Klipper's CLOCK_DIFF_MAX = 3<<28 ticks @ 48 MHz),
  `compress_bisect_add` degenerates into the documented
  "Invalid sequence" sequence → MCU shutdown.

Fix: In `_main_tick`, while in STATE_IDLE (and not synced, not
disable-pending, not still draining a prior move), refresh
last_step_clock via a 0.05 mm micro-anchor whenever
`mcu_now - _last_move_end_time > idle_anchor_gap`. Pattern is
the well-tested boot-anchor / SYNC-gap-anchor primitive — same
0.05 mm direction-aware step, same _submit_anchor_move() helper.

A second `_last_idle_anchor_time` gate prevents back-to-back anchors
on every tick after the first trip.
"""

import pytest

from fakes_klipper import FakeConfig, FakePrinter
from klipper_extras import buffer_feeder


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def set_sensor_active(feeder, sensor_name, active):
    """P7-49 helper duplicated to keep this test file self-contained."""
    polarity_flip = feeder._pin_polarity_flip[sensor_name]
    raw = (not active) if polarity_flip else active
    feeder._pin_stable_state[sensor_name] = raw
    feeder._pin_raw_state[sensor_name] = raw


def make_idle_feeder(values=None):
    """Feeder in STATE_IDLE, grace done, sensors quiet."""
    printer = FakePrinter()
    config = FakeConfig(printer=printer, values=values)
    feeder = buffer_feeder.BufferFeeder(config)
    feeder._startup_grace_done = True
    feeder._state = buffer_feeder.STATE_IDLE
    # All HALL inactive so _main_tick passes the HALL1 lockout check
    # and the cooldown/grip ticks don't interfere.
    set_sensor_active(feeder, 'hall_overflow', False)
    set_sensor_active(feeder, 'hall_full', False)
    set_sensor_active(feeder, 'hall_empty', False)
    set_sensor_active(feeder, 'entrance', False)
    return printer, feeder


def count_anchor_calls(monkeypatch, feeder):
    """Wrap sync._submit_anchor_move so each call is observable."""
    calls = []
    original = feeder.sync._submit_anchor_move

    def _spy():
        calls.append(feeder.stepper.get_mcu().estimated_print_time(
            feeder.reactor.monotonic()))
        # Don't actually run the real anchor (FakeStepper sequence
        # would set position; we only care about call counts). Mirror
        # the side effect the real call has on _last_move_end_time
        # so the watchdog's gate stays consistent.
        feeder._last_move_end_time = calls[-1] + 0.001
        return -1.0 if feeder.hall_overflow else 1.0

    monkeypatch.setattr(feeder.sync, "_submit_anchor_move", _spy)
    return calls


# ---------------------------------------------------------------------------
# Characterization: PRE-FIX behaviour
# ---------------------------------------------------------------------------


def test_pre_fix_no_anchor_on_long_idle_when_watchdog_disabled(monkeypatch):
    """Characterization test for the PRE-fix behaviour.

    Simulate "no watchdog" by setting `idle_anchor_gap` so large the
    gate can never trip during this test (999999 s). With that gate
    closed the IDLE-watchdog block has the same observable behaviour
    as the legacy code did — no anchor fires, and `_last_move_end_-
    time` stays frozen 20 s in the past, exactly the path that
    crashed the MCU after CLOCK_DIFF_MAX in Issue #12.

    This preserves a regression baseline: if anyone ever short-
    circuits the watchdog (e.g. by deleting the block), the
    behaviour test below will fail; but THIS test will still pass,
    documenting "what the old code did" forever.
    """
    _, feeder = make_idle_feeder(values={'idle_anchor_gap': 999999.0})
    calls = count_anchor_calls(monkeypatch, feeder)

    feeder._last_move_end_time = -20.0  # mcu_now ~0, gap 20 s

    feeder._main_tick(eventtime=0.0)

    assert calls == [], (
        "PRE-FIX baseline: with idle_anchor_gap raised out of reach, "
        "no anchor fires on idle gap — the original Issue #12 path.")


# ---------------------------------------------------------------------------
# Behaviour: POST-FIX watchdog fires under the right conditions
# ---------------------------------------------------------------------------


def test_idle_long_gap_fires_anchor(monkeypatch):
    """Post-fix path: IDLE + gap > idle_anchor_gap → exactly one anchor.

    The FakeMCU's `estimated_print_time` returns eventtime as-is, and
    the FakeReactor's `monotonic` returns the current `now`. We push
    `now` forward past idle_anchor_gap and verify the watchdog
    trips.
    """
    _, feeder = make_idle_feeder()  # default idle_anchor_gap=10.0
    calls = count_anchor_calls(monkeypatch, feeder)

    # Advance the reactor clock to 20 s; last move ended at t=0.
    feeder.reactor.now = 20.0
    feeder._last_move_end_time = 0.0

    feeder._main_tick(eventtime=20.0)

    assert len(calls) == 1, (
        "Watchdog must fire exactly one anchor when "
        "mcu_now - _last_move_end_time > idle_anchor_gap.")


def test_idle_short_gap_does_not_fire(monkeypatch):
    """IDLE + gap < idle_anchor_gap → no anchor (default 10 s window)."""
    _, feeder = make_idle_feeder()
    calls = count_anchor_calls(monkeypatch, feeder)

    feeder.reactor.now = 5.0
    feeder._last_move_end_time = 0.0

    feeder._main_tick(eventtime=5.0)

    assert calls == [], (
        "Watchdog must NOT fire while gap stays under idle_anchor_gap.")


def test_state_auto_with_active_bangbang_does_not_fire(monkeypatch):
    """P7-75 (Issue #31) erweiterte den Watchdog auf STATE_AUTO, ABER
    nur wenn Bang-Bang quiescent ist. Mit aktiver Bang-Bang-Session
    (_continuous_feed=True) muss der Watchdog Hände weg lassen —
    Bang-Bang owns the cursor management dort.

    Der frühere Test 'test_state_auto_long_gap_does_not_fire' war eine
    Aussage über das alte P7-70-Verhalten (STATE_AUTO immer tabu) und
    wurde durch P7-75 obsolet. Die AUTO-quiescent-Variante wird in
    test_p775_auto_watchdog.py geprüft."""
    _, feeder = make_idle_feeder()
    feeder._state = buffer_feeder.STATE_AUTO
    feeder._continuous_feed = True  # Bang-Bang aktiv
    # Entrance must be present for AUTO to be a valid state at this
    # point of the tick, otherwise the _bang_bang_tick might shuffle
    # things around. We patch out the bang-bang tick to keep the
    # observation surface narrow.
    calls = count_anchor_calls(monkeypatch, feeder)
    monkeypatch.setattr(feeder, "_bang_bang_tick", lambda et: None)

    feeder.reactor.now = 30.0
    feeder._last_move_end_time = 0.0

    feeder._main_tick(eventtime=30.0)

    assert calls == [], (
        "Watchdog must NOT fire in STATE_AUTO while bang-bang is "
        "actively streaming chunks (_continuous_feed=True).")


def test_sync_active_blocks_anchor(monkeypatch):
    """When the feeder stepper is bound to an extruder trapq
    (`_stepper_synced_to` set), the extruder side owns motion.
    Injecting our own anchor would race against the bound trapq
    and rip itersolve out under in-flight steps."""
    _, feeder = make_idle_feeder()
    # Simulate an active sync binding.
    feeder._stepper_synced_to = "extruder"

    calls = count_anchor_calls(monkeypatch, feeder)

    feeder.reactor.now = 30.0
    feeder._last_move_end_time = 0.0

    feeder._main_tick(eventtime=30.0)

    assert calls == [], (
        "Watchdog must NOT fire while _stepper_synced_to is set.")


def test_idempotent_against_back_to_back_ticks(monkeypatch):
    """A second tick immediately after a watchdog trip must NOT fire
    another anchor. The `_last_idle_anchor_time` gate enforces a
    fresh idle_anchor_gap-wide window between anchors — otherwise
    the watchdog would refire at the 50 Hz main-tick cadence as long
    as `_last_move_end_time` stays in the past."""
    _, feeder = make_idle_feeder()
    calls = count_anchor_calls(monkeypatch, feeder)

    feeder.reactor.now = 20.0
    feeder._last_move_end_time = 0.0
    feeder._main_tick(eventtime=20.0)
    assert len(calls) == 1, "first tick must trip the watchdog"

    # Second tick 50 ms later — well under idle_anchor_gap=10s.
    feeder.reactor.now = 20.05
    feeder._main_tick(eventtime=20.05)

    assert len(calls) == 1, (
        "Watchdog must remain idempotent within one idle_anchor_gap "
        "window — no second anchor on the very next tick.")


def test_anchor_advances_last_move_end_time_and_last_idle_anchor(monkeypatch):
    """Functional contract: after firing, the watchdog must update
    both `_last_move_end_time` (so the next post-anchor `mcu_now -
    last_move_end_time` window measures from the anchor) and
    `_last_idle_anchor_time` (so the dedicated anchor-spacing gate
    works)."""
    _, feeder = make_idle_feeder()
    count_anchor_calls(monkeypatch, feeder)

    feeder.reactor.now = 25.0
    feeder._last_move_end_time = 0.0
    pre_anchor_lmet = feeder._last_move_end_time
    pre_anchor_lat = feeder._last_idle_anchor_time

    feeder._main_tick(eventtime=25.0)

    assert feeder._last_move_end_time > pre_anchor_lmet, (
        "_last_move_end_time must advance after the anchor fires "
        "(spy mirrors the real _submit_anchor_move side effect).")
    assert feeder._last_idle_anchor_time > pre_anchor_lat, (
        "_last_idle_anchor_time must advance so the next-tick gate "
        "blocks re-firing within idle_anchor_gap.")


def test_custom_idle_anchor_gap_is_respected(monkeypatch):
    """User-configurable: setting idle_anchor_gap=20 must shift the
    watchdog trip-point. A 15 s gap must NOT fire (< 20); a 25 s gap
    must fire (> 20)."""
    _, feeder = make_idle_feeder(values={'idle_anchor_gap': 20.0})
    assert feeder.idle_anchor_gap == 20.0
    calls = count_anchor_calls(monkeypatch, feeder)

    # 15 s gap — under threshold.
    feeder.reactor.now = 15.0
    feeder._last_move_end_time = 0.0
    feeder._main_tick(eventtime=15.0)
    assert calls == [], "15 s gap must not trip with threshold=20"

    # 25 s gap — over threshold.
    feeder.reactor.now = 25.0
    feeder._last_move_end_time = 0.0
    feeder._main_tick(eventtime=25.0)
    assert len(calls) == 1, "25 s gap must trip with threshold=20"


def test_anchor_after_two_full_windows_fires_again(monkeypatch):
    """After one anchor at t=20, no anchor at t=20.05 (idempotent),
    then ANOTHER anchor must fire at t≈30+ once a fresh
    idle_anchor_gap window elapses since the first anchor.

    This guards against a regression where `_last_idle_anchor_time`
    is set so high (or never reset) that the watchdog goes silent
    permanently after one trip — defeating the whole point in a
    very-long-idle scenario."""
    _, feeder = make_idle_feeder()
    calls = count_anchor_calls(monkeypatch, feeder)

    # First trip at t=20.
    feeder.reactor.now = 20.0
    feeder._last_move_end_time = 0.0
    feeder._main_tick(eventtime=20.0)
    assert len(calls) == 1

    # Far in the future (t=40) — last anchor was at t≈20, so the
    # anchor-spacing gate has now elapsed twice. The watchdog
    # should re-fire because _last_move_end_time is also still
    # frozen (the spy only nudged it 1 ms past the anchor time, so
    # gap_moves = 40 - 20.001 ≈ 20 > 10).
    feeder.reactor.now = 40.0
    feeder._main_tick(eventtime=40.0)
    assert len(calls) == 2, (
        "After a full idle_anchor_gap elapses since the previous "
        "anchor, the watchdog must re-fire.")


def test_watchdog_fires_in_auto_with_hall_empty_when_flush_callback_bangbang(
        monkeypatch):
    """AUTO watchdog must still anchor in flush-callback mode even
    when hall_empty is true.

    In the flush-callback architecture, hall_empty does not imply an
    active reactor-tick submitter. Without this bypass, last_step_clock
    can age out during long idle windows before PRINT_START.
    """
    _, feeder = make_idle_feeder(
        values={'use_flush_callback_bang_bang': True})
    feeder._state = buffer_feeder.STATE_AUTO
    feeder._continuous_feed = False
    set_sensor_active(feeder, 'hall_empty', True)
    set_sensor_active(feeder, 'hall_full', False)
    set_sensor_active(feeder, 'hall_overflow', False)

    calls = count_anchor_calls(monkeypatch, feeder)
    feeder.reactor.now = 20.0
    feeder._last_move_end_time = 0.0
    feeder._main_tick(eventtime=20.0)

    assert len(calls) == 1, (
        "Watchdog must fire in STATE_AUTO with hall_empty=True when "
        "use_flush_callback_bang_bang=True.")


def test_watchdog_still_blocks_in_auto_with_hall_empty_when_classic_bangbang(
        monkeypatch):
    """Classic reactor-tick bang-bang keeps the original hall_empty
    block to avoid racing a live feed request."""
    _, feeder = make_idle_feeder(
        values={'use_flush_callback_bang_bang': False})
    feeder._state = buffer_feeder.STATE_AUTO
    feeder._continuous_feed = False
    set_sensor_active(feeder, 'hall_empty', True)
    set_sensor_active(feeder, 'hall_full', False)
    set_sensor_active(feeder, 'hall_overflow', False)
    monkeypatch.setattr(feeder, "_bang_bang_tick", lambda et: None)

    calls = count_anchor_calls(monkeypatch, feeder)
    feeder.reactor.now = 20.0
    feeder._last_move_end_time = 0.0
    feeder._main_tick(eventtime=20.0)

    assert calls == [], (
        "Classic reactor-tick bang-bang must keep the hall_empty "
        "watchdog block when use_flush_callback_bang_bang=False.")
