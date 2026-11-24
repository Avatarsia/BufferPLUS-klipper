"""Preserve callback boundaries when extracting anchor and motor policy."""
import pytest

from helpers import build_feeder
from klipper_extras.buffer_pending import SubmitResult


def make_feeder():
    printer, feeder = build_feeder({'lead_time': 0.1}, fire_connect=True)
    feeder.reactor.now = 10.0
    feeder._last_move_end_time = 9.0
    feeder._last_enable_schedule_time = 10.6
    feeder._current_move = {'end_time': 10.8}
    return printer, feeder


def test_gap_lookup_keeps_pre_callback_floors_and_reads_post_callback_lme():
    printer, feeder = make_feeder()
    calls = []

    def lookup_time():
        calls.append(True)
        feeder._last_move_end_time = 11.3
        feeder._last_enable_schedule_time = 20.0
        feeder._current_move['end_time'] = 30.0
        return 10.2

    printer.objects['toolhead'].get_last_move_time = lookup_time
    plan = feeder._plan_t0_anchor(None, 10.0, True, False, False)
    assert calls == [True]
    assert plan.t0 == pytest.approx(11.3)
    assert plan.enable_floor == 10.6
    assert plan.toolhead_time == 10.2


@pytest.mark.parametrize('forced_t0,lme,expected', [
    (10.7, 9.0, 10.8), (None, 10.9, 10.9),
])
def test_forced_and_abut_paths_never_lookup_toolhead(forced_t0, lme, expected):
    printer, feeder = make_feeder()
    feeder._last_move_end_time = lme

    def forbidden():
        pytest.fail('Unexpected toolhead lookup/flush')

    printer.objects['toolhead'].get_last_move_time = forbidden
    plan = feeder._plan_t0_anchor(forced_t0, 10.0, True, False, False)
    assert plan.t0 == pytest.approx(expected)


def test_gap_lookup_reads_live_silent_mode_and_lead_time():
    printer, feeder = make_feeder()
    feeder._current_move = None
    feeder._last_enable_schedule_time = 0.0
    feeder.idle_anchor_mode = 'move'

    def lookup_time():
        feeder.idle_anchor_mode = 'silent'
        feeder.lead_time = 0.2
        return 1.0

    printer.objects['toolhead'].get_last_move_time = lookup_time
    assert feeder._plan_t0_anchor(None, 10.0, True, False, False).t0 == 10.2


def test_submit_reads_mcu_floor_after_reprime_and_enable(monkeypatch):
    printer, feeder = make_feeder()
    feeder._stepcompress_primed = False
    feeder._current_move = None

    def reprime():
        feeder.reactor.now = 10.4

    monkeypatch.setattr(printer.objects['toolhead'], 'flush_step_generation', reprime)
    printer.objects['toolhead'].last_move_time = 10.0
    feeder.idle_anchor_mode = 'silent'
    result = feeder._submit_single_trapezoid(1.0, 10.0)
    assert result is SubmitResult.QUEUED
    assert feeder._stepper_enable.enables[-1] >= 10.5
    assert feeder.motion_queuing.append_calls[-1][1] >= 10.5


def test_handle_replacement_installs_guard_for_the_new_handle():
    from fakes_klipper import FakeStepperEnableHandle

    printer, feeder = make_feeder()
    feeder._current_move = None
    original = feeder._stepper_enable
    replacement = FakeStepperEnableHandle()
    feeder._stepper_enable = replacement
    feeder._install_stepper_disable_guard()
    feeder._install_stepper_disable_guard()
    replacement.motor_disable(12.0)
    assert replacement.disables == [12.0]
    assert original.disables == []


def test_local_nested_guard_preserves_outer_flag_on_error(monkeypatch):
    printer, feeder = make_feeder()
    feeder.motion_queuing.last_step_gen_time = 0.0
    error = RuntimeError('drain failed')

    def flush():
        assert feeder._stepper_disable_in_progress
        assert feeder._guard_motor_disable(12.0) is None
        assert feeder._stepper_disable_in_progress
        raise error

    monkeypatch.setattr(feeder.motion_queuing, 'flush_all_steps', flush, raising=False)
    with pytest.raises(RuntimeError) as caught:
        feeder._guard_motor_disable(11.0)
    assert caught.value is error
    assert not feeder._stepper_disable_in_progress
    assert feeder._stepper_enable.disables == []
