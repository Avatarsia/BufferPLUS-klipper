# Refactor Runde 2 Implementation Plan

> REQUIRED: superpowers:subagent-driven-development. Je Issue separater externer Codex-CLI-Agent; unabhängiger Task-Reviewer und finales Gesamt-Review.

**Goal:** Alle5freigegebenenRefactors behavior-preserving abschliessen.

**Architecture:** Feeder bleibt Klipper-/Mux-Adapter, die fuenf vorhandenen Verantwortungen erhalten Sidecars.

**Tech Stack:** Python3.x/Klipper/Pytest/nativeC/WSL.

**Spec:** docs/superpowers/specs/2026-10-13-refactor-round2-design.md

## Global Constraints

Der User hat die fuenf konkret vorgeschlagenen Punkte zur Umsetzung als Issues durch externe Subagenten freigegeben. Kein erneutes Ausfuehrungs-Go erforderlich. Keine Commits, Pushes, Merges, PRs, Deployments oder Hardware-Aktionen. Die ganze verifizierte erste Runde und alle vorherigen User-Aenderungen sind Ausgangsbasis.

Mechanische Extraktion mit ownergebundenen Komponenten und kompatiblen Feeder-Wrappers. State bleibt live am Owner; keine zweite Queue, keine allgemeine State-Migration, keine neuen Features oder Dependencies/Python-Version. Interne Calls weiter ueber Feeder-Wrappers fuer Tests/Diagnostik. Bestehende Source-Encoding/CRLF-LF erhalten, neue Dateien UTF-8. HALL-Sicherheit, JAM, Mux, SYNC, Cleanup-Policies, Boot-Anchor und Timing-Reihenfolge unveraendert. Keine historische Bug-/Skip-/Logging-Policy nebenbei aendern.

Reprime/MCU-Uhr, LateRecoveryClock nach Getter/Debug, Far-Future-SKIP und true completed stepgen statt wallclock bleiben erhalten. Watchdog behält P7-78-Override, kein neuer pauschaler Print-Block. Synthetische SyncCoordinator-Anker retain_skipped_distance=False; reale Filamentstreams behalten Restdistanz. Fehlgeschlagener SAVE bricht checked vor M83 ab; RESTORE-Recovery best-effort mit Retry-Latch; erfolgreicher expliziter Resave darf Slot ersetzen.

Tests: Windows Python C:/Users/3D Partner/AppData/Local/Programs/Python/Python312/python.exe -m pytest tests/ -q -p no:cacheprovider mit Timeout180. Default-Conda-Python hat kein pytest. Linux WSL Ubuntu /home/lexan/lll-timer-diagnosis-venv/bin/python tools/run_timing_regressions.py --klipper-path /home/lexan/lll-timer-diagnosis-klipper --check-unguarded. Klipper-Pin ce7002bedf37e938bb483572949f3703ac6476cb tracked clean. C5 positiv +3ExpectedRED spezifische Stale-auto-enable Assertion; kein Hardware-Nachweis. Baseline905passed8historicalSkips, keine Skip/Xfail-Aenderung.

## Reihenfolge und gemeinsame Oberflaechen

1 -> 2 -> 3 -> 4 -> 5. Alle beruehren buffer_feeder.py; Source-Writes deshalb sequenziell. Nebenbaustellen nicht refactoren. Je Task Snapshot, Charakterisierung, Implementierung, gezielte und volle Tests, isolierter Diff, Reviewer-Gate, ggf scoped Fix/Re-Review. Max5Fixrunden, finaler Review findet hoechstens einen gebuendelten Fixdispatch mit Re-Review.

## Review Focus

Stateful Metrics/Clock-Aufrufe nicht in reine Formatierung vorziehen; Print-Detektionen behalten ihre kontextabhaengigen Policies; Parserfehler vor/ nach Guards exakt wie Baseline; sensorbasierterResume und generischerResume getrennt; manuelle C-Owner nach kopiertem__dict__ mit eigenen Delegates, nie mit Owner1-Referenzen.

## Task 1: Refactor Runde 2: Diagnostik aus BufferFeeder extrahieren

Ownergebundene Diagnostik fuer Event-Logging, Metrics, State-Dump und Console-Response. Feeder behaelt kompatible Wrapper; Live-State bleibt am Owner. Keine neue Snapshot-Zeitgrenze oder veraenderte Log-Policy.

**Create:** klipper_extras/buffer_diagnostics.py (`BufferDiagnostics(owner)`, Feeder-Attribut `diagnostics`).

**Modify:** klipper_extras/buffer_feeder.py Imports/Init/kompatible Wrapper; nur noetige Consumer-Test-/C-Fixture-Verkabelung.

**Methods:** `_debug_event`, `_tick_debug_metrics`, `cmd_BUFFER_STATE_DUMP`, `_respond`.

**Akzeptanz:**

- Log-Level, Format, Rate-Limits und Reihenfolge der Getter/Clock-Aufrufe sind identisch.
- State-Dump und respond_info liefern dieselben Zeilen, ohne GCode-Mutex-Reentry/M117.
- Metrics behalten ihre vorhandenen Controller-Nebenwirkungen und Aktivierung; keine neue Berechnung im Motion-Pfad.
- LateRecoveryClock-Tests und komplette Suite bleiben gruen.

- [x] Betroffene Source und relevante NOT-TO-DO lesen.
- [x] Charakterisierung gegen unveraenderten Task-Snapshot mit existierenden Consumer-Vertraegen.
- [x] Nur diesen Refactor implementieren, Parserhelfer bei Task3 ausdruecklich trennen.
- [x] Gezielte Tests/full pytest sowie erforderliche echte C-Checks; no fake RED fuer mechanischen Refactor.
- [x] Scratch-Bericht/Diff/SourceFreeze, unabhaengiges Review und bei Findings scoped Fix/Re-Review.

## Task 2: Refactor Runde 2: Print-Session-Lifecycle kapseln

Print-Start/Pause/Resume/Ende, Print-Phase, Guards und Park-Fill als ein eigener Lifecycle. Live-Felder bleiben am Owner, Feeder-Event-/Methoden-Wrapper kompatibel. Kontextabhaengige Print-Detektion nicht homogenisieren oder vorzeitig cachen.

**Create:** klipper_extras/buffer_session.py (`PrintSessionCoordinator(owner)`, Feeder-Attribut `print_session`).

**Modify:** klipper_extras/buffer_feeder.py Imports/Init/kompatible Wrapper; nur noetige Consumer-Test-/C-Fixture-Verkabelung.

**Methods:** `_on_idle_printing`, `_clear_stale_suspend_if_print_inactive`, `_is_active_print_state`, `_get_print_stats_state`, `_get_tracker_velocity`, `_arm_critical_action_guard`, `_critical_action_guard_remaining`, `_set_print_phase`, `_maintain_msg_latches`, `_refresh_print_phase`, `_auto_submit_permission`, `_on_idle_ready`, `_maybe_park_full_on_print_end`.

**Akzeptanz:**

- Boot/standby darf keinen echten Print-Start ausloesen; echte Print-/Serial-Detektion unveraendert.
- Pause/Resume/Cancel/Error, JAM-Clear und Runout-Reinsert behalten Trigger/Reihenfolge.
- Park-Fill-One-Shot und HALL-Guards bleiben vor Bypass.
- Print-Phase/Guard-Zeitdomänen, BUFFER-Status/API und lokale Stale-Suspend-Recovery unveraendert.

- [x] Betroffene Source und relevante NOT-TO-DO lesen.
- [x] Charakterisierung gegen unveraenderten Task-Snapshot mit existierenden Consumer-Vertraegen.
- [x] Nur diesen Refactor implementieren, Parserhelfer bei Task3 ausdruecklich trennen.
- [x] Gezielte Tests/full pytest sowie erforderliche echte C-Checks; no fake RED fuer mechanischen Refactor.
- [x] Scratch-Bericht/Diff/SourceFreeze, unabhaengiges Review und bei Findings scoped Fix/Re-Review.

## Task 3: Refactor Runde 2: Legacy LOAD- und UNLOAD-Workflows extrahieren

Legacy-Distanz-/Phasensteuerung aus Feeder in eigenen Workflow-Owner extrahieren. Parameterparsing von Phase1, Phase3 und Legacy-UNLOAD in kleine ausdrueckliche Parser/Options trennen, am exakt bisherigen Zeitpunkt. Sensor-Workflow und Legacy-Workflow bleiben getrennt; vorhandene gemeinsame Cleanup-Scopes werden weiterverwendet.

**Create:** klipper_extras/buffer_legacy.py (`LegacyFilamentWorkflow(owner)`, Feeder-Attribut `legacy_workflow`).

**Modify:** klipper_extras/buffer_feeder.py Imports/Init/kompatible Wrapper; nur noetige Consumer-Test-/C-Fixture-Verkabelung.

**Methods:** `cmd_BUFFER_LOAD_PHASE1`, `_load_endstop_present`, `_load_to_endstop`, `cmd_BUFFER_LOAD_PHASE3`, `_load_phase3_tick`, `_exit_phase3_stable`, `cmd_BUFFER_UNLOAD_FILAMENT`, `cmd_BUFFER_UNLOAD_PHASE3`.

**Akzeptanz:**

- Parserreihenfolge, Defaults, Bounds, Fehler/Guards und Heating-/GCode-Scripts bleiben exakt erhalten.
- Sensor-/Distanzpfade, Already-present vor HALL1, 30-s-Overflow-Pause und Sensor-Resume-Owner bleiben erhalten.
- Phase3 OVERFLOW_OK/Stable-Exit/MAX_DISTANCE und JAM behalten ihre bisherigen Policies.
- STRICT/Best-Effort-Cleanup, Originalexception, SAVE/RESTORE-ACK und Mux-BUFFER-Selektoren unveraendert.
- Gerenderter Legacy-LOAD-Finalpush benutzt weiter BUFFER_LOAD_SYNCED; kein neuer Gesamt-Macro-Umbau.

- [x] Betroffene Source und relevante NOT-TO-DO lesen.
- [x] Charakterisierung gegen unveraenderten Task-Snapshot mit existierenden Consumer-Vertraegen.
- [x] Nur diesen Refactor implementieren, Parserhelfer bei Task3 ausdruecklich trennen.
- [x] Gezielte Tests/full pytest sowie erforderliche echte C-Checks; no fake RED fuer mechanischen Refactor.
- [x] Scratch-Bericht/Diff/SourceFreeze, unabhaengiges Review und bei Findings scoped Fix/Re-Review.

## Task 4: Refactor Runde 2: Idle-Watchdog und Anchor-Koordination kapseln

Die vier verbleibenden Watchdog-Helper (392 Zeilen in Baseline) in eigenen Coordinator verschieben. Gates, Diagnose und Anchor-Submit bleiben getrennte Methoden. Feeder/Main-Tick-Wrapper erhalten dieselbe Reihenfolge und Rueckgaben.

**Create:** klipper_extras/buffer_watchdog.py (`IdleWatchdogCoordinator(owner)`, Feeder-Attribut `idle_watchdog`).

**Modify:** klipper_extras/buffer_feeder.py Imports/Init/kompatible Wrapper; nur noetige Consumer-Test-/C-Fixture-Verkabelung.

**Methods:** `_tick_idle_watchdog`, `_log_idle_watchdog_blockers`, `_idle_watchdog_print_activity`, `_submit_idle_watchdog_anchor`.

**Akzeptanz:**

- Print-Block einschliesslich enger P7-78-Stale-Override- und Boot-Regeln unveraendert.
- Enable/Idle-Disable, HALL/SYNC/Pending-Guards, Warnungen und bestehende Rate-Limits unveraendert.
- Reactor-/MCU-Zeitdomänen, Getter- und Anchor/Clamping-Reihenfolge erhalten; Far-Future-Gap SKIP statt Clamp.
- Keine doppelte Flush-/Main-Tick-Submission; C5 positiv und drei spezifische --unguarded-Kontrollen bleiben gueltig.

- [x] Betroffene Source und relevante NOT-TO-DO lesen.
- [x] Charakterisierung gegen unveraenderten Task-Snapshot mit existierenden Consumer-Vertraegen.
- [x] Nur diesen Refactor implementieren, Parserhelfer bei Task3 ausdruecklich trennen.
- [x] Gezielte Tests/full pytest sowie erforderliche echte C-Checks; no fake RED fuer mechanischen Refactor.
- [x] Scratch-Bericht/Diff/SourceFreeze, unabhaengiges Review und bei Findings scoped Fix/Re-Review.

## Task 5: Refactor Runde 2: Streaming- und Submit-Koordination extrahieren

Chunk-Planung und Queue-Submit in ownergebundenen StreamingCoordinator verschieben. Reactor- und MCU-Flush-Einstiege bleiben separate Methoden. Pure Anchor-Planung, Motor-, Pending-, Controller-, Fault- und Cleanup-Owner behalten ihre bisherigen APIs. Keine neue Timing- oder Retry-Policy.

**Create:** klipper_extras/buffer_streaming.py (`StreamingCoordinator(owner)`, Feeder-Attribut `streaming`).

**Modify:** klipper_extras/buffer_feeder.py Imports/Init/kompatible Wrapper; nur noetige Consumer-Test-/C-Fixture-Verkabelung.

**Methods:** `_tick_pending_chunk`, `_flush_submit_streaming_chunk`, `_submit_move`, `_submit_single_trapezoid`, `_append_trapezoid_and_record`.

**Akzeptanz:**

- HALL1/HALL2/Abort/Endstop-Pruefungen bleiben vor Demand-/Park-Bypass.
- QUEUED/SKIPPED verbucht reale Distanz korrekt; synthetische Cursor-Anker bleiben explizit ohne Pending-Retry.
- SYNC/Local-/Global-Disable-Guards sowie Reprime/fresh-MCU/Anchor/Append-Reihenfolge unveraendert.
- Alle bestehenden Wrapper-Signaturen und Monkeypatch-Consumer erhalten; keine eigenen Trapq-Submits waehrend SYNC.
- Echte C-Harnesses initialisieren Delegate je manuell erzeugtem Owner; Serialqueue-Cleanup vor Steppersync; C5PASS und drei spezifische ExpectedRED.

- [x] Betroffene Source und relevante NOT-TO-DO lesen.
- [x] Charakterisierung gegen unveraenderten Task-Snapshot mit existierenden Consumer-Vertraegen.
- [x] Nur diesen Refactor implementieren, Parserhelfer bei Task3 ausdruecklich trennen.
- [x] Gezielte Tests/full pytest sowie erforderliche echte C-Checks; no fake RED fuer mechanischen Refactor.
- [x] Scratch-Bericht/Diff/SourceFreeze, unabhaengiges Review und bei Findings scoped Fix/Re-Review.
