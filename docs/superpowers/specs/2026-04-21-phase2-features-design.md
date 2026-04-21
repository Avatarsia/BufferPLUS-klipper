# LLL Buffer Plus Phase 2 — Feature Integration Design

**Datum:** 2026-04-21
**Branch:** `rebuild-sync-v2` (auf Phase-1-Stand nach R-14)
**Basis-Spec:** `2026-04-21-lll-buffer-refactor-design.md`
**Zusatz-Referenz:** vom User bereitgestellte `LLL_Plus.cfg` (1064 Zeilen) mit
parallelen Feature-Erweiterungen, die integriert werden sollen.

## 1. Kontext

Phase 1 hat `lll.cfg` refactored (Helper-Extraktion, Magic-Numbers raus,
Mirror-Vars weg, Section-Reorder). Phase 2 integriert Features aus einer
zweiten User-Variante `LLL_Plus.cfg`, die unabhaengig von Phase 1
entstanden ist.

**Prinzip:** Unsere Refactor-Hygiene (Helper, Variablen, Raw-State)
bleibt. Die Features aus `LLL_Plus.cfg` werden ueber den Refactor gelegt.
Wo Features in der LLL_Plus-Variante duplizierten Code nutzen, wird das
in unserer Helper-Architektur ausgedrueckt.

## 2. User-Entscheidungen

| Thema | Entscheidung |
|---|---|
| A1 sync_locked | uebernehmen |
| A2 sync_state-Latch-Hysterese | uebernehmen (glatte Regelkurve statt Sprung-zurueck-zu-nominal) |
| A3 Runout-Nachlauf (`runout_pause`) | uebernehmen |
| A4 MEASURE_LOAD-Kalibriermakros | uebernehmen |
| A5 CALIBRATE_FEEDER_SYNC | uebernehmen |
| A6 Sensorgesteuertes LOAD (HALL2-getriggert) | uebernehmen |
| A7 UNLOAD 50-mm-Chunks + zweigeteilte Phase 3 | uebernehmen |
| A8 `load_fast_distance` statt `load_fast1/2` | uebernehmen |
| A9 `load_buffer_max` | uebernehmen |
| A10 `runout_pause`-Flag | uebernehmen |
| A11 M117-Display mit Schalter | uebernehmen via `variable_display_status_enabled` |
| A12 Boot-Autostart vereinfacht (nur BUFFER_AUTO_ON) | uebernehmen |
| A13 Doku-Kalibrier-Anleitung | **raus aus cfg, rein in README** |
| B1-B5 Kalibrierwerte (18.86, 1300, 1.5s, max_extrude 100) | uebernehmen |
| C1 MCU-Serial `_33001E...` | **nicht** uebernehmen — alte Serial bleibt |
| D1-D8 Refactor-Regressionen | Phase-1-Loesungen behalten |

## 3. Neue Variablen in `_FILAMENT_VARS`

```
variable_sync_rotation_distance: 18.86   # war 19.5, vom User kalibriert
variable_triple_click_distance:  1300    # war 500
variable_triple_click_window:    1.5     # war 0.8

variable_load_fast_distance:     1000    # ersetzt load_fast1+load_fast2
variable_load_buffer_max:        2000    # Timeout fuer LOAD Phase 3
variable_runout_pause:           0       # 0 = externer Sensor, 1 = sofort pausieren
variable_display_status_enabled: 1       # 1 = M117-Ausgaben aktiv (Debug), 0 = still
```

Entfernt: `variable_load_fast1`, `variable_load_fast2`.
`rotation_distance` in `[extruder_stepper mellow]` ebenfalls 19.5 -> 18.86
fuer Boot-Konsistenz.

## 4. Neue State-Flags in `_BUFFER_AUTO_CONTROL`

```
variable_sync_locked:         0   # 1 = _APPLY_SYNC_STATE darf nichts tun
variable_sync_state:          0   # Hysterese-Latch: 0=init, 1=fast, 2=slow
variable_runout_filament_ref: 0   # filament_used-Referenz beim Runout
```

`saved_e_abs` bleibt (Phase 1). Mirror-Vars `hall1/2/3_active` bleiben
entfernt (Phase 1).

## 5. `_APPLY_SYNC_STATE` erweitert (Phase 2)

Drei Aenderungen gegenueber Phase 1:

1. **Early-Exit bei `sync_locked == 1`.** Wenn ein Caller (Runout,
   LOAD Phase 2, UNLOAD Phase 1+2) den Sync explizit gegen Hall-
   Interferenz abgesichert hat, macht `_APPLY_SYNC_STATE` gar nichts.
2. **Latch-Logik:** HALL3 aktiv -> `sync_state=1, DISTANCE=fast_rd`.
   HALL2 aktiv -> `sync_state=2, DISTANCE=slow_rd`. Kein Hall aktiv ->
   letzten `sync_state` beibehalten (Hysterese). `sync_state=0` nur bei
   Initialisierung/Reset -> nominal.
3. Raw-State-Queries bleiben (unsere R-4-Loesung). Keine Mirror-Vars.

## 6. `_SYNC_OFF` erweitert

Beim Trennen zusaetzlich `SET_EXTRUDER_ROTATION_DISTANCE EXTRUDER=mellow
DISTANCE={v.sync_rotation_distance}`. Garantiert dass nachfolgende
FORCE_MOVE-Ops immer mit kalibriertem 1:1 arbeiten, unabhaengig vom
letzten Hall-State.

## 7. MEASURE_LOAD (Kalibrier-Makros)

- `[gcode_macro _MEASURE_LOAD_STATE]` mit `variable_active: 0`,
  `variable_distance: 0`. Container.
- `[gcode_macro MEASURE_LOAD_START]`: setzt active=1, distance=0,
  manual_operation=1, _SYNC_OFF. User-Prompt "Taster druecken".
- `[gcode_macro MEASURE_LOAD_STOP]`: gibt Distanz aus, active=0,
  _APPLY_SYNC_STATE.
- `_MANUAL_FEED` erweitert: wenn `_MEASURE_LOAD_STATE.active==1`,
  Distance-Counter um `v.manual_chunk_distance` inkrementieren.

## 8. CALIBRATE_FEEDER_SYNC

- Sync aktivieren mit exakt `v.sync_rotation_distance` (kein +-20%).
- User-Prompt: `G1 E100 F60`, nachmessen, Wert anpassen.

## 9. Button-Mess-Modus-Toggle

Im `_BUTTON_CLICK_HANDLER` (oder alternativ im feed_button direkt, wenn
Handler-Parametrisierung zu komplex wird):

- Wenn `_MEASURE_LOAD_STATE.active == 1`: Toggle-Semantik. 1. Klick =
  `_MANUAL_FEED.active=1` + `_MANUAL_FEED`. 2. Klick = stoppen, dann
  `MEASURE_LOAD_STOP` aufrufen (gibt Ergebnis aus).
- Sonst: Triple-Click-Logik wie bisher.
- `release_gcode` wird im Mess-Modus **nicht** beachtet (Toggle!).

## 10. Runout mit Nachlauf

`buffer_entrance.runout_gcode`:

- Setze `sync_locked=1` (blockiert `_APPLY_SYNC_STATE`).
- Setze andere Flags (overfill_lock=0, initial_lockout=0, etc.).
- Wenn `v.runout_pause == 1`:
  - Sofort `SYNC_EXTRUDER_MOTION MOTION_QUEUE=` + Stepper disable + PAUSE.
- Sonst (`runout_pause == 0`, externer Sensor):
  - Speichere `runout_filament_ref = printer.print_stats.filament_used`.
  - Starte `[delayed_gcode _runout_stepper_disable]` mit DURATION=1.

`[delayed_gcode _runout_stepper_disable]`:

- Liest aktuelles `filament_used`.
- Wenn `cur >= ref + 100`: Stepper disable, Sync trennen, manual_operation=0.
- Sonst: Self-Call mit DURATION=1.

## 11. LOAD_FILAMENT (sensorgesteuert)

Drei Phasen + State-Machine:

- **Phase 1** — `_LOAD_PH_ONE_LOOP` + `[delayed_gcode _load_ph_one_delayed]`.
  Foerdert in `v.manual_chunk_distance`-Schritten (10 mm) bis
  `distance >= v.load_fast_distance`. FORCE_MOVE mit `v.fast_speed`,
  `v.force_move_accel`.
- **Phase 2** — `_LOAD_PH_TWO`. `sync_locked=1`, Sync an mit nominal,
  in 50-mm-Chunks `G1 E<chunk>` bis `v.load_slow` erreicht.
  `sync_locked=0` am Ende, `_SYNC_OFF`.
- **Phase 3** — `_LOAD_PH_THREE_LOOP` + `[delayed_gcode
  _load_ph_three_delayed]`. Foerdert 10-mm-Chunks, prueft
  HALL2-Raw-State nach jedem Chunk. Exit wenn HALL2 aktiv oder
  `distance >= v.load_buffer_max` (Warnung bei Timeout).

State-Container `LOAD_FILAMENT`: `variable_state` (1=Phase1, 3=Phase3,
0=idle), `variable_distance` (akkumuliert pro Phase), kein
`variable_e_abs` (unser `_SAVE_E_MODE`/`_RESTORE_E_MODE` uebernimmt).

Hall-Queries via `printer["gcode_macro _BUFFER_AUTO_CONTROL"].hall2_active`
gehen **nicht** — die Mirror-Vars haben wir entfernt. Stattdessen direkt
`printer["gcode_button buffer_hall2"].state == "RELEASED"`.

## 12. UNLOAD_FILAMENT

- **Phase 1 (Tip-Forming):** `sync_locked=1`, Sync an mit nominal,
  `v.tip_cycles` Zyklen mit `tip_push`/`tip_pull`, dann
  `tip_final_retract`. Alle G1 E-Moves (nominal-sync, keine Chunk-Aufteilung
  noetig da `tip_push+tip_pull < 50mm`). `M400`.
- **Phase 2 (Sync-Retract):** 50-mm-Chunks `G1 E-<chunk>` bei `v.slow_speed`
  bis `v.unload_sync` erreicht. Rest-`remainder` als einzelner Chunk.
  `M400`. Am Ende `sync_locked=0`, `_SYNC_OFF`.
- **Phase 3a:** `FORCE_MOVE DISTANCE=-v.load_fast_distance` am Stueck.
- **Phase 3b:** `_UNLOAD_FAST_RETRACT` bleibt (unser chunk-coupling-Fix
  aus R-9), chunk=50 statt 10 fuer Konsistenz mit LLL_Plus.cfg.

`_SAVE_E_MODE`/`_RESTORE_E_MODE` bleiben, kein `variable_e_abs` in
`UNLOAD_FILAMENT`.

## 13. Boot-Autostart (vereinfacht)

```
[delayed_gcode _boot_autostart]
initial_duration: 7.0
gcode:
    {% if printer["filament_switch_sensor buffer_entrance"].filament_detected %}
        M118 BOOT: Filament am Eingang - Sync aktiviert
        {% if ...display_status_enabled %}M117 BOOT: Sync an{% endif %}
        BUFFER_AUTO_ON
    {% else %}
        M118 BOOT: Kein Filament am Eingang - Standby
    {% endif %}
```

Keine Hall-State-Pruefung mehr, keine Auto-FORCE_BUFFER_FILL. Reale
Initial-Phase nur manuell oder ueber `insert_gcode`-Event.

## 14. M117-Display mit Schalter

Pattern:

```jinja
{% if printer["gcode_macro _FILAMENT_VARS"].display_status_enabled == 1 %}
    M117 <status-text>
{% endif %}
```

Alle M117-Stellen aus `LLL_Plus.cfg` uebernehmen, mit diesem Wrapper.
Orte: Buttons (Feed/Retract/Burst), Runout, LOAD/UNLOAD Phasen,
Initial-Grip, MEASURE_LOAD, CALIBRATE_FEEDER_SYNC.

## 15. Was sich **nicht** aendert

- MCU-Serial bleibt `_2D001E001457464E33313420` (nicht `_33001E...`).
- Alle Phase-1-Helper bleiben: `_PREPARE_INITIAL_FILL`,
  `_ABORT_ALL_FEED_LOOPS`, `_SAVE_E_MODE`, `_RESTORE_E_MODE`,
  `_BUTTON_CLICK_HANDLER`.
- Mirror-Vars bleiben entfernt (nicht wieder einfuehren).
- UPPER_SNAKE-Naming (`ENABLE_RUNOUT_SENSOR`) bleibt.
- Header-ASCII bleibt, Kalico-Block bleibt raus.
- Section-Reihenfolge aus R-13 bleibt.

## 16. Validierungs-Strategie

Wie in Phase 1: kein Laufzeit-Test ohne Hardware. Ersatz:
- Grep-basierte Vor-/Nach-Verifikation pro Task.
- Equivalence-Doc wird um Phase-2-Verhaltensaenderungen erweitert
  (Hysterese, Boot-Autostart, Runout-Nachlauf).
- Drucker-Integration-Test bleibt User-Vorbehalt.

## 17. Out-of-Scope (weiterhin)

Gleiche Liste wie Phase 1 §11:
- `[save_variables]` fuer persistente Kalibrierdaten
- Web-/Display-API (mehr als M117)
- Auto-Kalibrier-Makro (CALIBRATE_FEEDER_SYNC ist nur Prep)
- Multi-Buffer
- Telemetrie/Histogramme

## 18. Definition of Done

- [ ] Alle Features aus §3-§14 in `lll.cfg` integriert.
- [ ] Alle Phase-1-Refactor-Eigenschaften erhalten.
- [ ] README.md um Kalibrier-Anleitung erweitert.
- [ ] Equivalence-Doc um Phase-2-Diff erweitert.
- [ ] Branch `rebuild-sync-v2` auf User-Freigabe gepusht.
