# C-cont Hotfix 8 — Diagnostic Logs (KEIN Fix, reine Instrumentation)

**Datum:** 2026-05-14
**Branch-Basis:** `feature/c-cont-streaming` HEAD `eede1f8` (Hotfix 7)
**Status:** Plan freigegeben → Implementation
**Ziel:** Crash-Pfad `stepcompress c=3 i=0` bei Pause<5s + Restart-Race aus Hotfix 7 Hardware-Test 2026-05-14 instrumentieren

---

## Problem-Beobachtung (aus Hotfix-7-HW-Test)

Crash bei `print_time=859.122` mit `c=3 i=0`. Letzte 10s zeigen Pipeline-Hopping:
```
print_time=858.245: target=0 (Zwischenzone, vel<MIN_FLOOR)
print_time=859.122: target=15 (HALL3:on) → CRASH
```

Existierende Defense-Patches (P7-72/73/77B/78) **greifen NICHT** — Gap=1s < REPRIME_GAP=5s. Wurzel ist eine **separate Lücke** in der Anchor-Logik beim Pause<5s + Restart.

## Diagnose-Ziel

Den **exakten State zum Crash-Zeitpunkt** loggen:
- `mcu_now`, `_last_move_end_time`, gap, `_last_enable_schedule_time`
- `_stepcompress_primed`, berechneter `forced_t0`, resultierender `t0`
- `was_primed`, `stale_anchor`, `need_reprime`, `en`-Entscheidung
- Tracker-Zustand

## Code-Änderungen

### Datei: `klipper_extras/buffer_feeder.py`

**1. Instance-Variable in `BufferFeeder.__init__`:**
```python
# Hotfix8: Track last target_speed for pause-resume-edge diagnostic
self._last_target_speed = 0.0
```

**2. Im `_on_mcu_flush`, nach `target_speed = self._compute_target_feed_speed()`:**
```python
# Hotfix8 Diagnostic: log full state at pause-resume edge (target 0 -> >0).
# Kein Fix, nur Evidenz-Sammlung fuer Crash-Pfad-Analyse.
if target_speed > 0.0 and self._last_target_speed == 0.0:
    mcu = self.stepper.get_mcu()
    mcu_now_diag = mcu.estimated_print_time(self.reactor.monotonic())
    gap_diag = mcu_now_diag - self._last_move_end_time
    vel_ready = self.velocity_tracker.is_ready()
    vel = self.velocity_tracker.get_velocity() if vel_ready else 0.0
    logging.info(
        "buffer_feeder DIAG resume-edge: target=%.2f last_target=%.2f "
        "mcu_now=%.3f lme=%.3f gap=%.3fs en_sched=%.3f primed=%s "
        "step_gen=%.3f flush=%.3f move_in_flight=%s pending_mm=%.2f "
        "vel_ready=%s vel=%.2f",
        target_speed, self._last_target_speed,
        mcu_now_diag, self._last_move_end_time, gap_diag,
        self._last_enable_schedule_time, self._stepcompress_primed,
        step_gen_time, flush_time, self._move_in_flight(),
        self._pending_remaining_mm, vel_ready, vel)
self._last_target_speed = target_speed
```

**3. Im `_submit_single_trapezoid`, nach `t0`-Berechnung, vor `trapq_append`:**
```python
# Hotfix8 Diagnostic: log resolved anchor decision (forced_t0-Pfad nur,
# da Crash-Pfad ueber _on_mcu_flush kommt). Gated auf buffer_debug_-
# metrics um Log-Spam in nicht-Debug-Sessions zu vermeiden.
if forced_t0 is not None and self.buffer_debug_metrics:
    logging.info(
        "buffer_feeder DIAG submit: forced_t0=%.3f t0=%.3f mcu_now=%.3f "
        "lme=%.3f en=%.3f was_primed=%s stale_anchor=%s need_reprime=%s "
        "streaming=%s dist=%.2f speed=%.2f cmd_pos=%.2f",
        forced_t0, t0, mcu_now, self._last_move_end_time, en,
        was_primed, stale_anchor, need_reprime, streaming,
        signed_distance, speed, self._commanded_pos)
```

## Verbindlichkeit Regel #8 (Inline-Doku)

Beide Log-Blöcke bekommen Patch-Identifier `# Hotfix8 Diagnostic:` und Kommentar warum es **KEIN Fix** ist, sondern Evidenz-Sammlung.

## Tests

**Keine neuen Tests.** Pure Logging ändert kein Verhalten. Full-Suite muss weiterhin 406 grün laufen (Regression-Check).

## Tasks

- [ ] Step 1: Instance-Variable in `__init__` hinzufügen
- [ ] Step 2: Diagnose-Block 1 in `_on_mcu_flush` einbauen
- [ ] Step 3: Diagnose-Block 2 in `_submit_single_trapezoid` einbauen
- [ ] Step 4: `pytest tests/` — 406 grün erwartet (keine Verhaltensänderung)
- [ ] Step 5: Commit im Fork `Hotfix 8 Diagnostic` (eigener Commit, NICHT amend in Hotfix 7)
- [ ] Step 6: Push in Fork
- [ ] Step 7: Upload buffer_feeder.py auf Drucker + MD5
- [ ] Step 8: Klipper-Restart
- [ ] Step 9: klippy.log truncieren
- [ ] Step 10: **Druckbett-Frage** an User
- [ ] Step 11: Hardware-Test (Feedertest.gcode)
- [ ] Step 12: Log-Analyse — `grep DIAG` für Crash-Zeitpunkt-State

## Post-Test

Egal ob Crash oder Erfolg:
- **Crash:** DIAG-Logs zeigen exakten State → Hotfix 9 als gezielter Fix möglich (mit klarer Wurzel)
- **Kein Crash:** Heisenbug oder Logs-Side-Effect (unwahrscheinlich aber möglich). Logs entfernen + nochmal testen.

In keinem Fall verbleiben die DIAG-Logs permanent — entweder ersetzt durch echten Fix in Hotfix 9, oder entfernt nach Erkenntnis-Gewinn.

## Kein Upstream-PR

Hotfix 8 ist **reine lokale Diagnose**. Kein PR zum Maintainer. Nach Erkenntnis-Gewinn rauspatchen oder durch Hotfix-9-PR ersetzen.
