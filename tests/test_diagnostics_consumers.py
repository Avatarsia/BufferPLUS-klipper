"""Diagnostic output and live-state contracts characterized before extraction."""

import logging

import pytest

from helpers import build_feeder, set_sensor_active
from klipper_extras import buffer_feeder


def test_debug_events_keep_level_format_and_per_key_rate_limit(caplog):
    _, feeder = build_feeder({'buffer_debug_events': True})
    feeder.reactor.now = 10.0
    with caplog.at_level(logging.INFO):
        feeder._debug_event('first', 'distance=%.1f', 3.0,
                            level=logging.WARNING)
        feeder.reactor.now = 10.999
        feeder._debug_event('first', 'suppressed')
        feeder._debug_event('second', 'independent')
        feeder.reactor.now = 11.0
        feeder._debug_event('first', 'boundary')
        feeder._debug_event('first', 'unlimited', min_interval=0)
    records = [r for r in caplog.records if 'buffer_event[' in r.getMessage()]
    assert [(r.levelno, r.getMessage()) for r in records] == [
        (logging.WARNING, 'buffer_event[first]: distance=3.0'),
        (logging.INFO, 'buffer_event[second]: independent'),
        (logging.INFO, 'buffer_event[first]: boundary'),
        (logging.INFO, 'buffer_event[first]: unlimited'),
    ]


def test_disabled_events_do_not_read_clock(monkeypatch, caplog):
    _, feeder = build_feeder()

    def forbidden_clock():
        pytest.fail('Disabled diagnostics must not read the clock')

    monkeypatch.setattr(feeder.reactor, 'monotonic', forbidden_clock)
    with caplog.at_level(logging.INFO):
        feeder._debug_event('disabled', 'hidden')
    assert 'buffer_event[disabled]' not in caplog.text


def test_event_clock_failure_still_logs_without_rate_limit(monkeypatch, caplog):
    _, feeder = build_feeder({'buffer_debug_events': True})

    def unavailable_clock():
        raise RuntimeError('clock unavailable')

    monkeypatch.setattr(feeder.reactor, 'monotonic', unavailable_clock)
    with caplog.at_level(logging.INFO):
        feeder._debug_event('clock_failure', 'visible')
        feeder._debug_event('clock_failure', 'visible')
    assert caplog.text.count('buffer_event[clock_failure]: visible') == 2
    assert feeder._debug_event_last == {}


def test_diagnostics_keep_console_and_rate_limit_owners_separate(caplog):
    first_printer, first = build_feeder({'buffer_debug_events': True})
    second_printer, second = build_feeder({'buffer_debug_events': True})
    first.reactor.now = second.reactor.now = 10.0
    with caplog.at_level(logging.INFO):
        first._debug_event('shared_key', 'first')
        first._debug_event('shared_key', 'suppressed')
        second._debug_event('shared_key', 'second')
        first._respond('first console')
        second._respond('second console')
    assert 'buffer_event[shared_key]: first' in caplog.text
    assert 'buffer_event[shared_key]: second' in caplog.text
    assert 'suppressed' not in caplog.text
    assert first_printer.lookup_object('gcode').info_messages == [
        'BufferFeeder: first console']
    assert second_printer.lookup_object('gcode').info_messages == [
        'BufferFeeder: second console']


def test_metrics_preserve_getter_order_and_live_controller_effects(
        monkeypatch, caplog):
    _, feeder = build_feeder({'buffer_debug_metrics': True},
                             state=buffer_feeder.STATE_AUTO)
    for name in ('hall_empty', 'hall_full', 'hall_overflow'):
        set_sensor_active(feeder, name, False)
    set_sensor_active(feeder, 'hall_full', True)
    feeder._last_metrics_log_time = 9.0
    feeder._hall1_active_since = 8.0
    feeder.reactor.now = 10.0
    feeder._pending_remaining_mm = 7.0
    trace = []
    compute = feeder._compute_target_feed_speed

    def observed_compute():
        trace.append('compute')
        return compute()

    def observed(name, value):
        def read(*args):
            trace.append(name)
            return value
        return read

    monkeypatch.setattr(feeder, '_compute_target_feed_speed', observed_compute)
    monkeypatch.setattr(feeder.velocity_tracker, 'get_volumetric_flow',
                        observed('flow', 25.0))
    monkeypatch.setattr(feeder.reactor, 'monotonic', observed('clock', 10.0))
    monkeypatch.setattr(feeder.velocity_tracker, 'get_velocity',
                        observed('velocity', 15.0))
    monkeypatch.setattr(feeder.velocity_tracker, 'is_ready',
                        observed('ready', True))
    with caplog.at_level(logging.INFO):
        feeder._tick_debug_metrics(9.999)
        assert trace == []
        feeder._tick_debug_metrics(10.0)
        feeder._tick_debug_metrics(10.999)
    assert trace == ['compute', 'clock', 'flow', 'clock', 'velocity', 'ready']
    assert feeder._post_full_bias_clamp is True
    assert feeder._last_metrics_log_time == 10.0
    records = [r for r in caplog.records if 'buffer_metrics:' in r.getMessage()]
    assert [r.getMessage() for r in records] == [
        'buffer_metrics: state=AUTO hall=[H3:off H2:on H1:off] '
        'tracker_vel=15.0mm/s flow=25.0mm3/s high_flow=True '
        'ready=True target_speed=0.0mm/s pending_remaining=7.0mm '
        'hall1_persist=2.00s',
    ]
    assert records[0].levelno == logging.INFO


def test_disabled_metrics_do_not_run_controller(monkeypatch):
    _, feeder = build_feeder()

    def forbidden_compute():
        pytest.fail('Disabled metrics must not compute feed demand')

    monkeypatch.setattr(feeder, '_compute_target_feed_speed', forbidden_compute)
    feeder._tick_debug_metrics(10.0)
    assert feeder._last_metrics_log_time == 0.0


@pytest.mark.parametrize('console_failure', [False, True])
def test_response_uses_console_without_reentering_gcode(
        monkeypatch, caplog, console_failure):
    printer, feeder = build_feeder()
    gc = printer.lookup_object('gcode')

    def forbidden_script(*args, **kwargs):
        pytest.fail('Diagnostic response must not reenter the GCode mutex')

    monkeypatch.setattr(gc, 'run_script', forbidden_script)
    monkeypatch.setattr(gc, 'run_script_from_command', forbidden_script)
    if console_failure:
        def unavailable_console(message):
            raise RuntimeError('console unavailable')
        monkeypatch.setattr(gc, 'respond_info', unavailable_console)
    with caplog.at_level(logging.INFO):
        feeder._respond('ready')
    assert 'buffer_feeder: ready' in caplog.text
    assert gc.info_messages == ([] if console_failure else ['BufferFeeder: ready'])


def test_state_dump_preserves_clock_order_and_phase_side_effect(
        monkeypatch):
    printer, feeder = build_feeder()
    gc = printer.lookup_object('gcode')
    trace = []
    times = iter([10.0, 11.0, 12.0])
    feeder._post_full_recovery_until = 13.0
    feeder._benchmark_mode_reason = 'consumer'

    def clock():
        value = next(times)
        trace.append(('clock', value))
        return value

    def active():
        trace.append(('benchmark_active',))
        return True

    def remaining():
        trace.append(('benchmark_remaining',))
        return 4.0

    def phase(eventtime):
        trace.append(('phase', eventtime))
        feeder._print_extrusion_seen = True
        return 'PRINTING'

    def guard(eventtime):
        trace.append(('guard', eventtime))
        return 2.0

    def forbidden_script(*args, **kwargs):
        pytest.fail('State dump must not reenter the GCode mutex')

    monkeypatch.setattr(feeder.reactor, 'monotonic', clock)
    monkeypatch.setattr(feeder, '_benchmark_mode_active', active)
    monkeypatch.setattr(feeder, '_benchmark_mode_remaining', remaining)
    monkeypatch.setattr(feeder, '_refresh_print_phase', phase)
    monkeypatch.setattr(feeder, '_critical_action_guard_remaining', guard)
    monkeypatch.setattr(gc, 'run_script', forbidden_script)
    monkeypatch.setattr(gc, 'run_script_from_command', forbidden_script)
    gc.commands['BUFFER_STATE_DUMP']['handler'](None)
    assert trace == [
        ('clock', 10.0), ('benchmark_active',), ('benchmark_remaining',),
        ('clock', 11.0), ('phase', 11.0), ('clock', 12.0), ('guard', 12.0),
    ]
    assert 'post_full_recovery = 3.000s' in gc.info_messages
    assert 'benchmark_mode     = active=True left=4.0s reason=consumer' in gc.info_messages
    assert ('print_phase        = PRINTING seen_extrusion=True '
            'guard_left=2.000s reason=-') in gc.info_messages
    assert gc.info_messages[0] == '---- BUFFER STATE ----'
    assert gc.info_messages[-1] == '---- END STATE ----'
