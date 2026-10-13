# Timer too close: globaler Stepper-Disable

## Hardwarebefund

Quelle: `C:/Users/3D Partner/Downloads/klippy (5).log`.
Installierter Plugin-Commit: `e1dc62796774170f50bd4a204146cc5b0d00edd5`.
Installierter Klipper-Commit: `0499b30374315f2a9f49fc12808527fc7d0f5cfa`.
Der bisherige Plugin-Disable-Guard ist auf dem Drucker vorhanden.

Logzeilen 4328, 4356 und 4357 zeigen Digital-Out OID 2 mit
Clock 3973690347, danach 3984711036, danach erneut 3973690347.
Der letzte Auftrag liegt rund 231.7 ms vor der Shutdown-Clock 3984810249
(Zeile 4458). Unter der invertierten Enable-Pin-Konfiguration entspricht
das Enable, Disable und veraltetem Re-Enable.

Ein konkreter M84-Aufruf ist im Log nicht dokumentiert. Zeile 2006 setzt
den Idle-Timeout auf 600 s; der Abstand von etwa 900 s zum letzten
Heater-OFF beweist keinen Idle-Timeout. Der Fix behandelt den nachfolgend
reproduzierten globalen Disable-Race; ein Hardware-Replay steht aus.

## Reproduzierte Luecke

Klippers `flush_all_steps()` liest seinen Flush-Horizont vor den Callbacks.
Ein ausstehender Overflow-Prime kann im Callback hinter diesen Horizont
angehaengt werden. Der globale M18/M84-Pfad deaktiviert anschliessend den
Stepper direkt und umgeht den bisherigen Plugin-Guard. Der naechste echte
C-Activity-Check liefert den alten Move-Start zurueck.

`tools/verify_global_disable_stepgen.py` reproduziert das mit unveraenderten
Klipper-Methoden und echtem C-Generator. Gegen e1dc627 schlagen die drei
Faelle Direct, Global und Multi mit einem veralteten Re-Enable fehl.
Zeit, Sensoren und Demand sind kontrollierte Fixtures; UART und Serial-
Transport-Timing werden nicht simuliert.

## Freigegebener Fix

Der jeweilige Enable-Handle drainnt unvollstaendig generierte eigene Moves
vor Disable und aktualisiert den Timestamp danach. Reentrant Submits,
Pending-Streaming und Enables bleiben waehrend des Drains gesperrt.

Ein gemeinsamer Wrapper um `set_motors_enable` sperrt alle ausgewaehlten
Buffer schon vor dem globalen Flush. Danach werden deren unsent Streams
und Overflow-Primes verworfen. Andere Stepper und SYNC-Bindungen bleiben
erhalten. Verschachtelte Aufrufe stellen den vorherigen Sperrstatus wieder
her; Exceptions raeumen die temporaren Guards auf.

Verifikation unter Linux mit cffi und C-Compiler:

```sh
python -m pytest -q tests/
python tools/verify_disable_stepgen.py --klipper-path /path/to/klipper
python tools/verify_global_disable_stepgen.py --klipper-path /path/to/klipper
```

Die drei neuen C-Faelle zeigen mit dem Fix nur Enable und Disable, kein
veraltetes Re-Enable. Bereits abgeschlossene lokale Moves benoetigen
keinen zusaetzlichen Flush.
