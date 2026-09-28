"""Sensor-controlled fast load, with bounded simulated reactor time."""
import configparser
from pathlib import Path

import pytest

from fakes_klipper import FakeConfig, FakePrinter
from helpers import FakeGCmd, set_sensor_active
from klipper_extras import buffer_feeder


SENSOR = 'filament_switch_sensor toolhead'


class Sensor:
    enabled = True
    detected = False

    def get_status(self, eventtime):
        return {'enabled': self.enabled, 'filament_detected': self.detected}


def setup_load(monkeypatch, trigger=100., values=None):
    printer = FakePrinter()
    sensor = Sensor()
    printer.objects[SENSOR] = sensor
    config = {'load_endstop_sensor': SENSOR, 'load_fast_distance': 100.,
              'interrupt_chunk_mm': 3., 'idle_anchor_mode': 'silent'}
    config.update(values or {})
    feeder = buffer_feeder.BufferFeeder(FakeConfig(printer, config))
    printer.fire_event('klippy:connect')
    feeder._startup_grace_done = True
    feeder._state = buffer_feeder.STATE_IDLE
    for name in ('hall_empty', 'hall_full', 'hall_overflow'):
        set_sensor_active(feeder, name, False)
    feeder._stepcompress_primed = True
    reactor = printer.reactor
    monkeypatch.setattr(reactor, 'monotonic', lambda: reactor.now)
    moves = printer.objects['motion_queuing'].append_calls

    def progress():
        total = 0.
        for move in moves:
            _, start, ta, tc, td, _, _, _, direction, _, _, _, speed, accel = move
            elapsed = max(0., min(reactor.now - start, ta + tc + td))
            a = min(elapsed, ta)
            c = min(max(0., elapsed - ta), tc)
            d = min(max(0., elapsed - ta - tc), td)
            total += direction * (.5 * accel * a*a + speed*c
                                  + speed*d - .5 * accel*d*d)
        return total

    def pause(when):
        assert when < 65., 'load loop did not terminate'
        reactor.now = when
        if trigger is not None and progress() >= trigger - 1.e-8:
            sensor.detected = True
        feeder._tick_pending_chunk(when)
        return when

    monkeypatch.setattr(reactor, 'pause', pause)
    return feeder, sensor, progress, moves


@pytest.mark.parametrize('trigger', [90., 100., 109., 110.])
def test_trigger_in_tolerance_stops_load(monkeypatch, trigger):
    feeder, _, progress, moves = setup_load(monkeypatch, trigger)
    feeder.cmd_BUFFER_LOAD_PHASE1(FakeGCmd())
    assert trigger - 1.e-7 <= progress() <= min(110., trigger + 6.) + 1.e-7
    assert moves
    assert all(move[12] * (move[2] + move[3]) <= 3. + 1.e-7 for move in moves)
    assert feeder._pending_remaining_mm == 0
    assert feeder._state == buffer_feeder.STATE_IDLE


@pytest.mark.parametrize('trigger', [50., 88.])
def test_early_trigger_aborts(monkeypatch, trigger):
    feeder, _, progress, _ = setup_load(monkeypatch, trigger)
    with pytest.raises(RuntimeError, match='too early'):
        feeder.cmd_BUFFER_LOAD_PHASE1(FakeGCmd())
    assert progress() < trigger + 6.
    assert feeder._pending_remaining_mm == 0
    assert feeder._state == buffer_feeder.STATE_IDLE


def test_missing_trigger_stops_at_upper_limit(monkeypatch):
    feeder, _, progress, _ = setup_load(monkeypatch, None)
    with pytest.raises(RuntimeError, match='not reached'):
        feeder.cmd_BUFFER_LOAD_PHASE1(FakeGCmd())
    assert progress() == pytest.approx(110.)
    assert feeder._pending_remaining_mm == 0


def test_initially_present_skips_motion(monkeypatch):
    feeder, sensor, _, moves = setup_load(monkeypatch)
    sensor.detected = True
    feeder.cmd_BUFFER_LOAD_PHASE1(FakeGCmd())
    assert not moves
    assert feeder._state == buffer_feeder.STATE_IDLE


def test_initially_present_skips_motion_even_in_overflow(monkeypatch):
    feeder, sensor, _, moves = setup_load(monkeypatch)
    sensor.detected = True
    set_sensor_active(feeder, 'hall_overflow', True)
    feeder._state = buffer_feeder.STATE_OVERFLOW
    feeder._overflow_interrupted_state = buffer_feeder.STATE_LOADING_PULL
    feeder._overflow_resume_mm = 20.
    feeder.cmd_BUFFER_LOAD_PHASE1(FakeGCmd())
    assert not moves
    assert feeder._state == buffer_feeder.STATE_OVERFLOW
    assert feeder._overflow_resume_mm == 0.


def test_disabled_sensor_rejected(monkeypatch):
    feeder, sensor, _, moves = setup_load(monkeypatch)
    sensor.enabled = False
    with pytest.raises(RuntimeError, match='disabled'):
        feeder.cmd_BUFFER_LOAD_PHASE1(FakeGCmd())
    assert not moves


def test_legacy_distance_unchanged(monkeypatch):
    feeder, _, progress, _ = setup_load(
        monkeypatch, 20., {'load_endstop_sensor': ''})
    feeder.cmd_BUFFER_LOAD_PHASE1(FakeGCmd())
    assert progress() == pytest.approx(100.)


def test_missing_sensor_fails_config():
    with pytest.raises(Exception, match='toolhead'):
        buffer_feeder.BufferFeeder(FakeConfig(values={'load_endstop_sensor': SENSOR}))


def test_motion_sensor_rejected():
    with pytest.raises(RuntimeError, match='filament_switch_sensor'):
        buffer_feeder.BufferFeeder(FakeConfig(values={
            'load_endstop_sensor': 'filament_motion_sensor toolhead'}))


@pytest.mark.parametrize('fault', ['halt', 'overflow', 'jam', 'disabled', 'read'])
def test_mid_load_fault_cannot_resume(monkeypatch, fault):
    feeder, sensor, progress, moves = setup_load(monkeypatch, None)
    pause = feeder.reactor.pause
    fired = False

    def fault_pause(when):
        nonlocal fired
        pause(when)
        if not fired and progress() > 10.:
            fired = True
            if fault == 'halt':
                feeder._halt_requested = True
                feeder._halt_motion()
            elif fault == 'overflow':
                set_sensor_active(feeder, 'hall_overflow', True)
                feeder._enter_overflow()
            elif fault == 'jam':
                feeder._jam_active = True
                feeder._state = buffer_feeder.STATE_JAM
                feeder._halt_motion()
            elif fault == 'disabled':
                sensor.enabled = False
            else:
                def read_error(eventtime):
                    raise RuntimeError('sensor disconnected')
                monkeypatch.setattr(sensor, 'get_status', read_error)
        return when

    monkeypatch.setattr(feeder.reactor, 'pause', fault_pause)
    with pytest.raises(RuntimeError):
        feeder.cmd_BUFFER_LOAD_PHASE1(FakeGCmd())
    assert fired
    assert feeder._load_endstop_move is None
    assert feeder._pending_remaining_mm == 0
    assert feeder._overflow_resume_mm == 0
    assert feeder._overflow_interrupted_state is None
    count = len(moves)
    if fault == 'overflow':
        set_sensor_active(feeder, 'hall_overflow', False)
        feeder._exit_overflow()
    feeder._tick_pending_chunk(feeder.reactor.now + 1.)
    assert len(moves) == count


def test_timeout_clears_pending(monkeypatch):
    feeder, _, progress, _ = setup_load(monkeypatch, None, {'max_feed_time': 1.})
    with pytest.raises(RuntimeError, match='timeout'):
        feeder.cmd_BUFFER_LOAD_PHASE1(FakeGCmd())
    assert progress() < 100.
    assert feeder._pending_remaining_mm == 0
    assert feeder._load_endstop_move is None


@pytest.mark.parametrize('speed', [5., 100.])
def test_distance_override_and_different_profiles(monkeypatch, speed):
    feeder, _, progress, _ = setup_load(monkeypatch, 190.)
    feeder.cmd_BUFFER_LOAD_PHASE1(FakeGCmd({'DISTANCE': 200., 'SPEED': speed}))
    assert 190. <= progress() + 1.e-7 < 196.


def test_sensor_trigger_survives_bounce(monkeypatch):
    feeder, sensor, _, _ = setup_load(monkeypatch, None)
    original_poll = sensor.get_status
    fired = False

    def single_poll(eventtime):
        nonlocal fired
        monitor = feeder._load_endstop_move
        if monitor is not None and not fired and monitor.distance_at(eventtime) >= 95.:
            fired = True
            return {'enabled': True, 'filament_detected': True}
        return original_poll(eventtime)

    monkeypatch.setattr(sensor, 'get_status', single_poll)
    feeder.cmd_BUFFER_LOAD_PHASE1(FakeGCmd())
    assert fired
    assert feeder._pending_remaining_mm == 0


def load_macro_commands(feeder):
    jinja2 = pytest.importorskip('jinja2')
    config = configparser.RawConfigParser()
    config.read(Path(__file__).resolve().parents[1] / 'lll.cfg', encoding='utf-8')
    source = config['gcode_macro LOAD_FILAMENT']['gcode']
    env = jinja2.Environment(variable_start_string='{', variable_end_string='}',
                             undefined=jinja2.StrictUndefined)
    printer = {'buffer_feeder mellow': feeder.get_status(feeder.reactor.now),
               'extruder': {'temperature': 250.},
               'gcode_macro LOAD_FILAMENT': {'auto_heat_target': 250., 'temp_window': 5.},
               'configfile': {'settings': {'extruder': {'min_extrude_temp': 170.}}}}
    return [line.strip() for line in env.from_string(source).render(printer=printer).splitlines()
            if line.strip() and not line.strip().startswith('#')]


@pytest.mark.parametrize('sensor_enabled,full,expect_phase1', [
    (True, True, True), (True, False, True),
    (False, True, False), (False, False, True),
])
def test_load_macro_uses_sensor_even_when_buffer_full(
        monkeypatch, sensor_enabled, full, expect_phase1):
    feeder, _, _, _ = setup_load(monkeypatch, values={
        'load_endstop_sensor': SENSOR if sensor_enabled else ''})
    set_sensor_active(feeder, 'hall_full', full)
    commands = load_macro_commands(feeder)
    if sensor_enabled:
        assert len(commands) == 1
        assert commands[0].startswith('BUFFER_LOAD_FILAMENT BUFFER=mellow ')
    else:
        assert ('BUFFER_LOAD_PHASE1 BUFFER=mellow' in commands) is expect_phase1


@pytest.mark.parametrize('trigger', [50., None])
def test_load_macro_error_prevents_next_phase(monkeypatch, trigger):
    feeder, _, _, _ = setup_load(monkeypatch, trigger)
    def reject_load(cmd):
        raise RuntimeError('sensor workflow aborted')
    monkeypatch.setattr(feeder.toolhead_sensor, 'load', reject_load)
    phase3 = []
    with pytest.raises(RuntimeError):
        for command in load_macro_commands(feeder):
            if command.startswith('BUFFER_LOAD_FILAMENT '):
                feeder.cmd_BUFFER_LOAD_FILAMENT(FakeGCmd())
            elif command.startswith('BUFFER_LOAD_PHASE3 '):
                phase3.append(command)
    assert not phase3


def test_load_macro_repeat_with_overflow_delegates_live_checks(monkeypatch):
    feeder, sensor, _, moves = setup_load(monkeypatch)
    sensor.detected = True
    set_sensor_active(feeder, 'hall_overflow', True)
    feeder._state = buffer_feeder.STATE_OVERFLOW
    commands = load_macro_commands(feeder)
    assert len(commands) == 1
    assert commands[0].startswith('BUFFER_LOAD_FILAMENT BUFFER=mellow ')
    assert not moves


def test_sensor_workflow_accepts_partial_path_and_reports_run_on(monkeypatch):
    feeder, _, progress, _ = setup_load(monkeypatch, trigger=50.)
    run_on = feeder._load_to_endstop(FakeGCmd(), 100., feeder.load_fast_speed,
                                    minimum_ratio=0.0)
    assert 0. <= run_on <= 6.
    assert 50. <= progress() <= 56.
