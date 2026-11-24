"""Observable legacy command precedence and owner callback contracts."""
import pytest

from helpers import FakeGCmd, build_feeder, set_sensor_active
from klipper_extras import buffer_feeder


def make_feeder():
    printer, feeder = build_feeder(state=buffer_feeder.STATE_IDLE)
    for sensor in ('hall_empty', 'hall_full', 'hall_overflow'):
        set_sensor_active(feeder, sensor, False)
    return printer, feeder


@pytest.mark.parametrize('fault', ['jam', 'hall1', 'busy'])
def test_phase1_safety_error_precedes_invalid_distance(fault):
    _, feeder = make_feeder()
    if fault == 'jam':
        feeder._jam_active = True
    elif fault == 'hall1':
        set_sensor_active(feeder, 'hall_overflow', True)
    else:
        feeder._state = buffer_feeder.STATE_INITIAL_GRIP
    with pytest.raises(RuntimeError):
        feeder.cmd_BUFFER_LOAD_PHASE1(FakeGCmd({'DISTANCE': 0}))
    assert feeder._pending_remaining_mm == 0


@pytest.mark.parametrize('values', [
    {'MAX_DISTANCE': 0}, {'SPEED': 0}, {'STABLE_TIMEOUT': -1},
    {'OVERFLOW_OK': 2}, {'CHUNK_DISTANCE': 0},
])
def test_phase3_invalid_option_precedes_jam(values):
    _, feeder = make_feeder()
    feeder._jam_active = True
    with pytest.raises(ValueError):
        feeder.cmd_BUFFER_LOAD_PHASE3(FakeGCmd(values))
    assert feeder._jam_active
    assert feeder._pending_remaining_mm == 0


@pytest.mark.parametrize('fault', ['grace', 'busy'])
def test_legacy_unload_entry_error_precedes_invalid_tip_cycles(fault):
    _, feeder = make_feeder()
    if fault == 'grace':
        feeder._startup_grace_done = False
    else:
        feeder._state = buffer_feeder.STATE_INITIAL_GRIP
    with pytest.raises(RuntimeError):
        feeder.cmd_BUFFER_UNLOAD_FILAMENT(FakeGCmd({'TIP_CYCLES': -1}))
    assert feeder._stepper_synced_to is None
    assert not feeder._macro_state_saved


def test_sensor_unload_dispatch_precedes_legacy_parsing(monkeypatch):
    _, feeder = make_feeder()
    feeder._load_endstop_sensor = object()
    gcmd = FakeGCmd({'TIP_CYCLES': -1})
    seen = []
    result = object()

    def unload(command):
        seen.append(command)
        return result

    monkeypatch.setattr(feeder.toolhead_sensor, 'unload', unload)
    assert feeder.cmd_BUFFER_UNLOAD_FILAMENT(gcmd) is result
    assert seen == [gcmd]


def test_phase1_sensor_callback_reads_live_defaults(monkeypatch):
    _, feeder = make_feeder()
    feeder._load_endstop_sensor = object()
    feeder.load_fast_distance = 123.0
    feeder.load_fast_speed = 17.0
    monkeypatch.setattr(feeder, '_load_endstop_present', lambda: False)
    seen = []
    monkeypatch.setattr(feeder, '_load_to_endstop',
                        lambda *args: seen.append(args))
    gcmd = FakeGCmd()
    feeder.cmd_BUFFER_LOAD_PHASE1(gcmd)
    assert seen == [(gcmd, 123.0, 17.0)]


def test_phase3_command_uses_owner_tick_and_stable_exit(monkeypatch):
    _, feeder = make_feeder()
    set_sensor_active(feeder, 'hall_full', True)
    set_sensor_active(feeder, 'entrance', True)
    original = feeder._exit_phase3_stable
    exits = []

    def exit_stable(**kwargs):
        exits.append(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(feeder, '_exit_phase3_stable', exit_stable)
    monkeypatch.setattr(feeder, '_start_continuous_motion',
                        lambda *args: feeder._load_phase3_tick(10.0))
    feeder.cmd_BUFFER_LOAD_PHASE3(FakeGCmd())
    assert exits == [{'set_grace': False,
                      'respond_text': 'LOAD Phase 3: HALL2 reached, buffer full'}]
    assert feeder._state == buffer_feeder.STATE_AUTO
    assert not feeder._post_load_overflow_grace
