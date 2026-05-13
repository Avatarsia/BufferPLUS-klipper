# C-cont Hotfix 9 — forced_t0-Pfad Reprime-bei-Gap (Implementation Plan)

> **TDD-Workflow (Regel #7):** Tests ZUERST. RED → GREEN → Full-Suite grün.

**Spec:** `docs/superpowers/specs/2026-05-14-c-cont-hotfix9-forced-t0-reprime-gap.md`
**Branch-Basis:** `feature/c-cont-streaming` HEAD `257b011` (Hotfix 8 Diagnostic-Logs)
**Code-Review:** code-reviewer Subagent (Standard per Regel #7)
**Ziel-LOC:** Code: ~5 Zeilen Änderung + Kommentar-Block (~25 LOC). Tests: 3 neue Tests.

---

## File Structure

**Modified:**
- `klipper_extras/buffer_feeder.py`
  - Z.3537-3540: `need_reprime`-Berechnung vereinheitlichen (Branches entfernen)
  - Inline-Kommentar-Block über der Änderung (Hotfix9-Doku, Regel #8)

**Modified (Tests):**
- `tests/test_c_cont_streaming.py` ODER `tests/test_p7_66_streaming.py`
  - 3 neue Tests für Hotfix 9 (gap>5s Reprime, gap<5s kein Reprime, forced_t0=None unverändert)

**Keine neuen Files.**

---

## Task 1 — TDD-Implementation

### Step 1 — Tests schreiben (RED erwartet)

**1.1 Test-Helper für stepper.set_position-Mocking** (falls noch nicht vorhanden):

Helper-Funktion am Anfang des Test-Files prüfen — wenn schon vorhanden, nutzen. Sonst:

```python
def _capture_set_position(feeder, monkeypatch):
    """Capture stepper.set_position calls. Returns list of position tuples."""
    calls = []
    orig = feeder.stepper.set_position
    def capture(pos):
        calls.append(pos)
        return orig(pos) if hasattr(orig, '__call__') else None
    monkeypatch.setattr(feeder.stepper, 'set_position', capture)
    return calls
```

**1.2 Drei neue Tests in `test_c_cont_streaming.py`** (am Ende der Hotfix7-Tests, vor dem T5-Section):

```python
# ---------------------------------------------------------------------------
# C-cont Hotfix 9 (Hardware 2026-05-14 klippy.log Z.250 Timer-too-close).
# forced_t0-Pfad Reprime-bei-Gap. Schliesst langjaehrige Luecke wo gap>5s
# im flush-callback-Pfad keinen Cursor-Reset triggerte.
# Siehe specs/2026-05-14-c-cont-hotfix9-forced-t0-reprime-gap.md
# ---------------------------------------------------------------------------


def test_c_cont_hotfix9_forced_t0_large_gap_triggers_reprime(monkeypatch):
    """Hotfix9: forced_t0-Pfad mit gap > REPRIME_GAP=5s triggert
    set_position(0,0,0) Reprime. Schliesst Luecke vom Klipper-Idle-
    Crash 2026-05-14 (DIAG-Beleg gap=36.676s, primed=True,
    need_reprime=False -> Timer too close)."""
    printer, feeder = make_c_cont_feeder(monkeypatch)
    feeder._state = buffer_feeder.STATE_AUTO
    feeder._last_move_end_time = 12.7  # Alt: Boot-Anchor lange her
    feeder._stepcompress_primed = True
    feeder._commanded_pos = 0.05  # boot_feed

    # Mock mcu.estimated_print_time auf 49.4 (mcu_now)
    mcu = feeder.stepper.get_mcu()
    monkeypatch.setattr(mcu, 'estimated_print_time',
                        lambda mt: 49.4)
    set_position_calls = _capture_set_position(feeder, monkeypatch)

    # forced_t0-Submit mit gap=36.7s
    feeder._submit_single_trapezoid(5.0, 15.0, forced_t0=13.086)

    # Hotfix9: set_position muss aufgerufen worden sein (Cursor-Reset)
    assert len(set_position_calls) == 1
    assert set_position_calls[0] == (0., 0., 0.)
    # _commanded_pos: nach Reset auf 0 + Move-Distance 5 = 5
    assert feeder._commanded_pos == pytest.approx(5.0, abs=0.01)
    # Primed-Flag bleibt True
    assert feeder._stepcompress_primed is True


def test_c_cont_hotfix9_forced_t0_small_gap_no_reprime(monkeypatch):
    """Hotfix9 Regression: forced_t0 + gap<REPRIME_GAP=5s -> KEIN
    Reprime. Normale Submit-Logik unveraendert, kein Cursor-Reset."""
    printer, feeder = make_c_cont_feeder(monkeypatch)
    feeder._state = buffer_feeder.STATE_AUTO
    feeder._last_move_end_time = 48.0  # gap=1.4s
    feeder._stepcompress_primed = True
    feeder._commanded_pos = 100.0

    mcu = feeder.stepper.get_mcu()
    monkeypatch.setattr(mcu, 'estimated_print_time',
                        lambda mt: 49.4)
    set_position_calls = _capture_set_position(feeder, monkeypatch)

    feeder._submit_single_trapezoid(5.0, 15.0, forced_t0=49.0)

    # KEIN Reprime (gap<5s)
    assert len(set_position_calls) == 0
    # _commanded_pos addiert ohne Reset: 100 + 5 = 105
    assert feeder._commanded_pos == pytest.approx(105.0, abs=0.01)


def test_c_cont_hotfix9_forced_t0_not_primed_still_reprimes(monkeypatch):
    """Hotfix9 Regression: forced_t0 + not_primed (z.B. nach OVERFLOW
    Stepper-Disable) -> Reprime feuert wie vorher (gap unabhaengig).
    Stellt sicher dass der ursprüngliche P7-67 Reprime-Pfad nicht
    gebrochen wird."""
    printer, feeder = make_c_cont_feeder(monkeypatch)
    feeder._state = buffer_feeder.STATE_AUTO
    feeder._last_move_end_time = 49.0  # gap=0.4s
    feeder._stepcompress_primed = False  # NOT primed
    feeder._commanded_pos = 0.0

    mcu = feeder.stepper.get_mcu()
    monkeypatch.setattr(mcu, 'estimated_print_time',
                        lambda mt: 49.4)
    set_position_calls = _capture_set_position(feeder, monkeypatch)

    feeder._submit_single_trapezoid(5.0, 15.0, forced_t0=49.5)

    # Reprime weil not_primed (unveraendert von Hotfix9, war auch vorher so)
    assert len(set_position_calls) == 1
    assert feeder._stepcompress_primed is True
```

### Step 2 — pytest erwartet RED

```bash
cd D:/Entwicklung/LLL-Python/Repo
python -m pytest tests/test_c_cont_streaming.py -v -k "hotfix9" 2>&1 | tail -15
```

- [ ] **Erwartung:**
  - `test_c_cont_hotfix9_forced_t0_large_gap_triggers_reprime`: **FAIL** (kein set_position aufgerufen, weil current Code im forced_t0-Pfad gap nicht prüft)
  - `test_c_cont_hotfix9_forced_t0_small_gap_no_reprime`: **PASS** (Verhalten vorher schon korrekt)
  - `test_c_cont_hotfix9_forced_t0_not_primed_still_reprimes`: **PASS** (Verhalten vorher schon korrekt)

Mindestens 1 FAIL erwartet (large_gap-Test).

### Step 3 — Code implementieren

`klipper_extras/buffer_feeder.py`, Z.3537:

```python
# alt (Hotfix 0...8):
if forced_t0 is None:
    need_reprime = (not self._stepcompress_primed) or (gap > REPRIME_GAP)
else:
    need_reprime = not self._stepcompress_primed

# neu (Hotfix 9):
# Hotfix9 (Hardware 2026-05-14 klippy.log Z.250 + DIAG-Beleg Z.16-17,
# langjaehriger sporadischer Timer-too-close beim Druckstart). Wurzel:
# forced_t0-Pfad ignorierte gap-basierten Reprime-Trigger mit der Annahme
# "step_gen_time ist der Anker". Diese Annahme versagt wenn Klipper's
# motion_queuing zwischen Boot-Anchor und Print-Start nicht flusht —
# step_gen_time bleibt beim Boot-Wert, der erste Print-Submit kommt mit
# step_gen_time 10-60s alt. Cursor (last_step_clock) bei alter Position,
# neuer Step bei mcu_now → CLOCK_DIFF_MAX (~16.78s @ 48MHz) ueberschritten
# → MCU 'Timer too close'.
#
# Fix: gap-basierter Reprime-Trigger auch im forced_t0-Pfad. set_position(
# 0,0,0) im Reprime-Block ist im forced_t0-Pfad sicher (kein flush_step_-
# generation -> kein Reactor-Pause). Cursor wird auf 0 reset, neuer Move
# startet sauber bei konsistenter Position. Bei normalem Betrieb (gap<5s)
# keine Verhaltensaenderung.
#
# User-Beobachtung (2026-05-14): Problem besteht schon vor C-cont-Serie,
# sporadisch beim Druckstart nach laengerer Klipper-Idle-Zeit. Klipper-
# Restart half temporaer. Hotfix 9 schliesst die Luecke strukturell.
need_reprime = (not self._stepcompress_primed) or (gap > REPRIME_GAP)
```

- [ ] **Code-Block einfügen.** Alte if/else-Branches entfernen, durch vereinheitlichte Zeile ersetzen. Kommentar-Block davor.

### Step 4 — pytest erwartet GREEN

```bash
python -m pytest tests/test_c_cont_streaming.py -v -k "hotfix9" 2>&1 | tail -10
```

- [ ] **Erwartung:** alle 3 Hotfix9-Tests grün.

### Step 5 — Full-Suite

```bash
python -m pytest tests/ 2>&1 | tail -8
```

- [ ] **Erwartung:** Gesamte Test-Suite grün (vorher 406 → erwartet 409). Bei Regressionen: STOP, systematic-debugging.

---

## Task 2 — Code-Review via code-reviewer Subagent

**Voraussetzung:** Task 1 komplett, alle Tests grün.

### Step 1 — Git-Diff prep

```bash
git status   # Erwartung: 2 Files modified (buffer_feeder.py, test_c_cont_streaming.py)
git diff --stat
```

### Step 2 — Subagent dispatchen

Reviewer-Prompt enthält:
- Spec-Pfad
- Plan-Pfad
- BASE_SHA = `257b011` (Hotfix 8)
- HEAD_SHA = neuer Hotfix9-Commit
- Beschreibung: "Forced_t0-path reprime-gap fix for long-standing Klipper-idle-gap crash"
- Spezielle Review-Fokus-Punkte:
  - Ist die Vereinheitlichung der need_reprime-Logik sicher?
  - Greifen alle bestehenden Defense-Patches (P7-67, P7-71, P7-72) noch korrekt?
  - Side-effects auf bestehende set_position()-Aufrufe?
  - Kann das set_position(0,0,0) eine andere Race öffnen?

### Step 3 — Findings adressieren

- [ ] Critical → sofort fix
- [ ] Important → vor HW-Test fix
- [ ] Minor → Backlog

---

## Task 3 — Fork-Commit + Push

**Voraussetzung:** Task 2 fertig.

### Commit-Message (Maintainer-Stil)

```
fix(buffer_feeder): C-cont Hotfix 9 — forced_t0-Pfad Reprime-bei-Gap

Behebt langjaehrigen sporadischen Crash beim Druckstart:
  MCU 'LLL_PLUS' shutdown: Timer too close
  shutdown clock=... static_string_id=Timer too close

Wurzel-Ursache (DIAG-Beleg Hotfix 8, klippy.log Z.16-17):
  buffer_feeder DIAG resume-edge:
    mcu_now=49.382 lme=12.705 gap=36.676s primed=True
    step_gen=12.966 flush=12.966 [...]
  buffer_feeder DIAG submit:
    forced_t0=13.086 t0=49.502 was_primed=True stale_anchor=True
    need_reprime=False [...]

Bei Klipper-Idle-Zeit zwischen Boot und Druckstart liefert
motion_queuing einen veralteten step_gen_time an _on_mcu_flush.
Der buffer_feeder berechnet forced_t0 darauf basierend (in
Vergangenheit). Im resulting Stepcompress-Submit ist der neue
Step weit nach last_step_clock — Differenz > CLOCK_DIFF_MAX
(16.78s @ 48MHz) -> Timer too close.

Code-Stelle (_submit_single_trapezoid Z.3537):
  if forced_t0 is None:
    need_reprime = (not _stepcompress_primed) or (gap > REPRIME_GAP)
  else:
    need_reprime = not _stepcompress_primed  <- gap nicht geprueft

Fix: Beide Branches vereinheitlicht.
  need_reprime = (not _stepcompress_primed) or (gap > REPRIME_GAP)

set_position(0,0,0) im Reprime-Block ist im forced_t0-Pfad
sicher (flush_step_generation bleibt im None-Pfad gegated,
also kein Reactor-Pause-Issue). Cursor reset, neuer Move
startet sauber. Bei gap<5s keine Verhaltensaenderung.

User-Bestaetigung 2026-05-14: Problem besteht schon vor
C-cont-Hotfix-Serie, sporadisch beim Druckstart nach langer
Klipper-Idle-Zeit. Klipper-Restart half temporaer.

Tests: 406 -> 409 grün (+3 neue Hotfix9-Tests, keine Regressionen).

Spec: docs/superpowers/specs/2026-05-14-c-cont-hotfix9-forced-t0-
       reprime-gap.md

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
```

### Push

```bash
git push origin feature/c-cont-streaming
```

---

## Task 4 — Hardware-Test

### Step 1 — Upload + MD5 + Restart

```bash
cat "D:/Entwicklung/LLL-Python/Repo/klipper_extras/buffer_feeder.py" | \
  "C:/Program Files/PuTTY/plink.exe" -pw "..." -batch pi@192.168.40.3 \
  "cat > /home/pi/BufferPLUS-klipper/klipper_extras/buffer_feeder.py"
```

MD5 + Klipper-Restart wie üblich.

### Step 2 — klippy.log truncate (NEUE REGEL vom User 2026-05-14)

```bash
plink -pw "..." -batch pi@192.168.40.3 "sudo truncate -s 0 /home/pi/printer_data/logs/klippy.log"
```

### Step 3 — Druckbett-Frage (Regel #5)

### Step 4 — Spezifischer Test-Plan für Hotfix 9

Zwei Szenarien testen:

**A) Idle-Gap-Reproduktion (vorher zu Crash gefuehrt):**
1. Klipper-Restart, dann **30-60s warten**
2. Print-Start
3. **Erwartung:** kein Timer-too-close, Print laeuft an

**B) Normaler Print nach Klipper-Restart (Baseline):**
1. Klipper-Restart
2. **Sofortiger** Print-Start (wie heute mittag erfolgreich)
3. **Erwartung:** normales Verhalten (kein Reprime weil gap<5s)

### Step 5 — Log-Analyse

DIAG-Logs prüfen:
- `DIAG resume-edge: ... gap=XX.X` Werte
- Bei großem Gap: erscheint im DIAG-`submit` jetzt `need_reprime=True`?
- `stepcompress re-primed via flush_step_generation` Zeilen — neue erwartet bei großem Gap

---

## Task 5 — Maintainer-PR (User-Decision)

Hotfix 9 ist **starker PR-Kandidat zum Upstream**:
- Adressiert lang bestehendes Problem
- Hat konkrete DIAG-Evidenz
- Minimaler Fix (1 Zeile + Kommentar)
- Erklärt User-Erfahrung "Klipper-Restart hilft"

Aber: separate User-Entscheidung pro Fix (Regel #9). Erst nach erfolgreichem HW-Test entscheiden.

---

## Pending User-Approvals

- [x] Spec freigegeben (parallel zum laufenden Print)
- [ ] **Plan freigegeben → Phase 3 (TDD-Implementation) starten?**
