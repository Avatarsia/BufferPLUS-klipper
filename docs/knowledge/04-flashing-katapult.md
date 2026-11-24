# Katapult Bootloader + Klipper Flash fuer LLL Buffer Plus

> Referenz-Doku fuer Wiederholung / Support / Zweitgeraet. Der aktuell im Einsatz befindliche Buffer ist bereits geflasht. Inhalte extrahiert aus der Projekt-`README.md` und verifiziert gegen [Arksine/katapult](https://github.com/Arksine/katapult) sowie [klipper3d.org/Installation](https://www.klipper3d.org/Installation.html).

## STM32F072 MCU Grundlagen

- Flash-Groesse: 64 KB (F072C8) oder 128 KB (F072CB). Der LLL Buffer Plus verwendet laut Mellow den 128-KB-Varianten. Verifikation ueber Device-String: `usb-katapult_stm32f072xb_XXXXXX-if00` bzw. `usb-Klipper_stm32f072xb_XXXXXX-if00` — das `xb`-Suffix bestaetigt 128 KB.
- Externer 8 MHz Crystal (HSE) auf der Mellow-Platine.
- USB-DFU on-chip (Factory-Bootloader im System-Memory ab `0x1FFFC800`), Data-Pins PA11 (D-) und PA12 (D+).
- Katapult belegt die ersten 8 KiB des User-Flash (`0x08000000 - 0x08001FFF`), die Application (Klipper) startet ab 8 KiB Offset (`0x08002000`).

## Katapult bauen

Die Konfigurationsschritte sind 1:1 aus der lokalen `README.md` uebernommen (Abschnitt *Step 1.1: Build Katapult*) und gegen Katapult-Upstream plausibilisiert (Architektur/Processor/Clock/USB-Pins sind Standard-Optionen in `make menuconfig`).

```bash
cd ~
git clone https://github.com/Arksine/katapult
cd katapult
make menuconfig
```

**Katapult-Konfiguration fuer LLL Buffer Plus:**

| Option | Wert |
|---|---|
| Micro-controller Architecture | `STMicroelectronics STM32` |
| Processor model | `STM32F072` |
| Build Katapult deployment application | `Do Not build` |
| Clock Reference | `8 MHz crystal` |
| Communication interface | `USB (on PA11/PA12)` |
| Application start offset | `8KiB offset` |
| USB ids | Default belassen |
| Support bootloader entry on rapid double click | `[*]` aktiv |
| Enable bootloader entry on button (or gpio) state | nicht aktivieren |
| Enable Status LED | `[*]` aktiv |
| Status LED GPIO Pin | `PA8` |

```bash
make clean && make
```

Resultat: `~/katapult/out/katapult.bin`

## Katapult auf den Buffer flashen (Erstflash via DFU)

### DFU-Mode aktivieren

- **Methode 1 (BOOT-Button):** BOOT-Button halten, Reset druecken, BOOT loslassen.
- **Methode 2 (USB-Trigger):** USB trennen, BOOT halten, USB einstecken, BOOT loslassen.
- **Methode 3 (BOOT0-Jumper):** BOOT0 auf 3.3V bruecken, Reset druecken, Jumper entfernen.

### DFU-Mode verifizieren

```bash
lsusb | grep -i dfu
# Erwartet: Bus 00X Device YYY: ID 0483:df11 STMicroelectronics STM Device in DFU Mode
```

Falls nicht sichtbar:
```bash
sudo dfu-util -l
```

### Flash via dfu-util

```bash
sudo dfu-util -a 0 -D ~/katapult/out/katapult.bin \
  --dfuse-address 0x08000000:force:mass-erase:leave \
  -d 0483:df11
```

Erwartete Abschluss-Zeile: `File downloaded successfully`.

### Katapult-Geraet verifizieren

USB kurz trennen und wieder einstecken.

```bash
ls /dev/serial/by-id/
# Erwartet: usb-katapult_stm32f072xb_XXXXXX-if00
```

## Klipper bauen und ueber Katapult flashen

```bash
cd ~/klipper && make menuconfig
```

**Klipper-Konfiguration:**

| Option | Wert |
|---|---|
| Micro-controller Architecture | `STMicroelectronics STM32` |
| Processor model | `STM32F072` |
| Bootloader offset | `8KiB bootloader` |
| Clock Reference | `8 MHz crystal` |
| Communication interface | `USB (on PA11/PA12)` |

> **Kritisch:** Der Bootloader-Offset in Klipper MUSS mit dem Application-Start-Offset in Katapult uebereinstimmen (beide 8 KiB). Sonst springt Katapult beim Start ins Leere.

```bash
make clean && make
```

Resultat: `~/klipper/out/klipper.bin`

### Flash via flashtool.py

```bash
python3 ~/katapult/scripts/flashtool.py \
  -f ~/klipper/out/klipper.bin \
  -d /dev/serial/by-id/usb-katapult_stm32f072xb_XXXXXX-if00
```

### Alternative: `make flash`

```bash
cd ~/klipper
make flash FLASH_DEVICE=/dev/serial/by-id/usb-katapult_stm32f072xb_XXXXXX-if00
```

Erwartete Ausgabe (Auszug):
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

### Klipper-Geraet verifizieren

```bash
ls /dev/serial/by-id/
# Erwartet: usb-Klipper_stm32f072xb_XXXXXX-if00
```

Diese Device-ID wandert in `mellow-plus.cfg`:
```cfg
[mcu LLL_PLUS]
serial: /dev/serial/by-id/usb-Klipper_stm32f072xb_XXXXXX-if00
restart_method: command
```

## Firmware-Updates nach Erstflash

Dank Katapult kein BOOT-Jumper mehr noetig. Zwei Wege:

1. **Doppel-Klick auf Reset-Button** → Katapult wird fuer ca. 5 Sekunden aktiv, anschliessend Flash gegen die `usb-katapult_...`-Device-ID.
2. **Direkt gegen die Klipper-Device-ID** flashen — `flashtool.py` erkennt die laufende Klipper-Instanz, schickt ein Katapult-Request-Kommando und flasht automatisch.

```bash
# Variante 2 — direkter Update-Flow:
cd ~/klipper && make clean && make
python3 ~/katapult/scripts/flashtool.py \
  -f ~/klipper/out/klipper.bin \
  -d /dev/serial/by-id/usb-Klipper_stm32f072xb_XXXXXX-if00
```

## Fallback: Klipper ohne Katapult flashen

Wenn Katapult explizit nicht gewuenscht ist (z. B. fuer Debug-Builds):

**Klipper menuconfig:**
- Bootloader offset: `No bootloader`
- Rest wie oben (STM32F072, 8 MHz crystal, USB PA11/PA12).

**Flash via DFU:**
```bash
# Vorher in DFU-Mode bringen (siehe oben).
cd ~/klipper
make flash FLASH_DEVICE=0483:df11
```

Nachteil: Jedes kuenftige Update erfordert wieder manuellen DFU-Trigger.

## Troubleshooting

| Problem | Ursache | Fix |
|---|---|---|
| DFU-Geraet wird nicht erkannt | USB-Kabel ist Charge-only, falscher Port, BOOT nicht sauber | Datenkabel verwenden, anderen Port, BOOT-Sequenz wiederholen, `lsusb` ohne grep pruefen |
| `Cannot open DFU device` | Permission-Problem | `sudo dfu-util ...` oder udev-Rule fuer `0483:df11` |
| Katapult erscheint nicht nach Flash | Flash fehlerhaft, Offset-Mismatch, USB nicht re-enumeriert | 5-10 s warten, `dmesg | tail` pruefen, Katapult neu flashen |
| Klipper-Flash ueber Katapult bricht ab | Bootloader-Offset in Klipper != Katapult | `make menuconfig` im Klipper-Tree → `8KiB bootloader` zwingend |
| MCU nach Klipper-Flash nicht in `/dev/serial/by-id/` | Falsche Firmware oder Enumerations-Fehler | `dmesg | tail` pruefen, Klipper neu bauen + flashen |

## Quellen

- Lokale Projekt-`README.md` — primaere Quelle, enthaelt den verifizierten Flow des Geraete-Besitzers.
- [Arksine/katapult Upstream](https://github.com/Arksine/katapult) — bestaetigt: STM32F042/72 wird ueber DFU geflasht, `flashtool.py` mit `-d` fuer Serial-Device, `USB ids` konfigurierbar, menuconfig-Struktur (Architecture/Processor/Clock/Interface/Offset).
- [Klipper Installation Docs](https://www.klipper3d.org/Installation.html) — Hinweis auf `make menuconfig`-basierten Build, Verweis auf `config/`-Beispiele und `Bootloaders.html` fuer Offset-Details.
- STM32F072-Datenblatt (ST): Flash-Varianten C8/CB, USB-DFU-System-Bootloader, PA11/PA12 als Standard-USB-Pins.
