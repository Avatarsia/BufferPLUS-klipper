"""Motor disable must not re-arm activity callbacks for ungenerated moves."""

import pytest

from fakes_klipper import FakeConfig, FakePrinter
from klipper_extras import buffer_feeder


def make_feeder():
    printer = FakePrinter()
    feeder = buffer_feeder.BufferFeeder(FakeConfig(printer=printer, values={
        'idle_anchor_mode': 'silent', 'idle_anchor_gap': 999.0,
    }))
    printer.fire_event('klippy:connect')
    feeder._startup_grace_done = True
    feeder._state = buffer_feeder.STATE_IDLE
    feeder.reactor.now = 11.0
    feeder._current_move = {'end_time': 10.025}
    feeder._last_move_end_time = 10.025
    feeder._stepcompress_primed = True
    feeder.motion_queuing.last_step_gen_time = 10.005
    return feeder


@pytest.mark.parametrize('entry', ['_schedule_stepper_disable', '_disable_stepper'])
def test_disable_waits_even_when_mcu_has_passed_move_end(entry):
    feeder = make_feeder()
    getattr(feeder, entry)()

    assert feeder._stepper_enable.disables == []
    assert feeder._pending_disable
    assert feeder._stepcompress_primed


def test_pending_disable_retries_until_generation_completes():
    feeder = make_feeder()
    feeder._pending_disable = True
    feeder._main_tick(11.0)
    assert feeder._stepper_enable.disables == []
    assert feeder._pending_disable
    assert feeder._stepcompress_primed

    feeder.motion_queuing.last_step_gen_time = 10.025
    feeder._main_tick(11.1)
    assert len(feeder._stepper_enable.disables) == 1
    assert not feeder._pending_disable
    assert not feeder._stepcompress_primed


def test_halt_clamping_does_not_hide_ungenerated_move():
    feeder = make_feeder()
    feeder._last_move_end_time = 0.0
    feeder._schedule_stepper_disable()
    assert feeder._stepper_enable.disables == []
    assert feeder._pending_disable


def test_new_enable_cancels_deferred_disable():
    feeder = make_feeder()
    feeder._schedule_stepper_disable()
    assert feeder._pending_disable
    feeder._enable_stepper()
    assert not feeder._pending_disable
    assert feeder._stepper_enable.disables == []


def test_fully_generated_move_can_disable_without_toolhead_flush():
    feeder = make_feeder()
    feeder.motion_queuing.last_step_gen_time = 10.025
    toolhead = feeder.printer.lookup_object('toolhead')
    feeder._schedule_stepper_disable()
    assert len(feeder._stepper_enable.disables) == 1
    assert not feeder._stepcompress_primed
    assert toolhead.flush_calls == 0


def test_synthetic_time_anchor_without_a_move_does_not_block_disable():
    feeder = make_feeder()
    feeder._current_move = None
    feeder._last_move_end_time = 100.0  # startup/recovery scheduling floor
    feeder.motion_queuing.last_step_gen_time = 0.0
    feeder._schedule_stepper_disable()
    assert len(feeder._stepper_enable.disables) == 1
    assert not feeder._pending_disable


def test_flush_target_is_not_treated_as_completed_generation():
    feeder = make_feeder()
    queue = feeder.motion_queuing
    # A callback sees the new target BEFORE Klipper publishes completion.
    queue.register_flush_callback(lambda flush, target: feeder._disable_stepper())
    queue.trigger_flush(10.025, 10.025)
    assert feeder._stepper_enable.disables == []
    assert feeder._pending_disable
    feeder._schedule_stepper_disable()
    assert len(feeder._stepper_enable.disables) == 1
