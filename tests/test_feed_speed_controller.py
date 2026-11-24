"""Controller sequences run from immutable data, without Klipper objects."""

from dataclasses import FrozenInstanceError, replace

import pytest

from helpers import FakeGCmd, build_feeder, set_sensor_active
from klipper_extras import buffer_feeder
from klipper_extras.buffer_pending import SubmitResult
from klipper_extras.buffer_modulator import (
    FeedDemandSnapshot, FeedSpeedController, FeedSpeedSettings,
)


SETTINGS = FeedSpeedSettings(
    min_feed_floor=10.0, feed_hysteresis_stop_factor=0.8,
    feed_speed=60.0, hall3_demand_gain=1.5, feed_speed_gain=1.2,
    high_flow_mm3s_threshold=24.0)
DEMAND = FeedDemandSnapshot(
    eventtime=10.0, hall_overflow=False, hall_full=False, hall_empty=False,
    velocity_ready=True, extruder_velocity=20.0, volumetric_flow=25.0)


@pytest.mark.parametrize('changes,expected', [
    ({'hall_overflow': True, 'hall_full': True, 'hall_empty': True}, 0.0),
    ({'hall_full': True, 'hall_empty': True}, 0.0),
    ({'hall_empty': True, 'velocity_ready': False}, 0.0),
    ({'hall_empty': True, 'extruder_velocity': -1.0}, 0.0),
    ({'hall_empty': True, 'extruder_velocity': 1e-6}, 0.0),
    ({'hall_empty': True, 'extruder_velocity': 2e-6}, 10.0),
    ({'hall_empty': True, 'extruder_velocity': 5.0}, 10.0),
    ({'hall_empty': True}, 30.0),
    ({'hall_empty': True, 'extruder_velocity': 100.0}, 60.0),
    ({}, 24.0),
    ({'extruder_velocity': 5.0}, 0.0),
    ({'velocity_ready': False}, 0.0),
])
def test_snapshot_decisions(changes, expected):
    controller = FeedSpeedController()
    assert controller.compute_target_speed(replace(DEMAND, **changes), SETTINGS) == expected


def test_post_full_dwell_interrupt_and_recovery_sequence():
    controller = FeedSpeedController()
    assert controller.compute_target_speed(replace(DEMAND, hall_full=True), SETTINGS) == 0.0
    assert controller.compute_target_speed(replace(DEMAND, hall_empty=True), SETTINGS) == 20.0
    assert controller.compute_target_speed(replace(DEMAND, eventtime=10.25), SETTINGS) == 20.0
    assert controller._post_full_h3_since is None
    assert controller.compute_target_speed(
        replace(DEMAND, eventtime=11.0, hall_empty=True), SETTINGS) == 20.0
    assert controller.compute_target_speed(
        replace(DEMAND, eventtime=11.499, hall_empty=True), SETTINGS) == 20.0
    assert controller.compute_target_speed(
        replace(DEMAND, eventtime=11.5, hall_empty=True), SETTINGS) == 30.0
    assert controller._post_full_recovery_until == 12.5
    assert controller._high_flow_carry_armed_until == 12.25
    assert controller.effective_interrupt_chunk_mm(12.499, 9.0) == 3.0
    assert controller.effective_interrupt_chunk_mm(12.499, 2.0) == 2.0
    assert controller.effective_interrupt_chunk_mm(12.5, 9.0) == 9.0
    assert controller._post_full_recovery_until == 0.0


def test_carry_grace_and_flow_exit_hysteresis():
    controller = FeedSpeedController()
    # Demand arms carry before a below-floor high-flow continuation.
    controller.compute_target_speed(replace(DEMAND, hall_empty=True), SETTINGS)
    low = replace(DEMAND, eventtime=10.1, extruder_velocity=5.0,
                  volumetric_flow=23.0)
    assert controller.compute_target_speed(low, SETTINGS) == 6.0
    assert controller._high_flow_active_latched
    assert controller._high_flow_carry_armed_until == 10.85
    assert controller.compute_target_speed(
        replace(low, eventtime=10.2, volumetric_flow=22.999), SETTINGS) == 0.0
    assert not controller._high_flow_active_latched
    # An expired session cannot restart below floor even at high flow.
    assert controller.compute_target_speed(
        replace(low, eventtime=10.85, volumetric_flow=25.0), SETTINGS) == 0.0
    assert controller._high_flow_carry_armed_until == 0.0


def test_disabled_high_flow_and_speed_cap_below_floor():
    controller = FeedSpeedController()
    settings = replace(SETTINGS, feed_speed=4.0, high_flow_mm3s_threshold=0.0)
    demand = replace(DEMAND, hall_empty=True, extruder_velocity=5.0)
    assert controller.compute_target_speed(demand, settings) == 4.0
    assert not controller._high_flow_active_latched
    assert controller.compute_target_speed(replace(demand, hall_empty=False), settings) == 0.0


def test_snapshot_values_are_immutable():
    with pytest.raises(FrozenInstanceError):
        DEMAND.eventtime = 99.0
    with pytest.raises(FrozenInstanceError):
        SETTINGS.feed_speed = 99.0


def test_feeder_legacy_properties_share_controller_state_and_halt_resets():
    _, feeder = build_feeder()
    _, other = build_feeder()
    assert feeder.feed_controller is not other.feed_controller
    values = {
        '_modulator_feeding': True,
        '_high_flow_active_latched': True,
        '_high_flow_carry_armed_until': 100.0,
        '_post_full_bias_clamp': True,
        '_post_full_h3_since': 9.0,
        '_post_full_recovery_until': 100.0,
    }
    for name, value in values.items():
        setattr(feeder, name, value)
        assert getattr(feeder.feed_controller, name) == value
        assert name not in feeder.__dict__
        assert getattr(feeder.initial_runtime_state, name) == getattr(other, name)
    feeder._halt_motion()
    for name in values:
        assert getattr(feeder, name) == getattr(feeder.initial_runtime_state, name)
        assert getattr(feeder.feed_controller, name) == getattr(other, name)


def test_live_buffer_set_reaches_next_controller_decision(monkeypatch):
    _, feeder = build_feeder()
    for sensor in ('hall_overflow', 'hall_full'):
        set_sensor_active(feeder, sensor, False)
    set_sensor_active(feeder, 'hall_empty', True)
    monkeypatch.setattr(feeder.velocity_tracker, 'is_ready', lambda: True)
    monkeypatch.setattr(feeder.velocity_tracker, 'get_velocity', lambda: 20.0)
    monkeypatch.setattr(feeder.velocity_tracker, 'get_volumetric_flow', lambda: 25.0)
    original_controller = feeder.feed_controller
    feeder.cmd_BUFFER_SET(FakeGCmd({'SPEED': 50, 'HALL3_DEMAND_GAIN': 1.1}))
    assert feeder._compute_target_feed_speed() == 22.0
    feeder.cmd_BUFFER_SET(FakeGCmd({'SPEED': 18}))
    assert feeder._compute_target_feed_speed() == 18.0
    assert feeder.feed_controller is original_controller


def test_recovery_reads_time_lazily_at_actual_clamp_clear(monkeypatch):
    _, feeder = build_feeder({'buffer_debug_events': False})
    for sensor in ('hall_overflow', 'hall_full'):
        set_sensor_active(feeder, sensor, False)
    set_sensor_active(feeder, 'hall_empty', True)
    feeder._post_full_bias_clamp = True
    feeder._post_full_h3_since = 9.0
    clock_reads = []

    def monotonic():
        clock_reads.append(10.0 + len(clock_reads))
        return clock_reads[-1]

    monkeypatch.setattr(feeder.reactor, 'monotonic', monotonic)
    monkeypatch.setattr(feeder.velocity_tracker, 'is_ready', lambda: True)
    monkeypatch.setattr(feeder.velocity_tracker, 'get_velocity', lambda: 20.0)
    monkeypatch.setattr(feeder.velocity_tracker, 'get_volumetric_flow', lambda: 25.0)
    feeder._compute_target_feed_speed()
    assert clock_reads == [10.0, 11.0]
    assert feeder._post_full_recovery_until == 12.0
    assert feeder._high_flow_carry_armed_until == 10.75
    feeder._compute_target_feed_speed()
    # Once the clamp is clear, no extra clock read or recovery restart occurs.
    assert clock_reads == [10.0, 11.0, 12.0]
    assert feeder._post_full_recovery_until == 12.0


@pytest.mark.parametrize('delayed_source', ['getter', 'debug'])
def test_delayed_clamp_clear_keeps_recovery_cap_in_flush_submit(
        monkeypatch, delayed_source):
    clock = {'now': 10.0}
    original_debug = buffer_feeder.BufferFeeder._debug_event

    def debug(self, key, *args, **kwargs):
        if delayed_source == 'debug' and key == 'high_flow_state':
            clock['now'] += 0.25
        return original_debug(self, key, *args, **kwargs)

    monkeypatch.setattr(buffer_feeder.BufferFeeder, '_debug_event', debug)
    _, feeder = build_feeder({
        'buffer_debug_events': False, 'min_feed_floor': 10.0,
        'interrupt_chunk_mm': 9.0,
    })
    for sensor in ('hall_overflow', 'hall_full'):
        set_sensor_active(feeder, sensor, False)
    set_sensor_active(feeder, 'hall_empty', True)
    feeder._post_full_bias_clamp = True
    feeder._post_full_h3_since = 9.0
    monkeypatch.setattr(feeder.reactor, 'monotonic', lambda: clock['now'])
    monkeypatch.setattr(feeder.velocity_tracker, 'is_ready', lambda: True)

    def velocity():
        if delayed_source == 'getter':
            clock['now'] += 0.25
        return 20.0

    monkeypatch.setattr(feeder.velocity_tracker, 'get_velocity', velocity)
    monkeypatch.setattr(feeder.velocity_tracker, 'get_volumetric_flow', lambda: 25.0)
    assert feeder._compute_target_feed_speed() == 30.0
    assert clock['now'] == 10.25
    # Use the real flush consumer during the original protected interval.
    monkeypatch.setattr(feeder.velocity_tracker, 'get_velocity', lambda: 20.0)
    monkeypatch.setattr(feeder, '_auto_submit_permission',
                        lambda eventtime: (True, 'active_print'))
    submits = []

    def submit(distance, speed, **kwargs):
        submits.append((distance, speed, kwargs['submit_chunk_cap']))
        return SubmitResult.QUEUED

    monkeypatch.setattr(feeder, '_submit_move', submit)
    clock['now'] = 11.10
    feeder._flush_submit_streaming_chunk(step_gen_time=11.10, eventtime=11.10)
    assert submits == [(3.0, 30.0, 3.0)]
    assert feeder._post_full_recovery_until == 11.25
    assert feeder._effective_interrupt_chunk_mm(11.249) == 3.0
    assert feeder._effective_interrupt_chunk_mm(11.25) == 9.0
