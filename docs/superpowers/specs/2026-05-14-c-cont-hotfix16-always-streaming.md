# C-cont Hotfix 16 — Always-Streaming Mode (eliminiert Submit-Mode-Wechsel-Race)

**Datum:** 2026-05-14
**Branch-Basis:** `feature/c-cont-streaming` HEAD `4141aec` (Hotfix 12 DIAG-Baseline)
**Status:** Spec — pending User-Approval vor Phase 2 (Plan)
**Wurzel-Beleg:** 5 Hardware-Crashes (Runs 2-5) mit DIAG-Evidenz

---

## 1. Problem-Statement

### Hardware-Crash-Statistik (5 Runs, alle mid-print)

| Run | print_duration | Filament | Crash | Hotfix-Status |
|---|---|---|---|---|
| 2 | 4.8 min | 1325mm | c=22 i=0 | Hotfix 7+10 |
| 3 | 6.9 min | 2234mm | c=12 i=0 | Hotfix 7+10 |
| 4 | 7.2 min | 2393mm | c=15 i=0 | + Hotfix 14 (1ms padding) |
| 5 | 6.1 min | 1906mm | c=6 i=0 | + Hotfix 15 (50ms null-move) |
| 6 | 6.1 min | 1906mm | c=11 i=0 | + Hotfix 15 |

Statistische Range: 4.8-7.2 min reine Druckzeit. Padding (1ms, 50ms) zeigt **keine Wirkung**.

### Klipper-Source-Beweis (stepcompress.c Z.219)

```c
if (!move.count || (!move.interval && !move.add && move.count > 1)
    || move.interval >= 0x80000000) {
    errorf("stepcompress o=%d i=%d c=%d a=%d: Invalid sequence", ...);
}
```

Unsere Crashes treffen Bedingung 2: `interval=0 AND add=0 AND count>1`. `compress_bisect_add` packt mehrere Steps in eine queue_step-Command mit interval=0.

### DIAG-Pattern (Hotfix 12)

Direkt vor jedem Crash sehen wir Submit-Mode-Wechsel innerhalb 1-2 Sekunden:

```
Submit cmd_pos=N    streaming=True  → mode_hist=STREAM/...
Submit cmd_pos=N+5  streaming=False → mode_hist=STALE/...
Submit cmd_pos=N+10 streaming=True  → mode_hist=STREAM/...
→ CRASH c=N i=0
```

---

## 2. Wurzel-Identifikation

Im `_submit_single_trapezoid` gibt es zwei sehr unterschiedliche Pfade je nach `streaming`-Flag:

**Pfad A — `streaming=False` (Pfad in `_on_mcu_flush` wenn `move_in_flight=False`):**
```python
# Z.3502-3503 in _submit_single_trapezoid
if not streaming:
    self._enable_stepper()  # bumped _last_enable_schedule_time JEDEN Tick
```

**Pfad B — `streaming=True` (Pfad wenn `move_in_flight=True`):**
```python
# kein _enable_stepper Call → _last_enable_schedule_time bleibt stehen
```

**Korrektur zur ursprünglichen en-Floor-Theorie:**
Im stale_anchor=True Fall (lme in Vergangenheit) ergibt die Floor-Berechnung
```python
en = (0.0 if (streaming and was_primed and not need_reprime and not stale_anchor)
      else self._last_enable_schedule_time)
```
in BEIDEN Pfaden `en = _last_enable_schedule_time` (weil stale_anchor=True die streaming-Klausel abschaltet). Die `en`-Floor selbst ist **nicht** der Unterschied.

**Tatsächliche Wurzel-Hypothese (revidiert):**
Der reale Unterschied ist das **LEST-Bumping** (`_last_enable_schedule_time`). Wenn `move_in_flight=False` ↔ `True` alterniert, wird LEST in manchen Ticks neu auf `mcu_now+ahead` gesetzt (Pfad A) und in anderen nicht (Pfad B). Die Stepcompress-Pipeline sieht dadurch eine **nicht-monoton-konsistente Floor-Bewegung** in der t0-Sequenz, was Trapezoid-Folgen mit interval=0 erzeugen kann (compress_bisect_add packt Steps mit gleichem Clock).

`t0 = max(forced_t0, self._last_move_end_time, en, mcu_now)` — wenn LEST in Tick N+1 plötzlich höher springt als in Tick N (weil Pfad A erst jetzt wieder bumped), entsteht eine Stufe in der Step-Clock-Sequenz, die itersolve/stepcompress in seltenen Geometrien als invalid interpretiert.

---

## 3. Fix-Logik (C1 — Always-Streaming)

`_on_mcu_flush` submittet **immer mit `streaming=True`**, ungeachtet von `_move_in_flight()`:

```python
# alt:
self._submit_move(..., streaming=move_active, ...)

# neu (C1):
self._submit_move(..., streaming=True, ...)
```

**Effekt:** Submit-Mode bleibt konstant — kein Pfad-Wechsel mehr.

### Mitigations

**Mitigation 1 — Stepper-Enable-Sicherheit:**
`streaming=True` überspringt `_enable_stepper()`. Wenn Stepper disabled war (post-OVERFLOW, _pending_disable, primed=False), würde der Submit ohne Enable laufen → "Timer too close".

```python
# Vor _submit_move:
if (not self._stepcompress_primed
        or self._pending_disable
        or self._stepper_enable is None):
    self._enable_stepper()
```

**Mitigation 2 — lme-Floor (lme nicht stale):**
Bei `_move_in_flight()=False` ist `_last_move_end_time` in der Vergangenheit. Der existing Anchor-Code `anchor = step_gen_time + lead_time` bleibt unverändert — `t0 = max(forced_t0, lme, en, mcu_now)` rettet uns vor stale anchor via `mcu_now`-Floor.

---

## 4. Risiken (40% Restrisiko)

| Risiko | Wahrscheinlichkeit | Mitigation |
|---|---|---|
| Stepper-Enable-Race | 15% | Mitigation 1 (manuell enable) |
| Stale-lme-Anchor | 10% | Mitigation 2 (mcu_now-Floor) |
| Hotfix-9-Reprime-Interaktion | 10% | TDD-Test deckt das ab |
| Itersolve-Internal-Race | 5% | nicht von Python lösbar |

---

## 5. Tests (TDD - Phase 3)

### Test 1: streaming=True konstant
**Zweck:** Verifiziere dass `_on_mcu_flush` immer mit streaming=True submittet, auch wenn move_in_flight=False.
```python
def test_c_cont_hotfix16_always_streaming_when_no_move_in_flight():
    # Setup: keinerlei aktiver Move (lme in Vergangenheit)
    # Expectation: submit call hat streaming=True
```

### Test 2: streaming=True bei move_in_flight=True (unverändert)
**Zweck:** Regression — bei aktivem Move bleibt streaming=True (war vorher auch True).
```python
def test_c_cont_hotfix16_streaming_when_move_in_flight():
    # Setup: lme > mcu_now
    # Expectation: streaming=True (wie vorher)
```

### Test 3: Stepper-Enable wird bei not_primed gesichert
**Zweck:** Mitigation 1 — wenn _stepcompress_primed=False vor Submit, wird _enable_stepper aufgerufen.
```python
def test_c_cont_hotfix16_ensures_enable_when_not_primed():
    # Setup: _stepcompress_primed = False
    # _on_mcu_flush mit target>0
    # Expectation: _enable_stepper wurde aufgerufen vor _submit_move
```

### Test 4: Stepper-Enable wird bei _pending_disable gesichert
**Zweck:** Mitigation 1 — wenn _pending_disable=True, enable.
```python
def test_c_cont_hotfix16_ensures_enable_when_pending_disable():
    # Setup: _pending_disable=True
    # Expectation: _enable_stepper wurde aufgerufen
```

### Test 5: Anchor-Verhalten unverändert
**Zweck:** Regression — anchor=lme wenn move_active, anchor=step_gen+lead_time sonst.
```python
def test_c_cont_hotfix16_anchor_unchanged():
    # Beide Fälle prüfen
```

### Regression-Suite
Full-Suite muss grün bleiben: 412 → 412+5 neue = 417 erwartet.

---

## 6. Verbleibende Defense-Patches

Hotfix 7+9+10+12 bleiben alle aktiv:
- Hotfix 7 (Soft-Throttle) — Wurzel 1 gefixt
- Hotfix 9 (set_position bei gap>5s) — defensiver No-op, bleibt
- Hotfix 10 (Watchdog + Idle-Suppression) — Wurzel 2+4 gefixt
- Hotfix 12 (DIAG) — Verifikation

Hotfix 16 fügt sich als zusätzliche Schicht hinzu: streaming=True konstant.

---

## 7. Pending Decisions

- [ ] Spec-Approval → Phase 2 (Plan + TDD-Tests)
- [ ] Phase 4 (Code-Review): selbst oder code-reviewer Subagent?
- [ ] PR-Strategie: nach erfolgreichem HW-Test eigener Branch + PR

---

## 8. Iron-Law-Reminder

Wurzel 3 hat bereits 2 widerlegte Hotfix-Versuche (14, 15). Hotfix 16 ist der **dritte Versuch auf gleicher Wurzel** — IRON LAW STATUS KRITISCH.

**Pre-Commitment:** Wenn Hotfix 16 nach Hardware-Test nicht funktioniert (5 Runs ohne Crash-Verbesserung), KEIN Hotfix 17 ohne Architektur-Diskussion oder Maintainer-Issue.
