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
    """FakePrinter mit FakeExtruder (lookup_object('extruder') returns
    Object with get_status returning dict with 'position' key)."""
    printer = FakePrinter()
    fake_ext = types.SimpleNamespace()
    fake_ext._position = 0.0
    def get_status(eventtime):
        return {'position': fake_ext._position}
    fake_ext.get_status = get_status
    printer.objects['extruder'] = fake_ext
    return printer, fake_ext


def test_tracker_zero_initial(fake_extruder_printer):
    printer, _ = fake_extruder_printer
    tracker = buffer_feeder.ExtruderVelocityTracker(
        owner=None, printer=printer,
        sample_interval=0.025, window_size=0.3,
        filament_diameter=1.75)
    assert tracker.get_velocity() == 0.0
    assert tracker.get_volumetric_flow() == 0.0
    assert not tracker.is_ready()


def test_tracker_steady_state(fake_extruder_printer):
    printer, ext = fake_extruder_printer
    tracker = buffer_feeder.ExtruderVelocityTracker(
        owner=None, printer=printer,
        sample_interval=0.025, window_size=0.3,
        filament_diameter=1.75)
    t = 0.0
    for _ in range(12):
        ext._position = t * 10.0
        tracker.tick(t)
        t += 0.025
    assert tracker.is_ready()
    assert tracker.get_velocity() == pytest.approx(10.0, abs=0.1)


def test_tracker_velocity_step_lag(fake_extruder_printer):
    printer, ext = fake_extruder_printer
    tracker = buffer_feeder.ExtruderVelocityTracker(
        owner=None, printer=printer,
        sample_interval=0.025, window_size=0.3,
        filament_diameter=1.75)
    t = 0.0
    for _ in range(6):
        ext._position = 0.0
        tracker.tick(t)
        t += 0.025
    pos = 0.0
    for _ in range(6):
        pos += 10.0 * 0.025
        ext._position = pos
        tracker.tick(t)
        t += 0.025
    assert 3.0 < tracker.get_velocity() < 7.0


def test_tracker_retract_clamped(fake_extruder_printer):
    printer, ext = fake_extruder_printer
    tracker = buffer_feeder.ExtruderVelocityTracker(
        owner=None, printer=printer,
        sample_interval=0.025, window_size=0.3,
        filament_diameter=1.75)
    t = 0.0
    pos = 100.0
    for _ in range(12):
        ext._position = pos
        tracker.tick(t)
        pos -= 1.0
        t += 0.025
    assert tracker.get_velocity() == 0.0


def test_tracker_volumetric_calc(fake_extruder_printer):
    printer, ext = fake_extruder_printer
    tracker = buffer_feeder.ExtruderVelocityTracker(
        owner=None, printer=printer,
        sample_interval=0.025, window_size=0.3,
        filament_diameter=1.75)
    t = 0.0
    for _ in range(12):
        ext._position = t * 10.0
        tracker.tick(t)
        t += 0.025
    cross_section = math.pi * (1.75 / 2.0) ** 2
    expected = 10.0 * cross_section
    assert tracker.get_volumetric_flow() == pytest.approx(expected, abs=0.5)


def test_tracker_is_ready_threshold(fake_extruder_printer):
    printer, ext = fake_extruder_printer
    tracker = buffer_feeder.ExtruderVelocityTracker(
        owner=None, printer=printer,
        sample_interval=0.025, window_size=0.3,
        filament_diameter=1.75)
    t = 0.0
    for i in range(12):
        ext._position = t * 5.0
        tracker.tick(t)
        t += 0.025
        if i < 11:
            assert not tracker.is_ready()
    assert tracker.is_ready()
