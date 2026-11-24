# Timer too close nach Druckende: Diagnose

## Nachgewiesener MCU-Ausloeser

Quelle: `C:/Users/3D Partner/Downloads/klippy (4).log`.
Keine Session-Start-/Config-/Versionsbloecke in dieser Datei.

| Logzeile | Host-Zeit | Befehl |
|---|---:|---|
| 2421 | 432387.178920 | Digital-Out OID 2, clock=80915745, Pegel 0 |
| 2422-2423 | 432387.184026-432387.188990 | 25 Steps auf OID 0 |
| 2449 | 432387.424764 | Digital-Out OID 2, clock=91998587, Pegel 1 |
| 2450 | 432387.524248 | Digital-Out OID 2, erneut clock=80915745, Pegel 0 |
| 2551 | 432387.524799 | Shutdown bei clock=92059214: Timer too close |

Der letzte Schaltauftrag liegt rund 232,16 ms vor der Shutdown-Clock und
230,90 ms vor dem zuvor eingeplanten Schaltauftrag. Es handelt sich um einen
rueckwaerts datierten Auftrag; blosses Vergroessern eines Lead-Time-Wertes
wuerde dessen Herkunft nicht beheben.

Die lokale Config hat einen invertierten Enable-Pin. Unter dieser Zuordnung
lautet die Folge Enable, Disable, veraltetes Enable. Die OID-Zuordnung der
tatsaechlichen Session ist mangels Configblock nicht direkt nachweisbar.

Stats vor Shutdown: AUTO, HALL2 aktiv, Flow und Pending-Distanz 0;
sysload=0.45, bytes_invalid=0, keine neuen Retransmits. Keine positive Evidenz
fuer dauerhafte Host-Ueberlastung; kurze Scheduling-Verzoegerungen sind damit
nicht ausgeschlossen.

## Reproduktion mit echtem Klipper-Code

Verwendeter Klipper-Stand: `ce7002bedf37e938bb483572949f3703ac6476cb`.
Separater Checkout und Python-venv unter WSL, keine Druckerverbindung.

Verwendet wurden echte C-Trapq, kartesische Stepper-Kinematik,
`itersolve_generate_steps`, `itersolve_check_active`, sowie unveraenderte
Klipper-Klassen `MCU_stepper`, `EnableTracking`, `StepperEnablePin`.
Die lokalen Methoden `_schedule_stepper_disable` und `_move_in_flight`
wurden unveraendert per AST geladen. Hardware und Reactor-Zeit waren ersetzt.

1. Ein Move beginnt bei 10.000 s und endet bei 10.025 s (0.05 mm bei 2 mm/s).
2. Klippers echter Activity-Callback erzeugt Enable(10.000).
3. Echte C-Step-Generierung laeuft bis 10.005; Position danach 0.010019375 mm.
4. MCU-Zeit wird auf 10.100 gesetzt, waehrend der Generator noch bei 10.005
   steht. Der lokale Disable-Guard erlaubt die Abschaltung.
5. Der Disable-Endpunkt plant Disable(10.230) und registriert durch Klippers
   echtes `motor_disable` erneut den Activity-Callback.
6. Der naechste echte Activity-Check ermittelt im C-Code wieder 10.000.
   Ergebnis: Enable(10.000), Disable(10.230), Enable(10.000).
7. Gegenprobe: Nach C-Generierung bis 10.300 liefert `check_active` 0;
   der alte Move wird dann nicht mehr als aktiv erkannt.

Der alte Startzeitpunkt wurde in diesem Versuch NICHT als Callback-Ergebnis
vorgegeben, sondern von C-itersolve aus der realen Trapq ermittelt.
Die Verzogerung zwischen MCU und Generator wurde kontrolliert vorgegeben.
Dies ist eine Komponenten-Reproduktion, kein vollstaendiger Hardware- oder
Reactor-Replay des Logs.

Unabhaengige Gegenpruefung: Klipper fuehrt Activity-Callbacks und anschliessende
C-Generierung innerhalb desselben Flush-Aufrufs aus. Der Background-Scheduler
zielt regulaer auf Vorlauf vor der MCU. Der Versuch unterbricht diesen
atomaren Ablauf nicht, setzt aber einen Rueckstand beim NAECHSTEN Flush
voraus. Dessen Entstehung im Hardwarelauf ist nicht bewiesen. Normale
Background-Intervalle allein erklaeren diesen Rueckstand nicht.

## Betroffene lokale Stellen

- `buffer_feeder.py:2943`: Disable-Guard verwendet `_move_in_flight()`.
- `buffer_feeder.py:3416`: Dieser Guard vergleicht MCU-Zeit mit Move-Ende.
- `buffer_feeder.py:1496`: Deferred-Disable verwendet denselben Guard.
- `buffer_feeder.py:2519`: Der Flush-Pfad behandelt den Unterschied zwischen
  MCU-Zeit und Generator-Fortschritt bereits ausdruecklich.
- `buffer_stepper.py:71` und `buffer_feeder.py:3149`: `skip_enable` laesst
  explizite Plugin-Enable-Aufrufe aus, unterdrueckt aber nicht Klippers
  automatischen Activity-Callback.
- `tests/fakes_klipper.py:256`: Enable-Fake speichert nur Listen; bildet
  diese Callback- und C-Generator-Interaktion nicht ab.

## Grenze der Zuordnung

25 Steps entsprechen mit der Repository-Konfiguration rund 0.05010 mm.
Das passt zu einem Anchor-/Prime-Move, beweist aber seinen Aufrufer nicht.
Die lokale Config verwendet `idle_anchor_mode: silent`: Ein periodischer
Watchdog-Anchor ist damit ausgeschlossen. Ein Overflow-Prime oder eine
abweichende installierte Config/Version bleibt als Ursprung moeglich.
Ein globales M84/Idle-Timeout ist im vorhandenen Log nicht direkt belegt.

GitHub-Issue 73 beschreibt denselben Nutzerfall; der Remote-Branch
`refactor/cleanup-all-phases` zeigte beim Abgleich auf `93e8df5` wie der
lokale HEAD. Lokale uncommitted Aenderungen waren bereits vorhanden.

## Urspruenglicher Fix-Vorschlag

Den zentralen Disable-Pfad erst freigeben, wenn der reale Step-Generator
die relevanten Moves abgearbeitet hat; dabei eigene Trapq, SYNC und
globale Abschaltpfade beruecksichtigen. `skip_enable` nicht als Garantie
fuer stromlose Anchor-Steps behandeln. Regression mit echtem C-Generator
und Klipper-Enable-Callbacks, inklusive Gegenprobe und Idle-/SYNC-Safety.

Zum Abschluss der Diagnose: keine Plugin-Aenderung, kein Push, keine
Hardwareaktion. Die nachfolgende Umsetzung wurde vom Nutzer genehmigt.

## Bestehende Tests

In isolierter WSL-venv: `python -m pytest -q tests/test_idle_anchor_silent.py
tests/test_auto_idle_motor_disable.py tests/test_enable_does_not_flush_toolhead.py`:
24 passed, 0 failed. Dies bestaetigt die bisherige Python-Semantik, widerlegt
aber wegen der beschriebenen Fake-Grenzen den C-Callback-Race nicht.

## Umsetzung nach Freigabe

`_disable_stepper` wartet jetzt auf
`motion_queuing.last_step_gen_time >= _current_move['end_time']`.
Bis dahin bleibt `_pending_disable=True` und das Priming unveraendert.
Der bestehende Main-Tick versucht Disable erneut; ein neues Enable hebt
das vorgemerkte Disable weiterhin auf. Kein zusaetzlicher Toolhead-Flush.
Ohne eigenen Move wird nicht auf synthetische Startup-/Recovery-Anker
gewartet. HALL- und SYNC-Guards bleiben erhalten.

Regressionen zuerst gegen den alten Code: fuenf neue Python-Faelle schlugen
an der vorzeitigen Abschaltung fehl. Die separate C-Reproduktion erzeugte
Enable(10.0), Disable(10.15), Enable(10.0). Ein weiterer Red-Test deckte den
synthetischen Anchor ohne eigenen Move ab; auch dieser Fall ist korrigiert.

Endstand: 636 pytest-Tests bestanden, 15 bestehende Skips. Zwei C-Faelle
bestanden (vollstaendige und kontrolliert verzoegerte Generierung). Im
verzoegerten Fall: Enable(10.0), anschliessend erst Disable(10.35).
Die Generierung erfolgt nun ueber Klippers regulaeres
`steppersyncmgr_gen_steps`, mit File-Output nach `/dev/null`.

Reproduzierbarer Aufruf unter Linux mit installiertem cffi und C-Compiler:

```sh
python tools/verify_disable_stepgen.py --klipper-path /path/to/klipper
```

Der optionale Parameter `--feeder-path` erlaubt den Vorher/Nachher-Vergleich
gegen eine andere Plugin-Datei. Der Test laedt die vier benoetigten
Plugin-Methoden unveraendert per AST, echten C-Code und echte
Klipper-Enable-Callbacks. Er bildet keinen vollstaendigen Drucker nach.

Unabhaengiger Code-Review: completed-cursor-Verwendung, Pending-Retry und
No-Move-Fall geprueft. Globale M84-Aufrufe durchlaufen weiterhin Klippers
eigenen Flush-vor-Disable-Pfad. Kein Commit, Push oder Deployment.
Die Herkunft des Mini-Moves und des Generatorrueckstands im Hardwarelog
bleibt offen; behoben ist der reproduzierte lokale Disable-Race.
