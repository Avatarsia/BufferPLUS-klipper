# LLL Buffer Refactor Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refactor `printer_data/config/lll.cfg` (Mellow LLL Filament Buffer Plus Klipper config) per spec `2026-04-21-lll-buffer-refactor-design.md`: Altlasten entfernen, Helper-Macros einfuehren, Mirror-Variables abschaffen, Magic Numbers in `_FILAMENT_VARS` ziehen. **Kein Verhaltens-Unterschied nach aussen.** Zusaetzlich: Knowledge-Base in `docs/knowledge/` aufbauen.

**Architecture:** Variante-2-Sync-Feedback bleibt (permanenter `SYNC_EXTRUDER_MOTION` + dynamische `rotation_distance`-Modulation). Alle State-Entscheidungen konzentriert in `_APPLY_SYNC_STATE`, das direkt die Button-Raw-States abfragt statt Mirror-Variables. Wiederkehrende Sequenzen in Helper-Macros extrahiert.

**Tech Stack:** Klipper Mainline (`Klipper3d/klipper`), Jinja2-Templates in `gcode_macro`-Definitionen, `SYNC_EXTRUDER_MOTION` / `SET_EXTRUDER_ROTATION_DISTANCE` / `FORCE_MOVE` / `filament_switch_sensor` / `gcode_button`. Keine Python-Extension, keine Kalico-/Happy-Hare-Dependencies.

**Validierungs-Modus:** Ohne Drucker vor Ort gibt es keinen Unit-Test-Runner. Ersatz: **Grep-basierte Vor-/Nach-Verifikation** + **Semantik-Aequivalenz-Doc** am Ende. Jeder Task beginnt mit Grep-Snapshot (Ist-Zustand dokumentieren), endet mit Grep-Snapshot (Soll-Zustand verifiziert).

**Parallelisierung:**
- **Knowledge-Base-Tasks (KB-1..KB-4)** sind voneinander unabhaengig und koennen parallel per Subagent ausgefuehrt werden.
- **Refactor-Tasks (R-1..R-14)** haben Reihenfolge-Abhaengigkeiten und werden sequenziell ausgefuehrt, jeweils mit Review zwischen den Tasks.
- **R-15 (Push)** erst ganz am Schluss, nach Drucker-Test-Approval durch User.

---

# Teil A — Knowledge-Base (parallel)

## Task KB-1: Hardware-Referenz LLL Buffer Plus

**Files:**
- Create: `docs/knowledge/01-hardware-lllbuffer-plus.md`

- [ ] **Step 1: Quellen sichten**

Mit WebFetch folgende URLs anfragen und Infos extrahieren:
- `https://github.com/Fly3DTeam/Buffer` — README des Upstream-C++-Firmware-Repos. Frage: "Liste alle Pin-Belegungen, Features, unterstuetzte MCU-Varianten, und die Hardware-Schaltung des Mellow LLL Buffer Plus."
- `https://github.com/Avatarsia/BufferPLUS-klipper/blob/main/README.md` — Fork-README. Frage: "Extrahiere alle Sensor-Pin-Zuordnungen, Button-Pins, und Installations-Voraussetzungen."

- [ ] **Step 2: Datei schreiben**

Erstelle `docs/knowledge/01-hardware-lllbuffer-plus.md` mit folgenden Sections:
```markdown
# Mellow LLL Filament Buffer Plus — Hardware-Referenz

## Uebersicht
- Produktname, Hersteller, Zweck
- MCU: STM32F072xB, 8 MHz crystal, USB auf PA11/PA12

## Pin-Belegung (von Original-Firmware)
| Pin | Funktion | Typ |
|---|---|---|
| PC13 | Stepper STEP | Output |
| PA7 | Stepper DIR | Output |
| PA6 | Stepper ENABLE (inverted) | Output |
| PB1 | TMC2208 UART | UART |
| PB2 | HALL1 (Ueberlast/Top) | Input (Pull-up, inverted) |
| PB3 | HALL2 (Voll/Mid) | Input (Pull-up, inverted) |
| PB4 | HALL3 (Leer/Bottom) | Input (Pull-up, inverted) |
| PB7 | ENDSTOP3 (Filament-Eingang) | Input (Pull-up, inverted) |
| PB12 | Feed-Button | Input (Pull-up, inverted) |
| PB13 | Retract-Button | Input (Pull-up, inverted) |
| PA8 | Status-LED | Output |

## Mechanik
- Stepper: Pancake mit Planetengetriebe, gear_ratio 50:17
- 3 Hall-Sensoren erfassen die Position des beweglichen Buffer-Halses
- Filament-Flussrichtung: Entrance -> HALL3 (unten) -> HALL2 (mitte) -> HALL1 (oben, Ueberlast)

## Sensor-Semantik
- ENDSTOP3 aktiv = Filament im Einlauf
- HALL3 aktiv = Buffer-Hals ganz unten (Buffer leer, muss nachfuellen)
- HALL2 aktiv = Buffer-Hals mittig oben (Buffer voll, Feed drosseln)
- HALL1 aktiv = Buffer-Hals ganz oben (Ueberlast, Notanschlag)

## Button-Verhalten
- Feed/Retract pull-up + inverted (wie Halls)
- Feed-Button gedrueckt = `state == "PRESSED"` im Klipper-gcode_button
```

- [ ] **Step 3: Inhalt mit WebFetch-Ergebnissen ergaenzen**

Fuege in jede obige Section die konkreten Info-Snippets aus den WebFetches ein (mit Quellen-Link in jeder Section-Ueberschrift als Fussnote).

- [ ] **Step 4: Verifikation**

```bash
wc -l docs/knowledge/01-hardware-lllbuffer-plus.md
```
Expected: mindestens 60 Zeilen.

```bash
grep -c "^##" docs/knowledge/01-hardware-lllbuffer-plus.md
```
Expected: mindestens 5 Sections.

- [ ] **Step 5: Commit**

```bash
git add docs/knowledge/01-hardware-lllbuffer-plus.md
git commit -m "docs(kb): add hardware reference for LLL Buffer Plus

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task KB-2: Klipper-APIs Referenz

**Files:**
- Create: `docs/knowledge/02-klipper-apis.md`

- [ ] **Step 1: Klipper-Source-Dateien lesen**

Mit WebFetch auf `raw.githubusercontent.com/Klipper3d/klipper/master/klippy/kinematics/extruder.py` die Registrierung von `SYNC_EXTRUDER_MOTION` und `SET_EXTRUDER_ROTATION_DISTANCE` suchen; mux-Key und Parameter dokumentieren.

Weitere Quellen:
- `https://www.klipper3d.org/Config_Reference.html#extruder_stepper`
- `https://www.klipper3d.org/Config_Reference.html#gcode_button`
- `https://www.klipper3d.org/Config_Reference.html#filament_switch_sensor`
- `https://www.klipper3d.org/G-Codes.html#force_move`
- `https://www.klipper3d.org/Config_Reference.html#force_move`
- `https://www.klipper3d.org/Config_Reference.html#delayed_gcode`

Frage an jede URL: "Extrahiere alle Parameter, Defaults, und die exakte Syntax der G-Code-Kommandos. Liste Praezedenzfaelle und Kompatibilitaetshinweise."

- [ ] **Step 2: Datei schreiben**

Erstelle `docs/knowledge/02-klipper-apis.md` mit folgenden Sections:

```markdown
# Klipper-APIs fuer die LLL Buffer Config

## extruder_stepper
- Config-Keys, Defaults, Beispiel
- Verhalten gegenueber `[extruder]`

## SYNC_EXTRUDER_MOTION
- Exakte Syntax (Mainline Klipper Mux-Key: `EXTRUDER=`)
- Parameter: `EXTRUDER=<stepper-name>`, `MOTION_QUEUE=<extruder-name oder leer>`
- Semantik: MOTION_QUEUE leer = Sync aus, MOTION_QUEUE=extruder = Sync an
- Kalico-Abweichung: dort `STEPPER=` als Mux-Key

## SET_EXTRUDER_ROTATION_DISTANCE
- Syntax: `SET_EXTRUDER_ROTATION_DISTANCE EXTRUDER=<name> DISTANCE=<float>`
- Verhalten bei synchronisierten Steppern
- Persistenz (nicht persistent, nach Klipper-Restart zurueck)

## FORCE_MOVE
- Syntax: `FORCE_MOVE STEPPER=<name> DISTANCE=<float> VELOCITY=<mm/s> ACCEL=<mm/s^2>`
- Voraussetzung: `[force_move] enable_force_move: True`
- Nebenwirkung: `toolhead.flush_step_generation()` -> kann Druckkopf pausieren
- Darum: wenn Stepper synced ist, vorher Sync trennen

## gcode_button
- `pin:` mit `^!` fuer pullup+invert
- `press_gcode:`, `release_gcode:`
- `.state`-Property: `"PRESSED"` / `"RELEASED"`
- **Wichtig bei `^!`:** physisch aktiv -> `state == "RELEASED"` (invertiert)

## filament_switch_sensor
- `switch_pin:`, `pause_on_runout:`, `insert_gcode:`, `runout_gcode:`
- `.filament_detected`-Property: bool

## delayed_gcode
- `initial_duration:` (einmalig beim Boot)
- `UPDATE_DELAYED_GCODE ID=<name> DURATION=<seconds>` (0 = Abbruch)
- Ketten per Self-Call moeglich (Loop)

## gcode_macro
- `variable_<name>:` Praefix fuer persistente (innerhalb einer Klipper-Session) Variablen
- `SET_GCODE_VARIABLE MACRO=<macro> VARIABLE=<name> VALUE=<value>`
- `{params.DIRECTION}` in Body = Parameter-Zugriff
- Jinja2-Templates: `{% if %}`, `{% for %}`, `{% set %}`
```

- [ ] **Step 3: Quellzitate ergaenzen**

Bei jeder API den Link zur Original-Doc oder dem Source-Code in einer Fussnote. Besonders wichtig: exakte Registrierungs-Zeile aus `klippy/kinematics/extruder.py` fuer den mux-Key.

- [ ] **Step 4: Verifikation**

```bash
grep -c "SYNC_EXTRUDER_MOTION" docs/knowledge/02-klipper-apis.md
```
Expected: mindestens 3 (einmal Heading, einmal Syntax, einmal Kalico-Vergleich).

```bash
grep -c "EXTRUDER=" docs/knowledge/02-klipper-apis.md
```
Expected: mindestens 2.

- [ ] **Step 5: Commit**

```bash
git add docs/knowledge/02-klipper-apis.md
git commit -m "docs(kb): add Klipper API reference for buffer config

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task KB-3: Firmware-Upstream + Architektur-Vergleich

**Files:**
- Create: `docs/knowledge/03-architektur-vergleich.md`

- [ ] **Step 1: Alt-Config und Neu-Config diffen**

```bash
diff -u printer_data/config/mellow-plus.cfg printer_data/config/lll.cfg | head -100
```

Strukturelle Unterschiede rausarbeiten.

- [ ] **Step 2: Upstream C++-Firmware verstehen**

WebFetch `https://github.com/Fly3DTeam/Buffer` README + Source-Dateien-Liste. Frage: "Wie steuert die Original-C++-Firmware den Feeder-Stepper? Wie reagiert sie auf Hall-Sensoren? Laeuft sie permanent oder nur auf Events?"

- [ ] **Step 3: Datei schreiben**

Erstelle `docs/knowledge/03-architektur-vergleich.md` mit folgenden Sections:

```markdown
# Buffer-Steuerungs-Architekturen — Vergleich

## Variante 0: Original C++-Firmware (Fly3DTeam/Buffer)
- Autonom auf STM32, Klipper weiss nichts davon
- Feedback-Loop direkt auf dem MCU, keine Host-Rundreise
- Vorteil: Latenz, Host-unabhaengig
- Nachteil: keine Sync-Kopplung, Klipper kann Flow nicht beeinflussen

## Variante 1: Klipper-Config mit FORCE_MOVE-Bursts (mellow-plus.cfg)
- Host-basiert: gcode_button-Events triggern delayed_gcode-Loops mit FORCE_MOVE
- Jeder Burst pausiert den Druckkopf (toolhead flush)
- Vorteil: einfach, gut verstaendlich
- Nachteil: **Pause-Artefakte waehrend des Drucks**

## Variante 2: Sync-Feedback ueber rotation_distance (lll.cfg, aktuell)
- Feeder-Stepper permanent per SYNC_EXTRUDER_MOTION gekoppelt
- Hall-Events aendern nur `rotation_distance` via SET_EXTRUDER_ROTATION_DISTANCE
- Vorteil: **druckpausenfrei** waehrend Regelbetrieb
- Nachteil: FORCE_MOVE nur noch fuer Phasen ausserhalb Druck erlaubt
- Nachteil: komplexere State-Machine (Lockout-Flags, Initial-Grip/Follow)

## Vergleichstabelle
| Kriterium | Variante 0 | Variante 1 | Variante 2 |
|---|---|---|---|
| Druck-Pausen | - | ja | nein |
| Host-Abhaengig | nein | ja | ja |
| Sync zum Extruder | nein | nein | ja |
| Reaktionszeit | niedrig | mittel | mittel |
| Anpassbarkeit via Klipper | keine | hoch | hoch |

## Happy-Hare als Referenz
- Happy-Hare ist ein MMU-System, nutzt auch SYNC_EXTRUDER_MOTION
- Unser Design ist inspiriert von HH, aber wir brauchen keine Tool-Wechsel-Logik
- Unterschiede und Nicht-Ziele kurz beschreiben
```

- [ ] **Step 4: Verifikation**

```bash
grep -c "Variante" docs/knowledge/03-architektur-vergleich.md
```
Expected: mindestens 6 Erwaehnungen.

- [ ] **Step 5: Commit**

```bash
git add docs/knowledge/03-architektur-vergleich.md
git commit -m "docs(kb): add buffer architecture comparison

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task KB-4: Katapult-Flashing + STM32F072-Pinout

**Files:**
- Create: `docs/knowledge/04-flashing-katapult.md`

- [ ] **Step 1: Quellen-Recherche**

WebFetch:
- `https://github.com/Arksine/katapult` — Katapult-README. Frage: "Welche Optionen fuer STM32F072, welche Bootloader-Offsets, welche Flash-Kommandos?"
- `https://www.st.com/resource/en/datasheet/stm32f072c8.pdf` (nur Titel-Seite ueberschlaegig). Frage: "Welche Flash-Groesse, welche USB-DFU-Funktion?"

- [ ] **Step 2: Bestehende README.md des Forks extrahieren**

```bash
head -200 README.md > /tmp/readme-excerpt.txt
```

Die Katapult-Install-Schritte daraus in die neue Datei uebernehmen und ergaenzen/pruefen.

- [ ] **Step 3: Datei schreiben**

Erstelle `docs/knowledge/04-flashing-katapult.md` mit folgenden Sections:

```markdown
# Katapult Bootloader + Klipper Flash fuer LLL Buffer Plus

## STM32F072 MCU Grundlagen
- Flash: 64 KB (F072C8) oder 128 KB (F072CB)
- USB-DFU via PA11/PA12, 8 MHz Crystal extern
- Application Start Offset mit Katapult: 8 KiB

## Katapult bauen + flashen
(aus README.md uebernehmen, pruefen und ggf. modernisieren)

## Klipper bauen + flashen
(analog)

## DFU-Mode Triggers
- BOOT0 auf 3.3V jumpern + Reset
- Oder Double-Click Reset (wenn Katapult-Option aktiv)

## Verifikation
```bash
ls /dev/serial/by-id/
```
Nach Katapult-Flash: `usb-katapult_stm32f072xb_XXXX-if00`
Nach Klipper-Flash: `usb-Klipper_stm32f072xb_XXXX-if00`

## Troubleshooting
- DFU nicht erkannt: USB-Kabel (Datenkabel, nicht Ladekabel!), lsusb, dfu-util -l
- Flash schlaegt fehl: Bootloader-Offset muss identisch sein (8KiB bei Katapult-Build und Klipper-Build)
```

- [ ] **Step 4: Verifikation**

```bash
grep -c "STM32F072" docs/knowledge/04-flashing-katapult.md
```
Expected: mindestens 3.

- [ ] **Step 5: Commit**

```bash
git add docs/knowledge/04-flashing-katapult.md
git commit -m "docs(kb): add Katapult flashing + STM32F072 reference

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

# Teil B — Refactor (sequenziell)

## Task R-1: _FILAMENT_VARS erweitern um neue Variablen

**Files:**
- Modify: `printer_data/config/lll.cfg` (Section 557-612)

- [ ] **Step 1: Vor-Zustand-Snapshot**

```bash
grep -n "variable_" printer_data/config/lll.cfg | head -40
```
Soll zeigen: aktuelle 21 Variablen in `_FILAMENT_VARS` (von `sync_rotation_distance` bis `triple_click_window`).

- [ ] **Step 2: Neue Variablen hinzufuegen**

In `printer_data/config/lll.cfg`, im `[gcode_macro _FILAMENT_VARS]`-Block, direkt nach `variable_triple_click_window: 0.8` und vor dem `gcode:`-Block:

Old (Zeilen 606-610):
```
# === Triple-Click-Burst ===========================================
variable_triple_click_distance: 500
variable_triple_click_window:   0.8

gcode:
    # Reine Variablen-Container-Macro
```

New:
```
# === Triple-Click-Burst ===========================================
variable_triple_click_distance: 500
variable_triple_click_window:   0.8

# === Manuelle Taster-/Loop-Parameter ==============================
# DISTANCE fuer Manual-Feed/Retract-Loops und _UNLOAD_FAST_RETRACT
variable_manual_chunk_distance: 10
# VELOCITY fuer Manual-Taster (Feed + Retract vereinheitlicht)
variable_manual_speed:          15
# ACCEL fuer alle FORCE_MOVE-Aufrufe
variable_force_move_accel:    1000
# Tick-Rate des Manual-Loops [s]
variable_manual_loop_tick:     0.1
# Cooldown nach manuellem Taster-Release bis Sync wieder aktiv [s]
variable_reenable_cooldown:      1
# Kuerzerer Cooldown nach Triple-Click-Burst [s]
variable_reenable_cooldown_fast: 0.5

gcode:
    # Reine Variablen-Container-Macro
```

- [ ] **Step 3: Verifikation**

```bash
grep -c "variable_" printer_data/config/lll.cfg
```
Expected: alte Anzahl + 6 = **27** (von vorher 21 war: 2 in _MANUAL_*, 2 in _TRIPLE_CLICK_STATE, 6 in _BUFFER_AUTO_CONTROL, 3 in _BUFFER_AUTO_CONTROL hallN, 1 in LOAD, 3 in UNLOAD, 14 in _FILAMENT_VARS → zusammen 31 tatsaechlich. Der Check ist nur Grobschlag — wichtig ist dass die 6 neuen Namen da sind).

```bash
for v in manual_chunk_distance manual_speed force_move_accel manual_loop_tick reenable_cooldown reenable_cooldown_fast; do
  grep -c "variable_$v" printer_data/config/lll.cfg
done
```
Expected: jede Zeile gibt `1` aus.

- [ ] **Step 4: Commit**

```bash
git add printer_data/config/lll.cfg
git commit -m "refactor(lll.cfg): add 6 new variables to _FILAMENT_VARS

- manual_chunk_distance (10)
- manual_speed (15)
- force_move_accel (1000)
- manual_loop_tick (0.1)
- reenable_cooldown (1)
- reenable_cooldown_fast (0.5)

Magic numbers aus dem Code werden noch nicht referenziert (spaetere Tasks).
Spec: 2026-04-21-lll-buffer-refactor-design.md §8

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task R-2: Helper _PREPARE_INITIAL_FILL + _ABORT_ALL_FEED_LOOPS anlegen

**Files:**
- Modify: `printer_data/config/lll.cfg` (neu: Helper-Block)

- [ ] **Step 1: Vor-Zustand-Snapshot**

```bash
grep -n "_PREPARE_INITIAL_FILL\|_ABORT_ALL_FEED_LOOPS" printer_data/config/lll.cfg
```
Expected: keine Treffer (Helper existieren noch nicht).

- [ ] **Step 2: Helper-Macros hinzufuegen**

Nach dem `_reenable_autofeed`-delayed_gcode (Zeilen 394-398 im Original), Einfuegen:

```
# Konsolidiert die Init-Sequenz aus buffer_entrance.insert_gcode
# und FORCE_BUFFER_FILL. Startet Initial-Grip + Follow.
[gcode_macro _PREPARE_INITIAL_FILL]
gcode:
    SET_GCODE_VARIABLE MACRO=_BUFFER_AUTO_CONTROL VARIABLE=system_enabled VALUE=1
    SET_GCODE_VARIABLE MACRO=_BUFFER_AUTO_CONTROL VARIABLE=manual_operation VALUE=0
    SET_GCODE_VARIABLE MACRO=_BUFFER_AUTO_CONTROL VARIABLE=overfill_lock VALUE=0
    SET_GCODE_VARIABLE MACRO=_BUFFER_AUTO_CONTROL VARIABLE=initial_lockout VALUE=1
    _APPLY_SYNC_STATE
    UPDATE_DELAYED_GCODE ID=_start_initial_grip DURATION=0.1

# Stoppt alle aktiven Feed-Loops (Manual, Initial-Follow) und
# raeumt die zugehoerigen Flags auf. Caller muss danach selbst
# _APPLY_SYNC_STATE rufen, weil die Post-Action unterschiedlich ist.
[gcode_macro _ABORT_ALL_FEED_LOOPS]
gcode:
    SET_GCODE_VARIABLE MACRO=_BUFFER_AUTO_CONTROL VARIABLE=initial_lockout VALUE=0
    SET_GCODE_VARIABLE MACRO=_BUFFER_AUTO_CONTROL VARIABLE=initial_follow_active VALUE=0
    SET_GCODE_VARIABLE MACRO=_MANUAL_FEED VARIABLE=active VALUE=0
    SET_GCODE_VARIABLE MACRO=_MANUAL_RETRACT VARIABLE=active VALUE=0
    UPDATE_DELAYED_GCODE ID=_manual_feed_loop DURATION=0
    UPDATE_DELAYED_GCODE ID=_manual_retract_loop DURATION=0
    UPDATE_DELAYED_GCODE ID=_initial_follow_loop DURATION=0
    UPDATE_DELAYED_GCODE ID=_initial_follow_end DURATION=0
```

- [ ] **Step 3: Verifikation**

```bash
grep -c "^\[gcode_macro _PREPARE_INITIAL_FILL\]" printer_data/config/lll.cfg
```
Expected: `1`.

```bash
grep -c "^\[gcode_macro _ABORT_ALL_FEED_LOOPS\]" printer_data/config/lll.cfg
```
Expected: `1`.

Die Helper werden in dieser Task **definiert aber noch nicht aufgerufen** (Caller kommen in R-5 + R-9).

- [ ] **Step 4: Commit**

```bash
git add printer_data/config/lll.cfg
git commit -m "refactor(lll.cfg): add _PREPARE_INITIAL_FILL and _ABORT_ALL_FEED_LOOPS helpers

Helper-Macros angelegt, Caller werden spaeter umgestellt.
Spec: 2026-04-21-lll-buffer-refactor-design.md §6.1-6.2

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task R-3: Helper _SAVE_E_MODE + _RESTORE_E_MODE + saved_e_abs in State-Container

**Files:**
- Modify: `printer_data/config/lll.cfg` (State-Container + neuer Helper-Block)

- [ ] **Step 1: Vor-Zustand-Snapshot**

```bash
grep -n "variable_saved_e_abs\|_SAVE_E_MODE\|_RESTORE_E_MODE" printer_data/config/lll.cfg
```
Expected: keine Treffer.

- [ ] **Step 2: saved_e_abs zu _BUFFER_AUTO_CONTROL hinzufuegen**

Im `[gcode_macro _BUFFER_AUTO_CONTROL]`-Block (Zeilen 334-347), nach `variable_hall3_active: 0` und vor `gcode:`:

Old:
```
variable_hall3_active: 0            # 1 = Leer
gcode:
    _APPLY_SYNC_STATE
```

New:
```
variable_hall3_active: 0            # 1 = Leer
# E-Modus-Buchhaltung fuer LOAD/UNLOAD (von _SAVE_E_MODE gesetzt)
variable_saved_e_abs: 0
gcode:
    _APPLY_SYNC_STATE
```

(Der gcode-Body-Refactor auf "leer" kommt erst in R-11 zusammen mit dem Mirror-Var-Cleanup.)

- [ ] **Step 3: Helper-Macros anlegen**

Direkt nach den Helpers aus R-2 einfuegen:

```
# Speichert den aktuellen E-Absolut/Relativ-Modus und schaltet auf M83.
# Paarweise mit _RESTORE_E_MODE zu verwenden.
[gcode_macro _SAVE_E_MODE]
gcode:
    SET_GCODE_VARIABLE MACRO=_BUFFER_AUTO_CONTROL VARIABLE=saved_e_abs VALUE={1 if printer.gcode_move.absolute_extrude else 0}
    M83

# Stellt den von _SAVE_E_MODE gespeicherten E-Modus wieder her.
[gcode_macro _RESTORE_E_MODE]
gcode:
    {% if printer["gcode_macro _BUFFER_AUTO_CONTROL"].saved_e_abs %}
        M82
    {% else %}
        M83
    {% endif %}
```

- [ ] **Step 4: Verifikation**

```bash
grep -c "variable_saved_e_abs" printer_data/config/lll.cfg
```
Expected: `1`.

```bash
grep -c "^\[gcode_macro _SAVE_E_MODE\]\|^\[gcode_macro _RESTORE_E_MODE\]" printer_data/config/lll.cfg
```
Expected: `2`.

- [ ] **Step 5: Commit**

```bash
git add printer_data/config/lll.cfg
git commit -m "refactor(lll.cfg): add _SAVE_E_MODE / _RESTORE_E_MODE helpers

Zentrale E-Absolut/Relativ-Buchhaltung via saved_e_abs in
_BUFFER_AUTO_CONTROL. LOAD/UNLOAD werden spaeter umgestellt.
Spec: 2026-04-21-lll-buffer-refactor-design.md §6.3

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task R-4: _APPLY_SYNC_STATE auf Raw-Button-State umstellen

**Files:**
- Modify: `printer_data/config/lll.cfg` (Zeilen ~362-384)

- [ ] **Step 1: Vor-Zustand-Snapshot**

```bash
grep -n "hall[123]_active" printer_data/config/lll.cfg
```
Dokumentieren welche Stellen Mirror-Vars lesen (in _APPLY_SYNC_STATE) und welche sie setzen (in Button-Handlern).

- [ ] **Step 2: _APPLY_SYNC_STATE umschreiben**

Den kompletten `[gcode_macro _APPLY_SYNC_STATE]`-Block ersetzen. **Wichtig: Mirror-Vars bleiben im State-Container erstmal — sie werden in R-11 entfernt. Aber _APPLY_SYNC_STATE liest sie ab jetzt nicht mehr.**

Old (Zeilen 362-384):
```
[gcode_macro _APPLY_SYNC_STATE]
gcode:
    {% set v = printer["gcode_macro _FILAMENT_VARS"] %}
    {% set b = printer["gcode_macro _BUFFER_AUTO_CONTROL"] %}
    {% set nominal = v.sync_rotation_distance | float %}
    {% set mod     = v.sync_modulation | float %}
    {% set fast_rd = (nominal / (1.0 + mod)) | round(4) %}
    {% set slow_rd = (nominal / (1.0 - mod)) | round(4) %}

    {% if b.system_enabled == 0 or b.manual_operation == 1 or b.initial_lockout == 1 or b.overfill_lock == 1 %}
        # Sync aus - Feeder folgt nicht mehr dem Extruder
        SYNC_EXTRUDER_MOTION EXTRUDER=mellow MOTION_QUEUE=
    {% else %}
        # Sync an gegen Hauptextruder
        SYNC_EXTRUDER_MOTION EXTRUDER=mellow MOTION_QUEUE=extruder
        {% if b.hall2_active == 1 %}
            SET_EXTRUDER_ROTATION_DISTANCE EXTRUDER=mellow DISTANCE={slow_rd}
        {% elif b.hall3_active == 1 %}
            SET_EXTRUDER_ROTATION_DISTANCE EXTRUDER=mellow DISTANCE={fast_rd}
        {% else %}
            SET_EXTRUDER_ROTATION_DISTANCE EXTRUDER=mellow DISTANCE={nominal}
        {% endif %}
    {% endif %}
```

New:
```
# Zentrale Sync-/Rotation-Distance-Entscheidung. Liest Hall-States
# direkt aus der Klipper-Eventqueue (Raw-Button-State), nicht mehr
# ueber Mirror-Variablen.
#
# WICHTIG: Die Hall-Button-Pins sind mit `^!` konfiguriert (pullup +
# invert). Daraus folgt:
#     Sensor physisch AKTIV   ->   press event ausgeloest?  nein
#                             ->   state == "RELEASED"
#     Sensor physisch FREI    ->   state == "PRESSED"
# Das ist gegenintuitiv, aber eine Eigenschaft der Hardware-
# Invertierung. Darum unten: "RELEASED == Sensor aktiv".
[gcode_macro _APPLY_SYNC_STATE]
gcode:
    {% set v = printer["gcode_macro _FILAMENT_VARS"] %}
    {% set b = printer["gcode_macro _BUFFER_AUTO_CONTROL"] %}
    {% set hall1_active = printer["gcode_button buffer_hall1"].state == "RELEASED" %}
    {% set hall2_active = printer["gcode_button buffer_hall2"].state == "RELEASED" %}
    {% set hall3_active = printer["gcode_button buffer_hall3"].state == "RELEASED" %}
    {% set nominal = v.sync_rotation_distance | float %}
    {% set mod     = v.sync_modulation | float %}
    {% set fast_rd = (nominal / (1.0 + mod)) | round(4) %}
    {% set slow_rd = (nominal / (1.0 - mod)) | round(4) %}

    {% if b.system_enabled == 0 or b.manual_operation == 1 or b.initial_lockout == 1 or b.overfill_lock == 1 %}
        # Sync aus - Feeder folgt nicht mehr dem Extruder
        SYNC_EXTRUDER_MOTION EXTRUDER=mellow MOTION_QUEUE=
    {% else %}
        # Sync an gegen Hauptextruder
        SYNC_EXTRUDER_MOTION EXTRUDER=mellow MOTION_QUEUE=extruder
        {% if hall2_active %}
            SET_EXTRUDER_ROTATION_DISTANCE EXTRUDER=mellow DISTANCE={slow_rd}
        {% elif hall3_active %}
            SET_EXTRUDER_ROTATION_DISTANCE EXTRUDER=mellow DISTANCE={fast_rd}
        {% else %}
            SET_EXTRUDER_ROTATION_DISTANCE EXTRUDER=mellow DISTANCE={nominal}
        {% endif %}
    {% endif %}
```

- [ ] **Step 3: Verifikation**

```bash
grep -n "b.hall[123]_active" printer_data/config/lll.cfg
```
Expected: **keine Treffer mehr** in `_APPLY_SYNC_STATE` (nur die verbleibenden Mirror-SETs in den Hall-Handlern zeigen auf `hall*_active`, aber ohne `b.`-Prefix).

```bash
grep -c "printer\[.gcode_button buffer_hall[123].\].state" printer_data/config/lll.cfg
```
Expected: mindestens `3` (die drei neuen Lesestellen in `_APPLY_SYNC_STATE`).

- [ ] **Step 4: Commit**

```bash
git add printer_data/config/lll.cfg
git commit -m "refactor(lll.cfg): _APPLY_SYNC_STATE reads raw button state

Mirror-Variablen werden nicht mehr gelesen. Cleanup der
hallN_active-Vars und der Button-Handler-SETs kommt in R-11.
Spec: 2026-04-21-lll-buffer-refactor-design.md §6.5

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task R-5: _PREPARE_INITIAL_FILL + _ABORT_ALL_FEED_LOOPS in Callern einsetzen

**Files:**
- Modify: `printer_data/config/lll.cfg` (buffer_entrance.insert_gcode, FORCE_BUFFER_FILL, STOP_BUFFER_FILL, buffer_hall1.release_gcode)

- [ ] **Step 1: Vor-Zustand-Snapshot**

```bash
grep -n "UPDATE_DELAYED_GCODE ID=_start_initial_grip" printer_data/config/lll.cfg
```
Expected: 2 Treffer (insert_gcode, FORCE_BUFFER_FILL).

- [ ] **Step 2: buffer_entrance.insert_gcode auf _PREPARE_INITIAL_FILL umstellen**

Old (Zeilen ~240-246):
```
insert_gcode:
    M118 Buffer: Filament erkannt - Grip-Phase startet
    SET_GCODE_VARIABLE MACRO=_BUFFER_AUTO_CONTROL VARIABLE=system_enabled VALUE=1
    SET_GCODE_VARIABLE MACRO=_BUFFER_AUTO_CONTROL VARIABLE=manual_operation VALUE=0
    SET_GCODE_VARIABLE MACRO=_BUFFER_AUTO_CONTROL VARIABLE=overfill_lock VALUE=0
    SET_GCODE_VARIABLE MACRO=_BUFFER_AUTO_CONTROL VARIABLE=initial_lockout VALUE=1
    UPDATE_DELAYED_GCODE ID=_start_initial_grip DURATION=0.1
```

New:
```
insert_gcode:
    M118 Buffer: Filament erkannt - Grip-Phase startet
    _PREPARE_INITIAL_FILL
```

- [ ] **Step 3: FORCE_BUFFER_FILL auf _PREPARE_INITIAL_FILL umstellen**

Old (Zeilen ~444-450):
```
    {% if printer["filament_switch_sensor buffer_entrance"].filament_detected %}
        M118 FORCE_BUFFER_FILL: Erstbefuellung manuell gestartet (Grip + Follow)
        SET_GCODE_VARIABLE MACRO=_BUFFER_AUTO_CONTROL VARIABLE=system_enabled VALUE=1
        SET_GCODE_VARIABLE MACRO=_BUFFER_AUTO_CONTROL VARIABLE=manual_operation VALUE=0
        SET_GCODE_VARIABLE MACRO=_BUFFER_AUTO_CONTROL VARIABLE=overfill_lock VALUE=0
        SET_GCODE_VARIABLE MACRO=_BUFFER_AUTO_CONTROL VARIABLE=initial_lockout VALUE=1
        _APPLY_SYNC_STATE
        UPDATE_DELAYED_GCODE ID=_start_initial_grip DURATION=0.1
    {% else %}
```

New:
```
    {% if printer["filament_switch_sensor buffer_entrance"].filament_detected %}
        M118 FORCE_BUFFER_FILL: Erstbefuellung manuell gestartet (Grip + Follow)
        _PREPARE_INITIAL_FILL
    {% else %}
```

- [ ] **Step 4: STOP_BUFFER_FILL auf _ABORT_ALL_FEED_LOOPS umstellen**

Old (Zeilen ~482-492):
```
gcode:
    M118 STOP_BUFFER_FILL: Alle Foerder-Loops gestoppt
    SET_GCODE_VARIABLE MACRO=_BUFFER_AUTO_CONTROL VARIABLE=manual_operation VALUE=1
    SET_GCODE_VARIABLE MACRO=_BUFFER_AUTO_CONTROL VARIABLE=initial_lockout VALUE=0
    SET_GCODE_VARIABLE MACRO=_BUFFER_AUTO_CONTROL VARIABLE=initial_follow_active VALUE=0
    SET_GCODE_VARIABLE MACRO=_MANUAL_FEED VARIABLE=active VALUE=0
    SET_GCODE_VARIABLE MACRO=_MANUAL_RETRACT VARIABLE=active VALUE=0
    UPDATE_DELAYED_GCODE ID=_manual_feed_loop DURATION=0
    UPDATE_DELAYED_GCODE ID=_manual_retract_loop DURATION=0
    UPDATE_DELAYED_GCODE ID=_initial_follow_loop DURATION=0
    UPDATE_DELAYED_GCODE ID=_initial_follow_end DURATION=0
    _APPLY_SYNC_STATE
```

New:
```
gcode:
    M118 STOP_BUFFER_FILL: Alle Foerder-Loops gestoppt
    SET_GCODE_VARIABLE MACRO=_BUFFER_AUTO_CONTROL VARIABLE=manual_operation VALUE=1
    _ABORT_ALL_FEED_LOOPS
    _APPLY_SYNC_STATE
```

- [ ] **Step 5: buffer_hall1.release_gcode auf _ABORT_ALL_FEED_LOOPS umstellen**

Old (Zeilen ~317-328):
```
release_gcode:
    SET_GCODE_VARIABLE MACRO=_BUFFER_AUTO_CONTROL VARIABLE=hall1_active VALUE=1
    M118 HALL1 AKTIV - UEBERLAST-NOTANSCHLAG! Feeder entkoppelt+disabled
    SET_GCODE_VARIABLE MACRO=_BUFFER_AUTO_CONTROL VARIABLE=overfill_lock VALUE=1
    # Initial-Phase sofort abbrechen falls aktiv (TPU-Stau-Schutz)
    SET_GCODE_VARIABLE MACRO=_BUFFER_AUTO_CONTROL VARIABLE=initial_lockout VALUE=0
    SET_GCODE_VARIABLE MACRO=_BUFFER_AUTO_CONTROL VARIABLE=initial_follow_active VALUE=0
    UPDATE_DELAYED_GCODE ID=_initial_follow_loop DURATION=0
    UPDATE_DELAYED_GCODE ID=_initial_follow_end DURATION=0
    SET_GCODE_VARIABLE MACRO=_MANUAL_FEED VARIABLE=active VALUE=0
    UPDATE_DELAYED_GCODE ID=_manual_feed_loop DURATION=0
    _APPLY_SYNC_STATE
```

New (beachte: `SET hall1_active` bleibt erstmal drin, wird in R-11 entfernt):
```
release_gcode:
    SET_GCODE_VARIABLE MACRO=_BUFFER_AUTO_CONTROL VARIABLE=hall1_active VALUE=1
    M118 HALL1 AKTIV - UEBERLAST-NOTANSCHLAG! Feeder entkoppelt+disabled
    SET_GCODE_VARIABLE MACRO=_BUFFER_AUTO_CONTROL VARIABLE=overfill_lock VALUE=1
    _ABORT_ALL_FEED_LOOPS
    _APPLY_SYNC_STATE
```

- [ ] **Step 6: Verifikation**

```bash
grep -c "_PREPARE_INITIAL_FILL" printer_data/config/lll.cfg
```
Expected: `3` (1 Definition + 2 Caller).

```bash
grep -c "_ABORT_ALL_FEED_LOOPS" printer_data/config/lll.cfg
```
Expected: `3` (1 Definition + 2 Caller).

```bash
grep -c "UPDATE_DELAYED_GCODE ID=_start_initial_grip" printer_data/config/lll.cfg
```
Expected: `1` (nur noch in `_PREPARE_INITIAL_FILL`; die beiden alten Stellen sind weg).

- [ ] **Step 7: Commit**

```bash
git add printer_data/config/lll.cfg
git commit -m "refactor(lll.cfg): wire _PREPARE_INITIAL_FILL + _ABORT_ALL_FEED_LOOPS

Caller umgestellt: insert_gcode, FORCE_BUFFER_FILL,
STOP_BUFFER_FILL, HALL1.release_gcode. ~30 Zeilen Redundanz weg.
Spec: 2026-04-21-lll-buffer-refactor-design.md §6.1-6.2

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task R-6: _BUTTON_CLICK_HANDLER (parametriert) anlegen

**Files:**
- Modify: `printer_data/config/lll.cfg` (neuer Helper)

- [ ] **Step 1: Vor-Zustand-Snapshot**

```bash
grep -n "_BUTTON_CLICK_HANDLER" printer_data/config/lll.cfg
```
Expected: keine Treffer.

- [ ] **Step 2: Handler-Macro anlegen**

Nach den Helpern aus R-2/R-3 einfuegen (unter `_RESTORE_E_MODE`):

```
# Deduplifizierter Feed-/Retract-Button-Handler. Wird aus den
# [gcode_button feed_button] / [gcode_button retract_button]
# press_gcode-Bloecken aufgerufen mit DIRECTION=FEED oder =RETRACT.
# Klick-Semantik: 1x = Dauerlauf, 2x = Chunk-Puls, 3x = Triple-Burst.
[gcode_macro _BUTTON_CLICK_HANDLER]
gcode:
    {% set dir = params.DIRECTION|default('FEED')|upper %}
    {% set v = printer["gcode_macro _FILAMENT_VARS"] %}
    {% set t = printer["gcode_macro _TRIPLE_CLICK_STATE"] %}

    {% if dir == 'FEED' %}
        {% set new_count = (t.feed_count | int) + 1 %}
        {% set count_var = 'feed_count' %}
        {% set reset_id  = '_triple_feed_reset' %}
        {% set loop_id   = '_manual_feed_loop' %}
        {% set burst_macro = '_TRIPLE_FEED_BURST' %}
        {% set manual_macro = '_MANUAL_FEED' %}
        {% set puls_sign = 1 %}
        {% set word = 'Vorschub' %}
    {% else %}
        {% set new_count = (t.retract_count | int) + 1 %}
        {% set count_var = 'retract_count' %}
        {% set reset_id  = '_triple_retract_reset' %}
        {% set loop_id   = '_manual_retract_loop' %}
        {% set burst_macro = '_TRIPLE_RETRACT_BURST' %}
        {% set manual_macro = '_MANUAL_RETRACT' %}
        {% set puls_sign = -1 %}
        {% set word = 'Rueckzug' %}
    {% endif %}

    {% if new_count >= 3 %}
        SET_GCODE_VARIABLE MACRO=_TRIPLE_CLICK_STATE VARIABLE={count_var} VALUE=0
        UPDATE_DELAYED_GCODE ID={reset_id} DURATION=0
        {burst_macro}
    {% elif new_count == 2 %}
        M118 {word}-Taster (2. Klick) - {v.manual_chunk_distance} mm Puls
        SET_GCODE_VARIABLE MACRO={manual_macro} VARIABLE=active VALUE=0
        UPDATE_DELAYED_GCODE ID={loop_id} DURATION=0
        SET_GCODE_VARIABLE MACRO=_BUFFER_AUTO_CONTROL VARIABLE=manual_operation VALUE=1
        _SYNC_OFF
        FORCE_MOVE STEPPER="extruder_stepper mellow" DISTANCE={puls_sign * v.manual_chunk_distance} VELOCITY={v.manual_speed} ACCEL={v.force_move_accel}
        SET_GCODE_VARIABLE MACRO=_TRIPLE_CLICK_STATE VARIABLE={count_var} VALUE={new_count}
        UPDATE_DELAYED_GCODE ID={reset_id} DURATION={v.triple_click_window}
        UPDATE_DELAYED_GCODE ID=_reenable_autofeed DURATION={v.reenable_cooldown}
    {% else %}
        M118 {word}-Taster - Dauerlauf startet
        SET_GCODE_VARIABLE MACRO=_BUFFER_AUTO_CONTROL VARIABLE=manual_operation VALUE=1
        _SYNC_OFF
        SET_GCODE_VARIABLE MACRO={manual_macro} VARIABLE=active VALUE=1
        {manual_macro}
        SET_GCODE_VARIABLE MACRO=_TRIPLE_CLICK_STATE VARIABLE={count_var} VALUE={new_count}
        UPDATE_DELAYED_GCODE ID={reset_id} DURATION={v.triple_click_window}
    {% endif %}
```

- [ ] **Step 3: Verifikation**

```bash
grep -c "^\[gcode_macro _BUTTON_CLICK_HANDLER\]" printer_data/config/lll.cfg
```
Expected: `1`.

```bash
grep -c "params.DIRECTION" printer_data/config/lll.cfg
```
Expected: `1`.

- [ ] **Step 4: Commit**

```bash
git add printer_data/config/lll.cfg
git commit -m "refactor(lll.cfg): add parametrized _BUTTON_CLICK_HANDLER

Die Handler-Logik wird noch nicht verdrahtet; feed_button /
retract_button bleiben erstmal unveraendert. Anschluss in R-7.
Spec: 2026-04-21-lll-buffer-refactor-design.md §6.4

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task R-7: feed_button / retract_button auf Handler umstellen

**Files:**
- Modify: `printer_data/config/lll.cfg` (Zeilen ~87-153)

- [ ] **Step 1: Vor-Zustand-Snapshot**

```bash
wc -l printer_data/config/lll.cfg
```
Notieren.

- [ ] **Step 2: feed_button press_gcode kuerzen**

Old (Zeilen 87-114):
```
[gcode_button feed_button]
pin: ^!LLL_PLUS:PB12
press_gcode:
    {% set v         = printer["gcode_macro _FILAMENT_VARS"] %}
    {% set new_count = (printer["gcode_macro _TRIPLE_CLICK_STATE"].feed_count | int) + 1 %}
    {% if new_count >= 3 %}
        SET_GCODE_VARIABLE MACRO=_TRIPLE_CLICK_STATE VARIABLE=feed_count VALUE=0
        UPDATE_DELAYED_GCODE ID=_triple_feed_reset DURATION=0
        _TRIPLE_FEED_BURST
    {% elif new_count == 2 %}
        M118 Vorschub-Taster (2. Klick) - 10 mm Puls
        SET_GCODE_VARIABLE MACRO=_MANUAL_FEED VARIABLE=active VALUE=0
        UPDATE_DELAYED_GCODE ID=_manual_feed_loop DURATION=0
        SET_GCODE_VARIABLE MACRO=_BUFFER_AUTO_CONTROL VARIABLE=manual_operation VALUE=1
        _SYNC_OFF
        FORCE_MOVE STEPPER="extruder_stepper mellow" DISTANCE=10 VELOCITY=15 ACCEL=1000
        SET_GCODE_VARIABLE MACRO=_TRIPLE_CLICK_STATE VARIABLE=feed_count VALUE={new_count}
        UPDATE_DELAYED_GCODE ID=_triple_feed_reset DURATION={v.triple_click_window}
        UPDATE_DELAYED_GCODE ID=_reenable_autofeed DURATION=1
    {% else %}
        M118 Vorschub-Taster - Dauerlauf startet
        SET_GCODE_VARIABLE MACRO=_BUFFER_AUTO_CONTROL VARIABLE=manual_operation VALUE=1
        _SYNC_OFF
        SET_GCODE_VARIABLE MACRO=_MANUAL_FEED VARIABLE=active VALUE=1
        _MANUAL_FEED
        SET_GCODE_VARIABLE MACRO=_TRIPLE_CLICK_STATE VARIABLE=feed_count VALUE={new_count}
        UPDATE_DELAYED_GCODE ID=_triple_feed_reset DURATION={v.triple_click_window}
    {% endif %}
release_gcode:
    M118 Vorschub-Taster losgelassen
    SET_GCODE_VARIABLE MACRO=_MANUAL_FEED VARIABLE=active VALUE=0
    UPDATE_DELAYED_GCODE ID=_manual_feed_loop DURATION=0
    UPDATE_DELAYED_GCODE ID=_reenable_autofeed DURATION=1
```

New:
```
[gcode_button feed_button]
pin: ^!LLL_PLUS:PB12
press_gcode:
    _BUTTON_CLICK_HANDLER DIRECTION=FEED
release_gcode:
    {% set v = printer["gcode_macro _FILAMENT_VARS"] %}
    M118 Vorschub-Taster losgelassen
    SET_GCODE_VARIABLE MACRO=_MANUAL_FEED VARIABLE=active VALUE=0
    UPDATE_DELAYED_GCODE ID=_manual_feed_loop DURATION=0
    UPDATE_DELAYED_GCODE ID=_reenable_autofeed DURATION={v.reenable_cooldown}
```

- [ ] **Step 3: retract_button press_gcode kuerzen**

Old (Zeilen 121-153):
```
[gcode_button retract_button]
pin: ^!LLL_PLUS:PB13
press_gcode:
    {% set v         = printer["gcode_macro _FILAMENT_VARS"] %}
    {% set new_count = (printer["gcode_macro _TRIPLE_CLICK_STATE"].retract_count | int) + 1 %}
    {% if new_count >= 3 %}
        ...
    {% elif new_count == 2 %}
        ...
    {% else %}
        ...
    {% endif %}
release_gcode:
    M118 Rueckzug-Taster losgelassen
    SET_GCODE_VARIABLE MACRO=_MANUAL_RETRACT VARIABLE=active VALUE=0
    UPDATE_DELAYED_GCODE ID=_manual_retract_loop DURATION=0
    UPDATE_DELAYED_GCODE ID=_reenable_autofeed DURATION=1
```

New:
```
[gcode_button retract_button]
pin: ^!LLL_PLUS:PB13
press_gcode:
    _BUTTON_CLICK_HANDLER DIRECTION=RETRACT
release_gcode:
    {% set v = printer["gcode_macro _FILAMENT_VARS"] %}
    M118 Rueckzug-Taster losgelassen
    SET_GCODE_VARIABLE MACRO=_MANUAL_RETRACT VARIABLE=active VALUE=0
    UPDATE_DELAYED_GCODE ID=_manual_retract_loop DURATION=0
    UPDATE_DELAYED_GCODE ID=_reenable_autofeed DURATION={v.reenable_cooldown}
```

- [ ] **Step 4: Verifikation**

```bash
grep -c "_BUTTON_CLICK_HANDLER DIRECTION=" printer_data/config/lll.cfg
```
Expected: `2` (FEED + RETRACT Caller).

```bash
wc -l printer_data/config/lll.cfg
```
Expected: mindestens 50 Zeilen weniger als vor R-7.

- [ ] **Step 5: Commit**

```bash
git add printer_data/config/lll.cfg
git commit -m "refactor(lll.cfg): feed/retract buttons use _BUTTON_CLICK_HANDLER

Beide Button-Handler rufen nur noch den parametrierten Helper.
release_gcode-Bloecke lesen reenable_cooldown aus _FILAMENT_VARS.
Spec: 2026-04-21-lll-buffer-refactor-design.md §6.4

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task R-8: Manual-Loop-Macros auf Variablen umstellen

**Files:**
- Modify: `printer_data/config/lll.cfg` (Zeilen ~159-181 + Initial-Follow-Loop + Triple-Burst-Macros)

- [ ] **Step 1: Vor-Zustand-Snapshot**

```bash
grep -n "DISTANCE=10 VELOCITY=15\|DISTANCE=-10 VELOCITY=20\|DISTANCE=10 VELOCITY=15\|DURATION=0.1" printer_data/config/lll.cfg
```
Alle hardcoded Vorkommen auflisten.

- [ ] **Step 2: _MANUAL_FEED umstellen**

Old:
```
[gcode_macro _MANUAL_FEED]
variable_active: 0
gcode:
    {% if active == 1 %}
        FORCE_MOVE STEPPER="extruder_stepper mellow" DISTANCE=10 VELOCITY=15 ACCEL=1000
        UPDATE_DELAYED_GCODE ID=_manual_feed_loop DURATION=0.1
    {% endif %}
```

New:
```
[gcode_macro _MANUAL_FEED]
variable_active: 0
gcode:
    {% set v = printer["gcode_macro _FILAMENT_VARS"] %}
    {% if active == 1 %}
        FORCE_MOVE STEPPER="extruder_stepper mellow" DISTANCE={v.manual_chunk_distance} VELOCITY={v.manual_speed} ACCEL={v.force_move_accel}
        UPDATE_DELAYED_GCODE ID=_manual_feed_loop DURATION={v.manual_loop_tick}
    {% endif %}
```

- [ ] **Step 3: _MANUAL_RETRACT umstellen**

Old:
```
[gcode_macro _MANUAL_RETRACT]
variable_active: 0
gcode:
    {% if active == 1 %}
        FORCE_MOVE STEPPER="extruder_stepper mellow" DISTANCE=-10 VELOCITY=20 ACCEL=1000
        UPDATE_DELAYED_GCODE ID=_manual_retract_loop DURATION=0.1
    {% endif %}
```

New (beachte: `VELOCITY=20 → manual_speed (=15)` — vereinheitlicht):
```
[gcode_macro _MANUAL_RETRACT]
variable_active: 0
gcode:
    {% set v = printer["gcode_macro _FILAMENT_VARS"] %}
    {% if active == 1 %}
        FORCE_MOVE STEPPER="extruder_stepper mellow" DISTANCE=-{v.manual_chunk_distance} VELOCITY={v.manual_speed} ACCEL={v.force_move_accel}
        UPDATE_DELAYED_GCODE ID=_manual_retract_loop DURATION={v.manual_loop_tick}
    {% endif %}
```

- [ ] **Step 4: _TRIPLE_FEED_BURST auf reenable_cooldown_fast umstellen**

Old:
```
[gcode_macro _TRIPLE_FEED_BURST]
gcode:
    {% set v = printer["gcode_macro _FILAMENT_VARS"] %}
    {% set duration = (v.triple_click_distance / v.fast_speed + 0.5) | round(1) %}
    M118 Triple-Feed-Burst: {v.triple_click_distance} mm @ {v.fast_speed} mm/s vor (~{duration}s)
    SET_GCODE_VARIABLE MACRO=_MANUAL_FEED VARIABLE=active VALUE=0
    UPDATE_DELAYED_GCODE ID=_manual_feed_loop DURATION=0
    SET_GCODE_VARIABLE MACRO=_BUFFER_AUTO_CONTROL VARIABLE=manual_operation VALUE=1
    _SYNC_OFF
    FORCE_MOVE STEPPER="extruder_stepper mellow" DISTANCE={v.triple_click_distance} VELOCITY={v.fast_speed} ACCEL=1000
    UPDATE_DELAYED_GCODE ID=_reenable_autofeed DURATION=0.5
```

New:
```
[gcode_macro _TRIPLE_FEED_BURST]
gcode:
    {% set v = printer["gcode_macro _FILAMENT_VARS"] %}
    {% set duration = (v.triple_click_distance / v.fast_speed + 0.5) | round(1) %}
    M118 Triple-Feed-Burst: {v.triple_click_distance} mm @ {v.fast_speed} mm/s vor (~{duration}s)
    SET_GCODE_VARIABLE MACRO=_MANUAL_FEED VARIABLE=active VALUE=0
    UPDATE_DELAYED_GCODE ID=_manual_feed_loop DURATION=0
    SET_GCODE_VARIABLE MACRO=_BUFFER_AUTO_CONTROL VARIABLE=manual_operation VALUE=1
    _SYNC_OFF
    FORCE_MOVE STEPPER="extruder_stepper mellow" DISTANCE={v.triple_click_distance} VELOCITY={v.fast_speed} ACCEL={v.force_move_accel}
    UPDATE_DELAYED_GCODE ID=_reenable_autofeed DURATION={v.reenable_cooldown_fast}
```

- [ ] **Step 5: _TRIPLE_RETRACT_BURST analog**

Old:
```
[gcode_macro _TRIPLE_RETRACT_BURST]
gcode:
    {% set v = printer["gcode_macro _FILAMENT_VARS"] %}
    {% set duration = (v.triple_click_distance / v.fast_speed + 0.5) | round(1) %}
    M118 Triple-Retract-Burst: {v.triple_click_distance} mm @ {v.fast_speed} mm/s zurueck (~{duration}s)
    SET_GCODE_VARIABLE MACRO=_MANUAL_RETRACT VARIABLE=active VALUE=0
    UPDATE_DELAYED_GCODE ID=_manual_retract_loop DURATION=0
    SET_GCODE_VARIABLE MACRO=_BUFFER_AUTO_CONTROL VARIABLE=manual_operation VALUE=1
    _SYNC_OFF
    FORCE_MOVE STEPPER="extruder_stepper mellow" DISTANCE=-{v.triple_click_distance} VELOCITY={v.fast_speed} ACCEL=1000
    UPDATE_DELAYED_GCODE ID=_reenable_autofeed DURATION=0.5
```

New:
```
[gcode_macro _TRIPLE_RETRACT_BURST]
gcode:
    {% set v = printer["gcode_macro _FILAMENT_VARS"] %}
    {% set duration = (v.triple_click_distance / v.fast_speed + 0.5) | round(1) %}
    M118 Triple-Retract-Burst: {v.triple_click_distance} mm @ {v.fast_speed} mm/s zurueck (~{duration}s)
    SET_GCODE_VARIABLE MACRO=_MANUAL_RETRACT VARIABLE=active VALUE=0
    UPDATE_DELAYED_GCODE ID=_manual_retract_loop DURATION=0
    SET_GCODE_VARIABLE MACRO=_BUFFER_AUTO_CONTROL VARIABLE=manual_operation VALUE=1
    _SYNC_OFF
    FORCE_MOVE STEPPER="extruder_stepper mellow" DISTANCE=-{v.triple_click_distance} VELOCITY={v.fast_speed} ACCEL={v.force_move_accel}
    UPDATE_DELAYED_GCODE ID=_reenable_autofeed DURATION={v.reenable_cooldown_fast}
```

- [ ] **Step 6: _INITIAL_GRIP_PHASE und _initial_follow_loop umstellen**

Old `_INITIAL_GRIP_PHASE`:
```
    FORCE_MOVE STEPPER="extruder_stepper mellow" DISTANCE={distance} VELOCITY={v.initial_grip_speed} ACCEL=1000
```

New:
```
    FORCE_MOVE STEPPER="extruder_stepper mellow" DISTANCE={distance} VELOCITY={v.initial_grip_speed} ACCEL={v.force_move_accel}
```

Old `_initial_follow_loop`:
```
        FORCE_MOVE STEPPER="extruder_stepper mellow" DISTANCE=10 VELOCITY={v.initial_follow_speed} ACCEL=1000
        {% set loop_delay = ((10 / v.initial_follow_speed) + 0.05) | round(2) %}
```

New (DISTANCE und loop_delay-Teiler beide aus manual_chunk_distance):
```
        FORCE_MOVE STEPPER="extruder_stepper mellow" DISTANCE={v.manual_chunk_distance} VELOCITY={v.initial_follow_speed} ACCEL={v.force_move_accel}
        {% set loop_delay = ((v.manual_chunk_distance / v.initial_follow_speed) + 0.05) | round(2) %}
```

- [ ] **Step 7: Verifikation**

```bash
grep -c "ACCEL=1000" printer_data/config/lll.cfg
```
Expected: `0` (alle raus).

```bash
grep -c "DISTANCE=10 VELOCITY=\|DISTANCE=-10 VELOCITY=" printer_data/config/lll.cfg
```
Expected: `0`.

```bash
grep -c "{v.force_move_accel}" printer_data/config/lll.cfg
```
Expected: mindestens `6` (Manual-Feed, Manual-Retract, 2x Triple-Burst, Grip, Follow).

- [ ] **Step 8: Commit**

```bash
git add printer_data/config/lll.cfg
git commit -m "refactor(lll.cfg): manual/follow/triple macros use _FILAMENT_VARS

Alle hardcoded DISTANCE/VELOCITY/ACCEL/DURATION-Werte aus den
Loop-Macros gezogen, Magic Numbers weg. Manual Feed/Retract
Speed vereinheitlicht (15 mm/s, vorher 15 vs 20).
Spec: 2026-04-21-lll-buffer-refactor-design.md §8 + §6.1 (Q6.1)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task R-9: _UNLOAD_FAST_RETRACT Magic-Coupling fixen + LOAD/UNLOAD auf _SAVE_E_MODE

**Files:**
- Modify: `printer_data/config/lll.cfg` (Zeilen ~618-735)

- [ ] **Step 1: Vor-Zustand-Snapshot**

```bash
grep -n "DISTANCE=-10 VELOCITY={v.fast_speed}\|retracted + 10\|variable_e_abs" printer_data/config/lll.cfg
```

- [ ] **Step 2: LOAD_FILAMENT auf _SAVE_E_MODE / _RESTORE_E_MODE**

Old (Zeilen 618-657):
```
[gcode_macro LOAD_FILAMENT]
description: Filament laden (3-Phasen, Phase 2 via Extruder-Sync)
variable_e_abs: 0
gcode:
    {% set v    = printer["gcode_macro _FILAMENT_VARS"] %}
    {% set temp = printer.extruder.temperature %}

    {% if temp < v.min_temp %}
        {action_raise_error("LADEN abgebrochen: Hotend zu kalt ({:.0f}/{:.0f} C)".format(temp|float, v.min_temp|float))}
    {% endif %}

    SET_GCODE_VARIABLE MACRO=LOAD_FILAMENT VARIABLE=e_abs VALUE={1 if printer.gcode_move.absolute_extrude else 0}
    M83

    SET_GCODE_VARIABLE MACRO=_BUFFER_AUTO_CONTROL VARIABLE=manual_operation VALUE=1

    ...

    {% if printer["gcode_macro LOAD_FILAMENT"].e_abs %}M82{% else %}M83{% endif %}

    SET_GCODE_VARIABLE MACRO=_BUFFER_AUTO_CONTROL VARIABLE=manual_operation VALUE=0
    _APPLY_SYNC_STATE
    M118 LADEN abgeschlossen
```

New:
```
[gcode_macro LOAD_FILAMENT]
description: Filament laden (3-Phasen, Phase 2 via Extruder-Sync)
gcode:
    {% set v    = printer["gcode_macro _FILAMENT_VARS"] %}
    {% set temp = printer.extruder.temperature %}

    {% if temp < v.min_temp %}
        {action_raise_error("LADEN abgebrochen: Hotend zu kalt ({:.0f}/{:.0f} C)".format(temp|float, v.min_temp|float))}
    {% endif %}

    _SAVE_E_MODE
    SET_GCODE_VARIABLE MACRO=_BUFFER_AUTO_CONTROL VARIABLE=manual_operation VALUE=1

    # Phase 1: nur Feeder schnell (SYNC aus, FORCE_MOVE)
    M118 LADEN Phase 1: {v.load_fast1} mm @ {v.fast_speed} mm/s (nur Feeder)
    _SYNC_OFF
    FORCE_MOVE STEPPER="extruder_stepper mellow" DISTANCE={v.load_fast1} VELOCITY={v.fast_speed} ACCEL={v.force_move_accel}

    # Phase 2: Feeder + Extruder parallel synchron langsam.
    M118 LADEN Phase 2: {v.load_slow} mm @ {v.slow_speed} mm/s (Sync 1:1)
    SYNC_EXTRUDER_MOTION EXTRUDER=mellow MOTION_QUEUE=extruder
    SET_EXTRUDER_ROTATION_DISTANCE EXTRUDER=mellow DISTANCE={v.sync_rotation_distance}
    G1 E{v.load_slow} F{(v.slow_speed * 60)|int}
    M400

    # Phase 3: nur Feeder schnell (wieder Sync aus, FORCE_MOVE)
    M118 LADEN Phase 3: {v.load_fast2} mm @ {v.fast_speed} mm/s (nur Feeder)
    _SYNC_OFF
    FORCE_MOVE STEPPER="extruder_stepper mellow" DISTANCE={v.load_fast2} VELOCITY={v.fast_speed} ACCEL={v.force_move_accel}

    _RESTORE_E_MODE
    SET_GCODE_VARIABLE MACRO=_BUFFER_AUTO_CONTROL VARIABLE=manual_operation VALUE=0
    _APPLY_SYNC_STATE
    M118 LADEN abgeschlossen
```

**Aenderungen:** `variable_e_abs: 0` weg, `SET ... e_abs ...` + `M83` → `_SAVE_E_MODE`, Ende-M82/M83-Branch → `_RESTORE_E_MODE`, Phase-1/3 ACCEL → `{v.force_move_accel}`.

- [ ] **Step 3: UNLOAD_FILAMENT auf _SAVE_E_MODE + _UNLOAD_FAST_RETRACT Magic-Coupling fix**

Old UNLOAD_FILAMENT (Zeilen 664-704):
```
[gcode_macro UNLOAD_FILAMENT]
description: Filament entladen (Tip-Forming + Sync-Retract + Feeder-Rueckzug)
variable_state: 'idle'
variable_retracted: 0
variable_e_abs: 0
gcode:
    {% set v    = printer["gcode_macro _FILAMENT_VARS"] %}
    {% set temp = printer.extruder.temperature %}

    {% if temp < v.min_temp %}
        {action_raise_error("ENTLADEN abgebrochen: Hotend zu kalt ({:.0f}/{:.0f} C)".format(temp|float, v.min_temp|float))}
    {% endif %}

    SET_GCODE_VARIABLE MACRO=UNLOAD_FILAMENT VARIABLE=e_abs VALUE={1 if printer.gcode_move.absolute_extrude else 0}
    M83

    SET_GCODE_VARIABLE MACRO=_BUFFER_AUTO_CONTROL VARIABLE=manual_operation VALUE=1

    # Phase 1: Tip-Forming
    ...
    # Phase 3: nur Feeder-Rueckzug
    ...
    _UNLOAD_FAST_RETRACT
```

New:
```
[gcode_macro UNLOAD_FILAMENT]
description: Filament entladen (Tip-Forming + Sync-Retract + Feeder-Rueckzug)
variable_state: 'idle'
variable_retracted: 0
gcode:
    {% set v    = printer["gcode_macro _FILAMENT_VARS"] %}
    {% set temp = printer.extruder.temperature %}

    {% if temp < v.min_temp %}
        {action_raise_error("ENTLADEN abgebrochen: Hotend zu kalt ({:.0f}/{:.0f} C)".format(temp|float, v.min_temp|float))}
    {% endif %}

    _SAVE_E_MODE
    SET_GCODE_VARIABLE MACRO=_BUFFER_AUTO_CONTROL VARIABLE=manual_operation VALUE=1

    # Phase 1: Tip-Forming - Sync an auf nominal, G1 E steuert beides
    M118 ENTLADEN Phase 1: Tip-Forming ({v.tip_cycles} Zyklen @ {v.tip_speed} mm/s)
    SYNC_EXTRUDER_MOTION EXTRUDER=mellow MOTION_QUEUE=extruder
    SET_EXTRUDER_ROTATION_DISTANCE EXTRUDER=mellow DISTANCE={v.sync_rotation_distance}
    {% for i in range(v.tip_cycles) %}
        G1 E{v.tip_push} F{(v.tip_speed * 60)|int}
        G1 E-{v.tip_pull} F{(v.tip_speed * 60)|int}
    {% endfor %}
    G1 E-{v.tip_final_retract} F{(v.tip_final_speed * 60)|int}
    M400
    M118 ENTLADEN Phase 1: Tip-Forming abgeschlossen

    # Phase 2: Sync-Retract schnell
    M118 ENTLADEN Phase 2: {v.unload_sync} mm @ {v.fast_speed} mm/s (Sync-Retract)
    G1 E-{v.unload_sync} F{(v.fast_speed * 60)|int}
    M400

    # Phase 3: nur Feeder-Rueckzug, Sync aus + Polling
    M118 ENTLADEN Phase 3: Feeder-Rueckzug bis buffer_entrance frei (max {v.unload_fast_max} mm)
    _SYNC_OFF
    SET_GCODE_VARIABLE MACRO=UNLOAD_FILAMENT VARIABLE=state VALUE='fast_retract'
    SET_GCODE_VARIABLE MACRO=UNLOAD_FILAMENT VARIABLE=retracted VALUE=0
    _UNLOAD_FAST_RETRACT
```

- [ ] **Step 4: _UNLOAD_FAST_RETRACT fixen (Magic-Coupling + _RESTORE_E_MODE)**

Old (Zeilen 707-732):
```
[gcode_macro _UNLOAD_FAST_RETRACT]
gcode:
    {% set v         = printer["gcode_macro _FILAMENT_VARS"] %}
    {% set state     = printer["gcode_macro UNLOAD_FILAMENT"].state %}
    {% set entrance  = printer["filament_switch_sensor buffer_entrance"].filament_detected %}
    {% set retracted = printer["gcode_macro UNLOAD_FILAMENT"].retracted %}
    {% set loop_delay = ((10 / v.fast_speed) + 0.1) | round(2) %}

    {% if state == 'fast_retract' %}
        {% if entrance and retracted < v.unload_fast_max %}
            FORCE_MOVE STEPPER="extruder_stepper mellow" DISTANCE=-10 VELOCITY={v.fast_speed} ACCEL=1000
            SET_GCODE_VARIABLE MACRO=UNLOAD_FILAMENT VARIABLE=retracted VALUE={retracted + 10}
            UPDATE_DELAYED_GCODE ID=_unload_fast_delayed DURATION={loop_delay}
        {% else %}
            SET_GCODE_VARIABLE MACRO=UNLOAD_FILAMENT VARIABLE=state VALUE='idle'
            {% if printer["gcode_macro UNLOAD_FILAMENT"].e_abs %}M82{% else %}M83{% endif %}
            SET_GCODE_VARIABLE MACRO=_BUFFER_AUTO_CONTROL VARIABLE=system_enabled VALUE=0
            SET_GCODE_VARIABLE MACRO=_BUFFER_AUTO_CONTROL VARIABLE=manual_operation VALUE=0
            _APPLY_SYNC_STATE
            {% if not entrance %}
                M118 ENTLADEN abgeschlossen - buffer_entrance frei ({retracted} mm zurueck)
            {% else %}
                M118 ENTLADEN-WARNUNG: Timeout {v.unload_fast_max} mm erreicht - bitte pruefen
            {% endif %}
        {% endif %}
    {% endif %}
```

New:
```
[gcode_macro _UNLOAD_FAST_RETRACT]
gcode:
    {% set v         = printer["gcode_macro _FILAMENT_VARS"] %}
    {% set state     = printer["gcode_macro UNLOAD_FILAMENT"].state %}
    {% set entrance  = printer["filament_switch_sensor buffer_entrance"].filament_detected %}
    {% set retracted = printer["gcode_macro UNLOAD_FILAMENT"].retracted %}
    {% set chunk = v.manual_chunk_distance %}
    {% set loop_delay = ((chunk / v.fast_speed) + 0.1) | round(2) %}

    {% if state == 'fast_retract' %}
        {% if entrance and retracted < v.unload_fast_max %}
            FORCE_MOVE STEPPER="extruder_stepper mellow" DISTANCE=-{chunk} VELOCITY={v.fast_speed} ACCEL={v.force_move_accel}
            SET_GCODE_VARIABLE MACRO=UNLOAD_FILAMENT VARIABLE=retracted VALUE={retracted + chunk}
            UPDATE_DELAYED_GCODE ID=_unload_fast_delayed DURATION={loop_delay}
        {% else %}
            SET_GCODE_VARIABLE MACRO=UNLOAD_FILAMENT VARIABLE=state VALUE='idle'
            _RESTORE_E_MODE
            SET_GCODE_VARIABLE MACRO=_BUFFER_AUTO_CONTROL VARIABLE=system_enabled VALUE=0
            SET_GCODE_VARIABLE MACRO=_BUFFER_AUTO_CONTROL VARIABLE=manual_operation VALUE=0
            _APPLY_SYNC_STATE
            {% if not entrance %}
                M118 ENTLADEN abgeschlossen - buffer_entrance frei ({retracted} mm zurueck)
            {% else %}
                M118 ENTLADEN-WARNUNG: Timeout {v.unload_fast_max} mm erreicht - bitte pruefen
            {% endif %}
        {% endif %}
    {% endif %}
```

- [ ] **Step 5: Verifikation**

```bash
grep -c "variable_e_abs" printer_data/config/lll.cfg
```
Expected: `0` (aus LOAD und UNLOAD entfernt).

```bash
grep -c "DISTANCE=-10 VELOCITY=\|retracted + 10" printer_data/config/lll.cfg
```
Expected: `0` (Magic-Coupling weg).

```bash
grep -c "_SAVE_E_MODE\|_RESTORE_E_MODE" printer_data/config/lll.cfg
```
Expected: mindestens `5` (2 Definitions + 2 LOAD Caller + 2 UNLOAD Caller — jeweils mit und ohne Unterschrift, ich zaehle im mindestens.)

- [ ] **Step 6: Commit**

```bash
git add printer_data/config/lll.cfg
git commit -m "refactor(lll.cfg): LOAD/UNLOAD use _SAVE_E_MODE + fix chunk coupling

- LOAD_FILAMENT / UNLOAD_FILAMENT: variable_e_abs entfernt, stattdessen
  _SAVE_E_MODE / _RESTORE_E_MODE
- _UNLOAD_FAST_RETRACT: DISTANCE und retracted-Counter jetzt aus
  v.manual_chunk_distance (Magic-Coupling eliminiert)
- ACCEL=1000 in den Load/Unload-Phasen auf {v.force_move_accel}
Spec: 2026-04-21-lll-buffer-refactor-design.md §6.3 + §8

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task R-10: ENABLE_RUNOUT_SENSOR / DISABLE_RUNOUT_SENSOR umbenennen

**Files:**
- Modify: `printer_data/config/lll.cfg` (Zeilen ~228-234)

- [ ] **Step 1: Vor-Zustand-Snapshot**

```bash
grep -n "Enable_Runout_Sensor\|Disable_Runout_Sensor" printer_data/config/lll.cfg
```
Expected: 2 Definitionen.

- [ ] **Step 2: Umbenennen**

Old:
```
[gcode_macro Enable_Runout_Sensor]
gcode:
    SET_GCODE_VARIABLE MACRO=_BUFFER_AUTO_CONTROL VARIABLE=print_running VALUE=1

[gcode_macro Disable_Runout_Sensor]
gcode:
    SET_GCODE_VARIABLE MACRO=_BUFFER_AUTO_CONTROL VARIABLE=print_running VALUE=0
```

New:
```
[gcode_macro ENABLE_RUNOUT_SENSOR]
description: Aktiviert das Pausieren bei Filament-Runout (in PRINT_START nutzen)
gcode:
    SET_GCODE_VARIABLE MACRO=_BUFFER_AUTO_CONTROL VARIABLE=print_running VALUE=1

[gcode_macro DISABLE_RUNOUT_SENSOR]
description: Deaktiviert das Pausieren bei Filament-Runout (in PRINT_END nutzen)
gcode:
    SET_GCODE_VARIABLE MACRO=_BUFFER_AUTO_CONTROL VARIABLE=print_running VALUE=0
```

- [ ] **Step 3: Verifikation**

```bash
grep -c "Enable_Runout_Sensor\|Disable_Runout_Sensor" printer_data/config/lll.cfg
```
Expected: `0` (Camel_Snake ist weg).

```bash
grep -c "ENABLE_RUNOUT_SENSOR\|DISABLE_RUNOUT_SENSOR" printer_data/config/lll.cfg
```
Expected: mindestens `2`.

- [ ] **Step 4: Commit**

```bash
git add printer_data/config/lll.cfg
git commit -m "refactor(lll.cfg): rename Enable/Disable_Runout_Sensor to UPPER_SNAKE

Konsistenz mit anderen User-Facing-Macros (FORCE_BUFFER_FILL etc.).
User bestaetigt: nicht aus PRINT_START/END gerufen - sichere Umbenennung.
Spec: 2026-04-21-lll-buffer-refactor-design.md §9 (Q6.4)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task R-11: Mirror-Variables entfernen (Hall-Handler + State-Container cleanup)

**Files:**
- Modify: `printer_data/config/lll.cfg` (Hall-Handler 269-328 + _BUFFER_AUTO_CONTROL 334-347)

- [ ] **Step 1: Vor-Zustand-Snapshot**

```bash
grep -n "hall[123]_active" printer_data/config/lll.cfg
```
Expected: alle Mirror-SET + Variable-Deklarationen auflisten.

- [ ] **Step 2: buffer_hall3 bereinigen**

Old:
```
[gcode_button buffer_hall3]
pin: ^!LLL_PLUS:PB4
press_gcode:
    SET_GCODE_VARIABLE MACRO=_BUFFER_AUTO_CONTROL VARIABLE=hall3_active VALUE=0
    {% if printer["gcode_macro _BUFFER_AUTO_CONTROL"].initial_lockout == 1 %}
        M118 HALL3 FREI (ignoriert - Initial-Lockout)
    {% else %}
        M118 HALL3 FREI - Hals verlaesst Leer-Schwelle (Feed -> neutral)
        _APPLY_SYNC_STATE
    {% endif %}
release_gcode:
    SET_GCODE_VARIABLE MACRO=_BUFFER_AUTO_CONTROL VARIABLE=hall3_active VALUE=1
    {% if printer["gcode_macro _BUFFER_AUTO_CONTROL"].initial_lockout == 1 %}
        M118 HALL3 AKTIV (ignoriert - Initial-Lockout)
    {% else %}
        M118 HALL3 AKTIV - Leer-Schwelle erreicht (Feed +{(printer["gcode_macro _FILAMENT_VARS"].sync_modulation * 100)|int}%)
        _APPLY_SYNC_STATE
    {% endif %}
```

New:
```
[gcode_button buffer_hall3]
pin: ^!LLL_PLUS:PB4
press_gcode:
    {% if printer["gcode_macro _BUFFER_AUTO_CONTROL"].initial_lockout == 1 %}
        M118 HALL3 FREI (ignoriert - Initial-Lockout)
    {% else %}
        M118 HALL3 FREI - Hals verlaesst Leer-Schwelle (Feed -> neutral)
        _APPLY_SYNC_STATE
    {% endif %}
release_gcode:
    {% if printer["gcode_macro _BUFFER_AUTO_CONTROL"].initial_lockout == 1 %}
        M118 HALL3 AKTIV (ignoriert - Initial-Lockout)
    {% else %}
        M118 HALL3 AKTIV - Leer-Schwelle erreicht (Feed +{(printer["gcode_macro _FILAMENT_VARS"].sync_modulation * 100)|int}%)
        _APPLY_SYNC_STATE
    {% endif %}
```

- [ ] **Step 3: buffer_hall2 bereinigen (analog)**

In `buffer_hall2`: `SET ... hall2_active VALUE=0` und `SET ... hall2_active VALUE=1` entfernen.

- [ ] **Step 4: buffer_hall1 bereinigen**

In `buffer_hall1`: `SET ... hall1_active VALUE=0` und `SET ... hall1_active VALUE=1` entfernen. Rest bleibt (M118, overfill_lock, _ABORT_ALL_FEED_LOOPS, _APPLY_SYNC_STATE).

- [ ] **Step 5: _BUFFER_AUTO_CONTROL bereinigen**

Old:
```
[gcode_macro _BUFFER_AUTO_CONTROL]
variable_system_enabled: 0
variable_manual_operation: 0
variable_overfill_lock: 0
variable_print_running: 0
variable_initial_lockout: 0         # 1 = Hall2/3 ignorieren (Hall1 aber scharf)
variable_initial_follow_active: 0
# Hall-States (gespiegelt aus den button-handlern, damit _APPLY_SYNC_STATE
# ohne printer[...].state-Query arbeiten kann):
variable_hall1_active: 0            # 1 = Ueberlast
variable_hall2_active: 0            # 1 = Voll
variable_hall3_active: 0            # 1 = Leer
# E-Modus-Buchhaltung fuer LOAD/UNLOAD (von _SAVE_E_MODE gesetzt)
variable_saved_e_abs: 0
gcode:
    _APPLY_SYNC_STATE
```

New:
```
[gcode_macro _BUFFER_AUTO_CONTROL]
variable_system_enabled: 0
variable_manual_operation: 0
variable_overfill_lock: 0
variable_print_running: 0
# 1 = Hall2/3 ignorieren (Hall1 bleibt scharf, TPU-Stau-Schutz)
variable_initial_lockout: 0
variable_initial_follow_active: 0
# E-Modus-Buchhaltung fuer LOAD/UNLOAD (von _SAVE_E_MODE gesetzt)
variable_saved_e_abs: 0
gcode:
    # Body leer: State wird von Callern manipuliert, _APPLY_SYNC_STATE
    # wird explizit gerufen und braucht keinen impliziten Re-Sync hier.
```

- [ ] **Step 6: _STATE_DUMP anpassen (Mirror-Zeilen raus, Raw-States mit Klartext rein)**

Old (Zeilen 494-516):
```
[gcode_macro _STATE_DUMP]
description: Zeigt alle Buffer-Flags und Sensor-Zustaende (Diagnose)
gcode:
    {% set b = printer["gcode_macro _BUFFER_AUTO_CONTROL"] %}
    {% set t = printer["gcode_macro _TRIPLE_CLICK_STATE"] %}
    {% set s = printer["filament_switch_sensor buffer_entrance"] %}
    M118 ---- BUFFER STATE ----
    M118 system_enabled     = {b.system_enabled}
    M118 manual_operation   = {b.manual_operation}
    M118 overfill_lock      = {b.overfill_lock}
    M118 initial_lockout    = {b.initial_lockout}
    M118 initial_follow_act = {b.initial_follow_active}
    M118 print_running      = {b.print_running}
    M118 hall1_active       = {b.hall1_active}
    M118 hall2_active       = {b.hall2_active}
    M118 hall3_active       = {b.hall3_active}
    M118 feed_count         = {t.feed_count}
    M118 retract_count      = {t.retract_count}
    M118 buffer_entrance    = {s.filament_detected}
    M118 hall1 raw state    = {printer["gcode_button buffer_hall1"].state}
    M118 hall2 raw state    = {printer["gcode_button buffer_hall2"].state}
    M118 hall3 raw state    = {printer["gcode_button buffer_hall3"].state}
    M118 ---- END STATE ----
```

New:
```
[gcode_macro _STATE_DUMP]
description: Zeigt alle Buffer-Flags und Sensor-Zustaende (Diagnose)
gcode:
    {% set b = printer["gcode_macro _BUFFER_AUTO_CONTROL"] %}
    {% set t = printer["gcode_macro _TRIPLE_CLICK_STATE"] %}
    {% set s = printer["filament_switch_sensor buffer_entrance"] %}
    {% set h1 = printer["gcode_button buffer_hall1"].state %}
    {% set h2 = printer["gcode_button buffer_hall2"].state %}
    {% set h3 = printer["gcode_button buffer_hall3"].state %}
    M118 ---- BUFFER STATE ----
    M118 system_enabled     = {b.system_enabled}
    M118 manual_operation   = {b.manual_operation}
    M118 overfill_lock      = {b.overfill_lock}
    M118 initial_lockout    = {b.initial_lockout}
    M118 initial_follow_act = {b.initial_follow_active}
    M118 print_running      = {b.print_running}
    M118 saved_e_abs        = {b.saved_e_abs}
    M118 feed_count         = {t.feed_count}
    M118 retract_count      = {t.retract_count}
    M118 buffer_entrance    = {s.filament_detected}
    M118 HALL1 raw={h1} aktiv={'JA' if h1 == 'RELEASED' else 'nein'}
    M118 HALL2 raw={h2} aktiv={'JA' if h2 == 'RELEASED' else 'nein'}
    M118 HALL3 raw={h3} aktiv={'JA' if h3 == 'RELEASED' else 'nein'}
    M118 ---- END STATE ----
```

- [ ] **Step 7: Verifikation**

```bash
grep -c "hall[123]_active" printer_data/config/lll.cfg
```
Expected: `0` (alle Mirror-Referenzen weg).

```bash
grep -c "variable_hall" printer_data/config/lll.cfg
```
Expected: `0`.

```bash
grep -c "saved_e_abs" printer_data/config/lll.cfg
```
Expected: mindestens `2` (Variable + Anzeige in _STATE_DUMP).

- [ ] **Step 8: Commit**

```bash
git add printer_data/config/lll.cfg
git commit -m "refactor(lll.cfg): remove hall mirror variables, clean _BUFFER_AUTO_CONTROL

- variable_hall1/2/3_active aus _BUFFER_AUTO_CONTROL entfernt
- SET hall*_active aus allen Button-Handlern entfernt
- _BUFFER_AUTO_CONTROL gcode-Body geleert (toter Code)
- _STATE_DUMP zeigt Raw-States mit aktiv-Klartext
Spec: 2026-04-21-lll-buffer-refactor-design.md §6.5 + §7

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task R-12: Header aufraeumen (Kalico-Block weg, Voraussetzungen, ASCII)

**Files:**
- Modify: `printer_data/config/lll.cfg` (Zeilen 1-50 + restliche Umlaute)

- [ ] **Step 1: Vor-Zustand-Snapshot**

```bash
head -60 printer_data/config/lll.cfg
```

```bash
grep -nP '[\xc4\xd6\xdc\xe4\xf6\xfc\xdf\xb1]' printer_data/config/lll.cfg
```
Expected: Umlaute in Kommentaren finden (Zeilen 22, 69, 81, 401, 567, 570).

- [ ] **Step 2: Header ersetzen**

Old (Zeilen 1-50):
```
#####################################################################
#                  Mellow LLL Filament Plus Buffer
#####################################################################
#
# Architektur (Variante 2 - Happy-Hare-Style Sync-Feedback):
#
... (50 Zeilen mit Kalico-sed-Hinweis etc.)
#####################################################################
```

New:
```
#####################################################################
#                  Mellow LLL Filament Plus Buffer
#                  Klipper-Config (Mainline)
#####################################################################
#
# Architektur: Variante 2 - Happy-Hare-Style Sync-Feedback
# --------------------------------------------------------
# Der Feeder-Stepper [extruder_stepper mellow] ist permanent an den
# Hauptextruder gekoppelt (SYNC_EXTRUDER_MOTION EXTRUDER=mellow
# MOTION_QUEUE=extruder). Er bewegt sich damit 1:1 mit jeder
# Extruder-Bewegung - Teil der normalen Extruder-Move-Queue. KEINE
# separaten FORCE_MOVE-Aufrufe waehrend des Drucks, keine
# Druck-Pausen durch toolhead.flush_step_generation().
#
# Die Hall-Sensoren steuern NICHT mehr einen eigenstaendigen Feed-Loop,
# sondern aendern nur die effektive rotation_distance:
#   HALL3 AKTIV (Leer)   -> rotation_distance kleiner  -> mehr Transport
#                           pro Extruder-mm -> Buffer fuellt sich
#   HALL2 AKTIV (Voll)   -> rotation_distance groesser -> weniger Transport
#                           pro Extruder-mm -> Buffer leert sich
#   HALL1 AKTIV (Ueberlast)-> Sync komplett AUS + overfill_lock
#
# Modulation = +/-sync_modulation (zentral in _FILAMENT_VARS).
#
# FORCE_MOVE wird nur ausserhalb aktiver Drucke verwendet:
#   - Initial-Grip / Initial-Follow (Erstbefuellung)
#   - Manuelle Taster (Feed/Retract/Burst)
#   - LOAD_FILAMENT Phase 1 und 3
#   - UNLOAD_FILAMENT Phase 3 (_UNLOAD_FAST_RETRACT)
# In allen Faellen wird Sync vorher getrennt (_SYNC_OFF) und
# hinterher via _APPLY_SYNC_STATE wiederhergestellt.
#
# Plattform: Klipper Mainline (Klipper3d/klipper). Mux-Key
# SYNC_EXTRUDER_MOTION EXTRUDER=mellow. Nicht kompatibel mit Kalico-
# Forks ohne Anpassung des Mux-Keys.
#
# VORAUSSETZUNGEN in der Haupt-printer.cfg:
#   [force_move]
#   enable_force_move: True
#
#   [pause_resume]             # fuer PAUSE im runout_gcode
#
#   [extruder]
#   max_extrude_only_distance: 250   # >= load_slow / unload_sync
#
#####################################################################
```

- [ ] **Step 3: Kommentar-Umlaute in ASCII konvertieren**

Grep die restlichen Umlaut-Kommentare und ersetze:
- `±` → `+/-`
- `ä` → `ae`, `ö` → `oe`, `ü` → `ue`, `ß` → `ss`
- Umlaut-Grossbuchstaben analog

Betroffene Stellen (nach Header-Ersatz noch):
- Z. 81 (nach Verschiebung): `# Manuelle Vor-/Rückschub-Taster` → `# Manuelle Vor-/Rueckschub-Taster`
- Z. ~401: `# Initial-Befüllung: Grip + Follow` → `# Initial-Befuellung: Grip + Follow`
- _FILAMENT_VARS Kommentare: `±20%` → `+/-20%`

- [ ] **Step 4: Placeholder-Kommentar im extruder_stepper klarer**

Old (Zeilen 67-70):
```
    # rotation_distance: MUSS auf 1:1-Sync mit dem Hauptextruder kalibriert
    # sein. Dieser Wert wird zur Laufzeit via SET_EXTRUDER_ROTATION_DISTANCE
    # dynamisch auf kleiner/groesser geschoben (±sync_modulation).
    # 19.5 ist Platzhalter - bitte kalibrieren (siehe _FILAMENT_VARS).
    rotation_distance: 19.5
```

New:
```
    # Initial-Wert fuer Klipper-Boot. Zur Laufzeit wird dieser Wert
    # dynamisch ueberschrieben (SET_EXTRUDER_ROTATION_DISTANCE, siehe
    # _APPLY_SYNC_STATE). Kalibrierung erfolgt ueber
    # _FILAMENT_VARS.sync_rotation_distance.
    rotation_distance: 19.5
```

- [ ] **Step 5: Verifikation**

```bash
grep -c "Kalico" printer_data/config/lll.cfg
```
Expected: `1` (nur noch der Kompatibilitaetshinweis im Header, keine sed-Anleitung).

```bash
grep -nP '[\xc4\xd6\xdc\xe4\xf6\xfc\xdf\xb1]' printer_data/config/lll.cfg
```
Expected: keine Treffer (alle Umlaute/± in Kommentaren ersetzt).

```bash
grep -c "pause_resume" printer_data/config/lll.cfg
```
Expected: mindestens `1`.

- [ ] **Step 6: Commit**

```bash
git add printer_data/config/lll.cfg
git commit -m "refactor(lll.cfg): clean header, ASCII-only comments

- Kalico-sed-Block entfernt (Mainline-only)
- Voraussetzungen-Block mit [force_move], [pause_resume], max_extrude_only_distance
- Umlaute und +/- in Kommentaren konsequent ASCII
- extruder_stepper-Kommentar klarer
Spec: 2026-04-21-lll-buffer-refactor-design.md §10 + §3

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task R-13: Section-Reihenfolge final ordnen

**Files:**
- Modify: `printer_data/config/lll.cfg` (komplette Neuanordnung)

- [ ] **Step 1: Vor-Zustand-Snapshot**

```bash
grep -nE "^\[(gcode_macro|gcode_button|mcu|extruder_stepper|tmc2208|filament_switch_sensor|delayed_gcode|force_move|pause_resume)" printer_data/config/lll.cfg
```
Aktuelle Sektionsreihenfolge dokumentieren.

- [ ] **Step 2: Datei neu anordnen**

Ziel-Reihenfolge (per Spec §5):
1. Header (bleibt oben)
2. `[mcu LLL_PLUS]`
3. `[extruder_stepper mellow]`
4. `[tmc2208 extruder_stepper mellow]`
5. `[gcode_macro _FILAMENT_VARS]`
6. Private Helper-Makros: `_SYNC_OFF`, `_APPLY_SYNC_STATE`, `_PREPARE_INITIAL_FILL`, `_ABORT_ALL_FEED_LOOPS`, `_SAVE_E_MODE`, `_RESTORE_E_MODE`, `_BUTTON_CLICK_HANDLER`
7. State-Container: `_BUFFER_AUTO_CONTROL`, `_TRIPLE_CLICK_STATE`
8. Taster: `feed_button`, `retract_button`, `_MANUAL_FEED`, `_MANUAL_RETRACT`, `_manual_feed_loop`, `_manual_retract_loop`, `_TRIPLE_FEED_BURST`, `_TRIPLE_RETRACT_BURST`, `_triple_feed_reset`, `_triple_retract_reset`, `_reenable_autofeed`
9. Hall-Sensoren: `buffer_hall1`, `buffer_hall2`, `buffer_hall3`
10. Entrance-Sensor: `buffer_entrance`
11. User-Facing Makros: `ENABLE_RUNOUT_SENSOR`, `DISABLE_RUNOUT_SENSOR`, `FORCE_BUFFER_FILL`, `STOP_BUFFER_FILL`, `BUFFER_AUTO_ON`, `_STATE_DUMP`
12. Boot-Autostart: `_boot_autostart`
13. Initial-Grip/Follow: `_INITIAL_GRIP_PHASE`, `_start_initial_grip`, `_initial_follow_loop`, `_initial_follow_end`
14. LOAD/UNLOAD: `LOAD_FILAMENT`, `UNLOAD_FILAMENT`, `_UNLOAD_FAST_RETRACT`, `_unload_fast_delayed`

Section-Header mit `##############` dazwischen. Dies ist ein reiner Umordnungs-Commit ohne semantische Aenderungen.

- [ ] **Step 3: Verifikation (Struktur + Inhalt)**

Nach dem Move: Diff gegen den letzten Commit pruefen. Alle Definitions muessen erhalten sein, nur die Reihenfolge aendert sich. Praktisch:

```bash
# Liste aller Macro/Button/... Namen vor und nach: sortiert muessen sie identisch sein
git show HEAD:printer_data/config/lll.cfg | grep -E "^\[" | sort > /tmp/sections_before.txt
grep -E "^\[" printer_data/config/lll.cfg | sort > /tmp/sections_after.txt
diff /tmp/sections_before.txt /tmp/sections_after.txt
```
Expected: kein Output von `diff`.

```bash
wc -l printer_data/config/lll.cfg
```
Expected: ähnliche Zeilenzahl wie vorher (±10 Zeilen durch neue Section-Separatoren).

- [ ] **Step 4: Commit**

```bash
git add printer_data/config/lll.cfg
git commit -m "refactor(lll.cfg): reorder sections to match spec §5

Reine Umordnung, keine semantischen Aenderungen. Reihenfolge folgt
der logischen Abhaengigkeitskette: Plattform -> Parameter -> Helpers
-> State -> Inputs (Taster/Sensoren) -> User-Macros -> Workflows.
Spec: 2026-04-21-lll-buffer-refactor-design.md §5

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task R-14: Semantik-Aequivalenz-Dokument erstellen

**Files:**
- Create: `docs/superpowers/specs/2026-04-21-lll-buffer-refactor-equivalence.md`

- [ ] **Step 1: State-Transitions-Tabelle erstellen**

Fuer jede externe Trigger-Quelle (buffer_entrance insert/runout, buttons, halls, boot, manual macros) eine Tabelle:

| Trigger | _APPLY_SYNC_STATE Input (State-Flags + Hall-Queries) | Erwarteter Output (MOTION_QUEUE + rotation_distance) |
|---|---|---|

Diese Tabelle gegen **Vorher** (mit Mirror-Vars) und **Nachher** (mit Raw-Queries) ausgefuehrt und verglichen werden. Weil die Logik-Baeume identisch sind (nur Quelle der Wahrheit verschoben), sollte die Tabelle komplett identisch sein.

- [ ] **Step 2: Datei schreiben**

Erstelle `docs/superpowers/specs/2026-04-21-lll-buffer-refactor-equivalence.md` mit:

```markdown
# Semantik-Aequivalenz Refactor Phase 1

**Referenz-Spec:** 2026-04-21-lll-buffer-refactor-design.md
**Scope:** Zeigt fuer jede State-Transition, dass Vorher- und Nachher-Code dasselbe Ergebnis liefern.

## Methodik
Fuer jede Event-Quelle wird der Pfad zum _APPLY_SYNC_STATE-Output manuell simuliert:
1. Welche Flags werden gesetzt?
2. Welche Hall-Sensoren sind zu dem Zeitpunkt in welchem state?
3. Welchen MOTION_QUEUE und welche rotation_distance berechnet _APPLY_SYNC_STATE?

## 1. buffer_entrance.insert_gcode
(konkrete Tabelle)

## 2. buffer_entrance.runout_gcode
...

## 3. feed_button (1/2/3 Klicks)
...

## 4. retract_button (1/2/3 Klicks)
...

## 5. HALL1 press/release
...

## 6. HALL2 press/release
...

## 7. HALL3 press/release
...

## 8. Boot-Autostart
...

## 9. LOAD_FILAMENT (alle 3 Phasen + Rueckkehr)
...

## 10. UNLOAD_FILAMENT (alle 3 Phasen + _UNLOAD_FAST_RETRACT)
...

## 11. STOP_BUFFER_FILL / BUFFER_AUTO_ON / FORCE_BUFFER_FILL
...

## Fazit
- Alle 11 Event-Quellen ergeben identischen Output.
- Keine Verhaltensaenderung zum alten Code.
- Einziger funktionaler Unterschied: Manual Feed/Retract Speed (15 statt 15/20 mm/s, bewusste Vereinheitlichung laut Spec §8 / User Q6.1).
```

- [ ] **Step 3: Jede Event-Quelle konkret durchrechnen**

Pro Event-Quelle 2-5 Zeilen Prosa mit konkreten Flags und Output-Werten. Wo Vorher und Nachher abweichen, das explizit markieren.

- [ ] **Step 4: Verifikation**

```bash
grep -c "^## " docs/superpowers/specs/2026-04-21-lll-buffer-refactor-equivalence.md
```
Expected: mindestens `11` Sections.

```bash
grep -c "| Trigger" docs/superpowers/specs/2026-04-21-lll-buffer-refactor-equivalence.md
```
Expected: mindestens `11` Tabellen-Header.

- [ ] **Step 5: Commit**

```bash
git add docs/superpowers/specs/2026-04-21-lll-buffer-refactor-equivalence.md
git commit -m "docs: add equivalence verification for refactor phase 1

Belegt, dass keine der 11 Event-Quellen anderes Verhalten zeigt
als vor dem Refactor (ausser bewusste Feed/Retract-Speed-Unity).
Spec: 2026-04-21-lll-buffer-refactor-design.md §12

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task R-15: Branch pushen (nur auf User-Freigabe)

**Files:**
- kein File-Edit, nur git-Operation

- [ ] **Step 1: Branch-Status pruefen**

```bash
git log --oneline main..rebuild-sync-v2
```
Expected: Liste aller Refactor-Commits.

```bash
git diff main..rebuild-sync-v2 --stat
```
Expected: Stats fuer lll.cfg (±X Zeilen) + neue Knowledge-Base + Specs.

- [ ] **Step 2: User-Freigabe einholen**

Vor dem Push dem User vorlegen:
- Anzahl Commits
- Neue/geaenderte Zeilen
- Kurzer Changelog aus den Commit-Messages
Erst auf explizite Freigabe pushen.

- [ ] **Step 3: Push**

```bash
git push -u origin rebuild-sync-v2
```
Expected: Neue Remote-Branch `origin/rebuild-sync-v2`.

- [ ] **Step 4: Keine PR automatisch oeffnen**

PR-Erstellung bleibt dem User vorbehalten. `gh pr create` wird hier nicht aufgerufen.

---

# Self-Review des Plans

**Spec-Coverage-Check:**
- §2 Architektur (bleibt) — kein Task noetig (Bestandsschutz)
- §3 Plattform — R-12 (Header ohne Kalico-Block)
- §4 Projektstruktur — bereits in Baseline-Commit erledigt
- §5 Section-Layout — R-13
- §6.1 `_PREPARE_INITIAL_FILL` — R-2 + R-5
- §6.2 `_ABORT_ALL_FEED_LOOPS` — R-2 + R-5
- §6.3 `_SAVE_E_MODE`/`_RESTORE_E_MODE` — R-3 + R-9
- §6.4 `_BUTTON_CLICK_HANDLER` — R-6 + R-7
- §6.5 `_APPLY_SYNC_STATE` Raw-State — R-4 + R-11
- §7 State-Container — R-3 + R-11
- §8 `_FILAMENT_VARS` Erweiterung — R-1 + R-8 + R-9
- §9 Breaking Changes (Runout rename) — R-10
- §10 Kosmetik — R-12
- §12 Validierungs-Strategie — R-14
- §13 DoD — alle Checkpoints abgedeckt

**Placeholder-Scan:** kein "TBD", "TODO", "implement later", "similar to", "add appropriate handling" im Plan. Alle Code-Bloecke vollstaendig.

**Type-Konsistenz:**
- `_PREPARE_INITIAL_FILL` definiert in R-2 (keine Args), aufgerufen in R-5 ohne Args. OK.
- `_ABORT_ALL_FEED_LOOPS` definiert in R-2 (keine Args), aufgerufen in R-5 ohne Args. OK.
- `_SAVE_E_MODE` / `_RESTORE_E_MODE` definiert in R-3, aufgerufen in R-9. OK.
- `_BUTTON_CLICK_HANDLER DIRECTION=FEED|RETRACT` definiert in R-6, aufgerufen in R-7. OK.
- `_FILAMENT_VARS`-Namen: `manual_chunk_distance`, `manual_speed`, `force_move_accel`, `manual_loop_tick`, `reenable_cooldown`, `reenable_cooldown_fast` — in R-1 eingefuehrt, in R-7/R-8/R-9 referenziert. Konsistent.
- `_BUFFER_AUTO_CONTROL.saved_e_abs` — in R-3 angelegt, in R-9 via `_SAVE_E_MODE`/`_RESTORE_E_MODE` genutzt, in R-11 im `_STATE_DUMP` angezeigt. Konsistent.

---

# Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-04-21-lll-buffer-refactor-phase1.md`. Two execution options:

**1. Subagent-Driven (recommended)** — Fuer den Refactor (R-1..R-14) dispatche ich einen frischen Subagenten pro Task mit Review zwischen den Tasks. Fuer die Knowledge-Base (KB-1..KB-4) dispatche ich alle vier parallel. Am Ende Review-Merge durch Claude.

**2. Inline Execution** — Ich arbeite den Plan Task fuer Task selbst ab, mit Checkpoint-Stopps (typisch alle 3-4 Tasks) fuer User-Review.

Welche Variante?
