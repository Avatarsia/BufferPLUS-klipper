import pytest

from fakes_klipper import FakeConfig, FakePrinter
from klipper_extras.buffer_config import BufferConfigValues
from klipper_extras import buffer_feeder


def test_sensor_offset_default_and_override():
    assert BufferConfigValues.from_config(FakeConfig()).load_sensor_to_extruder == 10.
    settings = BufferConfigValues.from_config(FakeConfig(values={
        'load_sensor_to_extruder': 5.5,
    }))
    assert settings.load_sensor_to_extruder == 5.5


def test_missing_configured_sensor_explains_section_and_legacy_option():
    with pytest.raises(RuntimeError) as error:
        buffer_feeder.BufferFeeder(FakeConfig(values={
            'load_endstop_sensor': 'filament_switch_sensor missing_head',
        }))
    message = str(error.value)
    assert '[filament_switch_sensor missing_head]' in message
    assert 'load_endstop_sensor' in message
    assert 'switch_pin' in message


def test_unconfigured_sensor_preserves_legacy_unload(monkeypatch):
    printer = FakePrinter()
    feeder = buffer_feeder.BufferFeeder(FakeConfig(printer))
    assert feeder._load_endstop_sensor is None
    assert feeder.load_endstop_sensor == ''
    called = []
    monkeypatch.setattr(feeder.toolhead_sensor, 'unload', lambda cmd: called.append(cmd))
    # The original startup guard must still reject this before any movement.
    with pytest.raises(RuntimeError, match='startup grace'):
        feeder.cmd_BUFFER_UNLOAD_FILAMENT(None)
    assert not called
