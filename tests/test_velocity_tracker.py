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
    """FakePrinter mit FakeExtruder (Mainline-API: last_position-Attribut).

    Hotfix 2026-05-13: ExtruderVelocityTracker liest extruder.last_position
    direkt (Mainline-Klipper-API), KEIN get_status['position']. get_status
    bleibt als leeres dict zur Doku, dass Mainline keinen 'position'-Key
    hat."""
    printer = FakePrinter()
    fake_ext = types.SimpleNamespace()
    fake_ext.last_position = 0.0  # Mainline-API
    fake_ext.get_status = lambda eventtime: {}  # Mainline hat KEIN 'position'
    printer.objects['extruder'] = fake_ext
    return printer, fake_ext


def test_tracker_zero_initial(fake_extruder_printer):
    printer, _ = fake_extruder_printer
    tracker = buffer_feeder.ExtruderVelocityTracker(
        owner=None, printer=printer,
        sample_interval=0.02, window_size=0.3,
        filament_diameter=1.75)
    assert tracker.get_velocity() == 0.0
    assert tracker.get_volumetric_flow() == 0.0
    assert not tracker.is_ready()


def test_tracker_steady_state(fake_extruder_printer):
    printer, ext = fake_extruder_printer
    tracker = buffer_feeder.ExtruderVelocityTracker(
        owner=None, printer=printer,
        sample_interval=0.02, window_size=0.3,
        filament_diameter=1.75)
    t = 0.0
    for _ in range(16):
        ext.last_position = t * 10.0
        tracker.tick(t)
        t += 0.02
    assert tracker.is_ready()
    assert tracker.get_velocity() == pytest.approx(10.0, abs=0.1)


def test_tracker_velocity_step_lag(fake_extruder_printer):
    printer, ext = fake_extruder_printer
    tracker = buffer_feeder.ExtruderVelocityTracker(
        owner=None, printer=printer,
        sample_interval=0.02, window_size=0.3,
        filament_diameter=1.75)
    t = 0.0
    for _ in range(8):
        ext.last_position = 0.0
        tracker.tick(t)
        t += 0.02
    pos = 0.0
    for _ in range(8):
        pos += 10.0 * 0.02
        ext.last_position = pos
        tracker.tick(t)
        t += 0.02
    assert 3.0 < tracker.get_velocity() < 7.0


def test_tracker_retract_clamped(fake_extruder_printer):
    printer, ext = fake_extruder_printer
    tracker = buffer_feeder.ExtruderVelocityTracker(
        owner=None, printer=printer,
        sample_interval=0.02, window_size=0.3,
        filament_diameter=1.75)
    t = 0.0
    pos = 100.0
    for _ in range(16):
        ext.last_position = pos
        tracker.tick(t)
        pos -= 1.0
        t += 0.02
    assert tracker.get_velocity() == 0.0


def test_tracker_volumetric_calc(fake_extruder_printer):
    printer, ext = fake_extruder_printer
    tracker = buffer_feeder.ExtruderVelocityTracker(
        owner=None, printer=printer,
        sample_interval=0.02, window_size=0.3,
        filament_diameter=1.75)
    t = 0.0
    for _ in range(16):
        ext.last_position = t * 10.0
        tracker.tick(t)
        t += 0.02
    cross_section = math.pi * (1.75 / 2.0) ** 2
    expected = 10.0 * cross_section
    assert tracker.get_volumetric_flow() == pytest.approx(expected, abs=0.5)


def test_tracker_is_ready_threshold(fake_extruder_printer):
    printer, ext = fake_extruder_printer
    tracker = buffer_feeder.ExtruderVelocityTracker(
        owner=None, printer=printer,
        sample_interval=0.02, window_size=0.3,
        filament_diameter=1.75)
    t = 0.0
    for i in range(16):
        ext.last_position = t * 5.0
        tracker.tick(t)
        t += 0.02
        if i < 15:
            assert not tracker.is_ready()
    assert tracker.is_ready()


def test_tracker_irregular_ticks_use_exact_time_window(fake_extruder_printer):
    printer, ext = fake_extruder_printer
    tracker = buffer_feeder.ExtruderVelocityTracker(
        owner=None, printer=printer,
        sample_interval=0.02, window_size=0.3,
        filament_diameter=1.75)

    for t in (0.0, 0.031, 0.079, 0.121, 0.188, 0.251, 0.317, 0.389):
        ext.last_position = t * 7.0
        tracker.tick(t)

    assert tracker.is_ready()
    assert tracker.get_velocity() == pytest.approx(7.0)
    assert tracker._samples[0][0] <= 0.389 - 0.3
    assert tracker._samples[1][0] > 0.389 - 0.3


def test_tracker_long_gap_rearms_window(fake_extruder_printer):
    printer, ext = fake_extruder_printer
    tracker = buffer_feeder.ExtruderVelocityTracker(
        owner=None, printer=printer,
        sample_interval=0.02, window_size=0.3,
        filament_diameter=1.75)

    ext.last_position = 0.0
    tracker.tick(0.0)
    ext.last_position = 4.0
    tracker.tick(0.4)

    assert not tracker.is_ready()
    assert tracker.get_velocity() == 0.0
