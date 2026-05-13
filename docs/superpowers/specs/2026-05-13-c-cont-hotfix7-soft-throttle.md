# C-cont Hotfix 7 — Soft-Throttle (Verbrauchskonforme Feeder-Geschwindigkeit)

**Datum:** 2026-05-13
**Branch-Basis:** `feature/c-cont-streaming` HEAD (Hotfix 6, MD5 `f18a28ef290784aaf8a24ab046fdabb3`)
**Status:** Spec — pending User-Approval vor Phase 2 (Plan)
**Vorgänger-Spec:** `2026-05-13-high-flow-buffer-architecture.md` (allgemeine C-cont Vision)

---

## 1. Problem-Statement

### Hardware-Crash 2026-05-13
Feedertest.gcode Druck, reine Druckzeit 4min 11sec, Crash:
```
Z.104571: b'stepcompress o=0 i=0 c=12 a=0: Invalid sequence'
Z.104572: b"Error in syncemitter 'mellow' step generation"
Z.104573-79: Exception in flush_handler → mcu.error: Internal error in stepcompress
Z.104580: Transition to shutdown state
```
Hotfix 6 (HALL3 hardcoded 30 mm/s, HALL2 → 0, MIN_FLOOR=15 vel-Skip) lief seit Boot.

### Beobachtetes Pattern direkt vor Crash
80 Sekunden vor dem Crash: **16+ HALL1-OVERFLOW-Zyklen** (alle ~2s ein voller AUTO→OVERFLOW→IDLE→AUTO).
Letzte 5 Sekunden (Z.104560 – 104570):
```
target_speed-Sequenz: 30 → 0 → 0 → 0 → 30 → 18.7 → CRASH
HALL-State-Sequenz:    H3:on → off → off → off → H3:on → off → CRASH
tracker_vel:           8.7 → 8.0 → 11.5 → 7.5 → 7.2 → 17.0 mm/s
```

### Hardware-Geometrie (User-vermessen 2026-05-13)
- **Sensor-Typ:** optische Lichtschranken (KEINE Hall-Magnete trotz Codename `HALL`)
- **HALL3 ↔ HALL2 Abstand:** ~12.8 mm
- **HALL2 ↔ HALL1 Abstand:** ~3.6 mm
- **Arm-Auslöser (Fahne):** 3–4 mm Länge
- **Mechanischer Hebel:** ~2:1 (5mm Filament-Push → 2.5mm Arm-Deflektion via Schlaufe)
- **Voller Hub HALL3-Zone → HALL1:** 16.4 mm = entspricht ~32.8mm Filament-Push

### Direkte Beobachtung
Z.104017 – 104020 (Erst-OVERFLOW nach Print-Start):
```
t=0:   buffer_metrics: H3:on H2:off H1:off  vel=3.2  target=30  (HALL3-Bouncing)
t=+1s: buffer_metrics: H3:off H2:off H1:off vel=3.4  target=0   (Zwischenzone, Submit pausiert)
t=+2s: HALL1 OVERFLOW
       buffer_metrics: H3:off H2:on  H1:off vel=3.4  target=0   (HALL2 erst NACH dem HALL1-Edge)
```

→ Arm bewegte sich **trotz target=0** über die volle Hub-Strecke. Energie kam aus dem vorhergehenden 5mm-Chunk @ 30 mm/s. HALL2 wurde im Polling übersprungen (Auslöser-Geometrie + Polling-Aliasing).

---

## 2. Wurzel-Ursachen

### Wurzel 1 — Mechanische Schwung-Energie (PRIMÄR)
`interrupt_chunk_mm = 5` Filament-Push @ `feed_speed = 30 mm/s` injiziert pro Submit:
- 5 mm Filament-Energie → 2.5 mm Arm-Deflektion via Schlaufen-Hebel
- ~167 ms Chunk-Dauer mit accel/decel-Trapezoid (Schwung wird mitgegeben)
- Arm bewegt sich mechanisch weiter nach Submit-Ende (Trägheit + verbleibender Filament-Druck)

Bei nur **3.6mm HALL2→HALL1 Sicherheitsmarge** und Software-Reaktionszeit (Flush-Tick 250-500ms) ist die Software **strukturell zu langsam**, um den Arm vor HALL1 zu stoppen, sobald HALL3 freigegeben wurde.

### Wurzel 2 — Speed-Hopping triggert State-Drift (SEKUNDÄR)
Hotfix6's Logik (`HALL3 → 30`, Zwischenzone vel<15 → 0, vel≥15 → vel*1.10) erzeugt im normalen Pendel-Betrieb rapide Speed-Wechsel: `30 → 0 → 30 → 18.7 → 0 → 30...`. Jede target=0-Phase pausiert die Pipeline, jeder Wiederstart erzeugt einen neuen forced_t0-Anchor. Über 16+ OVERFLOW-Zyklen sammelt sich State-Drift (`_last_move_end_time`, `_stepcompress_primed`, `_continuous_feed`-Flags) bis zum Race.

### Wurzel 3 — HALL1-Storm belastet State-Machine (TERTIÄR)
Jeder HALL1-Trigger durchläuft: `_enter_overflow` → `_halt_motion` → `OVERFLOW → IDLE → AUTO` → `_resume_after_overflow`. Bei 16+ Zyklen pro Minute akkumulieren sich Race-Chancen in genau diesem Pfad. Patches P7-71, P7-72, P7-77B haben drei bekannte stale-anchor-Pfade abgesichert, aber unter Hotfix6's neuem Speed-Switching tritt offensichtlich ein vierter Pfad zutage.

---

## 3. Optionen-Vergleich

### Option A — Kleinere Chunks (`interrupt_chunk_mm`: 5 → 2)
Reduziert Energie pro Submit. Aber: bei niedrigen Chunk-Distanzen dominiert accel/decel-Phase (Triangular-Profile) → tatsächliche max-Geschwindigkeit sinkt unter feed_speed. Höhere Pipeline-Last (mehr Submits/sec → mehr State-Transitions → mehr Race-Chancen). Adressiert nur Wurzel 1, nicht Wurzel 2/3.

### Option B — Geschwindigkeits-Rampe (HALL3:on → 10 → 20 → 30 mm/s über 1s)
Begrenzt Schwung-Aufbau in der HALL3→HALL2-Annäherung. Aber: erfordert neue Zustands-Variablen (`_hall_empty_start_time`), kompliziert die Modulator-Logik, braucht Schwellenwert-Tuning, bei hochflussigem Druck kann der Buffer leerlaufen bevor die Rampe hochfährt.

### Option C — Soft-Throttle (verbrauchskonform) — **GEWÄHLT**
Feeder-Geschwindigkeit skaliert mit Extruder-Verbrauch:
```
HALL3:on, vel ≥ MIN_FLOOR:    target = max(vel * 1.5, MIN_FLOOR)    # Auffüllen mit 50% Margin
HALL3:on, vel < MIN_FLOOR:    target = MIN_FLOOR                    # Initial-Boost / Idle
Zwischenzone, vel ≥ MIN_FLOOR: target = max(vel * 1.10, MIN_FLOOR)  # bisheriges Hotfix6
Zwischenzone, vel < MIN_FLOOR: target = 0                           # bisheriges Hotfix6 (skip-Pfad)
HALL2:on:                     target = 0                            # bisheriges Hotfix5
HALL1:on:                     OVERFLOW (Notbremse)                  # unverändert
```

**Begründung:**
- Adressiert Wurzel 1 strukturell: bei langsamem Print (vel=10 mm/s) schiebt Feeder nur 15 mm/s statt fixe 30 → halbe Schwung-Energie pro Chunk → Arm bremst im HALL2-Sicherheitsfenster ab
- Reduziert Wurzel 2: target springt nicht mehr 0 ↔ 30, sondern fließt sanft mit `vel`. Speed-Hopping-Amplitude wird kleiner
- Reduziert Wurzel 3 als Konsequenz von Wurzel 1: weniger HALL1-Trigger → weniger OVERFLOW-Zyklen → weniger State-Drift-Chancen
- Verwendet bestehende Infrastruktur (`velocity_tracker.is_ready()` + `velocity_tracker.get_velocity()`)
- Konsistent über alle Hall-Zustände (kein Hardcoded-Sprung mehr)

**Trade-Off:**
- Bei Print-Start ist `velocity_tracker` noch nicht ready → fällt auf `MIN_FLOOR=15` zurück (akzeptabel für initial Buffer-Auffüllung)
- Bei Filament-Wechsel mit leerem Buffer: 15 mm/s initial ist langsam, Buffer-Voll-Befüllung dauert ~30s. Mitigation: Buffer-Wiederbefüllung läuft solange HALL3 stable → bei stable HALL3 ohne extruder-vel könnte ein zweiter Threshold den Boost erlauben (für spätere Iteration, nicht in Hotfix7)

---

## 4. Konkrete Code-Änderung

**Datei:** `klipper_extras/buffer_feeder.py`
**Funktion:** `SpeedModulator._compute_target_feed_speed()` (Z. 2731-2753)

**Aktuell (Hotfix 6):**
```python
if self.hall_overflow:
    return 0.0
if self.hall_empty:
    return 30.0  # Hotfix6: HALL3 hardcoded
if not self.velocity_tracker.is_ready():
    return self.feed_speed
vel = self.velocity_tracker.get_velocity()
if vel == 0:
    return self.feed_speed
if self.hall_full:
    return 0.0  # Hotfix5: HALL2 → 0
proposed = vel * 1.10
if proposed < self.MIN_FLOOR:
    return 0.0  # MIN_FLOOR-Skip
return proposed
```

**Hotfix 7 (Soft-Throttle):**
```python
# C-cont Hotfix7 (Hardware-Crash 2026-05-13 klippy.log Z.104571,
# c=12 i=0 Invalid sequence nach 16+ HALL1-OVERFLOW-Zyklen in 80s).
# Wurzel: Hardware-Geometrie (HALL2→HALL1 nur 3.6mm bei 12.8mm
# HALL3→HALL2) + 5mm-Chunk @ 30 mm/s Schwung-Energie => Arm
# überschießt HALL2 systematisch, HALL1-Storm belastet State-
# Machine bis Stepcompress-Race. Soft-Throttle koppelt
# feed_speed an extruder_velocity → kein fixer 30 mm/s-Schub
# mehr, Schwung-Amplitude skaliert mit Print-Bedarf.
MIN_FLOOR = 15.0  # mm/s (von Hotfix3 übernommen)

if self.hall_overflow:
    return 0.0
if self.hall_full:
    return 0.0  # Hotfix5

vel_ready = self.velocity_tracker.is_ready()
vel = self.velocity_tracker.get_velocity() if vel_ready else 0.0

if self.hall_empty:
    # HALL3:on → Buffer leer, Auffüllen mit 50% Margin über Verbrauch
    if not vel_ready or vel < MIN_FLOOR:
        return MIN_FLOOR  # Initial-Boost / Idle-Fall
    return min(max(vel * 1.5, MIN_FLOOR), self.feed_speed)

# Zwischenzone (kein HALL aktiv)
if not vel_ready or vel < MIN_FLOOR:
    return 0.0
return min(vel * 1.10, self.feed_speed)
```

**Schlüssel-Änderungen vs Hotfix 6:**
1. **HALL3:on → `max(vel*1.5, MIN_FLOOR)`** statt fix 30 mm/s. Cap auf `self.feed_speed` (30) als Obergrenze (Sicherheit).
2. **`min(..., self.feed_speed)`** in beiden Zonen: Soft-Cap auf konfiguriertes Maximum (verhindert vel*1.5 > 30 bei sehr schnellen Drucken).
3. **Initial-Boost-Pfad:** bei `vel_ready=False` (Print-Start, velocity_tracker noch sammelnd) UND HALL3:on → MIN_FLOOR=15.
4. **Reihenfolge:** `hall_full`-Check vor velocity-Berechnung (Performance, defensive Coding).

---

## 5. Erwartete Hardware-Effekte

**Bei Druck mit ~20 mm/s extruder_velocity (typisch):**
- Vorher Hotfix 6: HALL3:on → 30 mm/s fix. 5mm @ 30 = 167ms Chunk, ~2.5mm Arm-Push pro Chunk. Bei 3 Chunks/sec wäre der Arm in ~3s an HALL1.
- Mit Hotfix 7: HALL3:on → 20 * 1.5 = 30 mm/s, gleicher Effekt. Aber sobald Buffer in Zwischenzone (~67% der Zeit), springt target zu vel*1.1 = 22 mm/s und kein Schub-Stop mehr. Übergang HALL3 → Zwischenzone wird stetiger, weniger Speed-Hopping.

**Bei Druck mit ~5 mm/s extruder_velocity (low-flow):**
- Vorher: HALL3:on → 30 mm/s fix. Massiver Overshoot, 5mm Chunk = 6× extruder-Verbrauch. Buffer überfüllt sofort.
- Mit Hotfix 7: HALL3:on → max(5 * 1.5, 15) = 15 mm/s. Schwung-Energie ~halbiert. Arm sollte vor HALL1 in HALL2-Bereich abbremsen.

**Bei Druck-Start (vel_ready=False):**
- Beide: 15 mm/s Initial. Buffer-Wiederbefüllung läuft eine kurze Anlauf-Phase, bis velocity_tracker primt.

---

## 6. Verbleibende Risiken

1. **Wurzel 2/3 nicht vollständig adressiert:** Wenn HALL1-Storm in seltenen Edge-Cases doch noch auftritt (z.B. ruckartige Print-Pausen mit gefülltem Buffer), kann State-Drift weiterhin zur Stepcompress-Race führen. Defense-in-Depth-Patches (Hotfix 8+) bleiben für später möglich.
2. **`velocity_tracker`-Tuning:** Wenn `velocity_tracker` mit zu langer Mittelungs-Zeit läuft, hinkt `vel` hinter realem Bedarf hinterher → Buffer könnte leerlaufen bei schnellen Print-Geschwindigkeits-Sprüngen. Aktueller Tracker-Code (in P7-78+ unverändert) ist hardware-validiert.
3. **MIN_FLOOR = 15:** Bei sehr langsamem Print (3 mm/s, z.B. Linsen-Druck) übersteigt Soft-Throttle den Bedarf um 5× → Buffer könnte trotzdem überfüllen, aber langsamer als bei Hotfix 6 (15 statt 30 mm/s). Acceptable für ersten Hotfix7-Test.

---

## 7. Test-Strategie (für Phase 2-Plan)

### TDD-Tests (RED-Schritt)
- `test_hall_empty_with_vel_above_floor` — vel=20 → target=30 (capped auf feed_speed)
- `test_hall_empty_with_vel_at_floor` — vel=15 → target=22.5
- `test_hall_empty_with_vel_below_floor` — vel=8 → target=15 (MIN_FLOOR)
- `test_hall_empty_with_vel_zero_ready` — vel=0, ready=True → target=15
- `test_hall_empty_with_vel_not_ready` — ready=False → target=15
- `test_intermediate_zone_with_vel` — vel=20, all halls off → target=22 (vel*1.10)
- `test_intermediate_zone_low_vel` — vel=5, all halls off → target=0
- `test_hall_full` — HALL2:on → target=0 (regression-check)
- `test_hall_overflow` — HALL1:on → target=0 (regression-check)
- `test_feed_speed_cap` — vel=50, feed_speed=30 → target=30 (Soft-Cap)

### Hardware-Validierung (Phase 5)
Print-Bett-Frage an User → Feedertest.gcode starten → Log beobachten:
- **Erfolgs-Kriterium 1:** ≤2 HALL1-OVERFLOW-Zyklen über 4min Print (statt 16+)
- **Erfolgs-Kriterium 2:** Kein `stepcompress c=N i=0 Invalid sequence` Crash
- **Erfolgs-Kriterium 3:** `target_speed` in buffer_metrics korreliert sichtbar mit `tracker_vel` (statt 30/0-Hopping)

---

## 8. Commit-Message (Vorlage für Implementation-Phase)

```
C-cont Hotfix 7: Soft-Throttle — verbrauchskonforme Feeder-Geschwindigkeit

Hardware-Crash 2026-05-13 (Feedertest.gcode, 4min 11sec):
  stepcompress o=0 i=0 c=12 a=0: Invalid sequence
  Error in syncemitter 'mellow' step generation
  Exception in flush_handler → MCU-Shutdown

Wurzel-Ursache (Hardware-Geometrie + Pipeline-Energie):
  Buffer-Sensorik = optische Lichtschranken (NICHT Hall-Magnete)
  HALL3↔HALL2 Abstand: 12.8mm
  HALL2↔HALL1 Abstand:  3.6mm
  Auslöser-Länge:      3-4mm
  Hebel:               2:1 (5mm Filament-Push → 2.5mm Arm-Deflektion)
  Hotfix6 5mm-Chunk @ 30 mm/s injiziert Schwung-Energie die den
  Arm systematisch über HALL2 hinaus in HALL1 katapultiert
  (3.6mm Sicherheitsmarge < 1 Sub-Chunk Auswirkung).

Im klippy.log: 16+ HALL1-OVERFLOW-Zyklen in 80s vor Crash,
HALL2 in buffer_metrics nie als "on" gesehen vor HALL1-Trigger
(Polling-Aliasing wegen Schwung-Geschwindigkeit).

Fix-Logik:
  HALL3:on  → target = max(vel * 1.5, MIN_FLOOR)  # statt fix 30
  HALL2:on  → target = 0                          # unverändert
  HALL1:on  → OVERFLOW                            # unverändert
  Zwischen  → target = vel * 1.10 (oder 0 bei vel<MIN_FLOOR)
  cap:        target = min(target, feed_speed)

Effekt: feed_speed skaliert mit Druck-Bedarf statt fixem
Maximum. Bei langsamem Druck (vel=5 mm/s) schiebt Feeder nur
15 mm/s statt 30 → Schwung-Energie halbiert → Arm bremst in
HALL2-Sicherheitsfenster ab statt HALL1 zu treffen.

Tests: 10 neue Test-Cases in test_speed_modulator.py
       (vor: N → nach: N+10, alle grün)
```

---

## 9. Offene Fragen / Pending-User-Decisions

- [ ] User-Approval für Spec → Phase 2 (Plan)
- [ ] Vorgehen Phase 4 (Code-Review): selbst-Review (kleine Änderung, <50 LOC) oder code-reviewer Subagent?
- [ ] PR-Strategie nach Hardware-Test-Erfolg: in Fork commiten (Regel #9 zwingend) — separater PR zum Upstream nur nach User-Entscheidung
