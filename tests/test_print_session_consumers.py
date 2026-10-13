"""Observable print-session contracts at event, status and motion consumers."""
import pytest

from helpers import build_feeder, set_sensor_active
from klipper_extras import buffer_feeder


def make_feeder(print_state='printing', **values):
    printer, feeder = build_feeder(
        values, print_state=print_state, fire_connect=True,
        state=buffer_feeder.STATE_AUTO)
    for sensor in ('hall_empty', 'hall_full', 'hall_overflow'):
        set_sensor_active(feeder, sensor, False)
    set_sensor_active(feeder, 'entrance', True)
    return printer, feeder


def test_registered_print_event_uses_clock_after_live_status_read(monkeypatch):
    printer, feeder = make_feeder()
    feeder.reactor.now = 10.0
    seen = []
    stats = printer.objects['print_stats']

    def delayed_status(eventtime):
        seen.append(eventtime)
        feeder.reactor.now += 0.25
        return {'state': stats.state}

    monkeypatch.setattr(stats, 'get_status', delayed_status)
    printer.fire_event('idle_timeout:printing', 999.0)

    assert seen == [10.0, 10.251]
    assert feeder._print_running is True
    assert feeder._print_phase_since == 10.251
    assert feeder._critical_action_guard_until == pytest.approx(
        10.251 + feeder.critical_action_guard_s)
    assert feeder._critical_action_guard_reason == 'print_start'


@pytest.mark.parametrize('print_state, event_running, flush_active', [
    ('standby', False, False), ('paused', True, False),
    ('printing', True, True), ('', True, False),
])
def test_event_and_flush_print_detection_keep_context_policy(
        print_state, event_running, flush_active):
    printer, feeder = make_feeder(print_state)
    feeder._print_running = False
    printer.fire_event('idle_timeout:printing')
    assert feeder._print_running is event_running
    assert feeder._is_active_print_state(20.0) is flush_active
    # The next consumer must see live state, including serial-print fallback.
    printer.objects['print_stats'].state = 'printing'
    assert feeder._is_active_print_state(20.0) is True


def test_permission_keeps_owner_getter_hooks_and_supplied_time(monkeypatch):
    _, feeder = make_feeder()
    feeder.reactor.now = 100.0
    feeder._critical_action_guard_until = 10.5
    trace = []

    def state(eventtime):
        trace.append(('state', eventtime))
        feeder.reactor.now += 0.25
        return 'printing'

    def velocity():
        trace.append(('velocity',))
        return True, 15.0

    monkeypatch.setattr(feeder, '_get_print_stats_state', state)
    monkeypatch.setattr(feeder, '_get_tracker_velocity', velocity)
    assert feeder._auto_submit_permission(10.0) == (False, 'guarded')
    assert trace == [('state', 10.0), ('velocity',), ('state', 10.0)]
    assert feeder._print_extrusion_seen is True
    assert feeder._print_phase_since == 10.0
    assert feeder._auto_submit_permission(11.0) == (True, 'active')


def test_resume_jam_recovery_hooks_keep_order_before_auto(monkeypatch):
    printer, feeder = make_feeder('paused')
    feeder._state = buffer_feeder.STATE_JAM
    feeder._jam_active = True
    feeder._bang_bang_suspended = True
    trace = []
    names = ('_clear_recovery_flags', '_prepare_post_jam_recovery',
             '_try_restore_gcode_state', '_enable_stepper', '_set_state')
    for name in names:
        original = getattr(feeder, name)

        def observe(*args, _name=name, _original=original, **kwargs):
            trace.append(_name)
            return _original(*args, **kwargs)

        monkeypatch.setattr(feeder, name, observe)
    printer.fire_event('idle_timeout:printing')

    assert trace == list(names)
    assert feeder._state == buffer_feeder.STATE_AUTO
    assert feeder._jam_active is False
    assert feeder._bang_bang_suspended is False
    assert feeder._critical_action_guard_reason == 'jam_exit'


def test_print_end_submit_observes_oneshot_before_pending_bypass(monkeypatch):
    printer, feeder = make_feeder('cancelled')
    feeder._print_running = True
    feeder._bang_bang_suspended = True
    observed = []

    def submit(distance, speed, **kwargs):
        observed.append((feeder._park_full_attempted,
                         feeder._park_full_active,
                         feeder._bang_bang_suspended,
                         distance, speed, kwargs))
        feeder._pending_remaining_mm = 3.0

    monkeypatch.setattr(feeder, '_submit_move', submit)
    printer.fire_event('idle_timeout:ready')
    assert observed == [(True, False, False,
                         feeder.park_full_max_mm, feeder.feed_speed,
                         {'submit_chunk_cap': feeder._effective_interrupt_chunk_mm(
                             feeder._print_phase_since)})]
    assert feeder._park_full_active is True
    assert feeder._print_running is False
    printer.fire_event('idle_timeout:printing')
    printer.fire_event('idle_timeout:ready')
    assert len(observed) == 1
