# Refactor Analysis Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** Implement all seven approved refactor areas without losing the existing sensor-load changes.

**Architecture:** Incremental responsibility extraction. BufferFeeder remains the Klipper command/event adapter; existing public commands and status keys remain compatible.

**Tech Stack:** Python 3.x, Klipper motion_queuing/trapq, pytest, Python diagnostic runner.

**Spec:** docs/superpowers/specs/2026-10-13-refactor-analysis-design.md

## Global Constraints

- No commits, pushes, merges or unrelated fixes.
- Preserve all Safety-Invarianten from the spec and relevant NOT-TO-DO entries.
- Preserve existing encoding/line endings; new files UTF-8; no trailing whitespace.
- Separate implementing subagent per task; overlapping source changes sequential.
- Test bugs with observed RED then GREEN; mechanical refactors use behavioral characterization and full regression suite.
- Tests: `"C:/Users/3D Partner/AppData/Local/Programs/Python/Python312/python.exe" -m pytest tests/ -q -p no:cacheprovider` (180-second timeout).

## Review Focus

- Connect happens after construction and no-flush tests exercise actual motor scheduling.
- Runtime parameter aliases, bounds, caps and tracker side effects stay compatible.
- Endstop during overflow pause, JAM and already-present paths keep their distinct cleanup.
- Main-tick ordering prevents watchdog/flush duplicate submissions.
- Nested global disable restores previous guards; C-generation progress is not MCU time.

## Execution order

1, 2, 5, 3, 4, 6, 7. Each issue has a fresh implementer and task reviewer. User explicitly authorized implementation and chose separate subagents; no repeated execution approval.

### Task 1: Test-Fixtures und GCode-Fakes konsolidieren

**Issue:** https://github.com/Avatarsia/BufferPLUS-klipper/issues/76

**Files / evidence:** tests/conftest.py:61, tests/helpers.py, tests/test_enable_does_not_flush_toolhead.py:23. 33 lokale Feeder-Builder und 10 FakeGCmd-Klassen erzeugen Lifecycle-Drift. Connect wird im gemeinsamen Builder vor der Konstruktion ausgeloest.

**Interfaces:** Preserve public command/status signatures. New small component methods are consumed only through BufferFeeder adapters; tests verify the first observable consumer result.

**Implementation:** Gemeinsamer Feeder-Builder und GCmd-Stub; kompatible lokale Varianten migrieren, spezielle Setups ueber explizite Parameter/gezielte Nachkonfiguration erhalten. Connect immer nach Konstruktion.

**Acceptance:**
- Fixture-Regression reproduziert den Fehler vor dem Fix und wird danach gruen.
- Enable-/Disable-no-flush-Tests durchlaufen den echten Plugin-Scheduler und weisen Motor-Aufrufe nach.
- Keine bool("0")-Semantik; Config-Bounds und bestehende Spezial-Setups bleiben erhalten.

- [x] Read the affected implementation and relevant regression tests.
- [x] Add behavioral characterization; for the identified bug observe failing regression first.
- [x] Implement this issue only, preserving public wrappers for private methods used by diagnostics/tests.
- [x] Run targeted tests and complete pytest suite with timeout; report exact results.
- [x] Save report with changed files, RED/GREEN evidence and remaining limits to task scratch.
- [x] Independent review; fix any important findings and re-run covering tests.

### Task 2: Config-, Runtime-Tuning- und Status-Mappings zentralisieren

**Issue:** https://github.com/Avatarsia/BufferPLUS-klipper/issues/77

**Files / evidence:** klipper_extras/buffer_config.py, buffer_feeder.py:4848 und :5209 pflegen dieselben Parameter mehrfach. BUFFER_SET umfasst 219 Zeilen.

**Interfaces:** Preserve public command/status signatures. New small component methods are consumed only through BufferFeeder adapters; tests verify the first observable consumer result.

**Implementation:** Kleine deklarative Tuning-Tabelle fuer Parser, GCode-Key, Attribut, Bounds, Einheit und Status-Key; abhaengige Caps und Tracker-Nebenwirkungen als ausdrueckliche Hooks erhalten. Gemeinsame Regeln nur fuer tatsaechlich gemeinsame Parameter verwenden.

**Acceptance:**
- MAX_MOVE_CHUNK_MM wird vor INTERRUPT_CHUNK_MM angewendet; gemeinsames Anheben und Absenken bleiben korrekt.
- Filamentdurchmesser aktualisiert den Velocity-Tracker.
- Oeffentliche GCode-/Status-Namen, Legacy-Aliasse und Config-Fallbacks bleiben kompatibel.

- [x] Read the affected implementation and relevant regression tests.
- [x] Add behavioral characterization; for the identified bug observe failing regression first.
- [x] Implement this issue only, preserving public wrappers for private methods used by diagnostics/tests.
- [x] Run targeted tests and complete pytest suite with timeout; report exact results.
- [x] Save report with changed files, RED/GREEN evidence and remaining limits to task scratch.
- [x] Independent review; fix any important findings and re-run covering tests.

### Task 3: Pending-Stream-Lifecycle und Submit-Ergebnis kapseln

**Issue:** https://github.com/Avatarsia/BufferPLUS-klipper/issues/78

**Files / evidence:** klipper_extras/buffer_feeder.py:2152, :3117, :3225, :3552; buffer_sensors.py:97; buffer_cleanup.py:28. Zusammengehoerige Pending-Werte und Resume-Snapshots werden verteilt zurueckgesetzt.

**Interfaces:** Preserve public command/status signatures. New small component methods are consumed only through BufferFeeder adapters; tests verify the first observable consumer result.

**Implementation:** Kleine Lifecycle-Operationen fuer Start/Fortschritt/Finish und getrenntes Verwerfen von Overflow-Resume. Submit meldet ausdruecklich queued/skipped; Distanzbuchhaltung basiert auf tatsaechlich gequeueter Bewegung.

**Acceptance:**
- Skip/Reject verbraucht keine noch zu foerdernde Distanz; Regression zu #63.
- Park-Demand-Bypass erhaelt vorgeschaltete HALL1/HALL2-Guards.
- Endstop/HALT/JAM/Exhaustion raeumen ihre bisherigen Feldmengen; in-flight Moves und Overflow-Resume-Snapshot bleiben korrekt.

- [x] Read the affected implementation and relevant regression tests.
- [x] Add behavioral characterization; for the identified bug observe failing regression first.
- [x] Implement this issue only, preserving public wrappers for private methods used by diagnostics/tests.
- [x] Run targeted tests and complete pytest suite with timeout; report exact results.
- [x] Save report with changed files, RED/GREEN evidence and remaining limits to task scratch.
- [x] Independent review; fix any important findings and re-run covering tests.

### Task 4: FeedSpeedController extrahieren und Main-Tick gliedern

**Issue:** https://github.com/Avatarsia/BufferPLUS-klipper/issues/79

**Files / evidence:** klipper_extras/buffer_feeder.py:1428 (_main_tick:588 Zeilen), :2312 sowie Carry-/Clamp-Helper ab :212. buffer_modulator.py enthaelt derzeit nur den Velocity-Tracker.

**Interfaces:** Preserve public command/status signatures. New small component methods are consumed only through BufferFeeder adapters; tests verify the first observable consumer result.

**Implementation:** Eigene Regelungskomponente mit explizitem Demand-/Sensor-/Zeit-Snapshot und eigener Carry/Hysterese/Recovery-Verantwortung. Feeder bleibt Adapter. Main-Tick mechanisch in benannte Verantwortungsbloecke aufteilen, Ausfuehrungsreihenfolge beibehalten.

**Acceptance:**
- HALL1/HALL2 stoppen weiterhin vor Demand-Bypasses.
- HALL3-Dwell, High-Flow-Carry, Zwischenzone und Session-Reset liefern dieselben Ergebnisse.
- Bestehender Watchdog-Print-Block einschliesslich enger P7-78-Stale-Override-Regeln bleibt unveraendert; kein zweiter Submit aus Main-Tick bei Flush-Ownership.

- [x] Read the affected implementation and relevant regression tests.
- [x] Add behavioral characterization; for the identified bug observe failing regression first.
- [x] Implement this issue only, preserving public wrappers for private methods used by diagnostics/tests.
- [x] Run targeted tests and complete pytest suite with timeout; report exact results.
- [x] Save report with changed files, RED/GREEN evidence and remaining limits to task scratch.
- [x] Independent review; fix any important findings and re-run covering tests.

### Task 5: Runtime-State-Ownership als Initializer ausdruecklich machen

**Issue:** https://github.com/Avatarsia/BufferPLUS-klipper/issues/80

**Files / evidence:** klipper_extras/buffer_state.py:109 und buffer_config.py:225 kopieren Felder auf den Feeder. buffer_feeder.py:78-99 speichert diese Startobjekte trotzdem als settings/runtime_state; Live-Mutationen passieren am Owner.

**Interfaces:** Preserve public command/status signatures. New small component methods are consumed only through BufferFeeder adapters; tests verify the first observable consumer result.

**Implementation:** Live-Owner-Verantwortung dokumentieren und Initialwerte ausdruecklich initial_settings/initial_runtime_state benennen. Kompatible lesbare Legacy-Aliasse erhalten, soweit sinnvoll; keine globale Migration aller State-Zugriffe in diesem Issue.

**Acceptance:**
- BUFFER_SET wirkt weiter auf Live-Status und Motion.
- Neue Namen vermitteln Snapshot-Semantik; Legacy-Zugriff bleibt kompatibel.
- Init-Werte fuer Safety, Sync und Recovery bleiben unveraendert.

- [x] Read the affected implementation and relevant regression tests.
- [x] Add behavioral characterization; for the identified bug observe failing regression first.
- [x] Implement this issue only, preserving public wrappers for private methods used by diagnostics/tests.
- [x] Run targeted tests and complete pytest suite with timeout; report exact results.
- [x] Save report with changed files, RED/GREEN evidence and remaining limits to task scratch.
- [x] Independent review; fix any important findings and re-run covering tests.

### Task 6: Overflow- und Guaranteed-Cleanup-Verantwortung zusammenfuehren

**Issue:** https://github.com/Avatarsia/BufferPLUS-klipper/issues/81

**Files / evidence:** klipper_extras/buffer_feeder.py:1209/:1277, buffer_fault.py:69; sensorbasierte Resume-Schleife :4249. UNLOAD :4447 und buffer_toolhead.py:35 implementieren aehnliche SAVE/SYNC/RESTORE-Scopes.

**Interfaces:** Preserve public command/status signatures. New small component methods are consumed only through BufferFeeder adapters; tests verify the first observable consumer result.

**Implementation:** Overflow-Entry/Exit/Resume im FaultManager organisieren; den blockierenden Sensor-LOAD als ausdruecklichen Resume-Owner erhalten. Gemeinsamer Python-Operation-Scope fuer SAVE/RESTORE und separater SYNC-Scope mit ausdruecklicher strict/best-effort-Policy.

**Acceptance:**
- Aktuelle lokale HALL1-Pause/Resume-Aenderungen und 30-s-Timeout bleiben erhalten.
- JAM-Latch, Already-present-Skip und SYNC-Gate bleiben erhalten.
- Fehler bei SAVE/M83/SYNC/G1/UNSYNC/RESTORE verlieren weder den Originalfehler noch den benoetigten Cleanup.
- Alle nested Commands behalten BUFFER-Selector; Cleanup bleibt Python-try/finally.

- [x] Read the affected implementation and relevant regression tests.
- [x] Add behavioral characterization; for the identified bug observe failing regression first.
- [x] Implement this issue only, preserving public wrappers for private methods used by diagnostics/tests.
- [x] Run targeted tests and complete pytest suite with timeout; report exact results.
- [x] Save report with changed files, RED/GREEN evidence and remaining limits to task scratch.
- [x] Independent review; fix any important findings and re-run covering tests.

### Task 7: Zeitanker- und Motor-Disable-Koordination isolieren

**Issue:** https://github.com/Avatarsia/BufferPLUS-klipper/issues/82

**Files / evidence:** klipper_extras/buffer_feeder.py:3023-3116, :3368-3495. Printerweiter Disable-Wrapper, lokale Guards und Anchor-Planung liegen im Feeder.

**Interfaces:** Preserve public command/status signatures. New small component methods are consumed only through BufferFeeder adapters; tests verify the first observable consumer result.

**Implementation:** Bestehenden AnchorPlan fuer reine Entscheidungen aus expliziten Snapshots verwenden; Klipper-Zugriff/Plan-Anwendung im Adapter. Printerweiter DisableCoordinator und instanzbezogener Motor-Lifecycle kapseln die bestehenden Guards/Drain-Regeln. Unit- und echte C-Harnesses als dokumentierten gemeinsamen Check buendeln.

**Acceptance:**
- forced_t0=None und Far-Future-Toolhead-Zeit fuehren weiterhin zu SKIP statt Clamp.
- MCU-Zeit und tatsaechlicher Stepgen-Fortschritt bleiben getrennt.
- Globales/gezieltes/nested Disable drainnt eigene Moves und restauriert vorherige Guards.
- Enable erzeugt keinen Toolhead-Flush; Boot-Anchor und SYNC-Queue-Ownership bleiben erhalten.
- Beide C-Harnesses laufen auf Linux gegen einen protokollierten Klipper-Commit; Kontrolllauf --unguarded muss erwartungsgemaess scheitern.

- [x] Read the affected implementation and relevant regression tests.
- [x] Add behavioral characterization; for the identified bug observe failing regression first.
- [x] Implement this issue only, preserving public wrappers for private methods used by diagnostics/tests.
- [x] Run targeted tests and complete pytest suite with timeout; report exact results.
- [x] Save report with changed files, RED/GREEN evidence and remaining limits to task scratch.
- [x] Independent review; fix any important findings and re-run covering tests.

## Verifizierter lokaler Abschluss (2026-10-13)

Alle sieben Issues wurden durch separate Implementierer umgesetzt und unabhaengig reviewed. Die Ausgangsbasis enthielt bereits lokale User-Aenderungen; sie blieb Teil des Refactors. Es wurden keine Commits, Pushes, PRs oder Deployments ausgefuehrt. Issues bleiben bis zur Veroeffentlichung offen.

| Issue | Ergebnis | Task-Gate |
|---|---|---|
| #76 | Gemeinsamer Builder/GCmd; echter Connect-/Motor-Scheduler im no-flush-Test | CLEAN |
| #77 | 17 Parameter und 12 Status-Mappings zentral; 58 Differentialfaelle identisch | CLEAN |
| #78 | Pending-Lifecycle und explizites QUEUED/SKIPPED; reale Restdistanz erhalten | CLEAN |
| #79 | FeedSpeedController und Main-Tick 588 -> 49 Zeilen; spaete Recovery-Uhr erhalten | CLEAN nach einem Fix/Re-Review |
| #80 | Initializer-Snapshots benannt; kompatible Aliasse und Live-Owner erhalten | CLEAN |
| #81 | Fault-Lifecycle, gemeinsame Cleanup-Scopes, garantierter finaler Legacy-LOAD-Push | CLEAN |
| #82 | Reiner Anchor-Planner, Motor-/Disable-Koordination, gemeinsamer Linux-Runner | CLEAN |

Fuenf abschliessende technische Review-Scopes: Architektur, Workflows/Mux/Cleanup, Tests/Vertraege, Safety/Motion und Plattform/Performance. Alle CLEAN ohne offene Findings. Wegen vier verfuegbarer Agent-Slots liefen die Reviews in Wellen; Datenschutz-/WordPress-Pruefungen passen nicht zu dieser Klipper-Extension.

Eigenstaendige Review-Nachweise: 31 vorhandene Methodensignaturen, 23 bisherige Mux-Registrierungen und alle 58 Status-Ausdruecke erhalten; Importgraph der 17 Module aufloesbar ohne Zyklen. Beide Installer erfassen neue Sidecars. Alle 1.466 bestehenden Assertions und Skip/Xfail-Regeln erhalten. 12.000 Anchor-Plaene identisch, darunter 1.316 reentrante Lookups; 10 kritische Motion-Consumer AST-identisch; 44 gezielte finale Workflow-Consumer-Tests bestanden.

Gesamtsuite: Ausgangsbasis 695 passed / 8 skipped, final 905 passed / 8 skipped unter Windows und Linux. Die 210 zusaetzlichen Tests pruefen die neuen Consumer-/Fehlervertraege; acht historische HALL2-Skips bleiben unveraendert (separates Issue #69). BufferFeeder 5.283 -> 4.653 Zeilen, gesamter Plugin-Code 7.297 -> 7.645 Zeilen: Verantwortung wurde in kleinere Komponenten verschoben, kompatible Adapter bleiben.

Echte C-Verifikation: zwei lokale und drei globale/multi Disable-/EnableTracking-Faelle PASS; drei einzelne --unguarded-Kontrollen liefern Exit1 mit genau `AssertionError: Stale auto-enable fired`. Klipper-Pin `ce7002bedf37e938bb483572949f3703ac6476cb`, tracked Source clean. Runner weist falschen Pin, staged/unstaged tracked Source und fehlende Voraussetzungen vor Beginn der Tests ab. Keine Hardware-, TMC/UART- oder Transport-Scheduling-Freigabe aus diesen Komponentenchecks ableiten.

Gemeinsamer Linux-Check, mit vorhandenem Python-Environment samt pytest/cffi/gcc:

```text
python tools/run_timing_regressions.py --klipper-path /pfad/zu/klipper --check-unguarded
```

Windows-Unit-Suite:

```text
python -m pytest tests/ -q -p no:cacheprovider
```

## Festgehaltene Implementierungsentscheidungen

1. Stream-Finish raeumt Distanz, Cap und Park-Latch konsistent; Caller-Flags, bereits gequeueter Move und Overflow-Snapshot bleiben getrennt. Eine spaeter benoetigte abweichende Recovery-Cap-Policy muesste ausdruecklich als eigenes Profil mit Regression entstehen.
2. Der vorhandene enge P7-78-Stale-Watchdog-Override bleibt erhalten. Ein pauschaler Print-Block waere eine neue Timing-Policy und benoetigte eine separate Diagnose samt Hardware-Nachweis.
3. Nur synthetische SyncCoordinator-Cursoranker behalten Skip-ohne-Retry; reale Filamentstreams behalten Restdistanz. Ein Retry synthetischer Anker im blockierenden Main-Tick-Waiter erzeugte einen reproduzierten Deadlock. Eine andere Policy erforderte einen eigenen nichtblockierenden Recovery-Pfad.
4. Die Recovery-Uhr wird erst am tatsaechlichen HALL3-Clamp-Clear nach Getter/Diagnose gelesen. Ein frueher Snapshot verkuerzte die 1-s-Phase mit 3-mm-Cap. Das belegte Important-Review-Finding wurde behoben und nachgeprueft; Preis ist ein ausdruecklicher schmaler Zeit-Callback statt Reactor im Controller.
5. Macro-SAVE ist gezielt checked/STRICT und bricht vor M83 ab; Recovery/Macro-RESTORE bleibt best-effort mit Retry-Latch. Erfolgreicher expliziter Resave darf den Slot weiter ersetzen. Macros, die einen gescheiterten SAVE bislang still ignorierten, brechen nun ab, damit der originale E-Mode gesichert bleibt.
6. Anchor-Planung besitzt zwei Snapshots: Enable-/Current-End-Floors vor Toolhead-Lookup, Live-lme/lead_time/idle_mode danach. Die zusaetzlichen Datenobjekte erhalten die nachgewiesene Reentrancy-Reihenfolge; ein einzelner frueher oder spaeter Snapshot waere inkompatibel.
