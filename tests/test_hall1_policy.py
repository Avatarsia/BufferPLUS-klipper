import pytest

from fakes_klipper import FakeConfig, FakePrinter
from klipper_extras import buffer_feeder


def make_feeder():
    printer = FakePrinter()
    config = FakeConfig(printer=printer)
    return buffer_feeder.BufferFeeder(config)


def configure_hall1_context(
    feeder,
    *,
    hall_active=True,
    state=buffer_feeder.STATE_IDLE,
    load_phase3_overflow_ok=False,
    synced_to=None,
):
    feeder._state = state
    feeder._load_phase3_overflow_ok = load_phase3_overflow_ok
    feeder._stepper_synced_to = synced_to
    feeder._pin_stable_state["hall_overflow"] = not hall_active


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ({"hall_active": False}, False),
        (
            {
                "hall_active": True,
                "state": buffer_feeder.STATE_LOAD_PHASE_3,
                "load_phase3_overflow_ok": True,
            },
            False,
        ),
        ({"hall_active": True, "state": buffer_feeder.STATE_UNLOAD_PHASE_3}, False),
        ({"hall_active": True, "state": buffer_feeder.STATE_MANUAL_RETRACT}, False),
        ({"hall_active": True, "synced_to": "extruder"}, False),
        ({"hall_active": True}, True),
    ],
    ids=[
        "inactive",
        "phase3_overflow_ok",
        "unload_phase_3",
        "manual_retract",
        "synced_to_extruder",
        "base_case",
    ],
)
def test_is_hall1_active_sensor_callback(case, expected):
    feeder = make_feeder()

    configure_hall1_context(feeder, **case)

    assert feeder._is_hall1_active("sensor_callback") is expected


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ({"hall_active": False}, False),
        ({"hall_active": True, "state": buffer_feeder.STATE_OVERFLOW}, False),
        ({"hall_active": True, "state": buffer_feeder.STATE_MANUAL_RETRACT}, False),
        ({"hall_active": True, "state": buffer_feeder.STATE_UNLOAD_PHASE_3}, False),
        (
            {
                "hall_active": True,
                "state": buffer_feeder.STATE_LOAD_PHASE_3,
                "load_phase3_overflow_ok": True,
            },
            False,
        ),
        ({"hall_active": True, "synced_to": "extruder"}, False),
        ({"hall_active": True}, True),
    ],
    ids=[
        "inactive",
        "already_overflow",
        "manual_retract",
        "unload_phase_3",
        "phase3_overflow_ok",
        "synced_to_extruder",
        "base_case",
    ],
)
def test_is_hall1_active_main_tick(case, expected):
    feeder = make_feeder()

    configure_hall1_context(feeder, **case)

    assert feeder._is_hall1_active("main_tick") is expected


@pytest.mark.parametrize(
    ("context", "case", "expected"),
    [
        ("submit_move", {"hall_active": False}, False),
        (
            "submit_move",
            {
                "hall_active": True,
                "state": buffer_feeder.STATE_LOAD_PHASE_3,
                "load_phase3_overflow_ok": True,
            },
            False,
        ),
        ("submit_move", {"hall_active": True}, True),
        ("auto_on", {"hall_active": False}, False),
        ("auto_on", {"hall_active": True}, True),
        ("phase3_entry", {"hall_active": False}, False),
        ("phase3_entry", {"hall_active": True}, True),
    ],
    ids=[
        "submit_move_inactive",
        "submit_move_phase3_overflow_ok",
        "submit_move_active",
        "auto_on_inactive",
        "auto_on_active",
        "phase3_entry_inactive",
        "phase3_entry_active",
    ],
)
def test_is_hall1_active_other_contexts(context, case, expected):
    feeder = make_feeder()

    configure_hall1_context(feeder, **case)

    assert feeder._is_hall1_active(context) is expected


def test_is_hall1_active_rejects_unknown_context():
    feeder = make_feeder()
    configure_hall1_context(feeder, hall_active=True)

    with pytest.raises(ValueError, match="Unknown HALL1 context"):
        feeder._is_hall1_active("unknown_context")
