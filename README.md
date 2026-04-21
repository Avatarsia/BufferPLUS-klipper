# Klipper Configuration for Mellow LLL Buffer Plus

Complete Klipper configuration for the Mellow LLL Filament Plus Buffer with automatic filament feeding and buffer management.

> **Note:** This is the Klipper configuration. For the Buffer Plus firmware source code, see the [main repository README](../README.md).

# Revisions 
1/12/2026 - Updated config to use extra_stepper and force moves instead of the second extruder setup.  
            This avoids a few conflicts and allows the motor to be synced to the extruder  
            Added filament runout switch logic.  
            Can be enabled or disabled with Enable_Filament_Runout or Disable_Filament_Runout    

## Features

- ✅ **Automatic Buffer Control** - Fills buffer automatically when filament is detected
- ✅ **Smart Feed Bursts** - HALL2 sensor triggers small feed bursts during printing
- ✅ **Overfill Protection** - HALL1 prevents buffer from jamming into extruder
- ✅ **Manual Feed/Retract** - Physical buttons for manual filament loading
- ✅ **Filament Runout Detection** - Optional pause on filament runout

## Kalibrierung (nach Refactor-/Phase-2-Integration)

Die `printer_data/config/lll.cfg` nach Phase-2-Integration braucht einmalige
Einrichtung pro Geraet. Folgende 6 Schritte fuehren durch die Kalibrierung:

### Schritt 1: Pflicht-Variablen anpassen

In `[gcode_macro _FILAMENT_VARS]` diese vier Werte muessen auf das eigene
Setup angepasst werden, alle anderen sind sinnvolle Defaults:

- `variable_sync_rotation_distance`: Kalibrierter 1:1-Mitlauf Feeder/Extruder.
  Startwert `18.86` ist ein Platzhalter - unbedingt per Schritt 2 kalibrieren.
- `variable_load_fast_distance`: Weg vom Feeder bis Toolhead-Eingang [mm].
  Per Schritt 3 ausmessen.
- `variable_load_slow`: Weg Extruder-Eingang bis Nozzle-Spitze [mm].
  Typisch 40-60 mm Direct Drive, 60-100 mm Volcano. Per Schritt 4 ausmessen.
- `variable_unload_sync`: Wie `load_slow` (oder geringfuegig groesser).
  Muss Filament vollstaendig aus Heatbreak+Nozzle herausziehen.

### Schritt 2: `sync_rotation_distance` kalibrieren

**Wichtig:** `BUFFER_AUTO_ON` allein genuegt nicht - die +-20%-Modulation
ueber die Hall-Sensoren verfaelscht den Wert. Stattdessen:

1. Filament einlegen, Hotend auf Drucktemperatur heizen.
2. Konsole: `CALIBRATE_FEEDER_SYNC`. Feeder laeuft dann exakt 1:1 ohne
   Modulation.
3. Markierung am Filament kurz vor dem Feeder-Eingang anbringen.
4. Konsole: `G1 E100 F60`.
5. Nachmessen, wieviel Filament am Feeder durchgezogen wurde.
6. Anpassen: `neue_dist = alte_dist * (gemessene_mm / 100)`
7. Wert in `variable_sync_rotation_distance` eintragen.
8. Auch `rotation_distance` in `[extruder_stepper mellow]` auf denselben
   Wert setzen (Hardware-Default fuer Klipper-Start).
9. Klipper neu starten, Schritt 2 wiederholen bis Abweichung unter 1 mm.

### Schritt 3: `load_fast_distance` ausmessen

Genauer Weg vom Feeder bis zum Toolhead-Eingang per Taster-Messmodus:

1. Filament komplett aus dem System entfernen.
2. Frisches Filament in den Feeder-Eingang (buffer_entrance) stecken.
3. Konsole: `MEASURE_LOAD_START`.
4. Vorschub-Taster 1x druecken -> Foerderung startet.
5. Beobachten bis Filament-Spitze am Toolhead-Eingang erscheint.
6. Vorschub-Taster erneut druecken -> Foerderung stoppt, Ergebnis wird
   automatisch ausgegeben.
7. Ausgegebenen Wert als `variable_load_fast_distance` eintragen. Tipp:
   10-20 mm weniger als gemessen, damit das Filament nicht zu weit in
   den Extruder ragt vor Phase 2.

### Schritt 4: `load_slow` / `unload_sync` ausmessen

Mechanisch messen (Schieblehre oder Markierungen):
- Abstand Extruder-Klemme bis Nozzle-Spitze = `load_slow`.
- Gleicher oder etwas groesserer Wert fuer `unload_sync` (muss Filament
  vollstaendig aus der Nozzle ziehen).

### Schritt 5: Erstbefuellung testen

Nach Phase-2 startet Klipper beim Neustart **nicht** mehr automatisch
die Erstbefuellung — stattdessen wird nur der Sync aktiviert. Wuenscht
man eine Erstbefuellung:

1. Filament in den Feeder-Eingang stecken (wenn noch nicht geschehen).
2. Der `buffer_entrance`-`insert_gcode`-Event startet dann automatisch
   die Grip-Phase + Follow-Phase.
3. Bei bereits eingelegtem Filament ohne Neu-Insert-Event: manuell
   `FORCE_BUFFER_FILL` in der Konsole aufrufen.

### Schritt 6: `LOAD_FILAMENT` testen

Kompletter Lade-Zyklus mit sensorgesteuertem Ende:

1. Hotend auf Betriebstemperatur heizen.
2. Konsole: `LOAD_FILAMENT`.
3. Phase 1: Feeder foerdert `load_fast_distance` mm schnell (kein Sensor).
4. Phase 2: Feeder + Extruder synchron langsam durch Hotend
   (`load_slow` mm in 50-mm-Chunks).
5. Phase 3: Feeder fuellt Buffer bis HALL2 triggert (max `load_buffer_max`).
6. Bei Problemen mit Phase 2 (zu kurz/lang): `load_slow` anpassen.

### Optionale Feintuning-Variablen

- `variable_sync_modulation` (Default 0.20 = +-20%): staerkere Reaktion
  auf Hall-Sensoren, aber mehr Schlupf im Normalbetrieb.
- `variable_fast_speed` (Default 50 mm/s): Feeder-Schnellgeschwindigkeit.
  Bei Schlupf reduzieren.
- `variable_tip_cycles` / `tip_push` / `tip_pull` / `tip_speed`:
  Tip-Forming beim Entladen. TPU braucht andere Werte als PLA.
- `variable_initial_grip_speed` / `initial_grip_duration`: Erstbefuellung
  aggressiver/schwaecher.
- `variable_runout_pause`: `0` = externer Runout-Sensor uebernimmt Pause,
  Feeder laeuft 100 mm mit nach dem Triggern. `1` = direkt pausieren.
- `variable_display_status_enabled`: `1` = M117-Statusanzeige am Drucker-
  Display, `0` = stiller Betrieb (Debug-Off).

### Hilfsmakros

- `CALIBRATE_FEEDER_SYNC` - aktiviert Sync auf exakt nominaler
  rotation_distance (ohne Modulation), fuer Schritt 2.
- `MEASURE_LOAD_START` / `MEASURE_LOAD_STOP` - Toggle-Messmodus fuer
  Schritt 3.
- `FORCE_BUFFER_FILL` - manueller Start von Grip + Follow.
- `STOP_BUFFER_FILL` - alle Foerder-Loops sofort abbrechen.
- `BUFFER_AUTO_ON` - Sync aktivieren ohne Initial-Phase.
- `_STATE_DUMP` - Diagnostischer Dump aller Flags und Hall-States.

## Hardware Setup

### Sensor Configuration
- **ENDSTOP3 (PB7)**: Filament entrance sensor - detects when filament is loaded
- **HALL3 (PB4)**: Initial fill sensor - switches from continuous to burst mode
- **HALL2 (PB3)**: Primary buffer control - triggers feed bursts when neck extends
- **HALL1 (PB2)**: Overfill limiter - prevents buffer from over-filling

### Button Configuration
- **Feed Button (PB12)**: Manual continuous feed (hold to feed)
- **Retract Button (PB13)**: Manual continuous retract (hold to retract)

---

## Installation

### Step 1: Flash Katapult Bootloader (Recommended)

Katapult (formerly CanBoot) allows easy firmware updates without needing to press physical buttons or enter DFU mode.

#### 1.1: Build Katapult

```bash
cd ~
git clone https://github.com/Arksine/katapult
cd katapult
make menuconfig
```

**Katapult Configuration:**
- Micro-controller Architecture: `STMicroelectronics STM32`
- Processor model: `STM32F072`
- Build Katapult deployment application: `Do Not build`
- Clock Reference: `8 MHz crystal`
- Communication interface: `USB (on PA11/PA12)`
- Application start offset: `8KiB offset`
- USB ids: Leave default or customize
- Support bootloader entry on rapid double click: `[*]` ✓ (Enable this!)
- Enable bootloader entry on button (or gpio) state (Do not enable this)
- Enable Status LED `[*]`
- (PA8)   Status LED GPIO Pin

```bash
make clean
make
```

#### 1.2: Enter DFU Mode

The LLL Buffer Plus needs to be put into DFU (Device Firmware Update) mode:

**Method 1: Jumper BOOT0 to 3.3V**
1. Push and hold the boot button
2. Push the reset button
3. Release the boot button

**Method 2: BOOT Button (if accessible)**
1. Disconnect USB
2. Hold the **BOOT button** on the board
3. Connect USB while holding BOOT
4. Release BOOT button


#### 1.3: Verify DFU Mode

```bash
lsusb | grep DFU
```

You should see something like:
```
Bus 001 Device 015: ID 0483:df11 STMicroelectronics STM Device in DFU Mode
```

If not detected, try:
```bash
sudo dfu-util -l
```

#### 1.4: Flash Katapult

```bash
cd ~/katapult
sudo dfu-util -a 0 -D ~/katapult/out/katapult.bin --dfuse-address 0x08000000:force:mass-erase:leave -d 0483:df11
```

You should see output ending with:
```
File downloaded successfully
```

#### 1.5: Verify Katapult

Disconnect and reconnect USB. Check for Katapult device:

```bash
ls /dev/serial/by-id/
```

You should see something like:
```
usb-katapult_stm32f072xb_XXXXXX-if00
```

---

### Step 2: Build and Flash Klipper Firmware

#### 2.1: Build Klipper

```bash
cd ~/klipper
make menuconfig
```

**Klipper Configuration:**
- Micro-controller Architecture: `STMicroelectronics STM32`
- Processor model: `STM32F072`
- Bootloader offset: `8KiB bootloader` (for Katapult)
- Clock Reference: `8 MHz crystal`
- Communication interface: `USB (on PA11/PA12)`

**Important:** The bootloader offset MUST match what you set in Katapult (8KiB)!

```bash
make clean
make
```

#### 2.2: Flash Klipper via Katapult

Find your device ID:
```bash
ls /dev/serial/by-id/
```

Flash using Katapult's flashtool:
```bash
python3 ~/katapult/scripts/flashtool.py -f ~/klipper/out/klipper.bin -d /dev/serial/by-id/usb-katapult_stm32f072xb_XXXXXX-if00
```

Or using `make flash`:
```bash
make flash FLASH_DEVICE=/dev/serial/by-id/usb-katapult_stm32f072xb_XXXXXX-if00
```

You should see:
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

#### 2.3: Verify Klipper

Disconnect and reconnect USB. Check the device ID changed:

```bash
ls /dev/serial/by-id/
```

You should now see:
```
usb-Klipper_stm32f072xb_XXXXXX-if00
```

---

### Step 3: Configure Klipper

#### 3.1: Copy Configuration File

```bash
cp mellow.cfg ~/printer_data/config/
```

#### 3.2: Update printer.cfg

Add to your main `printer.cfg`:
```cfg
[include mellow.cfg]
```

#### 3.3: Update MCU Serial ID

Edit `mellow.cfg` and update the serial path:

```cfg
[mcu LLL_PLUS]
serial: /dev/serial/by-id/usb-Klipper_stm32f072xb_XXXXXX-if00
restart_method: command
```

Replace `XXXXXX` with your actual device ID from Step 2.3.

#### 3.4: Restart Klipper

```
FIRMWARE_RESTART
```

Check the Klipper web interface - you should see the LLL_PLUS MCU connected!

---

### Alternative: Flash Klipper Without Katapult

If you prefer not to use Katapult, you can flash Klipper directly:

#### Build Klipper (No Bootloader)

```bash
cd ~/klipper
make menuconfig
```

**Settings:**
- Micro-controller Architecture: `STMicroelectronics STM32`
- Processor model: `STM32F072`
- Bootloader offset: `No bootloader`
- Clock Reference: `8 MHz crystal`
- Communication interface: `USB (on PA11/PA12)`

```bash
make clean
make
```

#### Flash via DFU

1. Enter DFU mode (see Step 1.2)
2. Flash:
   ```bash
   make flash FLASH_DEVICE=0483:df11
   ```

> **Note:** Without Katapult, future firmware updates will require entering DFU mode manually each time.

---

## How It Works

### Initial Loading
1. Insert filament into entrance sensor (ENDSTOP3/PB7)
2. Buffer starts **continuous feeding** automatically
3. When neck reaches top (HALL3 triggers), switches to **burst mode**

### During Printing
1. Printer pulls filament → Buffer neck extends
2. When neck reaches mid-point (HALL2 releases) → **15mm feed burst**
3. Neck retracts back into housing
4. Repeat as needed

### Overfill Protection
1. If buffer overfills and neck extends too far (HALL1 releases)
2. **Auto-feed pauses** until neck retracts
3. Prevents jamming against extruder

---

## Configuration Tuning

### Adjust Feed Burst Amount
Change the burst size in `_BUFFER_FEED_BURST` macro:
```cfg
[gcode_macro _BUFFER_FEED_BURST]
gcode:
    {% if printer["gcode_macro _BUFFER_AUTO_CONTROL"].overfill_lock == 0 %}
        ACTIVATE_EXTRUDER EXTRUDER=extruder1
        M83
        G1 E15 F3000  # ← Change E15 to desired burst amount (mm)
        M118 Buffer: Feed burst complete
    {% endif %}
```

### Adjust Feed Speed
Change feed/retract speed (currently 3000 mm/min = 50 mm/s):
```cfg
G1 E10 F3000  # Change F3000 to desired speed (mm/min)
```

Common speeds:
- `F1800` = 30 mm/s (slower, more reliable)
- `F3000` = 50 mm/s (default)
- `F6000` = 100 mm/s (faster, may skip)

### Motor Current
Adjust TMC2208 current if motor is too weak or overheating:
```cfg
[tmc2208 extruder1]
uart_pin: LLL_PLUS:PB1
run_current: 0.35  # Increase up to 0.5 if motor skips, decrease to 0.25 if overheating
stealthchop_threshold: 999999
```

### Rotation Distance Calibration

To calibrate your buffer motor for accurate feeding:

1. **Mark the filament** 120mm from the entrance sensor
2. **Heat your hotend** (if min_extrude_temp is set)
3. **Activate the buffer extruder:**
   ```
   ACTIVATE_EXTRUDER EXTRUDER=extruder1
   ```
4. **Feed 100mm:**
   ```
   M83
   G1 E100 F300
   ```
5. **Measure** the actual distance the mark moved
6. **Calculate new rotation distance:**
   ```
   new_rotation_distance = current_rotation_distance * (100 / actual_distance_moved)
   ```
   
   Example: If mark moved 95mm instead of 100mm:
   ```
   new_rotation_distance = 18.86 * (100 / 95) = 19.85
   ```

7. **Update config:**
   ```cfg
   [extruder1]
   rotation_distance: 19.85  # Your calculated value
   ```

8. **Restart and test again** until accurate

---

## Troubleshooting

### Flashing Issues

**DFU device not detected:**
- Check USB cable (must be data cable, not charge-only)
- Try different USB port
- Check `lsusb` without grep to see all devices
- Verify BOOT0 is properly jumpered to 3.3V
- Try both BOOT button methods

**"Cannot open DFU device":**
```bash
sudo dfu-util -a 0 -D ~/katapult/out/katapult.bin --dfuse-address 0x08000000:force:mass-erase:leave -d 0483:df11
```
Run with `sudo` if permission denied.

**Katapult not appearing after flash:**
- Disconnect and reconnect USB
- Wait 5-10 seconds
- Check `dmesg | tail` for USB events
- Reflash Katapult - it may not have written correctly

**Klipper flash fails via Katapult:**
- Verify bootloader offset matches (8KiB in both Katapult and Klipper)
- Try entering Katapult manually: Double-tap reset button quickly
- Reflash Katapult and try again

### Buffer Operation Issues

**Buffer feeds continuously and won't stop:**
- Check HALL3 sensor is working: `QUERY_ENDSTOPS`
- Verify neck can physically reach HALL3 when extended
- Check sensor wiring and polarity
- Look for "HALL3 TRIGGERED" message in console

**HALL2 bursts happen too frequently:**
- Increase burst amount (E15 → E20 or E25)
- Check reverse bowden tube tension
- Verify printer is actually consuming filament

**Buffer overfills (HALL1 warning):**
- Decrease burst amount (E15 → E10)
- Check that printer is pulling filament from buffer
- Verify no clogs in bowden tube
- Check extruder is actually feeding

**Manual buttons don't work:**
- Verify button wiring to PB12 (feed) and PB13 (retract)
- Check console for "button pressed/released" messages
- Ensure buttons are wired normally-open (NO)
- Test with `QUERY_ENDSTOPS` while pressing

**MCU not detected after flashing Klipper:**
- Verify Klipper firmware is flashed (not Arduino or Katapult)
- Check USB connection
- Run `ls /dev/serial/by-id/` to find device
- Check `dmesg | tail` for USB enumeration errors
- Reflash Klipper firmware

**"Option 'step_pin' is not valid in section 'extruder X'":**
- Ensure section is named `[extruder1]` not `[extruder filament_buffer]`
- Klipper only supports numbered extruders: `extruder`, `extruder1`, `extruder2`, etc.

**TMC UART errors:**
- Verify UART pin is correct: `uart_pin: LLL_PLUS:PB1`
- Check TMC2208 is properly seated
- Verify run_current is not too low (minimum ~0.2)

---

## Updating Firmware (with Katapult)

Once Katapult is installed, updating Klipper is easy:

1. **Rebuild Klipper:**
   ```bash
   cd ~/klipper
   make clean
   make
   ```

2. **Flash via Katapult:**
   ```bash
   python3 ~/katapult/scripts/flashtool.py -f ~/klipper/out/klipper.bin -d /dev/serial/by-id/usb-Klipper_stm32f072xb_XXXXXX-if00
   ```

3. **Or use double-tap reset:**
   - Quickly press reset button twice
   - Device enters Katapult mode for 5 seconds
   - Flash using the Katapult device ID

No need to open the case or press BOOT buttons! 🎉

---

## Credits

Klipper configuration developed by [@ss1gohan13](https://github.com/ss1gohan13) for the Mellow LLL Filament Plus Buffer.

Hardware and original firmware by [Mellow 3D](https://github.com/mellow-3d).

Special thanks to:
- James on the Klipper Discord
- Ian on the Klipper Discord
- [Arksine](https://github.com/Arksine) for Katapult bootloader
- [Klipper](https://github.com/Klipper3d/klipper) team

## License

MIT License - Feel free to use and modify!
