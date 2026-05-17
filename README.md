# Mellow LLL Plus Filament Buffer for Klipper

Diese Erweiterung bindet den Mellow LLL Plus Buffer als eigene
Klipper-Python-Extension ein. Der Buffer-Feeder laeuft auf einer eigenen
Move-Queue und kann waehrend des Drucks parallel zum normalen Druckkopf
arbeiten.

Wichtig in einem Satz:

- Im normalen Druckbetrieb fuettert der Buffer selbststaendig nach,
  ohne dass der Druckkopf dafuer angehalten werden soll.
- Beim expliziten Laden und Entladen wird der Buffer bewusst an den
  Extruder gekoppelt. In diesen Sonderfaellen kann der Druckkopf
  sichtbar warten. Das ist Absicht und kein Fehler.

Die Datei `lll.cfg` ist die mitgelieferte Beispiel- und Testkonfiguration.
Der Python-Code hat zusaetzlich interne Fallback-Defaults, aber fuer den
Alltag ist `lll.cfg` die relevante Benutzerkonfiguration.

---

## Inhalt

- [Was das Plugin macht](#was-das-plugin-macht)
- [Was sich gegenueber frueher geaendert hat](#was-sich-gegenueber-frueher-geaendert-hat)
- [Voraussetzungen](#voraussetzungen)
- [Installation](#installation)
- [Wichtige Konfiguration](#wichtige-konfiguration)
- [Normale Bedienung](#normale-bedienung)
- [Wichtige GCode-Commands](#wichtige-gcode-commands)
- [Status und Logs](#status-und-logs)
- [Typische Probleme](#typische-probleme)
- [Technischer Anhang](#technischer-anhang)
- [Firmware flashen](#firmware-flashen)
- [Danksagungen](#danksagungen)
- [Lizenz](#lizenz)

---

## Was das Plugin macht

Der Buffer kennt vier wesentliche Zustaende:

| Signal | Einfache Bedeutung | Reaktion |
|---|---|---|
| `HALL3` | Buffer ist leer / weit unten | Buffer darf nachfoerdern |
| Zwischenzone | Buffer steht im Arbeitsbereich | Buffer haelt oder foerdert passend zum Verbrauch |
| `HALL2` | Buffer ist voll | Buffer stoppt |
| `HALL1` | Buffer ist ueberfuellt / kritisch | Sofortiger Sicherheits-Stopp |

Im aktuellen Standardbetrieb arbeitet das Plugin nicht mehr nur als
einfaches "an/aus". Stattdessen beobachtet es den realen Filamentverbrauch
des Extruders und passt die Buffer-Geschwindigkeit daran an.

Einfach gesagt:

1. Der Druckkopf zieht Filament.
2. Der Buffer erkennt, wie viel gerade verbraucht wird.
3. Der Buffer schiebt passend nach.
4. Die Sensoren begrenzen und sichern das Ganze mechanisch ab.

Das ist der Grund, warum dieses Projekt fuer den Druckbetrieb eine eigene
Queue benutzt und gerade nicht auf `SYNC_EXTRUDER_MOTION` oder
`MANUAL_STEPPER MOVE` im normalen AUTO-Pfad setzt.

### Was das Plugin nicht machen soll

- Es soll den Druckkopf waehrend des normalen Drucks nicht absichtlich
  anhalten.
- Es soll keine zweite Kinematik fuer XYZ sein.
- Es soll keine Wunder aus einer unkalibrierten Mechanik machen.
- Es ersetzt keinen echten Encoder oder Stall-Detection.

---

## Was sich gegenueber frueher geaendert hat

Falls du aeltere Branches oder alte Doku kennst, sind diese Punkte
wichtig:

- Der aktuelle Druckpfad ist auf die Python-Extension mit eigener Queue
  ausgelegt.
- Die mitgelieferte `lll.cfg` ist auf den heutigen Refactor-Stand
  abgestimmt.
- Der Standardbetrieb nutzt die moderne
  `motion_queuing`-Anbindung ueber `use_flush_callback_bang_bang: True`.
- Der Druckstart ist jetzt bewusst abgesichert: AUTO-Streaming bleibt
  gesperrt, bis echte Extruderbewegung erkannt wurde.
- Fuer hohe Durchsaetze gibt es eine High-Flow-Korrektur, damit der
  Buffer in der Zwischenzone nicht zu frueh "aufhoert".

---

## Voraussetzungen

Du brauchst:

- aktuelles Mainline-Klipper
- einen funktionierenden `LLL_PLUS`-MCU-Eintrag
- den Mellow LLL Plus Buffer mit angeschlossenem Stepper und Sensoren
- in `printer.cfg` mindestens:
  - `[pause_resume]`
  - `[extruder]`
  - `max_extrude_only_distance` gross genug fuer deine Buffer-Makros

Mit der mitgelieferten `lll.cfg` solltest du fuer den Extruder
mindestens diesen Wert einplanen:

```ini
[extruder]
max_extrude_only_distance: 400
```

Warum 400?

- `load_slow_distance` in der mitgelieferten Config ist 100 mm
- `unload_sync_distance` in der mitgelieferten Config ist 400 mm

Der Extruder muss also mindestens den groessten dieser rein
extrudergetriebenen Wege erlauben.

---

## Installation

### 1. Repo holen

```bash
cd ~
git clone https://github.com/Avatarsia/BufferPLUS-klipper.git
cd BufferPLUS-klipper
```

Wenn du einen bestimmten Entwicklungsstand testen willst, checke danach
den gewuenschten Branch aus.

### 2. Installer ausfuehren

```bash
./install.sh
```

Der Installer kann:

- die Python-Dateien nach `klippy/extras/` verlinken
- `lll.cfg` nach `printer_data/config/` kopieren
- `[include lll.cfg]` in `printer.cfg` ergaenzen
- optional den Moonraker `update_manager` eintragen

### 3. Klipper neu starten

```bash
sudo systemctl restart klipper
```

### 4. Pruefen

In der Klipper-Konsole:

```gcode
BUFFER_STATE_DUMP BUFFER=mellow
```

Wenn du nur eine einzige Buffer-Instanz hast, funktionieren viele
Commands oft auch ohne `BUFFER=mellow`. In dieser README nutze ich den
Parameter trotzdem immer explizit, damit die Beispiele eindeutig bleiben.

### Update-Hinweis

Es gibt zwei Wege:

- `./install.sh`
  - interaktiv
  - zeigt Unterschiede bei `lll.cfg`
  - besser fuer normale Anwender
- `./update.sh`
  - nicht interaktiv
  - zieht Git-Updates und ueberschreibt `lll.cfg`
  - eher fuer Entwickler oder bewusstes Testen

### Moonraker Auto-Update

Wenn du den `update_manager` manuell eintragen willst, achte darauf,
dass `primary_branch` zu dem Branch passt, den du wirklich benutzen
moechtest.

Beispiel:

```ini
[update_manager buffer_feeder]
type: git_repo
path: ~/BufferPLUS-klipper
origin: https://github.com/Avatarsia/BufferPLUS-klipper.git
primary_branch: <dein-branch>
is_system_service: False
managed_services: klipper
```

---

## Wichtige Konfiguration

Nicht jede Option ist fuer jeden Anwender gleich wichtig. Fuer die
meisten Setups gibt es zwei Gruppen:

### Diese Werte musst du praktisch immer anfassen

| Parameter | Wo | Wofuer |
|---|---|---|
| `rotation_distance` | `[buffer_feeder mellow]` | korrekte Foerdermenge des Buffer-Steppers |
| `load_fast_distance` | `[buffer_feeder mellow]` | Strecke bis kurz vor den Toolhead |
| `load_slow_distance` | `[buffer_feeder mellow]` | Strecke durch Heatbreak/Hotend beim Laden |
| `unload_sync_distance` | `[buffer_feeder mellow]` | synchroner Extruder-Rueckzug beim Entladen |

### Diese Werte sind in der mitgelieferten Config bereits bewusst gesetzt

Die aktuelle `lll.cfg` ist kein generischer "Minimalwert", sondern ein
getunter Arbeitsstand. Wichtige Beispiele:

| Parameter | Wert in `lll.cfg` | Bedeutung |
|---|---:|---|
| `feed_speed` | `70` | obere Nachfoerdergeschwindigkeit im AUTO-Betrieb |
| `min_feed_floor` | `10.0` | niedriger H3-Mindestwert, damit mittlere bis hohe Flows frueher in die dynamische Regelung kommen |
| `lead_time` | `0.12` | zeitlicher Vorlauf fuer geplante Moves |
| `use_flush_callback_bang_bang` | `True` | moderner Druckpfad ueber `motion_queuing` |
| `flush_callback_chunk_mm` | `45` | groessere Chunks fuer besseren Durchsatz |
| `interrupt_chunk_mm` | `9` | kleine Sicherheits-Sub-Chunks fuer schnelle Abbrueche |
| `strict_print_start_guard` | `True` | kein AUTO-Feed vor echter Extrusion |
| `high_flow_mm3s_threshold` | `24.0` | High-Flow-Schutz gegen Unterfoerderung |
| `buffer_debug_metrics` | `True` | aktuell fuer Test-/Hardware-Analyse aktiv |

### Welche Debug-Schalter es gibt

| Parameter | Wirkung |
|---|---|
| `buffer_debug_events` | loggt Entscheidungen und Handler-Ereignisse ins `klippy.log` |
| `buffer_debug_metrics` | loggt laufende Messwerte, Sensorlage und Timing ins `klippy.log` |

Fuer normalen Dauerbetrieb solltest du `buffer_debug_metrics` nur dann
aktiv lassen, wenn du bewusst Hardwaretests faehrst.

### High-Flow-Hinweis

Die aktuelle Logik beruecksichtigt hohe volumetrische Stroeme. Oberhalb
von `high_flow_mm3s_threshold` darf die Zwischenzone weiter
proportional foerdern, auch wenn die lineare Extruder-Geschwindigkeit
unterhalb des klassischen `min_feed_floor` liegt.

Der praktische Grund:

- `24 mm^3/s` sind bei `1.75 mm` Filament nur rund `10 mm/s` linear.
- Auf diesem Referenzsystem ist `min_feed_floor` deshalb bewusst auf
  `10.0` gesetzt, damit der Buffer bei ca. `24-30 mm^3/s` frueher aus
  dem festen H3-Minimum in die dynamische Regelung kommt.
- Ohne diesen Sonderfall waere der Buffer bei hohem Flow oft zu
  burst-lastig und koennte unterfoerdern.

---

## Normale Bedienung

### Normaler Druck

Im Normalfall laeuft das so:

1. Filament steckt im Buffer.
2. Der Druck startet.
3. Das Plugin wartet kurz, bis echte Extruderbewegung sichtbar ist.
4. Erst dann beginnt die automatische Nachfoerderung.

Das ist Absicht. So wird verhindert, dass der Buffer schon in der
Startphase "auf Verdacht" foerdert.

### Filament laden

Das mitgelieferte Makro ist:

```gcode
LOAD_FILAMENT
```

Der Ablauf ist:

1. Buffer foerdert schnell bis kurz vor den Toolhead.
2. Buffer fuellt sich bis zum Sensorsignal.
3. Buffer und Extruder laufen kurz synchron, damit das Filament sauber
   ins Hotend kommt.

Wichtig:

- Diese letzte Synchronphase ist bewusst ein Sonderpfad.
- Dabei kann der Druckkopf bzw. der normale Planner sichtbar warten.
- Das ist fuer LOAD gewollt und kein Widerspruch zum normalen
  AUTO-Druckbetrieb.

### Filament entladen

```gcode
UNLOAD_FILAMENT
```

Das Makro kuemmert sich um:

- Tip-Forming
- optionales Abkuehlen der Spitze
- synchronen Rueckzug ueber den Extruder
- anschliessenden Rueckzug durch den Buffer

### Buffer manuell fuellen

Wenn Filament am Eingang anliegt und du den Buffer einmal aktiv
vorspannen willst:

```gcode
FORCE_BUFFER_FILL BUFFER=mellow
```

Abbrechen:

```gcode
STOP_BUFFER_FILL BUFFER=mellow
```

### Jam zuruecksetzen

Wenn der Buffer auf JAM steht:

```gcode
CLEAR_JAM
```

oder direkt:

```gcode
BUFFER_CLEAR_JAM BUFFER=mellow
```

---

## Wichtige GCode-Commands

### Alltagsbefehle

| Command | Zweck |
|---|---|
| `BUFFER_AUTO_ON BUFFER=mellow` | AUTO-Betrieb einschalten |
| `BUFFER_AUTO_OFF BUFFER=mellow` | AUTO-Betrieb ausschalten und Lockouts aufraeumen |
| `BUFFER_HALT BUFFER=mellow` | Feeder sofort stoppen |
| `BUFFER_STATE_DUMP BUFFER=mellow` | kompletten Zustand ausgeben |
| `BUFFER_WAIT_IDLE BUFFER=mellow` | warten, bis der Buffer wirklich fertig ist |
| `FORCE_BUFFER_FILL BUFFER=mellow` | Buffer manuell greifen und fuellen |
| `STOP_BUFFER_FILL BUFFER=mellow` | laufenden Fill-/Grip-Vorgang abbrechen |
| `CLEAR_JAM` | JAM-Lockout ueber Wrapper-Makro loesen |

### Direkte Bewegungen

| Command | Zweck |
|---|---|
| `BUFFER_FEED BUFFER=mellow DISTANCE=<mm> SPEED=<mm/s>` | Filament vorwaerts foerdern |
| `BUFFER_RETRACT BUFFER=mellow DISTANCE=<mm> SPEED=<mm/s>` | Filament rueckwaerts foerdern |

Ohne `DISTANCE` laeuft `BUFFER_FEED` als Dauerlauf, bis du ihn stoppst.

### Lade-/Entlade-Bausteine

Diese Befehle sind eher fuer Debug oder eigene Makros gedacht:

| Command | Zweck |
|---|---|
| `BUFFER_LOAD_PHASE1 BUFFER=mellow` | schneller Vorlauf bis kurz vor den Toolhead |
| `BUFFER_LOAD_PHASE3 BUFFER=mellow` | Buffer-Sensorphase beim Laden |
| `BUFFER_UNLOAD_PHASE3 BUFFER=mellow` | rueckwaerts foerdern bis Eingang frei |
| `BUFFER_SYNC_TO_EXTRUDER BUFFER=mellow EXTRUDER=extruder` | Buffer an Extruder koppeln |
| `BUFFER_UNSYNC BUFFER=mellow` | Buffer wieder entkoppeln |

### Laufzeit-Tuning

Wichtiger Sammelbefehl:

```gcode
BUFFER_SET
```

Ohne Argumente zeigt er die aktuellen Laufzeitwerte.

Beispiele:

```gcode
BUFFER_SET DEBUG_EVENTS=1
BUFFER_SET DEBUG_METRICS=1
BUFFER_SET HIGH_FLOW_MM3S=30
BUFFER_SET LEAD_TIME=0.10
BUFFER_SET SPEED=75
```

Wichtige `BUFFER_SET`-Parameter:

| Parameter | Wirkung |
|---|---|
| `CHUNK_MM` | `flush_callback_chunk_mm` aendern |
| `INTERRUPT_CHUNK_MM` | Sicherheits-Sub-Chunk aendern |
| `SPEED` | `feed_speed` aendern |
| `LEAD_TIME` | `lead_time` aendern |
| `MAX_MOVE_CHUNK_MM` | maximale Move-Groesse aendern |
| `DEBUG_EVENTS` | Ereignis-Logs an/aus |
| `DEBUG_METRICS` | Metrik-Logs an/aus |
| `STRICT_START_GUARD` | Druckstart-Schutz an/aus |
| `CRITICAL_GUARD_S` | Schutzfenster nach kritischen Aktionen |
| `CONSERVATIVE_MODE` | defensiveren Testmodus aktivieren |
| `HIGH_FLOW_MM3S` | High-Flow-Schwelle aendern |

Wichtig: `BUFFER_SET` ist nicht persistent. Wenn ein Wert gut funktioniert,
musst du ihn anschliessend in `lll.cfg` uebernehmen.

---

## Status und Logs

### Schnellster Gesundheitscheck

```gcode
BUFFER_STATE_DUMP BUFFER=mellow
```

Das ist der beste erste Blick auf:

- aktuellem State
- Sensorlage
- aktiven Guards
- JAM-/RUNOUT-Status
- Debug-Flags

### Wichtige Status-Felder fuer Makros

Zugriff erfolgt ueber:

```jinja
printer["buffer_feeder mellow"].<feld>
```

Die wichtigsten Felder:

| Feld | Bedeutung |
|---|---|
| `state` | aktueller Hauptzustand des Buffers |
| `hall_empty` | HALL3 aktiv |
| `hall_full` | HALL2 aktiv |
| `hall_overflow` | HALL1 aktiv |
| `entrance_detected` | Filament am Eingang erkannt |
| `continuous_feed` | Buffer foerdert gerade aktiv |
| `jam_active` | JAM-Lockout ist aktiv |
| `bang_bang_suspended` | AUTO waehrend Pause unterdrueckt |
| `print_phase` | `inactive`, `guarded`, `active` oder `paused` |
| `critical_action_guard_remaining_s` | Restzeit eines Schutzfensters |
| `synced_to_extruder` | falls der Buffer gerade am Extruder haengt |

### Logs fuer Fehlersuche

Es gibt zwei Ebenen:

1. `buffer_debug_events`
   - fuer Entscheidungen
   - warum ein Submit gemacht oder ausgelassen wurde
2. `buffer_debug_metrics`
   - fuer Messwerte
   - Sensorzonen, Extruderverbrauch, Timing, High-Flow-Lage

Beides landet direkt im `klippy.log`.

Empfohlener Ablauf:

1. Fehler reproduzieren
2. `BUFFER_SET DEBUG_EVENTS=1`
3. falls noetig zusaetzlich `BUFFER_SET DEBUG_METRICS=1`
4. Test wiederholen
5. danach wieder abschalten

---

## Typische Probleme

### Der Druckkopf pausiert waehrend `LOAD_FILAMENT`

Das ist bei diesem Workflow erwartbar.

Grund:

- `LOAD_FILAMENT` benutzt in Phase 3 bewusst
  `BUFFER_SYNC_TO_EXTRUDER` und danach `BUFFER_UNSYNC`
- fuer diese Trapq-Umschaltung muss Klipper intern flushen
- das kann sichtbar wie eine kurze Pause wirken

Wichtig ist die Unterscheidung:

- im normalen AUTO-Druckbetrieb unerwuenscht
- bei explizitem LOAD/UNLOAD bewusst akzeptiert

### Der Buffer foerdert beim Druckstart zu frueh

Dafuer gibt es heute den `strict_print_start_guard`.

Pruefen:

- `strict_print_start_guard: True`
- `print_phase` in `BUFFER_STATE_DUMP`
- `buffer_debug_events`

### Der Buffer foerdert bei hohem Flow nicht schnell genug

Dann sind vor allem diese Punkte relevant:

- `feed_speed`
- `min_feed_floor`
- `feed_speed_gain`
- `flush_callback_chunk_mm`
- `interrupt_chunk_mm`
- `high_flow_mm3s_threshold`
- reale Mechanik und Kalibrierung

Fuer die aktuelle Logik ist der kritische Bereich besonders um
`24 mm^3/s` und darueber relevant.

### "Exception in flush_handler" oder "Invalid sequence"

Dann zuerst das `klippy.log` sichern, bevor du neu startest.

Wichtige Marker:

- `Exception in flush_handler`
- `stepcompress ... Invalid sequence`
- `Timer too close`
- der Python-Traceback direkt davor

Die Shutdown-Meldung in Mainsail ist meistens nur das Ende der Kette,
nicht die eigentliche Ursache.

### Plugin startet nicht wegen `motion_queuing`

Die aktuelle Konfiguration erwartet modernes Mainline-Klipper.
Fehlt die API, bricht das Plugin heute absichtlich mit klarer Meldung ab,
statt halb zu starten.

### Sensoren wirken invertiert

Dann zuerst:

- Pinbelegung pruefen
- `^!`-Konvention in `lll.cfg` nicht blind aendern
- `BUFFER_STATE_DUMP BUFFER=mellow` waehrend du den Arm haendisch bewegst

### Jam-Detection ist zu empfindlich

Anpassen:

- `jam_clog_dwell_time`
- `jam_clog_extrude_min`
- `jam_supply_dwell_time`

Oder komplett deaktivieren:

```ini
jam_detection_enabled: 0
```

---

## Technischer Anhang

Dieser Abschnitt ist bewusst kurz und soll die Architektur nur so weit
erklaeren, dass man ihr Verhalten versteht.

### 1. Normaler Druckpfad

Im normalen Druckbetrieb:

- hat der Buffer seine eigene Trapq
- darf der Buffer parallel zum Druckkopf arbeiten
- wird `SYNC_TO_EXTRUDER` nicht benutzt
- soll `flush_step_generation()` den Druckkopf nicht ausbremsen

Mit der mitgelieferten Config ist das der Standardpfad.

### 2. LOAD/UNLOAD-Sonderpfad

Beim Laden und Entladen ist das anders:

- Buffer und Extruder werden bewusst gekoppelt
- das ist mechanisch sauberer fuer Hotend-Einzug und Tip-Forming
- dafuer ist eine sichtbare Pause des normalen Druckpfads akzeptiert

### 3. Druckstart-Schutz

Das Plugin unterscheidet heute zwischen:

- Druck laeuft formal
- echte Extrusion hat wirklich begonnen
- Buffer darf wirklich nachfoerdern

Darum existiert `print_phase` und der Start-Guard.

### 4. High-Flow-Korrektur

Der Buffer arbeitet nicht nur mit den Sensoren, sondern auch mit dem
gemessenen Extruderverbrauch. Das ist besonders wichtig bei hoeheren
Durchsaetzen, weil dort ein reines HALL3-"an/aus" zu spaet oder zu
aggressiv reagieren kann.

### 5. Harte Grenzen

Das Projekt kann viel, aber nicht alles:

- nur ein Buffer gleichzeitig
- keine persistente automatische Kalibrierung
- keine Encoder-basierte Schlupferkennung
- Abhaengigkeit von aktueller Mainline-Klipper-API

---

## Firmware flashen

Kurzfassung:

1. Katapult-Bootloader flashen
2. Klipper-Firmware mit passendem Offset bauen
3. `serial:` in `[mcu LLL_PLUS]` auf dein Device setzen

Beispiel:

```bash
cd ~/katapult
make menuconfig
make clean && make
sudo dfu-util -a 0 -D out/katapult.bin --dfuse-address 0x08000000:force:mass-erase:leave -d 0483:df11
```

```bash
cd ~/klipper
make menuconfig
make clean && make
python3 ~/katapult/scripts/flashtool.py -f out/klipper.bin -d /dev/serial/by-id/usb-katapult_stm32f072xb_*
```

Danach in `lll.cfg`:

```ini
[mcu LLL_PLUS]
serial: /dev/serial/by-id/...
```

---

## Danksagungen

- Originale Konfigurationsidee von
  [@ss1gohan13](https://github.com/ss1gohan13)
- Hardware und Ausgangsfirma:
  [Mellow 3D](https://github.com/mellow-3d) und
  [Fly3DTeam](https://github.com/Fly3DTeam/Buffer)
- Referenz fuer Klipper-Patterns:
  [Happy Hare](https://github.com/moggieuk/Happy-Hare)
- Katapult:
  [Arksine](https://github.com/Arksine)
- Klipper-Team

---

## Lizenz

Dieses Repo liegt unter der GNU GPL v3. Siehe [LICENSE](LICENSE).
