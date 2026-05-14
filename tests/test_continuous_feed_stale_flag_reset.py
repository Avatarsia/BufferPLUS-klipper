"""Bugfix: _continuous_feed-Stale-Flag-Reset bei Print-Ende.

Hardware-Beleg (2026-05-14, nach erfolgreichem Fix-1-End-to-End-Druck):
  - Print 1 lief 30.7 min durch (vollstaendig successful)
  - Print 1 endete mit HALL3=True (Buffer wurde leergezogen)
  - 1 Stunde Idle
  - User setzt HALL3=True (bewusst) + startet Print 2
  - Print 2: Heating + 3x QGL-Retries > 240s ohne Extruder-Bewegung
  - _jam_tick triggert nach exakt 240s:
      *** JAM SUPPLY: HALL3 active 240s with feeder running ***
  - Aber: Buffer-Feeder hat in dieser Zeit GAR NICHT gefoerdert
    (Fix 1: target_speed=0 weil ext_vel=0; flush_no_demand kontinuierlich)

Wurzel: `_continuous_feed` ist ein Flag der beim Submit auf True gesetzt
wird. Reset passiert via:
  - `_halt_motion()` (HALT/OVERFLOW/JAM-Pfade)
  - `_on_idle_ready` PAUSED-Branch
  - NICHT im `_on_idle_ready` "Print ended normally"-Branch!

Konsequenz: Wenn Print regulaer endet (state=complete/standby), bleibt
`_continuous_feed=True` aus dem letzten Mid-Print-Submit. Beim naechsten
Print mit `target_speed=0` (Fix 1) wird der Flag NICHT neu gesetzt —
ist aber als stale-True bereits aktiv. _jam_tick interpretiert das als
"feeder_running_fwd=True" und startet den 240s-Dwell-Counter falsch.

Latenzgrad: Bug existiert seit Hotfix 7 (Soft-Throttle Einfuehrung),
war aber durch sofortige HALL3-Submits maskiert (Arm fiel auf HALL2
binnen Sekunden -> dwell-Counter Reset). Fix 1 (HALL3-Demand-Semantik)
hat den Bug entlarvt, NICHT verursacht.

Fix: `_continuous_feed = False` im `_on_idle_ready` ended-normally-Branch
ergaenzen — analog zum existierenden PAUSE-Branch-Reset.
"""

import pytest

from fakes_klipper import FakeConfig, FakePrinter, FakePrintStats
from klipper_extras import buffer_feeder


def set_sensor_active(feeder, sensor_name, active):
    polarity_flip = feeder._pin_polarity_flip[sensor_name]
    raw = (not active) if polarity_flip else active
    feeder._pin_stable_state[sensor_name] = raw
    feeder._pin_raw_state[sensor_name] = raw


def make_print_ended_feeder(print_state='complete'):
    """Feeder im Print-aktiv-Zustand, der gleich auf 'ended' wechseln
    wird via _on_idle_ready Hook."""
    base = {"use_flush_callback_bang_bang": True}
    printer = FakePrinter()
    printer.objects["print_stats"] = FakePrintStats(state=print_state)
    config = FakeConfig(printer=printer, values=base)
    feeder = buffer_feeder.BufferFeeder(config)
    printer.fire_event('klippy:connect')
    feeder._startup_grace_done = True
    feeder._state = buffer_feeder.STATE_AUTO
    set_sensor_active(feeder, 'hall_overflow', False)
    set_sensor_active(feeder, 'hall_full', False)
    set_sensor_active(feeder, 'hall_empty', False)
    return printer, feeder


# ---------------------------------------------------------------------------
# Bugfix-Tests
# ---------------------------------------------------------------------------


def test_continuous_feed_resets_on_print_ended_normally():
    """Wurzel: Print endet 'complete' / 'standby' -> _continuous_feed
    MUSS auf False zurueckgesetzt werden. Sonst stale-True triggert
    beim naechsten Print false-positive JAM SUPPLY.

    Pre-Fix: nur PAUSE-Branch resettet den Flag; "Print ended normally"
    laesst stale True stehen."""
    printer, feeder = make_print_ended_feeder(print_state='complete')
    # Simuliere aktiven Print mit laufendem continuous_feed
    feeder._print_running = True
    feeder._continuous_feed = True
    feeder._continuous_feed_direction = 1

    feeder._on_idle_ready()

    assert feeder._continuous_feed is False, (
        "Bugfix: _continuous_feed muss bei Print-Ende auf False "
        "zurueckgesetzt werden. Sonst bleibt der stale-Flag bis "
        "zum naechsten Print und triggert false-positive "
        "SUPPLY-JAM-Detection.")


def test_continuous_feed_direction_resets_on_print_ended_normally():
    """Auch _continuous_feed_direction muss zurueckgesetzt werden
    (Defense-in-depth — beide Felder sollten konsistent sein)."""
    printer, feeder = make_print_ended_feeder(print_state='complete')
    feeder._print_running = True
    feeder._continuous_feed = True
    feeder._continuous_feed_direction = 1

    feeder._on_idle_ready()

    assert feeder._continuous_feed_direction == 0, (
        "Bugfix: _continuous_feed_direction muss bei Print-Ende "
        "auf 0 zurueckgesetzt werden (konsistent mit _continuous_feed=False).")


def test_continuous_feed_resets_on_standby_state():
    """'standby' (Print-cancelled) muss genauso resetten wie 'complete'."""
    printer, feeder = make_print_ended_feeder(print_state='standby')
    feeder._print_running = True
    feeder._continuous_feed = True
    feeder._continuous_feed_direction = 1

    feeder._on_idle_ready()

    assert feeder._continuous_feed is False
    assert feeder._continuous_feed_direction == 0


# ---------------------------------------------------------------------------
# Regression: PAUSE-Branch behaviour bleibt identisch
# ---------------------------------------------------------------------------


def test_continuous_feed_still_resets_on_pause_unchanged():
    """Regression: PAUSE-Branch hat schon vor dem Fix _continuous_feed=
    False gesetzt. Das bleibt unveraendert (Defense-in-depth)."""
    printer, feeder = make_print_ended_feeder(print_state='paused')
    feeder._print_running = True
    feeder._continuous_feed = True
    feeder._continuous_feed_direction = 1

    feeder._on_idle_ready()

    assert feeder._continuous_feed is False
    assert feeder._bang_bang_suspended is True


# ---------------------------------------------------------------------------
# End-to-end: kein false-positive JAM SUPPLY nach Print-End-Idle
# ---------------------------------------------------------------------------


def test_no_false_positive_jam_supply_after_print_end_then_idle():
    """End-to-end: Print endet 'complete' bei HALL3=True + _continuous_-
    feed=True. Naechster Print startet nach 1h Idle, HALL3 noch True,
    aber Buffer-Feeder ist still (target_speed=0 wegen Fix 1).
    _jam_tick darf KEINEN dwell-Counter starten weil _continuous_feed
    nach Print-Ende False sein muss.

    Bug-Reproduktion vor Fix:
      _continuous_feed=True (stale) + hall_empty=True
        -> feeder_running_fwd=True
        -> _hall3_start_time gesetzt
        -> 240s spaeter: false-positive JAM SUPPLY
    """
    printer, feeder = make_print_ended_feeder(print_state='complete')
    feeder._print_running = True
    feeder._continuous_feed = True
    feeder._continuous_feed_direction = 1
    set_sensor_active(feeder, 'hall_empty', True)

    # Simulate Print-Ende
    feeder._on_idle_ready()

    # Nach Print-Ende: _continuous_feed muss False sein
    assert not feeder._continuous_feed, "Bugfix: stale-Flag-Reset"

    # Simuliere neuen Print-Start: _print_running=True, HALL3=True,
    # aber target_speed=0 (Fix 1 - kein Submit weil ext_vel=0).
    # _continuous_feed bleibt False weil kein Submit erfolgte.
    feeder._print_running = True  # neuer Print

    # _jam_tick check (manuell): feeder_running_fwd Berechnung
    feeder_running_fwd = (feeder._continuous_feed
                          and feeder._continuous_feed_direction == 1)
    assert feeder_running_fwd is False, (
        "Bugfix: feeder_running_fwd muss False sein wenn "
        "_continuous_feed nach Print-Ende-Reset auf False steht. "
        "Sonst false-positive JAM SUPPLY beim naechsten Print mit "
        "langer Heat-Up-Phase (240s ohne Extrusion).")
