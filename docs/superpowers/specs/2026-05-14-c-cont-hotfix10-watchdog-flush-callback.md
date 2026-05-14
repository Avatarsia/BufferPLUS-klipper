# C-cont Hotfix 10 — P7-78 Watchdog Flush-Callback-Pfad

**Datum:** 2026-05-14
**Branch-Basis:** `feature/c-cont-streaming` HEAD `0ec5ea2` (Hotfix 9)
**Status:** Spec — pending User-Approval (User hat bereits "TDD-Workflow starten" gewählt)
**Wurzel-Analyse:** vollständige DIAG-Evidenz + Code-Review der P7-75 Watchdog-Logik

---

## 1. Problem-Statement

### Hardware-Crash 2026-05-14 (Hotfix 9 Deploy-Test)
```
Klipper-Boot 00:43, Klipper-Idle 4 Minuten, Print-Start 00:47.
Z.128: buffer_feeder: stepcompress cursor reset via set_position (forced_t0 path, gap=257.7s, Hotfix9)
Z.363: MCU 'LLL_PLUS' shutdown: Timer too close
```

Hotfix 9 (set_position bei gap>5s) **hat keinen Effekt** — `set_position(0,0,0)` updated nur den `itersolve.commanded_pos`, NICHT `stepcompress.last_step_clock`. Daher Timer-too-close trotz Hotfix 9.

### DIAG-Evidenz (Hotfix 8)
```
DIAG resume-edge: mcu_now=316.386 lme=58.647 gap=257.7s
                  step_gen=58.907 flush=58.907 hall_empty=True
```

Schlüssel-Beobachtung: 4 Minuten Klipper-Idle, `step_gen_time` und `flush_time` blieben beim Boot-Anchor-Wert. HALL3:on war die ganze Zeit aktiv (`buffer_metrics` zeigt `target_speed=15.0` durchgehend), ABER kein einziger Submit erfolgte → `_last_move_end_time` blieb stale.

### Code-Review der P7-75 Watchdog-Logik

`_main_tick` (Z.2325-2334):
```python
if (self._state in (STATE_IDLE, STATE_AUTO)
        and not self._stepper_synced_to
        and not self._pending_disable
        and not self._move_in_flight()
        and self._pending_remaining_mm == 0.0
        and not self._continuous_feed
        and not self.hall_empty       # ← BLOCKIERT BEI HALL3:on
        and not self.hall_full
        and not self._needs_overflow_prime
        and not _print_active):
```

**Test-Annahme** (`test_3_hall_empty_blocks_watchdog`):
> *"hall_empty=True means bang-bang has an open feed-request pending — watchdog stepping in here would race against the legitimate feed submit."*

**Problem:** Bei `use_flush_callback_bang_bang=True` (unser Setup) ist `_bang_bang_tick` ein No-Op (Z.2735: `if self.use_flush_callback_bang_bang: return`). Bang-Bang feuert **NUR** via `_on_mcu_flush`, der während Klipper-Idle gar nicht aufgerufen wird (keine motion_queuing-Aktivität). Die Race-Annahme ist im flush-callback-Pfad **falsch**.

---

## 2. Wurzel-Ursache

P7-75 Watchdog-Bedingung `not self.hall_empty` ist eine Schutzbedingung gegen Race mit Reactor-Tick-Bang-Bang. Sie **gilt nicht** im flush-callback-Pfad, wo Bang-Bang außerhalb von `_main_tick` läuft. Konsequenz:

**Henne-Ei-Problem im Klipper-Idle:**
- HALL3 aktiv → "wir sollten feeden"
- `use_flush_callback_bang_bang=True` → kein Reactor-Bang-Bang
- Klipper's motion_queuing idle → kein `_on_mcu_flush`
- P7-78 Watchdog → blockiert durch `not hall_empty`
- → **kein einziger Submit für minuten/stunden** → `last_step_clock` altert
- → Erster Submit nach Print-Start: massive Cursor-Differenz → `Timer too close`

---

## 3. Fix-Logik

### Konkrete Code-Änderung

`klipper_extras/buffer_feeder.py`, Z.2331:

```python
# alt (P7-75):
and not self.hall_empty

# neu (Hotfix 10):
# P7-75 Original-Begruendung: "watchdog stepping in here would race
# against legitimate bang-bang feed submit". Diese Race existiert
# NUR im Reactor-Tick-Bang-Bang-Pfad (use_flush_callback_bang_bang=
# False). Im flush-callback-Pfad ist _bang_bang_tick ein No-Op
# (Z.2735) — Bang-Bang feuert NUR via _on_mcu_flush, der waehrend
# Klipper-Idle gar nicht gerufen wird. Bei HALL3:on + Klipper-Idle
# in diesem Pfad: keine Submission, last_step_clock altert, erster
# Print-Start-Submit -> Timer too close (Hardware 2026-05-14 Z.363).
and not (self.hall_empty and not self.use_flush_callback_bang_bang)
```

### Sicherheits-Analyse

- **`not _continuous_feed`** bleibt als Sub-Gate aktiv → kein Race mit aktivem Stream
- **`not hall_full`** bleibt → kein Forward-Feed bei vollem Buffer
- **Neu erlaubter Pfad:** `hall_empty=True AND not _continuous_feed AND use_flush_callback_bang_bang=True`
  → genau der stale-Klipper-Idle-Zustand
- Anchor-Move ist sehr klein (0.05mm typisch) → kein Risiko für HALL1-Overshoot bei leerem Buffer

---

## 4. Tests

### Existierender Test anpassen

`test_3_hall_empty_blocks_watchdog` (P7-75 Test):
- Aktuelles Setup: kein expliziter `use_flush_callback_bang_bang`-Wert (default?)
- Anpassung: explizit `use_flush_callback_bang_bang=False` (klassischer Reactor-Tick-Pfad)
- Assertion bleibt: bei `hall_empty=True` UND `not use_flush_callback_bang_bang`: kein Watchdog-Submit

### Neuer Test

`test_c_cont_hotfix10_watchdog_fires_with_flush_callback_bangbang`:
- Setup: `use_flush_callback_bang_bang=True`, `hall_empty=True`, gap>idle_anchor_gap
- Expectation: Watchdog FEUERT (im flush-callback-Pfad gibt es keine Race-Alternative)

### Regression-Tests

`test_3b_hall_full_blocks_watchdog`: bleibt unverändert (hall_full-Schutz bleibt aktiv)
Alle anderen P7-75 / P7-78-Tests: bleiben unverändert

---

## 5. Erwartete Effekte

**Vor Hotfix 10:**
- Klipper-Idle → Watchdog blockiert durch `hall_empty=True` → `last_step_clock` altert
- Print-Start → erster `_on_mcu_flush` mit altem `step_gen_time` → Timer too close

**Mit Hotfix 10:**
- Klipper-Idle → Watchdog feuert alle ~10s einen 0.05mm Anchor-Submit
- `_last_move_end_time` bleibt aktuell, `step_gen_time` bleibt aktuell
- Print-Start → erster `_on_mcu_flush` mit frischem `step_gen_time` → kein Crash

---

## 6. Beziehung zu Hotfix 9

Hotfix 9 (set_position bei gap>5s im `_submit_single_trapezoid`) **bleibt im Code** als zweite Verteidigungslinie. Wenn Hotfix 10 perfekt funktioniert, wird der Hotfix-9-Pfad nie getroffen. Wenn Hotfix 10 eine Edge-Case übersieht, fängt Hotfix 9 sie als sauber-loggender Diagnose-Marker. Spätere Cleanup-PR kann Hotfix 9 entfernen.

---

## 7. Risiken

1. **Watchdog feuert während Buffer-Refill nach Filament-Wechsel:** Wenn der User nach Filament-Wechsel HALL3:on hat aber motion_queuing idle ist, würde Watchdog 0.05mm-Anchor-Submits machen — das ist gut (Buffer füllt langsam) und kein Risiko.
2. **Andere Setups mit `use_flush_callback_bang_bang=False`:** Unverändert (P7-75-Verhalten bleibt). Maintainer-cfg-default ist `False`? User-cfg ist `True` (PR #20 vom User selbst).
3. **Maintainer hat use_flush_callback_bang_bang spezifisches Verhalten nicht im Auge gehabt:** Wahrscheinlich richtig — die P7-75 Commit-Message zeigt dass Codex-Verify keine flush-callback-Asymmetrie erwähnt.

---

## 8. PR-Strategie

Hotfix 10 ist ein **starker Maintainer-PR-Kandidat**:
- Adressiert echtes Hardware-Problem mit deterministischer DIAG-Evidenz
- Sehr gezielter Eingriff (eine Bedingungs-Verfeinerung)
- Erklärung warum P7-75-Annahme im flush-callback-Pfad nicht greift
- User-Beobachtung "Timer-too-close besteht schon länger" wird strukturell erklärt

---

## 9. Pending Decisions

- [ ] Spec-Approval → Phase 2 (Plan)
- [ ] Hotfix 9 wirklich im Code lassen oder revertieren?
