# Refactor aus Projektanalyse

Auftrag: alle fuenf priorisierten Analysepunkte sowie Overflow/Cleanup und Timing/Disable als Issues mit separaten Subagenten umsetzen.

## Erfolgskriterien

Alle sieben Issues implementiert und unabhaengig reviewed; Pytest gruen; echte C-Regressionen fuer Timing/Disable gruen. Bestehende User-Aenderungen bleiben enthalten. Keine eigenmaechtigen Commits, Pushes oder Merges.

## Architektur

Inkrementelle Extraktion statt Komplettumbau. BufferFeeder bleibt Klipper/Mux-Adapter; Regelung, Pending-Lifecycle, Fault/Cleanup und Timing/Motor-Koordination erhalten kleine explizite Verantwortungsbereiche. Initializer werden als Snapshots kenntlich gemacht.

## Safety-Invarianten

HALL1/HALL2-Guards vor Demand-Bypasses; HALL3-Sicherheit; Bestehenden Watchdog-Print-Block einschliesslich enger P7-78-Stale-Override-Regeln erhalten; Far-Future-Anker skippen; actual Stepgen-Fortschritt fuer Disable; Nested Guards restaurieren; Boot-Anchor erhalten; kein eigener Trapq-Submit waehrend SYNC; Mux-Selector bei nested Calls; guaranteed Cleanup in Python.

## Task 1: Test-Fixtures und GCode-Fakes konsolidieren

https://github.com/Avatarsia/BufferPLUS-klipper/issues/76

Gemeinsamer Feeder-Builder und GCmd-Stub; kompatible lokale Varianten migrieren, spezielle Setups ueber explizite Parameter/gezielte Nachkonfiguration erhalten. Connect immer nach Konstruktion.

- Fixture-Regression reproduziert den Fehler vor dem Fix und wird danach gruen.
- Enable-/Disable-no-flush-Tests durchlaufen den echten Plugin-Scheduler und weisen Motor-Aufrufe nach.
- Keine bool("0")-Semantik; Config-Bounds und bestehende Spezial-Setups bleiben erhalten.

## Task 2: Config-, Runtime-Tuning- und Status-Mappings zentralisieren

https://github.com/Avatarsia/BufferPLUS-klipper/issues/77

Kleine deklarative Tuning-Tabelle fuer Parser, GCode-Key, Attribut, Bounds, Einheit und Status-Key; abhaengige Caps und Tracker-Nebenwirkungen als ausdrueckliche Hooks erhalten. Gemeinsame Regeln nur fuer tatsaechlich gemeinsame Parameter verwenden.

- MAX_MOVE_CHUNK_MM wird vor INTERRUPT_CHUNK_MM angewendet; gemeinsames Anheben und Absenken bleiben korrekt.
- Filamentdurchmesser aktualisiert den Velocity-Tracker.
- Oeffentliche GCode-/Status-Namen, Legacy-Aliasse und Config-Fallbacks bleiben kompatibel.

## Task 3: Pending-Stream-Lifecycle und Submit-Ergebnis kapseln

https://github.com/Avatarsia/BufferPLUS-klipper/issues/78

Kleine Lifecycle-Operationen fuer Start/Fortschritt/Finish und getrenntes Verwerfen von Overflow-Resume. Submit meldet ausdruecklich queued/skipped; Distanzbuchhaltung basiert auf tatsaechlich gequeueter Bewegung.

- Skip/Reject verbraucht keine noch zu foerdernde Distanz; Regression zu #63.
- Park-Demand-Bypass erhaelt vorgeschaltete HALL1/HALL2-Guards.
- Endstop/HALT/JAM/Exhaustion raeumen ihre bisherigen Feldmengen; in-flight Moves und Overflow-Resume-Snapshot bleiben korrekt.

## Task 4: FeedSpeedController extrahieren und Main-Tick gliedern

https://github.com/Avatarsia/BufferPLUS-klipper/issues/79

Eigene Regelungskomponente mit explizitem Demand-/Sensor-/Zeit-Snapshot und eigener Carry/Hysterese/Recovery-Verantwortung. Feeder bleibt Adapter. Main-Tick mechanisch in benannte Verantwortungsbloecke aufteilen, Ausfuehrungsreihenfolge beibehalten.

- HALL1/HALL2 stoppen weiterhin vor Demand-Bypasses.
- HALL3-Dwell, High-Flow-Carry, Zwischenzone und Session-Reset liefern dieselben Ergebnisse.
- Bestehender Watchdog-Print-Block einschliesslich enger P7-78-Stale-Override-Regeln bleibt unveraendert; kein zweiter Submit aus Main-Tick bei Flush-Ownership.

## Task 5: Runtime-State-Ownership als Initializer ausdruecklich machen

https://github.com/Avatarsia/BufferPLUS-klipper/issues/80

Live-Owner-Verantwortung dokumentieren und Initialwerte ausdruecklich initial_settings/initial_runtime_state benennen. Kompatible lesbare Legacy-Aliasse erhalten, soweit sinnvoll; keine globale Migration aller State-Zugriffe in diesem Issue.

- BUFFER_SET wirkt weiter auf Live-Status und Motion.
- Neue Namen vermitteln Snapshot-Semantik; Legacy-Zugriff bleibt kompatibel.
- Init-Werte fuer Safety, Sync und Recovery bleiben unveraendert.

## Task 6: Overflow- und Guaranteed-Cleanup-Verantwortung zusammenfuehren

https://github.com/Avatarsia/BufferPLUS-klipper/issues/81

Overflow-Entry/Exit/Resume im FaultManager organisieren; den blockierenden Sensor-LOAD als ausdruecklichen Resume-Owner erhalten. Gemeinsamer Python-Operation-Scope fuer SAVE/RESTORE und separater SYNC-Scope mit ausdruecklicher strict/best-effort-Policy.

- Aktuelle lokale HALL1-Pause/Resume-Aenderungen und 30-s-Timeout bleiben erhalten.
- JAM-Latch, Already-present-Skip und SYNC-Gate bleiben erhalten.
- Fehler bei SAVE/M83/SYNC/G1/UNSYNC/RESTORE verlieren weder den Originalfehler noch den benoetigten Cleanup.
- Alle nested Commands behalten BUFFER-Selector; Cleanup bleibt Python-try/finally.

## Task 7: Zeitanker- und Motor-Disable-Koordination isolieren

https://github.com/Avatarsia/BufferPLUS-klipper/issues/82

Bestehenden AnchorPlan fuer reine Entscheidungen aus expliziten Snapshots verwenden; Klipper-Zugriff/Plan-Anwendung im Adapter. Printerweiter DisableCoordinator und instanzbezogener Motor-Lifecycle kapseln die bestehenden Guards/Drain-Regeln. Unit- und echte C-Harnesses als dokumentierten gemeinsamen Check buendeln.

- forced_t0=None und Far-Future-Toolhead-Zeit fuehren weiterhin zu SKIP statt Clamp.
- MCU-Zeit und tatsaechlicher Stepgen-Fortschritt bleiben getrennt.
- Globales/gezieltes/nested Disable drainnt eigene Moves und restauriert vorherige Guards.
- Enable erzeugt keinen Toolhead-Flush; Boot-Anchor und SYNC-Queue-Ownership bleiben erhalten.
- Beide C-Harnesses laufen auf Linux gegen einen protokollierten Klipper-Commit; Kontrolllauf --unguarded muss erwartungsgemaess scheitern.


## Recovery-Zeitvertrag

Die Post-Full-Recovery beginnt wie in der Ausgangsbasis beim tatsaechlichen HALL3-Clamp-Clear nach vorherigen Getter-/Diagnose-Aufrufen. Ein frueher Compute-Snapshot darf die 1-s-Phase mit 3-mm-Chunk-Cap nicht verkuerzen. Der Controller bekommt dafuer eine explizite schmale Adapter-Zeitgrenze, keine Reactor-Referenz.
