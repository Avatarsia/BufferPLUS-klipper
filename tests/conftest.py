import sys
import types
from pathlib import Path

import pytest


TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from fakes_klipper import FakeConfig, FakePrinter, FakePrinterStepper


if "stepper" not in sys.modules:
    fake_stepper = types.ModuleType("stepper")
    fake_stepper.PrinterStepper = FakePrinterStepper
    sys.modules["stepper"] = fake_stepper


from klipper_extras import buffer_feeder  # noqa: E402  — needs fake stepper module first


# ---------------------------------------------------------------------------
# Shared helpers (promoted from per-file copies 2026-05-12, Audit-2)
# ---------------------------------------------------------------------------


def set_sensor_active(feeder, sensor_name, active):
    """Seed a sensor's stable + raw state in polarity-aware fashion.

    P7-49: BOTH _pin_stable_state and _pin_raw_state must agree, otherwise
    check_debounce promotes a phantom edge on the very next main_tick and
    masks the actual state-transition under test.
    """
    polarity_flip = feeder._pin_polarity_flip[sensor_name]
    raw = (not active) if polarity_flip else active
    feeder._pin_stable_state[sensor_name] = raw
    feeder._pin_raw_state[sensor_name] = raw


class FakeGCmd:
    """Canonical gcmd stub for cmd_* dispatchers.

    Promoted from per-file copies in test_issue16_full_sequence /
    test_p756_cmd_coverage / test_hall1_integration / test_p746_audit_fixes /
    test_python_unload (2026-05-12, Audit-2).
    """

    def __init__(self, values=None):
        self.values = {key.upper(): value for key, value in (values or {}).items()}

    def get(self, key, default=None):
        return self.values.get(key.upper(), default)

    def get_int(self, key, default=None, **kwargs):
        return int(self.values.get(key.upper(), default))

    def get_float(self, key, default=None, **kwargs):
        return float(self.values.get(key.upper(), default))


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
    fake_printer.fire_event('klippy:connect')
    return buffer_feeder.BufferFeeder(fake_config)


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
