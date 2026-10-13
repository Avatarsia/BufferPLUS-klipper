"""Observable queue, callback and live-owner contracts for streaming."""
import pytest

from helpers import build_feeder, set_sensor_active
from klipper_extras import buffer_feeder
from klipper_extras.buffer_pending import SubmitResult


def make_feeder():
    printer, feeder = build_feeder(
        state=buffer_feeder.STATE_MANUAL_FEED, fire_connect=True)
    for sensor in ('hall_overflow', 'hall_full', 'hall_empty'):
        set_sensor_active(feeder, sensor, False)
    set_sensor_active(feeder, 'entrance', True)
    printer.reactor.now = 10.0
    return printer, feeder


@pytest.mark.parametrize('forced_t0,skip_enable', [
    (None, False), (10.3, False), (10.3, True),
])
def test_submit_callback_order_keeps_fresh_mcu_after_reprime_and_enable(
        monkeypatch, forced_t0, skip_enable):
    printer, feeder = make_feeder()
    trace = []
    monkeypatch.setattr(feeder.reactor, 'monotonic', lambda: feeder.reactor.now)

    def mcu_time(now):
        trace.append(('mcu', now))
        return now

    monkeypatch.setattr(feeder.stepper.get_mcu(), 'estimated_print_time', mcu_time)
    for name in ('_sanitize_forced_t0_floors', '_reprime_stepcompress_if_needed',
                 '_enable_stepper', '_compute_t0_anchor',
                 '_append_trapezoid_and_record'):
        original = getattr(feeder, name)

        def observe(*args, _name=name, _original=original, **kwargs):
            trace.append((_name,))
            result = _original(*args, **kwargs)
            if _name in ('_reprime_stepcompress_if_needed', '_enable_stepper'):
                feeder.reactor.now += 0.25
            return result

        monkeypatch.setattr(feeder, name, observe)
    assert feeder._submit_single_trapezoid(
        1.0, 10.0, forced_t0=forced_t0,
        skip_enable=skip_enable) is SubmitResult.QUEUED
    stages = [entry[0] for entry in trace if entry[0] != 'mcu']
    expected = [] if forced_t0 is None else ['_sanitize_forced_t0_floors']
    expected += ['_reprime_stepcompress_if_needed']
    if not skip_enable:
        expected += ['_enable_stepper']
    expected += ['_compute_t0_anchor', '_append_trapezoid_and_record']
    assert stages == expected
    anchor_index = trace.index(('_compute_t0_anchor',))
    expected_now = 10.25 if skip_enable else 10.5
    assert trace[anchor_index - 1] == ('mcu', expected_now)
    assert feeder.motion_queuing.append_calls[-1][1] >= expected_now
    assert len(feeder._stepper_enable.enables) == (0 if skip_enable else 1)


def test_append_endstop_record_precedes_live_accounting_and_queue_notification(
        monkeypatch):
    _, feeder = make_feeder()
    trace = []
    feeder._commanded_pos = 2.0
    feeder._measure_load_active = True
    original_append = feeder.trapq_append

    def append(*args):
        trace.append(('append', args[5], feeder._commanded_pos))
        original_append(*args)

    class EndstopObserver:
        def record(self, *args):
            trace.append(('record', feeder._commanded_pos))
            feeder._commanded_pos = 100.0
            feeder._feed_distance_accumulator = 40.0
            feeder._measure_load_distance = 50.0

    def activity(end_time):
        trace.append(('activity', feeder._commanded_pos,
                      feeder._feed_distance_accumulator,
                      feeder._measure_load_distance,
                      feeder._current_move['end_time'] == end_time))

    feeder._load_endstop_move = EndstopObserver()
    monkeypatch.setattr(feeder, 'trapq_append', append)
    monkeypatch.setattr(feeder.motion_queuing, 'note_mcu_movequeue_activity', activity)
    feeder._append_trapezoid_and_record(10.3, 3.0, 20.0)
    assert trace == [('append', 2.0, 2.0), ('record', 2.0),
                     ('activity', 103.0, 43.0, 53.0, True)]
    assert feeder._accumulated_feed_distance == 3.0


def test_flush_debug_callback_sees_armed_session_and_calls_live_owner_wrapper(
        monkeypatch):
    _, feeder = make_feeder()
    feeder._state = buffer_feeder.STATE_AUTO
    trace = []
    monkeypatch.setattr(feeder, '_compute_target_feed_speed', lambda: 25.0)
    monkeypatch.setattr(feeder, '_auto_submit_permission', lambda now: (True, 'printing'))

    def debug(event, *args, **kwargs):
        if event == 'flush_submit':
            trace.append(('session', feeder._continuous_feed,
                          feeder._continuous_feed_speed))
            # Diagnostics can replace a callback after construction.
            monkeypatch.setattr(feeder, '_submit_move',
                                lambda *a, **kw: trace.append(('submit', a, kw)))

    monkeypatch.setattr(feeder, '_debug_event', debug)
    feeder._flush_submit_streaming_chunk(10.0, 10.0)
    chunk = feeder.interrupt_chunk_mm
    assert trace == [('session', True, 25.0),
                     ('submit', (chunk, 25.0),
                      {'forced_t0': 10.0 + feeder.lead_time,
                       'streaming': False, 'submit_chunk_cap': chunk})]


def test_interleaved_owners_keep_pending_queues_and_callback_replacements_separate(
        monkeypatch):
    printer_a, first = make_feeder()
    _, second = make_feeder()
    first._submit_move(20.0, 25.0, submit_chunk_cap=9.0)
    second._submit_move(-5.0, 10.0)
    assert first._pending_remaining_mm == 11.0
    assert second._pending_remaining_mm == 0.0
    second_queue = list(second.motion_queuing.append_calls)
    calls = []
    original = first._submit_single_trapezoid

    def continuation(*args, **kwargs):
        calls.append((args, kwargs))
        return original(*args, **kwargs)

    monkeypatch.setattr(first, '_submit_single_trapezoid', continuation)
    printer_a.reactor.now = first._last_move_end_time
    first._tick_pending_chunk(printer_a.reactor.now)
    assert calls == [((9.0, 25.0), {'streaming': True})]
    assert first._pending_remaining_mm == 2.0
    assert first._commanded_pos == 18.0
    assert second._commanded_pos == -5.0
    assert second.motion_queuing.append_calls == second_queue
