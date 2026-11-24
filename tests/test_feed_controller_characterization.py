"""Observable pre-extraction behavior for the controller and tick adapter."""

import pytest

from helpers import build_feeder, set_sensor_active
from klipper_extras import buffer_feeder


def configured_feeder(monkeypatch):
    _, feeder = build_feeder(state=buffer_feeder.STATE_AUTO)
    for sensor in ('hall_overflow', 'hall_full', 'hall_empty'):
        set_sensor_active(feeder, sensor, False)
    feeder.min_feed_floor = 10.0
    feeder.feed_speed = 60.0
    feeder.feed_speed_gain = 1.2
    feeder.hall3_demand_gain = 1.5
    feeder.feed_hysteresis_stop_factor = 0.8
    feeder.high_flow_mm3s_threshold = 24.0
    demand = {'time': 1.0, 'ready': True, 'velocity': 20.0, 'flow': 25.0}
    monkeypatch.setattr(feeder.reactor, 'monotonic', lambda: demand['time'])
    monkeypatch.setattr(feeder.velocity_tracker, 'is_ready',
                        lambda: demand['ready'])
    monkeypatch.setattr(feeder.velocity_tracker, 'get_velocity',
                        lambda: demand['velocity'])
    monkeypatch.setattr(feeder.velocity_tracker, 'get_volumetric_flow',
                        lambda: demand['flow'])
    return feeder, demand


@pytest.mark.parametrize('overflow', [False, True])
def test_safety_precedes_tracker_reads(monkeypatch, overflow):
    feeder, _ = configured_feeder(monkeypatch)
    set_sensor_active(feeder, 'hall_full', True)
    set_sensor_active(feeder, 'hall_overflow', overflow)
    set_sensor_active(feeder, 'hall_empty', True)
    feeder._high_flow_carry_armed_until = 100.0
    feeder._high_flow_active_latched = True
    feeder._post_full_bias_clamp = True

    def unexpected_read():
        pytest.fail('Safety must stop before reading tracker demand')

    monkeypatch.setattr(feeder.velocity_tracker, 'is_ready', unexpected_read)
    assert feeder._compute_target_feed_speed() == 0.0
    assert feeder._high_flow_carry_armed_until == 0.0
    assert feeder._high_flow_active_latched is False
    assert feeder._post_full_bias_clamp is (not overflow)


def test_post_full_sequence_and_exact_time_boundaries(monkeypatch):
    feeder, demand = configured_feeder(monkeypatch)
    set_sensor_active(feeder, 'hall_full', True)
    assert feeder._compute_target_feed_speed() == 0.0
    set_sensor_active(feeder, 'hall_full', False)
    assert feeder._compute_target_feed_speed() == 20.0
    set_sensor_active(feeder, 'hall_empty', True)
    assert feeder._compute_target_feed_speed() == 20.0
    demand['time'] = 1.499
    assert feeder._compute_target_feed_speed() == 20.0
    demand['time'] = 1.5
    assert feeder._compute_target_feed_speed() == 30.0
    assert feeder._post_full_bias_clamp is False
    assert feeder._post_full_recovery_until == 2.5
    assert feeder._effective_interrupt_chunk_mm(2.499) == 3.0
    assert feeder._effective_interrupt_chunk_mm(2.5) == feeder.interrupt_chunk_mm
    assert feeder._post_full_recovery_until == 0.0
    assert feeder._is_high_flow_carry_armed(2.249)
    assert not feeder._is_high_flow_carry_armed(2.25)


def test_hysteresis_requires_an_existing_carry_session(monkeypatch):
    feeder, demand = configured_feeder(monkeypatch)
    demand.update(velocity=7.0, flow=25.0)
    assert feeder._compute_target_feed_speed() == 0.0
    set_sensor_active(feeder, 'hall_empty', True)
    assert feeder._compute_target_feed_speed() == 10.0
    set_sensor_active(feeder, 'hall_empty', False)
    demand.update(time=1.1, flow=22.0)
    assert feeder._compute_target_feed_speed() == pytest.approx(8.4)
    demand['time'] = 1.75
    assert feeder._compute_target_feed_speed() == 0.0


def tick_trace(monkeypatch):
    feeder, demand = configured_feeder(monkeypatch)
    trace = []
    monkeypatch.setattr(feeder.velocity_tracker, 'tick',
                        lambda t: trace.append('tracker'))
    monkeypatch.setattr(feeder, '_check_debounce',
                        lambda t: trace.append('debounce'))
    monkeypatch.setattr(feeder, '_maintain_msg_latches',
                        lambda s: trace.append('latches'))
    monkeypatch.setattr(feeder, '_get_print_stats_state', lambda t: 'printing')
    for method, label in (
            ('_tick_safety_timeouts', 'safety'),
            ('_tick_cooldown_end', 'cooldown'),
            ('_tick_grip_completion', 'grip'),
            ('_bang_bang_tick', 'bang_bang'),
            ('_tick_runout_follow', 'runout'),
            ('_tick_pending_chunk', 'pending')):
        monkeypatch.setattr(feeder, method,
                            lambda t, label=label: trace.append(label))
    monkeypatch.setattr(feeder, '_move_in_flight', lambda: False)
    monkeypatch.setattr(feeder, '_disable_stepper',
                        lambda: trace.append('disable'))
    feeder._pending_disable = True
    return feeder, trace


def test_tick_phase_order_before_and_after_extraction(monkeypatch):
    feeder, trace = tick_trace(monkeypatch)
    assert feeder._main_tick(2.0) == 2.0 + buffer_feeder.MAIN_TICK_INTERVAL
    assert trace == ['tracker', 'debounce', 'latches', 'safety', 'disable',
                     'cooldown', 'grip', 'bang_bang', 'runout', 'pending']


def test_startup_returns_after_sensor_observation(monkeypatch):
    feeder, trace = tick_trace(monkeypatch)
    feeder._startup_grace_done = False
    feeder._main_tick(2.0)
    assert trace == ['tracker', 'debounce']
    assert feeder._pending_disable


def test_hall1_returns_before_safety_and_disable(monkeypatch):
    feeder, trace = tick_trace(monkeypatch)
    feeder._hall1_active_since = -100.0
    monkeypatch.setattr(feeder, '_is_hall1_active', lambda context: True)
    monkeypatch.setattr(feeder, '_enter_overflow', lambda: trace.append('overflow'))
    feeder._main_tick(2.0)
    assert trace == ['tracker', 'debounce', 'latches', 'overflow']
    assert feeder._pending_disable


def test_suppressed_hall1_persist_preserves_later_phases(monkeypatch):
    feeder, trace = tick_trace(monkeypatch)
    feeder._hall1_active_since = -100.0
    monkeypatch.setattr(feeder, '_is_hall1_active', lambda context: False)
    feeder._main_tick(2.0)
    assert trace[-7:] == ['safety', 'disable', 'cooldown', 'grip',
                          'bang_bang', 'runout', 'pending']


def test_phase_exception_stops_tick_and_preserves_next_timer(monkeypatch, caplog):
    feeder, trace = tick_trace(monkeypatch)

    def fail(t):
        trace.append('failure')
        raise RuntimeError('characterization failure')

    monkeypatch.setattr(feeder, '_tick_safety_timeouts', fail)
    assert feeder._main_tick(2.0) == 2.0 + buffer_feeder.MAIN_TICK_INTERVAL
    assert trace == ['tracker', 'debounce', 'latches', 'failure']
    assert 'main_tick error' in caplog.text
    assert feeder._pending_disable
