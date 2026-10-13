"""Motor bleibt nach LOAD dauerbestromt (User-Report 2026-07-13).

Wurzel: Der AUTO-Idle-Disable (idle_motor_disable=True, Weg 2) laeuft
nur NACH einem Watchdog-Anchor — das Watchdog-Gate blockt aber bei
hall_full. Nach LOAD/park_full ist der Buffer voll -> Anchor feuert
nie -> Disable-Zweig nie erreicht -> Stepper stundenlang bestromt
(kochend heiss).

Das hall_full-Gate stammt aus Weg 1: ein ENABLED-Anchor schiebt
~18 mm/h Richtung HALL1 (Codex-Verify-Finding). Bei Weg 2 laeuft der
Anchor enable-los (skip_enable, Treiber stromlos -> ignoriert Pulse)
— der Drift-Grund entfaellt. Fix: hall_full blockt nur noch bei Weg 1
oder im P7-78-Print-Override (dessen Anchor enabled + forced_t0
submittet, dort bleibt der Drift-Schutz noetig).
"""

import pytest
from fakes_klipper import FakeConfig, FakePrinter
from klipper_extras import buffer_feeder


def set_sensor_active(feeder, sensor_name, active):
    polarity_flip = feeder._pin_polarity_flip[sensor_name]
    raw = (not active) if polarity_flip else active
    feeder._pin_stable_state[sensor_name] = raw
    feeder._pin_raw_state[sensor_name] = raw


def make_full_auto_feeder(values=None):
    """AUTO nach LOAD: Buffer voll (HALL2), kein Druck, Motor an."""
    printer = FakePrinter()
    config = FakeConfig(printer=printer, values=values)
    feeder = buffer_feeder.BufferFeeder(config)
    printer.fire_event('klippy:connect')
    feeder._startup_grace_done = True
    feeder._state = buffer_feeder.STATE_AUTO
    set_sensor_active(feeder, 'hall_overflow', False)
    set_sensor_active(feeder, 'hall_full', True)
    set_sensor_active(feeder, 'hall_empty', False)
    set_sensor_active(feeder, 'entrance', True)
    feeder.reactor.now = 20.0
    feeder._last_move_end_time = 0.0
    return printer, feeder


def spies(monkeypatch, feeder):
    anchors, disables = [], []

    def _anchor_spy(**kw):
        anchors.append(kw)
        feeder._last_move_end_time = 20.001
        return 1.0
    monkeypatch.setattr(feeder.sync, "_submit_anchor_move", _anchor_spy)
    monkeypatch.setattr(feeder, "_schedule_stepper_disable",
                        lambda: disables.append('disable'))
    return anchors, disables


def test_weg2_anchor_and_disable_fire_despite_hall_full(monkeypatch):
    _, feeder = make_full_auto_feeder(values={'idle_motor_disable': True})
    anchors, disables = spies(monkeypatch, feeder)

    feeder._main_tick(eventtime=20.0)

    assert len(anchors) == 1, (
        "Weg 2: enable-less anchor must fire despite hall_full — the "
        "drift rationale only applies to enabled (Weg 1) anchors")
    assert anchors[0].get('skip_enable') is True
    assert disables == ['disable'], (
        "the whole point: motor must go powerless after the anchor")


def test_weg1_still_blocked_by_hall_full(monkeypatch):
    _, feeder = make_full_auto_feeder(values={'idle_motor_disable': False})
    anchors, _ = spies(monkeypatch, feeder)

    feeder._main_tick(eventtime=20.0)

    assert anchors == [], (
        "Weg 1 keeps the hall_full block — enabled anchors push "
        "~18mm/h toward HALL1 overflow")
