"""Initializer snapshots stay separate from observable live feeder behavior."""

import pytest

from helpers import FakeGCmd, build_feeder, set_sensor_active
from klipper_extras import buffer_feeder


def test_runtime_tuning_reaches_status_and_flush_motion_without_rewriting_startup():
    printer, feeder = build_feeder(
        {'feed_speed': 30.0, 'accel': 1000.0,
         'use_flush_callback_bang_bang': True},
        state=buffer_feeder.STATE_AUTO, fire_connect=True,
        print_state='printing')
    startup = feeder.settings
    for sensor in ('hall_overflow', 'hall_full'):
        set_sensor_active(feeder, sensor, False)
    set_sensor_active(feeder, 'hall_empty', True)
    set_sensor_active(feeder, 'entrance', True)
    extruder = printer.lookup_object('extruder')
    for index in range(12):
        eventtime = index * 0.025
        extruder.last_position = eventtime * 50.0
        feeder.velocity_tracker.tick(eventtime)

    feeder.cmd_BUFFER_SET(FakeGCmd({'SPEED': 50.0, 'ACCEL': 1500.0}))

    status = feeder.get_status(printer.get_reactor().monotonic())
    assert status['feed_speed'] == 50.0
    assert status['accel'] == 1500.0
    motion = printer.lookup_object('motion_queuing')
    motion.trigger_flush(0.0, 0.0)
    assert motion.append_calls
    move = motion.append_calls[-1]
    queued_speed, queued_accel = move[-2:]
    assert queued_speed == pytest.approx(50.0)
    assert queued_accel == pytest.approx(1500.0)
    assert startup.feed_speed == 30.0
    assert startup.accel == 1000.0
    assert feeder.settings is startup
    assert feeder.initial_settings is startup


def test_feed_and_halt_change_live_status_without_rewriting_startup_state():
    printer, feeder = build_feeder(
        state=buffer_feeder.STATE_IDLE, fire_connect=True)
    startup = feeder.runtime_state
    set_sensor_active(feeder, 'hall_overflow', False)

    feeder.cmd_BUFFER_FEED(FakeGCmd({'DISTANCE': 5.0}))

    status = feeder.get_status(printer.get_reactor().monotonic())
    assert status['state'] == buffer_feeder.STATE_MANUAL_FEED
    assert status['commanded_pos_mm'] == pytest.approx(5.0)
    assert printer.lookup_object('motion_queuing').append_calls
    feeder.cmd_BUFFER_HALT(FakeGCmd())
    status = feeder.get_status(printer.get_reactor().monotonic())
    assert status['state'] == buffer_feeder.STATE_IDLE
    assert status['halt_requested'] is True
    assert startup._state == buffer_feeder.STATE_INIT
    assert startup._commanded_pos == 0.0
    assert startup._halt_requested is False
    assert feeder.runtime_state is startup
    assert feeder.initial_runtime_state is startup
