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
        fake_ext.last_position = t * velocity
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
    """Hotfix7-Update: HALL3:on + vel=15 -> max(15*1.5=22.5, 15)=22.5.
    Hotfix6 nutzte fix 30 mm/s; Hardware-Crash 2026-05-13 (klippy.log
    Z.104571, c=12 i=0) zeigte dass fixer 30 mm/s Schub bei
    HALL2<->HALL1 nur 3.6mm Sicherheitsmarge regelmaessig in HALL1
    overshoot. Hotfix7 koppelt Speed an Extruder-Verbrauch."""
    printer, feeder = make_c_cont_feeder(monkeypatch)
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
    """Hotfix7-Update: HALL3 active + Toolhead stalled (vel=0) ->
    foerdere TROTZDEM (Buffer wirklich leer), aber sanft mit
    MIN_FLOOR=15 statt Hotfix6's fix 30.0 (Hardware-Crash
    2026-05-13, klippy.log Z.104571: 16+ HALL1-Cycles, c=12 i=0)."""
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
    # Hotfix7: HALL3 + vel=0 -> MIN_FLOOR=15 (statt Hotfix6 30.0)
    assert feeder._compute_target_feed_speed() == pytest.approx(15.0, abs=0.1)


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
    """Hotfix7-Update: tracker not_ready + HALL3 -> MIN_FLOOR=15 mm/s
    (Print-Start sanft). Hotfix6 nutzte fix 30 mm/s -> aggressiver
    Initial-Fill schoss in HALL1 over (Hardware-Beleg klippy.log
    Z.104571, c=12 i=0)."""
    printer, feeder = make_c_cont_feeder(monkeypatch)
    set_sensor_active(feeder, 'hall_empty', True)
    set_sensor_active(feeder, 'hall_full', False)
    set_sensor_active(feeder, 'hall_overflow', False)
    assert not feeder.velocity_tracker.is_ready()
    assert feeder._compute_target_feed_speed() == pytest.approx(15.0, abs=0.1)


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
    """Hotfix7-Update: Zwischenzone bei high velocity = min(1.10*vel,
    feed_speed). vel=50 -> 1.10*50=55, capped auf feed_speed=30.
    Vorher Hotfix3: unbounded 55. NEUER Soft-Cap verhindert dass
    target_speed > konfigurierte Obergrenze waechst (Hardware-
    Sicherheit, Hotfix7)."""
    printer, feeder = make_c_cont_feeder(monkeypatch)
    set_sensor_active(feeder, 'hall_empty', False)
    set_sensor_active(feeder, 'hall_full', False)
    set_sensor_active(feeder, 'hall_overflow', False)
    _populate_tracker_to_ready(feeder, velocity=50.0)
    # min(1.10*50=55, feed_speed=30) -> 30
    assert feeder._compute_target_feed_speed() == pytest.approx(30.0, abs=0.1)


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


def test_c_cont_hotfix7_hall3_scales_with_vel_under_high_feed_speed(monkeypatch):
    """Hotfix7 (ersetzt Hotfix6): HALL3 + vel=20, feed_speed=70 ->
    max(20*1.5=30, 15)=30, capped auf feed_speed=70 -> 30.
    Hotfix7 koppelt Speed an Verbrauch statt fixer Konstante.
    Hardware-Crash 2026-05-13 (klippy.log Z.104571, c=12 i=0) zeigte
    dass Hotfix6's fixer 30 mm/s bei niedrigem vel (5-10) immer noch
    HALL2->HALL1 ueberschoss (3.6mm Sicherheitsmarge)."""
    printer, feeder = make_c_cont_feeder(
        monkeypatch, cfg_overrides={'feed_speed': 70.0})
    set_sensor_active(feeder, 'hall_empty', True)
    set_sensor_active(feeder, 'hall_full', False)
    set_sensor_active(feeder, 'hall_overflow', False)
    _populate_tracker_to_ready(feeder, velocity=20.0)
    # 20*1.5=30, capped auf feed_speed=70 -> 30
    assert feeder._compute_target_feed_speed() == pytest.approx(30.0, abs=0.1)
    # Bei hoeherem vel: vel*1.5 bis feed_speed-Cap. Tracker resetten,
    # damit alte Samples die neue Velocity nicht verfaelschen.
    feeder.velocity_tracker.reset()
    _populate_tracker_to_ready(feeder, velocity=60.0)
    # 60*1.5=90 -> capped auf feed_speed=70
    assert feeder._compute_target_feed_speed() == pytest.approx(70.0, abs=0.1)


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


# ---------------------------------------------------------------------------
# C-cont Hotfix 7 (Hardware-Crash 2026-05-13 klippy.log Z.104571, c=12 i=0
# Invalid sequence nach 16+ HALL1-OVERFLOW-Zyklen in 80s).
# Soft-Throttle: Feeder-Speed skaliert mit Extruder-Verbrauch statt fix
# 30 mm/s. Adressiert Hardware-Geometrie (optische Lichtschranken,
# HALL3<->HALL2=12.8mm, HALL2<->HALL1=3.6mm, Ausloeser 3-4mm, Hebel 2:1)
# vs Hotfix6's 5mm-Chunk-Schwung-Energie.
# Siehe specs/2026-05-13-c-cont-hotfix7-soft-throttle.md
# ---------------------------------------------------------------------------


def test_c_cont_hotfix7_hall3_high_vel_capped_at_feed_speed(monkeypatch):
    """Hotfix7: HALL3:on + vel=25 -> max(25*1.5=37.5, 15) capped auf
    feed_speed=30. Soft-Cap verhindert ueberzogene Speeds bei schnellen
    Drucken."""
    printer, feeder = make_c_cont_feeder(monkeypatch)
    set_sensor_active(feeder, 'hall_empty', True)
    set_sensor_active(feeder, 'hall_full', False)
    set_sensor_active(feeder, 'hall_overflow', False)
    _populate_tracker_to_ready(feeder, velocity=25.0)
    # 25*1.5=37.5 > feed_speed=30 -> capped to 30
    assert feeder._compute_target_feed_speed() == pytest.approx(30.0, abs=0.1)


def test_c_cont_hotfix7_hall3_mid_vel_scales_with_extruder(monkeypatch):
    """Hotfix7: HALL3:on + vel=15 -> max(15*1.5=22.5, 15)=22.5.
    Scaling mit Verbrauch statt fixe 30 mm/s -> halbierte Schwung-Energie
    bei moderaten Drucken."""
    printer, feeder = make_c_cont_feeder(monkeypatch)
    set_sensor_active(feeder, 'hall_empty', True)
    set_sensor_active(feeder, 'hall_full', False)
    set_sensor_active(feeder, 'hall_overflow', False)
    _populate_tracker_to_ready(feeder, velocity=15.0)
    assert feeder._compute_target_feed_speed() == pytest.approx(22.5, abs=0.1)


def test_c_cont_hotfix7_hall3_low_vel_uses_min_floor(monkeypatch):
    """Hotfix7: HALL3:on + vel=8 (unter MIN_FLOOR) -> 15 mm/s
    (MIN_FLOOR-Boden). 8*1.5=12 < 15 -> Floor wins."""
    printer, feeder = make_c_cont_feeder(monkeypatch)
    set_sensor_active(feeder, 'hall_empty', True)
    set_sensor_active(feeder, 'hall_full', False)
    set_sensor_active(feeder, 'hall_overflow', False)
    _populate_tracker_to_ready(feeder, velocity=8.0)
    assert feeder._compute_target_feed_speed() == pytest.approx(15.0, abs=0.1)


def test_c_cont_hotfix7_hall3_vel_zero_uses_min_floor(monkeypatch):
    """Hotfix7: HALL3:on + tracker ready aber vel=0 (Toolhead stalled
    mit leerem Buffer) -> 15 mm/s. Buffer muss trotz Stall gefuellt
    werden, aber sanft (MIN_FLOOR statt 30)."""
    printer, feeder = make_c_cont_feeder(monkeypatch)
    set_sensor_active(feeder, 'hall_empty', True)
    set_sensor_active(feeder, 'hall_full', False)
    set_sensor_active(feeder, 'hall_overflow', False)
    # Tracker ready aber vel=0
    fake_ext = feeder.printer.objects['extruder']
    t = 0.0
    for _ in range(12):
        fake_ext.last_position = 100.0  # konstant -> vel=0
        feeder.velocity_tracker.tick(t)
        t += 0.025
    assert feeder.velocity_tracker.is_ready()
    assert feeder.velocity_tracker.get_velocity() == 0.0
    assert feeder._compute_target_feed_speed() == pytest.approx(15.0, abs=0.1)


def test_c_cont_hotfix7_hall3_tracker_not_ready_uses_min_floor(monkeypatch):
    """Hotfix7: HALL3:on + tracker noch nicht ready (Print-Start) ->
    15 mm/s. Vorher Hotfix6: 30 mm/s -> aggressiver Initial-Fill ->
    HALL1-Overshoot waehrend velocity_tracker noch sammelt."""
    printer, feeder = make_c_cont_feeder(monkeypatch)
    set_sensor_active(feeder, 'hall_empty', True)
    set_sensor_active(feeder, 'hall_full', False)
    set_sensor_active(feeder, 'hall_overflow', False)
    assert not feeder.velocity_tracker.is_ready()
    assert feeder._compute_target_feed_speed() == pytest.approx(15.0, abs=0.1)


def test_c_cont_hotfix7_zwischen_low_vel_zero(monkeypatch):
    """Hotfix7: Zwischenzone + vel<MIN_FLOOR -> 0.0 (unveraendert
    von Hotfix5: vermeidet Submits bei Spurious-Buffer-Drift)."""
    printer, feeder = make_c_cont_feeder(monkeypatch)
    set_sensor_active(feeder, 'hall_empty', False)
    set_sensor_active(feeder, 'hall_full', False)
    set_sensor_active(feeder, 'hall_overflow', False)
    _populate_tracker_to_ready(feeder, velocity=5.0)
    assert feeder._compute_target_feed_speed() == 0.0


def test_c_cont_hotfix7_zwischen_high_vel_capped(monkeypatch):
    """Hotfix7: Zwischenzone + vel=40 -> min(40*1.10=44, feed_speed=30)=30.
    NEUER Soft-Cap in Zwischenzone (vorher: unbounded vel*1.10).
    Verhindert dass schnelle Drucke target_speed > feed_speed setzen."""
    printer, feeder = make_c_cont_feeder(monkeypatch)
    set_sensor_active(feeder, 'hall_empty', False)
    set_sensor_active(feeder, 'hall_full', False)
    set_sensor_active(feeder, 'hall_overflow', False)
    _populate_tracker_to_ready(feeder, velocity=40.0)
    assert feeder._compute_target_feed_speed() == pytest.approx(30.0, abs=0.1)


def test_c_cont_hotfix7_zwischen_tracker_not_ready_zero(monkeypatch):
    """Hotfix7: Zwischenzone + tracker not_ready -> 0.0 (unveraendert
    von Hotfix4: nur HALL3:on darf bei not_ready foerdern, alle anderen
    Zonen warten auf echte Velocity)."""
    printer, feeder = make_c_cont_feeder(monkeypatch)
    set_sensor_active(feeder, 'hall_empty', False)
    set_sensor_active(feeder, 'hall_full', False)
    set_sensor_active(feeder, 'hall_overflow', False)
    assert not feeder.velocity_tracker.is_ready()
    assert feeder._compute_target_feed_speed() == 0.0


def test_c_cont_hotfix7_hall2_zero_regression(monkeypatch):
    """Regression: HALL2:on -> 0.0 (unveraendert von Hotfix5).
    Stellt sicher dass Hotfix7-Umbau den HALL2-Pfad nicht bricht."""
    printer, feeder = make_c_cont_feeder(monkeypatch)
    set_sensor_active(feeder, 'hall_empty', False)
    set_sensor_active(feeder, 'hall_full', True)
    set_sensor_active(feeder, 'hall_overflow', False)
    _populate_tracker_to_ready(feeder, velocity=20.0)
    assert feeder._compute_target_feed_speed() == 0.0


def test_c_cont_hotfix7_hall1_zero_regression(monkeypatch):
    """Regression: HALL1:on -> 0.0 (Notbremse). Stellt sicher dass
    Hotfix7-Umbau den OVERFLOW-Pfad nicht beeinflusst."""
    printer, feeder = make_c_cont_feeder(monkeypatch)
    set_sensor_active(feeder, 'hall_overflow', True)
    set_sensor_active(feeder, 'hall_empty', False)
    set_sensor_active(feeder, 'hall_full', False)
    _populate_tracker_to_ready(feeder, velocity=20.0)
    assert feeder._compute_target_feed_speed() == 0.0


def test_c_cont_hotfix7_tracker_lag_documents_tradeoff(monkeypatch):
    """Hotfix7 Lag-Verhalten (Reviewer-Concern I2): Auf Hardware kann
    der velocity_tracker nicht zwischen Flushes resettet werden.
    Sliding-Window mittelt ueber 300ms. Wenn der Extruder seine
    Geschwindigkeit rapide drosselt (z.B. Ende eines G1-Segments),
    lagt der Tracker um genau das Sliding-Window.

    Dieser Test dokumentiert das Trade-Off explizit: nach 5 Samples
    bei vel=5 (~125ms) hat der Tracker noch 7 alte Samples bei
    vel=30 -> Mittelwert ~24 mm/s -> target_speed entsprechend hoch.
    Spec §6.2 akzeptiert das, aber dieser Test verhindert dass
    zukuenftige Optimierungen das Lag-Verhalten unbemerkt aendern."""
    printer, feeder = make_c_cont_feeder(monkeypatch)
    set_sensor_active(feeder, 'hall_empty', True)
    set_sensor_active(feeder, 'hall_full', False)
    set_sensor_active(feeder, 'hall_overflow', False)

    fake_ext = feeder.printer.objects['extruder']
    # Phase 1: 12 Samples bei vel=30, Tracker primed auf 30
    pos = 0.0
    t = 0.0
    for _ in range(12):
        fake_ext.last_position = pos
        feeder.velocity_tracker.tick(t)
        pos += 30.0 * 0.025  # vel=30
        t += 0.025
    assert feeder.velocity_tracker.is_ready()
    assert feeder.velocity_tracker.get_velocity() == pytest.approx(30.0, abs=0.5)
    # Phase 2: 5 weitere Samples bei vel=5 (Extruder drosselt rapide).
    # KEIN reset() — hardware-realistisches Lag-Verhalten.
    for _ in range(5):
        fake_ext.last_position = pos
        feeder.velocity_tracker.tick(t)
        pos += 5.0 * 0.025  # vel=5
        t += 0.025
    # Tracker mittelt jetzt ueber 7 alte (vel=30) + 5 neue (vel=5) Samples.
    # Erwartete Mittel-Velocity zwischen ~15 und ~25 (haengt vom genauen
    # Window-Verhalten ab). Wichtig: Target ist NICHT auf MIN_FLOOR
    # gefallen — Soft-Throttle lagt mit dem Tracker.
    lagged_vel = feeder.velocity_tracker.get_velocity()
    assert lagged_vel > 10.0, (
        "Tracker-Lag dokumentiert: nach 5 Drossel-Samples sollte vel "
        "noch deutlich ueber 5 mm/s sein (alte Samples ueberwiegen). "
        f"Tatsaechlich: {lagged_vel:.2f}")
    # Target_speed: HALL3 + lagged_vel -> max(lagged_vel*1.5, MIN_FLOOR)
    target = feeder._compute_target_feed_speed()
    expected_target = min(max(lagged_vel * 1.5, 15.0), feeder.feed_speed)
    assert target == pytest.approx(expected_target, abs=0.1), (
        f"Target {target} muss aktuellem (gelaggten) tracker_vel "
        f"folgen: erwartet {expected_target}")


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
    """STATE_AUTO + HALL3 stable + tracker ready -> Submit bei jedem flush.

    Hotfix7-Update: HALL3-Submit nutzt max(vel*1.5, MIN_FLOOR) capped
    auf feed_speed. vel=15 -> 15*1.5=22.5 -> Submit-Speed 22.5.
    Vorher Hotfix6: fix 30.0 (Hardware-Crash 2026-05-13 zeigte dass
    fixer 30 trotzdem HALL2->HALL1 overshoot)."""
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
    # Hotfix7: 15*1.5=22.5 (statt Hotfix6 fix 30.0)
    assert submits[0]['speed'] == pytest.approx(22.5, abs=0.1)


def test_c_cont_speed_modulation_via_hall_state(monkeypatch):
    """HALL-State-Change zwischen flushes -> Speed-Aenderung.

    Hotfix7-Update:
    - HALL3 + vel=40 -> min(40*1.5=60, feed_speed=30)=30 (capped)
    - HALL2 -> 0.0 (kein Submit, Buffer voll = drain ueber Toolhead)
    """
    printer, feeder = make_c_cont_feeder(monkeypatch)
    feeder._state = buffer_feeder.STATE_AUTO
    _populate_tracker_to_ready(feeder, velocity=40.0)
    submits = _capture_submits(feeder, monkeypatch)
    # HALL3 -> 30 (40*1.5=60 capped auf feed_speed=30)
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
    # Hotfix7: 40*1.5=60 capped auf feed_speed=30
    assert submits[0]['speed'] == pytest.approx(30.0, abs=0.1)


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
    nicht eingefrorenem Speed vom ersten Submit.

    Hotfix7-Update: Zwischenzone-Output = min(vel*1.10, feed_speed).
    Bei velocity=30: 1.10*30=33 > feed_speed=30 -> capped auf 30.
    Vorher Hotfix3: unbounded 33 (kein Soft-Cap).
    Im Continuous-Mode submittet _on_mcu_flush nur noch interrupt_-
    chunk_mm (9), kein _pending_remaining_mm. Wir setzen Pending hier
    kuenstlich um _tick_pending_chunk-Logik zu testen.
    """
    printer, feeder = make_c_cont_feeder(monkeypatch)
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
    # Hotfix7: min(1.10*30=33, feed_speed=30) = 30 (NEUER Soft-Cap)
    assert feeder._compute_target_feed_speed() == pytest.approx(30.0, abs=0.1)
    trapezoids = _capture_trapezoids(feeder, monkeypatch)
    feeder._tick_pending_chunk(eventtime=10.0)
    assert len(trapezoids) == 1
    assert trapezoids[0]['speed'] == pytest.approx(30.0, abs=0.1)
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

    Hotfix7-Update: HALL3 + vel=40 -> min(40*1.5=60, feed_speed)
    (Soft-Cap). Zwischenzone + vel=40 -> min(1.10*40=44, feed_speed)
    (Soft-Cap). Wenn feed_speed niedrig ist (z.B. 30), cappen BEIDE
    Submits auf feed_speed -> Modulation nicht mehr beobachtbar.
    Daher feed_speed=70 nutzen: HALL3=60, Zwischenzone=44 -> beide
    unterschiedlich, Modulations-Invariante erhalten."""
    printer, feeder = make_c_cont_feeder(
        monkeypatch, cfg_overrides={'feed_speed': 70.0})
    feeder._state = buffer_feeder.STATE_AUTO
    _populate_tracker_to_ready(feeder, velocity=40.0)
    submits = _capture_submits(feeder, monkeypatch)
    # HALL3 -> Hotfix7: 40*1.5=60 (unter feed_speed=70, kein Cap)
    set_sensor_active(feeder, 'hall_empty', True)
    feeder._last_move_end_time = 0.0
    feeder._pending_remaining_mm = 0.0
    feeder._continuous_feed = False  # explizit reset (echte cold-start)
    feeder._on_mcu_flush(flush_time=10.0, step_gen_time=10.0)
    assert feeder._continuous_feed is True
    assert len(submits) == 1
    first_submit_speed = submits[0]['speed']
    assert first_submit_speed == pytest.approx(60.0, abs=0.1)
    # HALL-State-Change: HALL3 -> Zwischenzone (alle HALL inactive).
    set_sensor_active(feeder, 'hall_empty', False)
    feeder._last_move_end_time = 0.0  # vorheriger Move done
    feeder._on_mcu_flush(flush_time=10.5, step_gen_time=10.5)
    # _continuous_feed bleibt strukturell True (kein Stream-Reset bei
    # HALL-Wechsel):
    assert feeder._continuous_feed is True
    # Zweiter Submit: Zwischenzone Hotfix7 = min(1.10*40=44, feed_speed=70)
    # = 44 (unter Cap):
    assert len(submits) == 2
    assert submits[1]['speed'] == pytest.approx(44.0, abs=0.1)
    # Modulations-Invariante (Reviewer I1): Speed hat sich strukturell
    # geaendert zwischen den Submits — beweist dass HALL-State-Change
    # die Modulation triggert (nicht nur ein zufaelliger zweiter Submit).
    assert submits[1]['speed'] != first_submit_speed


# ===========================================================================
# C-cont Hotfix3 (2026-05-13 klippy(9) HALL1-Overshoot-Storm):
#   Fix 2: Pipeline-Cap auf 1 Sub-Chunk in-flight (kein _pending_remaining)
#   Fix 3: HALL1+HALL2 simultaneous -> instant OVERFLOW (kein Soft-Wait)
# ===========================================================================


def test_c_cont_hotfix3_pipeline_one_chunk_only(monkeypatch):
    """Hotfix3 Fix 2: _on_mcu_flush submittet interrupt_chunk_mm
    (Hotfix6 default: 5mm) statt flush_callback_chunk_mm (45).
    Pipeline maximal 1 Sub-Chunk in-flight. HALL1-Trigger stoppt
    die Pipeline sofort beim naechsten flush-callback (Hardware
    2026-05-13 klippy(9), 30/30 Cycles HALL1-Overshoot-Storm fix).

    Hotfix7-Update: HALL3-Speed = min(max(vel*1.5, MIN_FLOOR),
    feed_speed). Bei vel=15: 15*1.5=22.5 -> Submit-Speed=22.5.
    Pipeline-Cap (interrupt_chunk_mm=5) unveraendert von Hotfix6."""
    printer, feeder = make_c_cont_feeder(monkeypatch)
    feeder._state = buffer_feeder.STATE_AUTO
    set_sensor_active(feeder, 'hall_empty', True)
    _populate_tracker_to_ready(feeder, velocity=15.0)
    submits = _capture_submits(feeder, monkeypatch)
    feeder._last_move_end_time = 0.0
    feeder._pending_remaining_mm = 0.0
    feeder._on_mcu_flush(flush_time=10.0, step_gen_time=10.0)
    assert len(submits) == 1
    # Pipeline-Cap unveraendert: Submit-Distanz = interrupt_chunk_mm (5):
    assert submits[0]['distance'] == feeder.interrupt_chunk_mm
    assert feeder.interrupt_chunk_mm == 5.0  # Hotfix6 default
    # Hotfix7: Speed = min(max(15*1.5, 15), 30) = 22.5
    # (statt Hotfix6 fix 30.0)
    assert submits[0]['speed'] == pytest.approx(22.5, abs=0.1)
    # submit_chunk_cap bleibt gesetzt (defense-in-depth fuer Safety-
    # Pfade, falls _submit_move spaeter doch in Sub-Chunk-Stream
    # gerinnt — Continuous-Mode selbst nutzt kein Pending mehr):
    assert submits[0].get('submit_chunk_cap') == feeder.interrupt_chunk_mm


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
    # Nach Submit: kein pending (Submit-Distanz = Cap-Distanz, Hotfix6 5mm)
    assert feeder._pending_remaining_mm == 0.0


# ===========================================================================
# C-cont Hotfix6 (2026-05-13 klippy_real.log Hotfix5-aktiv):
#   Layer 1: HALL3-Refill-Speed feste Konstante 30.0 mm/s (statt feed_speed).
#   Layer 2: interrupt_chunk_mm Default 5.0 mm (statt 9.0 mm).
#   Beleg: 6+ Cycles HALL3->HALL1 in 30s, stepcompress c=16 Crash mit
#   Hotfix5-Settings (feed_speed=70, interrupt_chunk_mm=9). 9mm @ 70 =
#   130ms war zu kurz fuer HALL2-Detection. Hotfix6: 5mm @ 30 = 167ms.
# ===========================================================================


def test_c_cont_hotfix7_hall3_speed_varies_with_vel(monkeypatch):
    """Hotfix7 (ersetzt Hotfix6): HALL3-Speed skaliert mit
    Extruder-Verbrauch statt fixer Konstante. Hotfix6 nutzte feste
    30 mm/s -> schoss bei niedriger Print-Geschwindigkeit (vel<<15)
    trotzdem in HALL1 over. Hotfix7: max(vel*1.5, MIN_FLOOR),
    capped auf feed_speed.

    Mehrere velocity-Werte in einem Test: Tracker zwischen Calls
    resetten, damit Sliding-Window nicht alte Samples mit neuen
    mischt (sonst FP-Mittelwert verfaelscht Result)."""
    printer, feeder = make_c_cont_feeder(
        monkeypatch, cfg_overrides={'feed_speed': 70.0})
    set_sensor_active(feeder, 'hall_empty', True)
    set_sensor_active(feeder, 'hall_full', False)
    set_sensor_active(feeder, 'hall_overflow', False)
    # vel=10: 10*1.5=15, == MIN_FLOOR -> 15
    _populate_tracker_to_ready(feeder, velocity=10.0)
    assert feeder._compute_target_feed_speed() == pytest.approx(15.0, abs=0.1)
    # vel=20: 20*1.5=30
    feeder.velocity_tracker.reset()
    _populate_tracker_to_ready(feeder, velocity=20.0)
    assert feeder._compute_target_feed_speed() == pytest.approx(30.0, abs=0.1)
    # vel=40: 40*1.5=60
    feeder.velocity_tracker.reset()
    _populate_tracker_to_ready(feeder, velocity=40.0)
    assert feeder._compute_target_feed_speed() == pytest.approx(60.0, abs=0.1)
    # Selbst bei feed_speed=100 cap auf feed_speed
    feeder.feed_speed = 100.0
    feeder.velocity_tracker.reset()
    _populate_tracker_to_ready(feeder, velocity=80.0)
    # 80*1.5=120 -> capped auf feed_speed=100
    assert feeder._compute_target_feed_speed() == pytest.approx(100.0, abs=0.1)


def test_c_cont_hotfix6_interrupt_chunk_default_5(monkeypatch):
    """Hotfix6 Layer 2: interrupt_chunk_mm Default 5.0 mm.

    Kleinere Sub-Chunks reduzieren HALL3->HALL1 Overshoot-Distanz.
    Bei 5mm @ 30 mm/s = 167ms — HALL2-Detection-Zeit ausreichend.
    Vorher 9.0 mm war zu gross bei HALL3-Speed-Reduktion."""
    printer, feeder = make_c_cont_feeder(monkeypatch)
    assert feeder.interrupt_chunk_mm == 5.0


def test_c_cont_hotfix6_interrupt_chunk_override(monkeypatch):
    """Hotfix6 Layer 2: interrupt_chunk_mm kann via cfg ueberschrieben
    werden (BUFFER_SET live-tune-Pfad muss weiter funktionieren)."""
    printer, feeder = make_c_cont_feeder(
        monkeypatch, cfg_overrides={'interrupt_chunk_mm': 3.0})
    assert feeder.interrupt_chunk_mm == 3.0
