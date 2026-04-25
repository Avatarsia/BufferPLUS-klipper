# Mellow LLL Plus Filament Buffer — Klipper Python-Extension

Klipper-Integration für den **Mellow LLL Plus Filament Buffer** mit einer
eigenständigen Python-Extension (`[buffer_feeder]`). Der Feeder-Stepper
läuft **entkoppelt** vom Hauptextruder — kein `SYNC_EXTRUDER_MOTION`,
keine Retract-Mirroring, keine PA-Propagation. Bang-Bang-Regelung mit
Hysterese über die drei HALL-Sensoren. Zero Toolhead-Coupling = zero
Druckkopf-Stuttering.

> **Branch-Kontext:** Dieser Branch (`python-ansatz`) ist ein
> Architektur-Rebuild gegenüber `rebuild-sync-v2` (Sync-Feedback-
> Architektur mit ±20% `rotation_distance`-Modulation). Die alte
> Config-Datei `LLL (1).cfg` wurde entfernt und durch `lll.cfg` +
> `klipper_extras/buffer_feeder.py` ersetzt.

---

## Inhalt

- [Kernidee](#kernidee)
- [Hardware](#hardware)
- [Installation](#installation)
- [Konfiguration](#konfiguration)
- [GCode-Commands](#gcode-commands)
- [Status-Felder](#status-felder)
- [Jam-Detection](#jam-detection)
- [LOAD / UNLOAD-Flow](#load--unload-flow)
- [Kalibrierung](#kalibrierung)
- [Fehlerbehebung](#fehlerbehebung)
- [Architektur-Details](#architektur-details)
- [Risiken + Grenzen](#risiken--grenzen)
- [Firmware flashen](#firmware-flashen)
- [Danksagungen](#danksagungen)
- [Lizenz](#lizenz)

---

## Kernidee

Der Buffer hat drei optische HALL-Sensoren, einen Filament-Eingangs-
Sensor und zwei manuelle Taster. Die Python-Extension besitzt den
Feeder-Stepper exklusiv und fährt ihn über eine **eigene trapq**
(Trapezoidal-Move-Queue), völlig unabhängig vom Haupt-Motion-Planner.

| Sensor | Position         | Wirkung                                    |
|--------|------------------|--------------------------------------------|
| HALL3  | unten (leer)     | aktiv → Feeder startet (Bang-Bang-an)      |
| HALL2  | oben (voll)      | aktiv → Feeder stoppt                       |
| HALL1  | ganz oben (over) | aktiv → Stepper SOFORT disable + Lockout    |

Zwischen HALL3 und HALL2 hält der Feeder seinen letzten Zustand
(Hysterese). Der Druckkopf bleibt während **jeder** Feeder-Operation
ungestört — auch während Retracts, Pressure-Advance-Moves oder langer
Dauer-Feeds.

---

## Hardware

- **Mellow LLL Filament Buffer Plus** (STM32F072, TMC2208)
- Feeder-Stepper: Pancake-Motor, `gear_ratio: 50:17`, 1:1-kalibrierter
  `rotation_distance`
- 3 optische Photo-Interrupter: HALL1 (PB2), HALL2 (PB3), HALL3 (PB4)
- 1 Filament-Switch am Eingang: `buffer_entrance` (PB7)
- 2 Taster: Feed (PB12), Retract (PB13)

Die Pin-Namen `HALL1/2/3` sind Legacy-Konvention aus der Upstream-
Firmware; es sind **optische** Sensoren, keine Hall-Effekt-Chips.

---

## Installation

### Voraussetzungen

- **Mainline Klipper** (letzte Version; die Extension nutzt
  `klippy/extras/motion_queuing.py`, das relativ neu ist).
- Klipper-Verzeichnis wird standardmäßig unter `~/klipper/` erwartet.

### Schritte

```bash
# 1. Repo auf dem Drucker-Host clonen (z.B. Raspberry Pi)
cd ~
git clone https://github.com/Avatarsia/BufferPLUS-klipper.git
cd BufferPLUS-klipper
git checkout python-ansatz

# 2. Interaktiver Installer
#    - legt einen Symlink für die Python-Extension nach klippy/extras/ an
#    - kopiert lll.cfg nach printer_data/config/ (Mainsail-editierbar; bei
#      späteren Repo-Updates zeigt der Installer einen Diff und lässt dich
#      wählen, ob du deine Version behalten oder die Repo-Version
#      übernehmen willst)
#    - hängt [include lll.cfg] in die printer.cfg
#    - registriert moonraker update_manager (optional)
./install.sh

# 3. In printer.cfg muss zusätzlich vorhanden sein (nicht automatisch ergänzt):
#    [pause_resume]
#    [extruder]
#      max_extrude_only_distance: 200

# 4. Klipper neu starten
sudo systemctl restart klipper

# 6. Verifizieren
#    Klipper-Konsole:
#      BUFFER_STATE_DUMP
#    → sollte state=IDLE plus alle Sensor-Zustände ausgeben.
```

### Moonraker Auto-Update (optional)

In `moonraker.conf` ergänzen:

```ini
[update_manager buffer_feeder]
type: git_repo
path: ~/BufferPLUS-klipper
origin: https://github.com/Avatarsia/BufferPLUS-klipper.git
primary_branch: python-ansatz
is_system_service: False
managed_services: klipper
```

---

## Konfiguration

Alle Parameter leben im `[buffer_feeder mellow]`-Block in `lll.cfg`.
Sinnvolle Defaults sind gesetzt — Pflicht-Kalibrierwerte sind mit **!!**
markiert.

| Gruppe | Parameter | Default | Bedeutung |
|---|---|---|---|
| **Stepper** | `rotation_distance` **!!** | 18.86 | 1:1-kalibriert, siehe [Kalibrierung](#kalibrierung) |
| **Speeds** | `feed_speed` | 30 mm/s | Bang-Bang Auto-Refill |
| | `manual_speed` | 15 mm/s | Taster Dauerlauf |
| | `burst_speed` | 50 mm/s | Triple-Click Burst |
| | `load_fast_speed` | 50 mm/s | LOAD Phase 1+3 |
| | `load_slow_speed` | 5 mm/s | LOAD Phase 3 (Extruder-Push ins Hotend) |
| | `grip_speed` | 55 mm/s | Initial-Grip |
| | `accel` | 1000 mm/s² | Feeder-Beschleunigung |
| **Distanzen** | `manual_chunk_distance` | 10 mm | 2-Klick-Puls-Distanz |
| | `burst_distance` | 1300 mm | Triple-Click Retract-Burst |
| | `grip_duration` | 10 s | Initial-Grip-Dauer |
| | `load_fast_distance` **!!** | 1000 mm | Kalibriert via MEASURE_LOAD_START |
| | `load_slow_distance` **!!** | 100 mm | Heatbreak-Push + Nozzle-Purge |
| | `load_buffer_max` | 2000 mm | LOAD Phase 3 Timeout |
| | `unload_sync_distance` **!!** | 250 mm | Muss ≥ load_slow_distance; Extruder-Anlauf zum sicheren Heatbreak-Austritt |
| | `unload_fast_max` | 2510 mm | UNLOAD Phase 3 Polling-Timeout |
| **Safety** | `max_feed_time` | 60 s | Max Dauerfeed ohne HALL2 |
| | `max_feed_distance` | 3000 mm | Alternative Grenze |
| | `hall_debounce_ms` | 50 | Sensor-Debounce |
| | `lead_time` | 0.3 s | Move-Scheduling-Lead |
| **Jam** | `jam_detection_enabled` | 1 | 0 deaktiviert komplett |
| | `jam_clog_dwell_time` | 60 s | HALL2-Dauer für Clog-Detektion |
| | `jam_clog_extrude_min` | 30 mm | Mindest-Extrusion in der Zeit |
| | `jam_supply_dwell_time` | 120 s | HALL3-Dauer für Supply-Jam |
| | `jam_action` | PAUSE | Action bei Jam |
| **Runout** | `runout_pause` | 0 | 0=externer Sensor, 1=intern PAUSE |
| | `runout_follow_mm` | 100 mm | Nachlauf bei runout_pause=0 |
| **Button/UI** | `triple_click_window` | 1.5 s | Max Zeit zwischen 3 Klicks |
| | `feed_burst_enabled` | 0 | 1 = Feed-Taster 3x = Burst |
| | `reenable_cooldown` | 1.0 s | Nach Manual-Op bis AUTO |
| | `reenable_cooldown_fast` | 0.5 s | Nach Burst |
| | `display_status_enabled` | 1 | M117 an/aus |
| **Initial-Fill** | `auto_load_after_follow` | 0 | 1 = nach Grip LOAD falls heiß |
| | `min_temp` | 180 °C | LOAD/UNLOAD Hotend-Check |

---

## GCode-Commands

Alle Commands werden direkt von der Extension bereitgestellt und können
in der Konsole oder aus Macros aufgerufen werden.

### Basis-Steuerung

| Command | Beschreibung |
|---|---|
| `BUFFER_FEED DISTANCE=<mm> SPEED=<mm/s>` | Feeder vorwärts. Ohne DISTANCE: Dauerlauf bis `BUFFER_HALT`. |
| `BUFFER_RETRACT DISTANCE=<mm> SPEED=<mm/s>` | Feeder rückwärts. |
| `BUFFER_HALT` | Sofort stoppen. |
| `BUFFER_AUTO_ON` | Bang-Bang aktivieren. Verweigert sich während Druck-PAUSE (`bang_bang_suspended=True`) — zuerst RESUME oder AUTO_OFF ausführen. |
| `BUFFER_AUTO_OFF` | Bang-Bang aus, State → IDLE. Full-Reset inkl. `bang_bang_suspended`-Clear (Operator-Override, falls RESUME nie kommt). Armt `halt_requested` → wartende Macros abortiert. Setzt `auto_off_by_user` → Reinsert triggert keinen automatischen Grip mehr. |
| `BUFFER_WAIT_IDLE` | Blockt bis Move fertig **und** State nicht mehr in einer LOAD/UNLOAD/GRIP-Phase. Raised auf OVERFLOW/JAM/HALT. |
| `BUFFER_STATE_DUMP` | Vollständigen State (inkl. Recovery-Flags) in Konsole. |
| `BUFFER_CLEAR_JAM` | Nach Jam-Event und Operator-Check: State → AUTO (falls entrance) oder IDLE (sonst). Restauriert GCode-State (E-Mode) aus failed LOAD/UNLOAD. Während pausiertem Druck bleibt Bang-Bang suspended bis RESUME. |
| `BUFFER_RESTORE_STATE` | Best-Effort Restore des vollen GCode-States (E-Mode, Position, Feedrate etc.) nach einem abgebrochenen LOAD_FILAMENT / UNLOAD_FILAMENT. Basiert auf Klippers `SAVE_GCODE_STATE` / `RESTORE_GCODE_STATE MOVE=0` unter `NAME=buffer_feeder_op`. Single-Shot (verbraucht den Save). No-op wenn kein Save anstehend. |

### LOAD/UNLOAD — Phasen-Primitive

| Command | Beschreibung |
|---|---|
| `BUFFER_LOAD_PHASE1 DISTANCE=<mm>` | Feeder allein schnell zum Toolhead (blocking). |
| `BUFFER_LOAD_PHASE2 DISTANCE=<mm> SPEED=<mm/s>` | Feeder parallel (non-blocking). |
| `BUFFER_LOAD_PHASE3` | Feed bis HALL2 aktiv (blocking). |
| `BUFFER_UNLOAD_PHASE1` | Feeder sauber anhalten, State → `UNLOAD_PHASE_1` für Tip-Forming (blocking). |
| `BUFFER_UNLOAD_PHASE2 DISTANCE=<mm> SPEED=<mm/s>` | Feeder retract parallel (non-blocking). |
| `BUFFER_UNLOAD_PHASE3` | Chunked retract bis entrance frei (blocking). |

Diese Primitive werden von den Macros `LOAD_FILAMENT` / `UNLOAD_FILAMENT`
orchestriert. Direkter Aufruf nur für Debug.

### Lifecycle

| Command | Beschreibung |
|---|---|
| `LOAD_FILAMENT` | Komplette Ladesequenz (3 Phasen). Hotend-Check. |
| `UNLOAD_FILAMENT` | Tip-Forming + Sync-Retract + Feeder-Rückzug. |
| `FORCE_BUFFER_FILL` | Full Initial-Fill-Cycle: Grip-Phase, dann Continuous-Feed bis HALL2 aktiv (Buffer voll) → AUTO. Cleart `auto_off_by_user`, sodass die Bang-Bang-Regelung danach weiterläuft. Refused während Druck-PAUSE. |
| `STOP_BUFFER_FILL` | Alles abbrechen, zurück in IDLE. |

### Kalibrierung

| Command | Beschreibung |
|---|---|
| `MEASURE_LOAD_START` | Feed-Taster in Toggle-Mode (1. Klick=start, 2. Klick=stop+Ausgabe). |
| `MEASURE_LOAD_STOP` | Manuell beenden (auch ohne 2. Klick). |
| `CALIBRATE_FEEDER_SYNC` | **Deprecated** — kein Sync mehr. Gibt Info aus. |

### Runout-Flags

| Command | Beschreibung |
|---|---|
| `ENABLE_RUNOUT_SENSOR` | `print_running=1` — in PRINT_START einbinden. |
| `DISABLE_RUNOUT_SENSOR` | `print_running=0` — in PRINT_END einbinden. |

---

## Status-Felder

Zugriff aus Macros via `printer["buffer_feeder mellow"].<feld>`:

| Feld | Typ | Bedeutung |
|---|---|---|
**Live State:**

| Feld | Typ | Bedeutung |
|---|---|---|
| `state` | str | INIT / IDLE / INITIAL_GRIP / AUTO / MANUAL_FEED / MANUAL_RETRACT / LOAD_PHASE_1..3 / UNLOAD_PHASE_1..3 / OVERFLOW / RUNOUT / JAM |
| `hall_empty` | bool | HALL3 aktiv (Buffer leer) |
| `hall_full` | bool | HALL2 aktiv (Buffer voll) |
| `hall_overflow` | bool | HALL1 aktiv (Überlauf) |
| `entrance_detected` | bool | Filament am Eingang |
| `feed_button_pressed` | bool | Live-Status |
| `retract_button_pressed` | bool | Live-Status |
| `continuous_feed` | bool | Extension fährt gerade Dauerfeed |
| `feed_direction` | int | +1/-1/0 |
| `feed_distance_acc_mm` | float | Akkumulierte Distanz im aktuellen Dauerfeed (Safety) |
| `total_accumulated_mm` | float | Lifetime-Counter |
| `commanded_pos_mm` | float | Internal position tracking |
| `print_running` | bool | Druck aktiv (aus idle_timeout) |
| `jam_active` | bool | Jam erkannt, Lockout scharf |
| `bang_bang_suspended` | bool | Bang-Bang pausiert (Druck-PAUSE bis RESUME) |
| `halt_requested` | bool | HALT/STOP_BUFFER_FILL/AUTO_OFF hat Abort armiert |
| `runout_follow_active` | bool | runout_pause=0 Nachlauf-Timer läuft |
| `runout_recovery_pending` | bool | Reinsert nach RUNOUT armiert — nächstes RESUME triggert grip+fill |
| `measure_load_active` | bool | MEASURE_LOAD-Modus |
| `measure_load_distance_mm` | float | Im Mess-Modus gefördert |
| `macro_state_saved` | bool | `buffer_feeder_op` GCode-State Save liegt an (konsumierbar via `BUFFER_RESTORE_STATE` / `BUFFER_CLEAR_JAM` / `AUTO_OFF` / `STOP_BUFFER_FILL` / RESUME) |

**Config-Werte (für Macro-Delegation):**

| Feld | Typ | Bedeutung |
|---|---|---|
| `feed_speed`, `manual_speed`, `burst_speed` | float | mm/s — Auto/Manual/Burst |
| `load_fast_speed`, `load_slow_speed`, `unload_fast_speed` | float | mm/s |
| `load_fast_distance`, `load_slow_distance`, `load_buffer_max` | float | mm — LOAD-Distanzen |
| `unload_sync_distance`, `unload_fast_max` | float | mm — UNLOAD-Distanzen |
| `min_temp`, `accel` | float | °C / mm/s² |

---

## Jam-Detection

Zwei Szenarien werden überwacht, beide nur aktiv in States AUTO und
LOAD_PHASE_3:

**Typ 1 — Nozzle-Clog (Konsum-Seite)**
- Signal: HALL2 aktiv UND Extruder extrudiert weiter (`last_position` wächst)
- Threshold: HALL2 aktiv ≥ `jam_clog_dwell_time` (60s) UND Extruder-Progress
  ≥ `jam_clog_extrude_min` (30mm) in dieser Zeit
- Aktion: PAUSE + M117 + M118 "Nozzle clog suspected"

**Typ 2 — Supply-Jam (Versorgung)**
- Signal: HALL3 aktiv UND Feeder läuft vorwärts
- Threshold: HALL3 aktiv ≥ `jam_supply_dwell_time` (120s) bei aktivem Feeder
- Aktion: PAUSE + M117 + M118 "Spool/supply jam suspected"

Nach einem Jam-Event: Operator prüft, behebt, dann `BUFFER_CLEAR_JAM`
oder RESUME (das idle_timeout-Event führt Extension wieder in AUTO).

Deaktivieren mit `jam_detection_enabled: 0` in der Config.

---

## LOAD / UNLOAD-Flow

### LOAD_FILAMENT (sensor-driven)

```
Phase 1: BUFFER_LOAD_PHASE1
         Feeder allein schnell (load_fast_speed) load_fast_distance mm
         bis kurz vor den Toolhead.
Phase 2: BUFFER_LOAD_PHASE3
         Feeder füllt den Buffer bis HALL2 auslöst (oder load_buffer_max).
         HALL2 = Sensor-Bestätigung dass das Filament gestaged ist
         und der Pfad bis zum Toolhead frei.
Phase 3: G1 E{load_slow_distance} F{load_slow_speed*60}, M400
         Extruder schiebt das Filament durchs Heatbreak ins Hotend.
         Der eigentliche Lade-Schritt am Druckkopf — Feeder steht.
```

`BUFFER_LOAD_PHASE2` (parallel feeder+extruder) bleibt als G-Code-
Befehl für andere Macros verfügbar, wird vom neuen `LOAD_FILAMENT`
aber nicht mehr genutzt.

### UNLOAD_FILAMENT

```
Phase 1: Tip-Forming — Extruder macht Push/Pull-Zyklen ALLEIN.
         BUFFER_UNLOAD_PHASE1 setzt den State auf UNLOAD_PHASE_1,
         hält alle Moves auf dem Feeder an und wartet bis die
         letzte Chunk-Bewegung ausgelaufen ist. In diesem State
         sind Operator-Buttons und FORCE_BUFFER_FILL blockiert —
         keine Kollision mit Tip-Forming.
         Abweichung zur alten Config: früher lief der Feeder mit;
         jetzt bleibt er still (die kleinen Moves im Hotend
         werden vom Buffer absorbiert).
Phase 2: BUFFER_UNLOAD_PHASE2 DISTANCE=180 + G1 E-180 parallel.
Phase 3: BUFFER_UNLOAD_PHASE3 — chunked 50mm-Retracts bis
         buffer_entrance frei meldet (max unload_fast_max).
```

---

## Kalibrierung

### rotation_distance (1:1)

1. Filament eingelegt, Hotend auf Drucktemperatur.
2. `BUFFER_AUTO_OFF` in der Konsole.
3. Markierung am Filament **direkt vor dem Feeder-Eingang** anbringen.
4. `BUFFER_FEED DISTANCE=100 SPEED=5` — fördert 100 mm.
5. Am Markierung nachmessen: wie viel Filament ging durch?
6. Neue `rotation_distance` = alte × (gemessen / 100).
7. In `[buffer_feeder mellow]` eintragen, Klipper-Restart.
8. Wiederholen bis Abweichung < 1 mm.

### load_fast_distance

> **ACHTUNG:** Hotend KALT lassen!

1. Filament vollständig aus dem System entfernen.
2. `FORCE_BUFFER_FILL` (oder frisch einstecken) — Grip-Phase läuft.
3. Nach ~10s: `MEASURE_LOAD_START`.
4. Feed-Taster 1x drücken — Feeder läuft.
5. Warten bis Filamentspitze am Toolhead erscheint.
6. Feed-Taster erneut drücken — Ausgabe in Konsole.
7. Gemessenen Wert (minus 10-20 mm Sicherheits-Puffer) in `load_fast_distance` eintragen.

---

## Fehlerbehebung

### Vorgehen bei einem Crash / Shutdown

Klipper öffnet `klippy.log` beim Start im Truncate-Modus — jeder Neustart
überschreibt das alte Log. Wenn ein Shutdown auftritt, **zuerst das Log
sichern**, bevor irgendein Restart ausgelöst wird (weder „FIRMWARE_RESTART"
noch `sudo systemctl restart klipper`, kein Reboot):

```bash
# 1) Log sofort wegkopieren
sudo cp /var/log/klipper_logs/klippy.log /tmp/klippy_crash_$(date +%H%M).log

# 2) Python-Traceback extrahieren (die Shutdown-Meldung in Mainsail ist
#    nur der Epilog — der eigentliche Stacktrace steht im Log davor)
CR=$(ls -1t /tmp/klippy_crash_*.log | head -1)
grep -n -B3 -A50 "Traceback\|flush_handler\|Invalid sequence\|Shutdown" "$CR" | tail -300
```

Wichtige Marker im Log:

- **`MCU 'X' shutdown: Command request`** — der Host hat den MCU
  heruntergefahren (nicht MCU-seitig entstanden). Der auslösende
  Python-Fehler steht vor dem Shutdown.
- **`Traceback (most recent call last)`** direkt vor dem Shutdown-
  Block — das ist die Ursache.
- **`stepcompress o=X i=Y c=Z a=W: Invalid sequence`** — Step-Generierung
  hat eine ungültige Sequenz produziert. Siehe „Exception in flush_handler"
  unten.
- **`Filament Sensor ... runout event detected`** — separater Sensor
  (nicht unser HALL), löst meist das user-eigene Runout-Macro aus.

Erst nach Log-Sicherung den Drucker wieder starten.

### Extension lädt nicht / Klipper-Error beim Start

- Logs prüfen: `tail -f ~/printer_data/logs/klippy.log`
- Symlink verifizieren: `ls -la ~/klipper/klippy/extras/buffer_feeder.py`
- Klipper-Version: Extension benötigt `motion_queuing` — das ist in
  aktuellen Mainline-Versionen vorhanden, aber nicht in älteren.
  Update mit: `cd ~/klipper && git pull && sudo systemctl restart klipper`

### BUFFER_STATE_DUMP zeigt falsche Sensor-States

- Debounce-Zeit zu kurz für dein Setup: `hall_debounce_ms: 100` setzen.
- Pin-Invertierung prüfen: `^!` im Config-Block muss bleiben (optische
  Sensoren mit Pullup + Invertierung).

### Feeder läuft in die falsche Richtung

- `dir_pin` im Config invertieren (prefix `!` hinzufügen oder entfernen).

### HALL1 triggert sofort nach Reset

- Mechanischer Stau im Buffer — manuell durchprüfen.
- `burst_distance` reduzieren.

### Jam-Detection gibt Fehlalarme

- `jam_clog_dwell_time` erhöhen (z.B. 120 s bei langsamen Drucken).
- `jam_supply_dwell_time` ebenfalls erhöhen.
- Oder komplett aus: `jam_detection_enabled: 0`.

### Taster reagieren nicht

- Verkabelung zu PB12 (Feed) / PB13 (Retract) prüfen.
- `BUFFER_STATE_DUMP` beobachten während drückens — `feed_button_pressed`
  sollte `True` werden.
- Während LOAD/UNLOAD/OVERFLOW/JAM sind Taster blockiert (by design).

### „Exception in flush_handler" / Shutdown beim ersten Feed

Symptom: alle MCUs shutdownen gleichzeitig mit `Command request`. Im
Log steht `stepcompress o=X i=0 c=N a=0: Invalid sequence` kurz vor
dem Shutdown.

Ursache (historisch, Fix ab Commit `52a5ba6`): stepcompress initialisiert
`last_step_clock = 0` und bleibt dort, bis der erste Step emittiert
wird. Wenn der Drucker vor dem ersten Buffer-Move mehr als ~17s
idle ist (Klippers `CLOCK_DIFF_MAX`), überschreitet der erste Step
die uint32-Interval-Grenze, der Bisect-Compressor erzeugt eine
degenerate Sequenz und `check_line()` rejected sie.

Fix ist in der Extension eingebaut:

- `stepper.set_position((0,0,0))` wird einmalig vor dem ersten Move
  aufgerufen (primed stepcompress).
- Zeitbasis ist `toolhead.get_last_move_time()` statt
  `mcu.estimated_print_time()` — das resynct intern gegen die aktuelle
  MCU-Clock.

Sollte der Fehler trotz aktueller Version wieder auftreten: Log
sichern (siehe oben), `BUFFER_STATE_DUMP` vor dem Crash prüfen,
Issue öffnen mit Traceback + Config-Snippet.

---

## Architektur-Details

### Warum eigene trapq?

Klipper hat einen zentralen Toolhead-Motion-Planner mit einer Lookahead-
Queue. Jede Operation, die einen Stepper zwischen Queues umhängt
(`SYNC_EXTRUDER_MOTION`, `FORCE_MOVE`, `MANUAL_STEPPER MOVE`), ruft
`toolhead.flush_step_generation()` — das leert die Lookahead-Queue
synchron und unterbricht sichtbar die Druckkopf-Bewegung.

Eine **eigene trapq** (wie Klipper's `manual_stepper` sie anlegt) wird
vom Background-Flusher separat bedient. Steps werden generiert, ohne
die Toolhead-Queue zu konsultieren.

**Zeitbasis:** die Extension nutzt `toolhead.get_last_move_time()` als
t0-Anker (wie auch `manual_stepper` und `force_move` es tun). Diese
Funktion flusht **ausschließlich** den Lookahead-Planner — sie drained
**nicht** die MCU-Step-Queue (das wäre `flush_step_generation()`, was
wir explizit vermeiden). Der Lookahead-Flush passiert im Druckbetrieb
sowieso ständig; zusätzlicher Overhead ist minimal.

Warum nicht einfach `mcu.estimated_print_time(reactor.monotonic())`?
Weil stepcompress' interne `last_step_clock` bis zum ersten Step auf
0 bleibt und Klippers `CLOCK_DIFF_MAX` bei ~17s liegt. Ein erster Move
nach langem Idle würde ein Interval > uint32 erzeugen → „Invalid
sequence" → Shutdown. `toolhead.get_last_move_time()` resynct intern
gegen `estimated_print_time + BUFFER_TIME_START` bzw.
`motion_queuing.calc_step_gen_restart()` — das Ergebnis liegt immer
im Zeitraum, den die Step-Gen-Maschinerie tracken kann.

Zusätzlich wird beim allerersten Move `stepper.set_position((0,0,0))`
aufgerufen (primed itersolve + gibt stepcompress einen Clock-Baseline),
gleiches Muster wie `force_move.manual_move`.

### Move-Submit-Pfad

```python
# Priming nur beim allerersten Move
if not self._stepcompress_primed:
    self.stepper.set_position((0., 0., 0.))
    self._stepcompress_primed = True

toolhead = printer.lookup_object('toolhead')
th_time = toolhead.get_last_move_time()   # nur Lookahead-Flush
t0 = max(th_time + lead_time, last_move_end_time)
trapq_append(own_trapq, t0, accel_t, cruise_t, decel_t, ...)
motion_queuing.note_mcu_movequeue_activity(t0 + total_time)
```

Keine `flush_step_generation()`-Aufrufe. Background-Flusher generiert
Steps, MCU führt sie parallel zu den Toolhead-Steps aus. Der
Lookahead-Flush unterbricht den Toolhead-Motion-Planner nicht sichtbar —
Retracts/PA-Moves laufen durch, als wäre der Buffer nicht da.

### Sensor-Polling

Über `buttons.register_buttons()` registriert die Extension Callbacks
auf allen sechs Pins (3×HALL, entrance, 2×Taster). Debouncing (50ms
default) erfolgt im Main-Tick bei 50 Hz.

### State-Machine

Explizite States (INIT, IDLE, INITIAL_GRIP, AUTO, MANUAL_FEED,
MANUAL_RETRACT, LOAD_PHASE_1-3, UNLOAD_PHASE_1-3, OVERFLOW, RUNOUT,
JAM). Transitions sind im Main-Tick oder in Event-Handlern getriggert.
HALL1-Overflow hat absolute Priorität über allem.

---

## Risiken + Grenzen

| Risiko | Mitigation |
|---|---|
| `motion_queuing`-API ist interne Klipper-API (nicht stabilisiert) | Bei jedem Klipper-Update testen |
| Keine Prior Art in Klipper-Community | Spec in `docs/superpowers/specs/2026-04-23-python-ansatz-design.md` dokumentiert alle Design-Entscheidungen |
| `toolhead.get_last_move_time()` flusht Lookahead-Planner | Nur Lookahead, **kein** Step-Gen-Drain — im Druck läuft das sowieso ständig, Overhead vernachlässigbar |
| Lead-Time-Fehlkalibrierung | `lead_time` konfigurierbar, Default 0.3s |
| Bug in Reactor-Logik → Endlosfeed | HALL1 Hard-Stop + `max_feed_time` / `max_feed_distance` |

**Bekannte Grenzen:**
- Tip-Forming läuft ohne Feeder-Mitlauf (Verhaltens-Änderung).
- Keine persistente Kalibrierung via `save_variables`.
- Single-Buffer only.
- Kein Encoder-/Stallguard-Slip-Detection.

---

## Firmware flashen

Der Abschnitt ist unverändert vom alten README. Siehe die originale
Version oder folgende Quick-Reference:

1. **Katapult-Bootloader** (empfohlen) flashen via DFU:
   ```bash
   cd ~/katapult && make menuconfig   # STM32F072, 8KiB offset, USB
   make clean && make
   sudo dfu-util -a 0 -D out/katapult.bin --dfuse-address 0x08000000:force:mass-erase:leave -d 0483:df11
   ```
2. **Klipper-Firmware** mit 8KiB-Offset bauen und flashen:
   ```bash
   cd ~/klipper && make menuconfig   # STM32F072, 8KiB bootloader, USB
   make clean && make
   python3 ~/katapult/scripts/flashtool.py -f out/klipper.bin -d /dev/serial/by-id/usb-katapult_stm32f072xb_*
   ```
3. In `lll.cfg` unter `[mcu LLL_PLUS]` die `serial:` auf das eigene
   `/dev/serial/by-id/...`-Device setzen.

---

## Danksagungen

- Original Klipper-Konfiguration von [@ss1gohan13](https://github.com/ss1gohan13)
  für den Mellow LLL Filament Plus Buffer — diese Python-Extension-
  Variante baut darauf auf.
- Hardware + Original-Firmware von [Mellow 3D](https://github.com/mellow-3d)
  und [Fly3DTeam](https://github.com/Fly3DTeam/Buffer).
- [Happy Hare](https://github.com/moggieuk/Happy-Hare) als Referenz
  für Klipper-Extension-Patterns.
- [Arksine](https://github.com/Arksine) für Katapult.
- Klipper-Team.

---

## Lizenz

MIT-Lizenz — freie Verwendung und Modifikation erlaubt.
