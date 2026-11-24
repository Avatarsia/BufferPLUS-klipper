# Buffer-Steuerungs-Architekturen — Vergleich

## Motivation
Für den Mellow LLL Buffer Plus gibt es drei konzeptionell unterschiedliche Steuerungen. Diese Datei begründet, warum im Projekt Variante 2 (Sync-Feedback) gewählt wurde und welche Eigenschaften die Alternativen haben.

## Variante 0: Original-C++-Firmware (Fly3DTeam/Buffer)
- Autonom auf dem STM32, Klipper weiß nichts vom Buffer
- Feedback-Loop direkt auf dem MCU, keine Host-Rundreise
- **Vorteil:** Latenz sehr niedrig, Host-unabhängig, läuft auch mit Marlin-Host
- **Nachteil:** keine Sync-Kopplung zwischen Hauptextruder und Feeder, Klipper kann den Flow nicht beeinflussen, keine Integration in Makros/Print-Start/End

## Variante 1: Klipper-Config mit FORCE_MOVE-Bursts (mellow-plus.cfg, alte Config)
- Host-basiert: `gcode_button`-Events triggern `delayed_gcode`-Loops mit `FORCE_MOVE`
- Jeder Burst führt zu `toolhead.flush_step_generation()` → Druckkopf pausiert kurz
- **Vorteil:** Code sehr einfach verständlich, kleine Config (~225 Zeilen)
- **Nachteil:** **Pause-Artefakte während des Drucks** — für Klipper-Einsatz am Produktionsdrucker ein No-Go (vom User explizit abgelehnt)

## Variante 2: Sync-Feedback über rotation_distance (lll.cfg, aktuelle Config)
- Feeder-Stepper permanent per `SYNC_EXTRUDER_MOTION EXTRUDER=mellow MOTION_QUEUE=extruder` an den Hauptextruder gekoppelt
- Hall-Events ändern nur `rotation_distance` via `SET_EXTRUDER_ROTATION_DISTANCE`
  - HALL3 aktiv (Buffer leer) → rotation_distance **kleiner** → mehr Feeder-Drehungen pro Extruder-mm → Buffer füllt sich
  - HALL2 aktiv (Buffer voll) → rotation_distance **größer** → weniger Feeder-Drehungen → Buffer leert sich
  - HALL1 aktiv (Überlast) → Sync OFF + overfill_lock
- Modulation-Tiefe: `±sync_modulation` (z.B. ±20%)
- **Vorteil:** druckpausenfrei während Regelbetrieb, glatte Regelkurve
- **Nachteil:** `FORCE_MOVE` nur noch außerhalb des Drucks erlaubt (Initial-Grip/Follow, manuelle Taster, LOAD/UNLOAD Phase 1+3). Komplexere State-Machine (Lockout-Flags, Initial-Grip/Follow).

## Vergleichstabelle

| Kriterium | Variante 0 (C++) | Variante 1 (Bursts) | Variante 2 (Sync) |
|---|---|---|---|
| Druck-Pausen | — | ja | **nein** |
| Host-Abhängigkeit | keine | ja | ja |
| Sync zum Hauptextruder | nein | nein | **ja** |
| Reaktionszeit | niedrig (µs) | mittel (ms) | mittel (ms) |
| Anpassbarkeit via Klipper | keine | hoch | hoch |
| Config-Komplexität | — | niedrig (~225 Zeilen) | hoch (~736 Zeilen) |

## Warum Variante 2 gewählt wurde
Der User druckt regelmäßig und lange. Druck-Pausen bei jedem Buffer-Feed (Variante 1) sind nicht akzeptabel. Variante 0 würde Klipper-Features (Runout-Pause, PRINT_START-Integration, Makro-Parameter) komplett aushebeln. Variante 2 ist der einzige Weg, der beides verbindet: Klipper-Integration und durchgehenden Druck.

## Happy-Hare als Referenz
Happy-Hare ist ein MMU/ERCF-System, das ebenfalls `SYNC_EXTRUDER_MOTION` einsetzt. Unser Design ist von HH inspiriert, aber wir brauchen keine Tool-Wechsel-Logik, kein Gate-Management, keine Encoder-Feedback-Loops. HH ist viel umfangreicher; unsere Config ist eine schmale Teilmenge desselben API-Vokabulars.

## Quellen
- [Fly3DTeam/Buffer Upstream](https://github.com/Fly3DTeam/Buffer)
- [Happy-Hare Projekt](https://github.com/moggieuk/Happy-Hare) (zum Vergleich)
- Lokale Configs: `printer_data/config/mellow-plus.cfg` vs. `printer_data/config/lll.cfg`
