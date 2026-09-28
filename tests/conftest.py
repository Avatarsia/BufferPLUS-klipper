import sys
import types
from pathlib import Path

import pytest


TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fakes_klipper import FakeConfig, FakePrinter, FakePrinterStepper
from helpers import FakeGCmd, set_sensor_active


if "stepper" not in sys.modules:
    fake_stepper = types.ModuleType("stepper")
    fake_stepper.PrinterStepper = FakePrinterStepper
    sys.modules["stepper"] = fake_stepper


from klipper_extras import buffer_feeder  # noqa: E402  — needs fake stepper module first


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_printer():
    return FakePrinter()


@pytest.fixture
def fake_config(fake_printer):
    return FakeConfig(printer=fake_printer)


@pytest.fixture
def feeder(fake_printer, fake_config):
    """Standard feeder ohne klippy:connect. Fuer Tests die _stepper_enable
    nicht brauchen."""
    return buffer_feeder.BufferFeeder(fake_config)


@pytest.fixture
def feeder_with_connect(fake_printer, fake_config):
    """Feeder mit klippy:connect-Event gefeuert. Verkabelt _stepper_enable.
    Pflicht fuer Tests die Enable-Verhalten exercisen (NOT-TO-DO 2026-05-12).
    """
    feeder_obj = buffer_feeder.BufferFeeder(fake_config)
    fake_printer.fire_event('klippy:connect')
    return feeder_obj


@pytest.fixture
def feeder_factory(fake_printer):
    """Builder fuer custom Feeder-Setups.

    Args:
        values: dict patched onto FakeConfig.DEFAULT_VALUES
        state: optional buffer_feeder.STATE_* to assign to feeder._state
        grace_done: default True — sets _startup_grace_done
        fire_connect: default False — fires klippy:connect before instantiation
    """

    def make(values=None, state=None, grace_done=True, fire_connect=False):
        if fire_connect:
            fake_printer.fire_event('klippy:connect')
        config = FakeConfig(printer=fake_printer, values=values)
        feeder_obj = buffer_feeder.BufferFeeder(config)
        if grace_done:
            feeder_obj._startup_grace_done = True
        if state is not None:
            feeder_obj._state = state
        return fake_printer, feeder_obj

    return make
