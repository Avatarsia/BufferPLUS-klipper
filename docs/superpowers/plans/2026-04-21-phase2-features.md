# LLL Buffer Plus Phase 2 — Feature Integration Plan

> **For agentic workers:** Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Integrate features from user-provided `LLL_Plus.cfg` into the refactored `lll.cfg` from Phase 1 without regressing Phase-1 quality (helper-macros, parametrized magics, raw-state queries).

**Architecture:** Extend existing Phase-1 helpers. New state flags (`sync_locked`, `sync_state`, `runout_filament_ref`) in `_BUFFER_AUTO_CONTROL`. New calibration macros (`MEASURE_LOAD_*`, `CALIBRATE_FEEDER_SYNC`). Sensor-driven LOAD (HALL2-triggered). UNLOAD with 50mm chunks respecting `max_extrude_only_distance`. Boot simplified. M117 display gated by switch.

**Tech Stack:** Klipper Mainline, Jinja2 templates. No new dependencies.

**Validation mode:** Grep-based pre/post verification per task. Equivalence-doc updated at end.

---

## Task P2-1: `_FILAMENT_VARS` Kalibrierwerte + neue Variablen

**Files:** Modify `printer_data/config/lll.cfg`

- [ ] Update `sync_rotation_distance: 19.5` → `18.86`
- [ ] Update `rotation_distance: 19.5` in `[extruder_stepper mellow]` → `18.86`
- [ ] Update `triple_click_distance: 500` → `1300`
- [ ] Update `triple_click_window: 0.8` → `1.5`
- [ ] Remove `variable_load_fast1: 810` and `variable_load_fast2: 1500`
- [ ] Add `variable_load_fast_distance: 1000`
- [ ] Add `variable_load_buffer_max: 2000`
- [ ] Add `variable_runout_pause: 0` with comment explaining 0/1
- [ ] Add `variable_display_status_enabled: 1` with comment for debug-off
- [ ] Commit: `refactor(lll.cfg): update calibration + new LOAD/runout/display vars`

## Task P2-2: `_BUFFER_AUTO_CONTROL` neue State-Flags

**Files:** Modify `printer_data/config/lll.cfg`

- [ ] Add `variable_sync_locked: 0` with comment
- [ ] Add `variable_sync_state: 0` with comment (Latch: 0=init, 1=fast, 2=slow)
- [ ] Add `variable_runout_filament_ref: 0` with comment
- [ ] Confirm `saved_e_abs` still present, mirror-vars still absent
- [ ] Commit: `refactor(lll.cfg): add sync_locked, sync_state, runout_filament_ref`

## Task P2-3: `_APPLY_SYNC_STATE` sync_locked + Latch-Hysterese

**Files:** Modify `printer_data/config/lll.cfg`

- [ ] Wrap body in `{% if b.sync_locked == 0 %}...{% endif %}` (early-exit)
- [ ] Add latch logic: HALL3-aktiv → `SET sync_state=1 DISTANCE=fast_rd`
- [ ] HALL2-aktiv → `SET sync_state=2 DISTANCE=slow_rd`
- [ ] kein Hall → `if sync_state==1: fast_rd; elif==2: slow_rd; else: nominal`
- [ ] Raw-state-queries (R-4) bleiben; keine Mirror-Var-Reads
- [ ] Comment-block updated: Hysterese-Semantik erklaeren
- [ ] Commit: `refactor(lll.cfg): _APPLY_SYNC_STATE adds sync_locked + hysteresis latch`

## Task P2-4: `_SYNC_OFF` erweitern

**Files:** Modify `printer_data/config/lll.cfg`

- [ ] Nach `SYNC_EXTRUDER_MOTION ... MOTION_QUEUE=` zusaetzlich
  `SET_EXTRUDER_ROTATION_DISTANCE EXTRUDER=mellow DISTANCE={v.sync_rotation_distance}`
- [ ] Comment: "Stellt sicher dass FORCE_MOVE-Ops immer mit 1:1 arbeiten"
- [ ] Commit: `refactor(lll.cfg): _SYNC_OFF also resets rotation_distance to nominal`

## Task P2-5: MEASURE_LOAD + CALIBRATE_FEEDER_SYNC

**Files:** Modify `printer_data/config/lll.cfg`

- [ ] Add `[gcode_macro _MEASURE_LOAD_STATE]` with `variable_active: 0`, `variable_distance: 0`
- [ ] Add `[gcode_macro MEASURE_LOAD_START]` with description, setzt active=1/distance=0, manual_operation=1, `_SYNC_OFF`, M118 user-prompt, optional M117
- [ ] Add `[gcode_macro MEASURE_LOAD_STOP]` mit description, gibt dist aus, active=0, `_APPLY_SYNC_STATE`
- [ ] Add `[gcode_macro CALIBRATE_FEEDER_SYNC]` mit description — Sync an, rotation_distance nominal, M118 instructions
- [ ] Extend `_MANUAL_FEED`: wenn `_MEASURE_LOAD_STATE.active==1`, inkrementiere `_MEASURE_LOAD_STATE.distance` um `v.manual_chunk_distance` pro Chunk
- [ ] Commit: `feat(lll.cfg): add MEASURE_LOAD_* and CALIBRATE_FEEDER_SYNC`

## Task P2-6: `_BUTTON_CLICK_HANDLER` Mess-Modus-Toggle

**Files:** Modify `printer_data/config/lll.cfg`

- [ ] Am Anfang von `_BUTTON_CLICK_HANDLER`: Pruefung
  `{% if _MEASURE_LOAD_STATE.active == 1 and params.DIRECTION == 'FEED' %}`
- [ ] Im Mess-Modus: wenn `_MANUAL_FEED.active == 0` -> starte Foerderung (wie Klick 1)
- [ ] Wenn `_MANUAL_FEED.active == 1` -> stoppe + rufe `MEASURE_LOAD_STOP`
- [ ] Sonst: bestehende Triple-Click-Logik
- [ ] `feed_button release_gcode` wrappen: nur wenn `_MEASURE_LOAD_STATE.active == 0` ausfuehren
- [ ] Commit: `feat(lll.cfg): _BUTTON_CLICK_HANDLER supports MEASURE_LOAD toggle`

## Task P2-7: Runout mit Nachlauf

**Files:** Modify `printer_data/config/lll.cfg`

- [ ] `buffer_entrance.runout_gcode` neu schreiben:
  - M118 Runout-Nachricht + optional M117
  - SET sync_locked=1, overfill_lock=0, initial_lockout=0, initial_follow_active=0, sync_state=0
  - UPDATE_DELAYED_GCODE ID=_initial_follow_loop DURATION=0
  - `{% set v %}` und `{% set ref = printer.print_stats.filament_used %}`
  - `{% if v.runout_pause == 1 %}` — sofort: SYNC_OFF, SET_STEPPER_ENABLE STEPPER="extruder_stepper mellow" ENABLE=0, optional PAUSE
  - `{% else %}` — `SET runout_filament_ref={ref}`, UPDATE_DELAYED_GCODE ID=_runout_stepper_disable DURATION=1
- [ ] Add `[delayed_gcode _runout_stepper_disable]`:
  - Liest `filament_used` + `runout_filament_ref`
  - Wenn cur >= ref+100: Stepper disable, sync_locked=0, system_enabled=0, manual_operation=0, SYNC_EXTRUDER_MOTION MOTION_QUEUE=
  - Sonst: Self-call DURATION=1
- [ ] Commit: `feat(lll.cfg): runout supports external sensor with 100mm feeder runout`

## Task P2-8: LOAD_FILAMENT sensorgesteuert

**Files:** Modify `printer_data/config/lll.cfg`

- [ ] `LOAD_FILAMENT` umschreiben:
  - Drop `variable_e_abs`
  - State: `variable_state: 0`, `variable_distance: 0`
  - Temp-Check bleibt, `_SAVE_E_MODE`, manual_operation=1, `_SYNC_OFF`
  - Setze state=1, distance=0, ruft `_LOAD_PH_ONE_LOOP`
- [ ] Add `[gcode_macro _LOAD_PH_ONE_LOOP]`:
  - State-guards. Wenn distance >= load_fast_distance: ruft `_LOAD_PH_TWO`
  - Sonst: FORCE_MOVE chunk (v.manual_chunk_distance) mit v.fast_speed + v.force_move_accel
  - Increment distance, UPDATE_DELAYED_GCODE `_load_ph_one_delayed`
- [ ] Add `[delayed_gcode _load_ph_one_delayed]` → ruft `_LOAD_PH_ONE_LOOP`
- [ ] Add `[gcode_macro _LOAD_PH_TWO]`:
  - M118/M117 Phase 2
  - `sync_locked=1`, SYNC an + rotation_distance nominal
  - 50mm-Chunks via `{% for i in range(full) %}G1 E{chunk}`
  - M400. `sync_locked=0`, `_SYNC_OFF`
  - Setze state=3, distance=0, ruft `_LOAD_PH_THREE_LOOP`
- [ ] Add `[gcode_macro _LOAD_PH_THREE_LOOP]`:
  - Liest `hall2_active` via **Raw-State** `printer["gcode_button buffer_hall2"].state == "RELEASED"`
  - Exit wenn hall2_active: state=0, `_RESTORE_E_MODE`, manual_operation=0, `_APPLY_SYNC_STATE`
  - Exit wenn distance >= load_buffer_max: gleiche Exit + Warnung
  - Sonst: FORCE_MOVE chunk + increment + self-delayed
- [ ] Add `[delayed_gcode _load_ph_three_delayed]` → ruft `_LOAD_PH_THREE_LOOP`
- [ ] Commit: `feat(lll.cfg): LOAD_FILAMENT sensor-driven via HALL2, 3-stage state machine`

## Task P2-9: UNLOAD_FILAMENT 50mm-Chunks + Phase 3 split

**Files:** Modify `printer_data/config/lll.cfg`

- [ ] `UNLOAD_FILAMENT` umschreiben:
  - Drop `variable_e_abs`, `variable_retracted` bleibt
  - Temp-Check, `_SAVE_E_MODE`, manual_operation=1
  - Phase 1: `sync_locked=1`, SYNC an nominal, Tip-Forming Zyklen, `tip_final_retract`, M400
  - Phase 2: 50mm-Chunks `G1 E-<chunk>` bei slow_speed, bis unload_sync erreicht, M400
  - `sync_locked=0`, `_SYNC_OFF`
  - Phase 3a: FORCE_MOVE -load_fast_distance am Stueck mit fast_speed + force_move_accel
  - Phase 3b: set state='fast_retract', retracted=load_fast_distance (**nicht 0**), ruft `_UNLOAD_FAST_RETRACT`
- [ ] `_UNLOAD_FAST_RETRACT` anpassen:
  - chunk = 50 (war 10 via manual_chunk_distance in unserer R-9-Loesung). **Entscheidung:** ich nutze `v.manual_chunk_distance` verdoppelt, NEIN besser neuer Variable oder hardcoded 50? Spec sagt 50. Laut LLL_Plus.cfg: hardcoded 50. **Entscheidung:** hardcoded `chunk = 50` in dem Macro (lokale Jinja-Variable), plus Kommentar warum.
  - Exit-Branch ruft `_RESTORE_E_MODE`
- [ ] Commit: `feat(lll.cfg): UNLOAD_FILAMENT uses 50mm chunks, phase 3 split`

## Task P2-10: Boot-Autostart + M117 mit display_status_enabled

**Files:** Modify `printer_data/config/lll.cfg`

- [ ] `_boot_autostart` vereinfachen:
  - Wenn `filament_detected`: M118 + optional M117 + `BUFFER_AUTO_ON`
  - Sonst: M118 Standby, kein FORCE_BUFFER_FILL
- [ ] M117-Integration alle Stellen aus LLL_Plus.cfg identifizieren und mit Wrapper einfuegen:
  `{% if printer["gcode_macro _FILAMENT_VARS"].display_status_enabled == 1 %}M117 <text>{% endif %}`
  Orte: feed_button (3 Branches), retract_button (3 Branches), runout, LOAD-Phasen, UNLOAD-Phasen, Triple-Burst, MEASURE_LOAD_*, CALIBRATE_FEEDER_SYNC, _boot_autostart
- [ ] Commit: `feat(lll.cfg): simplify _boot_autostart, add M117 with display toggle`

## Task P2-11: README + Equivalence-Doc-Update

**Files:**
- Modify: `README.md`
- Modify: `docs/superpowers/specs/2026-04-21-lll-buffer-refactor-equivalence.md`

- [ ] README: Section "Kalibrierung" hinzufuegen mit 6 Schritten:
  1. Pflicht-Variablen anpassen
  2. `sync_rotation_distance` kalibrieren via `CALIBRATE_FEEDER_SYNC`
  3. `load_fast_distance` kalibrieren via `MEASURE_LOAD_START`/`MEASURE_LOAD_STOP`
  4. `load_slow` / `unload_sync` ausmessen
  5. Erstbefuellung testen
  6. LOAD_FILAMENT testen
- [ ] Equivalence-Doc: neue Section "Phase 2 Verhaltensaenderungen":
  - Hysterese (sync_state-Latch statt Neutral-Fallback)
  - Boot-Autostart ohne Auto-FORCE_BUFFER_FILL
  - Runout mit 100mm Nachlauf bei externem Sensor
  - LOAD HALL2-getriggert statt festdistanz
  - UNLOAD mit 50mm-Chunks statt einem Block
- [ ] Commit: `docs: README calibration guide + equivalence phase 2 section`

## Task P2-12: Push

**Files:** none (git op)

- [ ] User-Freigabe einholen mit Changelog der Phase 2
- [ ] `git push -u origin rebuild-sync-v2`
- [ ] Keine PR automatisch

---

## Self-Review

**Spec coverage:** §3 (Vars) → P2-1. §4 (State-Flags) → P2-2. §5 (APPLY_SYNC_STATE) → P2-3. §6 (SYNC_OFF) → P2-4. §7 (MEASURE_LOAD) → P2-5. §8 (CALIBRATE_FEEDER_SYNC) → P2-5. §9 (Button-Mess-Toggle) → P2-6. §10 (Runout) → P2-7. §11 (LOAD) → P2-8. §12 (UNLOAD) → P2-9. §13 (Boot) → P2-10. §14 (M117) → P2-10. §15 (Nicht-Aenderungen) → impliziert in allen Tasks. §16 (Validation) → P2-11. §17 (Out-of-scope) → dokumentiert. §18 (DoD) → P2-12.

**Placeholder scan:** keine "TBD"/"TODO". Jeder Task hat konkrete Aenderungs-Schritte mit Variable/Macro-Namen.

**Type consistency:** `sync_locked`, `sync_state`, `runout_filament_ref` konsistent ueber P2-2, P2-3, P2-7. Hall-Raw-State-Query identisch zum Phase-1-Pattern.

**UNLOAD chunk-size Diskrepanz:** In P2-9 verwenden wir `chunk=50` hardcoded (passt LLL_Plus.cfg), das weicht von R-9's `manual_chunk_distance=10` ab. Bewusste Entscheidung: UNLOAD-Phase-3b-Chunk ist NICHT der gleiche Konzeptraum wie Manual-Loop-Chunk. Separat behalten.
