"""unload_phase3_speed — entkoppelt UNLOAD-Phase-3-Geschwindigkeit von
unload_fast_speed.

Problem:
  BUFFER_UNLOAD_PHASE3 und der synced G1 E-{sync_dist}-Move beim UNLOAD
  teilten sich denselben Konfigurationswert (unload_fast_speed).
  Der Sync-Move laeuft unter Extruder-Kontrolle — bei manchen Extrudern
  fuehren hohe Geschwindigkeiten (>10 mm/s) zum Schleifgeraeusch oder
  Greiferblockierung. Wer deshalb unload_fast_speed reduziert (z.B. 10 mm/s),
  zwang damit auch PHASE3 auf diese niedrige Geschwindigkeit. PHASE3 laeuft
  jedoch auf dem eigenen trapq des Buffers ohne Extruder-Kopplung und koennte
  problemlos mit 50+ mm/s laufen.

Fix:
  Neuer optionaler Konfigurationsschluessel unload_phase3_speed
  (Default = unload_fast_speed, also rueckwaertskompatibel).
  - cmd_BUFFER_UNLOAD_PHASE3 benutzt unload_phase3_speed als SPEED-Default.
  - Der Python-UNLOAD-Pfad (cmd_BUFFER_UNLOAD_FILAMENT) embedded
    unload_phase3_speed in den BUFFER_UNLOAD_PHASE3-Aufruf.

Typische Einstellung nach dem Fix:
  unload_fast_speed:   10  # mm/s — langsam fuer den Sync-Move (Extruder)
  unload_phase3_speed: 50  # mm/s — schnell fuer Phase 3 (Buffer allein)
"""

from fakes_klipper import FakeConfig, FakePrinter
from klipper_extras import buffer_feeder


class FakeGCmd:
    """Minimales GCmd-Fake: kein Wert gesetzt → Default wird durchgereicht."""
    def __init__(self, values=None):
        self.values = {k.upper(): v for k, v in (values or {}).items()}
        self.float_calls = {}   # Protokolliert: key → (default, returned)

    def get(self, key, default=None):
        return self.values.get(key.upper(), default)

    def get_int(self, key, default=None, **kwargs):
        return int(self.values.get(key.upper(), default))

    def get_float(self, key, default=None, **kwargs):
        result = float(self.values.get(key.upper(), default))
        self.float_calls[key.upper()] = (default, result)
        return result


def make_feeder(values=None):
    printer = FakePrinter()
    config = FakeConfig(printer=printer, values=values or {})
    feeder = buffer_feeder.BufferFeeder(config)
    feeder._startup_grace_done = True
    return printer, feeder


# ---------------------------------------------------------------------------
# Konfiguration — Standardwerte und unabhaengige Konfigurierbarkeit
# ---------------------------------------------------------------------------

def test_unload_phase3_speed_defaults_to_unload_fast_speed():
    """Wenn unload_phase3_speed nicht gesetzt ist, entspricht er
    unload_fast_speed — rueckwaertskompatibel."""
    _, feeder = make_feeder({"unload_fast_speed": 30.0})

    assert feeder.unload_phase3_speed == 30.0, (
        "Default von unload_phase3_speed muss unload_fast_speed (30.0) sein, "
        "ist aber %.1f" % feeder.unload_phase3_speed
    )


def test_unload_phase3_speed_configurable_independently():
    """unload_phase3_speed kann unabhaengig von unload_fast_speed gesetzt
    werden — Kernzweck des Features."""
    _, feeder = make_feeder({
        "unload_fast_speed":   10.0,
        "unload_phase3_speed": 50.0,
    })

    assert feeder.unload_fast_speed == 10.0
    assert feeder.unload_phase3_speed == 50.0


def test_unload_phase3_speed_default_is_factory_50():
    """Ohne jegliche Konfiguration: beide Werte auf Fabrik-Default 50 mm/s."""
    _, feeder = make_feeder()

    assert feeder.unload_fast_speed == 50.0
    assert feeder.unload_phase3_speed == 50.0


# ---------------------------------------------------------------------------
# Python-UNLOAD-Pfad — korrekte Geschwindigkeit im PHASE3-Script-Aufruf
# ---------------------------------------------------------------------------

def test_unload_filament_passes_phase3_speed_to_phase3_call():
    """cmd_BUFFER_UNLOAD_FILAMENT muss im BUFFER_UNLOAD_PHASE3-Aufruf
    unload_phase3_speed einbetten, NICHT unload_fast_speed.

    Kerntest: unload_fast_speed=10 (langsam fuer Sync-Move),
    unload_phase3_speed=50 (schnell fuer Buffer-Phase) — das generierte
    Skript muss SPEED=50 fuer PHASE3 enthalten."""
    printer, feeder = make_feeder({
        "unload_fast_speed":   10.0,
        "unload_phase3_speed": 50.0,
        "unload_sync_distance": 250.0,
    })
    printer.lookup_object("extruder").heater.temperature = 220.0
    gcode = printer.lookup_object("gcode")

    feeder.cmd_BUFFER_UNLOAD_FILAMENT(FakeGCmd())

    phase3_calls = [s for _, s in gcode.scripts
                    if s.startswith("BUFFER_UNLOAD_PHASE3")]
    assert len(phase3_calls) == 1, "Kein BUFFER_UNLOAD_PHASE3-Aufruf gefunden"
    call = phase3_calls[0]
    assert "SPEED=50" in call, (
        "BUFFER_UNLOAD_PHASE3 muss SPEED=50 (unload_phase3_speed) enthalten, "
        "nicht SPEED=10 (unload_fast_speed). Tatsaechlicher Aufruf: %r" % call
    )


def test_unload_filament_uses_fast_speed_for_sync_move_not_phase3():
    """Der synced G1 E-{sync_dist}-Move benutzt unload_fast_speed (10 mm/s),
    PHASE3 benutzt unload_phase3_speed (50 mm/s) — beide Werte sind im
    generierten Skript korrekt zugeordnet."""
    printer, feeder = make_feeder({
        "unload_fast_speed":   10.0,
        "unload_phase3_speed": 50.0,
        "unload_sync_distance": 250.0,
    })
    printer.lookup_object("extruder").heater.temperature = 220.0
    gcode = printer.lookup_object("gcode")

    feeder.cmd_BUFFER_UNLOAD_FILAMENT(FakeGCmd())

    scripts = [s for _, s in gcode.scripts]

    # Sync-Move-Block: G1 E-250 mit unload_fast_speed (10 mm/s → F600)
    sync_block = next((s for s in scripts if "G1 E-250" in s), None)
    assert sync_block is not None, "Kein Sync-Move-Block gefunden"
    assert "F600" in sync_block, (
        "Sync-Move G1 E-250 muss F600 (10 mm/s) enthalten; "
        "gefunden: %r" % sync_block
    )

    # PHASE3-Aufruf: unload_phase3_speed (50 mm/s)
    phase3_call = next((s for s in scripts
                        if s.startswith("BUFFER_UNLOAD_PHASE3")), None)
    assert phase3_call is not None
    assert "SPEED=50" in phase3_call, (
        "BUFFER_UNLOAD_PHASE3 muss SPEED=50 enthalten; "
        "gefunden: %r" % phase3_call
    )


def test_unload_filament_backwards_compat_single_speed():
    """Rueckwaertskompatibilitaet: Nur unload_fast_speed gesetzt,
    unload_phase3_speed nicht konfiguriert → beide benutzen fast_speed."""
    printer, feeder = make_feeder({
        "unload_fast_speed": 30.0,
        "unload_sync_distance": 250.0,
    })
    printer.lookup_object("extruder").heater.temperature = 220.0
    gcode = printer.lookup_object("gcode")

    feeder.cmd_BUFFER_UNLOAD_FILAMENT(FakeGCmd())

    phase3_calls = [s for _, s in gcode.scripts
                    if s.startswith("BUFFER_UNLOAD_PHASE3")]
    assert len(phase3_calls) == 1
    assert "SPEED=30" in phase3_calls[0], (
        "Ohne unload_phase3_speed muss PHASE3 unload_fast_speed (30.0) "
        "verwenden. Aufruf: %r" % phase3_calls[0]
    )


# ---------------------------------------------------------------------------
# cmd_BUFFER_UNLOAD_PHASE3 — benutzt phase3_speed als SPEED-Default
# ---------------------------------------------------------------------------

def test_cmd_buffer_unload_phase3_speed_default_is_phase3_speed():
    """cmd_BUFFER_UNLOAD_PHASE3 muss unload_phase3_speed als SPEED-Default
    verwenden, nicht unload_fast_speed.

    FakeGCmd zeichnet auf, welcher Default-Wert fuer 'SPEED' uebergeben
    wurde — so koennen wir pruefen, ob der richtige Wert als Default dient."""
    _, feeder = make_feeder({
        "unload_fast_speed":   10.0,
        "unload_phase3_speed": 75.0,
    })

    gcmd = FakeGCmd()
    # _check_phase_entry verlangt IDLE/AUTO/OVERFLOW/JAM/RUNOUT/UNLOAD_PHASE_3.
    feeder._state = buffer_feeder.STATE_IDLE
    # cmd_BUFFER_UNLOAD_PHASE3 laeuft durch (entrance-Sensor inaktiv → Loop
    # endet sofort nach einer Iteration).
    feeder.cmd_BUFFER_UNLOAD_PHASE3(gcmd)

    assert "SPEED" in gcmd.float_calls, \
        "cmd_BUFFER_UNLOAD_PHASE3 hat gcmd.get_float('SPEED', ...) nicht aufgerufen"
    speed_default, speed_used = gcmd.float_calls["SPEED"]
    assert speed_default == 75.0, (
        "SPEED-Default muss unload_phase3_speed (75.0) sein, "
        "nicht unload_fast_speed (10.0). Tatsaechlich: %.1f" % speed_default
    )


# ---------------------------------------------------------------------------
# get_status() — Feld ist sichtbar
# ---------------------------------------------------------------------------

def test_status_exposes_unload_phase3_speed():
    """get_status() muss unload_phase3_speed enthalten — Macros und
    Moonraker-Dashboards koennen den Wert lesen."""
    _, feeder = make_feeder({
        "unload_fast_speed":   10.0,
        "unload_phase3_speed": 50.0,
    })

    status = feeder.get_status(0.0)

    assert "unload_phase3_speed" in status, \
        "get_status() enthaelt kein 'unload_phase3_speed'-Feld"
    assert status["unload_phase3_speed"] == 50.0
