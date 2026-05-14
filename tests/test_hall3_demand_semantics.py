"""HALL3-Demand-Semantik — Wurzel C Praeventionsfix.

Der Kernpunkt aus PR #42: HALL3 alleine ist kein Demand. Die Tests
halten bewusst die Trennung zwischen Idle-Ruhestellung und echter
Extruderbewegung fest.
"""

import pytest

from fakes_klipper import FakeConfig, FakePrinter, FakePrintStats
from klipper_extras import buffer_feeder


def set_sensor_active(feeder, sensor_name, active):
    polarity_flip = feeder._pin_polarity_flip[sensor_name]
    raw = (not active) if polarity_flip else active
    feeder._pin_stable_state[sensor_name] = raw
    feeder._pin_raw_state[sensor_name] = raw


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


def populate_tracker(feeder, velocity, samples=12):
    fake_ext = feeder.printer.objects['extruder']
    t = 0.0
    for _ in range(samples):
        fake_ext.last_position = t * velocity
        feeder.velocity_tracker.tick(t)
        t += 0.025


def test_hall3_without_tracker_velocity_returns_zero():
    _, feeder = make_feeder()
    set_sensor_active(feeder, 'hall_empty', True)
    assert not feeder.velocity_tracker.is_ready()
    assert feeder._compute_target_feed_speed() == 0.0


def test_hall3_with_ready_tracker_but_zero_velocity_returns_zero():
    _, feeder = make_feeder()
    set_sensor_active(feeder, 'hall_empty', True)
    populate_tracker(feeder, velocity=0.0)
    assert feeder.velocity_tracker.is_ready()
    assert feeder._compute_target_feed_speed() == 0.0


def test_hall3_with_active_extruder_keeps_soft_throttle():
    _, feeder = make_feeder()
    set_sensor_active(feeder, 'hall_empty', True)
    populate_tracker(feeder, velocity=15.0)
    assert feeder._compute_target_feed_speed() == pytest.approx(22.5, abs=0.1)


def test_hall3_low_velocity_uses_floor_once_extruder_moves():
    _, feeder = make_feeder()
    set_sensor_active(feeder, 'hall_empty', True)
    populate_tracker(feeder, velocity=5.0)
    assert feeder._compute_target_feed_speed() == pytest.approx(
        feeder.min_feed_floor, abs=0.1)


def test_hall1_and_hall2_still_override_hall3_demand():
    _, feeder = make_feeder()
    set_sensor_active(feeder, 'hall_empty', True)
    populate_tracker(feeder, velocity=20.0)
    set_sensor_active(feeder, 'hall_overflow', True)
    assert feeder._compute_target_feed_speed() == 0.0
    set_sensor_active(feeder, 'hall_overflow', False)
    set_sensor_active(feeder, 'hall_full', True)
    assert feeder._compute_target_feed_speed() == 0.0


def test_zwischenzone_with_active_extruder_unchanged():
    _, feeder = make_feeder()
    populate_tracker(feeder, velocity=20.0)
    assert feeder._compute_target_feed_speed() == pytest.approx(22.0, abs=1.0)


def test_zwischenzone_idle_unchanged():
    _, feeder = make_feeder()
    assert feeder._compute_target_feed_speed() == 0.0


def test_print_start_with_hall3_and_no_extrusion_emits_no_submit():
    printer, feeder = make_feeder(print_state="printing")
    motion_q = printer.lookup_object('motion_queuing')
    set_sensor_active(feeder, 'hall_empty', True)
    feeder.reactor.now = 10.0

    appends_before = len(motion_q.append_calls)
    motion_q.trigger_flush(flush_time=10.0, step_gen_time=10.0)
    own = [c for c in motion_q.append_calls[appends_before:]
           if c[0] is feeder.trapq]
    assert own == []
