# C-cont Hotfix 9 — forced_t0-Pfad Reprime-bei-Gap

**Datum:** 2026-05-14
**Branch-Basis:** `feature/c-cont-streaming` HEAD `257b011` (Hotfix 8 Diagnostic-Logs)
**Status:** Spec — pending User-Approval vor Phase 2 (Plan)
**Vorgänger:** Hotfix 7 (Soft-Throttle, hardware-validiert mechanisch) + Hotfix 8 (DIAG-Logs)

---

## 1. Problem-Statement

### Hardware-Crash 2026-05-14 (Hotfix 8 Diagnostic-Run)
Print-Start nach längerer Klipper-Idle-Zeit. Klippy.log Z.250:
```
MCU 'LLL_PLUS' shutdown: Timer too close
shutdown clock=2383899598 static_string_id=Timer too close
```

### DIAG-Log-Evidenz (Hotfix 8 lieferte den Smoking Gun)

Direkt vor Crash (Z.16-17 im neuen Log):

```
buffer_feeder DIAG resume-edge:
  target=15.00 last_target=0.00 mcu_now=49.382 lme=12.705 gap=36.676s
  en_sched=12.560 primed=True step_gen=12.966 flush=12.966
  move_in_flight=False pending_mm=0.00 vel_ready=True vel=0.00

buffer_feeder DIAG submit:
  forced_t0=13.086 t0=49.502 mcu_now=49.382 lme=12.705 en=49.502
  was_primed=True stale_anchor=True need_reprime=False streaming=False
  dist=5.00 speed=15.00 cmd_pos=0.05
```

### User-Beobachtung (historischer Kontext)
- **Lang bestehendes Problem** (vor C-cont-Hotfix-Serie)
- Triggert **sporadisch direkt beim Druckstart**, BEVOR Heizung oder Bewegung beginnt
- **Klipper-Restart vor Druckstart hilft** (frischer motion_queuing-Anchor)
- C-cont macht es häufiger sichtbar (kontinuierliche flush-Callbacks)

### Was Hotfix 7 / Hotfix 8 zeigten
- Hotfix 7 (Soft-Throttle) **eliminierte erfolgreich** die mechanische HALL1-Storm-Wurzel
- Hotfix 8 DIAG-Logs **deterministisch identifizierten** die verbleibende Wurzel: eine **separate, langjährige Code-Lücke**, nicht spezifisch zu C-cont

### Validierung der Wurzel-Hypothese (2026-05-14)
User bestätigte: Klipper-Restart + sofortiger Print-Start → kein Crash, Print läuft. Das bestätigt deterministisch dass die Wurzel **Klipper-Idle-Zeit zwischen Boot und Print-Start** ist, nicht Heatsoak oder Hotfix-7-Logik.

---

## 2. Wurzel-Ursache

### Code-Lokation
`klipper_extras/buffer_feeder.py`, Funktion `_submit_single_trapezoid`, Z.3537:
```python
if forced_t0 is None:
    need_reprime = (not self._stepcompress_primed) or (gap > REPRIME_GAP)
else:
    need_reprime = not self._stepcompress_primed
```

### Die Lücke
Der **forced_t0-Pfad** (Z.3539) prüft **NUR** `_stepcompress_primed`. Der gap-basierte Reprime-Trigger (`gap > REPRIME_GAP=5.0s`) wird **nicht angewendet**.

### Code-Kommentar-Hintergrund (P7-52-Original-Design)
```python
# Flush-Callback-Pfad (forced_t0 gesetzt):
#   - flush_step_generation() NIEMALS aufrufen (ReactorError, weil
#     reactor.pause() innerhalb von assert_no_pause verboten ist).
#   - set_position() NUR wenn not-primed (d.h. nach Stepper-Disable,
#     z.B. nach OVERFLOW). Einmaliger Cursor-Reset ist sicher und
#     noetig. Bei primed=True: kein Aufruf (Schutz gegen rapide
#     SET_VELOCITY_LIMIT-Flush-Callbacks die Cursor korrumpieren).
#   - Gap-basierter Reprime entfaellt: step_gen_time ist der Anker.
```

**Die Annahme "step_gen_time ist der Anker" stimmt im Normalfall.** Aber sie versagt im folgenden Edge-Case:

### Wann der Edge-Case triggert

1. Klipper bootet, macht boot_feed (kleiner 0.05mm-Anchor-Push)
2. `_last_move_end_time = ~12.7s`, `_stepcompress_primed = True`
3. Klipper läuft idle (User-Browser-Idle, kein Print)
4. **Klipper's motion_queuing flusht nicht** während Idle → `step_gen_time` bleibt beim Boot-Anchor-Wert (12.966)
5. User startet Print → Moonraker schickt Print-Start
6. **Erster `_on_mcu_flush`-Aufruf mit `step_gen_time=12.966`** (alt!)
7. `forced_t0 = step_gen_time + lead_time = 13.086` → in der Vergangenheit
8. In `_submit_single_trapezoid`:
   - `gap = mcu_now - _last_move_end_time = 36.676s` → über REPRIME_GAP
   - **`need_reprime = False`** weil `_stepcompress_primed=True`
   - `t0 = max(forced_t0, lme, en, mcu_now) = en = 49.502`
9. **Trapezoid mit t0=49.502 wird submittet, aber `last_step_clock` ist bei ~12.7s**
10. Differenz 36.8s > `CLOCK_DIFF_MAX = 16.78s` @ 48MHz → MCU: **Timer too close**

### Warum die anderen Defense-Patches nicht greifen

| Patch | Funktion | Warum nicht aktiv hier |
|---|---|---|
| P7-72 (stale_anchor) | Forciert en-floor wenn `lme<=mcu_now` | Greift **danach** in der `en`-Logik, aber rettet t0 nicht vor zu großem Cursor-Sprung |
| P7-73 (forced_t0 clamp) | Cappt `forced_t0 > mcu_now + 2.0s` | forced_t0 ist hier in Vergangenheit, nicht Zukunft |
| P7-77B (anchor skip) | Skippt `th_time > mcu_now + 2.0s` | Nur im `forced_t0=None`-Pfad |
| P7-78 (Watchdog) | Auto-Anchor alle 10s | Feuert nur in `_main_tick` während `state=AUTO` — vor Print-Start nicht aktiv |

---

## 3. Fix-Logik

### Konkrete Code-Änderung

`klipper_extras/buffer_feeder.py`, Z.3537:

```python
# alt (Hotfix 0...8):
if forced_t0 is None:
    need_reprime = (not self._stepcompress_primed) or (gap > REPRIME_GAP)
else:
    need_reprime = not self._stepcompress_primed

# neu (Hotfix 9):
need_reprime = (not self._stepcompress_primed) or (gap > REPRIME_GAP)
```

**Erklärung:** Die beiden Branches werden vereinheitlicht. Im Reprime-Block sind die Aktionen für `forced_t0!=None` bereits korrekt gegated:
```python
if need_reprime:
    if forced_t0 is None:
        try:
            toolhead = self.printer.lookup_object('toolhead')
            toolhead.flush_step_generation()
            ...
    self.stepper.set_position((0., 0., 0.))
    self._commanded_pos = 0.0
    self._stepcompress_primed = True
```

`flush_step_generation()` läuft nur im `forced_t0=None`-Pfad — bleibt unverändert sicher.
`set_position(0,0,0)` + `_commanded_pos=0` im `forced_t0`-Pfad ist **sicher** (kein Reactor-Pause-Problem, kein gefährlicher Cursor-Reset weil Cursor sowieso obsolet ist nach 5s Gap).

### Warum diese Lösung sicher ist

1. **`flush_step_generation()`** bleibt im `forced_t0=None`-Branch gegated → keine Reactor-Pause-Race
2. **`set_position(0,0,0)`** setzt den stepcompress-Cursor auf 0 → next step bei t0=49.5s ist konsistent mit Position 0
3. **`_commanded_pos=0`** hält Klipper's Position-Tracking konsistent
4. **`_stepcompress_primed=True`** wird nochmal explizit gesetzt (ist es schon, aber idempotent)
5. **Bei normaler Operation** (gap<5s) ist `need_reprime=False`, kein Reprime, identisches Verhalten zu vorher
6. **Bei Edge-Case** (gap>5s mit forced_t0): Cursor-Reset rettet vor MCU-Crash, neuer Move startet sauber

### Code-Kommentar (Maintainer-Stil Regel #8)

```python
# Hotfix9 (Hardware 2026-05-14 klippy.log Z.250 + DIAG-Beleg
# Z.16-17, langjaehriger sporadischer Timer-too-close beim
# Druckstart). Wurzel: forced_t0-Pfad ignorierte gap-basierten
# Reprime-Trigger, mit der Annahme "step_gen_time ist der
# Anker". Diese Annahme versagt wenn Klipper's motion_queuing
# zwischen Boot-Anchor und Print-Start nicht flusht — step_gen_-
# time bleibt beim Boot-Wert, der erste Print-Submit kommt mit
# step_gen_time 10-60s alt. Cursor (last_step_clock) bei alter
# Position, neuer Step bei mcu_now → CLOCK_DIFF_MAX (~16.78s
# @ 48MHz) ueberschritten → MCU 'Timer too close'.
#
# Fix: gap-basierter Reprime-Trigger auch im forced_t0-Pfad.
# set_position(0,0,0) im Reprime-Block ist im forced_t0-Pfad
# sicher (kein flush_step_generation -> kein Reactor-Pause).
# Cursor wird auf 0 reset, neuer Move startet sauber bei
# konsistenter Position. Bei normalem Betrieb (gap<5s) keine
# Verhaltensaenderung.
#
# User-Beobachtung: Problem besteht schon vor C-cont-Serie,
# sporadisch beim Druckstart nach laengerer Klipper-Idle-Zeit.
# Klipper-Restart half temporaer. Hotfix 9 schliesst die Luecke
# strukturell.
```

---

## 4. Tests

### Reproduktion in Unit-Test

```python
def test_c_cont_hotfix9_forced_t0_gap_triggers_reprime(monkeypatch):
    """Hotfix9: forced_t0-Pfad mit gap > REPRIME_GAP triggert
    set_position(0,0,0) + _commanded_pos=0 + _stepcompress_primed
    bleibt True. Kein flush_step_generation (Reactor-Safe).
    Hardware 2026-05-14 Z.16-17: gap=36.676s, primed=True,
    crash Timer too close."""
    printer, feeder = make_c_cont_feeder(monkeypatch)
    # Simuliere Boot-Anchor lange in Vergangenheit
    feeder._last_move_end_time = 12.7
    feeder._stepcompress_primed = True
    feeder._commanded_pos = 0.05  # boot_feed
    # Mock mcu.estimated_print_time auf 49.4 (mcu_now)
    ...
    # Mock set_position um Aufruf abzufangen
    set_position_calls = []
    monkeypatch.setattr(feeder.stepper, 'set_position',
                        lambda p: set_position_calls.append(p))
    # forced_t0-Submit mit gap=36.7s
    feeder._submit_single_trapezoid(5.0, 15.0, forced_t0=13.086)
    # set_position muss aufgerufen worden sein (Reprime)
    assert len(set_position_calls) == 1
    assert set_position_calls[0] == (0., 0., 0.)
    # _commanded_pos auf 0 zurueckgesetzt + Move addiert
    assert feeder._commanded_pos == 5.0  # 0 + dist
```

### Regression: forced_t0-Pfad mit gap<5s

```python
def test_c_cont_hotfix9_forced_t0_small_gap_no_reprime(monkeypatch):
    """Hotfix9 Regression: forced_t0 + gap<REPRIME_GAP -> KEIN
    Reprime, normale Submit-Logik unveraendert."""
    printer, feeder = make_c_cont_feeder(monkeypatch)
    feeder._last_move_end_time = 48.0  # gap=1.4s
    feeder._stepcompress_primed = True
    feeder._commanded_pos = 100.0
    # Mock mcu_now=49.4
    ...
    set_position_calls = []
    monkeypatch.setattr(feeder.stepper, 'set_position',
                        lambda p: set_position_calls.append(p))
    feeder._submit_single_trapezoid(5.0, 15.0, forced_t0=49.0)
    # Kein set_position weil gap<5s
    assert len(set_position_calls) == 0
    # _commanded_pos addiert (kein Reset)
    assert feeder._commanded_pos == 105.0
```

### Regression: forced_t0=None-Pfad unveraendert

```python
def test_c_cont_hotfix9_legacy_path_unchanged(monkeypatch):
    """Hotfix9 Regression: forced_t0=None-Pfad (Reactor-Tick)
    mit gap>5s muss flush_step_generation aufrufen wie vorher."""
    ...
```

---

## 5. Risiken und Mitigation

### Risiko 1: Cursor-Reset während aktivem Stepcompress-Submit

`set_position(0,0,0)` reset den stepcompress-Cursor. Wenn zwischen Reset und neuem Submit eine andere Step-Quelle auf denselben Stepper schreibt, könnte das problematisch sein.

**Mitigation:** Unser Buffer-Stepper hat einen **eigenen Trapq** und ist **NICHT** sync'd zum Extruder während AUTO-Phase. Stepcompress-Cursor ist exklusiv. Risiko gering.

**Defense:** Bestehender P7-69-Guard (`self._stepper_synced_to is not None → return`) verhindert ohnehin Reprime im SYNC-Modus.

### Risiko 2: Veränderter Boot-Pfad

Boot-Sequenz nutzt `_stepcompress_primed=False` initial. Erster Submit triggert Reprime. Mit Hotfix 9 würde ALSO `not _stepcompress_primed=True → need_reprime=True` — identisches Verhalten zu vorher.

### Risiko 3: Häufige Reprimes bei pathologischem Idle-Pattern

Wenn der Buffer regelmäßig >5s pausiert (z.B. niedrigster Flow + Buffer voll), könnten viele Reprimes feuern. Jeder ist O(1), aber:
- `set_position` + `_commanded_pos=0` ist billig
- Kein `flush_step_generation` → kein Reactor-Pause
- Cursor-Reset ist nicht-destruktiv (alte Moves bleiben unberührt im trapq)

**Acceptable.** Wenn das zukünftig zum Problem wird: Schwellwert anpassen oder Tracking-Variable für "letzter Reprime-Zeitpunkt" einführen.

### Risiko 4: Interaktion mit Hotfix 7 Soft-Throttle

Hotfix 7 macht `target_speed=0` häufiger (Zwischenzone mit vel<MIN_FLOOR). Wenn Pausen >5s entstehen, würde Hotfix 9 jedes Mal Reprime auslösen. Aber das ist genau **korrekt** — diese Reprimes sind die fehlende Defense gegen den Crash-Pfad.

---

## 6. Erwartete Effekte nach Hardware-Test

1. **Timer-too-close beim Druckstart eliminiert**, auch nach langer Klipper-Idle-Zeit
2. **c=3 i=0 Invalid sequence eliminiert** (gleiche Wurzel, andere Symptom-Manifestation)
3. **Hotfix 8 DIAG-Logs zeigen** dass Reprime-Pfad bei großen Gaps korrekt feuert (DIAG `need_reprime=True` statt `False`)
4. **Print-Verhalten in normalen Drucken unverändert** (gap<5s → kein Reprime → identisches Verhalten)

---

## 7. Pending Decisions

- [ ] Spec-Approval → Phase 2 (Plan)
- [ ] Phase 4 Code-Review: code-reviewer Subagent (Standard) oder selbst (kleine Aenderung <10 LOC)?
- [ ] PR-Strategie: Hotfix 9 ist ein **wertvoller Maintainer-PR-Kandidat** weil:
  - Adressiert ein lang bestehendes, sporadisches Problem
  - Hat DIAG-Log-Evidenz mit konkreten State-Werten
  - Fix ist minimal (eine Zeile + bestehender Reprime-Block)
  - User-Erfahrung "Klipper-Restart hilft" als historisches Indiz
