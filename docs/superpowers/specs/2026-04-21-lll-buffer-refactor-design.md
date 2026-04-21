# LLL Buffer Plus Klipper Config Refactor — Design

**Datum:** 2026-04-21
**Branch:** `rebuild-sync-v2`
**Basis:** `printer_data/config/lll.cfg` (Variante 2 Sync-Feedback, 736 Zeilen)
**Ziel:** Altlasten aufraeumen, Struktur schaerfen — Verhalten (nach aussen) identisch.

## 1. Kontext

Der Mellow LLL Filament Buffer Plus ist physisch verbaut und laeuft am Drucker.
Die aktuelle Config (`lll.cfg`) ist ueber mehrere Iterationen entstanden und
enthaelt redundante Pfade, Magic Numbers und zwei unterschiedliche Konventionen
fuer Hall-State-Abfragen (Mirror-Variables + Raw-State-Query nebeneinander).

Der Refactor ist **Phase 1** einer mehrphasigen Entwicklung. Phase 2 (neue
Features) kommt nach Abschluss dieser Phase.

## 2. Architektur-Entscheidung (bleibt)

**Variante 2 — Happy-Hare-Style Sync-Feedback** bleibt unveraendert:
- Der Feeder-Stepper ist permanent via `SYNC_EXTRUDER_MOTION` an den
  Hauptextruder gekoppelt.
- Buffer-Regelung erfolgt ueber **dynamische `rotation_distance`-Modulation**
  (nominal ± `sync_modulation` = ±20%).
- Hall-Sensoren aendern nur `rotation_distance`, keine eigenstaendige
  Feed-Queue.
- `FORCE_MOVE` nur ausserhalb des Drucks (Initial-Grip/Follow, manuelle
  Taster, LOAD/UNLOAD Phase 1+3).

**Warum:** Ohne permanenten Sync pausiert der Druckkopf bei jedem
Buffer-Feed (Toolhead-Flush). Das ist fuer den Anwender ein hartes No-Go.
FORCE_MOVE-basierte Architekturen (wie die alte `mellow-plus.cfg`) sind
damit ausgeschlossen.

## 3. Plattform-Festlegung

- **Klipper-Fork:** Mainline (`Klipper3d/klipper`), nicht Kalico.
- **mux-Key:** `EXTRUDER=` in `SYNC_EXTRUDER_MOTION`.
- **Kein** Kalico-sed-Kompatibilitaetsblock mehr im Header.

## 4. Datei- und Projektstruktur

```
LLL Plus Buffer/                     ← Git-Repo-Root (Avatarsia Fork)
├── .git/                            ← origin=Avatarsia, upstream=ThaatGuy
├── .gitignore                       ← schliesst LLL (1).cfg aus
├── LICENSE
├── README.md
├── docs/
│   ├── knowledge/                   ← Knowledge-Base (Hardware, Klipper-APIs)
│   └── superpowers/specs/           ← diese Datei
└── printer_data/
    └── config/
        ├── lll.cfg                  ← NEU: Arbeitsbasis, wird refactored
        └── mellow-plus.cfg          ← liegt zum Vergleich
```

**Keine Modularisierung** in diesem Refactor. `lll.cfg` bleibt eine
Datei; Aufspaltung nach Gewerken (`buffer-core.cfg`, `buffer-load.cfg`)
wird fuer eine spaetere Phase zurueckgestellt.

## 5. Neues Section-Layout innerhalb `lll.cfg`

Reihenfolge der Sections (von oben nach unten):

1. **Header** — Architektur-Kurzbeschreibung, Voraussetzungen
   (`[force_move]`, `[pause_resume]`, `max_extrude_only_distance`),
   Mainline-Klipper-Hinweis.
2. **MCU / Stepper / TMC** — `[mcu LLL_PLUS]`, `[extruder_stepper mellow]`,
   `[tmc2208 extruder_stepper mellow]`.
3. **Konfigurierbare Parameter** — `_FILAMENT_VARS` (erweitert, s. §8).
4. **Private Helper-Makros** — `_SYNC_OFF`, `_APPLY_SYNC_STATE`,
   `_PREPARE_INITIAL_FILL`, `_ABORT_ALL_FEED_LOOPS`, `_SAVE_E_MODE`,
   `_RESTORE_E_MODE`, `_BUTTON_CLICK_HANDLER`.
5. **State-Container** — `_BUFFER_AUTO_CONTROL` (Flags),
   `_TRIPLE_CLICK_STATE`.
6. **Taster** — `feed_button`, `retract_button`, `_MANUAL_FEED`,
   `_MANUAL_RETRACT`, `_TRIPLE_FEED_BURST`, `_TRIPLE_RETRACT_BURST`.
7. **Hall-Sensoren** — `buffer_hall1/2/3` (ohne Mirror-SET).
8. **Entrance-Sensor** — `buffer_entrance`.
9. **User-Facing Makros** — `ENABLE_RUNOUT_SENSOR`, `DISABLE_RUNOUT_SENSOR`,
   `FORCE_BUFFER_FILL`, `STOP_BUFFER_FILL`, `BUFFER_AUTO_ON`, `_STATE_DUMP`.
10. **Boot-Autostart** — `_boot_autostart`.
11. **Initial-Grip + Follow** — `_INITIAL_GRIP_PHASE`,
    `_initial_follow_loop`, `_initial_follow_end`.
12. **LOAD / UNLOAD** — `LOAD_FILAMENT`, `UNLOAD_FILAMENT`,
    `_UNLOAD_FAST_RETRACT`, `_unload_fast_delayed`.

## 6. Neue Helper-Makros (Spezifikation)

### 6.1 `_PREPARE_INITIAL_FILL` (privat)
Konsolidiert die identische Init-Sequenz aus `buffer_entrance.insert_gcode`
und `FORCE_BUFFER_FILL`.

```
Semantik:
  system_enabled       = 1
  manual_operation     = 0
  overfill_lock        = 0
  initial_lockout      = 1
  _APPLY_SYNC_STATE
  UPDATE_DELAYED_GCODE ID=_start_initial_grip DURATION=0.1
```

### 6.2 `_ABORT_ALL_FEED_LOOPS` (privat)
Konsolidiert die Abbruch-Sequenz aus `STOP_BUFFER_FILL` und
`buffer_hall1.release_gcode`.

```
Semantik:
  initial_lockout      = 0
  initial_follow_active= 0
  _MANUAL_FEED.active  = 0
  _MANUAL_RETRACT.active = 0
  UPDATE_DELAYED_GCODE ID=_manual_feed_loop     DURATION=0
  UPDATE_DELAYED_GCODE ID=_manual_retract_loop  DURATION=0
  UPDATE_DELAYED_GCODE ID=_initial_follow_loop  DURATION=0
  UPDATE_DELAYED_GCODE ID=_initial_follow_end   DURATION=0
```

`STOP_BUFFER_FILL` und HALL1-release rufen danach selbst
`_APPLY_SYNC_STATE`, weil die Post-Action unterschiedlich ist
(`manual_operation`-Flag).

### 6.3 `_SAVE_E_MODE` / `_RESTORE_E_MODE` (privat)
Ersetzt die doppelte `variable_e_abs`-Buchhaltung in `LOAD_FILAMENT` und
`UNLOAD_FILAMENT`.

```
_SAVE_E_MODE:
  _BUFFER_AUTO_CONTROL.saved_e_abs =
      1 if printer.gcode_move.absolute_extrude else 0
  M83

_RESTORE_E_MODE:
  if saved_e_abs: M82 else: M83
```

Hinzufuegen: `variable_saved_e_abs: 0` in `_BUFFER_AUTO_CONTROL`.
Entfernen: `variable_e_abs` aus `LOAD_FILAMENT` und `UNLOAD_FILAMENT`.

### 6.4 `_BUTTON_CLICK_HANDLER` (privat, parametriert)
Deduplifiziert die Feed-/Retract-Button-Press-Logik. Wird vom Button
via `_BUTTON_CLICK_HANDLER DIRECTION=FEED` (oder `RETRACT`) aufgerufen.

Interne Pfade:
- Klick 1 (Dauerlauf starten) → `_MANUAL_FEED` / `_MANUAL_RETRACT`.
- Klick 2 (`manual_chunk_distance` Puls) → ein einzelnes `FORCE_MOVE`.
- Klick 3 (`triple_click_distance` Burst) → `_TRIPLE_FEED_BURST` /
  `_TRIPLE_RETRACT_BURST`.

Die Parametrisierung erfolgt ueber Jinja-Dispatch in dem Handler. Preis:
ein einmaliger `{% if params.DIRECTION == 'FEED' %}`-Switch je Pfad.
Nutzen: ~60 Zeilen Redundanz weg, Aenderung an der Click-Logik
passiert kuenftig an genau einer Stelle.

### 6.5 `_APPLY_SYNC_STATE` (bestehend, geaendert)

Mirror-Variables werden durch direkte Raw-State-Abfragen ersetzt.

```jinja
{% set hall1_active = printer["gcode_button buffer_hall1"].state == "RELEASED" %}
{% set hall2_active = printer["gcode_button buffer_hall2"].state == "RELEASED" %}
{% set hall3_active = printer["gcode_button buffer_hall3"].state == "RELEASED" %}

{% if b.system_enabled == 0 or b.manual_operation == 1
      or b.initial_lockout == 1 or b.overfill_lock == 1 %}
    SYNC_EXTRUDER_MOTION EXTRUDER=mellow MOTION_QUEUE=
{% else %}
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

Kommentarblock ueber der Stelle erklaert einmal: `^!`-Invertierung →
`RELEASED == Sensor aktiv`.

## 7. State-Container `_BUFFER_AUTO_CONTROL` (geaendert)

**Entfernt:** `variable_hall1_active`, `variable_hall2_active`,
`variable_hall3_active` (Mirrors, siehe §6.5).

**Hinzugefuegt:** `variable_saved_e_abs` (fuer `_SAVE_E_MODE`).

**Gleich bleibt:** `system_enabled`, `manual_operation`, `overfill_lock`,
`print_running`, `initial_lockout`, `initial_follow_active`.

**gcode-Body:** leer. (Vorher rief es `_APPLY_SYNC_STATE`, wurde aber
nirgends via Name aufgerufen → toter Code.)

## 8. `_FILAMENT_VARS` Erweiterungen

**Neue Variablen** (Magic Numbers aus dem Code):

```
variable_manual_chunk_distance: 10   # DISTANCE fuer Manual-Feed/Retract-Loops
variable_manual_speed:          15   # VELOCITY fuer Manual (Feed+Retract vereinheitlicht)
variable_force_move_accel:    1000   # ACCEL fuer alle FORCE_MOVE
variable_manual_loop_tick:     0.1   # Tick des Manual-Loops
variable_reenable_cooldown:      1   # regulaerer Cooldown
variable_reenable_cooldown_fast: 0.5 # Cooldown nach Triple-Burst
```

**Bleibt:** alles andere (`sync_rotation_distance`, `sync_modulation`,
`fast_speed`, `slow_speed`, `min_temp`, LOAD-/UNLOAD-Distanzen,
Tip-Forming-Parameter, Initial-Grip/Follow-Parameter,
Triple-Click-Parameter).

**Hinweis zum `_boot_autostart` delay:** `initial_duration: 7.0` muss
statisch bleiben (Klipper-Einschraenkung), aber Kommentar verweist auf
eine hypothetische `boot_delay`-Variable fuer Konsistenz.

## 9. Breaking Changes (User-visible)

**Nur einer:**
- `Enable_Runout_Sensor` → `ENABLE_RUNOUT_SENSOR`
- `Disable_Runout_Sensor` → `DISABLE_RUNOUT_SENSOR`

User hat bestaetigt: diese Makros werden **nicht** aus der Haupt-
`printer.cfg`, PRINT_START oder PRINT_END gerufen. Damit ist die
Umbenennung sicher; lediglich ggf. manuelle Console-Aufrufe des Users
muessen neu geschrieben werden.

Alle anderen User-Facing-Makros (`FORCE_BUFFER_FILL`,
`STOP_BUFFER_FILL`, `BUFFER_AUTO_ON`, `LOAD_FILAMENT`,
`UNLOAD_FILAMENT`) bleiben namensstabil.

## 10. Kosmetik

- Kalico-sed-Hinweis im Header (Z. 33–48 original) wird **entfernt**.
- Unicode (`±`, Umlaute) in Kommentaren wird **durch ASCII ersetzt**
  (`+/-`, `ae/oe/ue/ss`) fuer Konsistenz mit den restlichen Kommentaren.
- Placeholder-Kommentar „19.5 ist Platzhalter" im
  `[extruder_stepper mellow]` wird auf „Initial-Wert fuer Klipper-Boot,
  Laufzeitwert in `_FILAMENT_VARS.sync_rotation_distance`" geaendert.
- Header bekommt neuen Block „Voraussetzungen in printer.cfg":
  `[force_move] enable_force_move: True`, `[pause_resume]`,
  `[extruder] max_extrude_only_distance: 250 (oder hoeher)`.

## 11. Out-of-Scope (kommt in Phase 2 / spaeter)

Bewusst **nicht** im Refactor:
- Modularisierung / Aufsplittung in mehrere `.cfg`-Dateien.
- `[save_variables]` fuer persistente Kalibrier-/Zustandsdaten.
- Display-/Web-Status-Integration.
- Auto-Kalibrier-Makro fuer `sync_rotation_distance`.
- Multi-Buffer-Faehigkeit (2+ Instanzen).
- Telemetrie / Hall-Histogramme.
- Runout-Resume statt -Pause.

## 12. Validierungs-Strategie

Ohne Drucker-Hardware in der Entwicklungsumgebung kann keine echte
Laufzeit-Verifikation stattfinden. Stattdessen:

1. **Syntax-Check:** Klipper-seitiges Parsen muss vor einem Push
   grundsaetzlich moeglich sein. Wenn eine Klipper-Instanz im Zugriff
   ist: `make check` bzw. `klippy -c lll.cfg` Dryrun.
2. **Diff-Review:** Old-vs-New-Diff wird menschlich gegen die hier
   dokumentierten Aenderungen geprueft (Subagent + Review).
3. **Semantik-Aequivalenz:** Fuer jede State-Transition im alten
   `_APPLY_SYNC_STATE`-Pfad wird im neuen Code der gleiche `MOTION_QUEUE`-
   und `rotation_distance`-Output verifiziert (Tabelle im PR-Kommentar).
4. **Integration-Test am Drucker** bleibt dem User vorbehalten, **bevor**
   der Branch nach `main` gemerged wird.

## 13. Definition of Done (Phase 1)

- [ ] `lll.cfg` refactored, alle §6–§10-Punkte umgesetzt.
- [ ] Keine Funktionsaenderung (State-Transitionen aequivalent zum Original).
- [ ] Knowledge-Base in `docs/knowledge/` angelegt (separate Aufgabe,
      parallelisiert): LLL-Buffer-Hardware, STM32F072-Pinout,
      Klipper-APIs (`SYNC_EXTRUDER_MOTION`, `SET_EXTRUDER_ROTATION_DISTANCE`,
      `FORCE_MOVE`, `filament_switch_sensor`, `gcode_button`),
      Katapult-Flash-Workflow.
- [ ] Spec (diese Datei) + finaler Refactor-Plan im Repo committed.
- [ ] Branch `rebuild-sync-v2` gepusht, bereit fuer Drucker-Tests.
- [ ] User-Approval vor Merge nach `main`.
