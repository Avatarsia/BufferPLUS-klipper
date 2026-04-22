# Mellow LLL Plus Filament Buffer - Klipper-Konfiguration

Aktive Klipper-Konfiguration fuer den **Mellow LLL Plus Filament Buffer**. Der Feeder-Stepper laeuft permanent synchron mit dem Hauptextruder; die drei Hall-Sensoren regeln die effektive `rotation_distance` und halten den Buffer selbstaendig im mittleren Fuellbereich - ohne Druck-Pausen durch Feed-Bursts.

Diese Konfiguration ist der Rebuild-Branch `rebuild-sync-v2` und enthaelt gegenueber dem Upstream: sensor-gesteuertes LOAD/UNLOAD, Hysterese-Latch, HALL1-Safety auf allen Feed-Pfaden, Kalibriermakros (`CALIBRATE_FEEDER_SYNC`, `MEASURE_LOAD_START`) und einen Runout-Nachlauf fuer externe Filament-Sensoren.

> **Hinweis:** Das hier ist die Klipper-Konfiguration. Den Quellcode der Buffer-Plus-Firmware findest du im
> [Haupt-Repository](../README.md).

---

## Inhalt

- [Funktionsprinzip](#funktionsprinzip)
- [Was diese Config leistet](#was-diese-config-leistet)
- [Hardware](#hardware)
- [Voraussetzungen](#voraussetzungen)
- [Inbetriebnahme](#inbetriebnahme)
- [Variablen-Referenz](#variablen-referenz)
- [Taster-Bedienung](#taster-bedienung)
- [Benutzer-Macros](#benutzer-macros)
- [Runout-Verhalten](#runout-verhalten)
- [Tipps und Hinweise](#tipps-und-hinweise)
- [Interne Macros (Diagnose)](#interne-macros-diagnose)
- [Firmware flashen (einmalig)](#firmware-flashen-einmalig)
- [Fehlerbehebung](#fehlerbehebung)
- [Revisionen](#revisionen)
- [Danksagungen](#danksagungen)
- [Lizenz](#lizenz)

---

## Funktionsprinzip

Der Mellow LLL Plus ist ein aktiver Filament-Buffer zwischen Spool und Drucker. Er entkoppelt die Filamentrolle vom
Druckkopf und verhindert Zugspannungen im Filament-Pfad. Der eingebaute Feeder-Stepper laeuft permanent synchron
mit dem Hauptextruder (Variante 2 - Happy-Hare-Style). Drei Hall-Sensoren im Buffer messen den Fuellstand und
passen die Foerdergeschwindigkeit des Feeders dynamisch an:

| Sensor | Position                     | Zustand               | Reaktion des Feeders                                                              |
|--------|------------------------------|-----------------------|-----------------------------------------------------------------------------------|
| HALL3  | Untere Schwelle (nahe leer)  | Buffer fast leer      | `rotation_distance` verkleinern - Feeder foerdert etwas mehr als der Extruder    |
| HALL2  | Obere Schwelle (nahe voll)   | Buffer fast voll      | `rotation_distance` vergroessern - Feeder foerdert etwas weniger als der Extruder |
| HALL1  | Notanschlag (Ueberlauf)      | Buffer uebergelaufen  | Feeder komplett entkoppeln und deaktivieren                                       |

Die Modulation betraegt standardmaessig +-20 % der kalibrierten `rotation_distance`. Das haelt den Buffer dauerhaft
im mittleren Fuellbereich, ohne dass separate Feed-Loops benoetigt werden.

---

## Was diese Config leistet

- **Permanenter Feeder-Sync** - Feeder-Stepper laeuft dauerhaft synchron mit dem Hauptextruder via `SYNC_EXTRUDER_MOTION`. Keine Druck-Pausen durch Feed-Bursts.
- **Hall-Sensor-Modulation** - HALL2 (voll) und HALL3 (leer) modulieren die effektive `rotation_distance` um +-`sync_modulation` (Standard +-20 %). HALL1 (Ueberlast) trennt den Sync sofort.
- **Hysterese-Latch** - Letzter Hall-Zustand wird gehalten, bis der gegenteilige Hall triggert. Glatte Regelkurve ohne Sprung-Artefakte.
- **3-Klick-Taster** - Feed/Retract mit Dauerlauf (1 Klick), Puls (2 Klicks), Triple-Burst (3 Klicks, Retract standardmaessig; Feed optional via `variable_feed_burst_enabled`).
- **Sensor-gesteuertes LOAD_FILAMENT** - 3 Phasen: schnell zum Toolhead, synchron durchs Hotend (50-mm-Chunks), Buffer-Fuellung bis HALL2 triggert.
- **Chunked UNLOAD_FILAMENT** - Tip-Forming + synchroner Rueckzug in 50-mm-Chunks + vollstaendiger Feeder-Rueckzug inkl. Follow-Strecke.
- **Runout-Handling** - Externer Sensor via `runout_pause=0` (Feeder laeuft noch 100 mm leer, dann disable) oder Sofort-Pause via `runout_pause=1`.
- **Kalibriermakros** - `CALIBRATE_FEEDER_SYNC` fuer `sync_rotation_distance`, `MEASURE_LOAD_START` fuer `load_fast_distance` per Tastermessung.
- **Display-Toggle** - M117-Statusausgaben via `variable_display_status_enabled` umschaltbar.

---

## Hardware

### Sensor-Konfiguration

- **ENDSTOP3 (PB7)**: Filament-Eingangssensor - erkennt Filament am Buffer-Eingang (triggert Erstbefuellung via `_PREPARE_INITIAL_FILL`).
- **HALL3 (PB4)**: Untere Schwelle (Buffer nahe leer) - Feeder-Modulation erhoeht Foerdermenge (`rotation_distance` verkleinert).
- **HALL2 (PB3)**: Obere Schwelle (Buffer nahe voll) - Feeder-Modulation reduziert Foerdermenge (`rotation_distance` vergroessert). Dient zusaetzlich als LOAD-Abbruchsensor.
- **HALL1 (PB2)**: Ueberlast-Notanschlag - trennt Sync sofort und setzt `overfill_lock=1`.

### Taster-Konfiguration

- **Vorschub-Taster (PB12)**: 1 Klick = Dauerlauf-Vorschub, 2 Klicks = `manual_chunk_distance`-Puls, 3 Klicks = neuer Dauerlauf (oder Triple-Burst falls `variable_feed_burst_enabled=1`).
- **Rueckzug-Taster (PB13)**: 1 Klick = Dauerlauf-Rueckzug, 2 Klicks = `manual_chunk_distance`-Puls, 3 Klicks = Triple-Retract-Burst.

---

## Voraussetzungen

Folgende Eintraege muessen in der `printer.cfg` des Druckers vorhanden sein, bevor die Config eingebunden wird:

```cfg
[force_move]
enable_force_move: True

[pause_resume]

[extruder]
max_extrude_only_distance: 100  # mindestens 100, intern werden 50mm-Chunks verwendet
```

Die Config-Datei wird in der `printer.cfg` per `[include lll.cfg]` eingebunden. Bei Verwendung von Mainsail oder
Fluidd: Die Datei muss im Konfigurationsverzeichnis (`~/printer_data/config/`) liegen.

> **Hinweis Klipper-Variante:** Der Befehl `SYNC_EXTRUDER_MOTION` hat je nach Klipper-Fork unterschiedliche
> Parameter:
> - Mainline-Klipper: `SYNC_EXTRUDER_MOTION EXTRUDER=mellow MOTION_QUEUE=...`
> - Kalico-Fork: `SYNC_EXTRUDER_MOTION STEPPER=mellow MOTION_QUEUE=...`
>
> Die Config verwendet die Mainline-Syntax. Bei Kalico den Parameter anpassen.

---

## Inbetriebnahme

Die folgenden Schritte muessen einmalig bei der Ersteinrichtung durchgefuehrt werden. Danach genuegt es, die Config
einzubinden und den Drucker zu starten.

### 1. MCU Serial-ID eintragen

Die MCU-Serial-ID identifiziert den LLL-Plus-Controller eindeutig. Sie wird einmalig ausgelesen und in der Config
eingetragen.

1. LLL Plus per USB an den Raspberry Pi / Host-PC anschliessen.
2. In der Konsole ausfuehren: `ls /dev/serial/by-id/` - die ID des LLL-Controllers notieren (beginnt mit
   `usb-Klipper_stm32...`).
3. In der Config den Abschnitt `[mcu LLL_PLUS]` suchen und den `serial:`-Wert mit der eigenen ID ersetzen.

### 2. Pflicht-Variablen anpassen

Folgende Variablen in `_FILAMENT_VARS` sind mit `!!` markiert und muessen vor dem ersten Betrieb auf das eigene
Setup angepasst werden:

| Variable                 | Bedeutung                                                                           | Kalibrierung  |
|--------------------------|-------------------------------------------------------------------------------------|---------------|
| `sync_rotation_distance` | Kalibrierte `rotation_distance` fuer 1:1-Mitlauf des Feeders mit dem Extruder.      | Schritt 3     |
| `load_fast_distance`     | Foerderweg vom Ende der Follow-Phase bis zum Toolhead-Eingang [mm].                 | Schritt 4     |
| `load_slow`              | Synchroner Foerderweg durch Heatbreak und Nozzle beim Laden [mm].                   | Schritt 5     |
| `unload_sync`            | Synchroner Rueckzugsweg durch Heatbreak und Nozzle beim Entladen [mm].              | Schritt 5     |

Ausserdem: Den kalibrierten Wert von `sync_rotation_distance` auch in `[extruder_stepper mellow]` unter
`rotation_distance:` eintragen - das ist der Hardware-Startwert beim Klipper-Boot.

### 3. Feeder-Sync kalibrieren (CALIBRATE_FEEDER_SYNC)

Ziel: Der Feeder soll exakt gleich viel foerdern wie der Hauptextruder (1:1). Dieser Wert ist die Basis fuer die
+-20%-Modulation.

1. Filament einlegen, Hotend auf Drucktemperatur heizen.
2. In der Konsole: `CALIBRATE_FEEDER_SYNC` aufrufen. Der Feeder laeuft jetzt exakt 1:1 ohne Modulation.
3. Markierung am Filament direkt vor dem Feeder-Eingang anbringen.
4. In der Konsole: `G1 E100 F60` ausfuehren.
5. Nachmessen wie viel mm Filament am Feeder-Eingang durchgezogen wurde.
6. Neue `rotation_distance` berechnen: `neue_rd = alte_rd * (gemessene_mm / 100)`
7. Wert in `variable_sync_rotation_distance` UND in `[extruder_stepper mellow]` `rotation_distance` eintragen.
8. Klipper neu starten, Schritte 3-6 wiederholen bis Abweichung < 1 mm.

### 4. load_fast_distance kalibrieren (MEASURE_LOAD_START)

> **ACHTUNG:** Hotend KALT lassen! Bei warmem Hotend startet nach der Follow-Phase automatisch `LOAD_FILAMENT`
> (via `_initial_follow_end`) - das unterbricht die Kalibrierung bevor `load_fast_distance` eingetragen wurde.
> Hotend vollstaendig auf Raumtemperatur abkuehlen lassen vor Beginn.

Die Messung startet ab der Position, an der das Filament nach der Grip- und Follow-Phase steht. Der gemessene
Wert wird direkt eingetragen - kein Abzug notwendig.

1. Filament komplett aus dem System entfernen.
2. Sicherstellen: Hotend ist KALT (Raumtemperatur).
3. Frisches Filament in den Feeder-Eingang stecken. Grip- und Follow-Phase laufen automatisch durch (ca. 40
   Sekunden). Alternativ: `FORCE_BUFFER_FILL` aufrufen.
4. In der Konsole: `MEASURE_LOAD_START` aufrufen.
5. Vorschub-Taster am LLL 1x druecken - Foerderung startet (Toggle-Modus).
6. Warten bis die Filament-Spitze am Toolhead-Eingang erscheint.
7. Vorschub-Taster erneut druecken - Foerderung stoppt, Ergebnis wird in der Konsole ausgegeben.
8. Gemessenen Wert in `variable_load_fast_distance` eintragen. Tipp: 10-20 mm weniger als gemessen eintragen,
   damit das Filament nicht zu weit in den Extruder ragt vor Phase 2.

### 5. load_slow und unload_sync kalibrieren

**load_slow:** Weg vom Toolhead-Eingang (Ende Phase 1) bis zur Nozzle-Spitze. Entspricht dem Toolhead-Innenweg
inkl. Heatbreak und Nozzle-Laenge. Mit Schieblehre oder Markierung ausmessen. Typisch: 120-180 mm je nach
Toolhead.

**unload_sync:** Rueckzugsweg beim Entladen durch Heatbreak und Nozzle. Gleicher oder 5-10 mm groesserer Wert
als `load_slow`. Muss das Filament vollstaendig aus dem Hotend herausziehen.

### 6. Erstbefuellung und LOAD_FILAMENT testen

1. Hotend auf Betriebstemperatur vorheizen.
2. Filament in den Feeder-Eingang stecken.
3. Grip-Phase startet automatisch: ca. 550 mm @ 55 mm/s (10 Sekunden).
4. Follow-Phase startet automatisch: ca. 450 mm @ 15 mm/s (30 Sekunden).
5. Nach der Follow-Phase: Sync wird aktiviert. Wenn das Hotend warm genug ist (>= `min_temp`), startet
   `LOAD_FILAMENT` automatisch.
6. `LOAD_FILAMENT` Phase 1: Feeder foerdert `load_fast_distance` mm schnell zum Toolhead.
7. `LOAD_FILAMENT` Phase 2: Feeder + Extruder synchron langsam durch das Hotend.
8. `LOAD_FILAMENT` Phase 3: Feeder fuellt Buffer bis HALL2. Fertig.
9. Bei Problemen oder zum manuellen Neustart: `FORCE_BUFFER_FILL` in der Konsole aufrufen.

---

## Variablen-Referenz

Alle Variablen befinden sich im Macro `_FILAMENT_VARS` in der Config. Mit `!!` markierte Variablen sind Pflichtfelder
und muessen kalibriert werden. Alle anderen sind gut gewaehlte Richtwerte die in den meisten Setups funktionieren.

### Sync und Allgemein

| Variable                     | Standard | Einheit | Beschreibung |
|------------------------------|----------|---------|--------------|
| `sync_rotation_distance` !!  | 18.86    | mm      | Kalibrierte 1:1-`rotation_distance` des Feeders. Basis fuer alle Modulations-Berechnungen. Mit `CALIBRATE_FEEDER_SYNC` kalibrieren. Gleichen Wert auch in `[extruder_stepper mellow]` eintragen. |
| `sync_modulation`            | 0.20     | -       | Modulationsbreite als Anteil 0..1 (0.20 = +-20 %). Groesserer Wert = staerkere Reaktion auf Hall-Sensoren, aber mehr Schlupf im Normalbetrieb. |
| `fast_speed`                 | 50       | mm/s    | Schnelle Feeder-Geschwindigkeit fuer FORCE_MOVE-Operationen. Gilt fuer: Taster-Dauerlauf, LOAD Phase 1+3, UNLOAD Phase 3, Triple-Burst, Initial-Grip. |
| `slow_speed`                 | 5        | mm/s    | Langsame synchrone Foerdergeschwindigkeit fuer LOAD Phase 2 und UNLOAD Phase 2 (Feeder + Extruder gemeinsam durch das Hotend). |
| `min_temp`                   | 180      | degC    | Mindest-Hotend-Temperatur vor LOAD und UNLOAD. Wird die Temperatur unterschritten, bricht das Macro mit einer Fehlermeldung ab. |
| `manual_chunk_distance`      | 10       | mm      | Schrittweite pro Loop-Tick fuer Taster-Betrieb und LOAD Phase 1+3. Bestimmt zusammen mit `manual_loop_tick` die effektive Dauerlauf-Geschwindigkeit. |
| `manual_speed`               | 15       | mm/s    | Geschwindigkeit fuer alle manuellen Taster-Operationen. Gilt einheitlich fuer Vorschub- und Rueckzug-Taster. |
| `force_move_accel`           | 1000     | mm/s^2  | Beschleunigung fuer alle FORCE_MOVE-Operationen. Gilt fuer Taster, LOAD Phase 1+3, UNLOAD Phase 3, Triple-Burst und Initial-Grip. |
| `manual_loop_tick`           | 0.1      | s       | Takt des Taster-Dauerlauf-Loops. Effektive Dauergeschwindigkeit = `manual_chunk_distance / manual_loop_tick`. Beispiel: 10 mm / 0.1 s = 100 mm/s. |
| `reenable_cooldown`          | 1        | s       | Verzoegerung nach Taster-Loslassen bis die Sync-Automatik wieder aktiv wird. Verhindert sofortiges Wiedereinschalten nach kurzem Taster-Kontakt. |
| `reenable_cooldown_fast`     | 0.5      | s       | Verzoegerung nach einem Triple-Click-Burst bis die Sync-Automatik wieder aktiv wird. Kuerzer als `reenable_cooldown`, da der Burst selbst eine definierte Distanz zuruecklegt. |

### LOAD-Parameter

| Variable                | Standard | Einheit | Beschreibung |
|-------------------------|----------|---------|--------------|
| `load_fast_distance` !! | 550      | mm      | Foerderweg Phase 1: Feeder allein, schnell, vom Ende der Follow-Phase bis zum Toolhead-Eingang. Standard 550 (Richtwert vor Kalibrierung). **Aktueller Wert in der mitgelieferten Config: 1000** (bereits auf das User-Setup kalibriert). Mit `MEASURE_LOAD_START` selber kalibrieren (Hotend KALT). Gemessenen Wert direkt eintragen. |
| `load_slow` !!          | 180      | mm      | Synchroner Foerderweg Phase 2: Feeder + Extruder gemeinsam durch Heatbreak und Nozzle. Entspricht dem Toolhead-Innenweg. Typisch 120-180 mm je nach Toolhead. |
| `load_buffer_max`       | 2000     | mm      | Sicherheits-Timeout fuer LOAD Phase 3 (Buffer befuellen bis HALL2). Wird HALL2 nicht innerhalb dieser Distanz erreicht, laeuft das Macro trotzdem mit einer Warnmeldung durch. |

### UNLOAD-Parameter

| Variable            | Standard | Einheit | Beschreibung |
|---------------------|----------|---------|--------------|
| `unload_sync` !!    | 180      | mm      | Synchroner Rueckzugsweg Phase 2: Feeder + Extruder ziehen gemeinsam durch Heatbreak und Nozzle. Muss >= `load_slow` sein. Gleicher oder 5-10 mm groesserer Wert als `load_slow` empfohlen. |
| `unload_fast_max`   | 2510     | mm      | Maximale Feeder-Rueckzugsdistanz Phase 3 (Polling bis `buffer_entrance` frei). Sicherheits-Timeout bei Sensorausfall. |

### Tip-Forming

Das Tip-Forming formt die Filamentspitze vor dem Entladen. Es verhindert Fadenziehen und Verstopfungen beim
naechsten Ladevorgang. Die Parameter analog zum Prusa MMU-System. Werte je nach Filamenttyp anpassen (TPU
benoetigt z.B. andere Werte als PLA).

| Variable            | Standard | Einheit | Beschreibung |
|---------------------|----------|---------|--------------|
| `tip_cycles`        | 4        | -       | Anzahl Push/Pull-Zyklen zum Formen der Filamentspitze. |
| `tip_push`          | 8        | mm      | Vorschublaenge pro Tip-Forming-Zyklus. |
| `tip_pull`          | 10       | mm      | Rueckzuglaenge pro Tip-Forming-Zyklus. Etwas laenger als `tip_push` erzeugt Netto-Rueckzug. |
| `tip_speed`         | 20       | mm/s    | Geschwindigkeit waehrend der Push/Pull-Zyklen. |
| `tip_final_retract` | 25       | mm      | Finaler Retract nach den Zyklen - zieht die geformte Spitze aus der Schmelzzone. |
| `tip_final_speed`   | 50       | mm/s    | Geschwindigkeit des finalen Retracts. |

### Erstbefuellung (Grip + Follow)

Die Erstbefuellung laeuft in zwei Phasen ab. Die Grip-Phase ergreift das Filament mit hoher Geschwindigkeit. Die
anschliessende Follow-Phase foerdert langsamer und stellt sicher, dass das Filament den gesamten Weg bis kurz vor
den Toolhead-Eingang zuruecklegt.

| Variable                   | Standard | Einheit | Beschreibung |
|----------------------------|----------|---------|--------------|
| `initial_grip_speed`       | 55       | mm/s    | Geschwindigkeit der Grip-Phase. |
| `initial_grip_duration`    | 10       | s       | Dauer der Grip-Phase. Foerderstrecke = `initial_grip_speed * initial_grip_duration` (Standard: 550 mm). |
| `initial_follow_speed`     | 15       | mm/s    | Geschwindigkeit der Follow-Phase nach dem Grip. |
| `initial_follow_duration`  | 30       | s       | Dauer der Follow-Phase. Foerderstrecke = `initial_follow_speed * initial_follow_duration` (Standard: 450 mm). Bei Aenderung: `load_fast_distance` neu kalibrieren (`MEASURE_LOAD_START`, Hotend KALT). |
| `auto_load_after_follow`   | 0        | 0/1     | 0 = nach Follow-Phase nur Sync aktiv, User startet `LOAD_FILAMENT` manuell. 1 = bei Hotend >= `min_temp` automatisch `LOAD_FILAMENT`. |

### Triple-Click-Burst

Wichtig: Nur der Rueckzug-Taster hat standardmaessig einen Triple-Click. Der Vorschub-Taster ist bei
`feed_burst_enabled=0` (Default) so konfiguriert, dass der 3. Klick lediglich einen neuen Dauerlauf startet.

| Variable                 | Standard | Einheit | Beschreibung |
|--------------------------|----------|---------|--------------|
| `triple_click_distance`  | 1300     | mm      | Rueckzugsdistanz beim Triple-Click-Burst des Rueckzug-Tasters. Hauptsaechlich fuer schnellen Rueckzug wenn KEIN Filament im Toolhead ist. Mit eingelegtem Filament nur mit Bedacht verwenden - kein Temperatur-Check. |
| `triple_click_window`    | 1.5      | s       | Zeitfenster fuer Triple-Click-Erkennung am Rueckzug-Taster. Alle drei Klicks muessen innerhalb dieses Fensters erfolgen. |
| `feed_burst_enabled`     | 0        | 0/1     | 0 = Vorschub-Taster Klick 3 startet neuen Dauerlauf (sicher). 1 = Triple-Feed-Burst am Vorschub-Taster aktiv (Verstopfungsrisiko wenn Filament im Toolhead). |

### Runout und Anzeige

| Variable                  | Standard | Einheit | Beschreibung |
|---------------------------|----------|---------|--------------|
| `runout_pause`            | 0        | 0/1     | Verhalten bei Filament-Runout am `buffer_entrance`-Sensor: 0 = Externer Sensor vorhanden (z.B. BTT SFS). Feeder laeuft noch 100 mm weiter, dann Stepper deaktivieren. Der externe Sensor kuemmert sich um die Druckpause. 1 = Kein externer Sensor. Feeder sofort aus, Druck sofort pausieren. |
| `display_status_enabled`  | 1        | 0/1     | M117-Displayausgaben aktivieren (1) oder deaktivieren (0). Bei 0 erscheinen Statusmeldungen nur in der Konsole (M118), nicht auf dem Display. Nuetzlich wenn das Display von einer anderen Komponente verwendet wird. |

---

## Taster-Bedienung

Der LLL Plus verfuegt ueber zwei Taster: Vorschub-Taster (Feed) und Rueckzug-Taster (Retract). Beide reagieren auf
ein 3-Klick-System und sind im Normalbetrieb sowie waehrend LOAD/UNLOAD und der Erstbefuellung nutzbar.

| Klick-Anzahl            | Verhalten                                                                                                                                                                                                                                                                                                                                                                 | Stoppen durch                          |
|-------------------------|---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|----------------------------------------|
| 1 Klick (Dauerlauf)     | Feeder laeuft kontinuierlich in die gewaehlte Richtung, solange der Taster gedrueckt wird. Effektive Geschwindigkeit: `manual_chunk_distance / manual_loop_tick` (Standard: 10 mm / 0.1 s = 100 mm/s).                                                                                                                                                                     | Taster loslassen                       |
| 2 Klicks (Puls)         | Feeder foerdert exakt `manual_chunk_distance` mm (Standard: 10 mm) in einem einzigen Schritt und stoppt dann. Nuetzlich fuer feines Positionieren.                                                                                                                                                                                                                        | Automatisch nach `manual_chunk_distance` mm |
| 3 Klicks (Triple-Burst) | Nur Rueckzug-Taster: Feeder zieht `triple_click_distance` mm am Stueck zurueck bei `fast_speed`. Standard: 1300 mm @ 50 mm/s. Hauptsaechlich gedacht fuer schnellen Rueckzug wenn KEIN Filament im Toolhead ist (z.B. nach Filament-Wechsel). Mit eingelegtem Filament nur mit Bedacht verwenden - kein Temperatur-Check, kein Tip-Forming. Alle drei Klicks muessen innerhalb von `triple_click_window` Sekunden erfolgen. Vorschub-Taster: kein Triple-Click - 3. Klick startet Dauerlauf neu. | Automatisch nach `triple_click_distance` mm |

> **Cooldown:** Nach jeder manuellen Taster-Aktion wartet die Sync-Automatik `reenable_cooldown` Sekunden
> (Standard: 1 s) bevor sie wieder aktiv wird. Nach einem Triple-Burst gilt die kuerzere `reenable_cooldown_fast`
> (Standard: 0.5 s).

> **Vorschub-Taster im Mess-Modus:** Wenn `MEASURE_LOAD_START` aktiv ist, arbeitet der Vorschub-Taster im
> Toggle-Modus: 1. Druck = Foerderung startet, 2. Druck = Foerderung stoppt und Ergebnis wird ausgegeben. Das
> Loslassen des Tasters hat im Mess-Modus keinen Effekt.

> **Feed-Burst (optional):** Bei `feed_burst_enabled=1` ist auch am Vorschub-Taster ein Triple-Feed-Burst aktiv
> (Standard: deaktiviert wegen Verstopfungsgefahr, wenn Filament im Toolhead steht).

---

## Benutzer-Macros

Diese Macros sind direkt in der Klipper-Konsole aufrufbar und erscheinen in der Mainsail- bzw. Fluidd-Makro-Leiste.

### LOAD_FILAMENT

Laedt Filament von der aktuellen Position durch das Hotend und befuellt den Buffer. Bricht mit Fehlermeldung ab
wenn die Hotend-Temperatur unter `min_temp` liegt.

1. **Phase 1 - Schnell zum Toolhead:** Feeder allein foerdert `load_fast_distance` mm bei `fast_speed` vom Ende
   der Follow-Phase bis zum Toolhead-Eingang.
2. **Phase 2 - Sync durch Hotend:** Feeder und Extruder synchron (1:1, nominale `rotation_distance`) `load_slow` mm
   bei `slow_speed` durch Heatbreak und Nozzle. Hall-Sensoren haben in dieser Phase keinen Einfluss.
3. **Phase 3 - Buffer befuellen:** Feeder allein fuellt den Buffer bis HALL2 aktiv wird (maximale Sicherheitsdistanz:
   `load_buffer_max` mm).

### UNLOAD_FILAMENT

Entlaedt Filament aus Hotend und Buffer. Bricht mit Fehlermeldung ab wenn die Hotend-Temperatur unter
`min_temp` liegt.

1. **Phase 1 - Tip-Forming:** `tip_cycles` Push/Pull-Zyklen bei `tip_speed`, gefolgt von einem finalen Retract
   (`tip_final_retract` mm bei `tip_final_speed`). Formt die Filamentspitze und verhindert Verstopfungen.
2. **Phase 2 - Sync-Retract durch Hotend:** Feeder und Extruder synchron (1:1) `unload_sync` mm bei `slow_speed`
   rueckwaerts durch Heatbreak und Nozzle.
3. **Phase 3a - Schnell zurueck:** Feeder zieht `load_fast_distance` + Follow-Strecke
   (`initial_follow_speed * initial_follow_duration`) mm zurueck - kompletter Weg Toolhead -> `buffer_entrance`.
4. **Phase 3b - Polling bis Sensor frei:** Feeder zieht in 50 mm-Schritten zurueck bis `buffer_entrance` kein
   Filament mehr meldet (max. `unload_fast_max` mm).

### FORCE_BUFFER_FILL

Startet die Erstbefuellung (Grip + Follow) manuell. Nuetzlich wenn die automatische Erstbefuellung beim Einstecken
des Filaments nicht ausgeloest wurde oder neu gestartet werden soll. Prueft zuerst ob `buffer_entrance` Filament
meldet - bricht sonst mit Hinweis ab.

- Setzt alle Flags (`system_enabled`, `initial_lockout`) und deaktiviert den Sensor.
- Startet Grip-Phase (`initial_grip_speed * initial_grip_duration` mm).
- Startet Follow-Phase (`initial_follow_speed * initial_follow_duration` mm).
- Nach Follow-Phase: Sync aktivieren, `LOAD_FILAMENT` bei warmem Hotend automatisch starten (wenn
  `auto_load_after_follow=1`).

### STOP_BUFFER_FILL

Stoppt alle laufenden Foerder-Loops sofort und raeumt alle Flags auf. Notfall-Stopp fuer Grip-, Follow- und
manuelle Foerderung.

- Bricht `_initial_follow_loop`, `_manual_feed_loop` und `_manual_retract_loop` sofort ab.
- Setzt `initial_lockout = 0`, `initial_follow_active = 0`.
- Aktiviert `buffer_entrance`-Sensor wieder.
- Ruft `_APPLY_SYNC_STATE` auf (Sync-Zustand je nach Hall-Sensoren).

### BUFFER_AUTO_ON

Aktiviert die Sync-Automatik direkt ohne Grip- oder Follow-Phase. Nuetzlich wenn das Filament bereits im System
ist und der Sync nach einem manuellen Eingriff oder Neustart direkt aktiviert werden soll.

- Setzt `system_enabled = 1`, alle Sperren auf 0.
- Aktiviert `buffer_entrance`-Sensor.
- Ruft `_APPLY_SYNC_STATE` auf - Feeder laeuft sofort synchron mit dem Extruder.

### ENABLE_RUNOUT_SENSOR / DISABLE_RUNOUT_SENSOR

Aktiviert bzw. deaktiviert den Runout-Zaehler (`print_running`-Flag). Diese Macros haben nur Wirkung wenn
`runout_pause = 1` gesetzt ist. Bei `runout_pause = 0` (Standardwert, externer Sensor vorhanden) wird
`print_running` im Runout-Handler nie ausgewertet - ENABLE/DISABLE haben dann keinen Effekt. Bei
`runout_pause = 1` (kein externer Sensor): Macros in PRINT_START und PRINT_END einbinden.

- `ENABLE_RUNOUT_SENSOR`  in PRINT_START einbinden (nur wirksam bei `runout_pause = 1`)
- `DISABLE_RUNOUT_SENSOR` in PRINT_END einbinden (nur wirksam bei `runout_pause = 1`)

### CALIBRATE_FEEDER_SYNC

Versetzt den Feeder in den Kalibrierungsmodus: Sync ist aktiv mit exakt nominaler `rotation_distance`
(`sync_rotation_distance`), ohne +-20%-Modulation durch Hall-Sensoren. Dient zur Kalibrierung von
`sync_rotation_distance` (Schritt 3).

- Verwendung: `CALIBRATE_FEEDER_SYNC` aufrufen, dann `G1 E100 F60` ausfuehren, am Feeder nachmessen, Formel
  anwenden.

### MEASURE_LOAD_START / MEASURE_LOAD_STOP

Startet bzw. beendet die Messung der `load_fast_distance`. Im Mess-Modus arbeitet der Vorschub-Taster als
Toggle (1. Druck = Start, 2. Druck = Stopp + Ausgabe). Das Ergebnis wird in der Konsole ausgegeben und kann
direkt als `load_fast_distance` eingetragen werden. Hotend KALT halten!

- `MEASURE_LOAD_START` - schaltet den Vorschub-Taster in den Toggle-Modus.
- Vorschub-Taster 1x druecken - Foerderung laeuft.
- Warten bis Filament am Toolhead-Eingang erscheint.
- Vorschub-Taster nochmals druecken - `MEASURE_LOAD_STOP` wird automatisch aufgerufen, Ergebnis erscheint in
  der Konsole.

---

## Runout-Verhalten

Der Sensor `buffer_entrance` erkennt sowohl das Einstecken als auch das Verschwinden von Filament. Das
Runout-Verhalten unterscheidet zwei Situationen:

| Situation | Verhalten |
|-----------|-----------|
| Waehrend LOAD, UNLOAD oder manueller Taster-Operation | Das Filamentende passiert `buffer_entrance` planmaessig. Das Runout-Event wird ignoriert (kein Fehlerzustand). |
| Echter Runout waehrend des Drucks (`runout_pause = 0`, externer Sensor vorhanden) | Feeder laeuft noch synchron weiter. Der interne Polling-Loop wartet bis 100 mm mehr extrudiert wurden als beim Runout-Zeitpunkt. Dann: Feeder entkoppeln und Stepper deaktivieren. Die Druckpause wird vom externen Sensor (z.B. BTT SFS) ausgeloest. |
| Echter Runout waehrend des Drucks (`runout_pause = 1`, kein externer Sensor) | Feeder wird sofort entkoppelt und deaktiviert. Druck wird sofort pausiert (PAUSE). Filament pruefen, neu einlegen und RESUME aufrufen. |

---

## Tipps und Hinweise

### Externer Filament-Sensor empfohlen

Mit `runout_pause = 0` (Standardwert) wird erwartet, dass ein externer Sensor (z.B. BTT SFS V2) die Druckpause
beim Filament-Ende ausloest. Der Buffer kuemmert sich dann nur darum, den Feeder nach dem Runout sauber zu
stoppen (100 mm Nachlauf, dann Stepper deaktivieren). `ENABLE_RUNOUT_SENSOR` und `DISABLE_RUNOUT_SENSOR` haben
in dieser Konfiguration keinen Effekt und muessen nicht eingebunden werden.

### PRINT_START / PRINT_END Integration (nur bei runout_pause = 1)

Nur relevant wenn `runout_pause = 1` gesetzt ist (kein externer Sensor vorhanden). In diesem Modus pausiert der
Buffer den Druck bei Runout selbst - aber nur wenn `print_running = 1` ist. Ohne dieses Flag wird auch bei
`runout_pause = 1` keine Pause ausgeloest. Deshalb: `ENABLE_RUNOUT_SENSOR` am Ende von PRINT_START einbinden,
`DISABLE_RUNOUT_SENSOR` am Anfang von PRINT_END. Bei `runout_pause = 0` (Standardwert): diese Macros nicht
benoetigt.

### Nach Klipper-Neustart

Beim Neustart mit bereits eingelegtem Filament wird NUR der Sync aktiviert - kein automatisches Nachfoerdern.
Das verhindert unnoetiges Foerdern nach jedem Neustart. Eine neue Erstbefuellung nur manuell mit
`FORCE_BUFFER_FILL` starten.

### Buffer-Fuellstand pruefen

Im Normalbetrieb sollte der Buffer etwa im mittleren Drittel gefuellt sein. Ist er staendig voll (HALL2 dauerhaft
aktiv) oder staendig leer (HALL3 dauerhaft aktiv), `sync_modulation` erhoehen oder `sync_rotation_distance`
neu kalibrieren.

### Diagnose mit _STATE_DUMP

Bei unerwartetem Verhalten `_STATE_DUMP` in der Konsole aufrufen. Es werden alle Flags, Hall-Sensor-Rohwerte
und Zustandsvariablen ausgegeben. Besonders nuetzlich um zu pruefen ob Hall-Sensoren korrekt schalten
(raw=RELEASED = aktiv, raw=PRESSED = nicht aktiv).

### HALT wegen HALL1

Wenn HALL1 aktiv ist (Buffer uebergelaufen), entkoppelt der Feeder automatisch (Sync AUS). Sobald HALL1 wieder
inaktiv wird, reconnectet der Sync automatisch - kein manueller Eingriff noetig.

### sync_rotation_distance Wiederholung

Den kalibrierten Wert von `sync_rotation_distance` unbedingt auch in `[extruder_stepper mellow]`
`rotation_distance` eintragen. Dieser Wert ist der Hardware-Default beim Boot - ohne ihn startet Klipper mit dem
unkalibrierten Platzhalter-Wert.

---

## Interne Macros (Diagnose)

Die folgenden Macros sind interne Helfer und erscheinen nicht in der Mainsail-/Fluidd-Makro-Leiste
(Unterstrich-Praefix). Sie sollten im Normalbetrieb nicht direkt aufgerufen werden. Ausnahme: `_STATE_DUMP` ist
ein nuetzliches Diagnose-Werkzeug - fuer User ist nur dieses Macro relevant.

| Macro / Delayed-GCode              | Zweck |
|------------------------------------|-------|
| `_FILAMENT_VARS`                   | Container fuer alle Konfigurationsvariablen (kein ausfuehrbarer Code). |
| `_BUFFER_AUTO_CONTROL`             | Zentrale Zustandsvariablen: Flags fuer Sync, Lockouts, E-Modus, Runout-Referenz. |
| `_APPLY_SYNC_STATE`                | Kernlogik: Liest Hall-Sensor-Zustaende direkt aus der Hardware, berechnet `rotation_distance` und schaltet Sync an/aus. |
| `_SYNC_OFF`                        | Trennt Sync und setzt `rotation_distance` auf Nominalwert zurueck. |
| `_SAVE_E_MODE` / `_RESTORE_E_MODE` | Speichert und stellt den Extruder-Modus (absolut/relativ) bei LOAD/UNLOAD wieder her. |
| `_PREPARE_INITIAL_FILL`            | Gemeinsame Initialisierung fuer Erstbefuellung (`insert_gcode` und `FORCE_BUFFER_FILL`). |
| `_INITIAL_GRIP_PHASE`              | Fuehrt die Grip-Phase der Erstbefuellung aus. |
| `_initial_follow_loop`             | Delayed-GCode-Loop fuer die kontinuierliche Foerderung waehrend der Follow-Phase. |
| `_initial_follow_end`              | Beendet die Follow-Phase, hebt Lockout auf, startet Sync und ggf. `LOAD_FILAMENT`. |
| `_LOAD_PH_ONE_LOOP`                | Loop fuer LOAD Phase 1 (distanzbasierte Foerderung bis `load_fast_distance`). |
| `_LOAD_PH_TWO`                     | Fuehrt LOAD Phase 2 aus (synchrone Hotend-Durchfahrt). |
| `_LOAD_PH_THREE_LOOP`              | Loop fuer LOAD Phase 3 (Buffer befuellen bis HALL2 aktiv). |
| `_UNLOAD_FAST_RETRACT`             | UNLOAD Phase 3a: Schnell-Rueckzug Toolhead -> `buffer_entrance`. |
| `_ABORT_ALL_FEED_LOOPS`            | Cleanup-Helper: Bricht alle aktiven Feed-Loops ab. |
| `_BUTTON_CLICK_HANDLER`            | Zentraler Handler fuer Feed- und Retract-Taster (3-Klick-Logik). |
| `_MANUAL_FEED` / `_MANUAL_RETRACT` | Dauerlauf-Loops fuer Taster-Betrieb. |
| `_TRIPLE_FEED_BURST`               | Fuehrt den Triple-Click-Burst des Vorschub-Tasters aus (nur wenn `feed_burst_enabled=1`). |
| `_TRIPLE_RETRACT_BURST`            | Fuehrt den Triple-Click-Burst des Rueckzug-Tasters aus. |
| `_runout_stepper_disable`          | Polling-Loop nach Runout: deaktiviert Feeder nach 100 mm extrudiertem Filament. |
| `_boot_autostart`                  | Verzoegerter Start nach Klipper-Boot (7 s): aktiviert Sync wenn Filament vorhanden. |
| `_reenable_autofeed`               | Reaktiviert Sync nach Ablauf des `reenable_cooldown`. |
| `_STATE_DUMP`                      | **Diagnose-Macro:** Gibt alle aktuellen Flags, Sensor-Zustaende und Hall-Sensor-Rohwerte in der Konsole aus. Aufruf: `_STATE_DUMP` in der Konsole eingeben. Im Normalbetrieb nur dieses Macro sinnvoll direkt aufrufen. |

---

## Firmware flashen (einmalig)

Dieser Abschnitt beschreibt die einmalige Erstinstallation der MCU-Firmware. Wenn Klipper bereits laeuft und die
MCU erkannt wird, kann dieser Abschnitt uebersprungen werden.

### Katapult-Bootloader (empfohlen)

Katapult (frueher CanBoot) ermoeglicht einfache Firmware-Updates, ohne dass physische Taster gedrueckt oder
der DFU-Modus manuell aufgerufen werden muss.

#### 1. Katapult bauen

```bash
cd ~
git clone https://github.com/Arksine/katapult
cd katapult
make menuconfig
```

**Katapult-Konfiguration:**
- Micro-controller Architecture: `STMicroelectronics STM32`
- Processor model: `STM32F072`
- Build Katapult deployment application: `Do Not build`
- Clock Reference: `8 MHz crystal`
- Communication interface: `USB (on PA11/PA12)`
- Application start offset: `8KiB offset`
- USB ids: Standard lassen oder anpassen
- Support bootloader entry on rapid double click: `[*]` (unbedingt aktivieren!)
- Enable bootloader entry on button (or gpio) state (nicht aktivieren)
- Enable Status LED `[*]`
- (PA8)   Status LED GPIO Pin

```bash
make clean
make
```

#### 2. DFU-Modus aktivieren

Der LLL Buffer Plus muss in den DFU-Modus (Device Firmware Update) versetzt werden:

**Methode 1: BOOT0 auf 3.3V bruecken**
1. BOOT-Taster gedrueckt halten
2. Reset-Taster druecken
3. BOOT-Taster loslassen

**Methode 2: BOOT-Taster (falls zugaenglich)**
1. USB trennen
2. BOOT-Taster auf dem Board gedrueckt halten
3. USB bei gedruecktem BOOT-Taster einstecken
4. BOOT-Taster loslassen

#### 3. DFU-Modus pruefen

```bash
lsusb | grep DFU
```

Erwartete Ausgabe in etwa:
```
Bus 001 Device 015: ID 0483:df11 STMicroelectronics STM Device in DFU Mode
```

Falls nicht erkannt:
```bash
sudo dfu-util -l
```

#### 4. Katapult flashen

```bash
cd ~/katapult
sudo dfu-util -a 0 -D ~/katapult/out/katapult.bin --dfuse-address 0x08000000:force:mass-erase:leave -d 0483:df11
```

Die Ausgabe sollte enden mit:
```
File downloaded successfully
```

#### 5. Katapult verifizieren

USB trennen und erneut verbinden. Katapult-Geraet pruefen:

```bash
ls /dev/serial/by-id/
```

Erwartete Ausgabe in etwa:
```
usb-katapult_stm32f072xb_XXXXXX-if00
```

### Klipper-Firmware

#### 1. Klipper bauen

```bash
cd ~/klipper
make menuconfig
```

**Klipper-Konfiguration:**
- Micro-controller Architecture: `STMicroelectronics STM32`
- Processor model: `STM32F072`
- Bootloader offset: `8KiB bootloader` (fuer Katapult)
- Clock Reference: `8 MHz crystal`
- Communication interface: `USB (on PA11/PA12)`

**Wichtig:** Der Bootloader-Offset MUSS dem Wert aus Katapult entsprechen (8KiB)!

```bash
make clean
make
```

#### 2. Klipper via Katapult flashen

Geraete-ID ermitteln:
```bash
ls /dev/serial/by-id/
```

Mit Katapult-Flashtool flashen:
```bash
python3 ~/katapult/scripts/flashtool.py -f ~/klipper/out/klipper.bin -d /dev/serial/by-id/usb-katapult_stm32f072xb_XXXXXX-if00
```

Alternativ ueber `make flash`:
```bash
make flash FLASH_DEVICE=/dev/serial/by-id/usb-katapult_stm32f072xb_XXXXXX-if00
```

Erwartete Ausgabe:
```
Attempting to connect to bootloader
Katapult Connected
Protocol: 1.0.0
Flashing '/home/pi/klipper/out/klipper.bin'...
[##################################################]
Write complete: X pages
Verifying...
Verification Complete
CRC: 0xXXXXXXXX
Flashing successful
```

#### 3. Klipper verifizieren

USB trennen und erneut verbinden. Geraete-ID sollte sich geaendert haben:

```bash
ls /dev/serial/by-id/
```

Erwartete Ausgabe:
```
usb-Klipper_stm32f072xb_XXXXXX-if00
```

### Ohne Katapult (Alternative)

Wer Katapult nicht verwenden will, kann Klipper direkt flashen:

#### Klipper ohne Bootloader bauen

```bash
cd ~/klipper
make menuconfig
```

**Einstellungen:**
- Micro-controller Architecture: `STMicroelectronics STM32`
- Processor model: `STM32F072`
- Bootloader offset: `No bootloader`
- Clock Reference: `8 MHz crystal`
- Communication interface: `USB (on PA11/PA12)`

```bash
make clean
make
```

#### Via DFU flashen

1. DFU-Modus aktivieren (siehe Schritt 2 oben)
2. Flashen:
   ```bash
   make flash FLASH_DEVICE=0483:df11
   ```

> **Hinweis:** Ohne Katapult muss fuer jedes Firmware-Update wieder manuell der DFU-Modus aktiviert werden.

### Firmware aktualisieren (mit Katapult)

Nach der Katapult-Installation sind Klipper-Updates einfach:

1. **Klipper neu bauen:**
   ```bash
   cd ~/klipper
   make clean
   make
   ```

2. **Via Katapult flashen:**
   ```bash
   python3 ~/katapult/scripts/flashtool.py -f ~/klipper/out/klipper.bin -d /dev/serial/by-id/usb-Klipper_stm32f072xb_XXXXXX-if00
   ```

3. **Oder Double-Tap-Reset verwenden:**
   - Reset-Taster zweimal schnell druecken
   - Das Geraet wechselt fuer 5 Sekunden in den Katapult-Modus
   - Mit Katapult-Geraete-ID flashen

Gehaeuse oeffnen oder BOOT-Taster druecken ist nicht mehr noetig.

---

## Fehlerbehebung

### Flashing-Probleme

**DFU-Geraet wird nicht erkannt:**
- USB-Kabel pruefen (muss ein Datenkabel sein, nicht nur Ladekabel)
- Anderen USB-Port probieren
- `lsusb` ohne grep aufrufen um alle Geraete zu sehen
- Pruefen, ob BOOT0 korrekt auf 3.3V gebrueckt ist
- Beide BOOT-Taster-Methoden durchprobieren

**"Cannot open DFU device":**
```bash
sudo dfu-util -a 0 -D ~/katapult/out/katapult.bin --dfuse-address 0x08000000:force:mass-erase:leave -d 0483:df11
```
Bei Permission-Denied mit `sudo` ausfuehren.

**Katapult erscheint nach dem Flashen nicht:**
- USB trennen und erneut verbinden
- 5-10 Sekunden warten
- `dmesg | tail` auf USB-Events pruefen
- Katapult erneut flashen - eventuell war der Schreibvorgang nicht sauber

**Klipper-Flash via Katapult schlaegt fehl:**
- Pruefen, ob der Bootloader-Offset uebereinstimmt (8KiB in Katapult und Klipper)
- Katapult manuell aktivieren: Reset-Taster zweimal schnell druecken
- Katapult erneut flashen und erneut versuchen

### Buffer-Betrieb

> Die aktuelle Architektur nutzt permanenten Sync (`SYNC_EXTRUDER_MOTION EXTRUDER=mellow MOTION_QUEUE=extruder`)
> mit `rotation_distance`-Modulation ueber `SET_EXTRUDER_ROTATION_DISTANCE`. Hall-Sensoren triggern
> den Modulations-Latch, keine eigenstaendigen Feed-Bursts. Manueller Feed/Retract laeuft ueber
> `_MANUAL_FEED` / `_MANUAL_RETRACT` (FORCE_MOVE, nur ausserhalb Druck). Triple-Burst existiert nur
> am Retract-Taster (und optional am Feed-Taster via `feed_burst_enabled=1`).

**Feeder laeuft dauerhaft zu schnell oder zu langsam:**
- `sync_rotation_distance` falsch kalibriert - mit `CALIBRATE_FEEDER_SYNC` neu kalibrieren (Inbetriebnahme Schritt 3)
- Kalibrierten Wert auch in `[extruder_stepper mellow]` -> `rotation_distance` eintragen
- `sync_modulation` pruefen (Default 0.20 = +-20 %)

**Buffer dauerhaft leer (HALL3 dauerhaft aktiv):**
- Feeder foerdert zu wenig - `sync_rotation_distance` ggf. zu hoch
- Alternativ: `sync_modulation` zu klein (nicht genug Aufhol-Reserve)
- `_STATE_DUMP` in der Konsole pruefen (Hall-Rohwerte, Latch-Zustand)

**Buffer dauerhaft voll (HALL2 dauerhaft aktiv, ggf. HALL1-Overflow):**
- Feeder foerdert zu viel - `sync_rotation_distance` ggf. zu niedrig
- Reverse-Bowden-Spannung pruefen, Druckerverbrauch verifizieren

**HALL1-Overflow wird sofort nach Reset wieder getriggert:**
- Mechanischer Stau im Buffer - Filament-Durchgang manuell pruefen
- `v.triple_click_distance` ggf. reduzieren
- Retract-Mechanik auf Blockade pruefen

**LOAD_FILAMENT endet mit "HALL2 nicht erreicht" (Timeout):**
- `load_buffer_max` ggf. zu niedrig (Default 2000 mm)
- HALL2-Sensor defekt oder Wiring pruefen (`QUERY_ENDSTOPS`)
- Buffer-Mechanik blockiert

**UNLOAD_FILAMENT blockiert oder kein Retract:**
- Temperatur-Check schlaegt fehl: Hotend unter `min_temp` - erst aufheizen
- `sync_locked` bleibt haengen - `_STATE_DUMP` pruefen, ggf. Klipper-Restart

**Nach Klipper-Restart keine Automatik:**
- Boot triggert nicht automatisch eine Erstbefuellung (Design ab P2)
- Manuell `FORCE_BUFFER_FILL` aufrufen, oder Filament kurz rausziehen und wieder einfuehren

**Taster funktionieren nicht:**
- Verkabelung zu PB12 (Vorschub) und PB13 (Rueckzug) pruefen
- Konsole auf "button pressed/released"-Meldungen beobachten
- Sicherstellen, dass die Taster als Normally-Open (NO) verkabelt sind
- Mit `QUERY_ENDSTOPS` bei gedruecktem Taster testen

**MCU wird nach Klipper-Flash nicht erkannt:**
- Pruefen, ob wirklich Klipper-Firmware geflasht wurde (nicht Arduino oder Katapult)
- USB-Verbindung pruefen
- `ls /dev/serial/by-id/` ausfuehren um das Geraet zu finden
- `dmesg | tail` auf USB-Enumeration-Fehler pruefen
- Klipper-Firmware neu flashen

**TMC-UART-Fehler:**
- UART-Pin pruefen: `uart_pin: LLL_PLUS:PB1`
- Sitz des TMC2208 pruefen
- `run_current` nicht zu niedrig (Minimum ca. 0.2)

---

## Revisionen

12.01.2026 - Config auf `extra_stepper` + Force-Moves umgestellt (statt Second-Extruder-Setup). Vermeidet
Konflikte und ermoeglicht das Synchronisieren des Motors mit dem Extruder. Filament-Runout-Switch-Logik
hinzugefuegt. Aktivierung/Deaktivierung via `ENABLE_RUNOUT_SENSOR` / `DISABLE_RUNOUT_SENSOR`
(umbenannt in R-10 auf UPPER_SNAKE - siehe aktuelle Config).

---

## Danksagungen

Klipper-Konfiguration entwickelt von [@ss1gohan13](https://github.com/ss1gohan13) fuer den Mellow LLL
Filament Plus Buffer.

Hardware und Original-Firmware von [Mellow 3D](https://github.com/mellow-3d).

Besonderer Dank an:
- James aus dem Klipper-Discord
- Ian aus dem Klipper-Discord
- [Arksine](https://github.com/Arksine) fuer den Katapult-Bootloader
- Das [Klipper](https://github.com/Klipper3d/klipper)-Team

---

## Lizenz

MIT-Lizenz - Freie Verwendung und Modifikation erlaubt.
