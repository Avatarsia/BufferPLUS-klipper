# High-Flow Buffer-Architektur — Design-Spec

**Datum:** 2026-05-13
**Stable Baseline:** `python-ansatz` HEAD `03d4f2f` (P7-79)
**Test-Branches:** `feature/c-cont-streaming` + `feature/c-pred-adaptive`
**Status:** Design genehmigt, Implementation pending

---

## 1. Problem-Statement

### Beobachtungen
- **User (2026-05-13):** Bei ~18 mm³/s Flow sieht der Druckkopf Gedenkzeiten während der Buffer nachfördert. Buffer-Arm pendelt im unteren Drittel (HALL3-Bereich), erreicht HALL2 nie. Bei 30+ mm³/s kommt der Buffer nicht mehr zum Fördern — Arm bleibt permanent in HALL3. TMC2208-Drehmoment-Limit ausgeschlossen (Toolhead-Extruder zieht durch dieselbe Mechanik mehr).
- **Eifel-Joe (P7-78-Log):** Bei ~24 mm³/s: 10 OVERFLOW-Cycles in 810s Print, 0 HALL2-Events, Buffer-Arm pendelt HALL3 → HALL1 ohne stabile HALL2-Position. P7-77 B SKIP-Pfad blockiert 85% der Anchor-Versuche während Print (78/92).

### Wurzel-Ursachen
1. **HALL2-Bouncing**: Sub-Chunks (9mm) katapultieren den Arm in einem Schwung von HALL3 nach HALL1, HALL2 wird nur kurz gestreift, nie als "stable voll" erkannt.
2. **OVERFLOW-Cycle-Overhead**: Jeder Cycle (HALL1→OVERFLOW→IDLE→AUTO) = 250-700ms nicht-förderbare Zeit. Bei rapid cycling: bis zu 15s Totzeit pro 800s Print.
3. **STABLE_DROP_GRACE (0.5s)** wirft `_continuous_feed=True` weg bei jedem Zwischenzonen-Bounce → Streaming-Lookahead-Pipeline wird ständig neu initialisiert.
4. **Bang-Bang-Architektur reaktiv statt prädiktiv**: Buffer wartet auf HALL-Edges, statt Extruder-Verbrauch zu antizipieren. Bei hohem Flow ist die Reaktionszeit zu langsam.

### Ziel
- Stabile Förderung bei 30, 50, **70 mm³/s** (Volcano-Hotend, 1.75mm Filament, Bowden ~2m)
- Keine Druckkopf-Pausen (kein `flush_step_generation()` im Streaming-Pfad)
- Erhaltene Crash-Schutzschilder (P7-66..P7-79)

---

## 2. Architektur-Optionen — Vergleich

### Heutiger Stand (`03d4f2f`)
```
HALL3 active → _continuous_feed=True → Streaming-Submits (45mm Chunks, 9mm Sub-Chunks)
HALL2 active → _continuous_feed=False
HALL1 active → STATE_AUTO → OVERFLOW → IDLE → AUTO (Cycle 250-700ms)
Zwischenzone >0.5s → _continuous_feed=False
```

### Variante A: C-cont (Continuous-Streaming + HALL-Modulation)

**Konzeptionell**: Buffer läuft im AUTO **dauerhaft** im Streaming-Mode. HALL-Sensoren modulieren nur `feed_speed`. Keine OVERFLOW-Cycles im normalen Betrieb.

```
AUTO_STREAM + HALL3 stable → speed_target = max_feed_speed (auffüllen)
AUTO_STREAM + Zwischenzone → speed_target = extruder_velocity (Steady-State Balance)
AUTO_STREAM + HALL2 stable → speed_target = 0.5x extruder_velocity (langsam zur HALL1-Vermeidung)
AUTO_STREAM + HALL1 → speed_target = 0 (Notbremse, OHNE State-Wechsel)
AUTO_STREAM + HALL1-Persist >2s → State → AUTO_HALT (Hardware-Safety)
```

### Variante B: C-pred (Predictive + Bang-Bang behalten)

**Konzeptionell**: Behält die Bang-Bang-State-Machine, aber `feed_speed` wird **dynamisch** an Extruder-Velocity angepasst.

```
State-Machine: IDLE ↔ AUTO ↔ OVERFLOW (unverändert)
Velocity-Tracker im _main_tick (50Hz): misst E-Position-Delta → mm/s linear
Submit-Pfad: feed_speed = max(config_feed_speed, extruder_velocity × safety_factor)
              capped at max_feed_speed_hardware_limit
```

### Vergleichsmatrix

| Aspekt | C-cont | C-pred |
|--------|--------|--------|
| Erwartete Throughput-Grenze | 70+ mm³/s | 30-50 mm³/s |
| Code-Komplexität | Mittel-Groß | Klein-Mittel |
| Risiko bestehende Tests | 5-15 Tests anpassen | Tests bleiben grün |
| Hardware-Regression-Risiko bei niedrigem Flow | Mittel | Niedrig |
| Druckkopf-Pause-Risiko | 0 (Read-Only) | 0 (Read-Only) |
| Eifels c=14-Crash (P7-79) | Strukturell eliminiert | P7-79b vorhanden, aktiv |

---

## 3. State-Machine + Daten-Fluss

### C-cont — State-Machine

```
IDLE ←→ AUTO_STREAM ←→ AUTO_HALT (HALL1-Persist >2s, Hardware-Safety)
         ↕
         MANUAL/LOAD_PHASE_1-3/UNLOAD_* (unverändert)
```

- **AUTO_STREAM (ersetzt AUTO)**: `_continuous_feed=True` permanent, Submit-Loop läuft kontinuierlich in `_on_mcu_flush`. HALL-Edges modulieren nur die Speed.
- **AUTO_HALT (ersetzt OVERFLOW als Default-Reaktion)**: nur bei HALL1-Persist >`HALL1_PERSIST_TIMEOUT=2.0s` — d.h. Modulation auf Speed=0 reichte nicht und der Buffer ist mechanisch stuck.
- **OVERFLOW als State eliminiert** im AUTO-Pfad. Bleibt nur für SAFETY_DISTANCE/SUPPLY_JAM-Reaktionen (außerhalb AUTO).

### C-pred — State-Machine

```
IDLE ←→ AUTO ←→ OVERFLOW (alles unverändert)
         ↕
         MANUAL/LOAD/UNLOAD (unverändert)
```

- State-Machine 1:1 wie `03d4f2f`
- Neuer Component: `ExtruderVelocityTracker`
- Im Submit-Path: `feed_speed` durch Tracker-Output ersetzt

### Daten-Fluss

**C-cont:**
```
[Klipper Extruder.last_position]
        ↓ (passive read, 40Hz im _main_tick)
[ExtruderVelocityTracker]
        ↓ (sliding avg über 300ms → mm/s linear)
[SpeedModulator]
        ↓ (HALL-State + extruder_velocity → target_feed_speed)
[_on_mcu_flush Continuous Submit-Loop]
        ↓
[Buffer-Stepper Trapq] — eigener Trapq, getrennt vom Toolhead
```

**C-pred:**
```
[Klipper Extruder.last_position]
        ↓ (passive read, 40Hz)
[ExtruderVelocityTracker]
        ↓
[bestehender _on_mcu_flush Bang-Bang Pfad]
   ↓ (speed Override bei _continuous_feed=True)
[_submit_move mit dynamischer speed]
        ↓
[Buffer-Stepper Trapq]
```

---

## 4. Komponenten

### ExtruderVelocityTracker (beide Varianten gemeinsam)

```python
class ExtruderVelocityTracker:
    """Read-only passive tracker for extruder velocity.

    Uses extruder.get_status(eventtime)['position'] — no flush_step_generation,
    no SYNC, no lockstep with toolhead pipeline. Pure observer pattern.
    """
    def __init__(self, owner, printer, *,
                 sample_interval=0.025,    # 40Hz Sampling
                 window_size=0.3,           # 300ms gleitender Mittelwert
                 filament_diameter=1.75):
        self.owner = owner
        self.printer = printer
        self.sample_interval = sample_interval
        self.window_size = window_size
        self.cross_section = math.pi * (filament_diameter / 2.0) ** 2
        self.samples = collections.deque(
            maxlen=max(2, int(window_size / sample_interval)))
        self._extruder = None  # lazy lookup
        self._last_sample_time = 0.0

    def tick(self, eventtime):
        if eventtime - self._last_sample_time < self.sample_interval:
            return
        ext = self._get_extruder()
        if ext is None:
            return
        status = ext.get_status(eventtime)
        position = status.get('position', 0.0)
        self.samples.append((eventtime, position))
        self._last_sample_time = eventtime

    def get_velocity(self):
        """Returns linear filament velocity (mm/s, non-negative)."""
        if len(self.samples) < 2:
            return 0.0
        (t0, p0), (t1, p1) = self.samples[0], self.samples[-1]
        dt = t1 - t0
        if dt < 1e-6:
            return 0.0
        return max(0.0, (p1 - p0) / dt)

    def get_volumetric_flow(self):
        """Returns volumetric flow (mm³/s)."""
        return self.get_velocity() * self.cross_section

    def is_ready(self):
        return len(self.samples) == self.samples.maxlen
```

### C-cont — SpeedModulator

```python
def _modulate_target_speed(self):
    """Returns target feed_speed (mm/s) based on HALL state + extruder velocity."""
    if self.hall_overflow:  # HALL1 active
        return 0.0  # Notbremse via speed=0, ohne State-Wechsel
    extruder_vel = self.velocity_tracker.get_velocity()
    if self.hall_empty:  # HALL3 active = Buffer leer
        return self.max_feed_speed  # max-rate auffüllen
    if self.hall_full:  # HALL2 active = Buffer voll
        return 0.5 * extruder_vel  # langsamer als Verbrauch
    # Zwischenzone: balance auf Extruder-Verbrauch
    if self.velocity_tracker.is_ready():
        return extruder_vel
    return self.config_feed_speed  # Fallback bei nicht-ready Tracker
```

### C-pred — SpeedOverride

```python
def _compute_effective_feed_speed(self):
    """Returns feed_speed for current submit (mm/s)."""
    if not self.velocity_tracker.is_ready():
        return self.feed_speed  # Fallback auf config
    extruder_vel = self.velocity_tracker.get_velocity()
    safety_factor = self.buffer_predict_factor  # cfg-Param, default 1.2
    scaled = extruder_vel * safety_factor
    return min(max(self.feed_speed, scaled), self.max_feed_speed_cap)

# Neue cfg-Params (Standard-Klipper-Pattern via config.getfloat):
#   buffer_predict_factor: float = 1.2    # Sicherheits-Reserve über Extruder
#   max_feed_speed_cap: float = 100.0     # Hardware-Limit, Stepper-bedingt
# Beide via BUFFER_SET zur Laufzeit anpassbar.
```

---

## 5. Diagnostik-Logs (cfg-Param `buffer_debug_metrics=true`)

Im `_main_tick` (alle 1s) als Stats-Output:

```
buffer_metrics: state=AUTO_STREAM hall=[H3:on H2:off H1:off]
                tracker_vel=14.8mm/s flow=35.6mm³/s tracker_ready=True
                target_speed=15.2mm/s last_submit_speed=14.8mm/s
                pending_remaining=4.2mm last_chunk_age=0.024s
                stepper_total_mm=2847.3 ext_total_mm=2841.1
                stepper_vs_ext_ratio=1.002 (target>=1.0)
```

Pro Submit-Event (DEBUG-level):
```
buffer_submit: chunk=9.0mm speed=14.8mm/s t0=2347.124 mode=streaming
               accel_time=0.0148s cruise_time=0.575s decel_time=0.0148s
               total=0.605s effective_rate=14.88mm/s
```

OVERFLOW-Trigger (C-cont nur Hardware-Safety):
```
buffer_overflow_safety: hall1 persisted 2.1s > HALL1_PERSIST_TIMEOUT
                       last submitted speed=0.0 for 1.5s
                       buffer mechanically stuck — entering AUTO_HALT
```

Velocity-Tracker-Debug (DEBUG-level, alle 5s):
```
buffer_tracker: samples=12/12 window=300ms current_vel=14.8mm/s
                first=(t=2346.8, pos=42100.2) last=(t=2347.1, pos=42104.6)
                cross_section=2.405mm² volumetric=35.6mm³/s
```

---

## 6. Test-Strategie

### Unit-Tests gemeinsam — `tests/test_velocity_tracker.py`

- `test_tracker_zero_initial`
- `test_tracker_steady_state` (12 Samples, konstante Δposition)
- `test_tracker_velocity_step` (Lag-Verhalten verifizieren)
- `test_tracker_retract_clamped` (negative Δposition → 0)
- `test_tracker_volumetric_calc` (10 mm/s × 2.405 ≈ 24.05 mm³/s)
- `test_tracker_is_ready_after_window`

### C-cont — `tests/test_c_cont_streaming.py`

- `test_c_cont_continuous_streaming_in_auto`
- `test_c_cont_hall1_modulates_speed_to_zero`
- `test_c_cont_hall1_persist_triggers_safety_state`
- `test_c_cont_hall2_modulates_speed_low`
- `test_c_cont_zwischen_zone_balances`
- `test_c_cont_no_overflow_state_in_normal_op`
- `test_c_cont_eifel_24mm3s_no_cycles` (Reproduktion ohne Cycles)
- `test_c_cont_extruder_velocity_drives_speed`
- `test_c_cont_30mm3s_no_underflow`
- `test_c_cont_70mm3s_target_achievable`

### C-pred — `tests/test_c_pred_adaptive.py`

- `test_c_pred_low_flow_uses_config_speed`
- `test_c_pred_high_flow_scales_up`
- `test_c_pred_no_change_in_bang_bang_state_machine`
- `test_c_pred_tracker_not_ready_fallback`
- `test_c_pred_safety_factor_respected`
- `test_c_pred_max_cap_respected`

### Regressions

- 351 Tests von `03d4f2f` (P7-79) müssen weiterhin grün sein
- C-cont: bekannte Anpassungen bei 5-15 Tests die OVERFLOW-State exercisen

---

## 7. Hardware-Test-Plan — Inkrementell

Schrittweises Vorgehen mit klaren Erfolgs-Schwellen, **bevor** zur nächsten Flow-Stufe.

### Phase 1 — Baseline-Test (30 mm³/s als Erfolgs-Schwelle)

- Test-Modell: Calibration-Cube oder Vase-Mode-Hülle
- Print-Dauer: mindestens 10 min stabiler Steady-State
- **Erfolg**: keine Druckkopf-Stalls, keine Crashes, Förderrate ≥ Extruder-Velocity

Wenn Phase 1 stable → Phase 2.

### Phase 2 — Mid-Flow (50 mm³/s)

- Gleiches Modell, Flow auf 50 mm³/s
- Mindestens 15 min Steady-State
- **Erfolg**: gleiche Kriterien wie Phase 1, plus HALL2-Hits sichtbar (oder bei C-cont: Buffer-Arm visuell in HALL2-Bereich stabil)

Wenn Phase 2 stable → Phase 3.

### Phase 3 — High-Flow Target (70 mm³/s)

- Modell evtl. anpassen (volumetrischer Test-Print mit größerer Düse für realistisches Szenario)
- Mindestens 30 min Steady-State
- **Erfolg**: 70 mm³/s stabil, keine Crashes, keine Druckkopf-Stalls

### A/B-Vergleich am Ende

Beide Branches parallel auf identischer Print-Sequenz:
- Wer schafft welche Flow-Stufe stabiler?
- Welche Branch hat weniger OVERFLOW-Cycles / Druckkopf-Stalls?
- Welche Branch ist niedriger-Risiko für Mainline-Merge?

### Erfolgskriterium (Branch-Level)

- **C-cont passed**: stabil bei 70 mm³/s, keine Crashes, keine Druckkopf-Pausen
- **C-pred passed**: keine Regression bei <20 mm³/s, klare Verbesserung 20-30 mm³/s
- **Winner**: Branch mit besserem Throughput+Reliability-Verhältnis → Mainline-Merge

### Fallback

Falls beide Branches Probleme zeigen: zurück zu `python-ansatz` HEAD (P7-79) als stable Baseline.

---

## 8. Edge-Cases (beide Varianten)

- **Extruder-Retract**: `max(0.0, dp/dt)` clampt → Buffer fördert nicht reverse
- **Tracker pre-ready**: Fallback auf `config_feed_speed`
- **LOAD/UNLOAD-Phasen**: Tracker aktiv (Diagnostik), aber Speed-Modulation greift nicht
- **Manual Feed/Retract**: Tracker bleibt aktiv
- **klippy:disconnect**: Tracker auf `None` resetten, samples leeren
- **BUFFER_RESET**: Tracker resetten
- **Pause mid-print**: Tracker zeigt 0 nach `window_size` (300ms) → Speed = `config_feed_speed`, kein über-Fördern

---

## 9. Risiken

1. **C-cont strukturelle Regression bei niedrigem Flow**: AUTO_STREAM könnte bei <10 mm³/s Buffer überfüllen. Mitigation: HALL2-Modulator (0.5× extruder_vel). Test in Phase 1.
2. **C-pred safety_factor zu aggressiv**: 1.2 könnte HALL1-Overshoot triggern. Mitigation: BUFFER_SET-Konfigurierbar (`buffer_predict_factor`), default konservativ 1.1.
3. **Velocity-Tracker-Lag**: 300ms Window könnte bei abrupten Speed-Wechseln zu spät reagieren. Mitigation: kürzeres Window bei Hardware-Test bewerten.
4. **API-Brüche durch Klipper-Updates**: `extruder.get_status()['position']` ist mainline-stabil seit Jahren, aber nicht 100% garantiert. Mitigation: try/except mit Fallback auf `config_feed_speed`.
5. **Eifel-Joes c=14-Crash bei C-pred**: P7-79b (commit `03d4f2f`) ist auf beiden Branches Basis. Soll auch in C-cont aktiv bleiben als Defense-in-Depth.

---

## 10. Branch-Strategie

```
python-ansatz (stable trunk @ 03d4f2f P7-79)
├── feature/c-cont-streaming (vom 03d4f2f abzweigen)
└── feature/c-pred-adaptive (vom 03d4f2f abzweigen)
```

Beide Branches behalten P7-66..P7-79-Fixes. Nach Hardware-Test wird Winner-Branch auf `python-ansatz` gemerged, Loser-Branch gelöscht.

---

## 11. Nächste Schritte

1. Spec-Doc von User reviewen lassen
2. `feature/c-cont-streaming` und `feature/c-pred-adaptive` Branches anlegen
3. Pro Branch: `writing-plans`-Skill für Implementation-Plan
4. Implementation pro Branch in separater Session
5. Hardware-Tests sequenziell durchführen
6. A/B-Vergleich → Mainline-Merge des Winners
