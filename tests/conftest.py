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


@pytest.fixture
def fake_printer():
    return FakePrinter()


@pytest.fixture
def fake_config(fake_printer):
    return FakeConfig(printer=fake_printer)
