# LLL Buffer Plus — Python-Extension-Architektur (Variante 3)

**Datum:** 2026-04-23
**Branch:** `python-ansatz` (wird aus `rebuild-sync-v2` abgezweigt)
**Ersetzt:** Sync-Feedback-Architektur (Variante 2) mit permanentem `SYNC_EXTRUDER_MOTION`
**Target Klipper:** Mainline (letzter Release; `klippy/extras/motion_queuing.py` vorausgesetzt)

---

## 1. Kontext + Problem

### Warum der Rebuild

Die aktuelle Variante-2-Architektur koppelt den Feeder-Stepper via permanentem
`SYNC_EXTRUDER_MOTION EXTRUDER=mellow MOTION_QUEUE=extruder` an die Extruder-Motion-Queue.
Jede Extruder-Bewegung — inklusive Slicer-Retracts und Pressure-Advance-Mikroretracts —
propagiert 1:1 (moduliert ±20% via `rotation_distance`) an den Feeder.

**Konkreter Schaden:**
- Hörbarer Stepper-Chatter bei jedem Retract (Feeder reversiert mit voller Beschleunigung)
- Mechanischer Verschleiß am Pinch-Gear durch häufige Richtungswechsel
- Filament wird zwischen Feeder und Buffer-Kammer hin und her gezogen

**Warum keine inkrementelle Lösung (z.B. Firmware-Retract-Wrapping):**
Pressure-Advance propagiert unsichtbar durch die Motion-Queue — am G-Code-Layer nicht abfangbar.
Nur eine Architektur, die den Feeder vollständig von der Extruder-Queue **entkoppelt**, löst das
Problem sauber.

### Die ursprüngliche Memory-Regel

Projekt-Memory: *"permanenter `SYNC_EXTRUDER_MOTION` muss bleiben, FORCE_MOVE nur außerhalb
Druck."* Der Grund hinter der Regel war: FORCE_MOVE während Druck ruft
`toolhead.flush_step_generation()` und pausiert den Druckkopf.

**Variante 3 erfüllt den Grund der Regel, verletzt aber den Buchstaben:** Wir entfernen
`SYNC_EXTRUDER_MOTION` vollständig, aber bauen keinen FORCE_MOVE-Loop — stattdessen
ein Python-Extension mit **eigener trapq**, die den Feeder bewegt, ohne jemals
`flush_step_generation()` zu rufen. Druckkopf bleibt während des Drucks 100%
ungestört (auch besser als Variante 2 es war, weil nicht mal mehr `SET_EXTRUDER_ROTATION_DISTANCE`
aufgerufen wird).

---

## 2. Machbarkeits-Befund (Research Summary)

Recherche am 2026-04-23 auf Mainline-Klipper-Source (`master` Branch via
raw.githubusercontent.com):

### Kernergebnisse

1. **`manual_stepper.py` ist NICHT direkt als Vorbild geeignet.** Sein Move-Pfad
   (`do_move → sync_print_time`) ruft `toolhead.get_last_move_time()`, das den
   Toolhead-Lookahead synchronisiert — auch bei `SYNC=0`.

2. **Die trapq-Primitiva sind flush-frei nutzbar.** `trapq_append` +
   `motion_queuing.note_mcu_movequeue_activity` greifen nie auf die Toolhead-Queue zu.
   Background-Flusher generiert Steps aus beliebigen trapqs automatisch.

3. **Zeitbasis muss unabhängig sein.** Statt `toolhead.get_last_move_time()` wird
   `mcu.estimated_print_time(reactor.monotonic()) + LEAD_TIME` verwendet. Der
   MCU-Clock-Reader ist readonly und koppelt den Toolhead nicht.

4. **`SYNC_EXTRUDER_MOTION` ruft unbedingt `flush_step_generation()`** (in
   `kinematics/extruder.py:52`). Darf im Python-Ansatz während Druck **nie**
   aufgerufen werden — auch nicht bei LOAD/UNLOAD. LOAD Phase 2 läuft über
   zeit-koordinierte Parallel-Moves statt über Sync (Option ii aus Brainstorming).

5. **Prior Art: null.** Weder Happy Hare noch AFC-Klipper-Add-On noch TradRack
   bewegt einen echten Stepper asynchron während eines Drucks ohne Flush.
   Wir sind Pioniere.

### Der exakte flush-freie Move-Pfad

```python
# In unserer Extension:
now_pt = self.mcu.estimated_print_time(self.reactor.monotonic())
t0 = max(now_pt + self.lead_time, self._last_move_end_time)
# Trapezoidales Move-Profil berechnen (accel_t, cruise_t, decel_t)
self.trapq_append(
    self.trapq, t0, accel_t, cruise_t, decel_t,
    start_pos_x, 0., 0.,                # Position
    axis_r_x, 0., 0.,                   # Richtungsvektor
    start_v, cruise_v, accel            # Velocity-Profil
)
self._last_move_end_time = t0 + accel_t + cruise_t + decel_t
self.motion_queuing.note_mcu_movequeue_activity(self._last_move_end_time)
```

Kein `toolhead.*`-Aufruf im Move-Pfad. MCU kriegt die Steps via Background-Flusher
parallel zu den Toolhead-Steps.

---

## 3. Architektur-Übersicht

```
╔═════════════════════════════════════════════════════════════════╗
║  Haupt-Toolhead-Queue (X/Y/Z + Extruder)                        ║
║  ──────────────────────────────────────────                     ║
║  Niemals angefasst durch unsere Extension.                      ║
║  Druckkopf läuft vollkommen ungestört.                          ║
╚═════════════════════════════════════════════════════════════════╝
                              │
                              │ readonly:
                              │   printer.extruder.last_position
                              │   printer.print_stats.filament_used
                              ▼
╔═════════════════════════════════════════════════════════════════╗
║  [buffer_feeder mellow] — unsere Python-Extension               ║
║  ────────────────────────────────────────────                   ║
║                                                                 ║
║  Reactor-Timer @ 50 Hz:                                         ║
║    1. poll HALL1/2/3 + buffer_entrance states                   ║
║    2. debounce (50ms)                                           ║
║    3. state-machine tick → entscheidet aktuellen Modus          ║
║    4. je nach Modus: queue moves ODER leg still                 ║
║    5. jam-detection timer update                                ║
║                                                                 ║
║  GCode-Commands (registriert):                                  ║
║    BUFFER_FEED / BUFFER_RETRACT / BUFFER_HALT                   ║
║    BUFFER_AUTO_ON / BUFFER_AUTO_OFF                             ║
║    BUFFER_LOAD_PHASE1 / BUFFER_LOAD_PHASE2 / BUFFER_LOAD_PHASE3 ║
║    BUFFER_UNLOAD_FAST_RETRACT  (chunked polling bis entrance)   ║
║    BUFFER_STATE_DUMP  (Diagnose)                                ║
║    CALIBRATE_FEEDER_SYNC  (legacy-Compat; tut im neuen          ║
║       Ansatz nur rotation_distance-Preset-Check)                ║
║    MEASURE_LOAD_START / MEASURE_LOAD_STOP                       ║
║                                                                 ║
║  Button-Handler:                                                ║
║    feed_button / retract_button — Triple-Click-Logik in Python  ║
║                                                                 ║
║  Owns:                                                          ║
║    • Feeder-Stepper (PrinterStepper + eigene trapq)             ║
║    • TMC2208 config (unverändert übernommen)                    ║
║    • HALL1/2/3-Pin-Polling                                      ║
║    • buffer_entrance-Pin-Polling                                ║
║    • feed_button / retract_button-Pin-Polling                   ║
╚═════════════════════════════════════════════════════════════════╝
                              │
                              │ trapq_append / note_mcu_movequeue_activity
                              ▼
                    ┌──────────────────────┐
                    │ Feeder-eigene trapq  │   ← separate Queue
                    │ (Background-Flusher  │     keine Toolhead-Kopplung
                    │  generiert steps)    │
                    └──────────────────────┘
                              │
                              ▼
                    ┌──────────────────────┐
                    │ MCU (STM32F072)      │   ← kombiniert alle Step-
                    │ LLL_PLUS             │     streams auf Hardware-
                    │                      │     Ebene
                    └──────────────────────┘
```

### Zentrale Designprinzipien

- **Single Owner:** Der Feeder-Stepper gehört ausschließlich der Extension. Keine
  Macros rufen direkt FORCE_MOVE oder SYNC_EXTRUDER_MOTION auf den Feeder.
- **Flush-Frei:** Kein Pfad in der Extension darf `toolhead.flush_step_generation()`,
  `toolhead.wait_moves()`, oder `toolhead.get_last_move_time()` aufrufen — außer
  optional als einmaliger Init-Sync beim Klipper-Ready.
- **Sensor-driven:** Bang-Bang-Logik mit Hysterese läuft im Reactor-Timer, nicht
  in Klipper-Macros.
- **Macros sind dünn:** LLL.cfg enthält nur noch Workflow-Macros (LOAD_FILAMENT,
  UNLOAD_FILAMENT etc.), die BUFFER_*-Commands an die Extension delegieren.

---

## 4. Konfigurations-Block

```cfg
[buffer_feeder mellow]
# ----- Hardware (identisch mit altem [extruder_stepper mellow]) -----
step_pin: LLL_PLUS:PC13
dir_pin: LLL_PLUS:PA7
enable_pin: !LLL_PLUS:PA6
microsteps: 16
gear_ratio: 50:17
rotation_distance: 18.86        # 1:1-kalibriert (aus Phase 2)

# ----- Sensoren (Pin-Zuordnung) -----
hall_empty_pin:      ^!LLL_PLUS:PB4    # HALL3 = Buffer leer
hall_full_pin:       ^!LLL_PLUS:PB3    # HALL2 = Buffer voll
hall_overflow_pin:   ^!LLL_PLUS:PB2    # HALL1 = Überlauf
entrance_pin:        ^!LLL_PLUS:PB7    # Filament am Buffer-Eingang

# ----- Taster -----
feed_button_pin:    ^!LLL_PLUS:PB12
retract_button_pin: ^!LLL_PLUS:PB13

# ----- Geschwindigkeiten / Beschleunigungen -----
feed_speed:         30      # mm/s — Bang-Bang Auto-Refill
manual_speed:       15      # mm/s — Taster Dauerlauf
burst_speed:        50      # mm/s — Triple-Click Burst
load_fast_speed:    50      # mm/s — LOAD Phase 1+3
load_slow_speed:     5      # mm/s — LOAD Phase 2 (parallel zum Extruder)
unload_fast_speed:  50      # mm/s — UNLOAD Phase 3
tip_speed:          20      # mm/s — Tip-Forming (nicht vom Feeder gefahren!)
                            #        Feeder bleibt bei Tip-Forming still,
                            #        Tip wird allein vom Extruder gemacht.
grip_speed:         55      # mm/s — Initial-Grip
accel:            1000      # mm/s²

# ----- Distanzen -----
manual_chunk_distance: 10   # mm — 2-Klick-Puls
burst_distance:      1300   # mm — Retract-Triple-Burst
grip_duration:         10   # s — Initial-Grip-Dauer (→ ~550mm @ 55mm/s)
load_fast_distance:  1000   # mm — LOAD Phase 1 (kalibriert)
load_slow_distance:   180   # mm — LOAD Phase 2 (Heatbreak-Durchgang)
load_buffer_max:     2000   # mm — LOAD Phase 3 Timeout (bis HALL2)
unload_sync_distance: 180   # mm — UNLOAD Phase 2 (synchron zurück)
unload_fast_max:     2510   # mm — UNLOAD Phase 3 Max (Polling bis entrance frei)

# ----- Sicherheits-Limits -----
max_feed_time:        60    # s — Max Dauerfeed ohne HALL2-Trigger (Safety)
max_feed_distance:  3000    # mm — alternative Safety-Grenze
hall_debounce_ms:     50    # ms — Sensor-Debounce
lead_time:           0.3    # s — Move-Scheduling-Lead (MCU-Puffer)
watchdog_interval:    5     # s — Heartbeat-Check (Extension lebt)

# ----- Jam-Detection -----
jam_detection_enabled: 1
jam_clog_dwell_time:  60    # s — HALL2 Max-Verweil bei aktiver Extrusion
jam_clog_extrude_min: 30    # mm — Mindest-Extruder-Progress in der Zeit
jam_supply_dwell_time: 120  # s — HALL3 Max-Verweil bei aktivem Feeder
jam_action: PAUSE           # "PAUSE" oder eigener Macro-Name

# ----- Runout-Verhalten -----
runout_pause:        0      # 0 = externer Sensor, 100mm Nachlauf dann stepper off
                            # 1 = intern pausieren + sofort stepper off

# ----- Triple-Click -----
triple_click_window:  1.5   # s
feed_burst_enabled:   0     # 1 = Feed-Taster 3x-Klick = Burst (Verstopfungsrisiko!)

# ----- Display -----
display_status_enabled: 1   # M117 an/aus

# ----- Initial-Fill -----
auto_load_after_follow: 0   # 1 = nach Grip automatisch LOAD_FILAMENT falls heiß
min_temp: 180               # °C — LOAD/UNLOAD Hotend-Check

[tmc2208 buffer_feeder mellow]
uart_pin: LLL_PLUS:PB1
run_current: 0.450
```

### Hinweise

- Die Sektion `[extruder_stepper mellow]` aus der alten Config **verschwindet komplett**.
- Ebenso alle `[gcode_button buffer_hall*]`, `[gcode_button feed_button]`, `[gcode_button retract_button]`, `[filament_switch_sensor buffer_entrance]` — all diese Pins werden direkt von der Extension verwaltet.
- `[mcu LLL_PLUS]` bleibt unverändert.

---

## 5. State-Machine

```
                        ┌───────────┐
              boot ───▶ │   INIT    │
                        └─────┬─────┘
                              │ klippy:ready
                              ▼
                        ┌───────────┐  entrance_loss (runout)  ┌───────────┐
           ┌──────────▶ │   IDLE    │ ──────────────────────▶  │  RUNOUT   │
           │            └─────┬─────┘                          └─────┬─────┘
           │                  │ entrance_insert                      │
           │                  ▼                                      │ RESUME /
           │            ┌───────────┐                                │ FORCE_BUFFER_FILL
           │            │ INITIAL_  │                                │
           │            │   GRIP    │                                │
           │            └─────┬─────┘                                │
           │                  │ grip-duration elapsed                │
           │                  ▼                                      │
           │            ┌───────────┐                                │
           │            │   AUTO    │◀───────────────────────────────┘
           │            │ (BangBang)│
           │            └─────┬─────┘
           │                  │ manual button / command
           │         ┌────────┼────────┬──────────────┐
           │         ▼        ▼        ▼              ▼
           │  ┌─────────┐ ┌────────┐ ┌─────────┐ ┌──────────┐
           │  │ MANUAL_ │ │MANUAL_ │ │  LOAD_  │ │ UNLOAD_  │
           │  │  FEED   │ │RETRACT │ │ PHASE_* │ │ PHASE_*  │
           │  └────┬────┘ └───┬────┘ └────┬────┘ └────┬─────┘
           │       │          │           │           │
           └───────┴──────────┴───────────┴───────────┘
                               (fertig / HALT / abort)

           ┌───────────┐
           │ OVERFLOW  │ ◀── HALL1 aktiv (jederzeit, überall)
           └─────┬─────┘
                 │ HALL1 inaktiv
                 ▼
           zurück zu AUTO (oder IDLE falls Sensor nicht ok)

           ┌───────────┐
           │   JAM     │ ◀── Jam-Detection triggered
           └─────┬─────┘
                 │ jam_action (PAUSE o.ä.)
                 ▼
           zurück zu AUTO nach RESUME
```

### State-Beschreibung

| State | Feeder-Verhalten | Bang-Bang aktiv? | Jam-Detection? |
|---|---|---|---|
| INIT | stehen, enabled | nein | nein |
| IDLE | stehen, disabled | nein | nein |
| INITIAL_GRIP | läuft vorwärts, grip_speed | nein | nein |
| AUTO | Bang-Bang (HALL3=feed, HALL2=stop) | ja | ja |
| MANUAL_FEED | läuft vorwärts, manual_speed | nein | nein |
| MANUAL_RETRACT | läuft rückwärts, manual_speed | nein | nein |
| LOAD_PHASE_1 | läuft vorwärts, load_fast_speed | nein | nein |
| LOAD_PHASE_2 | läuft vorwärts, load_slow_speed (parallel Extruder) | nein | nein |
| LOAD_PHASE_3 | läuft vorwärts bis HALL2, feed_speed | nein (ist selbst Phase) | nein |
| UNLOAD_PHASE_1 | stehen (Extruder macht Tip-Forming allein) | nein | nein |
| UNLOAD_PHASE_2 | läuft rückwärts, parallel Extruder | nein | nein |
| UNLOAD_PHASE_3 | läuft rückwärts bis entrance frei | nein | nein |
| OVERFLOW | stehen, disabled, Lockout | nein | nein |
| RUNOUT | läuft je nach runout_pause-Modus | nein | nein |
| JAM | stehen, extern PAUSE ausgelöst | nein | nein |

### Übergangs-Regeln

- **HALL1 (Overflow)** hat Vorrang vor allem außer OVERFLOW selbst. Jeder Tick prüft erst
  Overflow, dann den State.
- **Druck-Pause** (vom Macro oder Jam-Action) pausiert Bang-Bang. Extension merkt sich
  `pending_state` und resumed bei RESUME.
- **Manuelle Taster** haben Vorrang vor AUTO, aber nicht über LOAD/UNLOAD. Während
  LOAD/UNLOAD sind Taster gesperrt (M118 Warnung).

---

## 6. Extension-API

### Registrierte GCode-Commands

Alle mit `cmd_<NAME>` als Methoden der `BufferFeeder`-Klasse.

| Command | Parameter | Beschreibung |
|---|---|---|
| `BUFFER_FEED` | `DISTANCE=<mm>` `SPEED=<mm/s>` `TIMEOUT=<s>` | Vorwärts-Move. Ohne DISTANCE: Dauerlauf bis `BUFFER_HALT`. |
| `BUFFER_RETRACT` | `DISTANCE=<mm>` `SPEED=<mm/s>` `TIMEOUT=<s>` | Rückwärts-Move. |
| `BUFFER_HALT` | — | Sofort stoppen (Decel-to-zero innerhalb accel-Zeit). |
| `BUFFER_AUTO_ON` | — | State → AUTO. Aktiviert Bang-Bang. |
| `BUFFER_AUTO_OFF` | — | State → IDLE. Deaktiviert Bang-Bang. |
| `BUFFER_LOAD_PHASE1` | `DISTANCE=<mm>` | Synchron-Call (blockiert bis fertig). |
| `BUFFER_LOAD_PHASE2` | `DISTANCE=<mm>` `SPEED=<mm/s>` | Startet parallelen Feeder-Move. Non-blocking: Caller (Macro) macht G1 E parallel, dann BUFFER_WAIT_IDLE. |
| `BUFFER_LOAD_PHASE3` | `MAX_DISTANCE=<mm>` | Feed bis HALL2 aktiv, max MAX_DISTANCE. |
| `BUFFER_UNLOAD_PHASE2` | `DISTANCE=<mm>` `SPEED=<mm/s>` | Non-blocking, analog PHASE2. |
| `BUFFER_UNLOAD_PHASE3` | `MAX_DISTANCE=<mm>` | Chunked retract bis entrance frei, max MAX_DISTANCE. |
| `BUFFER_WAIT_IDLE` | — | Blockiert bis Extension-State = IDLE oder AUTO. |
| `FORCE_BUFFER_FILL` | — | State → INITIAL_GRIP, startet Initial-Fill-Sequenz. |
| `STOP_BUFFER_FILL` | — | State → IDLE, bricht alles ab. |
| `BUFFER_STATE_DUMP` | — | M118 mit vollständigem State. |
| `CALIBRATE_FEEDER_SYNC` | — | Nur Info-Output — keine Sync-Funktion mehr nötig. Gibt 1:1-Test-Prozedur aus. |
| `MEASURE_LOAD_START` | — | Feed-Taster Toggle-Mode ein + Distance-Counter auf 0. |
| `MEASURE_LOAD_STOP` | — | Distance-Counter ausgeben, Toggle-Mode aus. |

### `get_status(eventtime)` Felder

```python
{
    # Live state
    "state": "AUTO",                          # str
    "hall_empty": False,                      # HALL3
    "hall_full": True,                        # HALL2
    "hall_overflow": False,                   # HALL1
    "entrance_detected": True,                # buffer_entrance
    "feed_button_pressed": False,             # bool
    "retract_button_pressed": False,          # bool
    "continuous_feed": False,                 # bool
    "feed_direction": 0,                      # -1, 0, +1
    "feed_distance_acc_mm": 0.0,              # mm in current continuous feed
    "total_accumulated_mm": 1234.5,           # mm lifetime
    "commanded_pos_mm": 0.0,                  # internal position tracking
    "print_running": False,                   # from idle_timeout:printing
    "jam_active": False,                      # bool
    "bang_bang_suspended": False,             # True during print-PAUSE
    "halt_requested": False,                  # armed by HALT/STOP_BUFFER_FILL
    "runout_follow_active": False,            # runout_pause=0 follow window
    "measure_load_active": False,             # MEASURE_LOAD mode on
    "measure_load_distance_mm": 0.0,          # mm measured
    # Config values (stable, read from config)
    "feed_speed": 30.0, "manual_speed": 15.0, "load_fast_speed": 50.0,
    "load_slow_speed": 5.0, "unload_fast_speed": 50.0,
    "load_fast_distance": 1000.0, "load_slow_distance": 180.0,
    "load_buffer_max": 2000.0, "unload_sync_distance": 180.0,
    "unload_fast_max": 2510.0, "min_temp": 180.0, "accel": 1000.0,
}
```

Zugriff aus Macros: `printer["buffer_feeder mellow"].hall_full`

### Event-Handler (intern)

| Event | Handler |
|---|---|
| `klippy:ready` | Reactor-Timer starten, initial state check |
| `klippy:shutdown` | Stepper disable, alle Timer stoppen |
| `idle_timeout:printing` | print_running=1, Jam-Detection scharf |
| `idle_timeout:ready` / `idle` | print_running=0, Jam-Detection still |

---

## 7. Feature-Mapping (Alt → Neu)

| Feature (alte cfg oder Phase-2-Spec) | Neue Umsetzung |
|---|---|
| `[extruder_stepper mellow]` + SYNC_EXTRUDER_MOTION | `[buffer_feeder mellow]` — Extension owns stepper |
| `_APPLY_SYNC_STATE` | Entfällt. Extension-State-Machine im Timer. |
| `_SYNC_OFF` | Entfällt. |
| `sync_rotation_distance` + ±20% Modulation | Entfällt. Feeder läuft mit fester `feed_speed` bei HALL3, stoppt bei HALL2. |
| `sync_locked` / `sync_state` Hysterese-Latch | Inhärent in Bang-Bang-State-Machine. |
| Feed-Button Dauerlauf / 2x-Puls / 3x-Burst | Python-Handler im Button-Callback. Triple-Click-Detect-State in der Extension. |
| Retract-Button analog | Analog. Burst standardmäßig aktiv (wie bisher). |
| `_TRIPLE_CLICK_STATE` | In-Memory-Dict in Extension. |
| `_MANUAL_FEED` / `_MANUAL_RETRACT` Loops | Extension-State MANUAL_FEED/RETRACT, kein delayed_gcode. |
| `_reenable_autofeed` (1s Cooldown) | Reactor-Timer Delay nach Manual-Operation. |
| HALL1/2/3 `[gcode_button]` + State-Mirror | Direkter Pin-Poll in Extension, kein gcode_button mehr. |
| `buffer_entrance` `[filament_switch_sensor]` | Analog, direkter Pin-Poll. insert/runout-Logik in Extension. |
| `_INITIAL_GRIP_PHASE` (FORCE_MOVE) | State INITIAL_GRIP — Extension fährt grip_duration × grip_speed mm vorwärts. |
| `_initial_follow_loop` (FORCE_MOVE Loop) | Entfällt. Nach Grip direkt Übergang zu AUTO. Bang-Bang füllt bis HALL2. |
| `_initial_follow_end` | Entfällt. Transition INITIAL_GRIP→AUTO ist im Timer. |
| `FORCE_BUFFER_FILL` | Bleibt als User-Command. Ruft Extension. |
| `_boot_autostart` | Bleibt als delayed_gcode, ruft `BUFFER_AUTO_ON` falls entrance detected. |
| `STOP_BUFFER_FILL` | Bleibt, ruft Extension (State → IDLE). |
| `BUFFER_AUTO_ON` | Bleibt als User-Command, ruft Extension. |
| `_STATE_DUMP` | Ruft `BUFFER_STATE_DUMP`. |
| LOAD_FILAMENT 3-Phasen | Macro orchestriert BUFFER_LOAD_PHASE1/2/3. Phase 2 koordiniert parallel: Extension fährt Feeder, Macro macht G1 E, dann BUFFER_WAIT_IDLE + M400. |
| LOAD Phase 3 sensorgesteuert (bis HALL2) | BUFFER_LOAD_PHASE3 liest Extension-`hall_full` intern. |
| UNLOAD_FILAMENT Phase 1 Tip-Forming | Macro macht G1 E push/pull auf Extruder. Feeder bleibt still (State UNLOAD_PHASE_1). Kein Sync mehr nötig. |
| UNLOAD Phase 2 Sync-Retract | BUFFER_UNLOAD_PHASE2 (parallel mit G1 E-). |
| UNLOAD Phase 3 Chunked | BUFFER_UNLOAD_PHASE3 (intern mit Polling und entrance-Check). |
| `_UNLOAD_FAST_RETRACT` Loop | Entfällt, in PHASE3 integriert. |
| `_SAVE_E_MODE` / `_RESTORE_E_MODE` | Bleiben als LLL.cfg-Helper (Extruder-Mode M82/M83 ist nicht Extension-Zuständigkeit). |
| `min_temp` Check | Bleibt in LOAD_FILAMENT / UNLOAD_FILAMENT Macros. |
| `Enable_Runout_Sensor` / `Disable_Runout_Sensor` | Setzen Extension-Flag `print_running`. |
| Runout mit `runout_pause=0/1` | Extension-interne Logik, konfigurierbar. |
| `_runout_stepper_disable` (100mm Nachlauf) | Extension-interne Reactor-Logik bei `runout_pause=0`. |
| HALL1 Hard-Stop + Lockout | Extension-State OVERFLOW. Hat Vorrang vor allem. Clearing automatisch bei HALL1 inaktiv. |
| `CALIBRATE_FEEDER_SYNC` | Nicht mehr funktional notwendig (kein Sync). Macro bleibt als Dokumentations-Hint (User-Info + nominal rotation_distance-Anzeige). |
| `MEASURE_LOAD_START/STOP` | Extension-Commands, Feed-Taster im Toggle-Modus via Flag. |
| `feed_burst_enabled` | Extension-Config. |
| `display_status_enabled` | Extension-Config, M117 an allen Stellen wrappt. |
| `auto_load_after_follow` | Nach INITIAL_GRIP, wenn Hotend heiß, LOAD_FILAMENT triggern (Macro-Delegation). |
| M117 Status-Ausgaben | Extension macht M117 an definierten Stellen. |
| **NEU: Jam-Detection** | Reactor-Timer tracked HALL-Verweildauer + Extruder-Progress. Siehe §8. |

---

## 8. Jam-Detection

### Algorithmus

```python
# Pro Reactor-Tick (alle 1s für Jam-Check):
now = reactor.monotonic()

if state not in ("AUTO", "LOAD_PHASE_3"):
    # nur während normaler Extrusion aktiv
    reset_jam_timers()
    return

# --- Jam-Typ 1: Nozzle-Clog (HALL2 stays active) ---
if hall_full and not hall_empty:
    if hall2_start_time is None:
        hall2_start_time = now
        hall2_start_extruder_pos = printer.extruder.last_position
    else:
        dwell = now - hall2_start_time
        extrude_progress = printer.extruder.last_position - hall2_start_extruder_pos
        if dwell >= jam_clog_dwell_time and extrude_progress >= jam_clog_extrude_min:
            trigger_jam("CLOG", f"HALL2 active {dwell:.0f}s, extruder moved {extrude_progress:.1f}mm — nozzle clog?")
else:
    hall2_start_time = None

# --- Jam-Typ 2: Supply-Jam (HALL3 stays active during feed) ---
if hall_empty and feeder_moving_forward:
    if hall3_start_time is None:
        hall3_start_time = now
    else:
        dwell = now - hall3_start_time
        if dwell >= jam_supply_dwell_time:
            trigger_jam("SUPPLY", f"HALL3 active {dwell:.0f}s with feeder running — spool tangle or feed blockage?")
else:
    hall3_start_time = None


def trigger_jam(kind, message):
    if jam_active:
        return   # schon gemeldet
    jam_active = True
    state = JAM
    stop_feeder()
    m118(f"*** JAM DETECTED: {kind} — {message} ***")
    if display_status_enabled:
        m117(f"JAM: {kind}")
    gcode.run_script_from_command(jam_action)   # "PAUSE" default
```

### Clearing

Jam-State wird gecleart durch:
- RESUME (Extension hookt idle_timeout-state-Wechsel)
- manueller `BUFFER_AUTO_ON`
- Klipper-Restart

Timer werden bei Clearing resettet.

### Tuning

Defaults sind konservativ. User sollte nach ersten Drucken die Werte anpassen:
- `jam_clog_dwell_time`: zu niedrig → Fehlalarme bei sehr lokalen Feaures (kleine Details). Zu hoch → Jam erst nach viel weggeschmolzenem Filament.
- `jam_supply_dwell_time`: abhängig von Feed-Speed und Spool-Typ. Lange Zeiten OK wenn User Spool selten wechselt.

---

## 9. Sensor-Integration

### HALL-Polling

Reactor-Timer @ 50 Hz (20ms) liest Pin-Zustände direkt. Debounce pro Sensor: Zustand muss `hall_debounce_ms` lang stabil sein bevor das Event durchgereicht wird.

Pseudo:
```python
def _sensor_poll(self, eventtime):
    for name in ("empty", "full", "overflow", "entrance"):
        raw = self._read_pin(name)
        if raw != self._raw_state[name]:
            self._raw_state[name] = raw
            self._raw_change_time[name] = eventtime
        elif (eventtime - self._raw_change_time[name]) * 1000 >= self.hall_debounce_ms:
            if self._stable_state[name] != raw:
                self._stable_state[name] = raw
                self._on_sensor_change(name, raw)
    return eventtime + 0.02
```

Implementierung: Wir lesen die Pin-Zustände über `mcu.lookup_pin()` + `MCU_endstop`-Pattern oder via `buttons.register_buttons()` mit einem internen Callback, der den debounce macht.

**Entscheidung:** `buttons.register_buttons()` — event-driven, niedrigere CPU-Last. Debounce geschieht über `mcu.register_response` Timing. Pattern wie in Klipper's eigenem `extras/filament_switch_sensor.py`.

### buffer_entrance

- **insert** (entrance wird aktiv): wenn State == IDLE → `FORCE_BUFFER_FILL` ausführen.
- **runout** (entrance wird inaktiv):
  - Wenn State ∈ {LOAD/UNLOAD/MANUAL_*}: ignorieren (geplantes Filament-Ende).
  - Sonst und `print_running == 1`:
    - `runout_pause == 1`: Stepper sofort disable, State → IDLE, `PAUSE` + M117
    - `runout_pause == 0`: Merke `filament_used_ref`. Starte Polling-Timer. Nach 100mm Extruder-Progress: Stepper disable. Externer Sensor kümmert sich um PAUSE.

### HALL-Events

| Sensor | Event | Aktion in AUTO-State |
|---|---|---|
| HALL3 aktiv | "Buffer leer" | Feeder an, Richtung vorwärts |
| HALL3 inaktiv | "leaving empty" | (weiterlaufen bis HALL2) |
| HALL2 aktiv | "Buffer voll" | Feeder aus |
| HALL2 inaktiv | "leaving full" | (stehen bleiben bis HALL3) |
| HALL1 aktiv | "Overflow" | SOFORT Stepper disable, State → OVERFLOW |
| HALL1 inaktiv | "overflow cleared" | State → AUTO (falls entrance noch detected) |

---

## 10. Sicherheits-Garantien

### Hard-Safeties

1. **HALL1 Overflow** — höchste Priorität. Prüfung in jedem Reactor-Tick.
   Auch während JAM, MANUAL_*, LOAD_*, UNLOAD_*.

2. **Max-Feed-Time + Max-Feed-Distance** — wenn die Extension in einem Dauerfeed-Zustand
   (AUTO mit HALL3 aktiv, MANUAL_FEED, LOAD_PHASE_3) länger als `max_feed_time` OR
   mehr als `max_feed_distance` am Stück feedet, harter Abort mit Error-M117.

3. **Watchdog** — Reactor-Timer registriert sich alle `watchdog_interval` Sekunden.
   Wenn Timer nicht läuft (z.B. Reactor-Deadlock): Extension kann sich nicht selbst
   retten, aber Klipper's eigenes "mcu host timeout" fängt das auf MCU-Ebene.

4. **Shutdown-Handler** — `klippy:shutdown` Event stoppt sofort alle Timer und
   disabled den Stepper.

### Soft-Safeties

- Jede `BUFFER_FEED/RETRACT` mit DISTANCE validiert gegen `max_feed_distance` pre-flight.
- Jede Dauerlauf-Variante (ohne DISTANCE) wird von `max_feed_time` begrenzt.
- Jam-Detection als zusätzliche Schicht für Anomalien, die unterhalb der Hard-Safety-Schwellen liegen.

### Fehlerausgaben

- Fehler: `action_respond_error("Buffer: ...")` + M117 Error
- Warnungen: M118 (Konsole) + optional M117 wenn `display_status_enabled=1`
- Status: M118 + optional M117

---

## 11. LOAD/UNLOAD ohne Sync

Der Ersatz für `SYNC_EXTRUDER_MOTION`-basiertes Parallel-Motion in Phase 2:

### LOAD Phase 2

```
Macro-Code:
  M400                            # Toolhead abwarten
  BUFFER_LOAD_PHASE2 DISTANCE=180 SPEED=5
  # ↑ non-blocking: Extension queued 180mm vorwärts @ 5mm/s
  G1 E180 F300                    # Extruder 180mm @ 5mm/s (300mm/min)
  # ↑ blockiert in toolhead-queue
  M400                            # auf extruder warten
  BUFFER_WAIT_IDLE                # auf Feeder warten
  # Jetzt sind beide ±0.1s synchron
```

Beide Motoren fahren mit exakt gleicher nominaler Geschwindigkeit. Start ungefähr zur gleichen Zeit (G1 E beginnt sofort nachdem Extension den Move angemeldet hat). Ende: beide fertig, Synchronisation per `M400` + `BUFFER_WAIT_IDLE`.

Drift ist erwartbar (vielleicht 1-2mm Unterschied bei 180mm), aber der Buffer-Kammer hat das Spiel — HALL2/HALL3 werden nicht bei so geringer Abweichung triggern.

### UNLOAD Phase 2 — analog

```
BUFFER_UNLOAD_PHASE2 DISTANCE=180 SPEED=<unload_fast_speed>
G1 E-180 F<unload_fast_speed*60>
M400
BUFFER_WAIT_IDLE
```

### UNLOAD Phase 1 — Tip-Forming

Kein Feeder-Mitlauf mehr. Extruder macht push/pull allein. Feeder steht still (State UNLOAD_PHASE_1).

Das ist eine Verhaltens-Änderung gegenüber der alten Config (wo Tip-Forming mit Sync lief). Rationale: Die Tip-Forming-Moves sind klein (8/10mm), das passiert in der Hotend-Zone. Der Feeder muss nicht dagegenarbeiten.

---

## 12. Button-Handling in Python

### Click-Detection

Reactor-Timer tracked pro Button:
- `click_count`
- `last_click_time`
- `button_held`

Algorithmus:
```
on_press(button):
    now = reactor.monotonic()
    if now - last_click_time > triple_click_window:
        click_count = 1
    else:
        click_count += 1
    last_click_time = now
    button_held = True

    if click_count == 1:
        start_manual_operation(button)  # Dauerlauf
    elif click_count == 2:
        stop_manual_operation()
        pulse(manual_chunk_distance)
    elif click_count == 3:
        stop_manual_operation()
        if button == "feed" and not feed_burst_enabled:
            start_manual_operation(button)  # einfach neu starten
        else:
            burst(burst_distance)
        click_count = 0

on_release(button):
    button_held = False
    if manual_operation_active:
        stop_manual_operation()
    # nach reenable_cooldown: State → AUTO (falls das der vorherige war)
```

### MEASURE_LOAD-Modus

Wenn MEASURE_LOAD_START aktiv: Feed-Button arbeitet im Toggle-Modus.
- 1. Klick: `start_manual_operation(feed)`
- 2. Klick: `stop` + `MEASURE_LOAD_STOP` intern
- release ignoriert

---

## 13. Installer-Script

`install.sh` im Repo-Root:

```bash
#!/bin/bash
# Installer für buffer_feeder Klipper-Extension
set -euo pipefail

KLIPPER_DIR="${HOME}/klipper"
EXT_SOURCE="${PWD}/klipper_extras/buffer_feeder.py"
EXT_TARGET="${KLIPPER_DIR}/klippy/extras/buffer_feeder.py"

echo "==> Klipper-Verzeichnis: ${KLIPPER_DIR}"
echo "==> Source-Datei:        ${EXT_SOURCE}"
echo "==> Ziel-Symlink:        ${EXT_TARGET}"

if [ ! -f "${EXT_SOURCE}" ]; then
    echo "FEHLER: ${EXT_SOURCE} nicht gefunden. Ausführen im Repo-Root?"
    exit 1
fi

if [ ! -d "${KLIPPER_DIR}/klippy/extras" ]; then
    echo "FEHLER: ${KLIPPER_DIR}/klippy/extras existiert nicht. Ist Klipper installiert?"
    exit 1
fi

# Symlink (überschreibt bestehenden Symlink/File)
ln -sf "${EXT_SOURCE}" "${EXT_TARGET}"
echo "==> Symlink gesetzt: ${EXT_TARGET} -> ${EXT_SOURCE}"

# moonraker update-manager Hint
cat <<EOF

Installation abgeschlossen.

Nächste Schritte:
  1. Klipper neu starten (sudo systemctl restart klipper)
  2. printer.cfg: [include lll.cfg] hinzufügen
  3. lll.cfg hat bereits den [buffer_feeder mellow]-Block

Optional für Moonraker-Auto-Update:
  Füge folgendes zu moonraker.conf hinzu:

[update_manager buffer_feeder]
type: git_repo
path: ${PWD}
origin: https://github.com/Avatarsia/BufferPLUS-klipper.git
primary_branch: python-ansatz
is_system_service: False
managed_services: klipper
EOF
```

Nutzung: `./install.sh` im Repo-Root auf dem Host (Raspberry Pi).

### Repo-Struktur

```
/                              (Repo-Root, Branch python-ansatz)
├── LICENSE
├── README.md                  (neu geschrieben, Python-Architektur)
├── install.sh                 (Installer, oben)
├── klipper_extras/
│   └── buffer_feeder.py       (Die Extension)
├── lll.cfg                    (Neue Config mit [buffer_feeder mellow])
├── docs/
│   ├── superpowers/specs/
│   │   └── 2026-04-23-python-ansatz-design.md  (dieses Dokument)
│   └── knowledge/             (bestehend, updaten falls nötig)
└── vergleich/                 (bestehend, Referenzmaterial)
```

---

## 14. Test-Plan

### Unit-Level (ohne Hardware)

1. **Config-Parsing:** Extension lädt korrekt bei `klippy:ready` mit Test-Config.
2. **State-Machine-Transitions:** Mock-Events → erwartete State-Übergänge.
3. **Triple-Click-Logik:** Mock-Button-Events → erwartete Actions.
4. **Jam-Detection-Logik:** Mock HALL- und Extruder-Position-Sequences → erwartete Trigger.
5. **Debounce:** Mock-Pin-Flicker → nur stabile State-Changes durchgereicht.

### Integration (am realen Drucker)

User führt folgende Tests sequentiell durch und meldet Ergebnis:

| # | Test | Erwartung |
|---|---|---|
| 1 | Klipper-Boot mit leerem Buffer | Extension lädt, State=IDLE, Stepper disabled |
| 2 | Filament einlegen (entrance trigger) | State → INITIAL_GRIP, Feeder läuft 10s |
| 3 | Nach Grip: Filament noch nicht bei HALL2 | State → AUTO, Feeder läuft weiter bis HALL2 |
| 4 | HALL2 erreicht | Feeder stoppt, State bleibt AUTO |
| 5 | Filament langsam rausziehen bis HALL3 | Feeder startet wieder |
| 6 | HALL1 manuell triggern (Mechanik pushen) | Feeder SOFORT aus, State → OVERFLOW, M117 |
| 7 | HALL1 wieder frei | State → AUTO, Feeder läuft wieder |
| 8 | Feed-Button 1 Klick + halten | Dauerfeed, stoppt beim Loslassen |
| 9 | Feed-Button 2x schnell klicken | 10mm Puls |
| 10 | Retract-Button 3x schnell klicken | 1300mm Retract-Burst |
| 11 | LOAD_FILAMENT bei heißem Hotend | Alle 3 Phasen sauber, Druckkopf fährt nicht |
| 12 | UNLOAD_FILAMENT bei heißem Hotend | Tip + Retract + Polling bis entrance frei |
| 13 | Druck starten, 1 Stunde drucken | KEIN Feeder-Chatter bei Retracts. Kein Toolhead-Stuttering. |
| 14 | Mitten im Druck: Spool festhalten (Supply-Jam simulieren) | Nach ~2min: JAM SUPPLY-Alarm + PAUSE |
| 15 | Mitten im Druck: Düse verstopfen (mit Finger blockieren — nur kurz!) | Nach ~1min: JAM CLOG-Alarm + PAUSE |
| 16 | Filament-Runout während Druck mit `runout_pause=0` | Feeder läuft 100mm weiter, dann aus. Externer Sensor pausiert. |

### Regressions-Check

Der End-of-Acceptance-Test ist: **User druckt einen 8h-Druck ohne Feeder-Chatter
und ohne Toolhead-Pausen.** Das ist das Kernziel der Architektur-Änderung.

---

## 15. Risiken + Bekannte Grenzen

### Risiken

| # | Risiko | Mitigation |
|---|---|---|
| R1 | `motion_queuing`-API ist nicht öffentlich garantiert → kann mit Klipper-Update brechen | Bei Update: Extension gegen geänderte API prüfen. Spec dokumentiert Abhängigkeiten (§2 Research-Summary). |
| R2 | Lead-Time-Fehlkalibrierung → MCU verpasst Move oder Feeder lagt | `lead_time` als Config-Parameter. Default 0.3s ist konservativ. Testen bei Integration. |
| R3 | Python-Bug → Feeder läuft endlos | Hard-Safeties §10: max_feed_time, max_feed_distance, HALL1. |
| R4 | Sensor-Flackern → Bang-Bang-Oszillation | Debounce 50ms (konfigurierbar). |
| R5 | Zu aggressive Jam-Detection → Fehlalarme während langsamer Print-Parts | Thresholds konservativ gewählt; `jam_detection_enabled=0` als Notausgang. |
| R6 | LOAD Phase 2 ohne Sync → Drift zwischen Feeder und Extruder | Drift ist gering (< 2mm auf 180mm), Buffer absorbiert. M400 + BUFFER_WAIT_IDLE synchronisieren am Ende. |
| R7 | Kein Community-Support | Alle Design-Entscheidungen in dieser Spec dokumentiert, Code kommentiert. |

### Bekannte Grenzen

- Die Extension ist **nicht portabel** zwischen Klipper-Versionen ohne Test.
- **Keine persistente Kalibrierung** (via `save_variables`) — out of scope.
- **Kein Encoder-Feedback** — Buffer-Position wird ausschließlich über die 3 HALL-Sensoren gelesen.
- **Single-Buffer only** — die Extension instanziiert einen Feeder. Multi-Buffer würde die Config-Struktur aufbohren.
- **Tip-Forming ohne Feeder-Mitlauf** — Verhaltens-Änderung gegenüber alter Config, akzeptabel (kleine Moves, Buffer absorbiert).

---

## 16. Out-of-Scope

- Auto-Kalibrierungs-Makros (über das bestehende `MEASURE_LOAD_START` hinaus)
- Moonraker/Fluidd-Integration mit speziellen Buttons (Standard-Macros sind genug)
- Encoder-basierte Slip-Detection
- TMC-Stallguard-Integration
- Persistente State-Storage
- Multi-Buffer-Support

---

## 17. Definition of Done

- [ ] Python-Extension `klipper_extras/buffer_feeder.py` implementiert alle §6-API-Commands
- [ ] Alle Feature-Mappings aus §7 funktional
- [ ] Jam-Detection aus §8 implementiert und testbar
- [ ] HALL1 Hard-Stop garantiert (auch bei gleichzeitigen manuellen Commands)
- [ ] `install.sh` funktioniert auf frischem Raspberry-Pi-Klipper-Host
- [ ] `lll.cfg` vollständig neu geschrieben, nur Macro-Wrapper + Konfig
- [ ] README aktualisiert auf neue Architektur
- [ ] Integration-Tests 1-13 vom User bestätigt
- [ ] Jam-Tests 14-15 demonstriert
- [ ] 8h-Druck ohne Chatter bestätigt
