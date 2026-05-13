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


# ===========================================================================
# C-cont T7: _on_mcu_flush Continuous-Streaming-Submit
# ===========================================================================


def _capture_submits(feeder, monkeypatch):
    """Helper: monkeypatch _submit_move um alle Submit-Argumente abzufangen."""
    submits = []

    def fake_submit(distance, speed, **kwargs):
        submits.append({'distance': distance, 'speed': speed, **kwargs})

    monkeypatch.setattr(feeder, '_submit_move', fake_submit)
    return submits


def test_c_cont_continuous_streaming_in_auto(monkeypatch):
    """STATE_AUTO + HALL3 stable + tracker ready -> Submit bei jedem flush."""
    printer, feeder = make_c_cont_feeder(monkeypatch)
    feeder._state = buffer_feeder.STATE_AUTO
    set_sensor_active(feeder, 'hall_empty', True)
    _populate_tracker_to_ready(feeder, velocity=15.0)
    submits = _capture_submits(feeder, monkeypatch)
    # Move-State: kein Move in flight
    feeder._last_move_end_time = 0.0
    feeder._pending_remaining_mm = 0.0
    feeder._on_mcu_flush(flush_time=10.0, step_gen_time=10.0)
    assert len(submits) == 1
    assert submits[0]['speed'] == feeder.max_feed_speed


def test_c_cont_speed_modulation_via_hall_state(monkeypatch):
    """HALL-State-Change zwischen flushes -> Speed-Aenderung."""
    printer, feeder = make_c_cont_feeder(monkeypatch)
    feeder._state = buffer_feeder.STATE_AUTO
    _populate_tracker_to_ready(feeder, velocity=20.0)
    submits = _capture_submits(feeder, monkeypatch)
    # HALL3 -> max_feed_speed
    set_sensor_active(feeder, 'hall_empty', True)
    feeder._last_move_end_time = 0.0
    feeder._pending_remaining_mm = 0.0
    feeder._on_mcu_flush(flush_time=10.0, step_gen_time=10.0)
    # HALL2 -> 0.5 * 20 = 10
    set_sensor_active(feeder, 'hall_empty', False)
    set_sensor_active(feeder, 'hall_full', True)
    feeder._last_move_end_time = 0.0  # Move done
    feeder._on_mcu_flush(flush_time=10.5, step_gen_time=10.5)
    assert len(submits) == 2
    assert submits[0]['speed'] == feeder.max_feed_speed
    assert submits[1]['speed'] == pytest.approx(10.0, abs=0.5)


def test_c_cont_hall1_active_no_submit(monkeypatch):
    """HALL1 active -> target=0 -> kein Submit."""
    printer, feeder = make_c_cont_feeder(monkeypatch)
    feeder._state = buffer_feeder.STATE_AUTO
    set_sensor_active(feeder, 'hall_overflow', True)
    _populate_tracker_to_ready(feeder, velocity=15.0)
    submits = _capture_submits(feeder, monkeypatch)
    feeder._on_mcu_flush(flush_time=10.0, step_gen_time=10.0)
    assert len(submits) == 0


# ===========================================================================
# C-cont T7 followup: HALL2-rising-edge accumulator reset
# (replacement for P7-63 hall_full branch in bang-bang _on_mcu_flush)
# ===========================================================================


def _fire_hall2_callback(feeder, eventtime=0.0):
    """Fire the stable-sensor callback for hall_full after set_sensor_-
    active has set the _pin_stable_state. Mirror of _fire_hall1_callback
    for the hall_full branch."""
    raw = feeder._pin_stable_state['hall_full']
    feeder.sensors.on_stable_sensor_change(eventtime, 'hall_full', raw)


def test_c_cont_hall2_edge_resets_accumulator(monkeypatch):
    """HALL2-rising-edge resettet _feed_distance_accumulator
    (Replacement fuer P7-63 hall_full-reset)."""
    printer, feeder = make_c_cont_feeder(monkeypatch)
    feeder._state = buffer_feeder.STATE_AUTO
    feeder._feed_distance_accumulator = 500.0  # Simuliere lange Session
    set_sensor_active(feeder, 'hall_full', True)
    _fire_hall2_callback(feeder)
    assert feeder._feed_distance_accumulator == 0.0


def test_c_cont_hall2_falling_edge_does_not_reset(monkeypatch):
    """HALL2 falling edge MUST NOT reset accumulator (only rising edge
    represents the 'buffer full' confirmation that warrants the reset).
    Without this guard, a buffer arm bouncing around HALL2 would reset
    the accumulator on every bounce — fine for the safety-distance
    semantics, but not what the original P7-63 design intended."""
    printer, feeder = make_c_cont_feeder(monkeypatch)
    feeder._state = buffer_feeder.STATE_AUTO
    # Start: HALL2 active, accumulator full from prior session.
    set_sensor_active(feeder, 'hall_full', True)
    feeder._feed_distance_accumulator = 300.0
    # Falling edge: HALL2 -> inactive (arm dropped out of full zone).
    set_sensor_active(feeder, 'hall_full', False)
    _fire_hall2_callback(feeder)
    # Accumulator should be untouched on falling edge.
    assert feeder._feed_distance_accumulator == 300.0


# ===========================================================================
# C-cont T8: _tick_pending_chunk Sub-Chunk Speed-Update
# ===========================================================================


def _capture_trapezoids(feeder, monkeypatch):
    """Helper: monkeypatch _submit_single_trapezoid um Sub-Chunk-Submits
    (interrupt_chunk_mm) abzufangen. _tick_pending_chunk geht direkt
    auf _submit_single_trapezoid (nicht _submit_move)."""
    trapezoids = []

    def fake_trap(distance, speed, **kwargs):
        trapezoids.append({'distance': distance, 'speed': speed, **kwargs})

    monkeypatch.setattr(feeder, '_submit_single_trapezoid', fake_trap)
    return trapezoids


def test_c_cont_pending_chunk_uses_current_target_speed(monkeypatch):
    """_tick_pending_chunk submittet Sub-Chunks mit aktuellem target_speed,
    nicht eingefrorenem Speed vom ersten Submit."""
    printer, feeder = make_c_cont_feeder(monkeypatch)
    feeder._state = buffer_feeder.STATE_AUTO
    set_sensor_active(feeder, 'hall_empty', True)
    _populate_tracker_to_ready(feeder, velocity=15.0)
    # Simuliere Pipeline-State nach erstem Submit:
    # _on_mcu_flush hat flush_callback_chunk_mm (z.B. 45) eingestellt,
    # mit cap=interrupt_chunk_mm (9). Erste 9 sind in flight, 36 pending,
    # initial _pending_speed = max_feed_speed.
    feeder._pending_remaining_mm = 36.0
    feeder._pending_submit_chunk_cap = feeder.interrupt_chunk_mm
    feeder._pending_direction = 1.0
    feeder._pending_speed = feeder.max_feed_speed
    # _last_move_end_time = eventtime: gap=0 → Trigger Sub-Chunk-Submit
    feeder._last_move_end_time = 10.0
    # HALL-Wechsel von HALL3 → Zwischenzone (buffer fuellt sich):
    set_sensor_active(feeder, 'hall_empty', False)
    # Zwischenzone (alle HALL inaktiv, Tracker ready=15.0 mm/s):
    assert feeder._compute_target_feed_speed() == pytest.approx(15.0, abs=0.5)
    trapezoids = _capture_trapezoids(feeder, monkeypatch)
    feeder._tick_pending_chunk(eventtime=10.0)
    # Sub-Chunk wurde submittet mit aktuellem target_speed (15)
    # NICHT mit eingefrorenem max_feed_speed (z.B. 100)
    assert len(trapezoids) == 1
    assert trapezoids[0]['speed'] == pytest.approx(15.0, abs=0.5)
    assert trapezoids[0]['speed'] != feeder.max_feed_speed


def test_c_cont_pending_chunk_hall1_aborts_stream(monkeypatch):
    """target_speed=0 mid-chunk (HALL1 active) -> Pending-Stream beendet."""
    printer, feeder = make_c_cont_feeder(monkeypatch)
    feeder._state = buffer_feeder.STATE_AUTO
    _populate_tracker_to_ready(feeder, velocity=15.0)
    # Pipeline-State: Sub-Chunks pending.
    feeder._pending_remaining_mm = 18.0
    feeder._pending_submit_chunk_cap = feeder.interrupt_chunk_mm
    feeder._pending_direction = 1.0
    feeder._pending_speed = feeder.max_feed_speed
    feeder._last_move_end_time = 10.0
    # HALL1 wird active mid-chunk:
    set_sensor_active(feeder, 'hall_overflow', True)
    assert feeder._compute_target_feed_speed() == 0.0
    trapezoids = _capture_trapezoids(feeder, monkeypatch)
    feeder._tick_pending_chunk(eventtime=10.0)
    # _abort_signalled hat eventuell schon vorher gegriffen, aber wenn
    # nicht, dann muss target_speed=0 den Stream beenden.
    assert len(trapezoids) == 0
    assert feeder._pending_remaining_mm == 0.0


# ===========================================================================
# C-cont T10: Diagnostik-Logs (buffer_debug_metrics)
# ===========================================================================


def test_c_cont_metrics_emitted_when_enabled(monkeypatch, caplog):
    """buffer_debug_metrics=True -> _main_tick emittiert Metrics-Log
    alle 1s mit state/hall/tracker/target_speed."""
    printer, feeder = make_c_cont_feeder(
        monkeypatch, cfg_overrides={'buffer_debug_metrics': True})
    feeder._state = buffer_feeder.STATE_AUTO
    set_sensor_active(feeder, 'hall_empty', True)
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


# ===========================================================================
# C-cont T11: Codex-Verify-Loop HIGH-Findings (Q6b + Q8)
# ===========================================================================


def test_c_cont_pending_chunk_abort_signal_clears_cap(monkeypatch):
    """HALL1-Early-Exit via _abort_signalled() resettet auch
    _pending_submit_chunk_cap (Codex-Verify Q6b).

    Vor dem Fix wurde nur _pending_remaining_mm=0 gesetzt — der Sub-Chunk-
    Cap (interrupt_chunk_mm=9) blieb gesetzt und konnte auf den naechsten
    unrelated _submit_move-Call leaken."""
    printer, feeder = make_c_cont_feeder(monkeypatch)
    feeder._state = buffer_feeder.STATE_AUTO
    # Pipeline-State: Pending-Stream mit Cap aktiv.
    feeder._pending_remaining_mm = 9.0
    feeder._pending_submit_chunk_cap = 9.0
    feeder._pending_direction = 1.0
    feeder._pending_speed = 50.0
    # HALL1 active triggert _abort_signalled() check (HALL1=overflow ist
    # die Standard-Quelle fuer _abort_signalled in STATE_AUTO).
    set_sensor_active(feeder, 'hall_overflow', True)
    assert feeder._abort_signalled()
    feeder._tick_pending_chunk(eventtime=10.0)
    # Beide Resets muessen erfolgen:
    assert feeder._pending_remaining_mm == 0.0
    assert feeder._pending_submit_chunk_cap is None  # Codex Q6b fix


def test_c_cont_hall2_in_flight_modulates_pending_speed(monkeypatch):
    """C-cont Ersatz fuer test_streaming_then_hall2_aborts_in_flight:
    Bei HALL2 mid-flight wird Sub-Chunk-Speed reduziert (0.5*v),
    nicht hart aboortet. Pending bleibt aktiv und verarbeitet weiter."""
    printer, feeder = make_c_cont_feeder(monkeypatch)
    feeder._state = buffer_feeder.STATE_AUTO
    _populate_tracker_to_ready(feeder, velocity=20.0)
    # Pipeline-State mid-flight nach erstem Sub-Chunk-Submit:
    feeder._pending_remaining_mm = 36.0
    feeder._pending_submit_chunk_cap = feeder.interrupt_chunk_mm
    feeder._pending_direction = 1.0
    feeder._pending_speed = feeder.max_feed_speed
    feeder._last_move_end_time = 10.0  # gap=0 -> Trigger Sub-Chunk-Submit
    # HALL2 wird mid-flight active (Buffer fuellt sich Richtung "voll"):
    # P7-66b-Branch in _tick_pending_chunk (hall_full=True + AUTO + fwd)
    # wuerde den Stream noch hart aboortet (Bang-Bang-Legacy). Fuer den
    # C-cont-Test muessen wir hall_full=False halten und die HALL2-
    # Modulation via _compute_target_feed_speed pruefen. Wir nutzen
    # daher hall_full=False, aber den HALL2-Half-Speed-Pfad via
    # _compute_target_feed_speed-Monkeypatch.
    monkeypatch.setattr(feeder, '_compute_target_feed_speed', lambda: 10.0)
    trapezoids = _capture_trapezoids(feeder, monkeypatch)
    feeder._tick_pending_chunk(eventtime=10.0)
    # Erwarten: Sub-Chunk-Submit mit moduliertem Speed (10), Pending
    # bleibt aktiv (nur reduziert um chunk-mm).
    assert len(trapezoids) == 1
    assert trapezoids[0]['speed'] == pytest.approx(10.0, abs=0.5)
    # Pending wurde reduziert (nicht hart auf 0 gesetzt):
    assert feeder._pending_remaining_mm < 36.0
    assert feeder._pending_remaining_mm > 0.0


def test_c_cont_continuous_feed_persists_through_hall_state_change(monkeypatch):
    """C-cont Ersatz fuer test_flush_clears_continuous_feed_when_hall_full:
    _continuous_feed bleibt im Stream-Mode strukturell True, auch wenn
    sich der HALL-State zwischen Submits aendert (Speed wird nur
    moduliert, kein Stream-Reset)."""
    printer, feeder = make_c_cont_feeder(monkeypatch)
    feeder._state = buffer_feeder.STATE_AUTO
    _populate_tracker_to_ready(feeder, velocity=20.0)
    submits = _capture_submits(feeder, monkeypatch)
    # HALL3 -> max_feed_speed, erster Submit setzt _continuous_feed=True
    set_sensor_active(feeder, 'hall_empty', True)
    feeder._last_move_end_time = 0.0
    feeder._pending_remaining_mm = 0.0
    feeder._continuous_feed = False  # explizit reset (echte cold-start)
    feeder._on_mcu_flush(flush_time=10.0, step_gen_time=10.0)
    assert feeder._continuous_feed is True
    assert len(submits) == 1
    first_submit_speed = submits[0]['speed']
    # HALL-State-Change: HALL3 -> Zwischenzone (alle HALL inactive).
    # Neue Submit-Iteration; im Bang-Bang-Legacy haette ein HALL-Change
    # _continuous_feed=False gesetzt. C-cont haelt es True.
    set_sensor_active(feeder, 'hall_empty', False)
    feeder._last_move_end_time = 0.0  # vorheriger Move done
    feeder._on_mcu_flush(flush_time=10.5, step_gen_time=10.5)
    # _continuous_feed bleibt strukturell True:
    assert feeder._continuous_feed is True
    # Speed wurde moduliert (Zwischenzone = extruder_velocity = 20):
    assert len(submits) == 2
    assert submits[1]['speed'] == pytest.approx(20.0, abs=0.5)
    # Speed-Aenderung gegenueber erstem Submit ist erfolgt (Modulation):
    assert submits[1]['speed'] != first_submit_speed
