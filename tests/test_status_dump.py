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


def test_get_status_exposes_important_flags():
    _, feeder = make_feeder(
        values={
            "use_python_unload": 1,
            "use_fault_overlay": True,
        }
    )
    feeder._state = buffer_feeder.STATE_AUTO
    set_sensor_active(feeder, "hall_overflow", True)
    set_sensor_active(feeder, "hall_full", True)
    set_sensor_active(feeder, "hall_empty", True)
    set_sensor_active(feeder, "entrance", True)
    feeder._jam_active = True
    feeder._fault_overflow = True
    feeder._fault_runout = True
    feeder._fault_jam = True
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
    assert status["fault_runout"] is True
    assert status["fault_jam"] is True
    assert status["use_python_unload"] == 1
    assert status["use_fault_overlay"] is True


def test_buffer_state_dump_reports_core_flags():
    printer, feeder = make_feeder(
        values={
            "use_python_unload": 1,
            "use_fault_overlay": True,
        }
    )
    gcode = printer.lookup_object("gcode")
    feeder._state = buffer_feeder.STATE_AUTO
    set_sensor_active(feeder, "hall_overflow", True)
    set_sensor_active(feeder, "hall_full", True)
    set_sensor_active(feeder, "hall_empty", True)
    set_sensor_active(feeder, "entrance", True)
    feeder._jam_active = True
    feeder._fault_overflow = True
    feeder._fault_runout = False
    feeder._fault_jam = True
    feeder._stepper_synced_to = "extruder"

    feeder.cmd_BUFFER_STATE_DUMP(None)
    dump = "\n".join(gcode.info_messages)

    assert "state              = AUTO" in dump
    assert "hall_empty (HALL3) = True" in dump
    assert "hall_full  (HALL2) = True" in dump
    assert "hall_overflow(HALL1)= True" in dump
    assert "entrance_detected  = True" in dump
    assert "jam_active         = True" in dump
    assert "overlay flags     = overflow=True runout=False jam=True (use=True)" in dump
    assert "synced_to_extruder = extruder" in dump
    # TODO: BUFFER_STATE_DUMP does not print use_python_unload yet, so
    # that flag is asserted via get_status() only.
