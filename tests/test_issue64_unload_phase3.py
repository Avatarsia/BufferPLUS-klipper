import pytest

from fakes_klipper import FakeConfig, FakePrinter, FakePrintStats
from helpers import FakeGCmd, set_sensor_active
from klipper_extras import buffer_feeder


def _make_feeder():
    printer = FakePrinter()
    printer.objects['print_stats'] = FakePrintStats(state='standby')
    feeder = buffer_feeder.BufferFeeder(FakeConfig(printer=printer))
    feeder._startup_grace_done = True
    feeder._state = buffer_feeder.STATE_IDLE
    for name in ('hall_overflow', 'hall_full', 'hall_empty'):
        set_sensor_active(feeder, name, False)
    set_sensor_active(feeder, 'entrance', True)
    return feeder


def test_unload_phase3_accepts_entrance_clear_after_last_chunk(monkeypatch):
    feeder = _make_feeder()
    feeder._hall2_start_time = 1.0
    feeder._hall3_start_time = 2.0
    feeder._hall3_drop_since = 3.0
    wait_calls = []

    def wait_for_last_chunk(gcmd, direction=-1):
        wait_calls.append(direction)
        if len(wait_calls) == 2:
            set_sensor_active(feeder, 'entrance', False)

    monkeypatch.setattr(feeder, '_wait_for_move_done', wait_for_last_chunk)
    submit_calls = []
    monkeypatch.setattr(feeder, '_submit_move',
                        lambda distance, speed: submit_calls.append((distance, speed)))

    feeder.cmd_BUFFER_UNLOAD_PHASE3(FakeGCmd({'MAX_DISTANCE': 1.0}))

    assert wait_calls == [-1, -1]
    assert submit_calls == [(-1.0, feeder.unload_phase3_speed)]
    assert feeder._state == buffer_feeder.STATE_IDLE
    assert feeder._stepcompress_primed is False
    assert feeder._jam_active is False
    assert feeder._hall2_start_time is None
    assert feeder._hall3_start_time is None
    assert feeder._hall3_drop_since is None


def test_unload_phase3_raises_when_entrance_stays_blocked(monkeypatch):
    feeder = _make_feeder()
    wait_calls = []
    monkeypatch.setattr(
        feeder, '_wait_for_move_done',
        lambda gcmd, direction=-1: wait_calls.append(direction))
    submit_calls = []
    monkeypatch.setattr(
        feeder, '_submit_move',
        lambda distance, speed: submit_calls.append((distance, speed)))

    with pytest.raises(RuntimeError, match=(
            r'UNLOAD Phase 3: MAX_DISTANCE 1mm reached without '
            r'entrance clear')):
        feeder.cmd_BUFFER_UNLOAD_PHASE3(FakeGCmd({'MAX_DISTANCE': 1.0}))

    assert wait_calls == [-1, -1]
    assert submit_calls == [(-1.0, feeder.unload_phase3_speed)]
    assert feeder._state == buffer_feeder.STATE_IDLE
    assert feeder._stepcompress_primed is False
