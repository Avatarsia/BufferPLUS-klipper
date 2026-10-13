"""Global Klipper disables must drain buffer moves before rearming activity."""

import pytest

from fakes_klipper import FakeConfig, FakePrinter
from klipper_extras import buffer_feeder


def make_feeder(printer=None, name='buffer_feeder mellow'):
    printer = printer or FakePrinter()
    feeder = buffer_feeder.BufferFeeder(FakeConfig(printer=printer, name=name, values={
        'idle_anchor_mode': 'silent', 'idle_anchor_gap': 999.0,
        'use_flush_callback_bang_bang': True,
    }))
    printer.fire_event('klippy:connect')
    feeder._startup_grace_done = True
    feeder._state = buffer_feeder.STATE_AUTO
    feeder.reactor.now = 11.0
    feeder._current_move = {'end_time': 10.025}
    feeder._last_move_end_time = 10.025
    feeder._stepcompress_primed = True
    feeder.motion_queuing.last_step_gen_time = 10.005
    return feeder


def test_global_disable_drains_and_refreshes_timestamp(monkeypatch):
    feeder = make_feeder()
    handle = feeder._stepper_enable
    drained = []

    def flush():
        assert handle.disables == []
        drained.append(feeder.motion_queuing.last_step_gen_time)
        feeder.motion_queuing.last_step_gen_time = 10.025
        feeder.reactor.now = 12.0

    monkeypatch.setattr(feeder.motion_queuing, 'flush_all_steps', flush,
                        raising=False)
    handle.motor_disable(10.1)

    assert drained == [10.005]
    assert len(handle.disables) == 1
    assert handle.disables[0] >= 12.0 + feeder.lead_time
    assert not feeder._stepcompress_primed
    assert not feeder._pending_disable


@pytest.mark.parametrize('entry', ['flush', 'submit', 'single', 'pending',
                                  'enable', 'disable'])
def test_global_drain_blocks_reentrant_motion(monkeypatch, entry):
    feeder = make_feeder()
    feeder._needs_overflow_prime = True
    feeder._pending_remaining_mm = 20.0
    feeder._pending_direction = 1
    feeder._pending_speed = 10.0
    handle = feeder._stepper_enable
    calls = []

    def flush():
        calls.append(entry)
        if entry == 'flush':
            feeder._on_mcu_flush(11.0, 11.0)
        elif entry == 'submit':
            feeder._submit_move(1.0, 10.0, forced_t0=11.1)
        elif entry == 'single':
            feeder._submit_single_trapezoid(1.0, 10.0, forced_t0=11.1)
        elif entry == 'pending':
            feeder._tick_pending_chunk(11.0)
        elif entry == 'enable':
            feeder._enable_stepper()
        else:
            handle.motor_disable(11.2)
        assert feeder.motion_queuing.append_calls == []
        assert handle.enables == []
        assert feeder._pending_remaining_mm == 20.0
        assert feeder._needs_overflow_prime
        feeder.motion_queuing.last_step_gen_time = 10.025

    monkeypatch.setattr(feeder.motion_queuing, 'flush_all_steps', flush,
                        raising=False)
    handle.motor_disable(11.2)
    assert calls == [entry]
    assert len(handle.disables) == 1


def test_global_disable_rejects_incomplete_drain(monkeypatch):
    feeder = make_feeder()
    monkeypatch.setattr(feeder.motion_queuing, 'flush_all_steps', lambda: None,
                        raising=False)
    with pytest.raises(RuntimeError, match='step generation'):
        feeder._stepper_enable.motor_disable(11.2)
    assert feeder._stepper_enable.disables == []
    assert feeder._stepcompress_primed
    assert not feeder._stepper_disable_in_progress


def test_global_disable_restores_guard_after_flush_failure(monkeypatch):
    feeder = make_feeder()

    def flush():
        raise RuntimeError('C generator failed')

    monkeypatch.setattr(feeder.motion_queuing, 'flush_all_steps', flush,
                        raising=False)
    with pytest.raises(RuntimeError, match='C generator failed'):
        feeder._stepper_enable.motor_disable(11.2)
    assert feeder._stepper_enable.disables == []
    assert feeder._stepcompress_primed
    assert not feeder._stepper_disable_in_progress


def test_global_disable_completed_move_does_not_flush(monkeypatch):
    feeder = make_feeder()
    feeder.motion_queuing.last_step_gen_time = 10.025

    def flush():
        pytest.fail('Completed move must not trigger another flush')

    monkeypatch.setattr(feeder.motion_queuing, 'flush_all_steps', flush,
                        raising=False)
    feeder._stepper_enable.motor_disable(11.2)
    assert feeder._stepper_enable.disables == [11.2]
    assert not feeder._stepcompress_primed


def test_connect_does_not_wrap_same_handle_twice():
    feeder = make_feeder()
    feeder.motion_queuing.last_step_gen_time = 10.025
    feeder._handle_connect()
    feeder._stepper_enable.motor_disable(11.2)
    assert feeder._stepper_enable.disables == [11.2]


def test_next_enable_stays_after_requested_disable_timestamp():
    feeder = make_feeder()
    feeder.motion_queuing.last_step_gen_time = 10.025
    feeder._stepper_enable.motor_disable(20.0)
    feeder._enable_stepper()
    assert feeder._stepper_enable.enables[0] >= 20.0 + feeder.lead_time


def test_global_transaction_blocks_all_selected_buffers(monkeypatch):
    printer = FakePrinter()
    first = make_feeder(printer, 'buffer_feeder first')
    second = make_feeder(printer, 'buffer_feeder second')
    se = printer.lookup_object('stepper_enable')
    calls = []

    def global_disable(names, enable):
        calls.append((names, enable))
        for owner in (first, second):
            assert owner._global_disable_in_progress
            owner._on_mcu_flush(11.0, 11.0)
            owner._tick_pending_chunk(11.0)
        assert printer.objects['motion_queuing'].append_calls == []
        printer.objects['motion_queuing'].last_step_gen_time = 10.025
        for name in names:
            se.lookup_enable(name).motor_disable(11.2)
        return True

    monkeypatch.setattr(se, 'set_motors_enable', global_disable, raising=False)
    for owner in (first, second):
        owner._needs_overflow_prime = True
        owner._pending_remaining_mm = 20.0
        owner._install_stepper_disable_guard(se)
    names = [first.stepper.get_name(), second.stepper.get_name()]
    assert se.set_motors_enable(names, False)
    assert calls == [(names, False)]
    for owner in (first, second):
        assert owner._stepper_enable.disables == [11.2]
        assert not owner._global_disable_in_progress
        assert not owner._needs_overflow_prime
        assert owner._pending_remaining_mm == 0.0
        assert not owner._stepcompress_primed


def test_global_transaction_releases_all_flags_on_error(monkeypatch):
    feeder = make_feeder()
    se = feeder.printer.lookup_object('stepper_enable')

    def fail(names, enable):
        assert feeder._global_disable_in_progress
        raise RuntimeError('global flush failed')

    monkeypatch.setattr(se, 'set_motors_enable', fail, raising=False)
    feeder._install_stepper_disable_guard(se)
    with pytest.raises(RuntimeError, match='global flush failed'):
        se.set_motors_enable([feeder.stepper.get_name()], False)
    assert not feeder._global_disable_in_progress
    assert feeder._stepper_enable.disables == []


def test_nested_global_disable_preserves_outer_submit_guard(monkeypatch):
    feeder = make_feeder()
    se = feeder.printer.lookup_object('stepper_enable')
    nested = []

    def original(names, enable):
        assert feeder._global_disable_in_progress
        if not nested:
            nested.append(True)
            se.set_motors_enable(names, enable)
            assert feeder._global_disable_in_progress
        return False

    monkeypatch.setattr(se, 'set_motors_enable', original, raising=False)
    feeder._install_stepper_disable_guard(se)
    se.set_motors_enable([feeder.stepper.get_name()], False)
    assert not feeder._global_disable_in_progress


@pytest.mark.parametrize('enable,names', [(True, ['buffer_feeder mellow']),
                                         (False, ['extruder'])])
def test_global_guard_preserves_unrelated_operations(monkeypatch, enable, names):
    feeder = make_feeder()
    se = feeder.printer.lookup_object('stepper_enable')
    calls = []

    def original(stepper_names, enabled):
        assert not feeder._global_disable_in_progress
        calls.append((stepper_names, enabled))
        return False

    monkeypatch.setattr(se, 'set_motors_enable', original, raising=False)
    feeder._install_stepper_disable_guard(se)
    assert se.set_motors_enable(names, enable) is False
    assert calls == [(names, enable)]


def test_targeted_disable_keeps_sync_binding_and_other_buffer_work(monkeypatch):
    printer = FakePrinter()
    first = make_feeder(printer, 'buffer_feeder first')
    second = make_feeder(printer, 'buffer_feeder second')
    first._stepper_synced_to = 'extruder'
    extruder_trapq = printer.objects['extruder'].get_trapq()
    first.stepper.set_trapq(extruder_trapq)
    second._pending_remaining_mm = 20.0
    second._needs_overflow_prime = True
    se = printer.lookup_object('stepper_enable')

    def original(names, enable):
        assert first._global_disable_in_progress
        assert not second._global_disable_in_progress
        printer.objects['motion_queuing'].last_step_gen_time = 10.025
        for name in names:
            se.lookup_enable(name).motor_disable(11.2)
        return True

    monkeypatch.setattr(se, 'set_motors_enable', original, raising=False)
    for owner in (first, second):
        owner._install_stepper_disable_guard(se)
    se.set_motors_enable([first.stepper.get_name()], False)
    assert first._stepper_synced_to == 'extruder'
    assert first.stepper.trapq is extruder_trapq
    assert second._stepper_enable.disables == []
    assert second._pending_remaining_mm == 20.0
    assert second._needs_overflow_prime
