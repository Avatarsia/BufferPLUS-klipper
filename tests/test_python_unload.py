import pytest

from fakes_klipper import FakeConfig, FakePrinter
from klipper_extras import buffer_feeder


class FakeGCmd:
    def __init__(self, values=None):
        self.values = {key.upper(): value for key, value in (values or {}).items()}

    def get(self, key, default=None):
        return self.values.get(key.upper(), default)

    def get_int(self, key, default=None, **kwargs):
        return int(self.values.get(key.upper(), default))

    def get_float(self, key, default=None, **kwargs):
        return float(self.values.get(key.upper(), default))


def test_buffer_unload_filament_smoke():
    fake_printer = FakePrinter()
    fake_config = FakeConfig(
        printer=fake_printer,
        values={"unload_sync_distance": 250},
    )
    feeder = buffer_feeder.BufferFeeder(fake_config)
    gcode = fake_printer.lookup_object("gcode")
    fake_printer.lookup_object("extruder").heater.temperature = 220.0

    feeder.cmd_BUFFER_UNLOAD_FILAMENT(FakeGCmd())

    scripts = gcode.scripts
    assert "BUFFER_UNLOAD_FILAMENT" in gcode.commands
    assert [mode for mode, _ in scripts] == ["run_script_from_command"] * 6
    assert scripts[0][1] == "SAVE_GCODE_STATE NAME=buffer_feeder_op"
    assert scripts[1][1] == "M83"
    assert scripts[2][1] == "BUFFER_SYNC_TO_EXTRUDER BUFFER=mellow EXTRUDER=extruder"
    assert scripts[3][1] == "\n".join([
        "G1 E8 F1200",
        "G1 E-10 F1200",
        "G1 E8 F1200",
        "G1 E-10 F1200",
        "G1 E8 F1200",
        "G1 E-10 F1200",
        "G1 E8 F1200",
        "G1 E-10 F1200",
        "G1 E-25 F3000",
        "G1 E-250 F3000",
        "M400",
    ])
    assert scripts[4][1] == "BUFFER_UNLOAD_PHASE3 BUFFER=mellow MAX_DISTANCE=2510 SPEED=50"
    assert scripts[5][1] == "RESTORE_GCODE_STATE NAME=buffer_feeder_op MOVE=0"
    assert gcode.info_messages[-1] == "BufferFeeder: UNLOAD abgeschlossen (Python workflow)"
    assert feeder._macro_state_saved is False


def test_buffer_unload_filament_unsyncs_on_error(monkeypatch):
    fake_printer = FakePrinter()
    fake_config = FakeConfig(
        printer=fake_printer,
        values={"unload_sync_distance": 250},
    )
    feeder = buffer_feeder.BufferFeeder(fake_config)
    gcode = fake_printer.lookup_object("gcode")
    fake_printer.lookup_object("extruder").heater.temperature = 220.0
    events = []

    def fake_run_script_from_command(script):
        events.append(("script", script))
        if script.startswith("G1 E8 F1200"):
            raise RuntimeError("boom")

    def fake_unsync():
        events.append(("unsync", None))
        return True

    monkeypatch.setattr(gcode, "run_script_from_command", fake_run_script_from_command)
    monkeypatch.setattr(feeder, "_unsync_if_synced", fake_unsync)

    with pytest.raises(RuntimeError, match="boom"):
        feeder.cmd_BUFFER_UNLOAD_FILAMENT(FakeGCmd())

    assert events == [
        ("script", "SAVE_GCODE_STATE NAME=buffer_feeder_op"),
        ("script", "M83"),
        ("script", "BUFFER_SYNC_TO_EXTRUDER BUFFER=mellow EXTRUDER=extruder"),
        ("script", "\n".join([
            "G1 E8 F1200",
            "G1 E-10 F1200",
            "G1 E8 F1200",
            "G1 E-10 F1200",
            "G1 E8 F1200",
            "G1 E-10 F1200",
            "G1 E8 F1200",
            "G1 E-10 F1200",
            "G1 E-25 F3000",
            "G1 E-250 F3000",
            "M400",
        ])),
        ("unsync", None),
        ("script", "RESTORE_GCODE_STATE NAME=buffer_feeder_op MOVE=0"),
    ]
    assert feeder._macro_state_saved is False
