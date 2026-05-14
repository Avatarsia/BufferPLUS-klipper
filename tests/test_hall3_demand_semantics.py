"""HALL3-Demand-Semantik — Wurzel C Praeventionsfix.

Hardware-Beleg (vier widerlegte Pre-Anchor-Iterationen):
  V0/V1/D2/V3 Crashes traten alle beim Druckstart auf, wenn:
    - HALL3=True (Buffer-Arm in entrance-Position, idle resting)
    - Klipper geht zu state='printing'
    - Idle-Suppression oeffnet
    - _flush_submit_streaming_chunk feuert sofort
    - Race mit PRINT_START Macro (SET_KINEMATIC_POSITION + TMC-UART)
    -> MCU 'LLL_PLUS' shutdown: Timer too close

Wurzel-Analyse: HALL3 hat zwei Bedeutungen:
  1. Aktiver Print: Extruder zieht Filament -> Arm hochgezogen -> echter
     Demand fuer Refill
  2. Idle: Arm liegt natuerlich oben durch fehlende Zugkraft -> KEIN
     Demand, Buffer ist einfach nur in Ruhe

Soft-Throttle (Hotfix 7) behandelt beide Faelle gleich (HALL3 -> MIN_FLOOR
*1.5). Im Druckstart-Moment laeuft das in den USB-Burst-Race.

Fix (γ): HALL3 nur als Demand-Signal interpretieren wenn der Extruder
tatsaechlich Filament zieht (tracker_velocity > 0). Bei idle / Pre-Print
ohne ext_vel -> target_speed=0 -> kein Submit -> kein Race.

Komplementaer zu PR #39 (Watchdog hall_empty Bypass) und Maintainer
97e97e7 (forced_t0 sanitize). Eliminiert die haeufigste Trigger-
Bedingung fuer Wurzel C strukturell, ohne Pre-Anchor-Versuch.
"""

import pytest

from fakes_klipper import FakeConfig, FakePrinter, FakePrintStats
from klipper_extras import buffer_feeder


def set_sensor_active(feeder, sensor_name, active):
    polarity_flip = feeder._pin_polarity_flip[sensor_name]
    raw = (not active) if polarity_flip else active
    feeder._pin_stable_state[sensor_name] = raw
    feeder._pin_raw_state[sensor_name] = raw


def make_modulator_feeder(values=None):
    base = {"use_flush_callback_bang_bang": True}
    if values:
        base.update(values)
    printer = FakePrinter()
    printer.objects["print_stats"] = FakePrintStats(state="printing")
    config = FakeConfig(printer=printer, values=base)
    feeder = buffer_feeder.BufferFeeder(config)
    printer.fire_event('klippy:connect')
    feeder._startup_grace_done = True
    feeder._state = buffer_feeder.STATE_AUTO
    set_sensor_active(feeder, 'hall_overflow', False)
    set_sensor_active(feeder, 'hall_full', False)
    set_sensor_active(feeder, 'hall_empty', False)
    return printer, feeder


def populate_tracker(feeder, velocity, samples=12):
    """Tracker mit linearer Position-Steigerung fuellen bis is_ready=True."""
    fake_ext = feeder.printer.objects['extruder']
    fake_ext.last_position = 0.0
    t = 0.0
    for _ in range(samples):
        fake_ext.last_position = t * velocity
        feeder.velocity_tracker.tick(t)
        t += 0.025


# ---------------------------------------------------------------------------
# Wurzel-C-Fix γ — HALL3 ohne ext_vel ist KEIN Demand
# ---------------------------------------------------------------------------


def test_hall3_alone_without_extruder_velocity_returns_zero():
    """Wurzel-C-Fix γ Kern: HALL3=True bei idle (kein extruder-Verbrauch)
    -> target_speed=0. Idle-Buffer-Arm-Position ist KEIN demand.

    Pre-Fix: hall_empty=True -> MIN_FLOOR (=15) auch bei ext_vel=0 ->
    triggert streaming-submit beim Druckstart -> Race mit
    SET_KINEMATIC_POSITION + TMC-UART -> Timer too close.

    Fix: HALL3 nur als demand wenn ext_vel > 0 (Extruder zieht aktiv).
    """
    printer, feeder = make_modulator_feeder()
    set_sensor_active(feeder, 'hall_empty', True)
    # Tracker NICHT zu ready bringen -> ext_vel=0
    assert not feeder.velocity_tracker.is_ready()

    result = feeder._compute_target_feed_speed()
    assert result == 0.0, (
        "HALL3+idle (kein ext_vel): target_speed=0. "
        "Erhalten: %.3f mm/s" % result)


def test_hall3_with_tracker_ready_but_zero_velocity_returns_zero():
    """HALL3=True, tracker ready aber ext_vel=0 (Pause / Heat-Up):
    immer noch KEIN Demand. Tracker-readiness allein reicht nicht;
    es muss aktive Bewegung sein."""
    printer, feeder = make_modulator_feeder()
    set_sensor_active(feeder, 'hall_empty', True)
    # Tracker zu ready bringen, aber mit velocity=0
    populate_tracker(feeder, velocity=0.0)
    assert feeder.velocity_tracker.is_ready()
    assert feeder.velocity_tracker.get_velocity() == 0.0

    result = feeder._compute_target_feed_speed()
    assert result == 0.0, (
        "HALL3 + tracker ready + ext_vel=0: target_speed=0. "
        "Erhalten: %.3f" % result)


def test_hall3_with_active_extruder_returns_demand():
    """Regression: HALL3=True UND Extruder zieht aktiv (ext_vel>0):
    Soft-Throttle-Demand wie vorher = max(MIN_FLOOR, ext_vel*1.5).
    Mid-Print-Refill bleibt unveraendert.
    """
    printer, feeder = make_modulator_feeder()
    set_sensor_active(feeder, 'hall_empty', True)
    populate_tracker(feeder, velocity=15.0)

    result = feeder._compute_target_feed_speed()
    # ext_vel=15 -> 15*1.5=22.5 mm/s (wie Hotfix 7 Soft-Throttle)
    assert result == pytest.approx(22.5, abs=0.1), (
        "HALL3 + ext_vel=15: target_speed=22.5 (Soft-Throttle). "
        "Erhalten: %.3f" % result)


def test_hall3_low_velocity_still_uses_min_floor():
    """HALL3 + sehr kleine extruder-vel (0 < vel < MIN_FLOOR):
    soll target_speed = MIN_FLOOR setzen (Soft-Throttle-Untergrenze).
    Wichtig fuer den Start des Mid-Print-Refills bei langsamer
    Anlauf-Phase."""
    printer, feeder = make_modulator_feeder()
    set_sensor_active(feeder, 'hall_empty', True)
    # vel < MIN_FLOOR (typisch 15) aber > 0
    populate_tracker(feeder, velocity=5.0)

    result = feeder._compute_target_feed_speed()
    # ext_vel=5 -> 5*1.5=7.5, aber max(7.5, MIN_FLOOR=15)=15
    assert result == pytest.approx(feeder.min_feed_floor, abs=0.1), (
        "HALL3 + low ext_vel: target_speed=MIN_FLOOR (Soft-Throttle "
        "Untergrenze). Erhalten: %.3f, erwartet: %.3f"
        % (result, feeder.min_feed_floor))


# ---------------------------------------------------------------------------
# Regression: andere Pfade bleiben unveraendert
# ---------------------------------------------------------------------------


def test_hall1_overflow_returns_zero_regardless_of_extruder_velocity():
    """Regression: HALL1=True -> 0 unabhaengig von ext_vel."""
    printer, feeder = make_modulator_feeder()
    set_sensor_active(feeder, 'hall_overflow', True)
    populate_tracker(feeder, velocity=20.0)
    assert feeder._compute_target_feed_speed() == 0.0


def test_hall2_full_returns_zero_regardless_of_extruder_velocity():
    """Regression: HALL2=True -> 0 (drain via toolhead, kein push)."""
    printer, feeder = make_modulator_feeder()
    set_sensor_active(feeder, 'hall_full', True)
    populate_tracker(feeder, velocity=20.0)
    assert feeder._compute_target_feed_speed() == 0.0


def test_zwischenzone_with_active_extruder_unchanged():
    """Regression: Zwischenzone + ext_vel>0 -> max(MIN_FLOOR, ext_vel*gain)."""
    printer, feeder = make_modulator_feeder()
    # Alle halls inactive
    populate_tracker(feeder, velocity=20.0)
    result = feeder._compute_target_feed_speed()
    # max(15, 20*1.10=22) = 22
    assert result == pytest.approx(22.0, abs=1.0)


def test_zwischenzone_idle_unchanged():
    """Regression: Zwischenzone + ext_vel=0 -> 0 (unveraendert)."""
    printer, feeder = make_modulator_feeder()
    # Alle halls inactive, tracker not ready
    result = feeder._compute_target_feed_speed()
    assert result == 0.0


# ---------------------------------------------------------------------------
# Wurzel-C-Trigger eliminiert
# ---------------------------------------------------------------------------


def test_print_start_with_hall3_no_extrusion_no_streaming_submit():
    """End-to-end Trigger-Test: HALL3=True + state='printing' +
    ext_vel=0 (frisch nach SD-Print-Start, vor Heat-Up). Erwartung:
    _flush_submit_streaming_chunk submittet KEINEN streaming-chunk —
    weil target_speed=0 in dieser Wurzel-Konstellation.

    Das eliminiert die haeufigste Wurzel-C-Trigger-Bedingung
    strukturell.
    """
    printer, feeder = make_modulator_feeder()
    motion_q = printer.lookup_object('motion_queuing')
    set_sensor_active(feeder, 'hall_empty', True)
    # Tracker not ready (frisch nach Klipper-restart)
    assert not feeder.velocity_tracker.is_ready()
    feeder._stepcompress_primed = True

    feeder.reactor.now = 10.0
    appends_before = len(motion_q.append_calls)
    motion_q.trigger_flush(flush_time=10.0, step_gen_time=10.0)
    own = [c for c in motion_q.append_calls[appends_before:]
           if c[0] is feeder.trapq]

    assert len(own) == 0, (
        "Wurzel-C-Trigger eliminiert: HALL3 + idle (kein ext_vel) "
        "DARF KEIN streaming-submit ausloesen. Erhalten: %d appends"
        % len(own))
