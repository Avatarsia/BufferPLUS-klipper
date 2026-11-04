# Extruder-Zuordnung pro Buffer

> Umsetzung mit superpowers:executing-plans; abschliessende Verifikation durch den vom Benutzer gewuenschten Claude-5.5-Low-Sub-Agenten.

**Ziel:** Zwei Buffer erfassen ausschliesslich den Verbrauch ihres konfigurierten Extruders. Bestehende Configs verwenden weiterhin `extruder`.

**Freigabe:** Chat: Option `extruder:`, getrennte AUTO-/Jam-Erfassung, LOAD/UNLOAD/SYNC und Regressionstests. Bestehender Checkout `refactor/cleanup-all-phases`.

**Architektur:** Startup-Option in BufferConfigValues; Verbrauch, Temperatur und Command-Defaults verwenden diese Zuordnung. Globaler Druckstatus und PAUSE bleiben druckerweit. G1-E-Workflows verlangen den passenden aktiven Extruder; keine automatische Tool-Umschaltung.

**Tech Stack:** Python, Klipper-Mux-Commands, pytest, Klipper-Jinja-Macros.

## Umsetzung

- [x] Regressionstests schreiben: getrennte Geschwindigkeiten/AUTO, Jam, Temperatur, Runout-Nachlauf, SYNC und aktive LOAD-/UNLOAD-Zuordnung; bestehender Default.
- [x] Neue Tests rot pruefen.
- [x] Config und Extruder-Zugriffe anpassen; unbekannte Extruder beim Connect ablehnen.
- [x] LOAD-/UNLOAD-Macros mit BUFFER-Auswahl versehen; Heizbefehle gezielt adressieren; Auto-Load korrekt dispatchen.
- [x] GCode-State-Slots pro Buffer trennen, damit Fehler-Recovery keinen anderen Slot ueberschreibt.
- [x] Beispielconfig und README um Multi-Extruder-Nutzung ergaenzen.
- [x] Gesamte pytest-Suite und git diff --check pruefen.
- [x] Ein abschliessender Claude-Haiku-5.5-Review mit Effort low; Findings anhand Code/Tests verifizieren.

## Verifikation und Entscheidungen

- Baseline: `pytest -q`: 947 passed, 8 skipped.
- Python auf PATH hat kein pytest; `pytest.exe` nutzt die vorhandene Python-3.12-Installation.
- Kein Commit/Push beauftragt. Vorhandene `.understand-anything`-Aenderungen bleiben unangetastet.
- Runout-Nachlauf muss pro Extruder zaehlen; globales `print_stats.filament_used` kann keine getrennten Pfade abbilden.

- Verifikation vor Sub-Agent: `pytest -q --tb=short`: 969 passed, 8 skipped; `git diff --check` ohne Befund.

- Claude Haiku 5.5 / low: eigener Lauf 969 passed, 8 skipped; keine must-fix Findings. Zwei should-fix Hinweise verifiziert und erledigt: State-Slot-Migration dokumentiert; EXTRUDER-Override fuer Sensor- und Legacy-LOAD parametrisiert getestet, AUTO-Zuordnung bleibt erhalten.
- Abschliessender eigener Lauf: `pytest -q --tb=short`: 971 passed, 8 skipped. Scoped `git diff --check` sauber. Keine weitere Review-Runde.
- Klipper-Quellenpruefung (2026-11-04, offizieller master): toolhead.py get_status Zeile 505 liefert extruder; extruder.py get_status Zeile 190 delegiert Heater-Status; heaters.py Zeile 149 liefert temperature; configfile.py Zeilen 437-439 liefern settings je Section. gcode_move.py cmd_G92 Zeilen 179-188 aendert base_position, nicht die physische Extruderposition; extruder.py find_past_position Zeilen 253-256 liest Stepper-Historie. Die beiden Review-Fragen sind damit keine bestaetigten Bugs. Kein Hardware-/Klipper-Sim-Nachweis.
- Quellen: https://github.com/Klipper3d/klipper/blob/master/klippy/toolhead.py ; https://github.com/Klipper3d/klipper/blob/master/klippy/kinematics/extruder.py ; https://github.com/Klipper3d/klipper/blob/master/klippy/extras/gcode_move.py ; https://github.com/Klipper3d/klipper/blob/master/klippy/extras/heaters.py ; https://github.com/Klipper3d/klipper/blob/master/klippy/configfile.py
