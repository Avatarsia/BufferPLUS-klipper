"""Multi-buffer consumers must not observe or move another extruder."""
import configparser
from pathlib import Path

import pytest

from fakes_klipper import FakeExtruder, FakePrinter
from helpers import FakeGCmd, build_feeder, set_sensor_active
from test_operation_cleanup import MacroRunner
from test_toolhead_sensor_workflow import setup_workflow


def make_pair():
    printer = FakePrinter()
    first_ext = printer.lookup_object('extruder')
    second_ext = FakeExtruder()
    first_ext.get_name = lambda: 'extruder'
    second_ext.get_name = lambda: 'extruder1'
    printer.objects['extruder1'] = second_ext
    printer.lookup_object('toolhead').get_extruder = lambda: second_ext
    _, first = build_feeder(printer=printer, state='AUTO')
    _, second = build_feeder({'extruder': 'extruder1'}, printer=printer,
                             name='buffer_feeder second', state='AUTO')
    for feeder in (first, second):
        set_sensor_active(feeder, 'hall_empty', True)
        set_sensor_active(feeder, 'hall_full', False)
        set_sensor_active(feeder, 'hall_overflow', False)
        set_sensor_active(feeder, 'entrance', True)
        feeder._print_running = True
    return printer, first, second, first_ext, second_ext


@pytest.mark.parametrize('moving', ['extruder', 'extruder1'])
def test_auto_demand_follows_only_assigned_extruder(moving):
    printer, first, second, first_ext, second_ext = make_pair()
    for index in range(13):
        now = index * .025
        printer.reactor.now = now
        first_ext.last_position = now * 20 if moving == 'extruder' else 0.
        second_ext.last_position = now * 20 if moving == 'extruder1' else 0.
        for feeder in (first, second):
            feeder._tick_velocity_observation(now)
    targets = [feeder._compute_target_feed_speed() for feeder in (first, second)]
    if moving == 'extruder':
        assert targets[0] > 0 and targets[1] == 0
    else:
        assert targets[0] == 0 and targets[1] > 0


def test_clog_detection_uses_assigned_extruder_progress():
    _, first, second, first_ext, second_ext = make_pair()
    for feeder in (first, second):
        set_sensor_active(feeder, 'hall_full', True)
        set_sensor_active(feeder, 'hall_empty', False)
        feeder._hall2_start_time = 0.
        feeder._hall2_start_extruder_pos = 0.
    first_ext.last_position = 40.
    first._jam_tick(100.)
    second._jam_tick(100.)
    assert first._jam_active and not second._jam_active
    second_ext.last_position = 40.
    second._jam_tick(101.)
    assert second._jam_active


def test_temperature_and_auto_load_target_assigned_buffer():
    printer, _, second, first_ext, second_ext = make_pair()
    first_ext.heater.temperature = 20.
    second_ext.heater.temperature = 220.
    second.auto_load_after_follow = True
    second._maybe_auto_load()
    assert second._hotend_temp() == 220.
    printer.reactor.fire_pending_timers(.1)
    scripts = [script for _, script in printer.lookup_object('gcode').scripts]
    assert 'LOAD_FILAMENT BUFFER=second' in scripts


def test_runout_follow_ignores_other_extruder_and_counts_own_progress():
    printer, _, second, first_ext, second_ext = make_pair()
    second._tick_velocity_observation(0.)
    second._on_entrance_runout(0.)
    assert second._runout_follow_active
    first_ext.last_position = 200.
    printer.lookup_object('print_stats').filament_used = 200.
    second._tick_velocity_observation(.1)
    second._tick_runout_follow(.1)
    assert second._runout_follow_active
    second_ext.last_position = 100.
    second._tick_velocity_observation(.2)
    second._tick_runout_follow(.2)
    assert not second._runout_follow_active
    assert second._state == 'IDLE'


def test_sync_defaults_to_assigned_extruder_and_cleans_up():
    _, first, second, _, second_ext = make_pair()
    second.cmd_BUFFER_SYNC_TO_EXTRUDER(FakeGCmd())
    assert second.stepper.trapq is second_ext.trapq
    assert first.stepper.trapq is first.sync.trapq
    second._unsync_if_synced()
    assert second.stepper.trapq is second.sync.trapq


def test_final_load_defaults_to_assigned_active_extruder():
    printer, first, second, _, second_ext = make_pair()
    runner = MacroRunner(printer)
    runner.run('BUFFER_LOAD_SYNCED BUFFER=second DISTANCE=2 SPEED=4')
    assert runner.moves == ['G1 E2 F240']
    assert second.stepper.trapq_sets[-2:] == [second_ext.trapq, second.sync.trapq]
    assert first.stepper.trapq_sets == [first.sync.trapq]
    assert not runner.relative


@pytest.mark.parametrize('active', [True, False])
def test_legacy_unload_uses_assigned_heater_and_checks_active_first(active):
    printer, _, second, first_ext, second_ext = make_pair()
    if not active:
        printer.lookup_object('toolhead').get_extruder = lambda: first_ext
    first_ext.heater.temperature = 250.
    second_ext.heater.temperature = 20.
    gcode = printer.lookup_object('gcode')
    if not active:
        with pytest.raises(RuntimeError, match='active extruder'):
            second.cmd_BUFFER_UNLOAD_FILAMENT(FakeGCmd())
        assert not gcode.scripts
        return
    second.cmd_BUFFER_UNLOAD_FILAMENT(FakeGCmd())
    scripts = [script for _, script in gcode.scripts]
    assert any('SET_HEATER_TEMPERATURE HEATER=extruder1 TARGET=250' in s for s in scripts)
    assert any('TEMPERATURE_WAIT SENSOR=extruder1 MINIMUM=245' in s for s in scripts)
    assert any('SET_HEATER_TEMPERATURE HEATER=extruder1 TARGET=170' in s for s in scripts)
    assert 'BUFFER_SYNC_TO_EXTRUDER BUFFER=second EXTRUDER=extruder1' in scripts


@pytest.mark.parametrize('command', ['load', 'unload'])
def test_sensor_workflow_defaults_to_assigned_extruder(command):
    flow, owner, _, calls = setup_workflow(present=True)
    owner.extruder_name = 'extruder1'
    ext = type('Extruder', (), {
        'get_name': lambda self: 'extruder1',
        'get_heater': lambda self: type('Heater', (), {'min_extrude_temp': 170.})(),
        'get_status': lambda self, now: {'temperature': 220.},
    })()
    owner.printer.lookup_object = lambda name: (
        type('Toolhead', (), {'get_extruder': lambda self: ext})()
        if name == 'toolhead' else ext)
    synced = []
    owner.sync.sync_to_extruder = synced.append
    getattr(flow, command)(FakeGCmd())
    assert synced == ['extruder1']


def render_macro(name, printer, params=None):
    jinja2 = pytest.importorskip('jinja2')
    config = configparser.RawConfigParser()
    config.read(Path(__file__).resolve().parents[1] / 'lll.cfg', encoding='utf-8')
    env = jinja2.Environment(variable_start_string='{', variable_end_string='}',
                             undefined=jinja2.StrictUndefined)
    def fail(message):
        raise RuntimeError(message)
    return env.from_string(config['gcode_macro ' + name]['gcode']).render(
        printer=printer, params=params or {},
        rawparams=' '.join('%s=%s' % item for item in (params or {}).items()),
        action_raise_error=fail)


@pytest.mark.parametrize('sensor', ['', 'filament_switch_sensor toolhead1'])
@pytest.mark.parametrize('override', [False, True])
def test_load_macro_routes_selected_buffer_and_heater(sensor, override):
    printer, _, second, _, _ = make_pair()
    second.load_endstop_sensor = sensor
    target = 'extruder' if override else 'extruder1'
    context = {
        'buffer_feeder second': second.get_status(0.),
        target: {'temperature': 20.},
        'toolhead': {'extruder': target},
        'gcode_macro LOAD_FILAMENT': {'auto_heat_target': 250., 'temp_window': 5.},
        'configfile': {'settings': {target: {'min_extrude_temp': 170.}}},
    }
    params = {'BUFFER': 'second'}
    if override:
        params['EXTRUDER'] = target
    rendered = render_macro('LOAD_FILAMENT', context, params)
    commands = [line.strip() for line in rendered.splitlines()
                if line.strip() and not line.strip().startswith('#')]
    assert any('BUFFER=second' in c for c in commands)
    assert not any('BUFFER=mellow' in c for c in commands)
    if sensor:
        assert commands[0].startswith('BUFFER_LOAD_FILAMENT BUFFER=second ')
        if override:
            assert 'EXTRUDER=extruder' in commands[0]
    else:
        assert 'SET_HEATER_TEMPERATURE HEATER=%s TARGET=250.0' % target in commands
        assert 'TEMPERATURE_WAIT SENSOR=%s MINIMUM=245.0' % target in commands
        assert any(c.startswith('BUFFER_LOAD_SYNCED BUFFER=second EXTRUDER=%s ' % target)
                   for c in commands)
        assert '_SAVE_E_MODE BUFFER=second' in commands
        assert '_RESTORE_E_MODE BUFFER=second' in commands
    assert second.get_status(0.)['extruder'] == 'extruder1'
    assert second.velocity_tracker.extruder_name == 'extruder1'


def test_state_recovery_slots_do_not_overwrite_other_buffer():
    printer, first, second, _, _ = make_pair()
    runner = MacroRunner(printer)
    first.cleanup.save_gcode_state(from_command=True)
    runner.relative = True
    second.cleanup.save_gcode_state(from_command=True)
    first.cleanup.restore_gcode_state(from_command=True)
    assert not runner.relative
    second.cleanup.restore_gcode_state(from_command=True)
    assert runner.relative


def test_unknown_extruder_rejected_on_connect():
    printer, _ = build_feeder({'extruder': 'extruder9'})
    with pytest.raises(RuntimeError, match='extruder9'):
        printer.fire_event('klippy:connect')


def test_non_extruder_object_rejected_on_connect():
    printer, _ = build_feeder({'extruder': 'toolhead'})
    with pytest.raises(RuntimeError, match='not an extruder'):
        printer.fire_event('klippy:connect')


def test_runout_retract_recovery_does_not_consume_follow_distance():
    _, _, second, _, second_ext = make_pair()
    second_ext.last_position = 10.
    second._tick_velocity_observation(0.)
    second._on_entrance_runout(0.)
    for now, position in ((.1, 0.), (.2, 10.), (.3, 100.)):
        second_ext.last_position = position
        second._tick_velocity_observation(now)
        second._tick_runout_follow(now)
        assert second._runout_follow_active
    second_ext.last_position = 110.
    second._tick_velocity_observation(.4)
    second._tick_runout_follow(.4)
    assert not second._runout_follow_active


@pytest.mark.parametrize('name', ['UNLOAD_FILAMENT', 'CLEAR_JAM',
                                  '_SAVE_E_MODE', '_RESTORE_E_MODE'])
def test_wrapper_macros_select_second_buffer(name):
    commands = render_macro(name, {}, {'BUFFER': 'second'})
    assert 'BUFFER=second' in commands
    assert 'BUFFER=mellow' not in commands


def test_load_macro_rejects_inactive_extruder_before_commands():
    _, _, second, _, _ = make_pair()
    context = {'buffer_feeder second': second.get_status(0.),
               'toolhead': {'extruder': 'extruder'}}
    with pytest.raises(RuntimeError, match='zuerst aktivieren'):
        render_macro('LOAD_FILAMENT', context, {'BUFFER': 'second'})
