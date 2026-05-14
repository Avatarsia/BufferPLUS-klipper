from fakes_klipper import FakeConfig, FakePrinter, FakePrintStats
from helpers import set_sensor_active
from klipper_extras import buffer_feeder


def make_feeder(values=None, print_state="printing"):
    base = {"use_flush_callback_bang_bang": True}
    if values:
        base.update(values)
    printer = FakePrinter()
    printer.objects["print_stats"] = FakePrintStats(state=print_state)
    config = FakeConfig(printer=printer, values=base)
    feeder = buffer_feeder.BufferFeeder(config)
    printer.fire_event('klippy:connect')
    feeder._startup_grace_done = True
    feeder._state = buffer_feeder.STATE_AUTO
    set_sensor_active(feeder, 'hall_overflow', False)
    set_sensor_active(feeder, 'hall_full', False)
    set_sensor_active(feeder, 'hall_empty', False)
    return printer, feeder


def _prime_tracker(feeder, *, velocity):
    fake_ext = feeder.printer.objects['extruder']
    t = 0.0
    for _ in range(12):
        fake_ext.last_position = t * velocity
        feeder.velocity_tracker.tick(t)
        t += 0.025


def test_print_start_guard_blocks_submit_during_critical_window():
    printer, feeder = make_feeder()
    motion_q = printer.lookup_object('motion_queuing')
    _prime_tracker(feeder, velocity=15.0)
    set_sensor_active(feeder, 'hall_empty', True)
    feeder.reactor.now = 10.0
    feeder._on_idle_printing()

    motion_q.trigger_flush(flush_time=10.0, step_gen_time=10.05)

    assert motion_q.append_calls == []
    status = feeder.get_status(10.0)
    assert status["print_phase"] == "guarded"
    assert status["critical_action_guard_reason"] == "print_start"


def test_auto_submit_resumes_after_guard_expires():
    printer, feeder = make_feeder()
    motion_q = printer.lookup_object('motion_queuing')
    _prime_tracker(feeder, velocity=15.0)
    set_sensor_active(feeder, 'hall_empty', True)
    feeder.reactor.now = 10.0
    feeder._on_idle_printing()
    feeder.reactor.now = 11.0

    motion_q.trigger_flush(flush_time=11.0, step_gen_time=11.05)

    own = [call for call in motion_q.append_calls if call[0] is feeder.trapq]
    assert own
    assert feeder.get_status(11.0)["print_phase"] == "active"


def test_get_status_exposes_transition_guard_fields():
    _, feeder = make_feeder(values={"buffer_conservative_mode": True})
    feeder.reactor.now = 4.0
    feeder._arm_critical_action_guard('unit_test', duration=0.2, eventtime=4.0)

    status = feeder.get_status(4.0)

    assert status["critical_action_guard_remaining_s"] >= 0.75
    assert status["critical_action_guard_reason"] == "unit_test"
    assert status["buffer_conservative_mode"] is True
