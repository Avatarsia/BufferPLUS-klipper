import pytest

from helpers import build_feeder, set_sensor_active
from klipper_extras import buffer_feeder
from klipper_extras.buffer_sensors import LoadEndstopMonitor


def make_stream():
    printer, feeder = build_feeder(
        state=buffer_feeder.STATE_MANUAL_FEED, fire_connect=True)
    for name in ('hall_empty', 'hall_full', 'hall_overflow'):
        set_sensor_active(feeder, name, False)
    set_sensor_active(feeder, 'entrance', True)
    printer.reactor.now = 10.0
    return printer, feeder


def queued_distance(feeder):
    return sum(call[12] * (call[2] + call[3])
               for call in feeder.motion_queuing.append_calls
               if call[0] is feeder.trapq)


def drain_pending(printer, feeder):
    printer.objects['toolhead'].last_move_time = 0.0
    for _ in range(8):
        if feeder._pending_remaining_mm <= 0:
            return
        printer.reactor.now = max(printer.reactor.now,
                                  feeder._last_move_end_time)
        feeder._tick_pending_chunk(printer.reactor.now)
    pytest.fail('pending stream did not drain')


def seed_pending(feeder, distance=20., direction=1.):
    feeder._pending_remaining_mm = distance
    feeder._pending_direction = direction
    feeder._pending_speed = 25.
    feeder._pending_submit_chunk_cap = 9.


def test_followup_far_future_skip_preserves_distance_then_recovers():
    printer, feeder = make_stream()
    seed_pending(feeder)
    printer.objects['toolhead'].last_move_time = 100.
    feeder._tick_pending_chunk(printer.reactor.now)
    assert queued_distance(feeder) == 0.
    assert feeder._pending_remaining_mm == 20.
    drain_pending(printer, feeder)
    assert queued_distance(feeder) == pytest.approx(20.)
    assert feeder._pending_remaining_mm == 0.
    assert feeder._pending_submit_chunk_cap is None


@pytest.mark.parametrize('distance', [20., 5.])
def test_initial_far_future_skip_preserves_entire_request(distance):
    printer, feeder = make_stream()
    printer.objects['toolhead'].last_move_time = 100.
    feeder._submit_move(distance, 25., submit_chunk_cap=9.)
    assert queued_distance(feeder) == 0.
    assert feeder._pending_remaining_mm == distance
    drain_pending(printer, feeder)
    assert queued_distance(feeder) == pytest.approx(distance)
    assert feeder._pending_remaining_mm == 0.


@pytest.mark.parametrize('direction', [1., -1.])
def test_successful_stream_records_each_queued_chunk(direction):
    printer, feeder = make_stream()
    feeder._submit_move(direction * 20., 25., submit_chunk_cap=9.)
    assert queued_distance(feeder) == pytest.approx(9.)
    assert feeder._pending_remaining_mm == pytest.approx(11.)
    drain_pending(printer, feeder)
    assert queued_distance(feeder) == pytest.approx(20.)
    assert feeder._pending_remaining_mm == 0.
    assert all(call[8] == direction
               for call in feeder.motion_queuing.append_calls)


@pytest.mark.parametrize('stop', ['hall2', 'endstop', 'new_session'])
def test_stream_end_clears_cap_and_park_without_clamping_cursor(stop):
    printer, feeder = make_stream()
    feeder._submit_move(20., 25., submit_chunk_cap=9.)
    feeder._park_full_active = True
    cursor = feeder._last_move_end_time
    current = feeder._current_move
    if stop == 'hall2':
        feeder._state = buffer_feeder.STATE_AUTO
        set_sensor_active(feeder, 'hall_full', True)
        feeder._tick_pending_chunk(printer.reactor.now)
    elif stop == 'endstop':
        class PresentSensor:
            def get_status(self, eventtime):
                return {'enabled': True, 'filament_detected': True}
        monitor = LoadEndstopMonitor(feeder, PresentSensor(), 20.)
        assert monitor.poll(printer.reactor.now)
    else:
        feeder._submit_move(1., 25.)
    assert feeder._pending_remaining_mm == 0.
    assert feeder._pending_submit_chunk_cap is None
    assert feeder._park_full_active is False
    if stop != 'new_session':
        assert feeder._last_move_end_time == cursor
        assert feeder._current_move is current


def test_terminal_endstop_during_initial_submit_does_not_rearm_pending():
    printer, feeder = make_stream()
    class PresentSensor:
        def get_status(self, eventtime):
            return {'enabled': True, 'filament_detected': True}
    feeder._load_endstop_move = LoadEndstopMonitor(feeder, PresentSensor(), 20.)
    feeder._submit_move(20., 25., submit_chunk_cap=9.)
    assert queued_distance(feeder) == 0.
    assert feeder._pending_remaining_mm == 0.
    assert feeder._pending_submit_chunk_cap is None


def test_append_exception_does_not_consume_pending(monkeypatch):
    printer, feeder = make_stream()
    seed_pending(feeder)
    def fail_append(*args):
        raise RuntimeError('append failed')
    monkeypatch.setattr(feeder, 'trapq_append', fail_append)
    with pytest.raises(RuntimeError, match='append failed'):
        feeder._tick_pending_chunk(printer.reactor.now)
    assert feeder._pending_remaining_mm == 20.
    assert queued_distance(feeder) == 0.


@pytest.mark.parametrize('guard', [
    '_stepper_synced_to', '_stepper_disable_in_progress',
    '_global_disable_in_progress'])
def test_followup_submit_guard_preserves_pending_until_released(guard):
    printer, feeder = make_stream()
    seed_pending(feeder)
    setattr(feeder, guard, 'extruder' if guard == '_stepper_synced_to' else True)
    feeder._tick_pending_chunk(printer.reactor.now)
    assert queued_distance(feeder) == 0.
    assert feeder._pending_remaining_mm == 20.
    setattr(feeder, guard, None if guard == '_stepper_synced_to' else False)
    drain_pending(printer, feeder)
    assert queued_distance(feeder) == pytest.approx(20.)
    assert feeder._pending_remaining_mm == 0.


@pytest.mark.parametrize('stop', ['hall1', 'halt', 'demand', 'exhaustion'])
def test_pending_end_preserves_snapshot_and_caller_specific_state(stop, monkeypatch):
    printer, feeder = make_stream()
    feeder._submit_move(20., 25., submit_chunk_cap=9.)
    feeder._park_full_active = stop != 'demand'
    feeder._continuous_feed = True
    feeder._feed_deadline_time = 99.
    feeder._overflow_resume_mm = 41.
    feeder._overflow_resume_dir = -1.
    feeder._overflow_resume_spd = 17.
    cursor = feeder._last_move_end_time
    current = feeder._current_move
    if stop == 'hall1':
        set_sensor_active(feeder, 'hall_overflow', True)
        feeder._tick_pending_chunk(printer.reactor.now)
    elif stop == 'halt':
        feeder._halt_motion()
    elif stop == 'demand':
        feeder._state = buffer_feeder.STATE_AUTO
        monkeypatch.setattr(feeder, '_compute_target_feed_speed', lambda: 0.)
        feeder._tick_pending_chunk(printer.reactor.now)
    else:
        drain_pending(printer, feeder)
    assert feeder._pending_remaining_mm == 0.
    assert feeder._pending_submit_chunk_cap is None
    assert feeder._park_full_active is False
    assert (feeder._overflow_resume_mm, feeder._overflow_resume_dir,
            feeder._overflow_resume_spd) == (41., -1., 17.)
    if stop == 'halt':
        assert feeder._continuous_feed is False
        assert feeder._feed_deadline_time is None
        assert feeder._last_move_end_time < cursor
        assert feeder._current_move is current
    elif stop != 'exhaustion':
        assert feeder._last_move_end_time == cursor
        assert feeder._current_move is current
        assert feeder._feed_deadline_time == 99.
        assert feeder._continuous_feed is (stop == 'hall1')


@pytest.mark.parametrize('hall', ['hall_full', 'hall_overflow', None])
def test_park_bypasses_zero_demand_after_sensor_guards(hall, monkeypatch):
    printer, feeder = make_stream()
    seed_pending(feeder)
    feeder._state = buffer_feeder.STATE_AUTO
    feeder._park_full_active = True
    monkeypatch.setattr(feeder, '_compute_target_feed_speed', lambda: 0.)
    if hall is not None:
        set_sensor_active(feeder, hall, True)
    feeder._tick_pending_chunk(printer.reactor.now)
    if hall is not None:
        assert queued_distance(feeder) == 0.
        assert feeder._pending_remaining_mm == 0.
        assert feeder._pending_submit_chunk_cap is None
        assert feeder._park_full_active is False
    else:
        assert queued_distance(feeder) == pytest.approx(9.)
        assert feeder._pending_remaining_mm == 11.
        assert feeder._park_full_active is True


@pytest.mark.parametrize('fault', ['hall1', 'jam', 'halt'])
def test_retract_recovery_only_halt_ends_pending(fault):
    printer, feeder = make_stream()
    seed_pending(feeder, direction=-1.)
    feeder._state = buffer_feeder.STATE_MANUAL_RETRACT
    if fault == 'hall1':
        set_sensor_active(feeder, 'hall_overflow', True)
    elif fault == 'jam':
        feeder._jam_active = True
    else:
        feeder._halt_requested = True
    feeder._tick_pending_chunk(printer.reactor.now)
    if fault == 'halt':
        assert queued_distance(feeder) == 0.
        assert feeder._pending_remaining_mm == 0.
    else:
        assert queued_distance(feeder) == pytest.approx(9.)
        assert feeder._pending_remaining_mm == 11.
        assert feeder.motion_queuing.append_calls[-1][8] == -1.
    assert feeder._jam_active is (fault == 'jam')


def test_overflow_snapshots_pending_before_halt_then_resumes_queue():
    printer, feeder = make_stream()
    seed_pending(feeder)
    feeder._state = buffer_feeder.STATE_LOADING_PULL
    feeder._enter_overflow()
    assert feeder._pending_remaining_mm == 0.
    assert feeder._overflow_resume_mm == 20.
    assert feeder._overflow_resume_dir == 1.
    assert feeder._overflow_resume_spd == 25.
    feeder._exit_overflow()
    assert feeder._overflow_resume_mm == 0.
    assert feeder._state == buffer_feeder.STATE_LOADING_PULL
    assert queued_distance(feeder) + feeder._pending_remaining_mm == pytest.approx(20.)
    drain_pending(printer, feeder)
    assert queued_distance(feeder) == pytest.approx(20.)


def test_submit_result_reports_actual_append_and_skip():
    from klipper_extras.buffer_pending import SubmitResult
    printer, feeder = make_stream()
    printer.objects['toolhead'].last_move_time = 100.
    assert feeder._submit_single_trapezoid(5., 25.) is SubmitResult.SKIPPED
    assert queued_distance(feeder) == 0.
    printer.objects['toolhead'].last_move_time = 0.
    assert feeder._submit_single_trapezoid(5., 25.) is SubmitResult.QUEUED
    assert queued_distance(feeder) == pytest.approx(5.)


def test_synthetic_anchor_skip_does_not_retry_or_block(monkeypatch):
    printer, feeder = make_stream()
    printer.objects['toolhead'].last_move_time = 100.
    def unexpected_wait(when):
        pytest.fail('skipped synthetic anchor must not wait for pending retry')
    monkeypatch.setattr(printer.reactor, 'pause', unexpected_wait)
    assert feeder.sync._submit_anchor_move() == 1.
    assert queued_distance(feeder) == 0.
    assert feeder._pending_remaining_mm == 0.
    assert feeder._pending_submit_chunk_cap is None


@pytest.mark.parametrize('forced_t0', [None, 10.3])
def test_successful_synthetic_anchor_retains_queue_and_drain(forced_t0, monkeypatch):
    printer, feeder = make_stream()
    if forced_t0 is not None:
        printer.objects['toolhead'].last_move_time = 100.
    original_pause = printer.reactor.pause
    waits = []
    def bounded_wait(when):
        waits.append(when)
        assert len(waits) < 20, 'anchor drain did not terminate'
        return original_pause(when)
    monkeypatch.setattr(printer.reactor, 'pause', bounded_wait)
    assert feeder.sync._submit_anchor_move(forced_t0=forced_t0) == 1.
    assert queued_distance(feeder) == pytest.approx(.05)
    assert not feeder._move_in_flight()
    assert feeder._pending_remaining_mm == 0.
