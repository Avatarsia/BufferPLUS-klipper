"""Sync / unsync / halt-order tests for BufferFeeder.

Migrated from test_sync_coordinator.py, test_unsync.py, test_halt_order.py (2026-05-12).
"""

import pytest

from fakes_klipper import FakeConfig, FakePrinter
from klipper_extras import buffer_feeder


def make_feeder(values=None):
    printer = FakePrinter()
    config = FakeConfig(printer=printer, values=values)
    feeder = buffer_feeder.BufferFeeder(config)
    return printer, feeder


def set_sensor_active(feeder, sensor_name, active):
    polarity_flip = feeder._pin_polarity_flip[sensor_name]
    feeder._pin_stable_state[sensor_name] = (not active) if polarity_flip else active


# --- SYNC: _sync_to_extruder + _anchor_step ---

def test_sync_to_extruder_flushes_rebinds_trapq_then_enables_synced_stepper(monkeypatch):
    # The live sync path flushes, zeros position, binds the extruder trapq,
    # recomputes scan windows, then enables/responds with the synced flag set.
    printer, feeder = make_feeder()
    toolhead = printer.lookup_object("toolhead")
    motion_queuing = printer.lookup_object("motion_queuing")
    extruder = printer.lookup_object("extruder")
    events = []

    original_flush = toolhead.flush_step_generation
    original_set_position = feeder.stepper.set_position
    original_set_trapq = feeder.stepper.set_trapq
    original_scan = motion_queuing.check_step_generation_scan_windows

    def wrapped_flush():
        events.append("flush_step_generation")
        return original_flush()

    def wrapped_set_position(position):
        events.append(("set_position", position))
        return original_set_position(position)

    def wrapped_set_trapq(trapq):
        events.append(("set_trapq", trapq))
        return original_set_trapq(trapq)

    def wrapped_scan():
        events.append("check_step_generation_scan_windows")
        return original_scan()

    monkeypatch.setattr(toolhead, "flush_step_generation", wrapped_flush)
    monkeypatch.setattr(feeder.stepper, "set_position", wrapped_set_position)
    monkeypatch.setattr(feeder.stepper, "set_trapq", wrapped_set_trapq)
    monkeypatch.setattr(
        motion_queuing,
        "check_step_generation_scan_windows",
        wrapped_scan,
    )
    monkeypatch.setattr(
        feeder,
        "_enable_stepper",
        lambda: events.append(
            ("enable_stepper", feeder._stepper_synced_to, feeder._stepcompress_primed)
        ),
    )
    monkeypatch.setattr(
        feeder,
        "_respond",
        lambda message: events.append(("respond", message)),
    )

    feeder._sync_to_extruder("extruder")

    assert events == [
        "flush_step_generation",
        ("set_position", (0.0, 0.0, 0.0)),
        ("set_trapq", extruder.get_trapq()),
        "check_step_generation_scan_windows",
        ("enable_stepper", "extruder", True),
        ("respond", "Buffer-Feeder synced to 'extruder' — follows extruder moves"),
    ]
    assert feeder.stepper.last_trapq_set is extruder.get_trapq()
    assert feeder._stepper_synced_to == "extruder"
    assert feeder._stepcompress_primed is True


def test_anchor_step_without_overflow_uses_forward_boot_move(monkeypatch):
    # With HALL1 inactive, the anchor step nudges forward by 0.05mm and
    # waits using direction=+1 before reporting the feed variant.
    _, feeder = make_feeder()
    set_sensor_active(feeder, "hall_overflow", False)
    events = []

    monkeypatch.setattr(feeder, "_enable_stepper", lambda: events.append("enable_stepper"))
    monkeypatch.setattr(
        feeder,
        "_submit_move",
        lambda distance, speed: events.append(("submit_move", distance, speed)),
    )
    monkeypatch.setattr(
        feeder,
        "_wait_for_move_done",
        lambda gcmd=None, direction=+1, allow_overflow=False: events.append(
            ("wait_for_move_done", direction)
        ),
    )
    monkeypatch.setattr(
        feeder,
        "_respond",
        lambda message: events.append(("respond", message)),
    )

    feeder._anchor_step()

    assert events == [
        "enable_stepper",
        ("submit_move", 0.05, 10.0),
        ("wait_for_move_done", 1),
        ("respond", "Stepcompress anchor primed (boot feed 0.05mm)"),
    ]


def test_anchor_step_with_overflow_uses_retract_boot_move(monkeypatch):
    # With HALL1 active, the same anchor path flips direction and reports
    # the retract variant of the boot nudge.
    _, feeder = make_feeder()
    set_sensor_active(feeder, "hall_overflow", True)
    events = []

    monkeypatch.setattr(feeder, "_enable_stepper", lambda: events.append("enable_stepper"))
    monkeypatch.setattr(
        feeder,
        "_submit_move",
        lambda distance, speed: events.append(("submit_move", distance, speed)),
    )
    monkeypatch.setattr(
        feeder,
        "_wait_for_move_done",
        lambda gcmd=None, direction=+1, allow_overflow=False: events.append(
            ("wait_for_move_done", direction)
        ),
    )
    monkeypatch.setattr(
        feeder,
        "_respond",
        lambda message: events.append(("respond", message)),
    )

    feeder._anchor_step()

    assert events == [
        "enable_stepper",
        ("submit_move", -0.05, 10.0),
        ("wait_for_move_done", -1),
        ("respond", "Stepcompress anchor primed (boot retract 0.05mm)"),
    ]


# --- UNSYNC: _unsync_if_synced + Cleanup-Cmds ---

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


# --- HALT: cmd_BUFFER_HALT order ---

def test_cmd_buffer_halt_unsyncs_before_halting_motion(monkeypatch):
    _, feeder = make_feeder()
    feeder._state = buffer_feeder.STATE_AUTO
    calls = []
    seen = {
        "unsync_called": False,
        "halt_called": False,
    }

    def fake_unsync():
        calls.append(("unsync", seen["halt_called"]))
        seen["unsync_called"] = True
        return False

    def fake_halt_motion():
        calls.append(("halt_motion", seen["unsync_called"]))
        seen["halt_called"] = True

    monkeypatch.setattr(feeder, "_unsync_if_synced", fake_unsync)
    monkeypatch.setattr(feeder, "_halt_motion", fake_halt_motion)
    monkeypatch.setattr(feeder, "_set_state", lambda state: None)
    monkeypatch.setattr(feeder, "_respond", lambda message: None)

    feeder.cmd_BUFFER_HALT(None)

    assert calls == [
        ("unsync", False),
        ("halt_motion", True),
    ]
