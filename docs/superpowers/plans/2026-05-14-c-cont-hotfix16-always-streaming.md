# C-cont Hotfix 16 — Always-Streaming (Implementation Plan)

> **TDD-Workflow (Regel #7):** Tests ZUERST. RED → GREEN → Full-Suite grün.

**Spec:** `docs/superpowers/specs/2026-05-14-c-cont-hotfix16-always-streaming.md`
**Branch-Basis:** `feature/c-cont-streaming` HEAD `4141aec`
**Ziel-LOC:** ~10 Zeilen Code in `_on_mcu_flush`, ~5 neue Tests

---

## Tasks

### Task 1 — Tests schreiben (RED erwartet)

5 neue Tests in `tests/test_c_cont_streaming.py`:

1. `test_c_cont_hotfix16_always_streaming_when_no_move_in_flight` — submit hat streaming=True wenn move_in_flight=False
2. `test_c_cont_hotfix16_streaming_when_move_in_flight` — Regression: streaming=True wenn move_in_flight=True
3. `test_c_cont_hotfix16_ensures_enable_when_not_primed` — Mitigation 1: _enable_stepper bei primed=False
4. `test_c_cont_hotfix16_ensures_enable_when_pending_disable` — Mitigation 1: _enable_stepper bei _pending_disable=True
5. `test_c_cont_hotfix16_no_enable_when_already_active` — Regression: keine doppelten enable-Calls bei normalem Submit

### Task 2 — pytest RED

```bash
python -m pytest tests/test_c_cont_streaming.py -v -k "hotfix16"
```
Erwartung: Mindestens 1-2 Tests FAIL (streaming-Test sollte fail weil aktueller Code streaming=move_active nutzt).

### Task 3 — Code implementieren

`klipper_extras/buffer_feeder.py`, in `_on_mcu_flush` direkt vor `_submit_move`:

```python
# C-cont Hotfix 16 — C1: Always-Streaming Mode
# (Hardware 2026-05-14 Runs 2-5 c=N i=0 Invalid sequence nach
# 4.8-7.2 min Druckzeit, Wurzel: Submit-Mode-Wechsel)
# Mitigation 1: Stepper-Enable sicherstellen vor streaming-Submit
if (not self._stepcompress_primed
        or self._pending_disable
        or self._stepper_enable is None):
    self._enable_stepper()
self._submit_move(
    self.interrupt_chunk_mm,
    target_speed,
    forced_t0=anchor,
    streaming=True,  # C1: konstant
    submit_chunk_cap=self.interrupt_chunk_mm)
```

### Task 4 — pytest GREEN

Erwartung: alle 5 neuen Tests grün.

### Task 5 — Full-Suite

`pytest tests/` muss 417 passed (412 + 5 neue) zeigen, 0 Regressionen.

### Task 6 — Commit + HW-Test

Erst nach Tests grün:
1. Commit im Fork
2. Push
3. Upload + MD5
4. Klipper-Restart
5. Druckbett-Frage
6. Print starten + beobachten

---

## Pending User-Approval

- [x] Spec freigegeben
- [ ] Plan freigegeben → Phase 3 starten?
