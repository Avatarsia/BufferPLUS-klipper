"""P7-52 — Flush-callback driven bang-bang (Klipper-Mainline-Pattern).

Klipper's klippy/extras/motion_queuing.py exposes register_flush_
callback(callback, can_add_trapq=False) which fires synchronously
during the MCU flush cycle with signature (flush_time, step_gen_time).
This is the same hook toolhead.py:168-170 uses for its own runout
detection.

By piggy-backing on this hook for bang-bang submits, we get:
- Cursor-safe t0 anchor: step_gen_time + lead_time always lands in
  the next flush iteration (not 10s in the future like
  toolhead.get_last_move_time would when an extruder is mid-move)
- No race with Klipper's own step-gen — the callback runs INSIDE
  the flush cycle, not outside it
- Reactivity tied to flush rate (~50-100ms typical)

The feature is gated by use_flush_callback_bang_bang config flag.
Default off until validated on hardware.

This test characterizes the callback's behavior across all relevant
states. Hardware test still required to confirm cursor anchor is
truly race-free at the MCU level.
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
    base = {"use_flush_callback_bang_bang": True}
    if values:
        base.update(values)
    printer = FakePrinter()
    printer.objects["print_stats"] = FakePrintStats(state="printing")
    config = FakeConfig(printer=printer, values=base)
    feeder = buffer_feeder.BufferFeeder(config)
    feeder._startup_grace_done = True
    feeder._state = buffer_feeder.STATE_AUTO
    # All HALLs inactive by default (FakeConfig polarity-flip would
    # otherwise leave HALL2 spuriously active and the bang-bang
    # decision branch falls into hall_full first).
    set_sensor_active(feeder, 'hall_overflow', False)
    set_sensor_active(feeder, 'hall_full', False)
    set_sensor_active(feeder, 'hall_empty', False)
    # Wurzel-C-Praevention γ (2026-05-14): Tracker mit aktiver
    # ext_vel vorbefuellen damit HALL3-Demand-Pfad-Tests funktionieren.
    fake_ext = printer.objects['extruder']
    t = 0.0
    for _ in range(12):
        fake_ext.last_position = t * 15.0
        feeder.velocity_tracker.tick(t)
        t += 0.025
    return printer, feeder


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def test_flush_callback_registered_when_flag_on():
    printer, feeder = make_feeder()
    motion_q = printer.lookup_object('motion_queuing')

    # The callback must have been registered during BufferFeeder init.
    callbacks = [cb for cb, _ in motion_q.flush_callbacks]
    assert feeder._on_mcu_flush in callbacks


def test_flush_callback_registered_even_when_flag_off():
    """Registration is unconditional so we can toggle the feature
    flag at runtime; the callback itself short-circuits on the flag."""
    printer = FakePrinter()
    config = FakeConfig(printer=printer,
                        values={"use_flush_callback_bang_bang": False})
    feeder = buffer_feeder.BufferFeeder(config)
    motion_q = printer.lookup_object('motion_queuing')

    callbacks = [cb for cb, _ in motion_q.flush_callbacks]
    assert feeder._on_mcu_flush in callbacks


# ---------------------------------------------------------------------------
# Submit decisions
# ---------------------------------------------------------------------------

def test_flush_submits_when_hall3_active_and_state_auto():
    printer, feeder = make_feeder()
    motion_q = printer.lookup_object('motion_queuing')
    set_sensor_active(feeder, 'hall_empty', True)
    # mcu_now must be consistent with step_gen_time — flush callbacks
    # only fire with step_gen_time close to (typically a few ms ahead
    # of) mcu_now. P7-73 (Issue #31) clamps forced_t0 to
    # mcu_now + 2.0s, so an artificial reactor.now=0 + step_gen_time=
    # 5.05 would now (correctly) trip the far-future clamp.
    feeder.reactor.now = 5.0

    appends_before = len(motion_q.append_calls)
    motion_q.trigger_flush(flush_time=5.0, step_gen_time=5.05)

    new_appends = motion_q.append_calls[appends_before:]
    own_appends = [c for c in new_appends if c[0] is feeder.trapq]
    assert own_appends, "expected buffer-stepper submit on HALL3"
    # t0 should anchor at step_gen_time + lead_time, NOT at toolhead.
    t0 = own_appends[0][1]
    expected = 5.05 + feeder.lead_time
    assert abs(t0 - expected) < 0.01, (
        "t0=%.3f, expected ~%.3f (step_gen_time + lead_time)"
        % (t0, expected))
    assert feeder._continuous_feed is True


@pytest.mark.skip(
    reason="C-cont T7 removed Bang-Bang hall_full=no-submit semantic. "
           "Streaming now continues with target_speed = 0.5 * extruder_"
           "velocity (SpeedModulator). See docs/superpowers/plans/"
           "2026-05-13-c-cont-streaming.md T7 and docs/superpowers/specs/"
           "2026-05-13-high-flow-buffer-architecture.md.")
def test_flush_no_submit_when_hall_full():
    printer, feeder = make_feeder()
    motion_q = printer.lookup_object('motion_queuing')
    set_sensor_active(feeder, 'hall_full', True)

    appends_before = len(motion_q.append_calls)
    motion_q.trigger_flush(flush_time=5.0, step_gen_time=5.05)

    new_appends = motion_q.append_calls[appends_before:]
    own_appends = [c for c in new_appends if c[0] is feeder.trapq]
    assert not own_appends


@pytest.mark.skip(
    reason="C-cont T7 removed Bang-Bang cycle (no hall_full -> "
           "_continuous_feed=False reset). _continuous_feed stays True "
           "structurally; only target_speed is modulated. See "
           "docs/superpowers/plans/2026-05-13-c-cont-streaming.md T7.")
def test_flush_clears_continuous_feed_when_hall_full():
    printer, feeder = make_feeder()
    motion_q = printer.lookup_object('motion_queuing')
    feeder._continuous_feed = True
    set_sensor_active(feeder, 'hall_full', True)

    motion_q.trigger_flush(flush_time=5.0, step_gen_time=5.05)

    assert feeder._continuous_feed is False


def test_flush_no_submit_when_state_idle():
    """LOAD/UNLOAD macros own non-AUTO states. Bang-bang must not
    interfere via the flush callback."""
    printer, feeder = make_feeder()
    motion_q = printer.lookup_object('motion_queuing')
    feeder._state = buffer_feeder.STATE_IDLE
    set_sensor_active(feeder, 'hall_empty', True)

    appends_before = len(motion_q.append_calls)
    motion_q.trigger_flush(flush_time=5.0, step_gen_time=5.05)

    new_appends = motion_q.append_calls[appends_before:]
    own_appends = [c for c in new_appends if c[0] is feeder.trapq]
    assert not own_appends


def test_flush_no_submit_when_synced_to_extruder():
    """Macro-driven SYNC has the stepper bound to the extruder trapq.
    Submitting on own_trapq during that window would queue moves on
    the wrong trapq — exactly the P7-45 race we already fixed for
    the sensor-callback path."""
    printer, feeder = make_feeder()
    motion_q = printer.lookup_object('motion_queuing')
    feeder._stepper_synced_to = 'extruder'
    set_sensor_active(feeder, 'hall_empty', True)

    appends_before = len(motion_q.append_calls)
    motion_q.trigger_flush(flush_time=5.0, step_gen_time=5.05)

    new_appends = motion_q.append_calls[appends_before:]
    own_appends = [c for c in new_appends if c[0] is feeder.trapq]
    assert not own_appends


def test_flush_no_submit_when_bang_bang_suspended():
    """Print-pause sets _bang_bang_suspended; we must respect it."""
    printer, feeder = make_feeder()
    motion_q = printer.lookup_object('motion_queuing')
    feeder._bang_bang_suspended = True
    set_sensor_active(feeder, 'hall_empty', True)

    appends_before = len(motion_q.append_calls)
    motion_q.trigger_flush(flush_time=5.0, step_gen_time=5.05)

    new_appends = motion_q.append_calls[appends_before:]
    own_appends = [c for c in new_appends if c[0] is feeder.trapq]
    assert not own_appends


def test_flush_no_submit_when_hall1_active():
    """HALL1-active forward-reject in _submit_move still applies.
    A overfilled buffer must not be fed even if HALL3 is somehow
    also asserted (sensor glitch / wiring)."""
    printer, feeder = make_feeder()
    motion_q = printer.lookup_object('motion_queuing')
    set_sensor_active(feeder, 'hall_empty', True)
    set_sensor_active(feeder, 'hall_overflow', True)

    appends_before = len(motion_q.append_calls)
    motion_q.trigger_flush(flush_time=5.0, step_gen_time=5.05)

    new_appends = motion_q.append_calls[appends_before:]
    own_appends = [c for c in new_appends if c[0] is feeder.trapq]
    assert not own_appends


# ---------------------------------------------------------------------------
# Feature flag gating
# ---------------------------------------------------------------------------

def test_flush_callback_no_op_when_flag_off():
    """With flag off the callback is a no-op; legacy reactor-tick
    bang-bang carries the workload."""
    printer, feeder = make_feeder(values={
        "use_flush_callback_bang_bang": False,
    })
    set_sensor_active(feeder, 'hall_empty', True)
    motion_q = printer.lookup_object('motion_queuing')

    appends_before = len(motion_q.append_calls)
    motion_q.trigger_flush(flush_time=5.0, step_gen_time=5.05)

    new_appends = motion_q.append_calls[appends_before:]
    own_appends = [c for c in new_appends if c[0] is feeder.trapq]
    assert not own_appends, (
        "Flush-callback path triggered despite "
        "use_flush_callback_bang_bang=False")


def test_legacy_bang_bang_tick_no_op_when_flag_on():
    """Mirror: the legacy reactor-tick path becomes a no-op when the
    flush-callback flag is on, so we don't double-submit."""
    printer, feeder = make_feeder()
    motion_q = printer.lookup_object('motion_queuing')
    set_sensor_active(feeder, 'hall_empty', True)

    appends_before = len(motion_q.append_calls)
    feeder._bang_bang_tick(eventtime=5.0)

    new_appends = motion_q.append_calls[appends_before:]
    own_appends = [c for c in new_appends if c[0] is feeder.trapq]
    assert not own_appends


# ---------------------------------------------------------------------------
# Cursor-anchor verification (the actual fix)
# ---------------------------------------------------------------------------

def test_flush_t0_does_not_use_toolhead_get_last_move_time():
    """The whole point of P7-52 is that t0 ignores
    toolhead.get_last_move_time. Even if the toolhead is mid-move
    (10s ahead), the flush-callback submit lands at step_gen_time
    + lead_time."""
    printer, feeder = make_feeder()
    motion_q = printer.lookup_object('motion_queuing')
    toolhead = printer.lookup_object('toolhead')
    set_sensor_active(feeder, 'hall_empty', True)

    # Toolhead 10s in the future (active G1 E50)
    toolhead.last_move_time = 15.0
    feeder.reactor.now = 5.0

    appends_before = len(motion_q.append_calls)
    motion_q.trigger_flush(flush_time=5.0, step_gen_time=5.05)

    new_appends = motion_q.append_calls[appends_before:]
    own_appends = [c for c in new_appends if c[0] is feeder.trapq]
    assert own_appends
    t0 = own_appends[0][1]
    # Anchor MUST be near step_gen_time, NOT near toolhead's 15.0.
    assert t0 < 6.0, (
        "P7-52 broken: t0=%.3f, expected near step_gen_time=5.05. "
        "Toolhead at 15.0 was used as anchor — exactly the lag bug "
        "we're trying to eliminate." % t0)
