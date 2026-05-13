# C-cont (Continuous-Streaming) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Buffer-Stepper läuft im STATE_AUTO dauerhaft im Streaming-Mode. HALL-Sensoren modulieren nur die `feed_speed`. Keine OVERFLOW-State-Transitions im normalen Betrieb. Strukturell für 70 mm³/s ausgelegt.

**Architecture:** ExtruderVelocityTracker (read-only, passiv) misst Toolhead-Velocity. SpeedModulator wandelt HALL-State + Velocity in `target_feed_speed`. `_on_mcu_flush` Submit-Loop läuft kontinuierlich, Speed variiert pro Submit. HALL1 (Überlast) setzt nur Speed=0; nur bei Persist >2s wird `_enter_overflow` als Hardware-Safety getriggert.

**Tech Stack:** Python (Klipper-Plugin), pytest, FakeKlipper-Mocks (`tests/fakes_klipper.py`).

**Base Branch:** `feature/c-cont-streaming` (von `de3603d` auf `python-ansatz`).

**Spec Reference:** `docs/superpowers/specs/2026-05-13-high-flow-buffer-architecture.md` (C-cont Variante).

---

## File Structure

**Modified:**
- `klipper_extras/buffer_feeder.py` (~3800 Zeilen):
  - `BufferFeeder.__init__` — cfg-Params + Tracker-Init
  - `BufferFeeder._main_tick` — Tracker.tick + HALL1-Persist-Check
  - `BufferFeeder._on_mcu_flush` — Continuous-Streaming-Umbau
  - `HallSensorMonitor._set_semantic_state` — HALL1 defer in STATE_AUTO
  - Neue Klasse: `ExtruderVelocityTracker` (vor `BufferFeeder`)
  - Neue Methode: `BufferFeeder._compute_target_feed_speed`
  - Neue Methoden: `BufferFeeder._mark_hall1_active` / `_mark_hall1_cleared`

**Created:**
- `tests/test_velocity_tracker.py` — Unit-Tests für ExtruderVelocityTracker
- `tests/test_c_cont_streaming.py` — Integration-Tests für C-cont-Streaming

**Modified (Anpassungen bestehender Tests):**
- 5-15 Tests in `tests/test_*.py` die `STATE_AUTO → STATE_OVERFLOW` durch HALL1-Edge erwarten. Tasks identifizieren und anpassen.

---

## Task 1: ExtruderVelocityTracker — Klasse + Unit-Tests

**Files:**
- Modify: `klipper_extras/buffer_feeder.py` (neue Klasse vor `BufferFeeder` einfügen, nach Z.768)
- Create: `tests/test_velocity_tracker.py`

- [ ] **Step 1: Test-File anlegen mit fehlenden Tests**

Schreibe `tests/test_velocity_tracker.py`:

```python
"""Unit-Tests fuer ExtruderVelocityTracker.

Passive Observer der Extruder.last_position. KEIN flush_step_generation,
KEIN SYNC. Sliding-Average ueber 300ms fuer Glaettung.
"""

import math
import sys
import types

import pytest

from fakes_klipper import FakeConfig, FakePrinter
from klipper_extras import buffer_feeder


@pytest.fixture
def fake_extruder_printer():
    """FakePrinter mit FakeExtruder (lookup_object('extruder') returns
    Object with get_status returning dict with 'position' key)."""
    printer = FakePrinter()
    fake_ext = types.SimpleNamespace()
    fake_ext._position = 0.0
    def get_status(eventtime):
        return {'position': fake_ext._position}
    fake_ext.get_status = get_status
    printer.objects['extruder'] = fake_ext
    return printer, fake_ext


def test_tracker_zero_initial(fake_extruder_printer):
    """Fresh tracker: kein Sample -> velocity 0.0, is_ready False."""
    printer, _ = fake_extruder_printer
    tracker = buffer_feeder.ExtruderVelocityTracker(
        owner=None, printer=printer,
        sample_interval=0.025, window_size=0.3,
        filament_diameter=1.75)
    assert tracker.get_velocity() == 0.0
    assert tracker.get_volumetric_flow() == 0.0
    assert not tracker.is_ready()


def test_tracker_steady_state(fake_extruder_printer):
    """12 Samples, lineare Position-Steigerung -> erwartete Velocity."""
    printer, ext = fake_extruder_printer
    tracker = buffer_feeder.ExtruderVelocityTracker(
        owner=None, printer=printer,
        sample_interval=0.025, window_size=0.3,
        filament_diameter=1.75)
    # Simuliere 10 mm/s linear: pos = t * 10
    t = 0.0
    for _ in range(12):
        ext._position = t * 10.0
        tracker.tick(t)
        t += 0.025
    assert tracker.is_ready()
    assert tracker.get_velocity() == pytest.approx(10.0, abs=0.1)


def test_tracker_velocity_step_lag(fake_extruder_printer):
    """Sample-Step von 0 -> 10 mm/s: Sliding-Avg laggt ueber window_size."""
    printer, ext = fake_extruder_printer
    tracker = buffer_feeder.ExtruderVelocityTracker(
        owner=None, printer=printer,
        sample_interval=0.025, window_size=0.3,
        filament_diameter=1.75)
    # 6 Samples mit 0 mm/s
    t = 0.0
    for _ in range(6):
        ext._position = 0.0
        tracker.tick(t)
        t += 0.025
    # 6 Samples mit 10 mm/s
    pos = 0.0
    for _ in range(6):
        pos += 10.0 * 0.025
        ext._position = pos
        tracker.tick(t)
        t += 0.025
    # Window enthaelt mix: avg sollte ungefaehr 5 mm/s sein (mittig).
    assert 3.0 < tracker.get_velocity() < 7.0


def test_tracker_retract_clamped(fake_extruder_printer):
    """Negative Position-Delta -> Velocity auf 0 geclampt (kein Retract)."""
    printer, ext = fake_extruder_printer
    tracker = buffer_feeder.ExtruderVelocityTracker(
        owner=None, printer=printer,
        sample_interval=0.025, window_size=0.3,
        filament_diameter=1.75)
    # 12 Samples mit fallender Position (Retract)
    t = 0.0
    pos = 100.0
    for _ in range(12):
        ext._position = pos
        tracker.tick(t)
        pos -= 1.0
        t += 0.025
    assert tracker.get_velocity() == 0.0


def test_tracker_volumetric_calc(fake_extruder_printer):
    """Volumetric: 10 mm/s linear * pi * 0.875^2 ~ 24.05 mm^3/s."""
    printer, ext = fake_extruder_printer
    tracker = buffer_feeder.ExtruderVelocityTracker(
        owner=None, printer=printer,
        sample_interval=0.025, window_size=0.3,
        filament_diameter=1.75)
    t = 0.0
    for _ in range(12):
        ext._position = t * 10.0
        tracker.tick(t)
        t += 0.025
    cross_section = math.pi * (1.75 / 2.0) ** 2
    expected = 10.0 * cross_section
    assert tracker.get_volumetric_flow() == pytest.approx(expected, abs=0.5)


def test_tracker_is_ready_threshold(fake_extruder_printer):
    """is_ready True nach window_size/sample_interval ticks (12 fuer 300/25)."""
    printer, ext = fake_extruder_printer
    tracker = buffer_feeder.ExtruderVelocityTracker(
        owner=None, printer=printer,
        sample_interval=0.025, window_size=0.3,
        filament_diameter=1.75)
    t = 0.0
    for i in range(12):
        ext._position = t * 5.0
        tracker.tick(t)
        t += 0.025
        if i < 11:
            assert not tracker.is_ready()
    assert tracker.is_ready()
```

- [ ] **Step 2: Tests laufen lassen — RED erwartet**

Run:
```bash
PYTHONPATH=. venv-test/Scripts/pytest.exe tests/test_velocity_tracker.py -v
```

Erwartet: `AttributeError: module 'klipper_extras.buffer_feeder' has no attribute 'ExtruderVelocityTracker'`

- [ ] **Step 3: Klasse implementieren**

In `klipper_extras/buffer_feeder.py` nach Z.768 (vor `class BufferFeeder`):

```python
class ExtruderVelocityTracker:
    """Read-only passive tracker for extruder velocity.

    Uses extruder.get_status(eventtime)['position'] — no flush_step_
    generation, no SYNC, no lockstep with toolhead pipeline. Pure
    observer pattern. Output drives C-cont SpeedModulator and C-pred
    safety-factor override.
    """

    def __init__(self, owner, printer, *,
                 sample_interval=0.025,
                 window_size=0.3,
                 filament_diameter=1.75):
        self.owner = owner
        self.printer = printer
        self.sample_interval = sample_interval
        self.window_size = window_size
        self._cross_section = math.pi * (filament_diameter / 2.0) ** 2
        self._max_samples = max(2, int(window_size / sample_interval))
        self._samples = collections.deque(maxlen=self._max_samples)
        self._extruder = None
        self._last_sample_time = 0.0

    def _get_extruder(self):
        if self._extruder is not None:
            return self._extruder
        self._extruder = self.printer.lookup_object('extruder', None)
        return self._extruder

    def tick(self, eventtime):
        """Call from _main_tick (50Hz reactor). Throttles internally
        to sample_interval (default 25ms / 40Hz)."""
        if eventtime - self._last_sample_time < self.sample_interval:
            return
        ext = self._get_extruder()
        if ext is None:
            return
        try:
            status = ext.get_status(eventtime)
        except Exception:
            return
        position = status.get('position', 0.0) if isinstance(
            status, dict) else 0.0
        self._samples.append((eventtime, position))
        self._last_sample_time = eventtime

    def get_velocity(self):
        """Returns linear filament velocity (mm/s, non-negative).
        0.0 if fewer than 2 samples or negative dp."""
        if len(self._samples) < 2:
            return 0.0
        (t0, p0), (t1, p1) = self._samples[0], self._samples[-1]
        dt = t1 - t0
        if dt < 1e-6:
            return 0.0
        return max(0.0, (p1 - p0) / dt)

    def get_volumetric_flow(self):
        """Returns volumetric flow (mm^3/s)."""
        return self.get_velocity() * self._cross_section

    def is_ready(self):
        """True after sliding window has filled (window_size seconds
        of samples accumulated)."""
        return len(self._samples) == self._max_samples

    def reset(self):
        """Clear all samples. Call on klippy:disconnect / BUFFER_RESET."""
        self._samples.clear()
        self._last_sample_time = 0.0
```

Plus imports am Anfang der Datei verifizieren: `import collections`, `import math` müssen vorhanden sein (oder ergänzen).

- [ ] **Step 4: Tests laufen lassen — GREEN erwartet**

Run:
```bash
PYTHONPATH=. venv-test/Scripts/pytest.exe tests/test_velocity_tracker.py -v
```

Erwartet: 6 PASSED

- [ ] **Step 5: Full-Suite-Validation**

Run:
```bash
PYTHONPATH=. venv-test/Scripts/pytest.exe tests/ --tb=no -q
```

Erwartet: 357 PASSED (351 baseline + 6 tracker tests).

- [ ] **Step 6: Commit**

```bash
git add klipper_extras/buffer_feeder.py tests/test_velocity_tracker.py
git commit -m "$(cat <<'EOF'
feat(buffer_feeder): add ExtruderVelocityTracker (C-cont T1)

Read-only passive tracker für Extruder-Filament-Velocity. Wird in
C-cont vom SpeedModulator als Input genutzt. Nutzt
extruder.get_status()['position'] (kein flush_step_generation,
kein SYNC) -> kein Druckkopf-Pause-Risiko.

40Hz Sampling, 300ms sliding average. 1.75mm Filament default.
Output: linear mm/s und volumetric mm^3/s.

6 Unit-Tests grün. 351 -> 357 total.
EOF
)"
```

---

## Task 2: Tracker-Integration in BufferFeeder

**Files:**
- Modify: `klipper_extras/buffer_feeder.py` (BufferFeeder.__init__, _main_tick)

- [ ] **Step 1: Test schreiben — Integration**

In neuer Datei `tests/test_c_cont_streaming.py`:

```python
"""Integration-Tests für C-cont Continuous-Streaming."""

import math
import types

import pytest

from fakes_klipper import (
    FakeConfig,
    FakePrinter,
    FakePrintStats,
)
from klipper_extras import buffer_feeder


def make_c_cont_feeder(monkeypatch, *, print_state='printing'):
    """Helper analog test_p778_print_block_stale_override.make_auto_feeder."""
    # ... Pattern aus test_p778 wiederverwenden — Setup mit
    #     STATE_AUTO + use_flush_callback_bang_bang=True
    # ... FakeExtruder mit get_status({'position': ...}) registrieren
    raise NotImplementedError(
        "Reuse make_auto_feeder pattern from test_p778, add fake_extruder")


def test_c_cont_tracker_initialized(monkeypatch):
    """BufferFeeder.__init__ erzeugt velocity_tracker."""
    printer, feeder = make_c_cont_feeder(monkeypatch)
    assert hasattr(feeder, 'velocity_tracker')
    assert isinstance(feeder.velocity_tracker,
                      buffer_feeder.ExtruderVelocityTracker)


def test_c_cont_tracker_tick_in_main_tick(monkeypatch):
    """_main_tick ruft tracker.tick(eventtime)."""
    printer, feeder = make_c_cont_feeder(monkeypatch)
    tick_calls = []
    monkeypatch.setattr(
        feeder.velocity_tracker, 'tick',
        lambda t: tick_calls.append(t))
    feeder._main_tick(eventtime=10.0)
    assert 10.0 in tick_calls
```

- [ ] **Step 2: Helper `make_c_cont_feeder` ausarbeiten**

Lies `tests/test_p778_print_block_stale_override.py` `make_auto_feeder` als Vorlage. Erweitere:
- FakeExtruder als Mock registrieren (FakePrinter.objects['extruder'])
- `get_status(eventtime)` returns `{'position': self._extruder_position}`

- [ ] **Step 3: Tests laufen lassen — RED**

Erwartet: AttributeError (kein `velocity_tracker` attribute).

- [ ] **Step 4: Integration implementieren**

In `BufferFeeder.__init__` (suche Z.~1051 — bei den anderen P7-78 init lines):

```python
# C-cont T2: ExtruderVelocityTracker fuer Speed-Modulation.
# Read-only, passiver Observer. Greift NICHT in Toolhead-Pipeline ein.
self.velocity_tracker = ExtruderVelocityTracker(
    owner=self, printer=self.printer,
    sample_interval=0.025,
    window_size=0.3,
    filament_diameter=config.getfloat(
        'filament_diameter', 1.75, above=0.))
```

In `_main_tick` (Z.~1884), als allererste Anweisung im try-Block (vor allen anderen Operationen):

```python
# C-cont T2: Velocity-Tracker tick (40Hz, intern throttled).
self.velocity_tracker.tick(eventtime)
```

- [ ] **Step 5: Tests laufen lassen — GREEN**

Run:
```bash
PYTHONPATH=. venv-test/Scripts/pytest.exe tests/test_c_cont_streaming.py -v
PYTHONPATH=. venv-test/Scripts/pytest.exe tests/ --tb=no -q
```

Erwartet: alle PASSED, total 359.

- [ ] **Step 6: Commit**

```bash
git add klipper_extras/buffer_feeder.py tests/test_c_cont_streaming.py
git commit -m "feat(buffer_feeder): integrate velocity_tracker in BufferFeeder (C-cont T2)"
```

---

## Task 3: cfg-Params + Konstanten

**Files:**
- Modify: `klipper_extras/buffer_feeder.py` (BufferFeeder.__init__ config-block)

- [ ] **Step 1: Test schreiben**

In `tests/test_c_cont_streaming.py` ergänzen:

```python
def test_c_cont_cfg_params_loaded(monkeypatch):
    """Neue cfg-Params werden gelesen mit Defaults."""
    printer, feeder = make_c_cont_feeder(monkeypatch)
    assert hasattr(feeder, 'max_feed_speed')
    assert feeder.max_feed_speed == 100.0  # default
    assert hasattr(feeder, 'hall1_persist_timeout')
    assert feeder.hall1_persist_timeout == 2.0  # default
    assert hasattr(feeder, 'buffer_debug_metrics')
    assert feeder.buffer_debug_metrics is False  # default

def test_c_cont_cfg_params_custom(monkeypatch):
    """Custom cfg-Params funktionieren."""
    cfg_overrides = {
        'max_feed_speed': 80.0,
        'hall1_persist_timeout': 3.0,
        'buffer_debug_metrics': True,
    }
    printer, feeder = make_c_cont_feeder(monkeypatch, cfg_overrides=cfg_overrides)
    assert feeder.max_feed_speed == 80.0
    assert feeder.hall1_persist_timeout == 3.0
    assert feeder.buffer_debug_metrics is True
```

`make_c_cont_feeder` muss `cfg_overrides` als kwarg unterstützen — analog zu existierenden Patterns.

- [ ] **Step 2: Tests laufen — RED**

Erwartet: AttributeError.

- [ ] **Step 3: cfg-Params hinzufügen**

In `BufferFeeder.__init__`, direkt nach `self.interrupt_chunk_mm = ...` (Z.~857-860):

```python
# C-cont T3: max_feed_speed = Cap fuer SpeedModulator-Output. Bei
# extruder_velocity * factor sollte das Stepper-Hardware-Limit nicht
# ueberschritten werden. Default 100 mm/s (deutlich ueber Default
# feed_speed=30, lll.cfg-Wert=70).
self.max_feed_speed = config.getfloat(
    'max_feed_speed', 100.0, above=0.)
if self.max_feed_speed < self.feed_speed:
    raise config.error(
        "max_feed_speed (%.1f) must be >= feed_speed (%.1f)"
        % (self.max_feed_speed, self.feed_speed))

# C-cont T3: HALL1-Persist-Timeout. HALL1 (Ueberlast) im STATE_AUTO
# loest erst nach diesem Timeout den echten OVERFLOW-State aus. In
# der Zwischenzeit setzt SpeedModulator nur target_speed=0 (kein
# State-Wechsel, kein Lockout). Default 2.0s.
self.hall1_persist_timeout = config.getfloat(
    'hall1_persist_timeout', 2.0, above=0.)

# C-cont T3: Diagnostik-Logs (Buffer-Metrics alle 1s, Per-Submit-
# DEBUG, OVERFLOW-Safety-Events). Default off fuer Production.
self.buffer_debug_metrics = config.getboolean(
    'buffer_debug_metrics', False)
```

- [ ] **Step 4: Tests laufen — GREEN**

Erwartet: 2 neue Tests GREEN, total 361.

- [ ] **Step 5: Commit**

```bash
git add klipper_extras/buffer_feeder.py tests/test_c_cont_streaming.py
git commit -m "feat(buffer_feeder): add C-cont cfg-params (max_feed_speed, hall1_persist_timeout, buffer_debug_metrics)"
```

---

## Task 4: SpeedModulator (`_compute_target_feed_speed`)

**Files:**
- Modify: `klipper_extras/buffer_feeder.py` (BufferFeeder neue Methode)

- [ ] **Step 1: Tests schreiben**

In `tests/test_c_cont_streaming.py`:

```python
def test_c_cont_modulator_hall1_zero(monkeypatch):
    """HALL1 active -> target_speed = 0 (Notbremse)."""
    printer, feeder = make_c_cont_feeder(monkeypatch)
    feeder.sensors._state['hall_overflow'] = True
    assert feeder._compute_target_feed_speed() == 0.0


def test_c_cont_modulator_hall3_max(monkeypatch):
    """HALL3 active (Buffer leer) + tracker ready -> max_feed_speed."""
    printer, feeder = make_c_cont_feeder(monkeypatch)
    feeder.sensors._state['hall_empty'] = True
    feeder.sensors._state['hall_full'] = False
    feeder.sensors._state['hall_overflow'] = False
    # Tracker ready durch 12 ticks faken
    _populate_tracker_to_ready(feeder, velocity=15.0)
    assert feeder._compute_target_feed_speed() == feeder.max_feed_speed


def test_c_cont_modulator_hall2_half(monkeypatch):
    """HALL2 active (Buffer voll) -> 0.5 * extruder_velocity."""
    printer, feeder = make_c_cont_feeder(monkeypatch)
    feeder.sensors._state['hall_empty'] = False
    feeder.sensors._state['hall_full'] = True
    feeder.sensors._state['hall_overflow'] = False
    _populate_tracker_to_ready(feeder, velocity=20.0)
    assert feeder._compute_target_feed_speed() == pytest.approx(10.0, abs=0.5)


def test_c_cont_modulator_zwischenzone_balance(monkeypatch):
    """Zwischenzone (kein HALL aktiv) -> extruder_velocity."""
    printer, feeder = make_c_cont_feeder(monkeypatch)
    feeder.sensors._state['hall_empty'] = False
    feeder.sensors._state['hall_full'] = False
    feeder.sensors._state['hall_overflow'] = False
    _populate_tracker_to_ready(feeder, velocity=12.0)
    assert feeder._compute_target_feed_speed() == pytest.approx(12.0, abs=0.5)


def test_c_cont_modulator_tracker_not_ready_fallback(monkeypatch):
    """Tracker not_ready (boot, first 300ms) -> Fallback config feed_speed."""
    printer, feeder = make_c_cont_feeder(monkeypatch)
    feeder.sensors._state['hall_empty'] = False
    feeder.sensors._state['hall_full'] = False
    feeder.sensors._state['hall_overflow'] = False
    # Tracker not ready
    assert not feeder.velocity_tracker.is_ready()
    assert feeder._compute_target_feed_speed() == feeder.feed_speed


def _populate_tracker_to_ready(feeder, *, velocity):
    """Fake-Helper: 12 ticks mit linearer Position-Steigerung."""
    fake_ext = feeder.printer.objects['extruder']
    t = 0.0
    for _ in range(12):
        fake_ext._position = t * velocity
        feeder.velocity_tracker.tick(t)
        t += 0.025
```

- [ ] **Step 2: Tests laufen — RED**

Erwartet: AttributeError `_compute_target_feed_speed` not defined.

- [ ] **Step 3: Methode implementieren**

In `BufferFeeder`, neue Methode (z.B. nach `_compute_effective_feed_speed`-Pattern, oder vor `_on_mcu_flush`):

```python
def _compute_target_feed_speed(self):
    """C-cont T4: SpeedModulator.

    HALL-Sensoren + ExtruderVelocity -> target feed_speed (mm/s)
    fuer den naechsten Submit. Returns 0.0 als Notbremse (HALL1).

    Logik:
      HALL1 (overflow)      -> 0.0 (Notbremse, ohne State-Wechsel)
      HALL3 (empty)         -> max_feed_speed (Buffer auffuellen)
      HALL2 (full)          -> 0.5 * extruder_velocity (langsam)
      Zwischenzone          -> extruder_velocity (Balance)
      Tracker not_ready     -> config feed_speed (Fallback)
    """
    if self.hall_overflow:
        return 0.0
    if self.hall_empty:
        return self.max_feed_speed
    if not self.velocity_tracker.is_ready():
        return self.feed_speed
    extruder_vel = self.velocity_tracker.get_velocity()
    if self.hall_full:
        return 0.5 * extruder_vel
    return extruder_vel
```

- [ ] **Step 4: Tests laufen — GREEN**

Erwartet: 5 neue Tests GREEN, total 366.

- [ ] **Step 5: Commit**

```bash
git add klipper_extras/buffer_feeder.py tests/test_c_cont_streaming.py
git commit -m "feat(buffer_feeder): add SpeedModulator _compute_target_feed_speed (C-cont T4)"
```

---

## Task 5: HALL1 Soft-Trigger im STATE_AUTO

**Files:**
- Modify: `klipper_extras/buffer_feeder.py` (HallSensorMonitor._set_semantic_state, BufferFeeder.__init__, neue Methoden)

- [ ] **Step 1: Tests schreiben**

In `tests/test_c_cont_streaming.py`:

```python
def test_c_cont_hall1_in_auto_defers_no_state_change(monkeypatch):
    """STATE_AUTO + HALL1-Edge -> KEINE state-transition zu OVERFLOW."""
    printer, feeder = make_c_cont_feeder(monkeypatch)
    feeder._state = buffer_feeder.STATE_AUTO
    assert feeder._state == buffer_feeder.STATE_AUTO
    # HALL1 active simulieren
    feeder.sensors._set_semantic_state('hall_overflow', True)
    # State sollte AUTO bleiben (kein OVERFLOW)
    assert feeder._state == buffer_feeder.STATE_AUTO
    # Aber _hall1_active_since muss gesetzt sein
    assert feeder._hall1_active_since is not None


def test_c_cont_hall1_in_load_keeps_immediate_overflow(monkeypatch):
    """Nicht-AUTO-State + HALL1-Edge -> sofortiger OVERFLOW (Backward-compat)."""
    printer, feeder = make_c_cont_feeder(monkeypatch)
    feeder._state = buffer_feeder.STATE_LOAD_PHASE_1
    feeder.sensors._set_semantic_state('hall_overflow', True)
    # In LOAD-Phase soll HALL1 sofort OVERFLOW triggern (wie bisher)
    assert feeder._state == buffer_feeder.STATE_OVERFLOW


def test_c_cont_hall1_cleared_resets_timestamp(monkeypatch):
    """HALL1 cleared (falling edge) -> _hall1_active_since = None."""
    printer, feeder = make_c_cont_feeder(monkeypatch)
    feeder._state = buffer_feeder.STATE_AUTO
    feeder.sensors._set_semantic_state('hall_overflow', True)
    assert feeder._hall1_active_since is not None
    feeder.sensors._set_semantic_state('hall_overflow', False)
    assert feeder._hall1_active_since is None
```

- [ ] **Step 2: Tests laufen — RED**

Erwartet: AttributeError oder behavior mismatch.

- [ ] **Step 3: Init für `_hall1_active_since`**

In `BufferFeeder.__init__` (suche bei den anderen state-Init-Variablen, z.B. neben `_last_mcu_flush_time`):

```python
# C-cont T5: HALL1-Persist-Tracking. In STATE_AUTO loest HALL1-Edge
# nicht mehr direkt _enter_overflow aus, sondern setzt nur den
# Timestamp. _main_tick prueft Persist > hall1_persist_timeout und
# eskaliert dann zu echtem OVERFLOW (Hardware-Safety). None = HALL1
# inactive, float = reactor.monotonic() zum Edge-Time.
self._hall1_active_since = None
```

- [ ] **Step 4: Defer-Logic in HallSensorMonitor**

In `HallSensorMonitor._set_semantic_state` (Z.~249-256), Ersetze den `hall_overflow`-Branch:

```python
if name == 'hall_overflow':
    if value:
        # C-cont T5: In STATE_AUTO defer the immediate _enter_overflow
        # to _main_tick (which checks for hall1_persist_timeout). In
        # other states (LOAD, MANUAL, UNLOAD) keep the immediate
        # trigger — those paths have their own safety semantics and
        # need synchronous overflow-handling.
        if owner._state == STATE_AUTO:
            owner._mark_hall1_active()
        else:
            owner._enter_overflow()
    else:
        owner._mark_hall1_cleared()
        if owner._state == STATE_OVERFLOW:
            owner._exit_overflow()
```

- [ ] **Step 5: Helper-Methoden in BufferFeeder**

Suche eine geeignete Stelle (nähe `_enter_overflow`, Z.~1709) und ergänze:

```python
def _mark_hall1_active(self):
    """C-cont T5: HALL1-Edge im STATE_AUTO — defer state-transition,
    nur Timestamp setzen. _main_tick prueft Persist > hall1_persist_-
    timeout fuer echten OVERFLOW-Safety-Trigger."""
    if self._hall1_active_since is None:
        self._hall1_active_since = self.reactor.monotonic()

def _mark_hall1_cleared(self):
    """C-cont T5: HALL1 falling-edge — Timestamp loeschen, Persist-
    Counter zurueck auf None."""
    self._hall1_active_since = None
```

- [ ] **Step 6: Tests laufen — GREEN**

Erwartet: 3 neue Tests GREEN, total 369.

- [ ] **Step 7: Commit**

```bash
git add klipper_extras/buffer_feeder.py tests/test_c_cont_streaming.py
git commit -m "feat(buffer_feeder): HALL1 soft-trigger in STATE_AUTO (C-cont T5)"
```

---

## Task 6: HALL1-Persist-Check im _main_tick

**Files:**
- Modify: `klipper_extras/buffer_feeder.py` (BufferFeeder._main_tick)

- [ ] **Step 1: Tests schreiben**

In `tests/test_c_cont_streaming.py`:

```python
def test_c_cont_hall1_persist_triggers_overflow_safety(monkeypatch):
    """HALL1 active > hall1_persist_timeout -> echter _enter_overflow."""
    printer, feeder = make_c_cont_feeder(monkeypatch)
    feeder._state = buffer_feeder.STATE_AUTO
    feeder.hall1_persist_timeout = 2.0
    # HALL1-Edge
    feeder.sensors._set_semantic_state('hall_overflow', True)
    # 1s spaeter — noch im timeout
    feeder.reactor.now = 1.0
    feeder._hall1_active_since = 0.0  # explicit
    feeder._main_tick(eventtime=1.0)
    assert feeder._state == buffer_feeder.STATE_AUTO
    # 2.5s spaeter — Persist > timeout
    feeder.reactor.now = 2.5
    feeder._main_tick(eventtime=2.5)
    assert feeder._state == buffer_feeder.STATE_OVERFLOW


def test_c_cont_hall1_short_blip_no_safety(monkeypatch):
    """HALL1 active < hall1_persist_timeout, dann cleared -> kein OVERFLOW."""
    printer, feeder = make_c_cont_feeder(monkeypatch)
    feeder._state = buffer_feeder.STATE_AUTO
    feeder.hall1_persist_timeout = 2.0
    feeder.sensors._set_semantic_state('hall_overflow', True)
    feeder.reactor.now = 0.5
    feeder._main_tick(eventtime=0.5)
    # HALL1 cleared bevor timeout
    feeder.sensors._set_semantic_state('hall_overflow', False)
    feeder.reactor.now = 1.0
    feeder._main_tick(eventtime=1.0)
    assert feeder._state == buffer_feeder.STATE_AUTO
    assert feeder._hall1_active_since is None
```

- [ ] **Step 2: Tests laufen — RED**

Erwartet: Tests scheitern (kein Persist-Check im _main_tick).

- [ ] **Step 3: Persist-Check implementieren**

In `_main_tick` (Z.~1884), nach der Tracker.tick-Zeile aus Task 2 ergänzen:

```python
# C-cont T6: HALL1-Persist-Check. Wenn HALL1 laenger als hall1_-
# persist_timeout aktiv ist, eskaliere zu echtem _enter_overflow
# (Hardware-Safety-State). In der Zwischenzeit setzt SpeedModulator
# bereits target_speed=0, der Stepper foerdert nicht.
if (self._state == STATE_AUTO
        and self._hall1_active_since is not None):
    persist_duration = (
        self.reactor.monotonic() - self._hall1_active_since)
    if persist_duration >= self.hall1_persist_timeout:
        if self.buffer_debug_metrics:
            logging.info(
                "buffer_feeder: HALL1-Persist %0.2fs > %0.2fs "
                "threshold — entering OVERFLOW state (C-cont T6)",
                persist_duration, self.hall1_persist_timeout)
        self._enter_overflow()
        # _hall1_active_since wird durch _enter_overflow nicht
        # automatisch gecleared (es bleibt active bis HALL1 falling)
```

- [ ] **Step 4: Tests laufen — GREEN**

Erwartet: 2 neue Tests GREEN, total 371.

- [ ] **Step 5: Commit**

```bash
git add klipper_extras/buffer_feeder.py tests/test_c_cont_streaming.py
git commit -m "feat(buffer_feeder): HALL1-Persist-Check eskaliert zu OVERFLOW (C-cont T6)"
```

---

## Task 7: `_on_mcu_flush` Continuous-Streaming-Umbau

**Files:**
- Modify: `klipper_extras/buffer_feeder.py` (`_on_mcu_flush` Z.~2469-2729)

- [ ] **Step 1: Tests schreiben**

In `tests/test_c_cont_streaming.py`:

```python
def test_c_cont_continuous_streaming_in_auto(monkeypatch):
    """STATE_AUTO + HALL3 stable + tracker ready -> kontinuierliche Submits."""
    printer, feeder = make_c_cont_feeder(monkeypatch)
    feeder._state = buffer_feeder.STATE_AUTO
    feeder.sensors._state['hall_empty'] = True
    _populate_tracker_to_ready(feeder, velocity=15.0)
    submits = _capture_submits(feeder, monkeypatch)
    # Trigger _on_mcu_flush mehrfach
    for i in range(3):
        feeder._on_mcu_flush(flush_time=10.0 + i*0.05,
                              step_gen_time=10.0 + i*0.05)
    # Erwarten: jeder flush triggert Submit mit target_speed = max_feed_speed
    assert len(submits) >= 3
    for s in submits:
        assert s['speed'] == feeder.max_feed_speed


def test_c_cont_speed_modulation_via_hall_state(monkeypatch):
    """HALL-State-Change zwischen flushes -> Speed-Aenderung im naechsten Submit."""
    printer, feeder = make_c_cont_feeder(monkeypatch)
    feeder._state = buffer_feeder.STATE_AUTO
    _populate_tracker_to_ready(feeder, velocity=20.0)
    submits = _capture_submits(feeder, monkeypatch)
    # HALL3 -> max_feed_speed
    feeder.sensors._state['hall_empty'] = True
    feeder._on_mcu_flush(flush_time=10.0, step_gen_time=10.0)
    # HALL2 -> 0.5 * 20 = 10
    feeder.sensors._state['hall_empty'] = False
    feeder.sensors._state['hall_full'] = True
    feeder._on_mcu_flush(flush_time=10.5, step_gen_time=10.5)
    assert submits[0]['speed'] == feeder.max_feed_speed
    assert submits[1]['speed'] == pytest.approx(10.0, abs=0.5)


def test_c_cont_hall1_active_speed_zero_no_submit(monkeypatch):
    """HALL1 active -> target_speed=0 -> kein Submit (oder Submit mit speed=0)."""
    printer, feeder = make_c_cont_feeder(monkeypatch)
    feeder._state = buffer_feeder.STATE_AUTO
    feeder.sensors._set_semantic_state('hall_overflow', True)
    _populate_tracker_to_ready(feeder, velocity=15.0)
    submits = _capture_submits(feeder, monkeypatch)
    feeder._on_mcu_flush(flush_time=10.0, step_gen_time=10.0)
    # Erwarten: kein submit (oder submit mit speed=0)
    assert len(submits) == 0


def _capture_submits(feeder, monkeypatch):
    """Helper: monkeypatch _submit_move um alle Submit-Argumente abzufangen."""
    submits = []
    def fake_submit(distance, speed, **kwargs):
        submits.append({'distance': distance, 'speed': speed, **kwargs})
    monkeypatch.setattr(feeder, '_submit_move', fake_submit)
    return submits
```

- [ ] **Step 2: Tests laufen — RED**

Erwartet: Tests scheitern weil `_on_mcu_flush` noch Bang-Bang-Logik hat.

- [ ] **Step 3: `_on_mcu_flush` Umbau**

Der bestehende `_on_mcu_flush` (Z.~2469-2729) enthält die Bang-Bang-Logik. **Wichtig:** den Defer-Guard aus P7-79 (Z.~2504-2547) MUSS bleiben.

Ersetze den Submit-Pfad-Block (von "if self.hall_full:" Z.~2575 bis "_auto_between_since = None"-Resets) durch C-cont-Logic. Konkret:

```python
def _on_mcu_flush(self, flush_time, step_gen_time):
    # P7-78: track flush-callback activity (UNVERAENDERT)
    self._last_mcu_flush_time = flush_time

    # Early-returns (UNVERAENDERT bis incl. P7-79 Defer-Guard)
    if not self.use_flush_callback_bang_bang:
        return
    if self._bang_bang_suspended:
        return
    if self._state != STATE_AUTO:
        return
    # ... P7-79 Defer (Z.~2504-2547) UNVERAENDERT
    if self._needs_overflow_prime:
        # UNVERAENDERT — prime-Pfad bleibt
        ...
        return
    if self._stepper_synced_to is not None:
        return
    if self._is_hall1_active('submit_move'):
        # In C-cont nur fuer LOAD/UNLOAD-Pfade — STATE_AUTO laeuft
        # weiter mit target_speed=0
        return

    # C-cont T7: Continuous-Streaming-Submit.
    # Berechne target_speed via SpeedModulator. Wenn 0 (HALL1 active
    # oder kein extruder_velocity bekannt): kein Submit.
    target_speed = self._compute_target_feed_speed()
    if target_speed <= 0.0:
        # Notbremse oder Fallback waehrend Boot. Kein Submit, aber
        # _last_move_end_time wird vom Lookahead-Pfad weiter geprueft.
        if self.buffer_debug_metrics:
            logging.debug(
                "buffer_feeder: target_speed=0 — kein Submit "
                "(hall1=%s ready=%s)",
                self.hall_overflow,
                self.velocity_tracker.is_ready())
        return

    # Lookahead-Check: laeuft noch ein Move? Dann nur naechsten Chunk
    # vorbereiten wenn remaining <= lead_time (P7-66 Streaming-Pattern).
    move_active = self._move_in_flight()
    if move_active:
        remaining = self._last_move_end_time - step_gen_time
        if remaining > self.lead_time:
            return  # noch frueh, kein neuer Chunk
    if self._pending_remaining_mm > 0:
        return  # Sub-Chunk-Pipeline laeuft

    # Anchor wie in Bang-Bang (P7-66 Pattern)
    if move_active:
        anchor = self._last_move_end_time
    else:
        anchor = step_gen_time + self.lead_time

    # C-cont T7: continuous_feed bleibt strukturell True im Stream-
    # Mode. Reset feed_distance_accumulator nur bei echtem Boot.
    if not self._continuous_feed:
        self._feed_distance_accumulator = 0.0
        self._feed_deadline_time = None
        self._continuous_feed = True
        self._continuous_feed_direction = 1
    self._continuous_feed_speed = target_speed

    self._submit_move(
        self.flush_callback_chunk_mm,
        target_speed,                  # <-- KEY DIFF: target_speed statt feed_speed
        forced_t0=anchor,
        streaming=move_active,
        submit_chunk_cap=self.interrupt_chunk_mm)
```

**WICHTIG:** Diese Umstellung impacts den bestehenden Bang-Bang-Pfad. STABLE_DROP_GRACE-Block (Z.~2720-2728) wird obsolet (Tracker übernimmt die Speed-Modulation).

- [ ] **Step 4: Tests laufen — GREEN**

Run:
```bash
PYTHONPATH=. venv-test/Scripts/pytest.exe tests/test_c_cont_streaming.py -v
```

Erwartet: 3 neue Tests GREEN.

**Aber Achtung:** existierende Tests werden ggf. brechen (Task 9). Volle Suite NICHT erwarten grün zu sein.

- [ ] **Step 5: Vorläufig commit (mit Tests-Status-Hinweis)**

```bash
git add klipper_extras/buffer_feeder.py tests/test_c_cont_streaming.py
git commit -m "feat(buffer_feeder): continuous-streaming _on_mcu_flush umbau (C-cont T7)

Existing tests may fail — addressed in T9."
```

---

## Task 8: `_tick_pending_chunk` Speed-Update

**Files:**
- Modify: `klipper_extras/buffer_feeder.py` (`_tick_pending_chunk` Z.~2365)

- [ ] **Step 1: Test schreiben**

```python
def test_c_cont_pending_chunk_uses_current_target_speed(monkeypatch):
    """_tick_pending_chunk submittet Sub-Chunks mit aktuellem target_speed,
    nicht eingefrorenem Speed vom ersten Submit."""
    printer, feeder = make_c_cont_feeder(monkeypatch)
    feeder._state = buffer_feeder.STATE_AUTO
    feeder.sensors._state['hall_empty'] = True
    _populate_tracker_to_ready(feeder, velocity=15.0)
    submits = _capture_submits(feeder, monkeypatch)
    # Initial-Submit mit max_feed_speed
    feeder._on_mcu_flush(flush_time=10.0, step_gen_time=10.0)
    # HALL3 -> Zwischenzone (Buffer fuellt sich) zwischen Sub-Chunks
    feeder.sensors._state['hall_empty'] = False
    # Naechster sub-chunk via _tick_pending_chunk
    feeder._tick_pending_chunk(eventtime=10.1)
    # Erwarten: sub-chunk-speed = aktueller target (extruder_velocity)
    assert submits[1]['speed'] == pytest.approx(15.0, abs=0.5)
```

- [ ] **Step 2: Tests laufen — RED**

- [ ] **Step 3: _tick_pending_chunk-Update**

In `_tick_pending_chunk` (Z.~2365): wo die Sub-Chunk-Submits geschickt werden, ersetze festen `feed_speed` durch `_compute_target_feed_speed()`:

```python
# C-cont T8: Sub-Chunk-Speed dynamisch aus SpeedModulator.
# Frueher fest auf _continuous_feed_speed/feed_speed (init beim
# ersten Submit) — fuehrte zu Speed-Lag wenn HALL-State zwischen
# Sub-Chunks wechselte.
sub_chunk_speed = self._compute_target_feed_speed()
if sub_chunk_speed <= 0.0:
    # HALL1 active mid-chunk — beende Pending-Stream
    self._pending_remaining_mm = 0.0
    return
self._continuous_feed_speed = sub_chunk_speed
# ... existing submit logic with sub_chunk_speed
```

- [ ] **Step 4: Tests laufen — GREEN**

- [ ] **Step 5: Commit**

```bash
git commit -m "feat(buffer_feeder): _tick_pending_chunk uses target_speed (C-cont T8)"
```

---

## Task 9: Bestehende Tests adressieren

**Files:**
- Modify: `tests/test_*.py` (5-15 Tests die OVERFLOW-Sofort-Trigger im STATE_AUTO erwarten)

- [ ] **Step 1: Identify failing tests**

Run:
```bash
PYTHONPATH=. venv-test/Scripts/pytest.exe tests/ --tb=line -q 2>&1 | grep FAILED
```

Erwartet: 5-15 FAILED Tests. Sammle die Namen.

- [ ] **Step 2: Per failure analysieren**

Für jeden FAILED Test:
- Lesen was er testet
- Wenn er HALL1-Edge → sofortiger OVERFLOW erwartet: **Anpassen** auf C-cont-Semantik (HALL1 → soft-defer, OVERFLOW erst nach Persist)
- Wenn er anders bricht: **echter Bug** → Implementation reviewen

- [ ] **Step 3: Tests anpassen**

Beispiel-Pattern für betroffene Tests:

```python
# Alt:
feeder.sensors._set_semantic_state('hall_overflow', True)
assert feeder._state == STATE_OVERFLOW  # FAIL in C-cont

# Neu (C-cont):
feeder.sensors._set_semantic_state('hall_overflow', True)
# In STATE_AUTO ist HALL1 ein soft-trigger:
if pre_state == STATE_AUTO:
    assert feeder._state == STATE_AUTO  # bleibt
    assert feeder._hall1_active_since is not None
    # Persist-Test extra:
    feeder.reactor.now = feeder._hall1_active_since + 2.5
    feeder._main_tick(eventtime=feeder.reactor.now)
    assert feeder._state == STATE_OVERFLOW
else:
    assert feeder._state == STATE_OVERFLOW
```

- [ ] **Step 4: Full-Suite GREEN**

Run:
```bash
PYTHONPATH=. venv-test/Scripts/pytest.exe tests/ --tb=no -q
```

Erwartet: alle Tests grün.

- [ ] **Step 5: Commit**

```bash
git add tests/
git commit -m "test: adapt existing tests to C-cont HALL1-soft-trigger (C-cont T9)"
```

---

## Task 10: Diagnostik-Logs (buffer_debug_metrics)

**Files:**
- Modify: `klipper_extras/buffer_feeder.py` (Diagnostik-Hook im `_main_tick`)

- [ ] **Step 1: Test schreiben**

```python
def test_c_cont_metrics_emitted_when_enabled(monkeypatch, caplog):
    """buffer_debug_metrics=True -> _main_tick emittiert Metrics-Log
    alle 1s mit state/hall/tracker/target_speed."""
    printer, feeder = make_c_cont_feeder(
        monkeypatch, cfg_overrides={'buffer_debug_metrics': True})
    feeder._state = buffer_feeder.STATE_AUTO
    feeder.sensors._state['hall_empty'] = True
    _populate_tracker_to_ready(feeder, velocity=15.0)
    feeder.reactor.now = 10.0
    feeder._main_tick(eventtime=10.0)
    feeder.reactor.now = 11.0  # +1s
    with caplog.at_level('INFO'):
        feeder._main_tick(eventtime=11.0)
    metrics = [r for r in caplog.records if 'buffer_metrics' in r.message]
    assert len(metrics) >= 1


def test_c_cont_metrics_not_emitted_when_disabled(monkeypatch, caplog):
    """buffer_debug_metrics=False -> kein Metrics-Log."""
    printer, feeder = make_c_cont_feeder(monkeypatch)  # default False
    feeder._state = buffer_feeder.STATE_AUTO
    with caplog.at_level('INFO'):
        feeder._main_tick(eventtime=10.0)
    metrics = [r for r in caplog.records if 'buffer_metrics' in r.message]
    assert len(metrics) == 0
```

- [ ] **Step 2: Tests laufen — RED**

- [ ] **Step 3: Metrics-Hook implementieren**

In `_main_tick` ergänze rate-limited Metrics-Log:

```python
# C-cont T10: Diagnostik-Logs (alle 1s wenn buffer_debug_metrics).
if self.buffer_debug_metrics:
    last_metrics = getattr(self, '_last_metrics_log_time', 0.0)
    if eventtime - last_metrics >= 1.0:
        target_speed = self._compute_target_feed_speed()
        flow = self.velocity_tracker.get_volumetric_flow()
        logging.info(
            "buffer_metrics: state=%s hall=[H3:%s H2:%s H1:%s] "
            "tracker_vel=%.1fmm/s flow=%.1fmm3/s ready=%s "
            "target_speed=%.1fmm/s "
            "pending_remaining=%.1fmm hall1_persist=%s",
            self._state,
            'on' if self.hall_empty else 'off',
            'on' if self.hall_full else 'off',
            'on' if self.hall_overflow else 'off',
            self.velocity_tracker.get_velocity(),
            flow,
            self.velocity_tracker.is_ready(),
            target_speed,
            self._pending_remaining_mm,
            ("%.2fs" % (self.reactor.monotonic() - self._hall1_active_since))
                if self._hall1_active_since is not None else "off")
        self._last_metrics_log_time = eventtime
```

Plus Init in `__init__`:

```python
# C-cont T10: Rate-Limit fuer Metrics-Log (1s).
self._last_metrics_log_time = 0.0
```

- [ ] **Step 4: Tests laufen — GREEN**

- [ ] **Step 5: Commit**

```bash
git add klipper_extras/buffer_feeder.py tests/test_c_cont_streaming.py
git commit -m "feat(buffer_feeder): buffer_debug_metrics diagnostic logs (C-cont T10)"
```

---

## Task 11: Codex-Verify End-to-End

**Files:** keine (Verify-Run)

- [ ] **Step 1: Full-Suite-Verify**

Run:
```bash
PYTHONPATH=. venv-test/Scripts/pytest.exe tests/ --tb=short -q
```

Erwartet: alle Tests grün, gesamt ~370-380.

- [ ] **Step 2: Codex-Verify-Loop**

Via `codex:rescue` Skill mit folgenden Fragen:

1. C-cont SpeedModulator-Logik korrekt vs. Spec? HALL-State-Mapping zu target_speed wie spezifiziert?
2. HALL1-Soft-Trigger sicher? Persist-Check race-frei?
3. P7-78 `_last_mcu_flush_time` + P7-79 Defer-Guard im `_on_mcu_flush` Umbau erhalten?
4. Interaktion mit existierenden Schutzschildern (P7-66..P7-77)?
5. Move-Splitting (`interrupt_chunk_mm`) unverändert?
6. Pending-Chunk-Speed (T8) korrekt: keine Race mit Pending-Pipeline?
7. Diagnostik-Logs nur bei `buffer_debug_metrics=True` (kein Production-Spam)?

Falls Codex HIGH/CRITICAL → STOPPEN, dokumentieren, nicht auto-fixen.

- [ ] **Step 3: Bei PASS — End-Commit / Branch-Push**

```bash
git status
git log --oneline -15  # T1-T10 commits + dieser final-Commit
```

Wenn alles clean: branch ist bereit für Hardware-Test.

```bash
# NICHT pushen ohne User-OK
echo "Branch feature/c-cont-streaming bereit für User-Review + Push-Approval."
```

---

## Self-Review-Punkte

**Spec-Coverage:**
- ✅ ExtruderVelocityTracker (Task 1)
- ✅ Tracker-Integration (Task 2)
- ✅ cfg-Params (Task 3)
- ✅ SpeedModulator (Task 4)
- ✅ HALL1-Soft-Trigger + Persist (Tasks 5, 6)
- ✅ Continuous-Streaming `_on_mcu_flush` (Task 7)
- ✅ Pending-Chunk-Speed (Task 8)
- ✅ Bestehende Tests (Task 9)
- ✅ Diagnostik (Task 10)
- ✅ Codex-Verify (Task 11)
- ❓ Hardware-Test-Plan: separat, nicht im Plan (folgt nach Branch-Approval)

**Placeholder-Scan:** Keine TBDs, alle Code-Steps haben konkreten Code.

**Type-Consistency:** `target_speed` (float, mm/s) ist konsistent durchgängig. `_compute_target_feed_speed()` returns float. `_hall1_active_since: Optional[float]` (None oder reactor.monotonic()).

**Bekannte Risiken:**
- Task 7-Umbau impacts viele bestehende Code-Pfade → Task 9 erwartet 5-15 Test-Adjustments
- HALL1-Soft-Trigger im STATE_AUTO ist eine breaking semantic change → Macros / externe Caller müssen evtl. adressiert werden (kein expliziter Task — falls Probleme auftreten, ad-hoc fixen)

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-05-13-c-cont-streaming.md`. Two execution options:

**1. Subagent-Driven (recommended)** — Dispatch fresh subagent per task, review between tasks, fast iteration. Geeignet für 11 Tasks.

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints. Geeignet wenn User die Schritte live verfolgen will.

Welche Option?
