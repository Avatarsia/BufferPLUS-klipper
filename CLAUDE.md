# LLL Plus Buffer Refactor

Klipper-Python-Extension fuer den Mellow LLL Plus Filament Buffer. Refactor-Ordner mit dem aktuellen Stand des Plugins.

## Projekt-Identitaet

- **Repository:** `Avatarsia/BufferPLUS-klipper` (Fork von `ThaatGuy-COTS/BufferPLUS-klipper`)
- **Hauptbranch:** `refactor/cleanup-all-phases`
- **Funktion:** Buffer-Feeder laeuft auf eigener Move-Queue, fuettert waehrend des Drucks parallel nach
- **Beispielkonfig:** `lll.cfg` (mitgeliefert, primaere Benutzerkonfig)

## Tech Stack

- Python 3.x (Klipper-Extension via `klipper_extras/`)
- Klipper Mux-Command Pattern (`register_mux_command`, NICHT `register_command`)
- HALL-Sensor (3-Stage: HALL1/HALL2/HALL3) als Buffer-Fuell-Indikator
- Pytest fuer Unit-Tests (`tests/`, `pytest.ini`)
- Bash-Installer (`install.sh`, `update.sh`)

## Verzeichnisstruktur

```
LLL-Plus-Buffer-refactor/
  klipper_extras/        -- buffer_feeder.py, Klipper-Plugin-Code
  lll.cfg                -- Beispiel-/Default-Klipper-Config
  printer_data/          -- Test-Drucker-Configs
  docs/                  -- Architektur-Dokumentation
  tests/                 -- Pytest-Unit-Tests
  install.sh / update.sh -- Klipper-Side Installer-Scripts
```

## Konventionen

- Klipper-Mux-Commands: IMMER `register_mux_command` verwenden, nicht `register_command` (Naming-Konflikt mit anderen Extensions)
- HALL-Sensor-Sicherheits-Logik niemals umgehen — bei HALL3-Stall sofort stoppen
- Idle-Stream-Suppression beachten: in flush-Callback-Pfaden Watchdog notwendig (siehe NOT-TO-DO)
- Phasen-Konstanten bei Renames: alle Aufrufer per `grep` finden, nicht nur die Definition aendern
- Vor PR-Merge: pytest gruen, Review gruen (`/code-review`), Klipper-Sim-Test gruen (siehe `twin-printer/`)

## Verwandte Projekte

- **`twin-printer/`** (`Projekte\twin-printer\`) — Klipper-Simulator fuer headless Hardware-Tests
- **`Avatarsia/BufferPLUS-klipper`** — Upstream-Fork, Sync via `git remote upstream`

## Workflow

1. Code-Change in `klipper_extras/buffer_feeder.py`
2. `pytest tests/` gruen
3. `install.sh` auf Test-Drucker oder via `twin-printer/` simulieren
4. Review via `/code-review` (alternativ codex:rescue)
5. Commit + Push auf eigene Branch, PR an `refactor/cleanup-all-phases`
6. Erst nach Multi-Round-Verifikation merge

## Bekannte Themen

Siehe `NOT-TO-DO.md` — aktuell Schwerpunkt auf Crash-Vermeidung bei hohen Speeds (C-cont-Hotfix-Linie), HALL-Race-Conditions, Step-Generator-Race im Flush-Callback.
