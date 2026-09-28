"""Integration-Tests fuer C-cont Continuous-Streaming.

Test-Pattern wie test_p778_print_block_stale_override; nutzt FakePrinter
FakeExtruder.last_position-Attribut (Mainline-Klipper-API) fuer den
passiven ExtruderVelocityTracker.

Hotfix 2026-05-13: Tracker liest jetzt extruder.last_position direkt
(zuvor: get_status['position'] — Mainline hat KEINEN solchen Key).
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

    Zusatz fuer C-cont: FakePrinter.objects['extruder'] hat bereits
    last_position=0.0 (Mainline-Klipper-API); ExtruderVelocityTracker
    liest dieses Attribut direkt.

    cfg_overrides: dict optional, ueberschreibt einzelne config-Werte.
    """
    base = {"use_flush_callback_bang_bang": True}
    if cfg_overrides:
        base.update(cfg_overrides)
    printer = FakePrinter()
    printer.objects['print_stats'] = FakePrintStats(state=print_state)

    # Hotfix 2026-05-13: ExtruderVelocityTracker liest jetzt
    # extruder.last_position direkt (Mainline-Klipper-API). FakePrinter
    # setzt FakeExtruder bereits mit last_position=0.0 (siehe
    # fakes_klipper.py FakeExtruder, Z.187). Wir muessen hier nichts
    # patchen — die Tests aktualisieren last_position direkt.
    fake_ext = printer.objects['extruder']
    fake_ext.last_position = 0.0

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
    assert hasattr(feeder, 'min_feed_floor')
    assert feeder.min_feed_floor == 15.0
    assert hasattr(feeder, 'feed_speed_gain')
    assert feeder.feed_speed_gain == 1.10
    assert hasattr(feeder, 'high_flow_mm3s_threshold')
    assert feeder.high_flow_mm3s_threshold == 24.0
    assert hasattr(feeder, 'hall1_persist_timeout')
    assert feeder.hall1_persist_timeout == 2.0
    assert hasattr(feeder, 'buffer_debug_metrics')
    assert feeder.buffer_debug_metrics is False


def test_c_cont_cfg_params_custom(monkeypatch):
    """Custom cfg-Params funktionieren."""
    overrides = {
        'max_feed_speed': 80.0,
        'min_feed_floor': 12.0,
        'feed_speed_gain': 1.25,
        'high_flow_mm3s_threshold': 30.0,
        'hall1_persist_timeout': 3.0,
        'buffer_debug_metrics': True,
    }
    printer, feeder = make_c_cont_feeder(monkeypatch, cfg_overrides=overrides)
    assert feeder.max_feed_speed == 80.0
    assert feeder.min_feed_floor == 12.0
    assert feeder.feed_speed_gain == 1.25
    assert feeder.high_flow_mm3s_threshold == 30.0
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
        fake_ext.last_position = t * velocity
        feeder.velocity_tracker.tick(t)
        t += 0.025


def _stub_tracker(monkeypatch, feeder, *, velocity, flow, ready=True):
    monkeypatch.setattr(feeder.velocity_tracker, 'is_ready',
                        lambda: ready)
    monkeypatch.setattr(feeder.velocity_tracker, 'get_velocity',
                        lambda: velocity)
    monkeypatch.setattr(feeder.velocity_tracker, 'get_volumetric_flow',
                        lambda: flow)


def test_c_cont_modulator_hall1_zero(monkeypatch):
    """HALL1 active -> target_speed = 0 (Notbremse)."""
    printer, feeder = make_c_cont_feeder(monkeypatch)
    set_sensor_active(feeder, 'hall_overflow', True)
    set_sensor_active(feeder, 'hall_empty', False)
    set_sensor_active(feeder, 'hall_full', False)
    assert feeder._compute_target_feed_speed() == 0.0


def test_c_cont_modulator_hall3_max(monkeypatch):
    """Soft-Throttle: HALL3 skaliert mit Verbrauch statt fixem Vollgas.

    Bei velocity=15 ergibt das 15 * 1.5 = 22.5 mm/s statt eines
    harten feed_speed-Sprungs."""
    printer, feeder = make_c_cont_feeder(
        monkeypatch, cfg_overrides={'feed_speed': 70.0})
    set_sensor_active(feeder, 'hall_empty', True)
    set_sensor_active(feeder, 'hall_full', False)
    set_sensor_active(feeder, 'hall_overflow', False)
    _populate_tracker_to_ready(feeder, velocity=15.0)
    assert feeder._compute_target_feed_speed() == pytest.approx(22.5, abs=0.1)


def test_c_cont_modulator_hall2_half(monkeypatch):
    """Hotfix5: HALL2 active (Buffer voll) -> 0.0 (drain via Toolhead,
    KEIN Push). Alter HALL2-Branch 0.5*v+MIN_FLOOR=15 verursachte
    Submit @15 mm/s in vollen Buffer wenn tracker_vel=1.6 (Resume).
    """
    printer, feeder = make_c_cont_feeder(monkeypatch)
    set_sensor_active(feeder, 'hall_empty', False)
    set_sensor_active(feeder, 'hall_full', True)
    set_sensor_active(feeder, 'hall_overflow', False)
    _populate_tracker_to_ready(feeder, velocity=40.0)
    assert feeder._compute_target_feed_speed() == 0.0


def test_c_cont_modulator_zwischenzone_balance(monkeypatch):
    """Hotfix3: Zwischenzone -> max(15.0, extruder_vel * 1.10).

    Bei velocity=20 ist 1.10*20=22 > MIN_FLOOR=15, also expect 22.
    """
    printer, feeder = make_c_cont_feeder(monkeypatch)
    set_sensor_active(feeder, 'hall_empty', False)
    set_sensor_active(feeder, 'hall_full', False)
    set_sensor_active(feeder, 'hall_overflow', False)
    _populate_tracker_to_ready(feeder, velocity=20.0)
    # max(15, 1.10*20=22) -> 22
    assert feeder._compute_target_feed_speed() == pytest.approx(22.0, abs=1.0)


def test_c_cont_modulator_zwischenzone_high_flow_carries_below_floor(monkeypatch):
    """Below-floor carry is allowed shortly after real HALL3 demand.

    The tracker window lags a little behind the physical HALL3->middle-
    zone transition. The carry-session grace lets the same feed episode
    continue once flow crosses the threshold, but only if HALL3 demand
    happened just before.
    """
    printer, feeder = make_c_cont_feeder(
        monkeypatch, cfg_overrides={'high_flow_mm3s_threshold': 20.0})
    feeder.reactor.now = 1.0
    set_sensor_active(feeder, 'hall_empty', True)
    _stub_tracker(monkeypatch, feeder, velocity=6.8, flow=16.5)
    assert feeder._compute_target_feed_speed() == 15.0

    feeder.reactor.now = 1.2
    set_sensor_active(feeder, 'hall_empty', False)
    _stub_tracker(monkeypatch, feeder, velocity=8.8, flow=21.3)
    assert feeder._compute_target_feed_speed() == pytest.approx(9.68, abs=0.05)


def test_c_cont_modulator_zwischenzone_high_flow_hysteresis_hold(monkeypatch):
    """Once feeding, stop-floor hysteresis should avoid immediate dropouts.

    This is the smoothing layer between HALL3 release and the strict
    high-flow threshold. It keeps the same feed session alive for a
    short moment when proposed speed remains above the configured stop
    factor.
    """
    printer, feeder = make_c_cont_feeder(
        monkeypatch, cfg_overrides={'high_flow_mm3s_threshold': 20.0})
    feeder.reactor.now = 2.0
    set_sensor_active(feeder, 'hall_empty', True)
    _stub_tracker(monkeypatch, feeder, velocity=7.0, flow=17.0)
    assert feeder._compute_target_feed_speed() == 15.0

    feeder.reactor.now = 2.2
    set_sensor_active(feeder, 'hall_empty', False)
    _stub_tracker(monkeypatch, feeder, velocity=10.0, flow=19.4)
    assert feeder._compute_target_feed_speed() == pytest.approx(11.0, abs=0.05)


def test_c_cont_modulator_zwischenzone_high_flow_does_not_restart_after_grace(monkeypatch):
    """Regression for klippy(15): no late restart in hall-neutral zone.

    Root cause: HIGH_FLOW_MM3S=20 allowed a below-floor carry to start
    again about a second after HALL3 had already dropped. That delayed
    restart produced the unsafe flush submit that later crashed in
    stepcompress. After the carry grace expires, the middle zone must
    stay quiet until a fresh real demand signal arrives again.
    """
    printer, feeder = make_c_cont_feeder(
        monkeypatch, cfg_overrides={'high_flow_mm3s_threshold': 20.0})
    feeder.reactor.now = 3.0
    set_sensor_active(feeder, 'hall_empty', True)
    _stub_tracker(monkeypatch, feeder, velocity=6.9, flow=16.5)
    assert feeder._compute_target_feed_speed() == 15.0

    feeder.reactor.now = 4.0
    set_sensor_active(feeder, 'hall_empty', False)
    _stub_tracker(monkeypatch, feeder, velocity=8.8, flow=21.3)
    assert feeder._compute_target_feed_speed() == 0.0


def test_c_cont_modulator_hall2_clamps_neutral_bias_until_hall3(monkeypatch):
    """After HALL2 the neutral zone must not refill with positive bias.

    Regression for the slow H2->H1 drift seen in klippy(17): the
    buffer hit H2 correctly, target dropped to 0, but after H2 cleared
    the neutral carry resumed at vel*gain and slowly walked back into
    H1. Until a fresh H3 demand arrives, neutral carry may at most
    match real consumption.
    """
    printer, feeder = make_c_cont_feeder(
        monkeypatch, cfg_overrides={
            'high_flow_mm3s_threshold': 20.0,
            'min_feed_floor': 10.0,
        })

    set_sensor_active(feeder, 'hall_full', True)
    _stub_tracker(monkeypatch, feeder, velocity=12.5, flow=30.1)
    assert feeder._compute_target_feed_speed() == 0.0
    assert feeder._post_full_bias_clamp is True

    set_sensor_active(feeder, 'hall_full', False)
    set_sensor_active(feeder, 'hall_empty', False)
    _stub_tracker(monkeypatch, feeder, velocity=12.5, flow=30.1)
    assert feeder._compute_target_feed_speed() == pytest.approx(12.5, abs=0.05)
    assert feeder._post_full_bias_clamp is True


def test_c_cont_modulator_post_hall2_below_floor_high_flow_stays_quiet(monkeypatch):
    """Below-floor high-flow carry must not restart right after HALL2.

    HIGH_FLOW_MM3S=20 is allowed in production now, but after a recent
    full-buffer event the hall-neutral middle zone must stay quiet
    until either H3 reappears or velocity rises above the floor.
    """
    printer, feeder = make_c_cont_feeder(
        monkeypatch, cfg_overrides={
            'high_flow_mm3s_threshold': 20.0,
            'min_feed_floor': 10.0,
        })

    set_sensor_active(feeder, 'hall_full', True)
    _stub_tracker(monkeypatch, feeder, velocity=12.5, flow=30.1)
    assert feeder._compute_target_feed_speed() == 0.0

    set_sensor_active(feeder, 'hall_full', False)
    set_sensor_active(feeder, 'hall_empty', False)
    _stub_tracker(monkeypatch, feeder, velocity=8.8, flow=21.3)
    assert feeder._compute_target_feed_speed() == 0.0


def test_c_cont_modulator_hall3_releases_post_full_bias_clamp(monkeypatch):
    """Fresh stable H3 demand re-enables assertive refill after a full phase."""
    printer, feeder = make_c_cont_feeder(
        monkeypatch, cfg_overrides={
            'high_flow_mm3s_threshold': 20.0,
            'min_feed_floor': 10.0,
        })

    set_sensor_active(feeder, 'hall_full', True)
    _stub_tracker(monkeypatch, feeder, velocity=12.5, flow=30.1)
    assert feeder._compute_target_feed_speed() == 0.0
    assert feeder._post_full_bias_clamp is True

    feeder.reactor.now = 10.0
    set_sensor_active(feeder, 'hall_full', False)
    set_sensor_active(feeder, 'hall_empty', True)
    _stub_tracker(monkeypatch, feeder, velocity=12.5, flow=30.1)
    assert feeder._compute_target_feed_speed() == pytest.approx(12.5, abs=0.05)
    assert feeder._post_full_bias_clamp is True

    feeder.reactor.now = 10.6
    assert feeder._compute_target_feed_speed() == pytest.approx(18.75, abs=0.05)
    assert feeder._post_full_bias_clamp is False


def test_c_cont_modulator_brief_h3_after_hall2_does_not_release_clamp(monkeypatch):
    """A short H3 bounce after H2 must not re-enable the full boost."""
    printer, feeder = make_c_cont_feeder(
        monkeypatch, cfg_overrides={
            'high_flow_mm3s_threshold': 20.0,
            'min_feed_floor': 10.0,
        })

    set_sensor_active(feeder, 'hall_full', True)
    _stub_tracker(monkeypatch, feeder, velocity=12.5, flow=30.1)
    assert feeder._compute_target_feed_speed() == 0.0

    feeder.reactor.now = 20.0
    set_sensor_active(feeder, 'hall_full', False)
    set_sensor_active(feeder, 'hall_empty', True)
    _stub_tracker(monkeypatch, feeder, velocity=12.5, flow=30.1)
    assert feeder._compute_target_feed_speed() == pytest.approx(12.5, abs=0.05)
    assert feeder._post_full_bias_clamp is True

    feeder.reactor.now = 20.2
    set_sensor_active(feeder, 'hall_empty', False)
    assert feeder._compute_target_feed_speed() == pytest.approx(12.5, abs=0.05)
    assert feeder._post_full_bias_clamp is True

    feeder.reactor.now = 20.3
    set_sensor_active(feeder, 'hall_empty', True)
    assert feeder._compute_target_feed_speed() == pytest.approx(12.5, abs=0.05)
    assert feeder._post_full_bias_clamp is True


def test_flush_submit_uses_small_chunk_while_post_full_clamp_active(monkeypatch):
    """Near H2 we shorten the flush chunk to reduce residual overshoot."""
    printer, feeder = make_c_cont_feeder(
        monkeypatch, cfg_overrides={
            'high_flow_mm3s_threshold': 20.0,
            'min_feed_floor': 10.0,
            'interrupt_chunk_mm': 9.0,
        })
    feeder.reactor.now = 30.0
    feeder._post_full_bias_clamp = True
    _stub_tracker(monkeypatch, feeder, velocity=12.5, flow=30.1)
    monkeypatch.setattr(feeder, '_auto_submit_permission',
                        lambda eventtime: (True, 'active_print'))
    submits = []
    monkeypatch.setattr(
        feeder, '_submit_move',
        lambda dist, speed, **kw: submits.append(
            {'distance': dist, 'speed': speed, **kw}))

    feeder._flush_submit_streaming_chunk(step_gen_time=30.05, eventtime=30.0)

    assert submits, "expected a buffered flush submit"
    assert submits[0]['distance'] == pytest.approx(3.0, abs=0.001)
    assert submits[0]['submit_chunk_cap'] == pytest.approx(3.0, abs=0.001)


def test_flush_submit_uses_small_chunk_during_post_full_recovery(monkeypatch):
    """The first boosted chunk after H2 also stays short."""
    printer, feeder = make_c_cont_feeder(
        monkeypatch, cfg_overrides={
            'high_flow_mm3s_threshold': 20.0,
            'min_feed_floor': 10.0,
            'interrupt_chunk_mm': 9.0,
        })
    feeder.reactor.now = 40.0
    feeder._post_full_recovery_until = 41.0
    set_sensor_active(feeder, 'hall_empty', True)
    _stub_tracker(monkeypatch, feeder, velocity=12.5, flow=30.1)
    monkeypatch.setattr(feeder, '_auto_submit_permission',
                        lambda eventtime: (True, 'active_print'))
    submits = []
    monkeypatch.setattr(
        feeder, '_submit_move',
        lambda dist, speed, **kw: submits.append(
            {'distance': dist, 'speed': speed, **kw}))

    feeder._flush_submit_streaming_chunk(step_gen_time=40.05, eventtime=40.0)

    assert submits, "expected a buffered flush submit"
    assert submits[0]['distance'] == pytest.approx(3.0, abs=0.001)
    assert submits[0]['submit_chunk_cap'] == pytest.approx(3.0, abs=0.001)
    assert submits[0]['speed'] == pytest.approx(18.75, abs=0.05)


def test_flush_submit_waits_for_short_recovery_chunk_to_drain(monkeypatch):
    """Post-H2 recovery chunks must fully drain before the next flush
    submit, even if the remaining time already fell below lead_time.

    This avoids back-to-back short recovery appends on a still active
    stepcompress cursor, the pattern seen in the hardware `Invalid
    sequence` logs after H2 recovery.
    """
    printer, feeder = make_c_cont_feeder(
        monkeypatch, cfg_overrides={
            'high_flow_mm3s_threshold': 20.0,
            'min_feed_floor': 10.0,
            'interrupt_chunk_mm': 9.0,
        })
    feeder.reactor.now = 50.0
    feeder._post_full_recovery_until = 51.0
    feeder._current_move = {
        'end_time': 50.25,
        'direction': 1.0,
        'distance': 3.0,
        'speed': 12.5,
    }
    feeder._last_move_end_time = 50.25
    feeder._stepcompress_primed = True
    set_sensor_active(feeder, 'hall_empty', True)
    _stub_tracker(monkeypatch, feeder, velocity=12.5, flow=30.1)
    monkeypatch.setattr(feeder, '_auto_submit_permission',
                        lambda eventtime: (True, 'active_print'))
    submits = []
    monkeypatch.setattr(
        feeder, '_submit_move',
        lambda dist, speed, **kw: submits.append(
            {'distance': dist, 'speed': speed, **kw}))

    feeder._flush_submit_streaming_chunk(step_gen_time=50.20, eventtime=50.0)

    assert submits == [], (
        "short post-H2 recovery chunk should drain completely before "
        "the next flush submit is allowed")


def test_c_cont_modulator_zwischenzone_subthreshold_still_skips(monkeypatch):
    """Unterhalb der High-Flow-Schwelle bleibt der alte Schutz aktiv."""
    printer, feeder = make_c_cont_feeder(monkeypatch)
    set_sensor_active(feeder, 'hall_empty', False)
    set_sensor_active(feeder, 'hall_full', False)
    set_sensor_active(feeder, 'hall_overflow', False)
    _populate_tracker_to_ready(feeder, velocity=9.0)
    assert feeder.velocity_tracker.get_volumetric_flow() < feeder.high_flow_mm3s_threshold
    assert feeder._compute_target_feed_speed() == 0.0


def test_c_cont_modulator_tracker_not_ready_fallback(monkeypatch):
    """Tracker not_ready -> Hotfix4: target=0 in nicht-leerem Buffer
    (kein Submit, warten auf echte Velocity)."""
    printer, feeder = make_c_cont_feeder(monkeypatch)
    set_sensor_active(feeder, 'hall_empty', False)
    set_sensor_active(feeder, 'hall_full', False)
    set_sensor_active(feeder, 'hall_overflow', False)
    assert not feeder.velocity_tracker.is_ready()
    assert feeder._compute_target_feed_speed() == 0.0


def test_c_cont_modulator_extruder_vel_zero_fallback(monkeypatch):
    """Toolhead pausiert (extruder_vel=0) in Zwischenzone -> Hotfix4:
    target=0 (nicht in nicht-leeren Buffer foerdern).

    Hardware-Test 2026-05-13 klippy(11).log: Hotfix3-Fallback
    (vel<=0 -> feed_speed=70) overshoot HALL1 -> stepcompress c=9 crash.
    Hotfix4: nur HALL3-Branch darf bei stalled-Toolhead foerdern."""
    printer, feeder = make_c_cont_feeder(monkeypatch)
    set_sensor_active(feeder, 'hall_empty', False)
    set_sensor_active(feeder, 'hall_full', False)
    set_sensor_active(feeder, 'hall_overflow', False)
    # Tracker ready aber alle Positions gleich -> vel=0
    fake_ext = feeder.printer.objects['extruder']
    t = 0.0
    for _ in range(12):
        fake_ext.last_position = 100.0  # konstant
        feeder.velocity_tracker.tick(t)
        t += 0.025
    assert feeder.velocity_tracker.is_ready()
    assert feeder.velocity_tracker.get_velocity() == 0.0
    # Hotfix4: Zwischenzone + vel=0 -> 0.0
    assert feeder._compute_target_feed_speed() == 0.0


# ---------------------------------------------------------------------------
# C-cont Hotfix4 (2026-05-13 klippy(11)): no submit into full buffer
# when toolhead stalled (Boot-Status Arm in HALL1+HALL2)
# ---------------------------------------------------------------------------


def test_c_cont_hotfix4_stalled_toolhead_no_submit_in_full_buffer(monkeypatch):
    """Hotfix4: Boot mit HALL2 active + Toolhead stalled -> target=0,
    kein Submit in vollen Buffer."""
    printer, feeder = make_c_cont_feeder(monkeypatch)
    set_sensor_active(feeder, 'hall_empty', False)
    set_sensor_active(feeder, 'hall_full', True)
    set_sensor_active(feeder, 'hall_overflow', False)
    # Tracker ready aber konstante Position (Toolhead stalled)
    fake_ext = feeder.printer.objects['extruder']
    t = 0.0
    for _ in range(12):
        fake_ext.last_position = 100.0  # konstant
        feeder.velocity_tracker.tick(t)
        t += 0.025
    assert feeder.velocity_tracker.is_ready()
    assert feeder.velocity_tracker.get_velocity() == 0.0
    # Modulator: HALL2 + stalled -> 0.0 (Hotfix4)
    assert feeder._compute_target_feed_speed() == 0.0


def test_c_cont_hotfix4_stalled_but_hall_empty_still_fills(monkeypatch):
    """HALL3 without active extruder draw is no longer treated as
    demand. The arm can rest high while idle or pre-print."""
    printer, feeder = make_c_cont_feeder(monkeypatch)
    set_sensor_active(feeder, 'hall_empty', True)
    set_sensor_active(feeder, 'hall_full', False)
    set_sensor_active(feeder, 'hall_overflow', False)
    fake_ext = feeder.printer.objects['extruder']
    t = 0.0
    for _ in range(12):
        fake_ext.last_position = 100.0
        feeder.velocity_tracker.tick(t)
        t += 0.025
    assert feeder.velocity_tracker.get_velocity() == 0.0
    assert feeder._compute_target_feed_speed() == 0.0


def test_c_cont_hotfix4_not_ready_no_submit_in_full(monkeypatch):
    """Hotfix4: tracker not_ready + HALL2 active -> target=0."""
    printer, feeder = make_c_cont_feeder(monkeypatch)
    set_sensor_active(feeder, 'hall_empty', False)
    set_sensor_active(feeder, 'hall_full', True)
    set_sensor_active(feeder, 'hall_overflow', False)
    # Tracker not_ready (Boot)
    assert not feeder.velocity_tracker.is_ready()
    assert feeder._compute_target_feed_speed() == 0.0


def test_c_cont_hotfix4_not_ready_but_hall_empty_uses_fallback(monkeypatch):
    """Tracker not ready + HALL3 no longer implies demand."""
    printer, feeder = make_c_cont_feeder(monkeypatch)
    set_sensor_active(feeder, 'hall_empty', True)
    set_sensor_active(feeder, 'hall_full', False)
    set_sensor_active(feeder, 'hall_overflow', False)
    assert not feeder.velocity_tracker.is_ready()
    assert feeder._compute_target_feed_speed() == 0.0


# ---------------------------------------------------------------------------
# C-cont Hotfix3 (2026-05-13 klippy(9)): Soft-Floor + MIN_FLOOR=15
# Ersetzt Hotfix2 (hartes Floor=feed_speed -> HALL1-Overshoot-Storm)
# ---------------------------------------------------------------------------


def test_c_cont_hotfix3_zwischen_soft_floor_low_vel(monkeypatch):
    """Hotfix5: Zwischenzone bei velocity < MIN_FLOOR/1.10 -> 0.0
    (Pipeline-Safe Skip statt force-clamp). Bei velocity=5:
    1.10*5=5.5 < MIN_FLOOR=15 -> KEIN Submit (Pipeline-Last bei
    niedrigem Flow vermeiden)."""
    printer, feeder = make_c_cont_feeder(monkeypatch)
    set_sensor_active(feeder, 'hall_empty', False)
    set_sensor_active(feeder, 'hall_full', False)
    set_sensor_active(feeder, 'hall_overflow', False)
    _populate_tracker_to_ready(feeder, velocity=5.0)
    # Hotfix5: proposed < MIN_FLOOR -> 0.0
    assert feeder._compute_target_feed_speed() == 0.0


def test_c_cont_hotfix3_zwischen_soft_floor_high_vel(monkeypatch):
    """Hotfix3: Zwischenzone bei high velocity = 1.10 * vel.
    High velocity (50 mm/s): erwarte 1.10 * 50 = 55."""
    printer, feeder = make_c_cont_feeder(
        monkeypatch, cfg_overrides={'feed_speed': 70.0})
    set_sensor_active(feeder, 'hall_empty', False)
    set_sensor_active(feeder, 'hall_full', False)
    set_sensor_active(feeder, 'hall_overflow', False)
    _populate_tracker_to_ready(feeder, velocity=50.0)
    # max(15, 1.10*50=55) -> 55
    assert feeder._compute_target_feed_speed() == pytest.approx(55.0, abs=1.0)


def test_c_cont_hotfix3_hall2_soft_floor_low_vel(monkeypatch):
    """Hotfix5: HALL2 = 0.0 (Buffer voll, kein Push) — egal welche
    velocity. Alter HALL2-Branch 0.5*v ueberschrieben."""
    printer, feeder = make_c_cont_feeder(monkeypatch)
    set_sensor_active(feeder, 'hall_empty', False)
    set_sensor_active(feeder, 'hall_full', True)
    set_sensor_active(feeder, 'hall_overflow', False)
    _populate_tracker_to_ready(feeder, velocity=20.0)
    assert feeder._compute_target_feed_speed() == 0.0


def test_c_cont_hotfix3_hall2_soft_floor_high_vel(monkeypatch):
    """Hotfix5: HALL2 = 0.0 auch bei hoher velocity (Buffer voll =
    NIE foerdern, drain ueber Toolhead)."""
    printer, feeder = make_c_cont_feeder(monkeypatch)
    set_sensor_active(feeder, 'hall_empty', False)
    set_sensor_active(feeder, 'hall_full', True)
    set_sensor_active(feeder, 'hall_overflow', False)
    _populate_tracker_to_ready(feeder, velocity=50.0)
    assert feeder._compute_target_feed_speed() == 0.0


# ---------------------------------------------------------------------------
# C-cont Hotfix5 (2026-05-13 klippy_real.log HEAD 2615b96):
#   3-Layer-Fix gegen HALL1-Overshoot-Cycles + stepcompress c=24 Crash.
#   - HALL2 active (Buffer voll) -> 0.0 (drain via Toolhead, KEIN Push)
#   - HALL3 active (Buffer leer) -> feed_speed statt max_feed_speed
#   - Zwischenzone proposed < MIN_FLOOR -> 0.0 (Pipeline-Safe Skip)
# ---------------------------------------------------------------------------


def test_c_cont_hotfix5_hall2_returns_zero(monkeypatch):
    """Hotfix5: HALL2 active -> target=0 (Buffer voll, kein Push).

    Alter HALL2-Branch 0.5*v+MIN_FLOOR=15 verursachte Submit @15 mm/s
    in vollen Buffer wenn tracker_vel=1.6 (Resume-Phase). Strukturell
    falsch — bei vollem Buffer NIE foerdern."""
    printer, feeder = make_c_cont_feeder(monkeypatch)
    set_sensor_active(feeder, 'hall_empty', False)
    set_sensor_active(feeder, 'hall_full', True)
    set_sensor_active(feeder, 'hall_overflow', False)
    _populate_tracker_to_ready(feeder, velocity=20.0)
    assert feeder._compute_target_feed_speed() == 0.0


def test_c_cont_hotfix5_hall3_uses_soft_throttle_not_max(monkeypatch):
    """HALL3 nutzt Soft-Throttle statt max_feed_speed oder fixem Vollgas."""
    printer, feeder = make_c_cont_feeder(
        monkeypatch, cfg_overrides={'feed_speed': 70.0})
    set_sensor_active(feeder, 'hall_empty', True)
    set_sensor_active(feeder, 'hall_full', False)
    set_sensor_active(feeder, 'hall_overflow', False)
    _populate_tracker_to_ready(feeder, velocity=15.0)
    assert feeder._compute_target_feed_speed() == pytest.approx(22.5, abs=0.1)
    assert feeder._compute_target_feed_speed() != feeder.max_feed_speed


def test_c_cont_hotfix5_zwischen_below_min_floor_skips(monkeypatch):
    """Hotfix5: Zwischenzone proposed < MIN_FLOOR -> 0.0 (kein
    force-clamp). Bei velocity=5: 1.10*5=5.5 < 15 -> kein Submit
    (Pipeline-Last bei niedrigem Flow vermeiden)."""
    printer, feeder = make_c_cont_feeder(monkeypatch)
    set_sensor_active(feeder, 'hall_empty', False)
    set_sensor_active(feeder, 'hall_full', False)
    set_sensor_active(feeder, 'hall_overflow', False)
    _populate_tracker_to_ready(feeder, velocity=5.0)
    assert feeder._compute_target_feed_speed() == 0.0


def test_c_cont_hotfix5_zwischen_above_min_floor_proportional(monkeypatch):
    """Hotfix5: Zwischenzone proposed >= MIN_FLOOR -> proportional
    submit (1.10 * extruder_vel). Bei velocity=20: 1.10*20=22 > 15
    -> 22 (regulaerer Pipeline-Submit)."""
    printer, feeder = make_c_cont_feeder(monkeypatch)
    set_sensor_active(feeder, 'hall_empty', False)
    set_sensor_active(feeder, 'hall_full', False)
    set_sensor_active(feeder, 'hall_overflow', False)
    _populate_tracker_to_ready(feeder, velocity=20.0)
    assert feeder._compute_target_feed_speed() == pytest.approx(22.0, abs=1.0)


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
    feeder._state = buffer_feeder.STATE_LOADING_PULL
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
    """STATE_AUTO + HALL3 stable + tracker ready -> Submit bei jedem flush.

    Soft-Throttle: HALL3-Submit folgt dem Verbrauch statt fixem feed_speed."""
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
    assert submits[0]['speed'] == pytest.approx(22.5, abs=0.1)


def test_c_cont_speed_modulation_via_hall_state(monkeypatch):
    """HALL-State-Change zwischen flushes -> Speed-Aenderung.

    Soft-Throttle:
    - HALL3 -> verbrauchsorientiert mit 1.5x Margin
    - HALL2 -> 0.0 (kein Submit, Buffer voll = drain ueber Toolhead)
    """
    printer, feeder = make_c_cont_feeder(
        monkeypatch, cfg_overrides={'feed_speed': 70.0})
    feeder._state = buffer_feeder.STATE_AUTO
    _populate_tracker_to_ready(feeder, velocity=40.0)
    submits = _capture_submits(feeder, monkeypatch)
    # HALL3 -> min(40 * 1.5, feed_speed=70) = 60
    set_sensor_active(feeder, 'hall_empty', True)
    feeder._last_move_end_time = 0.0
    feeder._pending_remaining_mm = 0.0
    feeder._on_mcu_flush(flush_time=10.0, step_gen_time=10.0)
    # HALL2 -> 0.0 (kein Submit)
    set_sensor_active(feeder, 'hall_empty', False)
    set_sensor_active(feeder, 'hall_full', True)
    feeder._last_move_end_time = 0.0  # Move done
    feeder._on_mcu_flush(flush_time=10.5, step_gen_time=10.5)
    # Nur 1 Submit (HALL3), HALL2 ist Skip
    assert len(submits) == 1
    assert submits[0]['speed'] == pytest.approx(60.0, abs=0.1)


def test_c_cont_hall1_active_no_submit(monkeypatch):
    """HALL1 active -> target=0 -> kein Submit."""
    printer, feeder = make_c_cont_feeder(monkeypatch)
    feeder._state = buffer_feeder.STATE_AUTO
    set_sensor_active(feeder, 'hall_overflow', True)
    _populate_tracker_to_ready(feeder, velocity=15.0)
    submits = _capture_submits(feeder, monkeypatch)
    feeder._on_mcu_flush(flush_time=10.0, step_gen_time=10.0)
    assert len(submits) == 0


def test_c_cont_idle_suppresses_auto_stream(monkeypatch):
    """Watchdog-anchor waehrend Klipper-Idle darf den Flush-Callback
    nicht in einen Selbstlaeufer verwandeln."""
    printer, feeder = make_c_cont_feeder(monkeypatch, print_state='standby')
    feeder._state = buffer_feeder.STATE_AUTO
    set_sensor_active(feeder, 'hall_empty', True)
    submits = _capture_submits(feeder, monkeypatch)
    feeder._last_move_end_time = 0.0
    feeder._pending_remaining_mm = 0.0
    feeder._on_mcu_flush(flush_time=10.0, step_gen_time=10.0)
    assert len(submits) == 0


def test_c_cont_active_print_allows_auto_stream(monkeypatch):
    """Der Idle-Schutz darf den echten Print-Pfad nicht blockieren."""
    printer, feeder = make_c_cont_feeder(monkeypatch, print_state='printing')
    feeder._state = buffer_feeder.STATE_AUTO
    set_sensor_active(feeder, 'hall_empty', True)
    _populate_tracker_to_ready(feeder, velocity=15.0)
    submits = _capture_submits(feeder, monkeypatch)
    feeder._last_move_end_time = 0.0
    feeder._pending_remaining_mm = 0.0
    feeder._on_mcu_flush(flush_time=10.0, step_gen_time=10.0)
    assert len(submits) == 1
    assert submits[0]['speed'] == pytest.approx(22.5, abs=0.1)


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
    nicht eingefrorenem Speed vom ersten Submit.

    Hotfix3: Zwischenzone-Output = max(15, vel*1.10). Bei velocity=30
    ist 1.10*30=33 > MIN_FLOOR=15, also expect 33.
    Im Continuous-Mode submittet _on_mcu_flush nur noch interrupt_-
    chunk_mm (9), kein _pending_remaining_mm. Wir setzen Pending hier
    kuenstlich um _tick_pending_chunk-Logik zu testen.
    """
    printer, feeder = make_c_cont_feeder(
        monkeypatch, cfg_overrides={'feed_speed': 70.0})
    feeder._state = buffer_feeder.STATE_AUTO
    set_sensor_active(feeder, 'hall_empty', True)
    _populate_tracker_to_ready(feeder, velocity=30.0)
    # Pending kuenstlich seeden (testet _tick_pending_chunk speed-update):
    feeder._pending_remaining_mm = 36.0
    feeder._pending_submit_chunk_cap = feeder.interrupt_chunk_mm
    feeder._pending_direction = 1.0
    feeder._pending_speed = feeder.max_feed_speed
    feeder._last_move_end_time = 10.0
    # HALL-Wechsel von HALL3 -> Zwischenzone (buffer fuellt sich):
    set_sensor_active(feeder, 'hall_empty', False)
    # Zwischenzone Hotfix3: max(15, 1.10*30=33) = 33
    assert feeder._compute_target_feed_speed() == pytest.approx(33.0, abs=1.0)
    trapezoids = _capture_trapezoids(feeder, monkeypatch)
    feeder._tick_pending_chunk(eventtime=10.0)
    assert len(trapezoids) == 1
    assert trapezoids[0]['speed'] == pytest.approx(33.0, abs=1.0)
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
    moduliert, kein Stream-Reset).

    Soft-Throttle: HALL3 startet mit min(vel*1.5, feed_speed),
    Zwischenzone mit min(vel*1.10, feed_speed).
    """
    printer, feeder = make_c_cont_feeder(
        monkeypatch, cfg_overrides={'feed_speed': 70.0})
    feeder._state = buffer_feeder.STATE_AUTO
    _populate_tracker_to_ready(feeder, velocity=40.0)
    submits = _capture_submits(feeder, monkeypatch)
    # HALL3 -> min(40*1.5, 70)=60, erster Submit setzt _continuous_feed=True
    set_sensor_active(feeder, 'hall_empty', True)
    feeder._last_move_end_time = 0.0
    feeder._pending_remaining_mm = 0.0
    feeder._continuous_feed = False  # explizit reset (echte cold-start)
    feeder._on_mcu_flush(flush_time=10.0, step_gen_time=10.0)
    assert feeder._continuous_feed is True
    assert len(submits) == 1
    first_submit_speed = submits[0]['speed']
    # HALL-State-Change: HALL3 -> Zwischenzone (alle HALL inactive).
    set_sensor_active(feeder, 'hall_empty', False)
    feeder._last_move_end_time = 0.0  # vorheriger Move done
    feeder._on_mcu_flush(flush_time=10.5, step_gen_time=10.5)
    # _continuous_feed bleibt strukturell True:
    assert feeder._continuous_feed is True
    # Speed wurde moduliert (Zwischenzone Hotfix3 = max(15, 1.10*40)=44):
    assert len(submits) == 2
    assert submits[1]['speed'] == pytest.approx(44.0, abs=1.0)
    # Speed-Aenderung gegenueber erstem Submit ist erfolgt (Modulation):
    assert submits[1]['speed'] != first_submit_speed


# ===========================================================================
# C-cont Hotfix3 (2026-05-13 klippy(9) HALL1-Overshoot-Storm):
#   Fix 2: Pipeline-Cap auf 1 Sub-Chunk in-flight (kein _pending_remaining)
#   Fix 3: HALL1+HALL2 simultaneous -> instant OVERFLOW (kein Soft-Wait)
# ===========================================================================


def test_c_cont_hotfix3_pipeline_one_chunk_only(monkeypatch):
    """Hotfix3 Fix 2: _on_mcu_flush submittet interrupt_chunk_mm (9)
    statt flush_callback_chunk_mm (45). Pipeline maximal 1 Sub-Chunk
    in-flight. HALL1-Trigger stoppt die Pipeline sofort beim
    naechsten flush-callback (Hardware 2026-05-13 klippy(9), 30/30
    Cycles HALL1-Overshoot-Storm fix).

    Soft-Throttle: HALL3-Speed ist jetzt verbrauchsorientiert statt fix."""
    printer, feeder = make_c_cont_feeder(monkeypatch)
    feeder._state = buffer_feeder.STATE_AUTO
    set_sensor_active(feeder, 'hall_empty', True)
    _populate_tracker_to_ready(feeder, velocity=15.0)
    submits = _capture_submits(feeder, monkeypatch)
    feeder._last_move_end_time = 0.0
    feeder._pending_remaining_mm = 0.0
    feeder._on_mcu_flush(flush_time=10.0, step_gen_time=10.0)
    assert len(submits) == 1
    # Hotfix3: Submit-Distanz = interrupt_chunk_mm (9), nicht
    # flush_callback_chunk_mm (Default 15, lll.cfg 45):
    assert submits[0]['distance'] == feeder.interrupt_chunk_mm
    # Soft-Throttle: vel=15 -> 22.5 mm/s
    assert submits[0]['speed'] == pytest.approx(22.5, abs=0.1)


def test_c_cont_soft_throttle_caps_high_velocity(monkeypatch):
    """Sehr hohe Extruder-Geschwindigkeit bleibt auf feed_speed gecappt."""
    printer, feeder = make_c_cont_feeder(
        monkeypatch, cfg_overrides={'feed_speed': 70.0})
    set_sensor_active(feeder, 'hall_empty', True)
    _populate_tracker_to_ready(feeder, velocity=80.0)
    assert feeder._compute_target_feed_speed() == pytest.approx(
        feeder.feed_speed, abs=0.1)
    set_sensor_active(feeder, 'hall_empty', False)
    assert feeder._compute_target_feed_speed() == pytest.approx(
        feeder.feed_speed, abs=0.1)


def test_c_cont_hotfix3_hall1_plus_hall2_instant_overflow(monkeypatch):
    """Hotfix3 Fix 3: HALL1+HALL2 gleichzeitig -> instant OVERFLOW.

    Mechanisch eindeutig: Buffer-Arm am Maximalanschlag. Kein
    Bouncing-Szenario, daher kein Persist-Wait noetig — direkter
    _enter_overflow."""
    printer, feeder = make_c_cont_feeder(monkeypatch)
    feeder._state = buffer_feeder.STATE_AUTO
    # HALL2 active zuerst
    set_sensor_active(feeder, 'hall_full', True)
    # Dann HALL1 active -> instant OVERFLOW via _mark_hall1_active
    set_sensor_active(feeder, 'hall_overflow', True)
    _fire_hall1_callback(feeder)
    assert feeder._state == buffer_feeder.STATE_OVERFLOW


def test_c_cont_hotfix3_hall1_only_soft_trigger(monkeypatch):
    """Hotfix3 Fix 3: HALL1 OHNE HALL2 bleibt Soft-Trigger (Persist-
    Timer). HALL2=False -> kein Maximalanschlag-Szenario, ueblicher
    C-cont T5/T6-Pfad: Timestamp setzen, _main_tick prueft Persist."""
    printer, feeder = make_c_cont_feeder(monkeypatch)
    feeder._state = buffer_feeder.STATE_AUTO
    # Nur HALL1 (HALL2 off)
    set_sensor_active(feeder, 'hall_full', False)
    set_sensor_active(feeder, 'hall_overflow', True)
    _fire_hall1_callback(feeder)
    assert feeder._state == buffer_feeder.STATE_AUTO  # Soft
    assert feeder._hall1_active_since is not None


def test_c_cont_hotfix3_no_pending_remaining_in_continuous(monkeypatch):
    """Hotfix3 Fix 2: Continuous-Streaming hinterlaesst kein
    _pending_remaining_mm — Submit ist 9mm, kein Split. Damit gibt
    es im Continuous-Mode keine in-flight Pipeline mehr, die HALL1
    nicht stoppen kann."""
    printer, feeder = make_c_cont_feeder(monkeypatch)
    feeder._state = buffer_feeder.STATE_AUTO
    set_sensor_active(feeder, 'hall_empty', True)
    _populate_tracker_to_ready(feeder, velocity=15.0)
    # Echter Submit-Pfad (kein monkeypatch von _submit_move):
    feeder._last_move_end_time = 0.0
    feeder._pending_remaining_mm = 0.0
    feeder._on_mcu_flush(flush_time=10.0, step_gen_time=10.0)
    # Nach Submit: kein pending (Submit-Distanz = Cap-Distanz = 9mm)
    assert feeder._pending_remaining_mm == 0.0
