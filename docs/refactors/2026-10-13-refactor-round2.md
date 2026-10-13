# Refactor-Runde 2: verifizierte lokale Umsetzung

Beginn: 2026-10-13. Abschluss: 2026-10-14. Repository:
`Avatarsia/BufferPLUS-klipper`, lokaler Branch `refactor/cleanup-all-phases`.

Alle fuenf Punkte wurden durch jeweils einen eigenen externen Codex-CLI-Agenten
implementiert und separat unabhaengig geprueft. Die gemeinsame Feeder-Datei
wurde sequenziell bearbeitet; abschliessend folgte ein Review mit fuenf Scopes.

| Issue | Neuer Coordinator / Workflow | Extrahierte Methoden | Task-Review |
| --- | --- | ---: | --- |
| [#83](https://github.com/Avatarsia/BufferPLUS-klipper/issues/83) | `BufferDiagnostics` | 4 | CLEAN |
| [#84](https://github.com/Avatarsia/BufferPLUS-klipper/issues/84) | `PrintSessionCoordinator` | 13 | CLEAN |
| [#85](https://github.com/Avatarsia/BufferPLUS-klipper/issues/85) | `LegacyFilamentWorkflow` | 8 | CLEAN |
| [#86](https://github.com/Avatarsia/BufferPLUS-klipper/issues/86) | `IdleWatchdogCoordinator` | 4 | CLEAN |
| [#87](https://github.com/Avatarsia/BufferPLUS-klipper/issues/87) | `StreamingCoordinator` | 5 | CLEAN |

Die 34 bestehenden Feeder-Methoden bleiben als kompatible Wrapper erhalten.
Live-State und interne Callback-Hooks bleiben auf ihrem jeweiligen Owner.
Legacy LOAD/UNLOAD hat drei separate frozen Options/Parser-Paare, aufgerufen
an den bisherigen Stellen. Reactor- und MCU-Flush-Einstiege bleiben getrennt.
Es wurde keine neue Motion-, Safety-, Timing- oder Retry-Policy eingefuehrt.
`buffer_feeder.py` umfasst jetzt 2961 statt 4653 Zeilen.

## Nachweise

- Frische Verifikation nach Integration im Originalprojekt: Windows und Linux
  jeweils **947 passed, 8 bestehende skipped, 0 failed**.
- Echter Klipper-C-Test: **5 PASS** (local completed/delayed sowie global
  direct/global/multi). Drei `--unguarded`-Negativkontrollen liefern jeweils
  den spezifischen Fehler `AssertionError: Stale auto-enable fired`.
- Klipper-Pin: `ce7002bedf37e938bb483572949f3703ac6476cb`; tracked Source clean.
- Unabhaengiger Gesamtvergleich gegen die echte Baseline dieser Runde:
  alle 34 Methodenkoerper nach Owner-Rueckbindung und Parser-Expansion
  AST-identisch; gesamter uebriger Feeder-AST identisch.
- 84 bestehende Testdateien bytegleich. Zwei Source-Inspection-Tests lesen
  jetzt das Watchdog-Modul; Assertions, Signaturen und Skip-Dekoratoren
  unveraendert. Die Owner-Normalisierung erhaelt den Strict-Operator-Check.
- Bestehende CRLF/LF- und UTF-8-Eigenschaften erhalten; neue Dateien UTF-8/LF.
  Python-3.7-Syntaxcheck und Whitespace-Pruefung bestanden.
- Alle fuenf Task-Reviews und alle fuenf Final-Review-Scopes **CLEAN**:
  Architecture (eigener externer read-only Codex-CLI-Agent), Motion/Safety,
  Workflows/API, Tests/Regression und Platform/Installation.
- Originalprojekt vor Integration gegen alle 135 Start-Hashes geprueft;
  keine konkurrierenden Aenderungen ueberschrieben. Alle integrierten Bytes
  anschliessend gegen den geprueften Worktree bestaetigt. Erste Runde
  (#76-#82) und vorhandene User-Aenderungen bleiben enthalten.

## Reproduktion und Grenzen

`python -m pytest tests/ -q -p no:cacheprovider`

Unter Linux mit dem genannten Klipper-Pin:
`python tools/run_timing_regressions.py --klipper-path <klipper-checkout> --check-unguarded`

Die acht historischen Skips bestehen fort. Python-Consumer-Traces verwenden
Fakes; die C-Harnesses pruefen echte Klipper-Enable/Stepgen-Pfade. Kein neuer
Hardware-Nachweis. Die Umsetzung ist lokal und uncommitted: kein Push, PR,
Merge oder Deploy. Issues #83-#87 erhalten die Nachweise und bleiben bis zur
Veroeffentlichung offen. Worktree, Snapshots und Review-/Testprotokolle bleiben
zur Wiederherstellung in `.lll-refactor2-8d0139f8/worktree/.superpowers/sdd/refactor-round2`
im Projekt-Elternordner erhalten.
