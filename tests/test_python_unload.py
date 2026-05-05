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
    # P7-64: 8 scripts statt 6 — pre-cool moves, cooling-move,
    # post-cool moves sind jetzt drei separate run_script_from_command
    # Aufrufe.
    assert [mode for mode, _ in scripts] == ["run_script_from_command"] * 8
    assert scripts[0][1] == "SAVE_GCODE_STATE NAME=buffer_feeder_op"
    assert scripts[1][1] == "M83"
    assert scripts[2][1] == "BUFFER_SYNC_TO_EXTRUDER BUFFER=mellow EXTRUDER=extruder"
    # Pre-cool: 6 cycles (P7-64 default tip_cycles=6) à 2 G1 = 12 lines.
    # tip_pull default = 14 (P7-64).
    assert scripts[3][1] == "\n".join([
        "G1 E8 F1200",
        "G1 E-14 F1200",
        "G1 E8 F1200",
        "G1 E-14 F1200",
        "G1 E8 F1200",
        "G1 E-14 F1200",
        "G1 E8 F1200",
        "G1 E-14 F1200",
        "G1 E8 F1200",
        "G1 E-14 F1200",
        "G1 E8 F1200",
        "G1 E-14 F1200",
    ])
    # Cooling-Move: M118 + M104 + TEMPERATURE_WAIT.
    assert scripts[4][1] == "\n".join([
        "M118 UNLOAD Cooling-Move: heize runter auf 150 C",
        "M104 S150",
        "TEMPERATURE_WAIT SENSOR=extruder MAXIMUM=160",
    ])
    # Post-cool: tip_final_retract default = 50 (P7-64), sync_dist=250
    # (Test-override), M400.
    assert scripts[5][1] == "\n".join([
        "G1 E-50 F3000",
        "G1 E-250 F3000",
        "M400",
    ])
    # MAX_DISTANCE: unload_fast_max default = 5000 (P7-64) — Test
    # ueberschreibt nur unload_sync_distance, also kommt der neue
    # 5000-default zum Tragen.
    assert scripts[6][1] == "BUFFER_UNLOAD_PHASE3 BUFFER=mellow MAX_DISTANCE=5000 SPEED=50"
    assert scripts[7][1] == "RESTORE_GCODE_STATE NAME=buffer_feeder_op MOVE=0"
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
        # P7-64: pre-cool moves sind jetzt ein separates Script. Erste
        # Zeile beginnt mit "G1 E8 F1200" — das ist der erste push.
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
            "G1 E-14 F1200",
            "G1 E8 F1200",
            "G1 E-14 F1200",
            "G1 E8 F1200",
            "G1 E-14 F1200",
            "G1 E8 F1200",
            "G1 E-14 F1200",
            "G1 E8 F1200",
            "G1 E-14 F1200",
            "G1 E8 F1200",
            "G1 E-14 F1200",
        ])),
        ("unsync", None),
        ("script", "RESTORE_GCODE_STATE NAME=buffer_feeder_op MOVE=0"),
    ]
    assert feeder._macro_state_saved is False


def test_buffer_unload_filament_skips_cooling_when_disabled():
    """P7-64: USE_COOLING_MOVE=0 ueberspringt den Cooling-Move-Block.
    Pre-cool und post-cool Scripts folgen direkt aufeinander, kein
    Cooling-Move-Script dazwischen."""
    fake_printer = FakePrinter()
    fake_config = FakeConfig(
        printer=fake_printer,
        values={"unload_sync_distance": 250},
    )
    feeder = buffer_feeder.BufferFeeder(fake_config)
    gcode = fake_printer.lookup_object("gcode")
    fake_printer.lookup_object("extruder").heater.temperature = 220.0

    feeder.cmd_BUFFER_UNLOAD_FILAMENT(FakeGCmd({"USE_COOLING_MOVE": 0}))

    scripts = gcode.scripts
    # 7 statt 8 scripts (Cooling-Move-Block fehlt).
    assert [mode for mode, _ in scripts] == ["run_script_from_command"] * 7
    assert scripts[0][1] == "SAVE_GCODE_STATE NAME=buffer_feeder_op"
    assert scripts[1][1] == "M83"
    assert scripts[2][1] == "BUFFER_SYNC_TO_EXTRUDER BUFFER=mellow EXTRUDER=extruder"
    # Pre-cool moves direkt gefolgt von post-cool moves, kein Cooling.
    assert scripts[3][1].startswith("G1 E8 F1200")
    assert scripts[3][1].endswith("G1 E-14 F1200")
    assert scripts[4][1] == "\n".join([
        "G1 E-50 F3000",
        "G1 E-250 F3000",
        "M400",
    ])
    # Sicherheit: kein Cooling-Move-Script in scripts.
    for _, script in scripts:
        assert "Cooling-Move" not in script
        assert "M104" not in script
        assert "TEMPERATURE_WAIT" not in script
    assert scripts[5][1] == "BUFFER_UNLOAD_PHASE3 BUFFER=mellow MAX_DISTANCE=5000 SPEED=50"
    assert scripts[6][1] == "RESTORE_GCODE_STATE NAME=buffer_feeder_op MOVE=0"


def test_buffer_unload_filament_uses_custom_cool_temp():
    """P7-64: COOL_TEMP und COOL_TEMP_MAX werden vom Cooling-Move-Script
    uebernommen."""
    fake_printer = FakePrinter()
    fake_config = FakeConfig(
        printer=fake_printer,
        values={"unload_sync_distance": 250},
    )
    feeder = buffer_feeder.BufferFeeder(fake_config)
    gcode = fake_printer.lookup_object("gcode")
    fake_printer.lookup_object("extruder").heater.temperature = 220.0

    feeder.cmd_BUFFER_UNLOAD_FILAMENT(FakeGCmd({
        "COOL_TEMP": 120,
        "COOL_TEMP_MAX": 130,
    }))

    scripts = gcode.scripts
    cooling_script = scripts[4][1]
    assert "M104 S120" in cooling_script
    assert "MAXIMUM=130" in cooling_script
    assert "heize runter auf 120 C" in cooling_script


def test_buffer_unload_filament_uses_custom_extruder_name():
    """P7-64 Round 2: EXTRUDER=extruder1 wird im Cooling-Move
    TEMPERATURE_WAIT-Script und im BUFFER_SYNC_TO_EXTRUDER-Script
    verwendet — kein hardcoded SENSOR=extruder mehr."""
    fake_printer = FakePrinter()
    fake_config = FakeConfig(
        printer=fake_printer,
        values={"unload_sync_distance": 250},
    )
    feeder = buffer_feeder.BufferFeeder(fake_config)
    gcode = fake_printer.lookup_object("gcode")
    fake_printer.lookup_object("extruder").heater.temperature = 220.0

    feeder.cmd_BUFFER_UNLOAD_FILAMENT(FakeGCmd({"EXTRUDER": "extruder1"}))

    scripts = gcode.scripts
    # Sync-Script verwendet den custom extruder name.
    assert scripts[2][1] == "BUFFER_SYNC_TO_EXTRUDER BUFFER=mellow EXTRUDER=extruder1"
    # Cooling-Move TEMPERATURE_WAIT verwendet den custom extruder name,
    # NICHT mehr hardcoded "extruder".
    cooling_script = scripts[4][1]
    assert "TEMPERATURE_WAIT SENSOR=extruder1 MAXIMUM=160" in cooling_script
    assert "SENSOR=extruder " not in cooling_script
    assert "SENSOR=extruder\n" not in cooling_script


def test_buffer_unload_filament_skips_pre_cool_when_tip_cycles_zero():
    """P7-64 Round 2: TIP_CYCLES=0 erzeugt keinen leeren pre_cool_moves
    Script-Submit. Cooling-Move und post-cool bleiben aktiv."""
    fake_printer = FakePrinter()
    fake_config = FakeConfig(
        printer=fake_printer,
        values={"unload_sync_distance": 250},
    )
    feeder = buffer_feeder.BufferFeeder(fake_config)
    gcode = fake_printer.lookup_object("gcode")
    fake_printer.lookup_object("extruder").heater.temperature = 220.0

    feeder.cmd_BUFFER_UNLOAD_FILAMENT(FakeGCmd({"TIP_CYCLES": 0}))

    scripts = gcode.scripts
    # 7 statt 8 scripts (pre_cool entfaellt). Cooling-Move-Default=1
    # bleibt aktiv, post-cool laeuft.
    assert [mode for mode, _ in scripts] == ["run_script_from_command"] * 7
    assert scripts[0][1] == "SAVE_GCODE_STATE NAME=buffer_feeder_op"
    assert scripts[1][1] == "M83"
    assert scripts[2][1] == "BUFFER_SYNC_TO_EXTRUDER BUFFER=mellow EXTRUDER=extruder"
    # Direkt nach Sync kommt der Cooling-Move (kein leerer pre_cool).
    assert scripts[3][1] == "\n".join([
        "M118 UNLOAD Cooling-Move: heize runter auf 150 C",
        "M104 S150",
        "TEMPERATURE_WAIT SENSOR=extruder MAXIMUM=160",
    ])
    # Post-cool unveraendert.
    assert scripts[4][1] == "\n".join([
        "G1 E-50 F3000",
        "G1 E-250 F3000",
        "M400",
    ])
    assert scripts[5][1] == "BUFFER_UNLOAD_PHASE3 BUFFER=mellow MAX_DISTANCE=5000 SPEED=50"
    assert scripts[6][1] == "RESTORE_GCODE_STATE NAME=buffer_feeder_op MOVE=0"
    # Sicherheit: kein G1-Push-Script (waere pre_cool gewesen).
    for _, script in scripts:
        assert "G1 E8 F1200" not in script
