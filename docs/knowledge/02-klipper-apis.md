# Klipper-APIs für die LLL Buffer Config

## Plattform-Festlegung

Der User fährt **Mainline Klipper** (`Klipper3d/klipper`). Alle Beispiele hier benutzen Mainline-Syntax. Abweichende Forks (z. B. Kalico) werden explizit gekennzeichnet, wenn sie relevant sind.

---

## extruder_stepper

Die Section `[extruder_stepper name]` definiert zusätzliche Stepper, die optional mit einem `[extruder]` synchronisiert werden können, ohne einen weiteren Extruder-Heater/Heizkreis zu erzeugen. Typischer Use-Case: mehrere Motoren (Dual-Drive, Buffer-Feeder), die gemeinsam mit dem Main-Extruder fahren oder unabhängig gestellt werden.

### Config-Keys (Mainline Klipper)

```ini
[extruder_stepper my_extruder_stepper]
step_pin:
#   Step GPIO pin. Pflicht.
dir_pin:
#   Direction GPIO pin. Pflicht.
enable_pin:
#   Enable pin (default active high; '!' invertiert auf active low).
microsteps:
#   Anzahl Microsteps. Pflicht.
rotation_distance:
#   Distanz (mm) pro voller Stepper-Umdrehung. Pflicht.
#full_steps_per_rotation: 200
#   200 fuer 1.8-Grad-Motoren, 400 fuer 0.9-Grad. Default 200.
#gear_ratio:
#   Getriebe-Uebersetzung z. B. "5:1". Default: keine.
#step_pulse_duration:
#   Minimale Zeit zwischen Step-Pulsen. Default 100ns (TMC UART/SPI)
#   bzw. 2us fuer andere.
#pressure_advance: 0.0
#   Nur wirksam wenn an einen Extruder synced.
#pressure_advance_smooth_time: 0.040
```

### Verhalten gegenüber `[extruder]`

- Beim Boot ist ein `extruder_stepper` standardmässig mit dem ersten `[extruder]` synchronisiert, sofern beide existieren. Für unsere Buffer-Motoren (die NICHT drucken sollen) muss der Sync beim Start aktiv entfernt werden (`SYNC_EXTRUDER_MOTION EXTRUDER=<name> MOTION_QUEUE=`).
- Nur synced Stepper respektieren `pressure_advance`; ein un-synced Stepper wird über `FORCE_MOVE` oder über den synchronisierten Master-Extruder bewegt.

### Wann `gear_ratio` vs. `microsteps`

- `microsteps` ist TMC-Treiber-Config; wird immer gesetzt.
- `gear_ratio` nur angeben, wenn mechanisches Getriebe (z. B. 5:1 bei BMG-Klonen) vorhanden ist. Für den LLL-Buffer-Feeder (direkt getriebener NEMA14 / NEMA17 ohne Planetengetriebe) leer lassen und stattdessen `rotation_distance` auf den effektiven Umfang der Feeder-Rolle rechnen.

Quelle: `Config_Reference.html#extruder_stepper`.

---

## SYNC_EXTRUDER_MOTION

### Exakte Syntax (Mainline Klipper)

```
SYNC_EXTRUDER_MOTION EXTRUDER=<stepper-name> MOTION_QUEUE=<extruder-name-oder-leer>
```

- `EXTRUDER=<stepper-name>`: Name des zu steuernden `[extruder_stepper ...]` (oder eines `[extruder]`).
- `MOTION_QUEUE=<extruder-name>`: Ziel-Extruder, dessen Bewegungs-Queue gefolgt werden soll.
- **MOTION_QUEUE leer = Sync AUS**: der Stepper fährt nicht mehr mit dem Extruder mit; er kann dann per `FORCE_MOVE` unabhängig bewegt werden.
- **MOTION_QUEUE=extruder = Sync AN**: der Stepper folgt 1:1 allen E-Moves des benannten Extruders.

### Evidenz für den mux-Key (Mainline)

Aus `klippy/kinematics/extruder.py` (Mainline, ca. Zeile 69–71):

```python
gcode.register_mux_command("SYNC_EXTRUDER_MOTION", "EXTRUDER",
                           self.name, self.cmd_SYNC_EXTRUDER_MOTION,
                           desc=self.cmd_SYNC_EXTRUDER_MOTION_help)
```

Das zweite Argument `"EXTRUDER"` ist der Mux-Key — der Parameter in der G-Code-Zeile heißt also **`EXTRUDER=`**.

### Kalico-Abweichung

Im Kalico-Fork heißt der Mux-Key `STEPPER=` statt `EXTRUDER=`. **Für diese Config irrelevant** — der User fährt Mainline. Wer von einem Kalico-Makro kopiert, muss den Parameter anpassen, sonst schlägt `SYNC_EXTRUDER_MOTION` mit "missing parameter" fehl.

Quelle: `github.com/Klipper3d/klipper/blob/master/klippy/kinematics/extruder.py`.

---

## SET_EXTRUDER_ROTATION_DISTANCE

### Syntax

```
SET_EXTRUDER_ROTATION_DISTANCE EXTRUDER=<name> DISTANCE=<float>
```

- `EXTRUDER=<name>`: Zielstepper — kann ein `[extruder]` ODER ein `[extruder_stepper ...]` sein.
- `DISTANCE=<float>`: neue `rotation_distance` in mm. Mit `0` abzufragen liefert den aktuellen Wert (nicht zum Setzen benutzen).

### Evidenz für den mux-Key

Aus `klippy/kinematics/extruder.py` (Mainline, ca. Zeile 66–68):

```python
gcode.register_mux_command("SET_EXTRUDER_ROTATION_DISTANCE", "EXTRUDER",
                           self.name, self.cmd_SET_E_ROTATION_DISTANCE,
                           desc=self.cmd_SET_E_ROTATION_DISTANCE_help)
```

Mux-Key: `"EXTRUDER"` → Parameter: **`EXTRUDER=`**.

### Verhalten bei synchronisierten Steppern

- Ändert die effektive Fördermenge des Steppers für alle folgenden Moves.
- Synchron gekoppelte Stepper fahren weiterhin dieselben Steps, aber der geänderte `rotation_distance` wirkt sich direkt auf die Materiallänge aus.

### Persistenz

**Nicht persistent.** Nach Klipper-Restart (oder `FIRMWARE_RESTART`) wird der Wert aus der `.cfg` wiederhergestellt. Wenn ein dynamisch gesetzter Wert über Reboot hinweg halten soll, muss er über `save_variables` oder Makro-Logik beim Boot neu gesetzt werden.

---

## FORCE_MOVE

### Syntax

```
FORCE_MOVE STEPPER=<config_name> DISTANCE=<mm> VELOCITY=<mm/s> [ACCEL=<mm/s^2>]
```

- `STEPPER=`: Config-Name des Steppers (z. B. `extruder_stepper feeder_left`).
- `DISTANCE=`: mm (signed — Vorzeichen = Richtung).
- `VELOCITY=`: Konstante Geschwindigkeit.
- `ACCEL=`: Optional; `0` deaktiviert Beschleunigung.

### Voraussetzung

```ini
[force_move]
enable_force_move: True
```

Ohne diesen Flag lehnt Klipper `FORCE_MOVE` ab.

### Nebenwirkungen (wichtig)

Aus der offiziellen Doku (G-Codes.html):

> "No boundary checks are performed; no kinematic updates are made; other parallel steppers on an axis will not be moved."

Konkret:

- Kinematik wird in einen ungültigen Zustand versetzt; nach dem Gebrauch auf XYZ-Achsen ist `G28` erforderlich, um sie zurückzusetzen. Für reine Extruder-/Feeder-Stepper ist das unkritisch.
- **Toolhead-Flush**: FORCE_MOVE erzwingt einen Flush der Move-Queue. Wenn der Stepper synchronisiert ist oder zur Toolhead gehört, pausiert der Druckkopf kurz.
- FORCE_MOVE bewegt **nur den einen benannten Stepper** — synchronisierte Partner-Stepper bleiben stehen. Das führt bei einem synced Buffer-Stepper zum Sync-Bruch.

### Darum in dieser Config

Wenn ein Stepper synced ist, vor dem `FORCE_MOVE` immer

```
SYNC_EXTRUDER_MOTION EXTRUDER=<stepper> MOTION_QUEUE=
```

aufrufen (Sync lösen), sonst wird der eigentliche Extruder beim Flush und beim anschliessenden Move auseinanderlaufen. Nach dem `FORCE_MOVE` ggfs. mit `SYNC_EXTRUDER_MOTION EXTRUDER=<stepper> MOTION_QUEUE=extruder` wieder aktiv koppeln.

Quelle: `klipper3d.org/G-Codes.html#force_move`.

---

## gcode_button

### Config-Keys

```ini
[gcode_button my_button]
pin:
#   GPIO-Pin. Pflicht.
#   Pin-Prefixe: '^' = Pullup, '!' = invert, '^!' = beides kombiniert.
#press_gcode:
#   G-Code-Template, das beim Wechsel RELEASED -> PRESSED laeuft.
#release_gcode:
#   G-Code-Template, das beim Wechsel PRESSED -> RELEASED laeuft.
#analog_range_press_gcode / analog_range_release_gcode / analog_pullup_resistor:
#   nur fuer Analog-Pins relevant.
```

### Properties unter `printer["gcode_button name"]`

- **`.state`** — String: `"PRESSED"` oder `"RELEASED"`.

### Wichtig bei `^!`-Pin

Der `!`-Prefix invertiert das Signal. Wenn der Sensor physisch aktiv ist (z. B. Mikroschalter geschlossen, `IR`-Sensor hat Filament erkannt), wird der interne Zustand nach Invertierung als **RELEASED** gewertet — der `release_gcode` triggert also, wo man naiv den `press_gcode` erwartet.

Faustregel mit `pin: ^!...`:

- Sensor **aktiv** → interner State `RELEASED` → `release_gcode` feuert beim Wechsel dorthin.
- Sensor **inaktiv** → interner State `PRESSED` → `press_gcode` feuert beim Wechsel dorthin.
- Abfrage über `{% if printer["gcode_button foo"].state == "RELEASED" %}` bedeutet mit `^!`: "Sensor ist gerade AKTIV".

Wer konsistent "Sensor aktiv = PRESSED" haben will, lässt das `!` weg und nutzt nur `^` — dann drehen sich press/release sinngemäss um. Die Entscheidung hängt am Hardware-Signalpegel (Active-High vs. Active-Low) des jeweiligen Sensors.

Quelle: `klipper3d.org/Config_Reference.html#gcode_button`.

---

## filament_switch_sensor

### Config-Keys

```ini
[filament_switch_sensor my_sensor]
switch_pin:
#   GPIO-Pin des Schalters. Pflicht. Pullup/invert wie bei gcode_button.
pause_on_runout: True
#   Bei Runout automatisch PAUSE aufrufen. Default True.
runout_gcode:
#   Optionaler G-Code beim Runout (nach PAUSE, falls pause_on_runout).
insert_gcode:
#   G-Code beim Einlegen von Filament.
event_delay: 3.0
#   Minimale Sekunden zwischen aufeinanderfolgenden Events. Default 3.0.
#pause_delay: 0.5
#   Verzoegerung (s) zwischen Erkennen und PAUSE. Default 0.5.
```

### Properties unter `printer["filament_switch_sensor name"]`

- **`.filament_detected`** — Boolean: `True` wenn Filament anliegt.
- **`.enabled`** — Boolean: ob der Sensor aktiv überwacht.

### Beispiel

```ini
[filament_switch_sensor runout_sensor]
switch_pin: ^PG4
pause_on_runout: True
runout_gcode: M600
insert_gcode: M601
event_delay: 3.0
```

Quelle: `klipper3d.org/Config_Reference.html#filament_switch_sensor`.

---

## delayed_gcode

### Syntax

```ini
[delayed_gcode my_task]
gcode:
    # G-Code-Template, Jinja2 erlaubt
    M118 Hello from delayed
#initial_duration: 0.0
#   Sekunden nach Klipper-Start, dann einmalig feuern. 0 = nie auto-feuern.
```

### `UPDATE_DELAYED_GCODE`

```
UPDATE_DELAYED_GCODE ID=<name> DURATION=<seconds>
```

- `DURATION=0` bricht einen geplanten Lauf ab.
- `DURATION=N` (N>0) feuert in N Sekunden; ein bereits geplanter Lauf wird auf den neuen Wert überschrieben.

### Self-Call-Loops

Ja, ein `delayed_gcode` darf sich am Ende seines Bodies selbst mit `UPDATE_DELAYED_GCODE ID=self DURATION=...` neu planen → periodischer Polling-Timer. Beispiel:

```ini
[delayed_gcode buffer_tick]
gcode:
    # ... Buffer-Zustand pruefen ...
    UPDATE_DELAYED_GCODE ID=buffer_tick DURATION=1
initial_duration: 1
```

Zum Stoppen: irgendwo `UPDATE_DELAYED_GCODE ID=buffer_tick DURATION=0` aufrufen.

Quelle: `klipper3d.org/Config_Reference.html#delayed_gcode`.

---

## gcode_macro

### Grundsyntax

```ini
[gcode_macro MY_MACRO]
description: optional
gcode:
    # Jinja2-Template
    M118 hello
```

Makro-Namen erscheinen im G-Code in Großschreibung (Klipper normalisiert).

### `variable_<name>:` — persistente Makro-Variablen

```ini
[gcode_macro BUFFER_STATE]
variable_sync_active: False
variable_last_direction: "idle"
gcode:
    M118 sync={printer["gcode_macro BUFFER_STATE"].sync_active}
```

- Gilt pro Klipper-Session (bis `FIRMWARE_RESTART` / Reboot).
- Variablennamen müssen klein geschrieben sein.
- Zugriff von außen: `printer["gcode_macro BUFFER_STATE"].sync_active`.

### `SET_GCODE_VARIABLE`

```
SET_GCODE_VARIABLE MACRO=<macro_name> VARIABLE=<name> VALUE=<python-literal>
```

- `VALUE` wird als Python-Literal geparst. Strings in Anführungszeichen: `VALUE="'idle'"` (äussere Anführung für G-Code-Parser, innere Anführung macht den String).
- Die Änderung wird erst nach dem Ende des aktuellen Makro-Durchlaufs wirksam — wichtig: "Macros are first evaluated in entirety and only then are the resulting commands executed."

### `params.X`-Zugriff

- Parameter werden in Upper-Case ausgewertet: Aufruf `MY_MACRO VAL=3` → im Body `{params.VAL}`.
- Werte sind immer Strings. Konvertieren mit `| int` / `| float`.
- `rawparams` liefert die ursprüngliche Parameter-Zeile inkl. Kommentaren.

### Jinja2-Features

- Kontrollfluss: `{% if ... %}{% elif %}{% else %}{% endif %}`, `{% for x in range(n) %}{% endfor %}`, `{% set var = ... %}`.
- Filter: `| int`, `| float`, `| round(n)`, `| default(x)`, `| abs`, `| string`, `| lower`, `| upper`.
- Konstrukte: `{% if params.DIRECTION | default('idle') | lower == 'feed' %}...{% endif %}`.
- Zugriff auf Printer-State: `printer.toolhead.position.x`, `printer.extruder.target`, `printer["gcode_button foo"].state`, etc.

### Default-Wert mit Parameter

```jinja
{% set dist = params.DIST|default(5)|float %}
G1 E{dist} F300
```

Quellen: `klipper3d.org/Command_Templates.html`, `klipper3d.org/G-Codes.html#gcode_macro`.

---

## Quellen

- [klipper3d.org Config Reference](https://www.klipper3d.org/Config_Reference.html)
- [klipper3d.org G-Codes](https://www.klipper3d.org/G-Codes.html)
- [klipper3d.org Command Templates](https://www.klipper3d.org/Command_Templates.html)
- [Klipper3d/klipper kinematics/extruder.py](https://github.com/Klipper3d/klipper/blob/master/klippy/kinematics/extruder.py)
