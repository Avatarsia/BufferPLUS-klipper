import pytest

from fakes_klipper import FakeConfig, FakePrinter
from klipper_extras import buffer_feeder


def make_feeder():
    printer = FakePrinter()
    config = FakeConfig(printer=printer)
    feeder = buffer_feeder.BufferFeeder(config)
    return printer, feeder


def test_unsync_if_synced_without_sync_is_noop():
    printer, feeder = make_feeder()
    toolhead = printer.lookup_object("toolhead")
    motion_queuing = printer.lookup_object("motion_queuing")
    trapq_before = feeder.stepper.last_trapq_set
    flush_calls_before = toolhead.flush_calls
    scan_checks_before = motion_queuing.scan_window_checks

    result = feeder._unsync_if_synced()

    assert result is False
    assert feeder._stepper_synced_to is None
    assert toolhead.flush_calls == flush_calls_before
    assert motion_queuing.scan_window_checks == scan_checks_before
    assert feeder.stepper.last_trapq_set is trapq_before


def test_unsync_if_synced_restores_own_trapq_and_resets_sync_state():
    printer, feeder = make_feeder()
    toolhead = printer.lookup_object("toolhead")
    motion_queuing = printer.lookup_object("motion_queuing")
    extruder = printer.lookup_object("extruder")
    feeder.stepper.set_trapq(extruder.get_trapq())
    feeder._stepper_synced_to = "extruder"
    feeder._commanded_pos = 42.0
    feeder._last_move_end_time = 0.0
    flush_calls_before = toolhead.flush_calls
    scan_checks_before = motion_queuing.scan_window_checks

    result = feeder._unsync_if_synced()

    assert result is True
    assert toolhead.flush_calls == flush_calls_before + 1
    assert feeder.stepper.position == (0.0, 0.0, 0.0)
    assert feeder.stepper.last_trapq_set is feeder.trapq
    assert motion_queuing.scan_window_checks == scan_checks_before + 1
    assert feeder._stepper_synced_to is None
    assert feeder._commanded_pos == 0.0
    assert feeder._last_move_end_time >= feeder.lead_time


@pytest.mark.parametrize(
    ("method_name", "label"),
    [
        ("cmd_BUFFER_HALT", "BUFFER_HALT"),
        ("cmd_BUFFER_AUTO_OFF", "BUFFER_AUTO_OFF"),
        ("cmd_STOP_BUFFER_FILL", "STOP_BUFFER_FILL"),
    ],
)
def test_cleanup_commands_call_unsync_if_synced(monkeypatch, method_name, label):
    _, feeder = make_feeder()
    calls = []

    monkeypatch.setattr(
        feeder,
        "_unsync_if_synced",
        lambda: calls.append(label) or False,
    )
    monkeypatch.setattr(feeder, "_halt_motion", lambda: None)
    monkeypatch.setattr(feeder, "_set_state", lambda state: None)
    monkeypatch.setattr(
        feeder,
        "_try_restore_gcode_state",
        lambda from_command=False: None,
    )
    monkeypatch.setattr(feeder, "_clear_recovery_flags", lambda: None)

    getattr(feeder, method_name)(None)

    assert calls == [label]
