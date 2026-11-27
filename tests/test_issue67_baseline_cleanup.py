"""Issue #67: baseline failures must not leak temporary safety overrides."""
from pathlib import Path

import pytest

from helpers import FakeGCmd


class RawGCmd(FakeGCmd):
    def __init__(self):
        super().__init__({'BUFFER': 'mellow'})

    def get_raw_command_parameters(self):
        return 'BUFFER=mellow FLOW=35 CASE_ID=case_7 SPEED=80'


@pytest.mark.parametrize('failure', [None, 'PREP', 'G1'])
@pytest.mark.parametrize('jam_action', ['PAUSE', 'MY_JAM ARG=1', ''])
def test_baseline_wrapper_restores_temporary_state(
        feeder_factory, monkeypatch, failure, jam_action):
    printer, feeder = feeder_factory()
    gc = printer.lookup_object('gcode')
    feeder.jam_action = jam_action
    feeder.hall3_demand_gain = 1.23456
    feeder.buffer_debug_events = False
    feeder.buffer_debug_metrics = True
    feeder._print_running = True
    scripts = []

    def run_script(script):
        scripts.append(script)
        if script.startswith('_BUFFER_BASELINE_RUN '):
            feeder.jam_action = ''
            feeder.hall3_demand_gain = 1.5
            feeder.buffer_debug_events = True
            feeder.buffer_debug_metrics = False
            feeder._print_running = False
            feeder._benchmark_mode_until = feeder.reactor.monotonic() + 900
            feeder.feed_speed = 80
            if failure:
                raise RuntimeError('injected ' + failure)

    monkeypatch.setattr(gc, 'run_script_from_command', run_script)
    if failure:
        with pytest.raises(RuntimeError, match='injected ' + failure):
            feeder.cmd_BUFFER_RUN_BASELINE(RawGCmd())
    else:
        feeder.cmd_BUFFER_RUN_BASELINE(RawGCmd())

    assert feeder.jam_action == jam_action
    assert feeder.hall3_demand_gain == 1.23456
    assert feeder.buffer_debug_events is False
    assert feeder.buffer_debug_metrics is True
    assert feeder._print_running is True
    assert feeder._benchmark_mode_until == 0
    assert feeder.feed_speed == 80
    assert scripts == [
        'SAVE_GCODE_STATE NAME=buffer_baseline_run_mellow',
        '_BUFFER_BASELINE_RUN BUFFER=mellow FLOW=35 CASE_ID=case_7 SPEED=80',
        'RESTORE_GCODE_STATE NAME=buffer_baseline_run_mellow MOVE=0',
    ]


def test_baseline_cleanup_failure_still_restores_gcode(feeder_factory, monkeypatch):
    printer, feeder = feeder_factory()
    gc = printer.lookup_object('gcode')
    scripts = []

    def run_script(script):
        scripts.append(script)
        if script.startswith('_BUFFER_BASELINE_RUN '):
            feeder.jam_action = ''
            feeder.hall3_demand_gain = 2
            feeder._print_running = False

    def fail_disable(*args, **kwargs):
        raise RuntimeError('injected bench cleanup')

    feeder._print_running = True
    monkeypatch.setattr(gc, 'run_script_from_command', run_script)
    monkeypatch.setattr(feeder, '_set_benchmark_mode', fail_disable)
    with pytest.raises(RuntimeError, match='bench cleanup'):
        feeder.cmd_BUFFER_RUN_BASELINE(RawGCmd())
    assert feeder.jam_action == 'PAUSE'
    assert feeder.hall3_demand_gain == 1.5
    assert feeder._print_running is True
    assert scripts[-1] == 'RESTORE_GCODE_STATE NAME=buffer_baseline_run_mellow MOVE=0'


def test_baseline_public_macro_keeps_variables_and_forwards_rawparams(feeder_factory):
    cfg = (Path(__file__).resolve().parents[1] / 'lll.cfg').read_text(encoding='utf-8')
    public, internal = cfg.split('[gcode_macro BUFFER_BASELINE_RUN]', 1)[1].split(
        '[gcode_macro _BUFFER_BASELINE_RUN]', 1)
    assert 'variable_default_flow: 30' in public
    assert 'BUFFER_RUN_BASELINE ' in public
    assert 'BUFFER=mellow {% endif %}{rawparams}' in public
    assert 'printer["gcode_macro BUFFER_BASELINE_RUN"]' in internal
    assert 'SAVE_GCODE_STATE NAME=buffer_baseline_run_mellow' not in internal
    assert 'RESTORE_GCODE_STATE NAME=buffer_baseline_run_mellow' not in internal
    printer, feeder = feeder_factory()
    assert any(r['mux_key'] == 'BUFFER' and r['mux_value'] == feeder.name
               for r in printer.lookup_object('gcode').mux_commands['BUFFER_RUN_BASELINE'])


@pytest.mark.parametrize('explicit_selector', [True, False])
def test_baseline_second_instance_routes_rendered_body_and_restores(
        feeder_factory, monkeypatch, explicit_selector):
    import ast
    import jinja2
    from fakes_klipper import FakeConfig
    from klipper_extras.buffer_feeder import BufferFeeder
    from test_buffer_set_runtime_tuning import FakeGCmd as TuningGCmd

    printer, mellow = feeder_factory()
    other = BufferFeeder(FakeConfig(printer=printer, name='buffer_feeder other'))
    other.jam_action = 'OTHER_JAM'
    gc = printer.lookup_object('gcode')
    cfg = (Path(__file__).resolve().parents[1] / 'lll.cfg').read_text(encoding='utf-8')
    public, body = cfg.split('[gcode_macro BUFFER_BASELINE_RUN]', 1)[1].split(
        '[gcode_macro _BUFFER_BASELINE_RUN]', 1)
    defaults = {}
    for line in public.splitlines():
        if line.startswith('variable_'):
            key, value = line.split(':', 1)
            defaults[key[len('variable_'):]] = ast.literal_eval(value.strip())
    body = body.split('gcode:', 1)[1].split('[delayed_gcode', 1)[0]
    env = jinja2.Environment(variable_start_string='{', variable_end_string='}')
    template = env.from_string(body)
    status = {'buffer_feeder mellow': vars(mellow),
                 'buffer_feeder other': vars(other),
                 'gcode_macro BUFFER_BASELINE_RUN': defaults,
                 'toolhead': {'homed_axes': 'xyz'}}
    routed = []

    def run_script(script):
        if not script.startswith('_BUFFER_BASELINE_RUN '):
            return
        params = dict(word.split('=', 1) for word in script.split()[1:])
        rendered = template.render(params=params, printer=status)
        for line in rendered.splitlines():
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if 'BUFFER=' in line:
                routed.append(line)
            if line.startswith('BUFFER_SET '):
                for registration in gc.mux_commands['BUFFER_SET']:
                    params = dict(word.split('=', 1) for word in line.split()[1:])
                    if registration['mux_value'] == params['BUFFER']:
                        registration['handler'](TuningGCmd(params))
                        break
            if line.startswith('BUFFER_PREP_BASELINE '):
                raise RuntimeError('injected PREP')

    class OtherGCmd(TuningGCmd):
        def get_raw_command_parameters(self):
            return 'BUFFER=other' if explicit_selector else ''

    monkeypatch.setattr(gc, 'run_script_from_command', run_script)
    with pytest.raises(RuntimeError, match='injected PREP'):
        if explicit_selector:
            for registration in gc.mux_commands['BUFFER_RUN_BASELINE']:
                if registration['mux_value'] == 'other':
                    registration['handler'](OtherGCmd({'BUFFER': 'other'}))
                    break
        else:
            # Model the single-instance default-mux call without BUFFER=.
            other.cmd_BUFFER_RUN_BASELINE(OtherGCmd())
    assert routed
    assert all('BUFFER=other' in line for line in routed)
    assert other.jam_action == 'OTHER_JAM'
    assert mellow.jam_action == 'PAUSE'
    assert mellow.buffer_debug_events is False
