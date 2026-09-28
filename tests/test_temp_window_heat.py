"""Temperaturfenster fuer LOAD/UNLOAD-Aufheizen (User-Request 2026-07-13).

M109 wartet auf Ziel-Temperatur in BEIDE Richtungen (inkl. Runter-
Warten bei Ueberschwingen) und blockiert bis exakt eingeregelt.
Fuer LOAD/UNLOAD reicht "warm genug": M104 (Ziel setzen) +
TEMPERATURE_WAIT MINIMUM=(Ziel - Fenster, geclampt auf min_temp) —
kein Runter-Warten, Weiterlauf sobald das Fenster erreicht ist.
Fenster via TEMP_WINDOW-Param (Default 5.0 C).
"""

from fakes_klipper import FakeConfig, FakePrinter
from helpers import FakeGCmd
from klipper_extras import buffer_feeder


def make_cold_feeder(temp=100.0):
    printer = FakePrinter()
    feeder = buffer_feeder.BufferFeeder(FakeConfig(printer=printer))
    printer.fire_event('klippy:connect')
    feeder._startup_grace_done = True
    printer.lookup_object('extruder').heater.temperature = temp
    return printer, feeder


def heat_script(printer):
    scripts = [s for _, s in printer.lookup_object('gcode').scripts]
    return scripts[0] if scripts else ""


def test_unload_heat_uses_temperature_wait_window():
    printer, feeder = make_cold_feeder(temp=100.0)

    feeder.cmd_BUFFER_UNLOAD_FILAMENT(FakeGCmd())

    script = heat_script(printer)
    assert "M109" not in script, (
        "M109 waits for the exact target (both directions) — use "
        "M104 + TEMPERATURE_WAIT MINIMUM with a window instead")
    assert "M104 S250" in script
    assert "TEMPERATURE_WAIT SENSOR=extruder MINIMUM=245" in script, (
        "default window is 5C below AUTO_HEAT_TARGET=250")


def test_unload_heat_window_param_and_min_temp_clamp():
    printer, feeder = make_cold_feeder(temp=100.0)

    feeder.cmd_BUFFER_UNLOAD_FILAMENT(
        FakeGCmd({'AUTO_HEAT_TARGET': 182.0, 'TEMP_WINDOW': 20.0}))

    script = heat_script(printer)
    # 182-20=162 laege unter min_temp (180) -> Clamp auf min_temp,
    # sonst laufen die G1-E-Moves unter der Extrude-Schwelle los.
    assert "TEMPERATURE_WAIT SENSOR=extruder MINIMUM=180" in script


def test_unload_no_heat_when_warm_enough():
    printer, feeder = make_cold_feeder(temp=220.0)

    feeder.cmd_BUFFER_UNLOAD_FILAMENT(FakeGCmd())

    assert "TEMPERATURE_WAIT" not in heat_script(printer).split("\n")[0] \
        or "MINIMUM" not in heat_script(printer)
    assert "M104 S250" not in heat_script(printer), (
        "temp >= min_temp: no auto-heat at all (existing behavior)")
