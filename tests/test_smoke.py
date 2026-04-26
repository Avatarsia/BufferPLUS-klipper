from klipper_extras import buffer_feeder


def test_buffer_feeder_smoke(fake_config):
    feeder = buffer_feeder.BufferFeeder(fake_config)
    gcode = fake_config.get_printer().lookup_object("gcode")

    status = feeder.get_status(0.0)

    assert status["state"] == buffer_feeder.STATE_INIT
    assert "hall_overflow" in status

    gcode.commands["BUFFER_STATE_DUMP"]["handler"](None)

    assert gcode.info_messages
    assert gcode.info_messages[0] == "---- BUFFER STATE ----"
