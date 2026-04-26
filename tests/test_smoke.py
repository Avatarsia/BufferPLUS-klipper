from klipper_extras import buffer_feeder


def test_buffer_feeder_smoke(fake_config):
    feeder = buffer_feeder.BufferFeeder(fake_config)
    gcode = fake_config.get_printer().lookup_object("gcode")

    status = feeder.get_status(0.0)

    assert status["state"] == buffer_feeder.STATE_INIT
    assert "hall_overflow" in status
    assert "fault_overflow" in status
    assert "fault_runout" in status
    assert "fault_jam" in status
    assert "use_fault_overlay" in status
    assert status["fault_overflow"] is False
    assert status["fault_runout"] is False
    assert status["fault_jam"] is False
    assert status["use_fault_overlay"] is False

    gcode.commands["BUFFER_STATE_DUMP"]["handler"](None)

    assert gcode.info_messages
    assert gcode.info_messages[0] == "---- BUFFER STATE ----"
