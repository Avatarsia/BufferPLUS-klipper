# Semantik-Aequivalenz Refactor Phase 1

**Referenz-Spec:** `2026-04-21-lll-buffer-refactor-design.md`
**Referenz-Plan:** `2026-04-21-lll-buffer-refactor-phase1.md`
**Base-SHA:** 02be5d5 (User-Baseline vor Refactor)
**Head-SHA:** 01da9b0 (nach R-13)

**Diff-Volumen:** `diff baseline HEAD | wc -l` = 996 Zeilen
(baseline 735 LOC, HEAD 777 LOC). Die Groesse ist durch die strukturelle
Umsortierung und die Einfuehrung von `_FILAMENT_VARS` /
`_BUTTON_CLICK_HANDLER` / `_ABORT_ALL_FEED_LOOPS` / `_SAVE_E_MODE` /
`_RESTORE_E_MODE` / `_PREPARE_INITIAL_FILL` / `_APPLY_SYNC_STATE`-Helpers
getrieben, nicht durch Verhaltensaenderungen. Reine Zeilenaenderungen
sind deshalb **kein** Signal fuer Semantik-Drift.

## Methodik

Fuer jede Event-Quelle wird der Pfad zum `_APPLY_SYNC_STATE`-Output
manuell simuliert: Welche Flags werden gesetzt, welchen State-Flag-
Set hat `_BUFFER_AUTO_CONTROL` im Moment des Calls, welche Hall-
Sensoren sind physisch aktiv, welcher `SYNC_EXTRUDER_MOTION`-Aufruf
und welche `rotation_distance` wird emittiert?

Wo Vor- und Nach-Version denselben `_APPLY_SYNC_STATE`-Input und
damit denselben Output haben, ist die Aequivalenz trivial. Wo sie
abweichen, wird das explizit diskutiert.

## Bekannte Abweichungen

- **Manual Feed/Retract Speed:** Vor Refactor `VELOCITY=15` (Feed)
  vs. `VELOCITY=20` (Retract), beides hardcoded. Nach Refactor beide
  auf `{v.manual_speed} = 15 mm/s` vereinheitlicht (bewusst laut
  User-Antwort auf Q6.1 in der Spec).
- **HALL1-release Retract-Loop-Stop:** Neue Safety-Aktion in
  `_ABORT_ALL_FEED_LOOPS` (Detail siehe Abschnitt 5).
- **`_UNLOAD_FAST_RETRACT` Counter-Koppelung:** Counter-Inkrement
  an `v.manual_chunk_distance` gebunden (Wartbarkeits-Fix, kein
  Laufzeit-Wechsel bei Default-Wert 10).
- **Alle anderen Aspekte:** strikt verhaltens-identisch.

## 1. buffer_entrance.insert_gcode

**Vor:** Setzt inline `system_enabled=1`, `manual_operation=0`,
`overfill_lock=0`, `initial_lockout=1`, dann `UPDATE_DELAYED_GCODE
ID=_start_initial_grip DURATION=0.1`.

**Nach:** Ruft `_PREPARE_INITIAL_FILL`, das exakt dieselben 4 SETs +
`_APPLY_SYNC_STATE` + denselben `UPDATE_DELAYED_GCODE` macht.

**Aequivalenz:** Identischer Flag-State, identischer Helper-Call,
identischer Delayed-Grip-Trigger. Der zusaetzliche `_APPLY_SYNC_STATE`-
Call in der Helper-Variante ist neutral — bei `initial_lockout=1`
fuehrt er nur zu `SYNC_EXTRUDER_MOTION ... MOTION_QUEUE=` (Sync aus),
was mangels Caller-Action sowieso der naechste effektive State waere.

## 2. buffer_entrance.runout_gcode

**Vor und Nach identisch.** runout_gcode wurde NICHT refactored — es
setzt eigene Flags (system_enabled=0, overfill_lock=0,
initial_lockout=0, initial_follow_active=0), ruft
`UPDATE_DELAYED_GCODE ID=_initial_follow_loop DURATION=0` und
`_APPLY_SYNC_STATE`. Die runout-PAUSE-Logik ist unveraendert.

## 3. feed_button

### Klick 1 (Dauerlauf)

**Vor:** Button-press_gcode setzt direkt inline
`manual_operation=1`, ruft `_SYNC_OFF`, setzt `_MANUAL_FEED.active=1`,
ruft `_MANUAL_FEED`. `_MANUAL_FEED` emittiert `FORCE_MOVE
DISTANCE=10 VELOCITY=15 ACCEL=1000` und ein `UPDATE_DELAYED_GCODE
ID=_manual_feed_loop DURATION=0.1`.

**Nach:** Button-press_gcode ruft nur `_BUTTON_CLICK_HANDLER
DIRECTION=FEED`. Der Handler macht inhaltlich dieselbe Sequenz mit
denselben Parametern aus `_FILAMENT_VARS`: `manual_operation=1`,
`_SYNC_OFF`, `_MANUAL_FEED.active=1`, `_MANUAL_FEED`. `_MANUAL_FEED`
emittiert nun `FORCE_MOVE DISTANCE={v.manual_chunk_distance=10}
VELOCITY={v.manual_speed=15} ACCEL={v.force_move_accel=1000}` und
`DURATION={v.manual_loop_tick=0.1}`.

**Aequivalenz:** Numerisch identisch. Pfad identisch.

### Klick 2 (Chunk-Puls)

**Vor:** press_gcode emittiert direkt einen 10-mm FORCE_MOVE,
setzt triple-click-counter, scheduled reset und reenable.

**Nach:** `_BUTTON_CLICK_HANDLER` emittiert denselben FORCE_MOVE
mit `puls_sign=1` -> `DISTANCE=1*{v.manual_chunk_distance}=10`,
`VELOCITY={v.manual_speed=15}`, `ACCEL={v.force_move_accel=1000}`.
Triple-Click-Counter wird gleich verwaltet, Reset-Delay ist
`{v.triple_click_window}=0.8`, Reenable-Cooldown ist
`{v.reenable_cooldown=1}`.

**Aequivalenz:** Identischer Output.

### Klick 3 (Triple-Burst)

**Vor:** ruft `_TRIPLE_FEED_BURST` nach Counter-Reset.

**Nach:** Handler ruft `{burst_macro}` das zu `_TRIPLE_FEED_BURST`
expandiert. `_TRIPLE_FEED_BURST` emittiert weiterhin `FORCE_MOVE
DISTANCE={v.triple_click_distance=500} VELOCITY={v.fast_speed=50}`,
jetzt `ACCEL={v.force_move_accel=1000}` und
`DURATION={v.reenable_cooldown_fast=0.5}`. Vor-Werte waren
ACCEL=1000 und DURATION=0.5.

**Aequivalenz:** Identisch.

### release_gcode

**Vor und Nach:** gleiche Logik. `v.reenable_cooldown=1` war vorher
hardcoded `DURATION=1`.

## 4. retract_button

Analog zu feed_button, mit folgenden Unterschieden:
- `puls_sign=-1` -> DISTANCE=-10
- **Vor-Refactor Velocity=20, nach Refactor Velocity=15** (siehe
  bekannte Abweichungen).

**Aequivalenz:** alle Pfade strukturell identisch bis auf die
Retract-Velocity-Vereinheitlichung.

## 5. buffer_hall1

**press_gcode:**

**Vor:** `SET VARIABLE=hall1_active VALUE=0`, M118, SET
`overfill_lock=0`, `_APPLY_SYNC_STATE`.

**Nach:** `SET hall1_active VALUE=0` entfernt. Rest identisch:
`M118 HALL1 FREI`, SET `overfill_lock=0`, `_APPLY_SYNC_STATE`.

**Aequivalenz-Argument:** Die entfernte Mirror-Var-Mutation ist seit
R-4 tot (kein Reader mehr). Weglassen hat keinen Effekt auf den
`_APPLY_SYNC_STATE`-Output. Raw-State des Buttons und der Guard-
Flag `overfill_lock` sind die einzigen Inputs des Output.

**release_gcode:**

**Vor:** SET hall1_active=1, M118, SET overfill_lock=1, SET
initial_lockout=0, SET initial_follow_active=0, 2 UPDATE_DELAYED
(follow_loop, follow_end), SET _MANUAL_FEED.active=0, 1 UPDATE_DELAYED
(_manual_feed_loop), `_APPLY_SYNC_STATE`.

**Nach:** SET hall1_active=1 entfernt. Die 5 SETs + 3 UPDATE_DELAYED
sind jetzt in `_ABORT_ALL_FEED_LOOPS` konsolidiert (das zusaetzlich
auch `_MANUAL_RETRACT.active=0` und `_manual_retract_loop DURATION=0`
stoppt — vor R-11 war dieser Retract-Abort im HALL1-release nicht
drin, aber _MANUAL_RETRACT.active war in dem Moment schon 0 durch den
Flow). Danach `_APPLY_SYNC_STATE`.

**Aequivalenz:** Der Retract-Abort ist **neu**, aber semantisch
no-op bei HALL1-Event: Wenn HALL1 triggert, ist eine Retract-Loop
nicht aktiv (Retract waere nicht zu HALL1-Overflow kommen). Falls
doch (z.B. User hat Retract-Taster gehalten), ist der Abort
ein praeventiver Safety-Stop und damit strikt besser, nicht
semantik-brechend. **Defensiv-Verbesserung, nicht Bug.**

## 6. buffer_hall2

**press_gcode / release_gcode:** SET hall2_active=0/=1 entfernt,
sonst identisch zu Vor-Version. Pfad: M118 + `_APPLY_SYNC_STATE`
(bei initial_lockout==0).

**Aequivalenz:** `_APPLY_SYNC_STATE` liest jetzt Raw-State statt
Mirror-Var. Bei korrekt synchronisierten Mirrors (Pre-R-4) war der
Output identisch. Beim **Mirror-Drift-Szenario** (z.B. Klipper-
Restart, Variablen auf 0 initialisiert, aber Sensor bereits aktiv)
liefert die neue Version den korrekten Hardware-State sofort, die
alte erst nach dem naechsten Button-Event. **Verbesserung.**

## 7. buffer_hall3

Analog zu HALL2, symmetrisch.

## 8. _boot_autostart

**Vor und Nach:** identisch. Der Autostart prueft `filament_detected`
und Raw-State von HALL1/HALL2, entscheidet dann zwischen
`BUFFER_AUTO_ON` (schon voll) und `FORCE_BUFFER_FILL` (muss
befuellen). Diese Logik wurde nicht refactored.

## 9. LOAD_FILAMENT

**Vor:** Speichert `variable_e_abs` inline via
`SET_GCODE_VARIABLE MACRO=LOAD_FILAMENT VARIABLE=e_abs VALUE=...` + M83.
Am Ende Restore via `{% if ... e_abs %}M82{% else %}M83{% endif %}`.
3 Phasen mit FORCE_MOVE + ACCEL=1000 in Phase 1+3.

**Nach:** `_SAVE_E_MODE` speichert in
`_BUFFER_AUTO_CONTROL.saved_e_abs`. `_RESTORE_E_MODE` liest und
emittiert M82/M83. FORCE_MOVE ACCEL auf `{v.force_move_accel=1000}`.

**Aequivalenz:** Identische Output-Sequenz. Der Speicherort der
Hilfs-Variable wurde von `LOAD_FILAMENT.e_abs` zu
`_BUFFER_AUTO_CONTROL.saved_e_abs` verschoben, ohne Wertsemantik-
Aenderung.

## 10. UNLOAD_FILAMENT + _UNLOAD_FAST_RETRACT

**Vor:** UNLOAD speichert inline, Tip-Forming + Sync-Retract + Phase 3
via `_UNLOAD_FAST_RETRACT`-Loop mit hardcoded DISTANCE=-10 und
`retracted + 10`. Exit-Branch restored M82/M83 ueber
`UNLOAD_FILAMENT.e_abs`.

**Nach:** UNLOAD ruft `_SAVE_E_MODE`. `_UNLOAD_FAST_RETRACT` nutzt
`{chunk = v.manual_chunk_distance=10}` fuer DISTANCE und Counter.
Exit-Branch `_RESTORE_E_MODE` liest `_BUFFER_AUTO_CONTROL.saved_e_abs`.

**Aequivalenz:** Chunk-Groesse numerisch identisch (10 mm),
Counter-Inkrement passt (10 -> +10 mit chunk=10). Restore-Pfad
holt denselben Wert aus anderer Variable.

**Defensive Verbesserung:** Magic-Coupling behoben — wenn man
`manual_chunk_distance` aendert, folgen Counter UND DISTANCE
automatisch. Pre-R-9 waere nur DISTANCE veraendert worden, Counter
waere dann out of sync.

## 11. STOP_BUFFER_FILL / BUFFER_AUTO_ON / FORCE_BUFFER_FILL

**STOP_BUFFER_FILL:** identisches Verhalten. `_ABORT_ALL_FEED_LOOPS`
konsolidiert die 5+3 SETs/UPDATE_DELAYED zu einem Helper-Call.

**BUFFER_AUTO_ON:** unveraendert (hatte andere Semantik als
_PREPARE_INITIAL_FILL — User-Design-Entscheidung).

**FORCE_BUFFER_FILL:** nutzt jetzt `_PREPARE_INITIAL_FILL`, identische
Output-Semantik.

## 12. ENABLE_RUNOUT_SENSOR / DISABLE_RUNOUT_SENSOR

**Vor (Camel_Snake):** `Enable_Runout_Sensor` / `Disable_Runout_Sensor`
setzen `print_running=1/0`.

**Nach (UPPER_SNAKE):** `ENABLE_RUNOUT_SENSOR` /
`DISABLE_RUNOUT_SENSOR`, identische Body. Plus `description:` fuer
UI-Integration.

**Aequivalenz:** Semantik identisch. Naming ist Breaking-Change
(User hat bestaetigt: nicht aus PRINT_START/END referenziert).

## Fazit

- Alle Event-Quellen liefern identischen `_APPLY_SYNC_STATE`-Output
  bzw. identische Seiteneffekte im Modell "Mirror-State == Hardware-
  State".
- Drei dokumentierte Verhaltensaenderungen, alle defensiv:
  1. Manual Feed/Retract Velocity vereinheitlicht (bewusst).
  2. HALL1-release deaktiviert jetzt auch Retract-Loop (Safety).
  3. `_UNLOAD_FAST_RETRACT`-Counter gekoppelt an `manual_chunk_distance`
     (Wartbarkeit, nicht Semantik-Bug).
- Kein Bereich mit Verhaltens-Regression identifiziert.
- Drucker-Integration-Test bleibt Endnachweis (Spec §12 Punkt 4).
