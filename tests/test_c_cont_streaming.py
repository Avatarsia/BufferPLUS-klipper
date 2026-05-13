"""Integration-Tests fuer C-cont Continuous-Streaming.

Test-Pattern wie test_p778_print_block_stale_override mit zusaetzlichem
FakeExtruder-Patch (get_status statt last_position) fuer den passiven
ExtruderVelocityTracker.
"""

import types

import pytest

from fakes_klipper import (
    FakeConfig,
    FakePrinter,
    FakePrintStats,
)
from klipper_extras import buffer_feeder


# ---------------------------------------------------------------------------
# Helpers (Pattern aus test_p778_print_block_stale_override uebernommen)
# ---------------------------------------------------------------------------


def set_sensor_active(feeder, sensor_name, active):
    polarity_flip = feeder._pin_polarity_flip[sensor_name]
    raw = (not active) if polarity_flip else active
    feeder._pin_stable_state[sensor_name] = raw
    feeder._pin_raw_state[sensor_name] = raw


def make_c_cont_feeder(monkeypatch, *, print_state='printing',
                       cfg_overrides=None):
    """Feeder in STATE_AUTO mit FakeExtruder fuer Velocity-Tracker.

    Uebernimmt das `make_auto_feeder`-Setup aus test_p778:
      - use_flush_callback_bang_bang=True
      - print_stats(state=print_state)
      - Sensoren quiescent (HALL2-Hysterese-Zwischenzone)

    Zusatz fuer C-cont: aktualisiert FakePrinter.objects['extruder'] so
    dass es ein get_status(eventtime) hat (ExtruderVelocityTracker
    benoetigt get_status(eventtime)['position']).

    cfg_overrides: dict optional, ueberschreibt einzelne config-Werte.
    """
    base = {"use_flush_callback_bang_bang": True}
    if cfg_overrides:
        base.update(cfg_overrides)
    printer = FakePrinter()
    printer.objects['print_stats'] = FakePrintStats(state=print_state)

    # FakeExtruder mit get_status fuer ExtruderVelocityTracker.
    # FakePrinter setzt bereits einen FakeExtruder mit last_position,
    # aber kein get_status. Wir patchen ihn mit einem zusaetzlichen
    # Attribut _position und einer get_status-Methode (Pattern aus
    # tests/test_velocity_tracker.py fixture).
    fake_ext = printer.objects['extruder']
    fake_ext._position = 0.0

    def get_status(eventtime, _ext=fake_ext):
        return {'position': _ext._position}

    fake_ext.get_status = get_status

    config = FakeConfig(printer=printer, values=base)
    feeder = buffer_feeder.BufferFeeder(config)
    printer.fire_event('klippy:connect')
    feeder._startup_grace_done = True
    feeder._state = buffer_feeder.STATE_AUTO
    set_sensor_active(feeder, 'hall_overflow', False)
    set_sensor_active(feeder, 'hall_full', False)
    set_sensor_active(feeder, 'hall_empty', False)
    set_sensor_active(feeder, 'entrance', True)
    return printer, feeder


# ===========================================================================
# C-cont T2: Tracker-Integration in BufferFeeder
# ===========================================================================


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


# ===========================================================================
# C-cont T3: cfg-Params
# ===========================================================================


def test_c_cont_cfg_params_loaded(monkeypatch):
    """Neue cfg-Params werden gelesen mit Defaults."""
    printer, feeder = make_c_cont_feeder(monkeypatch)
    assert hasattr(feeder, 'max_feed_speed')
    assert feeder.max_feed_speed == 100.0
    assert hasattr(feeder, 'hall1_persist_timeout')
    assert feeder.hall1_persist_timeout == 2.0
    assert hasattr(feeder, 'buffer_debug_metrics')
    assert feeder.buffer_debug_metrics is False


def test_c_cont_cfg_params_custom(monkeypatch):
    """Custom cfg-Params funktionieren."""
    overrides = {
        'max_feed_speed': 80.0,
        'hall1_persist_timeout': 3.0,
        'buffer_debug_metrics': True,
    }
    printer, feeder = make_c_cont_feeder(monkeypatch, cfg_overrides=overrides)
    assert feeder.max_feed_speed == 80.0
    assert feeder.hall1_persist_timeout == 3.0
    assert feeder.buffer_debug_metrics is True


# ===========================================================================
# C-cont T4: SpeedModulator (_compute_target_feed_speed)
# ===========================================================================


def _populate_tracker_to_ready(feeder, *, velocity):
    """Fake-Helper: 12 ticks mit linearer Position-Steigerung, damit
    velocity_tracker.is_ready() == True wird."""
    fake_ext = feeder.printer.objects['extruder']
    t = 0.0
    for _ in range(12):
        fake_ext._position = t * velocity
        feeder.velocity_tracker.tick(t)
        t += 0.025


def test_c_cont_modulator_hall1_zero(monkeypatch):
    """HALL1 active -> target_speed = 0 (Notbremse)."""
    printer, feeder = make_c_cont_feeder(monkeypatch)
    set_sensor_active(feeder, 'hall_overflow', True)
    set_sensor_active(feeder, 'hall_empty', False)
    set_sensor_active(feeder, 'hall_full', False)
    assert feeder._compute_target_feed_speed() == 0.0


def test_c_cont_modulator_hall3_max(monkeypatch):
    """HALL3 active (Buffer leer) -> max_feed_speed."""
    printer, feeder = make_c_cont_feeder(monkeypatch)
    set_sensor_active(feeder, 'hall_empty', True)
    set_sensor_active(feeder, 'hall_full', False)
    set_sensor_active(feeder, 'hall_overflow', False)
    _populate_tracker_to_ready(feeder, velocity=15.0)
    assert feeder._compute_target_feed_speed() == feeder.max_feed_speed


def test_c_cont_modulator_hall2_half(monkeypatch):
    """HALL2 active (Buffer voll) -> 0.5 * extruder_velocity."""
    printer, feeder = make_c_cont_feeder(monkeypatch)
    set_sensor_active(feeder, 'hall_empty', False)
    set_sensor_active(feeder, 'hall_full', True)
    set_sensor_active(feeder, 'hall_overflow', False)
    _populate_tracker_to_ready(feeder, velocity=20.0)
    assert feeder._compute_target_feed_speed() == pytest.approx(10.0, abs=0.5)


def test_c_cont_modulator_zwischenzone_balance(monkeypatch):
    """Zwischenzone -> extruder_velocity."""
    printer, feeder = make_c_cont_feeder(monkeypatch)
    set_sensor_active(feeder, 'hall_empty', False)
    set_sensor_active(feeder, 'hall_full', False)
    set_sensor_active(feeder, 'hall_overflow', False)
    _populate_tracker_to_ready(feeder, velocity=12.0)
    assert feeder._compute_target_feed_speed() == pytest.approx(12.0, abs=0.5)


def test_c_cont_modulator_tracker_not_ready_fallback(monkeypatch):
    """Tracker not_ready -> fallback config feed_speed."""
    printer, feeder = make_c_cont_feeder(monkeypatch)
    set_sensor_active(feeder, 'hall_empty', False)
    set_sensor_active(feeder, 'hall_full', False)
    set_sensor_active(feeder, 'hall_overflow', False)
    assert not feeder.velocity_tracker.is_ready()
    assert feeder._compute_target_feed_speed() == feeder.feed_speed


# ===========================================================================
# C-cont T5: HALL1 Soft-Trigger im STATE_AUTO
# ===========================================================================


def _fire_hall1_callback(feeder, eventtime=0.0):
    """Fire the stable-sensor callback after set_sensor_active has set the
    _pin_stable_state. In production this is fired via check_debounce; in
    tests we invoke it directly to bypass the debounce-timer ceremony."""
    raw = feeder._pin_stable_state['hall_overflow']
    feeder.sensors.on_stable_sensor_change(eventtime, 'hall_overflow', raw)


def test_c_cont_hall1_in_auto_defers_no_state_change(monkeypatch):
    """STATE_AUTO + HALL1-Edge -> KEINE state-transition zu OVERFLOW."""
    printer, feeder = make_c_cont_feeder(monkeypatch)
    feeder._state = buffer_feeder.STATE_AUTO
    assert feeder._state == buffer_feeder.STATE_AUTO
    # HALL1 active triggern via set_sensor_active + callback fire
    set_sensor_active(feeder, 'hall_overflow', True)
    _fire_hall1_callback(feeder)
    # State sollte AUTO bleiben (kein OVERFLOW)
    assert feeder._state == buffer_feeder.STATE_AUTO
    # _hall1_active_since muss gesetzt sein
    assert feeder._hall1_active_since is not None


def test_c_cont_hall1_in_load_keeps_immediate_overflow(monkeypatch):
    """Nicht-AUTO-State + HALL1-Edge -> sofortiger OVERFLOW."""
    printer, feeder = make_c_cont_feeder(monkeypatch)
    feeder._state = buffer_feeder.STATE_LOAD_PHASE_1
    set_sensor_active(feeder, 'hall_overflow', True)
    _fire_hall1_callback(feeder)
    # In LOAD-Phase soll HALL1 sofort OVERFLOW triggern
    assert feeder._state == buffer_feeder.STATE_OVERFLOW


def test_c_cont_hall1_cleared_resets_timestamp(monkeypatch):
    """HALL1 cleared (falling edge) -> _hall1_active_since = None."""
    printer, feeder = make_c_cont_feeder(monkeypatch)
    feeder._state = buffer_feeder.STATE_AUTO
    set_sensor_active(feeder, 'hall_overflow', True)
    _fire_hall1_callback(feeder)
    assert feeder._hall1_active_since is not None
    set_sensor_active(feeder, 'hall_overflow', False)
    _fire_hall1_callback(feeder)
    assert feeder._hall1_active_since is None


# ===========================================================================
# C-cont T6: HALL1-Persist-Check im _main_tick
# ===========================================================================


def test_c_cont_hall1_persist_triggers_overflow_safety(monkeypatch):
    """HALL1 active > hall1_persist_timeout -> echter _enter_overflow."""
    printer, feeder = make_c_cont_feeder(monkeypatch)
    feeder._state = buffer_feeder.STATE_AUTO
    feeder.hall1_persist_timeout = 2.0
    # HALL1-Edge (Soft-Trigger via T5)
    set_sensor_active(feeder, 'hall_overflow', True)
    _fire_hall1_callback(feeder)
    assert feeder._state == buffer_feeder.STATE_AUTO  # Soft, kein OVERFLOW
    assert feeder._hall1_active_since is not None
    # Simuliere Timer-Fortschritt: _hall1_active_since wurde auf jetziges
    # reactor.monotonic() gesetzt — manipulieren wir reactor.now direkt.
    feeder._hall1_active_since = 0.0
    feeder.reactor.now = 1.0  # 1s — noch im Timeout
    feeder._main_tick(eventtime=1.0)
    assert feeder._state == buffer_feeder.STATE_AUTO
    feeder.reactor.now = 2.5  # 2.5s — Persist > timeout
    feeder._main_tick(eventtime=2.5)
    assert feeder._state == buffer_feeder.STATE_OVERFLOW


def test_c_cont_hall1_short_blip_no_safety(monkeypatch):
    """HALL1 active < hall1_persist_timeout, dann cleared -> kein OVERFLOW."""
    printer, feeder = make_c_cont_feeder(monkeypatch)
    feeder._state = buffer_feeder.STATE_AUTO
    feeder.hall1_persist_timeout = 2.0
    set_sensor_active(feeder, 'hall_overflow', True)
    _fire_hall1_callback(feeder)
    feeder._hall1_active_since = 0.0
    feeder.reactor.now = 0.5
    feeder._main_tick(eventtime=0.5)
    # HALL1 cleared bevor timeout
    set_sensor_active(feeder, 'hall_overflow', False)
    _fire_hall1_callback(feeder)
    assert feeder._hall1_active_since is None
    feeder.reactor.now = 1.0
    feeder._main_tick(eventtime=1.0)
    assert feeder._state == buffer_feeder.STATE_AUTO
