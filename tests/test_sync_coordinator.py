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
