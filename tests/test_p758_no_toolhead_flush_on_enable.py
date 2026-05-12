"""P7-58 — _schedule_time_for_enable_toggle no longer flushes the toolhead.

User-reported bug 2026-04-29:
"wenn der buffer geladen wird hat der extruder eine kleine verzögerung
und dementsprechend lücken im druck"

Root cause: Every bang-bang feed-trigger called _enable_stepper() →
_schedule_time_for_enable_toggle() → toolhead.get_last_move_time().
The mainline toolhead.get_last_move_time() implementation runs the
lookahead pipeline (_process_lookahead / _flush_lookahead) before
returning print_time — host-side planning work that briefly stalls
the extruder. Frequency: ~2 Hz during sustained bang-bang refill,
enough to produce visible gaps in the print.

Fix: Drop the toolhead.get_last_move_time() call. The buffer stepper
runs in own_trapq, so the toolhead's last move time has no bearing
on our enable scheduling. mcu_now + lead_time and the existing
_last_move_end_time / _last_enable_schedule_time floors are
sufficient.
"""


def test_enable_does_not_flush_toolhead(feeder_factory):
    """The fix: _enable_stepper() must NOT trigger any toolhead flush.
    Pre-fix this counter would tick up on every bang-bang feed."""
    printer, feeder = feeder_factory()
    toolhead = printer.lookup_object('toolhead')
    flushes_before = toolhead.flush_calls
    th_lookups_before = toolhead.get_last_move_time_calls

    feeder._enable_stepper()

    assert toolhead.flush_calls == flushes_before
    assert toolhead.get_last_move_time_calls == th_lookups_before


def test_disable_does_not_flush_toolhead(feeder_factory):
    """Same fix applies to disable_stepper — it shares the helper."""
    printer, feeder = feeder_factory()
    toolhead = printer.lookup_object('toolhead')
    flushes_before = toolhead.flush_calls

    feeder._disable_stepper()

    assert toolhead.flush_calls == flushes_before


def test_enable_schedule_pt_advances_with_lead_time(feeder_factory):
    """The 'Timer too close' guard (P7-56) must still hold: each
    enable/disable schedule monotonically advances by at least
    lead_time. Verified by repeat-call delta."""
    _, feeder = feeder_factory(values={'lead_time': 0.3})
    feeder._last_enable_schedule_time = 0.0

    pt1 = feeder._schedule_time_for_enable_toggle()
    pt2 = feeder._schedule_time_for_enable_toggle()

    assert pt2 >= pt1 + feeder.lead_time, (
        "consecutive toggle scheduling must advance by >= lead_time")


def test_enable_schedule_respects_last_move_end_time(feeder_factory):
    """If a buffer move was scheduled to end far in the future, the
    next enable/disable must be after that — so the motor toggle
    happens AFTER the steps it was meant to drive."""
    _, feeder = feeder_factory(values={'lead_time': 0.3})
    feeder._last_move_end_time = 100.0

    pt = feeder._schedule_time_for_enable_toggle()

    assert pt >= 100.0 + feeder.lead_time


def test_enable_schedule_uses_mcu_now_floor(feeder_factory):
    """When _last_move_end_time is in the past, mcu_now + lead_time
    is the binding floor (not toolhead's last_move_time anymore)."""
    printer, feeder = feeder_factory(values={'lead_time': 0.3})
    feeder._last_move_end_time = 0.0
    feeder._last_enable_schedule_time = 0.0
    # Set toolhead.last_move_time to something obviously different —
    # if the helper still queried toolhead, we'd see this value bleed
    # through. After P7-58 the toolhead value must be ignored.
    toolhead = printer.lookup_object('toolhead')
    toolhead.last_move_time = 999.0

    pt = feeder._schedule_time_for_enable_toggle()

    # mcu_now is FakeMCU.estimated_print_time(monotonic) = monotonic.
    # lead_time=0.3, so pt should be ~ monotonic + 0.3, NOT ~999.3.
    assert pt < 50.0, (
        "_schedule_time_for_enable_toggle must not pick up toolhead's "
        "last_move_time after P7-58")
    assert toolhead.get_last_move_time_calls == 0
