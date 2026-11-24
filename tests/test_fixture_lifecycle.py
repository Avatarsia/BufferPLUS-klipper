"""Regressions for the shared fake Klipper lifecycle."""

import pytest

from fakes_klipper import FakeConfig


@pytest.fixture
def fake_config(fake_printer):
    return FakeConfig(printer=fake_printer, values={'feed_speed': 73.0})


def test_shared_feeder_honors_supplied_config_fixture(feeder, fake_config):
    assert feeder.feed_speed == 73.0
    assert feeder.stepper.config is fake_config


def test_factory_connect_wires_motor_after_construction(feeder_factory):
    printer, feeder = feeder_factory(fire_connect=True)
    handle = printer.lookup_object('stepper_enable').lookup_enable(
        feeder.stepper.get_name())

    feeder._enable_stepper()

    assert len(handle.enables) == 1
    assert handle.enables[0] > 0.0


def test_factory_default_leaves_connect_explicit(feeder_factory):
    _, feeder = feeder_factory()

    assert feeder._stepper_enable is None
    assert feeder._startup_grace_done is True


def test_factory_can_preserve_startup_grace(feeder_factory):
    _, feeder = feeder_factory(grace_done=False)

    assert feeder._startup_grace_done is False
