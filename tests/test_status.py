"""Status / smoke tests for BufferFeeder.

Migrated from test_smoke.py + test_status_dump.py (2026-05-12).
"""

from klipper_extras import buffer_feeder
from helpers import set_sensor_active


# --- Bootstrap (from test_smoke.py) ---

def test_buffer_feeder_smoke(fake_config):
    feeder = buffer_feeder.BufferFeeder(fake_config)
    gcode = fake_config.get_printer().lookup_object("gcode")

    status = feeder.get_status(0.0)

    assert status["state"] == buffer_feeder.STATE_INIT
    assert "hall_overflow" in status
    assert "fault_overflow" in status
    assert "overflow_overlay_enabled" in status
    assert "use_fault_overlay" in status
    assert status["fault_overflow"] is False
    assert status["overflow_overlay_enabled"] is False
    assert status["use_fault_overlay"] is False

    gcode.commands["BUFFER_STATE_DUMP"]["handler"](None)

    assert gcode.info_messages
    assert gcode.info_messages[0] == "---- BUFFER STATE ----"


# --- get_status / STATE_DUMP (from test_status_dump.py) ---

def test_get_status_exposes_important_flags(feeder_factory):
    _, feeder = feeder_factory(
        values={"use_fault_overlay": True},
        grace_done=False,
    )
    feeder._state = buffer_feeder.STATE_AUTO
    set_sensor_active(feeder, "hall_overflow", True)
    set_sensor_active(feeder, "hall_full", True)
    set_sensor_active(feeder, "hall_empty", True)
    set_sensor_active(feeder, "entrance", True)
    feeder._jam_active = True
    feeder._fault_overflow = True
    feeder._stepper_synced_to = "extruder"

    status = feeder.get_status(0.0)

    assert status["state"] == buffer_feeder.STATE_AUTO
    assert status["hall_overflow"] is True
    assert status["hall_full"] is True
    assert status["hall_empty"] is True
    assert status["entrance_detected"] is True
    assert status["jam_active"] is True
    assert status["synced_to_extruder"] == "extruder"
    assert status["fault_overflow"] is True
    assert status["overflow_overlay_enabled"] is True
    assert status["use_fault_overlay"] is True


def test_buffer_state_dump_reports_core_flags(feeder_factory):
    printer, feeder = feeder_factory(
        values={"use_fault_overlay": True},
        grace_done=False,
    )
    gcode = printer.lookup_object("gcode")
    feeder._state = buffer_feeder.STATE_AUTO
    set_sensor_active(feeder, "hall_overflow", True)
    set_sensor_active(feeder, "hall_full", True)
    set_sensor_active(feeder, "hall_empty", True)
    set_sensor_active(feeder, "entrance", True)
    feeder._jam_active = True
    feeder._fault_overflow = True
    feeder._stepper_synced_to = "extruder"

    feeder.cmd_BUFFER_STATE_DUMP(None)
    dump = "\n".join(gcode.info_messages)

    assert "state              = AUTO" in dump
    assert "hall_empty (HALL3) = True" in dump
    assert "hall_full  (HALL2) = True" in dump
    assert "hall_overflow(HALL1)= True" in dump
    assert "entrance_detected  = True" in dump
    assert "jam_active         = True" in dump
    assert "overflow overlay  = active=True enabled=True" in dump
    assert "synced_to_extruder = extruder" in dump
