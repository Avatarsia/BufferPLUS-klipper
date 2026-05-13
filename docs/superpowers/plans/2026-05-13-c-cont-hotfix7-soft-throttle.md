# C-cont Hotfix 7 — Soft-Throttle (Implementation Plan)

> **TDD-Workflow (Regel #7):** Tests ZUERST. Jede Task durchläuft fest 5 Steps: 1) Tests schreiben → 2) pytest erwartet RED → 3) Code minimal implementieren → 4) pytest erwartet GREEN → 5) Full-Suite pytest grün, keine Regressionen.

**Spec:** `docs/superpowers/specs/2026-05-13-c-cont-hotfix7-soft-throttle.md`
**Branch-Basis:** `feature/c-cont-streaming` HEAD (Hotfix 6, MD5 `f18a28ef290784aaf8a24ab046fdabb3`)
**Code-Review:** code-reviewer Subagent nach Task 3 (Phase 4 vor Hardware-Upload)
**Ziel-LOC:** Modulator-Funktion ~30 Zeilen, ~10 neue Tests, ~5 Updates an bestehenden Tests

---

## File Structure

**Modified:**
- `klipper_extras/buffer_feeder.py`
  - Methode `SpeedModulator._compute_target_feed_speed` (Z. 2731-2753)
  - Inline-Kommentar-Block über der Methode (Hotfix7-Doku, Maintainer-Stil, Regel #8)

**Modified (Tests):**
- `tests/test_c_cont_streaming.py`
  - Bestehende Tests die `30.0` als HALL3-Wert erwarten: 5 Tests updaten oder neu schreiben
  - 10 neue Tests für Hotfix7 hinzufügen

**Keine neuen Files** (kompakter Hotfix, kein Architektur-Umbau).

---

## Task 1 — Test-Suite erweitern + bestehende Tests anpassen

**Files:** `tests/test_c_cont_streaming.py`

### Step 1 — Tests schreiben (RED erwartet)

**1.1 Neue Hotfix7-Tests hinzufügen** (Block nach Z.387, nach bestehenden Hotfix6-Tests einfügen):

```python
# ---------------------------------------------------------------------------
# C-cont Hotfix 7 (Hardware 2026-05-13 klippy.log Z.104571, c=12 i=0 nach
# 16+ HALL1-OVERFLOW-Zyklen). Soft-Throttle: Feeder-Speed skaliert mit
# Extruder-Verbrauch statt fix 30 mm/s. Adressiert Hardware-Geometrie
# (HALL2->HALL1 nur 3.6mm Sicherheitsmarge vs 5mm-Chunk-Schwung-Energie).
# Siehe specs/2026-05-13-c-cont-hotfix7-soft-throttle.md
# ---------------------------------------------------------------------------


def test_c_cont_hotfix7_hall3_high_vel_capped_at_feed_speed(monkeypatch):
    """Hotfix7: HALL3:on + vel=25 -> max(25*1.5=37.5, 15) capped auf
    feed_speed=30. Soft-Cap verhindert ueberzogene Speeds bei schnellen
    Drucken."""
    printer, feeder = make_c_cont_feeder(monkeypatch)
    set_sensor_active(feeder, 'hall_empty', True)
    set_sensor_active(feeder, 'hall_full', False)
    set_sensor_active(feeder, 'hall_overflow', False)
    _populate_tracker_to_ready(feeder, velocity=25.0)
    # 25*1.5=37.5 > feed_speed=30 -> capped to 30
    assert feeder._compute_target_feed_speed() == pytest.approx(30.0, abs=0.1)


def test_c_cont_hotfix7_hall3_mid_vel_scales_with_extruder(monkeypatch):
    """Hotfix7: HALL3:on + vel=15 -> max(15*1.5=22.5, 15)=22.5.
    Scaling mit Verbrauch statt fixe 30 mm/s -> halbierte Schwung-Energie
    bei moderaten Drucken."""
    printer, feeder = make_c_cont_feeder(monkeypatch)
    set_sensor_active(feeder, 'hall_empty', True)
    set_sensor_active(feeder, 'hall_full', False)
    set_sensor_active(feeder, 'hall_overflow', False)
    _populate_tracker_to_ready(feeder, velocity=15.0)
    assert feeder._compute_target_feed_speed() == pytest.approx(22.5, abs=0.1)


def test_c_cont_hotfix7_hall3_low_vel_uses_min_floor(monkeypatch):
    """Hotfix7: HALL3:on + vel=8 (unter MIN_FLOOR) -> 15 mm/s
    (MIN_FLOOR-Boden). 8*1.5=12 < 15 -> Floor wins."""
    printer, feeder = make_c_cont_feeder(monkeypatch)
    set_sensor_active(feeder, 'hall_empty', True)
    set_sensor_active(feeder, 'hall_full', False)
    set_sensor_active(feeder, 'hall_overflow', False)
    _populate_tracker_to_ready(feeder, velocity=8.0)
    assert feeder._compute_target_feed_speed() == pytest.approx(15.0, abs=0.1)


def test_c_cont_hotfix7_hall3_vel_zero_uses_min_floor(monkeypatch):
    """Hotfix7: HALL3:on + tracker ready aber vel=0 (Toolhead stalled
    mit leerem Buffer) -> 15 mm/s. Buffer muss trotz Stall gefuellt
    werden, aber sanft (MIN_FLOOR statt 30)."""
    printer, feeder = make_c_cont_feeder(monkeypatch)
    set_sensor_active(feeder, 'hall_empty', True)
    set_sensor_active(feeder, 'hall_full', False)
    set_sensor_active(feeder, 'hall_overflow', False)
    # Tracker ready aber vel=0
    fake_ext = feeder.printer.objects['extruder']
    t = 0.0
    for _ in range(12):
        fake_ext.last_position = 100.0  # konstant -> vel=0
        feeder.velocity_tracker.tick(t)
        t += 0.025
    assert feeder.velocity_tracker.is_ready()
    assert feeder.velocity_tracker.get_velocity() == 0.0
    assert feeder._compute_target_feed_speed() == pytest.approx(15.0, abs=0.1)


def test_c_cont_hotfix7_hall3_tracker_not_ready_uses_min_floor(monkeypatch):
    """Hotfix7: HALL3:on + tracker noch nicht ready (Print-Start) ->
    15 mm/s. Vorher Hotfix6: 30 mm/s -> aggressiver Initial-Fill ->
    HALL1-Overshoot waehrend velocity_tracker noch sammelt."""
    printer, feeder = make_c_cont_feeder(monkeypatch)
    set_sensor_active(feeder, 'hall_empty', True)
    set_sensor_active(feeder, 'hall_full', False)
    set_sensor_active(feeder, 'hall_overflow', False)
    assert not feeder.velocity_tracker.is_ready()
    assert feeder._compute_target_feed_speed() == pytest.approx(15.0, abs=0.1)


def test_c_cont_hotfix7_zwischen_low_vel_zero(monkeypatch):
    """Hotfix7: Zwischenzone + vel<MIN_FLOOR -> 0.0 (unveraendert
    von Hotfix5: vermeidet Submits bei Spurious-Buffer-Drift)."""
    printer, feeder = make_c_cont_feeder(monkeypatch)
    set_sensor_active(feeder, 'hall_empty', False)
    set_sensor_active(feeder, 'hall_full', False)
    set_sensor_active(feeder, 'hall_overflow', False)
    _populate_tracker_to_ready(feeder, velocity=5.0)
    assert feeder._compute_target_feed_speed() == 0.0


def test_c_cont_hotfix7_zwischen_high_vel_capped(monkeypatch):
    """Hotfix7: Zwischenzone + vel=40 -> min(40*1.10=44, feed_speed=30)=30.
    NEUER Soft-Cap in Zwischenzone (vorher: unbounded vel*1.10).
    Verhindert dass schnelle Drucke target_speed > feed_speed setzen."""
    printer, feeder = make_c_cont_feeder(monkeypatch)
    set_sensor_active(feeder, 'hall_empty', False)
    set_sensor_active(feeder, 'hall_full', False)
    set_sensor_active(feeder, 'hall_overflow', False)
    _populate_tracker_to_ready(feeder, velocity=40.0)
    assert feeder._compute_target_feed_speed() == pytest.approx(30.0, abs=0.1)


def test_c_cont_hotfix7_zwischen_tracker_not_ready_zero(monkeypatch):
    """Hotfix7: Zwischenzone + tracker not_ready -> 0.0 (unveraendert
    von Hotfix4: nur HALL3:on darf bei not_ready foerdern, alle anderen
    Zonen warten auf echte Velocity)."""
    printer, feeder = make_c_cont_feeder(monkeypatch)
    set_sensor_active(feeder, 'hall_empty', False)
    set_sensor_active(feeder, 'hall_full', False)
    set_sensor_active(feeder, 'hall_overflow', False)
    assert not feeder.velocity_tracker.is_ready()
    assert feeder._compute_target_feed_speed() == 0.0


def test_c_cont_hotfix7_hall2_zero_regression(monkeypatch):
    """Regression: HALL2:on -> 0.0 (unveraendert von Hotfix5).
    Stellt sicher dass Hotfix7-Umbau den HALL2-Pfad nicht bricht."""
    printer, feeder = make_c_cont_feeder(monkeypatch)
    set_sensor_active(feeder, 'hall_empty', False)
    set_sensor_active(feeder, 'hall_full', True)
    set_sensor_active(feeder, 'hall_overflow', False)
    _populate_tracker_to_ready(feeder, velocity=20.0)
    assert feeder._compute_target_feed_speed() == 0.0


def test_c_cont_hotfix7_hall1_zero_regression(monkeypatch):
    """Regression: HALL1:on -> 0.0 (Notbremse). Stellt sicher dass
    Hotfix7-Umbau den OVERFLOW-Pfad nicht beeinflusst."""
    printer, feeder = make_c_cont_feeder(monkeypatch)
    set_sensor_active(feeder, 'hall_overflow', True)
    set_sensor_active(feeder, 'hall_empty', False)
    set_sensor_active(feeder, 'hall_full', False)
    _populate_tracker_to_ready(feeder, velocity=20.0)
    assert feeder._compute_target_feed_speed() == 0.0
```

**1.2 Bestehende Hotfix6-Tests anpassen** (5 Tests):

| Test (Zeile) | Bisheriger Erwartungswert | Neuer Erwartungswert | Begründung |
|---|---|---|---|
| `test_c_cont_modulator_hall3_max` (Z.154) | `== 30.0` mit vel=15 | `== pytest.approx(22.5, abs=0.1)` | 15*1.5=22.5 (Soft-Throttle) |
| `test_c_cont_hotfix4_stalled_but_hall_empty_still_fills` (Z.257) | `== 30.0` mit vel=0 | `== pytest.approx(15.0, abs=0.1)` | MIN_FLOOR-Pfad |
| `test_c_cont_hotfix4_not_ready_but_hall_empty_uses_fallback` (Z.288) | `== 30.0` mit not_ready | `== pytest.approx(15.0, abs=0.1)` | MIN_FLOOR-Pfad |
| `test_c_cont_hotfix6_hall3_uses_30_not_feed_speed` (Z.376) | `== 30.0` | **Test umschreiben** als `test_c_cont_hotfix7_hall3_scales_with_vel` | Hotfix6 ist obsolet |
| `test_c_cont_hotfix6_hall3_speed_constant_30` (Z.919) | `== 30.0` zweimal | **Test umschreiben** als `test_c_cont_hotfix7_hall3_speed_varies_with_vel` | Hotfix6 ist obsolet |
| `test_c_cont_hotfix3_zwischen_soft_floor_high_vel` (Z.319) | `== pytest.approx(55.0)` für vel=50 | `== pytest.approx(30.0)` | NEUER Soft-Cap (feed_speed=30) |

Jeder Test bekommt einen Kommentar `# Hotfix7-Update:` mit kurzer Begründung.

### Step 2 — pytest erwartet RED

```bash
cd D:/Entwicklung/LLL-Python/Repo
python -m pytest tests/test_c_cont_streaming.py -v -k "hotfix7 or modulator_hall3_max or hotfix4_stalled_but_hall_empty or hotfix4_not_ready_but_hall_empty or hotfix6_hall3 or hotfix3_zwischen_soft_floor_high_vel"
```

- [ ] **Erwartung:** 10 neue Tests FAIL (assertion error, Methode liefert noch alte Werte), 4-5 Update-Tests FAIL (alter Wert vs neuer Erwartungswert). Alle anderen Tests grün.

### Step 3 — Code implementieren

`klipper_extras/buffer_feeder.py`, Methode `_compute_target_feed_speed` in `SpeedModulator` (Z. 2731-2753) ersetzen:

```python
def _compute_target_feed_speed(self):
    # C-cont Hotfix7 (Hardware-Crash 2026-05-13 klippy.log Z.104571,
    # c=12 i=0 Invalid sequence nach 16+ HALL1-OVERFLOW-Zyklen in
    # 80s). Wurzel: Hardware-Geometrie (optische Lichtschranken,
    # HALL3<->HALL2 = 12.8mm, HALL2<->HALL1 = 3.6mm, Ausloeser-
    # Fahne 3-4mm, Hebel 2:1) + Hotfix6's 5mm-Chunk @ 30 mm/s
    # injiziert Schwung-Energie die den Arm systematisch ueber
    # HALL2 katapultiert. HALL2->HALL1 in nur 3.6mm (=120ms bei
    # 30 mm/s) < Flush-Tick-Reaktionszeit (~250-500ms). Software
    # strukturell zu langsam, ergo Schwung-Energie muss runter.
    #
    # Soft-Throttle: target_speed skaliert mit Extruder-Verbrauch:
    #   HALL3:on, vel>=MIN_FLOOR:   max(vel*1.5, MIN_FLOOR), cap feed_speed
    #   HALL3:on, vel<MIN_FLOOR:    MIN_FLOOR (Initial-Boost / Idle)
    #   HALL3:on, not ready:        MIN_FLOOR (Print-Start)
    #   Zwischenzone, vel>=MIN_FLOOR: min(vel*1.10, feed_speed)
    #   Zwischenzone, vel<MIN_FLOOR oder not ready: 0
    #   HALL2:on:                   0 (Hotfix5, unveraendert)
    #   HALL1:on:                   0 (Notbremse, unveraendert)
    #
    # Hotfix6 (vorher): HALL3:on -> 30 mm/s fix. Bei niedriger
    # Print-Geschwindigkeit injiziert das 6x Verbrauch -> Buffer-
    # Arm-Overshoot HALL2->HALL1 (siehe Hardware-Beleg oben).
    # Hotfix7 koppelt Push an Bedarf: bei vel=5 schiebt Feeder
    # nur 15 mm/s (statt 30) -> halbierte Schwung-Energie ->
    # Arm bremst in HALL2-Sicherheitsfenster ab.
    #
    # Soft-Cap auf feed_speed (NEU vs Hotfix3): verhindert dass
    # vel*1.5 oder vel*1.10 bei schnellen Drucken target_speed
    # ueber die konfigurierte Obergrenze treibt. Hardware-sicher
    # weil feed_speed im cfg bereits hardware-validiert ist.
    MIN_FLOOR = 15.0  # mm/s, ererbt von Hotfix3 (validated baseline)

    if self.hall_overflow:
        return 0.0
    if self.hall_full:
        return 0.0  # Hotfix5: HALL2 = Buffer voll, kein Push

    vel_ready = self.velocity_tracker.is_ready()
    vel = self.velocity_tracker.get_velocity() if vel_ready else 0.0

    if self.hall_empty:
        # HALL3:on -> Buffer leer, Auffuellen mit 50% Margin
        if not vel_ready or vel < MIN_FLOOR:
            return MIN_FLOOR
        return min(max(vel * 1.5, MIN_FLOOR), self.feed_speed)

    # Zwischenzone (kein HALL aktiv)
    if not vel_ready or vel < MIN_FLOOR:
        return 0.0
    return min(vel * 1.10, self.feed_speed)
```

- [ ] **Code-Block einfügen.** Alten Block (Hotfix6) komplett ersetzen.

### Step 4 — pytest erwartet GREEN

```bash
python -m pytest tests/test_c_cont_streaming.py -v -k "hotfix7 or modulator_hall3 or hotfix4_stalled or hotfix4_not_ready or hotfix3_zwischen_soft_floor_high_vel"
```

- [ ] **Erwartung:** Alle 14-15 modifizierten/neuen Tests grün.

### Step 5 — Full-Suite pytest

```bash
python -m pytest tests/ -v
```

- [ ] **Erwartung:** Gesamte Test-Suite grün. Falls Regressionen: STOP, root-cause Investigation per superpowers:systematic-debugging.

---

## Task 2 — Code-Review via code-reviewer Subagent (Phase 4)

**Voraussetzung:** Task 1 komplett, alle Tests grün.

### Step 1 — Git-Commit lokal (Hotfix7 in Working-Copy)

```bash
cd D:/Entwicklung/LLL-Python/Repo
git status   # Erwartung: 2 Files modified (buffer_feeder.py, test_c_cont_streaming.py)
git diff --stat
```

- [ ] **Verifikation:** Nur die 2 Files erscheinen, Diff-Size plausibel (~30 LOC Code, ~150 LOC Tests).

### Step 2 — Diff für Reviewer vorbereiten

Reviewer-Agent erhält:
- Spec-Pfad: `docs/superpowers/specs/2026-05-13-c-cont-hotfix7-soft-throttle.md`
- Plan-Pfad: `docs/superpowers/plans/2026-05-13-c-cont-hotfix7-soft-throttle.md`
- BASE_SHA = aktueller `feature/c-cont-streaming` HEAD (Hotfix6)
- HEAD_SHA = lokaler Working-Copy-Commit

### Step 3 — code-reviewer Subagent dispatchen

Via Agent-Tool, subagent_type='superpowers:code-reviewer'. Prompt enthält:
- Was implementiert wurde (Soft-Throttle, Hardware-Geometrie-driven Refactor)
- Plan-Referenz (Pfad)
- Spec-Referenz (Pfad)
- Git BASE/HEAD SHAs
- Beschreibung der Hardware-Wurzel-Analyse

### Step 4 — Findings adressieren

- [ ] **Critical** → sofort fix vor Hardware-Test
- [ ] **Important** → fix vor Hardware-Test (Standard)
- [ ] **Minor** → in Backlog, optional inline-fix

Bei push-back: User-Diskussion ob Reviewer-Feedback berechtigt.

### Step 5 — Re-Test nach Findings-Fixes

```bash
python -m pytest tests/ -v
```

- [ ] **Erwartung:** weiter grün.

---

## Task 3 — Fork-Commit (Regel #9)

**Voraussetzung:** Task 2 fertig, Code-Review-Findings adressiert.

### Step 1 — Commit-Message vorbereiten

Maintainer-Stil, basierend auf Vorlage in Spec Kap. 8:
- Hardware-Beleg zitiert (klippy.log Z.104571, c=12 i=0)
- Wurzel benannt (Hardware-Geometrie + Pipeline-Energie)
- Fix-Logik erklärt (Soft-Throttle, Skalierung mit vel)
- Test-Count (vor: N → nach: N+10 minus 2 ersetzt = N+8)

### Step 2 — Branch-Wahl

Branch: `feature/c-cont-streaming` (gleicher Branch, weiterer Commit)

### Step 3 — Commit + Push

```bash
git add klipper_extras/buffer_feeder.py tests/test_c_cont_streaming.py docs/superpowers/specs/2026-05-13-c-cont-hotfix7-soft-throttle.md docs/superpowers/plans/2026-05-13-c-cont-hotfix7-soft-throttle.md
git commit -m "..." # Heredoc mit Maintainer-Format
git push origin feature/c-cont-streaming
```

- [ ] **Verifikation:** Push erfolgreich, Commit-SHA notieren.

---

## Task 4 — Hardware-Test (Phase 5)

**Voraussetzung:** Task 3 fertig, Fork-Commit pushed.

### Step 1 — Upload buffer_feeder.py auf Drucker

```bash
cat "D:/Entwicklung/LLL-Python/Repo/klipper_extras/buffer_feeder.py" | \
  "C:\Program Files\PuTTY\plink.exe" -pw "Je040280-+" -batch pi@192.168.40.3 \
  "cat > /home/pi/BufferPLUS-klipper/klipper_extras/buffer_feeder.py"
```

### Step 2 — MD5-Verifikation

```bash
plink -pw "Je040280-+" -batch pi@192.168.40.3 \
  "md5sum /home/pi/BufferPLUS-klipper/klipper_extras/buffer_feeder.py"
```

Lokaler MD5 vergleichen mit:
```bash
md5sum D:/Entwicklung/LLL-Python/Repo/klipper_extras/buffer_feeder.py
```

- [ ] **Erwartung:** MD5 identisch.

### Step 3 — Klipper-Neustart

```bash
plink -pw "Je040280-+" -batch pi@192.168.40.3 "sudo systemctl restart klipper"
```

### Step 4 — Druckbett-Frage an User (Regel #5)

- [ ] **Frage:** "Ist das Druckbett frei?"
- [ ] **Auf Bestätigung warten** — nicht ohne Bestätigung weitermachen.

### Step 5 — User startet Druck, Beobachtung

User startet Feedertest.gcode. Erwartete Erfolgs-Kriterien:
- ≤2 HALL1-OVERFLOW-Zyklen über 4min Print (statt 16+)
- Kein `stepcompress c=N i=0 Invalid sequence` Crash
- `target_speed` in buffer_metrics korreliert mit `tracker_vel`

### Step 6 — Log analysieren (egal ob Erfolg oder Crash)

```bash
"C:\Program Files\PuTTY\plink.exe" -pw "Je040280-+" -batch pi@192.168.40.3 \
  "cat /home/pi/printer_data/logs/klippy.log" > "D:\Entwicklung\LLL-Python\klippy.log"
```

- [ ] **Bei Erfolg:** Erfolgs-Bericht an User, Memory-Update (Hotfix7 hardware-validated)
- [ ] **Bei Crash:** `superpowers:systematic-debugging` Phase 1 → Crash-Analyse, KEIN Quick-Fix

---

## Risiko-Management

**Iron-Law-Reminder:** Nach 3 fehlgeschlagenen Fixes (Hotfix4, 5, 6 zählen als bisherige Versuche) → Architektur-Frage stellen, KEIN weiterer Hotfix8 ohne Diskussion mit User.

**Aktueller Stand:** Hotfix7 ist Versuch 4 in der C-cont-Serie unter feature/c-cont-streaming. Wenn Hotfix7 die HALL1-Overshoot nicht ausreichend reduziert (z.B. nur 16 → 10 Zyklen statt 16 → 2), ist das ein **klares Signal** für Architektur-Diskussion (z.B. Wechsel zu prädiktivem Ansatz aus High-Flow-Spec, oder Hardware-Geometrie-Anpassung).

---

## Pending User-Approvals

- [x] Spec freigegeben (Phase 1 abgeschlossen)
- [ ] **Plan freigegeben → Phase 3 (TDD-Implementation) starten?**
