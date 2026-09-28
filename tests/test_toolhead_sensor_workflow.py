from types import SimpleNamespace

import pytest

from helpers import FakeGCmd
from klipper_extras.buffer_toolhead import ToolheadSensorWorkflow


def setup_workflow(present=False, clear_after=2):
    calls = []
    helper = SimpleNamespace(sensor_enabled=False, min_event_systime=17.)
    sensor = SimpleNamespace(runout_helper=helper, present=present)
    sensor.get_status = lambda now: dict(enabled=helper.sensor_enabled,
                                        filament_detected=sensor.present)
    reactor = SimpleNamespace(now=0., NEVER=1.e30)
    reactor.monotonic = lambda: reactor.now
    reactor.pause = lambda when: setattr(reactor, 'now', when)
    heater = SimpleNamespace(min_extrude_temp=170.)
    extruder = SimpleNamespace(get_heater=lambda: heater,
                              get_status=lambda now: {'temperature': 220.},
                              get_name=lambda: 'extruder')
    objects = {'extruder': extruder, 'extruder1': extruder,
               'toolhead': SimpleNamespace(get_extruder=lambda: extruder)}
    owner = SimpleNamespace(
        reactor=reactor, _load_endstop_sensor=sensor,
        load_endstop_sensor='filament_switch_sensor toolhead',
        printer=SimpleNamespace(lookup_object=lambda name: objects[name]),
        _startup_grace_done=True, _state='IDLE', _halt_requested=False,
        _pending_remaining_mm=0., _macro_state_saved=False,
        load_sensor_to_extruder=10., load_fast_distance=1000.,
        load_fast_speed=100., load_slow_distance=5., load_slow_speed=5.,
        unload_sync_distance=20., unload_fast_speed=50.,
        unload_fast_max=1000., unload_phase3_speed=50., min_temp=180.,
        interrupt_chunk_mm=3., max_move_chunk_mm=50., max_feed_time=60.,
        _stepper_synced_to=None, name='mellow', hall_full=False,
        _cmd_error=RuntimeError, _respond=lambda message: calls.append(message),
        _check_phase_entry=lambda *args: None,
        _raise_if_locked_out=lambda *args, **kwargs: None,
        _move_in_flight=lambda: False, _enable_stepper=lambda: None)
    owner._set_state = lambda value: setattr(owner, '_state', value)
    owner._halt_motion = lambda: calls.append('halt')
    def fast(*args, **kwargs):
        calls.append(('fast', kwargs))
        sensor.present = True
        return 0.
    owner._load_to_endstop = fast
    owner._submit_move = lambda *args, **kwargs: calls.append(('offset', args, kwargs))
    owner._unsync_if_synced = lambda: calls.append('unsync')
    owner.sync = SimpleNamespace(sync_to_extruder=lambda name: calls.append('sync'))
    def script(value, **kwargs):
        calls.append(value)
        if value.startswith('G1 '):
            reactor.now += .1
        if value.startswith('G1 E-'):
            script.retracted += float(value.split()[1][1:]) * -1
            if clear_after is not None and script.retracted >= clear_after:
                sensor.present = False
    script.retracted = 0.
    owner._gcode_run_script_checked = script
    return ToolheadSensorWorkflow(owner), owner, sensor, calls


def test_load_sensor_offset_before_sync_and_restore_disabled_sensor():
    flow, owner, sensor, calls = setup_workflow()
    flow.load(FakeGCmd())
    offset = next(c for c in calls if isinstance(c, tuple) and c[0] == 'offset')
    assert offset[1] == (10., 5.)
    assert calls.index(offset) < calls.index('sync')
    assert next(c for c in calls if isinstance(c, tuple) and c[0] == 'fast')[1]['minimum_ratio'] == 0.
    assert not sensor.runout_helper.sensor_enabled
    assert sensor.runout_helper.min_event_systime == 17.


def test_already_present_skips_fast_and_offset():
    flow, owner, sensor, calls = setup_workflow(present=True)
    flow.load(FakeGCmd())
    assert not any(isinstance(c, tuple) for c in calls)
    assert 'sync' in calls


def test_unload_checks_sensor_during_tip_retract():
    flow, owner, sensor, calls = setup_workflow(present=True)
    flow.unload(FakeGCmd())
    retracts = [c for c in calls if isinstance(c, str) and c.startswith('G1 E-')]
    assert len(retracts) == 2
    assert all('E-1 ' in c and c.endswith('M400') for c in retracts)
    assert calls.index('unsync') < next(i for i,c in enumerate(calls) if c.startswith('BUFFER_UNLOAD_PHASE3'))
    assert not sensor.runout_helper.sensor_enabled


def test_unload_already_clear_only_buffer():
    flow, owner, sensor, calls = setup_workflow()
    flow.unload(FakeGCmd())
    assert 'sync' not in calls
    assert not any(c.startswith('G1 ') for c in calls)


def test_stuck_sensor_bounded_and_cleanup():
    flow, owner, sensor, calls = setup_workflow(present=True, clear_after=None)
    with pytest.raises(RuntimeError, match='SYNC_DIST'):
        flow.unload(FakeGCmd({'SYNC_DIST': 3, 'TIP_CYCLES': 0, 'USE_COOLING_MOVE': 0}))
    assert 'unsync' in calls
    assert not sensor.runout_helper.sensor_enabled
    assert any(c.startswith('RESTORE_GCODE_STATE') for c in calls)
    assert not any(c.startswith('BUFFER_UNLOAD_PHASE3') for c in calls)


def test_missing_sensor_is_descriptive():
    flow, owner, sensor, calls = setup_workflow()
    owner._load_endstop_sensor = None
    with pytest.raises(RuntimeError, match='load_endstop_sensor'):
        flow.load(FakeGCmd())
    assert not calls


@pytest.mark.parametrize('state', ['JAM', 'OVERFLOW'])
def test_recovery_unload_never_pushes(state):
    flow, owner, sensor, calls = setup_workflow(present=True)
    owner._state = state
    flow.unload(FakeGCmd({'USE_COOLING_MOVE': 0}))
    assert not any(c.startswith('G1 E1 ') for c in calls)


def test_nominal_fast_overrun_counts_towards_offset():
    flow, owner, sensor, calls = setup_workflow()
    def fast(*args, **kwargs):
        sensor.present = True
        return 2.5
    owner._load_to_endstop = fast
    flow.load(FakeGCmd())
    offset = next(c for c in calls if isinstance(c, tuple))
    assert offset[1] == (7.5, 5.)


def test_sensor_events_suppressed_but_raw_status_enabled():
    flow, owner, sensor, calls = setup_workflow(present=True)
    original = owner._gcode_run_script_checked
    def script(value, **kwargs):
        assert sensor.runout_helper.sensor_enabled
        assert sensor.runout_helper.min_event_systime == owner.reactor.NEVER
        original(value, **kwargs)
    owner._gcode_run_script_checked = script
    flow.unload(FakeGCmd({'USE_COOLING_MOVE': 0}))


def test_partial_sync_failure_restores_everything():
    flow, owner, sensor, calls = setup_workflow(present=True)
    def sync(name):
        raise RuntimeError('sync failed')
    owner.sync.sync_to_extruder = sync
    with pytest.raises(RuntimeError, match='sync failed'):
        flow.load(FakeGCmd())
    assert 'unsync' in calls
    assert not flow.active
    assert not sensor.runout_helper.sensor_enabled
    assert sensor.runout_helper.min_event_systime == 17.
    assert any(c.startswith('RESTORE_GCODE_STATE') for c in calls)


def test_offset_timeout_aborts_without_sync():
    flow, owner, sensor, calls = setup_workflow()
    original = owner._submit_move
    def submit(*args, **kwargs):
        original(*args, **kwargs)
        owner._move_in_flight = lambda: True
    owner._submit_move = submit
    owner.max_feed_time = .02
    with pytest.raises(RuntimeError, match='max_feed_time'):
        flow.load(FakeGCmd())
    assert 'sync' not in calls
    assert not sensor.runout_helper.sensor_enabled


def test_reentrant_workflow_rejected_and_sensor_restored():
    flow, owner, sensor, calls = setup_workflow(present=True)
    owner.sync.sync_to_extruder = lambda name: flow.unload(FakeGCmd())
    with pytest.raises(RuntimeError, match='bereits'):
        flow.load(FakeGCmd())
    assert not flow.active
    assert not sensor.runout_helper.sensor_enabled


def test_unload_motion_timeout_counts_tip_moves():
    flow, owner, sensor, calls = setup_workflow(present=True, clear_after=None)
    owner.max_feed_time = .15
    with pytest.raises(RuntimeError, match='max_feed_time'):
        flow.unload(FakeGCmd())
    assert len([c for c in calls if c.startswith('G1 ')]) == 2
    assert not sensor.runout_helper.sensor_enabled


def test_sync_distance_counts_tip_pull_distance():
    flow, owner, sensor, calls = setup_workflow(present=True, clear_after=None)
    with pytest.raises(RuntimeError, match='SYNC_DIST=3'):
        flow.unload(FakeGCmd({'SYNC_DIST': 3}))
    assert len([c for c in calls if c.startswith('G1 E-')]) == 3


def test_incompatible_sensor_fails_before_any_movement():
    flow, owner, sensor, calls = setup_workflow()
    del sensor.runout_helper
    with pytest.raises(RuntimeError, match='filament_switch_sensor'):
        flow.load(FakeGCmd())
    assert not calls


def test_sensor_restored_even_when_gcode_restore_fails():
    flow, owner, sensor, calls = setup_workflow()
    original = owner._gcode_run_script_checked
    def script(value, **kwargs):
        if value.startswith('RESTORE_GCODE_STATE'):
            raise RuntimeError('restore failed')
        original(value, **kwargs)
    owner._gcode_run_script_checked = script
    with pytest.raises(RuntimeError, match='restore failed'):
        flow.unload(FakeGCmd())
    assert not flow.active
    assert not sensor.runout_helper.sensor_enabled
    assert sensor.runout_helper.min_event_systime == 17.


def test_inactive_extruder_rejected_before_heat_or_motion():
    flow, owner, sensor, calls = setup_workflow()
    with pytest.raises(RuntimeError, match='aktiven Extruder'):
        flow.load(FakeGCmd({'EXTRUDER': 'extruder1'}))
    assert 'sync' not in calls
    assert not any(isinstance(c, tuple) for c in calls)
    assert not any(c.startswith('SET_HEATER_TEMPERATURE') for c in calls)
    assert not sensor.runout_helper.sensor_enabled
