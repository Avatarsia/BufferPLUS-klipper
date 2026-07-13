"""Codex-Review-Runde 1 (2026-07-13) auf den Gruppe-1..4-Fixes.

Finding 1: unsync forced-completion loeste den Sync-Latch auch dann,
wenn AUCH das Recovery-set_trapq fehlschlug — Submits waeren wieder
erlaubt, obwohl der Stepper evtl. noch auf der Extruder-Trapq haengt.
Latch darf nur nach ERFOLGREICHEM Recovery geloest werden (Fehlschlag
= harter Lockout, Retry via BUFFER_UNSYNC).
Gleiches Muster im sync_to_extruder-Rollback: schlaegt der Rollback
fehl, muss der Latch die Realitaet (halb-geswappt) reflektieren und
Submits blocken.

Finding 2: Der UNLOAD_FILAMENT-Entry-Guard erlaubte STATE_INIT ohne
Grace-Check — UNLOAD konnte waehrend der 2s-Boot-Grace (Sensorbild
nicht settled, Safety-Logik suspendiert) Bewegungen starten.
"""

import pytest
from fakes_klipper import FakeConfig, FakePrinter
from helpers import FakeGCmd
from klipper_extras import buffer_feeder


def set_sensor_active(feeder, sensor_name, active):
    polarity_flip = feeder._pin_polarity_flip[sensor_name]
    raw = (not active) if polarity_flip else active
    feeder._pin_stable_state[sensor_name] = raw
    feeder._pin_raw_state[sensor_name] = raw


def make_feeder(values=None):
    printer = FakePrinter()
    config = FakeConfig(printer=printer, values=values)
    feeder = buffer_feeder.BufferFeeder(config)
    printer.fire_event('klippy:connect')
    feeder._startup_grace_done = True
    feeder._state = buffer_feeder.STATE_IDLE
    for name in ('hall_overflow', 'hall_full', 'hall_empty'):
        set_sensor_active(feeder, name, False)
    set_sensor_active(feeder, 'entrance', True)
    return printer, feeder


def test_unsync_double_failure_keeps_sync_latch(monkeypatch):
    """Recovery-set_trapq schlaegt AUCH fehl: Latch muss gesetzt
    bleiben — sonst laufen Own-Submits auf der Extruder-Trapq."""
    printer, feeder = make_feeder()
    feeder._sync_to_extruder('extruder')

    def _flush_boom():
        raise RuntimeError("flush failed")
    monkeypatch.setattr(
        printer.objects['toolhead'], "flush_step_generation", _flush_boom)

    def _trapq_boom(trapq):
        raise RuntimeError("set_trapq failed")
    monkeypatch.setattr(feeder.stepper, "set_trapq", _trapq_boom)

    with pytest.raises(RuntimeError):
        feeder._unsync_if_synced()

    assert feeder._stepper_synced_to is not None, (
        "failed recovery must keep the sync latch — clearing it "
        "re-enables submits on a stepper that may still be bound to "
        "the extruder trapq")


def test_unsync_single_failure_still_recovers(monkeypatch):
    """Regression-Guard: schlaegt nur der Swap fehl, das Recovery aber
    durch, wird der Latch geloest (Gruppe-2-Verhalten)."""
    printer, feeder = make_feeder()
    feeder._sync_to_extruder('extruder')

    def _flush_boom():
        raise RuntimeError("flush failed")
    monkeypatch.setattr(
        printer.objects['toolhead'], "flush_step_generation", _flush_boom)

    with pytest.raises(RuntimeError):
        feeder._unsync_if_synced()

    assert feeder._stepper_synced_to is None
    assert feeder.stepper.trapq is feeder.sync.trapq
    assert feeder._stepcompress_primed is False


def test_sync_rollback_failure_arms_latch(monkeypatch):
    """Schlaegt der Sync-Rollback fehl (Stepper evtl. auf Extruder-
    Trapq haengengeblieben), muss der Latch gesetzt sein und Submits
    blocken."""
    printer, feeder = make_feeder()
    real_set_trapq = feeder.stepper.set_trapq
    calls = {'n': 0}

    def _trapq_swap_then_boom(trapq):
        calls['n'] += 1
        if calls['n'] == 1:
            return real_set_trapq(trapq)  # Swap auf Extruder laeuft durch
        raise RuntimeError("rollback set_trapq failed")
    monkeypatch.setattr(feeder.stepper, "set_trapq", _trapq_swap_then_boom)

    def _scan_boom():
        raise RuntimeError("scan windows failed")
    monkeypatch.setattr(
        printer.objects['motion_queuing'],
        "check_step_generation_scan_windows", _scan_boom)

    with pytest.raises(RuntimeError):
        feeder._sync_to_extruder('extruder')

    assert feeder._stepper_synced_to == 'extruder', (
        "failed rollback must arm the latch to reflect the half-swap "
        "and keep own-trapq submits blocked")


def test_unload_filament_rejected_during_startup_grace():
    printer, feeder = make_feeder()
    feeder._startup_grace_done = False
    feeder._state = buffer_feeder.STATE_INIT

    with pytest.raises(Exception, match="[Gg]race"):
        feeder.cmd_BUFFER_UNLOAD_FILAMENT(FakeGCmd())

    assert printer.objects['gcode'].script_invocations == []
