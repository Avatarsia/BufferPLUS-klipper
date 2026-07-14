"""AUTO-Feed bei manueller Extrusion ausserhalb eines Drucks
(User-Request 2026-07-14).

Vorher: _auto_submit_permission erlaubte Flush-Feeding nur in Phase
'active' (print_stats=='printing'). Manuelles Extrudieren via
Mainsail (state='standby') lief den Buffer leer — der Extruder zog
den Arm bis HALL3/Blockade und der User musste in ~30mm-Schritten
(Buffer-Arm-Weg) extrudieren.

Fix: neue Phase 'manual' — ausserhalb eines Drucks (nicht 'paused',
nicht suspendiert, kein aktiver Guard) erlaubt AKTIVE Extruder-
Vorwaertsbewegung (velocity_tracker; Retracts sind im Tracker
geclampt) das Flush-Feeding. Config: feed_on_manual_extrusion
(Default True).
"""

import pytest
from fakes_klipper import FakeConfig, FakePrinter, FakePrintStats
from klipper_extras import buffer_feeder


def set_sensor_active(feeder, sensor_name, active):
    polarity_flip = feeder._pin_polarity_flip[sensor_name]
    raw = (not active) if polarity_flip else active
    feeder._pin_stable_state[sensor_name] = raw
    feeder._pin_raw_state[sensor_name] = raw


def make_feeder(values=None, ps_state="standby", moving=True):
    printer = FakePrinter()
    printer.objects["print_stats"] = FakePrintStats(state=ps_state)
    base = {'use_flush_callback_bang_bang': True}
    base.update(values or {})
    feeder = buffer_feeder.BufferFeeder(FakeConfig(printer=printer, values=base))
    printer.fire_event('klippy:connect')
    feeder._startup_grace_done = True
    feeder._state = buffer_feeder.STATE_AUTO
    set_sensor_active(feeder, 'hall_overflow', False)
    set_sensor_active(feeder, 'hall_full', False)
    set_sensor_active(feeder, 'hall_empty', True)   # Buffer leer -> Demand
    set_sensor_active(feeder, 'entrance', True)
    ext = printer.objects['extruder']
    t = 0.0
    for _ in range(14):
        ext.last_position = (t * 15.0) if moving else 0.0
        feeder.velocity_tracker.tick(t)
        t += 0.025
    return printer, feeder


def flush_submits(monkeypatch, feeder):
    calls = []
    monkeypatch.setattr(feeder, "_submit_move",
                        lambda d, s, **kw: calls.append((d, s)))
    return calls


def test_manual_extrusion_outside_print_feeds(monkeypatch):
    _, feeder = make_feeder()
    calls = flush_submits(monkeypatch, feeder)

    feeder._on_mcu_flush(flush_time=0.35, step_gen_time=0.4)

    assert len(calls) == 1, (
        "active manual extrusion (standby) must feed the buffer — "
        "otherwise it drains to HALL3 and blocks every ~30mm")
    assert calls[0][0] > 0


def test_no_feed_when_extruder_still(monkeypatch):
    _, feeder = make_feeder(moving=False)
    calls = flush_submits(monkeypatch, feeder)

    feeder._on_mcu_flush(flush_time=0.35, step_gen_time=0.4)

    assert calls == [], (
        "standby without extruder motion stays 'inactive' — no feed")


def test_manual_feed_disabled_via_config(monkeypatch):
    _, feeder = make_feeder(values={'feed_on_manual_extrusion': False})
    calls = flush_submits(monkeypatch, feeder)

    feeder._on_mcu_flush(flush_time=0.35, step_gen_time=0.4)

    assert calls == []


def test_manual_feed_respects_critical_guard(monkeypatch):
    _, feeder = make_feeder()
    calls = flush_submits(monkeypatch, feeder)
    feeder._arm_critical_action_guard('unsync')  # frisch nach UNSYNC

    feeder._on_mcu_flush(flush_time=0.35, step_gen_time=0.4)

    assert calls == [], (
        "manual feeding must respect an active critical-action guard")


def test_manual_feed_respects_suspend(monkeypatch):
    _, feeder = make_feeder(ps_state="paused")
    feeder._bang_bang_suspended = True
    calls = flush_submits(monkeypatch, feeder)

    feeder._on_mcu_flush(flush_time=0.35, step_gen_time=0.4)

    assert calls == []
