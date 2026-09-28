# Mellow LLL Filament Buffer Plus - Hardware-Referenz

Stand: 2026-04-21. Quellen: Avatarsia-Fork-README[^1], Fly3DTeam-Upstream-Firmware[^2],
`printer_data/config/mellow-plus.cfg` im Avatarsia-Fork[^3].

## Uebersicht

- Produktname: **Mellow LLL Filament Buffer Plus** (auch "LLL Plus Buffer")
- Hersteller: Mellow 3D (Fly3DTeam)
- Zweck: Aktiver Filament-Puffer mit Hall-Sensor-geregeltem Nachfoerder-Stepper.
  Haelt zwischen Spule/AMS und Extruder eine definierte Filament-Schlaufe,
  gleicht Zugkraefte aus und entlastet die Extruder-Kinematik.
- MCU: **STM32F072xB** (128 KiB Flash, ARM Cortex-M0), externer 8 MHz HSE-Crystal,
  USB-CDC auf **PA11 / PA12**.[^2][^4]
- Firmware-Stack upstream: C++ via PlatformIO, Variant `fly_buffer_f072c8` in der
  `platformio.ini` (STM32F072C8-Familie, bei Mainline-Klipper wird die xB-Variante
  mit 128 KiB Flash verwendet).[^2]
- Bootloader-Empfehlung (Avatarsia-Fork): **Katapult** (8 KiB App-Offset), danach
  Klipper-Firmware mit passendem Offset.[^1]

## Pin-Belegung (aus `mellow-plus.cfg` im Avatarsia-Fork verifiziert[^3])

Alle Pins sind MCU-Aliasname `LLL_PLUS:<Pin>` in der Klipper-Config.

**Hinweis zum Sensortyp:** Die Pin-Namen `HALL1/2/3` folgen der Upstream-
Konvention (Fly3DTeam/Buffer). Auf diesem Hardware-Sample sind es tatsaechlich
**optische Durchlichtschranken / Photo-Interrupter**, keine Hall-Effekt-Chips
(vom User anhand der verbauten Komponenten verifiziert). Das Signalverhalten
ist aber identisch zu Open-Collector-Halls: digital, aktiv LOW mit Pullup,
darum `^!` als Klipper-Pin-Praefix.

| Pin  | Funktion                       | Typ                          | Klipper-Praefix |
|------|--------------------------------|------------------------------|-----------------|
| PC13 | Stepper STEP                   | Output                       | (kein)          |
| PA7  | Stepper DIR                    | Output                       | (kein)          |
| PA6  | Stepper ENABLE (aktiv LOW)     | Output, invertiert           | `!`             |
| PB1  | TMC2208 UART                   | UART (Single-Wire)           | (kein)          |
| PB2  | HALL1 (Ueberlast-Notanschlag, ganz oben) | Input, Pullup + invertiert | `^!`   |
| PB3  | HALL2 (Voll-Schwelle, oben)    | Input, Pullup + invertiert   | `^!`            |
| PB4  | HALL3 (Leer-Schwelle, unten)   | Input, Pullup + invertiert   | `^!`            |
| PB7  | ENDSTOP3 (Filament-Einlauf)    | Input, Pullup + invertiert   | `^!`            |
| PB12 | Feed-Button                    | Input, Pullup + invertiert   | `^!`            |
| PB13 | Retract-Button                 | Input, Pullup + invertiert   | `^!`            |
| PA8  | Status-LED (Bootloader/Firmware) | Output                     | (im Fork nicht als `output_pin` definiert)[^5] |
| PA11 | USB D-                         | USB-CDC                      | -               |
| PA12 | USB D+                         | USB-CDC                      | -               |

## Mechanik

- Stepper: Pancake mit Planetengetriebe.
- `gear_ratio: 50:17` laut Config-Konvention im Fork-Umfeld (finale `rotation_distance`
  wird per 100-mm-Kalibrierung bestimmt und in der User-Config geschrieben).[^1]
- Drei Hall-Sensoren erfassen die Position des beweglichen Buffer-Halses ("neck"),
  der sich beim Befuellen nach oben bewegt und beim Verbrauch nach unten zieht.
- Filament-Pfad: Spule/AMS --> **ENDSTOP3 (Einlauf, PB7)** --> Buffer-Mechanik mit
  beweglichem Hals --> Extruder.

## Sensor-Semantik

**Autoritative Quelle:** `printer_data/config/lll.cfg`, Zeilen 260-268
(User hat die Anordnung "laut Foto" direkt auf seiner Hardware verifiziert).
Die Upstream-/Fork-READMEs[^1][^6] beschreiben eine **abweichende** Zuordnung
(HALL3 als "Initial-Fill komplett"/oben), die fuer dieses konkrete Geraet
**nicht zutrifft**. Siehe "Abweichung Upstream" unten.

Der Buffer-Hals bewegt sich beim Befuellen nach oben (mehr Filament im Puffer
-> Hals steigt) und beim Verbrauch nach unten (Extruder zieht Filament ab,
Hals folgt). Mit dieser Mechanik:

- **ENDSTOP3 (PB7) aktiv** = Filament am Einlauf eingelegt/erkannt.
- **HALL3 (PB4) aktiv** = Buffer-Hals an der untersten Position =
  **Leer-Schwelle**. Auto-Regelung beschleunigt den Feeder (kleinere
  `rotation_distance`), bis der Hals wieder nach oben geht.
- **HALL2 (PB3) aktiv** = Buffer-Hals an der oberen Arbeitsposition =
  **Voll-Schwelle**. Auto-Regelung drosselt den Feeder (groessere
  `rotation_distance`), bis der Hals wieder nach unten geht.
- **HALL1 (PB2) aktiv** = Buffer-Hals ganz oben, ueber HALL2 =
  **Ueberlast-Notanschlag**. Sync wird komplett getrennt und der Feeder
  wegen `overfill_lock` blockiert, bis der Hals physisch zurueckzieht.

### Abweichung Upstream

Die Avatarsia-Fork-README und die ThaatGuy-Zwischenfork-README dokumentieren
eine gegenlaeufige Zuordnung (HALL3 = Initial-Fill komplett, HALL2 als
Feed-Burst-Mittelpunkt). Vermutlich beschreibt diese Doku ein anderes
Wiring-Muster oder ein frueheres Hardware-Revision. Fuer die in diesem Repo
gepflegte Config (`lll.cfg`) und die Variante-2-Sync-Architektur gilt die
oben genannte Zuordnung.

### Invertierungs-Nuance (Klipper `^!`)

Die Hall-Sensoren und Buttons sind elektrisch als **Open-Drain / aktiv LOW mit
Pullup** verdrahtet. In der Klipper-Config wird das mit `^!` abgebildet:

- `^` = interner Pullup aktiv.
- `!` = logische Invertierung, sodass die in Klipper sichtbare Logik wieder
  "HIGH = aktiv" entspricht.

Fuer `[gcode_button]` bedeutet das konkret: Bei `pin: ^!PB12` ist die Klipper-
interne Bewertung invertiert. **Wenn der Button physisch gedrueckt ist (Schalter
schliesst gegen GND)**, feuert Klipper das `release_gcode` und `button.state ==
"RELEASED"`. Das `press_gcode` feuert beim physischen Loslassen mit `state ==
"PRESSED"`. Diese Semantik-Umkehr ist beim Schreiben der Button-Macros zu
beruecksichtigen - die physische Aktion entspricht dem Release-Event, nicht dem
Press-Event.

Gleiches gilt analog fuer die Hall-Sensoren, sofern sie via `gcode_button`
eingebunden werden; bei `filament_switch_sensor` ist das Verhalten unkritisch,
weil dort direkt der `[filament_switch_sensor].filament_detected`-Wert abgefragt
wird (Klipper meldet nach Invertierung sauber "detected = True" bei physischer
Aktivierung des Sensors).

## Button-Verhalten

- Feed-Button (PB12) und Retract-Button (PB13): beide `^!`, normally-open (NO).[^1]
- Gedrueckt halten = kontinuierliche Bewegung. Kurzer Klick = Einzelschritt
  (je nach User-Macro-Implementierung im Klipper-Config-Layer).
- Wegen `^!`: physisches Druecken => Klipper-Event `release_gcode` / `state ==
  "RELEASED"`. Diese Konvention wird in den Refactor-Macros (R-6, R-7) gezielt
  aufgegriffen.

## Status-LED

- Hardware-LED auf **PA8**, wird von Katapult und optional von der Klipper-
  Firmware als Heartbeat genutzt.[^1]
- In der aktuellen Avatarsia-`mellow-plus.cfg` ist kein `[output_pin]` oder
  `[led]`-Block auf PA8 definiert; die LED wird also nur durch Firmware/
  Bootloader bedient, nicht durch User-Macros.[^3][^5]

## Flashing / Bootloader-Kurzreferenz

- Katapult-Build: STM32F072, 8 MHz Crystal, USB auf PA11/PA12, 8 KiB App-Offset,
  Status-LED PA8.[^1]
- DFU-Mode: entweder **BOOT-Button halten** beim USB-Einstecken, oder **BOOT0
  per Jumper auf 3.3V** vor Reset.[^1]
- Klipper-Build: identischer 8 KiB Offset, Flashen ueber Katapult unter Angabe
  der Device-ID aus `/dev/serial/by-id/`.[^1]

## Offene Punkte / unverifiziert

- Sensortyp: **optische Durchlichtschranken / Photo-Interrupter** (vom User
  anhand der verbauten Komponenten verifiziert). Die Pin-Label `HALL1/2/3`
  sind aus Upstream-Kompatibilitaet uebernommen, obwohl die Sensorik nicht
  magnetisch ist. Exaktes Bauteil (z.B. TCST1103 / GP1A57HRJ00F / EE-SX*)
  nicht aus den Quellen extrahierbar; das Signalprofil (digital, aktiv LOW,
  Pullup) ist fuer die Klipper-Config identisch zu Hall-Open-Collector.
- MCU-Suffix: `platformio.ini` referenziert den Variant-Ordner `f072c8` (64 KiB
  Flash-Naming), Klipper nutzt `stm32f072xb` (128 KiB) - wahrscheinlich nutzt
  das Board einen F072CB, der die f072c8-Variant-Config teilt. **Nicht hart
  verifiziert**, Empfehlung: Board-Aufdruck pruefen vor Katapult-Build.
- `gear_ratio 50:17`: in `lll.cfg` Zeile 66 explizit so gesetzt (User-Config
  ist die autoritative Quelle).

## Quellen

[^1]: Avatarsia/BufferPLUS-klipper README, https://github.com/Avatarsia/BufferPLUS-klipper/blob/main/README.md
[^2]: Fly3DTeam/Buffer (Upstream-Firmware, C++/PlatformIO), https://github.com/Fly3DTeam/Buffer
[^3]: Avatarsia/BufferPLUS-klipper `printer_data/config/mellow-plus.cfg`, https://raw.githubusercontent.com/Avatarsia/BufferPLUS-klipper/main/printer_data/config/mellow-plus.cfg
[^4]: Fly3DTeam/Buffer `platformio.ini` (HSE_VALUE=8000000L, USBCON, USBD_USE_CDC), https://github.com/Fly3DTeam/Buffer/blob/main/platformio.ini
[^5]: `mellow-plus.cfg` enthaelt keinen `[output_pin]`-Block fuer PA8 - die LED
      wird ausschliesslich durch Firmware/Bootloader angesteuert.
[^6]: ThaatGuy-COTS/BufferPLUS-klipper README (Zwischen-Fork-Dokumentation),
      https://github.com/ThaatGuy-COTS/BufferPLUS-klipper/blob/main/README.md
