"""Consumer regressions for original errors, state acknowledgement and sync."""
import configparser
from pathlib import Path

import pytest

from helpers import FakeGCmd, build_feeder, set_sensor_active
from test_load_endstop import load_macro_commands
from test_toolhead_sensor_workflow import setup_workflow


def test_sensor_original_error_survives_cleanup_failures():
    flow, owner, sensor, calls = setup_workflow(present=True)
    original_error = RuntimeError('original G1')
    original = owner._gcode_run_script_checked

    def run(script, **kwargs):
        if script.startswith('G1 '):
            raise original_error
        if script.startswith('RESTORE_GCODE_STATE'):
            calls.append('restore attempted')
            raise RuntimeError('secondary restore')
        return original(script, **kwargs)

    def unsync():
        calls.append('unsync attempted')
        raise RuntimeError('secondary unsync')

    owner._gcode_run_script_checked = run
    owner._unsync_if_synced = unsync
    with pytest.raises(RuntimeError) as caught:
        flow.load(FakeGCmd())
    assert caught.value is original_error
    assert 'unsync attempted' in calls and 'restore attempted' in calls
    assert not flow.active
    assert not sensor.runout_helper.sensor_enabled
    assert sensor.runout_helper.min_event_systime == 17.
    assert owner._macro_state_saved


def test_best_effort_restore_failure_retains_retry_latch(monkeypatch):
    printer, feeder = build_feeder()
    gcode = printer.lookup_object('gcode')
    feeder._macro_state_saved = True

    def fail(script):
        raise RuntimeError('restore failed')

    with monkeypatch.context() as patch:
        patch.setattr(gcode, 'run_script_from_command', fail)
        assert not feeder.cleanup.try_restore_gcode_state(from_command=True)
        assert feeder._macro_state_saved
    assert feeder.cleanup.try_restore_gcode_state(from_command=True)
    assert not feeder._macro_state_saved
    assert not feeder.cleanup.try_restore_gcode_state(from_command=True)
    assert len(gcode.scripts) == 1


class MacroRunner:
    """Small external GCode model: real plugin mux dispatch plus E-mode slots."""
    def __init__(self, printer):
        self.gcode = printer.lookup_object('gcode')
        self.relative = False
        self.slots = {}
        self.moves = []
        self.commands = []
        self.fail_move = None
        config = configparser.RawConfigParser()
        config.read(Path(__file__).resolve().parents[1] / 'lll.cfg', encoding='utf-8')
        self.macros = {name: config['gcode_macro ' + name]['gcode']
                       for name in ('_SAVE_E_MODE', '_RESTORE_E_MODE')}
        self.gcode.run_script_from_command = self.run
        self.gcode.run_script = self.run

    def run(self, script):
        for line in script.splitlines():
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            self.commands.append(line)
            name, *words = line.split()
            params = dict(word.split('=', 1) for word in words if '=' in word)
            if name in self.macros:
                self.run(self.macros[name])
            elif name in self.gcode.mux_commands:
                matches = [entry for entry in self.gcode.mux_commands[name]
                           if entry['mux_value'] == params.get(entry['mux_key'])]
                assert len(matches) == 1, (name, params)
                matches[0]['handler'](FakeGCmd(params))
            elif name == 'SAVE_GCODE_STATE':
                self.slots[params['NAME']] = self.relative
            elif name == 'RESTORE_GCODE_STATE':
                self.relative = self.slots[params['NAME']]
            elif name == 'M83':
                self.relative = True
            elif name == 'G1':
                if self.fail_move is not None:
                    raise self.fail_move
                assert self.relative
                self.moves.append(line)
            else:
                assert name in ('M118', 'M400'), line


def legacy_load_setup(monkeypatch):
    printer, feeder = build_feeder(state='IDLE', fire_connect=True)
    extruder = printer.lookup_object('extruder')
    extruder.get_name = lambda: 'extruder'
    printer.lookup_object('toolhead').get_extruder = lambda: extruder
    set_sensor_active(feeder, 'hall_full', True)
    # Staging is already complete; this regression exercises the final push.
    for entry in printer.lookup_object('gcode').mux_commands['BUFFER_LOAD_PHASE3']:
        if entry['mux_value'] == feeder.name:
            monkeypatch.setitem(entry, 'handler', lambda gcmd: None)
    return printer, feeder, MacroRunner(printer)


def test_legacy_load_macro_error_unsyncs_and_restores_original_mode(monkeypatch):
    printer, feeder, runner = legacy_load_setup(monkeypatch)
    original_error = RuntimeError('extrusion rejected')
    runner.fail_move = original_error
    with pytest.raises(RuntimeError) as caught:
        runner.run('\n'.join(load_macro_commands(feeder)))
    assert caught.value is original_error
    assert feeder._stepper_synced_to is None
    assert feeder.stepper.trapq is feeder.sync.trapq
    assert not runner.relative
    assert not feeder._macro_state_saved
    assert not any(line.startswith('BUFFER_AUTO_ON') for line in runner.commands)


def test_legacy_load_macro_success_consumes_original_slot_once(monkeypatch):
    printer, feeder, runner = legacy_load_setup(monkeypatch)
    runner.run('\n'.join(load_macro_commands(feeder)))
    assert len(runner.moves) == 1
    assert feeder._stepper_synced_to is None
    assert feeder.stepper.trapq is feeder.sync.trapq
    assert not runner.relative
    assert not feeder._macro_state_saved
    assert len([c for c in runner.commands if c.startswith('SAVE_GCODE_STATE')]) == 1
    assert len([c for c in runner.commands if c.startswith('RESTORE_GCODE_STATE')]) == 1


def test_legacy_macro_save_failure_aborts_before_relative_mode(monkeypatch):
    printer, feeder, runner = legacy_load_setup(monkeypatch)
    original_error = RuntimeError('SAVE rejected')
    original = runner.run

    def run(script):
        if script.startswith('SAVE_GCODE_STATE'):
            raise original_error
        return original(script)

    monkeypatch.setattr(runner, 'run', run)
    monkeypatch.setattr(runner.gcode, 'run_script_from_command', run)
    with pytest.raises(RuntimeError) as caught:
        runner.run('\n'.join(load_macro_commands(feeder)))
    assert caught.value is original_error
    assert not runner.relative
    assert not runner.moves
    assert not feeder._macro_state_saved
    assert feeder._stepper_synced_to is None


@pytest.mark.parametrize('stage', ['SAVE', 'M83', 'SYNC', 'G1', 'M400', 'UNSYNC', 'RESTORE'])
@pytest.mark.parametrize('workflow', ['load', 'unload'])
def test_sensor_failure_stages_restore_sensor_and_preserve_error(stage, workflow):
    flow, owner, sensor, calls = setup_workflow(present=True)
    original_error = RuntimeError(stage + ' failed')
    events = []
    original = owner._gcode_run_script_checked

    def run(script, **kwargs):
        for line in script.splitlines():
            command = line.split()[0].split('_')[0]
            events.append(command)
            if command == stage:
                raise original_error
        return original(script, **kwargs)

    def sync(name):
        events.append('SYNC')
        owner._stepper_synced_to = name
        if stage == 'SYNC':
            raise original_error

    def unsync():
        events.append('UNSYNC')
        if stage == 'UNSYNC':
            raise original_error
        owner._stepper_synced_to = None

    owner._gcode_run_script_checked = run
    owner.sync.sync_to_extruder = sync
    owner._unsync_if_synced = unsync
    with pytest.raises(RuntimeError) as caught:
        getattr(flow, workflow)(FakeGCmd())
    assert caught.value is original_error
    assert not flow.active
    assert not sensor.runout_helper.sensor_enabled
    assert sensor.runout_helper.min_event_systime == 17.
    assert owner._macro_state_saved is (stage == 'RESTORE')
    if stage == 'SAVE':
        assert events == ['SAVE']
    else:
        assert 'RESTORE' in events
    if stage not in ('SAVE', 'M83'):
        assert 'UNSYNC' in events
    assert owner._stepper_synced_to == ('extruder' if stage == 'UNSYNC' else None)


def test_sensor_error_halt_failure_still_discards_resume():
    flow, owner, sensor, calls = setup_workflow(present=True)
    original_error = RuntimeError('original sync')
    halts = []

    def halt():
        halts.append(True)
        if len(halts) > 1:
            raise RuntimeError('secondary halt')

    def sync(name):
        owner._overflow_interrupted_state = 'LOADING_PULL'
        owner._overflow_resume_mm = 99.
        raise original_error

    owner._halt_motion = halt
    owner.sync.sync_to_extruder = sync
    with pytest.raises(RuntimeError) as caught:
        flow.load(FakeGCmd())
    assert caught.value is original_error
    assert owner._overflow_interrupted_state is None
    assert owner._overflow_resume_mm == 0.
    assert not owner._macro_state_saved
    assert not flow.active
    assert not sensor.runout_helper.sensor_enabled


@pytest.mark.parametrize('stage', ['SAVE', 'M83', 'SYNC', 'G1', 'M400', 'UNSYNC', 'RESTORE'])
def test_legacy_unload_failure_stages_keep_order_and_original(monkeypatch, stage):
    printer, feeder = build_feeder(state='IDLE', fire_connect=True)
    printer.lookup_object('extruder').heater.temperature = 220.
    runner = MacroRunner(printer)
    original_error = RuntimeError(stage + ' failed')
    events = []
    original = runner.run
    original_unsync = feeder._unsync_if_synced

    def run(script):
        for line in script.splitlines():
            command = line.split()[0]
            events.append(command)
            token = command.split('_')[0]
            if token == stage:
                raise original_error
        result = original(script)
        if stage == 'SYNC' and script.startswith('BUFFER_SYNC_TO_EXTRUDER'):
            raise original_error  # Mutation already happened; cleanup must detach.
        return result

    def unsync():
        events.append('UNSYNC')
        if stage == 'UNSYNC':
            raise original_error
        return original_unsync()

    monkeypatch.setattr(runner.gcode, 'run_script_from_command', run)
    monkeypatch.setattr(feeder, '_unsync_if_synced', unsync)
    with pytest.raises(RuntimeError) as caught:
        feeder.cmd_BUFFER_UNLOAD_FILAMENT(FakeGCmd({'USE_COOLING_MOVE': 0}))
    assert caught.value is original_error
    if stage == 'SAVE':
        assert events == ['SAVE_GCODE_STATE']
    else:
        assert events[-1] == 'RESTORE_GCODE_STATE'
    if stage not in ('SAVE', 'M83'):
        assert events.index('UNSYNC') < events.index('RESTORE_GCODE_STATE')
    assert 'BUFFER_UNLOAD_PHASE3' not in events or stage == 'RESTORE'
    assert feeder._macro_state_saved is (stage == 'RESTORE')
    if stage != 'UNSYNC':
        assert feeder._stepper_synced_to is None
        assert feeder.stepper.trapq is feeder.sync.trapq


def test_legacy_load_new_mux_routes_multiple_instances(monkeypatch):
    printer, first, runner = legacy_load_setup(monkeypatch)
    _, second = build_feeder(printer=printer, name='buffer_feeder second',
                             state='IDLE', fire_connect=True)
    runner.run('BUFFER_LOAD_SYNCED BUFFER=second DISTANCE=2 SPEED=4')
    assert runner.moves == ['G1 E2 F240']
    assert first.stepper.trapq_sets == [first.sync.trapq]
    assert second.stepper.trapq_sets[-2:] == [printer.lookup_object('extruder').trapq,
                                           second.sync.trapq]
    assert not first._macro_state_saved and not second._macro_state_saved
    assert not runner.relative


@pytest.mark.parametrize('stage', ['M83', 'SYNC', 'G1', 'M400', 'UNSYNC', 'RESTORE'])
@pytest.mark.parametrize('relative', [False, True])
def test_final_load_adopts_original_mode_on_failure(monkeypatch, stage, relative):
    printer, feeder, runner = legacy_load_setup(monkeypatch)
    runner.relative = relative
    runner.run('_SAVE_E_MODE')
    original_error = RuntimeError(stage + ' failed')
    original = runner.run
    original_unsync = feeder._unsync_if_synced
    cleanup = []

    def run(script):
        if stage in ('M83', 'G1', 'M400') and any(
                line.split()[0] == stage for line in script.splitlines()):
            raise original_error
        if script.startswith('RESTORE_GCODE_STATE'):
            cleanup.append('RESTORE')
            if stage == 'RESTORE':
                raise original_error
        result = original(script)
        if stage == 'SYNC' and script.startswith('BUFFER_SYNC_TO_EXTRUDER'):
            raise original_error
        return result

    def unsync():
        cleanup.append('UNSYNC')
        if stage == 'UNSYNC':
            raise original_error
        return original_unsync()

    monkeypatch.setattr(runner.gcode, 'run_script_from_command', run)
    monkeypatch.setattr(feeder, '_unsync_if_synced', unsync)
    with pytest.raises(RuntimeError) as caught:
        runner.run('BUFFER_LOAD_SYNCED BUFFER=mellow DISTANCE=2 SPEED=4')
    assert caught.value is original_error
    assert cleanup == (['RESTORE'] if stage == 'M83' else ['UNSYNC', 'RESTORE'])
    assert feeder._macro_state_saved is (stage == 'RESTORE')
    assert runner.relative is (True if stage == 'RESTORE' else relative)
    if stage != 'UNSYNC':
        assert feeder._stepper_synced_to is None
        assert feeder.stepper.trapq is feeder.sync.trapq
    assert len([c for c in runner.commands if c.startswith('SAVE_GCODE_STATE')]) == 1


@pytest.mark.parametrize('guard', ['grace', 'busy', 'jam', 'halt', 'synced', 'extruder'])
def test_final_load_guards_precede_save_and_motion(monkeypatch, guard):
    printer, feeder, runner = legacy_load_setup(monkeypatch)
    params = {'BUFFER': 'mellow'}
    if guard == 'grace':
        feeder._startup_grace_done = False
    elif guard == 'busy':
        feeder._state = 'LOADING_PULL'
    elif guard == 'jam':
        feeder._jam_active = True
    elif guard == 'halt':
        feeder._halt_requested = True
    elif guard == 'synced':
        feeder._stepper_synced_to = 'extruder'
    else:
        params['EXTRUDER'] = 'extruder1'
    before = list(feeder.stepper.trapq_sets)
    with pytest.raises(RuntimeError):
        feeder.cmd_BUFFER_LOAD_SYNCED(FakeGCmd(params))
    assert not runner.commands
    assert feeder.stepper.trapq_sets == before
    assert not feeder._macro_state_saved


def test_final_load_keeps_staged_hall1_path(monkeypatch):
    printer, feeder, runner = legacy_load_setup(monkeypatch)
    set_sensor_active(feeder, 'hall_overflow', True)
    feeder._state = 'OVERFLOW'
    runner.run('BUFFER_LOAD_SYNCED BUFFER=mellow DISTANCE=2 SPEED=4')
    assert runner.moves == ['G1 E2 F240']
    assert feeder.stepper.trapq is feeder.sync.trapq
    assert feeder._stepper_synced_to is None
    assert feeder.hall_overflow


def test_macro_restore_keeps_best_effort_retry_policy(monkeypatch):
    printer, feeder, runner = legacy_load_setup(monkeypatch)
    runner.run('_SAVE_E_MODE')
    original = runner.gcode.run_script_from_command

    def fail(script):
        raise RuntimeError('restore failed')

    monkeypatch.setattr(runner.gcode, 'run_script_from_command', fail)
    feeder.cmd_BUFFER_RESTORE_MACRO_STATE(FakeGCmd())
    assert feeder._macro_state_saved and runner.relative
    monkeypatch.setattr(runner.gcode, 'run_script_from_command', original)
    feeder.cmd_BUFFER_RESTORE_MACRO_STATE(FakeGCmd())
    assert not feeder._macro_state_saved and not runner.relative


@pytest.mark.parametrize('fail_second_save', [False, True])
def test_explicit_macro_resave_ack_preserves_or_replaces_slot(monkeypatch, fail_second_save):
    printer, feeder, runner = legacy_load_setup(monkeypatch)
    feeder.cmd_BUFFER_SAVE_MACRO_STATE(FakeGCmd())
    runner.relative = True
    if fail_second_save:
        original_error = RuntimeError('resave rejected')

        def fail(script):
            raise original_error

        with monkeypatch.context() as patch:
            patch.setattr(runner.gcode, 'run_script_from_command', fail)
            with pytest.raises(RuntimeError) as caught:
                feeder.cmd_BUFFER_SAVE_MACRO_STATE(FakeGCmd())
            assert caught.value is original_error
    else:
        feeder.cmd_BUFFER_SAVE_MACRO_STATE(FakeGCmd())
    assert feeder._macro_state_saved
    feeder.cmd_BUFFER_RESTORE_MACRO_STATE(FakeGCmd())
    assert runner.relative is (not fail_second_save)
    assert not feeder._macro_state_saved
