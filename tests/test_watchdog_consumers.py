"""Observable watchdog callback, clock-domain and live-owner contracts."""
import logging

import pytest

from helpers import build_feeder, set_sensor_active
from klipper_extras import buffer_feeder


def make_feeder(print_state='standby'):
    printer, feeder = build_feeder(
        {'idle_motor_disable': True}, state=buffer_feeder.STATE_AUTO,
        print_state=print_state, fire_connect=True)
    for name in ('hall_overflow', 'hall_full', 'hall_empty', 'entrance'):
        set_sensor_active(feeder, name, False)
    feeder.reactor.now = 20.0
    feeder._last_move_end_time = 0.0
    feeder._last_idle_anchor_time = 0.0
    feeder._last_extruder_motion_time = -100.0
    return printer, feeder


def test_diagnostic_and_status_latency_precede_live_gate_and_anchor(monkeypatch):
    printer, feeder = make_feeder()
    trace = []
    feeder._continuous_feed = True

    def mcu_time(now):
        trace.append(('mcu', now))
        return now + 100.0

    original_debug = logging.debug

    def debug(message, *args, **kwargs):
        original_debug(message, *args, **kwargs)
        if 'watchdog skip' in message:
            trace.append(('diagnostic', feeder.reactor.now))
            feeder.reactor.now += 0.25
            feeder._continuous_feed = False

    def status(eventtime):
        trace.append(('status', eventtime, feeder.reactor.now))
        feeder.reactor.now += 0.5
        return {'state': 'standby'}

    def anchor(**kwargs):
        trace.append(('anchor', kwargs, feeder.reactor.now))
        feeder._last_move_end_time = 120.8

    monkeypatch.setattr(feeder.stepper.get_mcu(), 'estimated_print_time', mcu_time)
    monkeypatch.setattr(logging, 'debug', debug)
    monkeypatch.setattr(printer.objects['print_stats'], 'get_status', status)
    monkeypatch.setattr(feeder.sync, '_submit_anchor_move', anchor)
    monkeypatch.setattr(feeder, '_schedule_stepper_disable',
                        lambda: trace.append(('disable',)))
    assert feeder._tick_idle_watchdog(7.0) is None
    assert trace == [
        ('mcu', 20.0), ('diagnostic', 20.001), ('status', 7.0, 20.251),
        ('mcu', 20.751),
        ('anchor', {'skip_enable': True, 'speed': feeder.idle_anchor_speed}, 20.752),
        ('disable',),
    ]
    assert feeder._last_watchdog_skip_log_time == 120.0
    assert feeder._last_idle_anchor_time == 120.751


@pytest.mark.parametrize('flush_time, expected', [
    (0.0, (True, True, False)),
    (110.0, (True, True, False)),
    (109.999, (False, True, True)),
])
def test_print_override_uses_mcu_time_and_strict_boot_boundary(
        monkeypatch, flush_time, expected):
    printer, feeder = make_feeder('printing')
    feeder._last_mcu_flush_time = flush_time
    calls = []
    monkeypatch.setattr(feeder.stepper.get_mcu(), 'estimated_print_time',
                        lambda now: calls.append(now) or now + 100.0)
    assert feeder._idle_watchdog_print_activity(7.0) == expected
    assert calls == ([] if flush_time == 0.0 else [20.0])
    assert printer.objects['print_stats'].state == 'printing'


def test_owner_anchor_hook_observes_live_status_change(monkeypatch):
    printer, feeder = make_feeder()
    calls = []

    def status(eventtime):
        set_sensor_active(feeder, 'hall_full', True)
        feeder.idle_motor_disable = False
        return {'state': 'standby'}

    monkeypatch.setattr(printer.objects['print_stats'], 'get_status', status)
    monkeypatch.setattr(feeder, '_submit_idle_watchdog_anchor',
                        lambda *args: calls.append(args))
    feeder._tick_idle_watchdog(20.0)
    assert calls == []
    # The next call observes changed runtime settings, without reconstructing.
    monkeypatch.setattr(printer.objects['print_stats'], 'get_status',
                        lambda eventtime: {'state': 'standby'})
    feeder.idle_motor_disable = True
    feeder._tick_idle_watchdog(20.0)
    assert calls == [(20.002, 20.002, False, True)]


def test_instance_local_watchdog_hooks_and_main_tick_dispatch(monkeypatch):
    _, first = make_feeder()
    _, second = make_feeder('printing')
    calls = []
    original = first._tick_idle_watchdog

    def watchdog(eventtime):
        calls.append(('tick', eventtime))
        return original(eventtime)

    monkeypatch.setattr(first, '_tick_idle_watchdog', watchdog)
    monkeypatch.setattr(first, '_log_idle_watchdog_blockers',
                        lambda eventtime: calls.append(('diagnostic', eventtime)))
    monkeypatch.setattr(first, '_idle_watchdog_print_activity',
                        lambda eventtime: (False, True, False))
    monkeypatch.setattr(first, '_submit_idle_watchdog_anchor',
                        lambda *args: calls.append(('anchor', args)))
    assert first._main_tick(20.0) == 20.0 + buffer_feeder.MAIN_TICK_INTERVAL
    assert calls == [('tick', 20.0), ('diagnostic', 20.0),
                     ('anchor', (20.0, 20.0, False, True))]
    assert second._idle_watchdog_print_activity(20.0) == (True, True, False)
    assert second._last_idle_anchor_time == 0.0
