"""Exercise the real feeder, sensor monitor and sync coordinator together."""
from types import SimpleNamespace

import pytest

from helpers import FakeGCmd, set_sensor_active
from test_load_endstop import setup_load


def make_workflow(monkeypatch, trigger=30.):
    feeder, sensor, progress, moves = setup_load(monkeypatch, trigger, {
        'load_slow_distance': 4., 'load_sensor_to_extruder': 10.,
    })
    sensor.runout_helper = SimpleNamespace(sensor_enabled=False,
                                           min_event_systime=0.)
    sensor.get_status = lambda now: {
        'enabled': sensor.runout_helper.sensor_enabled,
        'filament_detected': sensor.detected,
    }
    extruder = feeder.printer.lookup_object('extruder')
    extruder.get_name = lambda: 'extruder'
    feeder.printer.lookup_object('toolhead').get_extruder = lambda: extruder
    extruder.heater.min_extrude_temp = 170.
    extruder.get_status = lambda now: {'temperature': 220.}
    scripts = []
    original = feeder._gcode_run_script_checked

    def execute(script, **kwargs):
        scripts.append((script, feeder._stepper_synced_to))
        original(script, **kwargs)

    monkeypatch.setattr(feeder, '_gcode_run_script_checked', execute)
    return feeder, sensor, progress, moves, scripts


def test_load_real_fast_offset_and_sync_order(monkeypatch):
    feeder, sensor, progress, moves, scripts = make_workflow(monkeypatch)
    edges = []
    pause = feeder.reactor.pause

    def track_edge(when):
        before = sensor.detected
        result = pause(when)
        if not before and sensor.detected:
            edges.append(progress())
        return result

    monkeypatch.setattr(feeder.reactor, 'pause', track_edge)
    feeder.cmd_BUFFER_LOAD_FILAMENT(FakeGCmd())
    assert len(edges) == 1
    assert progress() - edges[0] == pytest.approx(10., abs=0.01)
    sync_moves = [(script, sync) for script, sync in scripts
                  if script.startswith('G1 ')]
    assert len(sync_moves) == 4
    assert all(sync == 'extruder' for _, sync in sync_moves)
    assert feeder._stepper_synced_to is None
    assert not sensor.runout_helper.sensor_enabled
    assert not feeder._macro_state_saved
    assert feeder._pending_remaining_mm == 0


def test_load_hall1_error_restores_sensor_without_sync(monkeypatch):
    feeder, sensor, _, moves, scripts = make_workflow(monkeypatch)
    pause = feeder.reactor.pause

    def block(when):
        result = pause(when)
        set_sensor_active(feeder, 'hall_overflow', True)
        return result

    monkeypatch.setattr(feeder.reactor, 'pause', block)
    with pytest.raises(RuntimeError, match='HALL1|OVERFLOW'):
        feeder.cmd_BUFFER_LOAD_FILAMENT(FakeGCmd())
    assert feeder._stepper_synced_to is None
    assert feeder._pending_remaining_mm == 0
    assert not sensor.runout_helper.sensor_enabled
    assert not any(script.startswith('G1 ') for script, _ in scripts)
    assert feeder.reactor.now >= 30.


@pytest.mark.parametrize('durations', [(0.2,), (20., 20.)])
def test_load_resumes_after_each_short_hall1_pause(monkeypatch, durations):
    feeder, sensor, progress, moves, _ = make_workflow(monkeypatch, trigger=90.)
    feeder.max_feed_time = 20.
    pause = feeder.reactor.pause
    starts = []
    active_since = None
    move_count = None

    def overflow(when):
        nonlocal active_since, move_count
        result = pause(when)
        if active_since is not None:
            assert len(moves) == move_count, 'new moves submitted during HALL1'
            if when - active_since >= durations[len(starts) - 1]:
                set_sensor_active(feeder, 'hall_overflow', False)
                feeder._exit_overflow()
                active_since = None
        elif len(starts) < len(durations) and progress() > 10. + len(starts) * 20.:
            active_since = when
            starts.append(when)
            set_sensor_active(feeder, 'hall_overflow', True)
            feeder._enter_overflow()
            move_count = len(moves)
        return result

    monkeypatch.setattr(feeder.reactor, 'pause', overflow)
    feeder.cmd_BUFFER_LOAD_FILAMENT(FakeGCmd())
    assert len(starts) == len(durations)
    assert sensor.detected
    assert 99. <= progress() <= 106.
    assert all(move[12] * (move[2] + move[3]) <= 3. + 1.e-7 for move in moves)
    assert feeder._overflow_resume_mm == 0.
    assert feeder._load_endstop_move is None


def test_unload_real_sync_is_detached_before_buffer_only_phase(monkeypatch):
    feeder, sensor, _, _, scripts = make_workflow(monkeypatch)
    sensor.detected = True
    execute = feeder._gcode_run_script_checked
    count = []

    def retract(script, **kwargs):
        execute(script, **kwargs)
        if script.startswith('G1 E-'):
            count.append(script)
            if len(count) == 2:
                sensor.detected = False

    monkeypatch.setattr(feeder, '_gcode_run_script_checked', retract)
    feeder.cmd_BUFFER_UNLOAD_FILAMENT(FakeGCmd({'TIP_CYCLES': 0,
                                              'USE_COOLING_MOVE': 0}))
    assert len(count) == 2
    assert all(sync == 'extruder' for script, sync in scripts
               if script.startswith('G1 E-'))
    assert any(script.startswith('BUFFER_UNLOAD_PHASE3 ') and sync is None
               for script, sync in scripts)
    assert feeder._stepper_synced_to is None
    assert not sensor.runout_helper.sensor_enabled


def test_extruder_move_error_cleans_real_sync(monkeypatch):
    feeder, sensor, _, _, scripts = make_workflow(monkeypatch)
    sensor.detected = True
    execute = feeder._gcode_run_script_checked

    def fail_move(script, **kwargs):
        if script.startswith('G1 '):
            raise RuntimeError('extruder move failed')
        execute(script, **kwargs)

    monkeypatch.setattr(feeder, '_gcode_run_script_checked', fail_move)
    with pytest.raises(RuntimeError, match='extruder move failed'):
        feeder.cmd_BUFFER_LOAD_FILAMENT(FakeGCmd())
    assert feeder._stepper_synced_to is None
    assert not sensor.runout_helper.sensor_enabled
    assert not feeder._macro_state_saved
