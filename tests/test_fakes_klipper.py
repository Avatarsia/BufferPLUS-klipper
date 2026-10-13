from fakes_klipper import (
    FakeButtons,
    FakeConfig,
    FakeGCode,
    FakeMotionQueuing,
    FakePrinterStepper,
    FakeReactor,
)


def test_fake_buttons_trigger_pin_calls_registered_callbacks_in_order():
    buttons = FakeButtons()
    seen = []

    def first(eventtime, raw_state):
        seen.append(("first", eventtime, raw_state))

    def second(eventtime, raw_state):
        seen.append(("second", eventtime, raw_state))

    buttons.register_buttons(["fake:hall_overflow"], first)
    buttons.register_buttons(["fake:hall_overflow", "fake:hall_full"], second)

    eventtime = buttons.trigger_pin("fake:hall_overflow", 1)

    assert buttons.callbacks_by_pin["fake:hall_overflow"] == [first, second]
    assert buttons.trigger_calls == [("fake:hall_overflow", 1, eventtime)]
    assert seen == [
        ("first", eventtime, 1),
        ("second", eventtime, 1),
    ]


def test_fake_reactor_monotonic_and_callback_registration_are_deterministic():
    reactor = FakeReactor()
    executed = []

    def callback(eventtime):
        executed.append(eventtime)

    first = reactor.monotonic()
    second = reactor.monotonic()
    registration = reactor.register_callback(callback, 5.0)

    assert reactor.NOW == 0.0
    assert first == 0.0
    assert second == 0.001
    assert registration == {"callback": callback, "when": 5.0}
    assert reactor.callback_registrations == [registration]
    assert executed == []

    assert reactor.pause(2.5) == 2.5
    assert reactor.monotonic() == 2.5


def test_fake_motion_queuing_records_movequeue_activity():
    motion_queuing = FakeMotionQueuing()

    motion_queuing.note_mcu_movequeue_activity(1.25)
    motion_queuing.note_mcu_movequeue_activity(2.5)

    assert motion_queuing.note_mcu_movequeue_activity_calls == [1.25, 2.5]
    assert motion_queuing.activity == [1.25, 2.5]


def test_fake_printer_stepper_tracks_trapq_assignments():
    stepper = FakePrinterStepper(FakeConfig())
    first_trapq = object()
    second_trapq = object()

    stepper.set_trapq(first_trapq)
    stepper.set_trapq(second_trapq)

    assert stepper.trapq_sets == [first_trapq, second_trapq]
    assert stepper.last_trapq_set is second_trapq
    assert stepper.trapq is second_trapq


def test_fake_gcode_records_script_invocations_in_order():
    gcode = FakeGCode()

    gcode.run_script("M118 hello")
    gcode.run_script_from_command("BUFFER_AUTO_ON")

    assert gcode.script_invocations == [
        ("run_script", "M118 hello"),
        ("run_script_from_command", "BUFFER_AUTO_ON"),
    ]
    assert gcode.scripts == gcode.script_invocations
