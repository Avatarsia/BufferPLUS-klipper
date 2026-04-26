from fakes_klipper import FakeConfig, FakePrinter
from klipper_extras import buffer_feeder


def make_feeder():
    printer = FakePrinter()
    config = FakeConfig(printer=printer)
    return buffer_feeder.BufferFeeder(config)


def test_clear_recovery_flags_resets_jam_and_hall_timers():
    feeder = make_feeder()
    feeder._jam_active = True
    feeder._hall2_start_time = 12.0
    feeder._hall3_start_time = 34.0

    feeder._clear_recovery_flags()

    assert feeder._jam_active is False
    assert feeder._hall2_start_time is None
    assert feeder._hall3_start_time is None


def test_resume_after_overflow_without_interrupted_state_is_noop(monkeypatch):
    feeder = make_feeder()
    events = []
    feeder._pin_stable_state["entrance"] = False
    feeder._overflow_interrupted_state = None
    feeder._overflow_interrupted_follow = False
    feeder._overflow_resume_mm = 0.0

    monkeypatch.setattr(feeder, "_enable_stepper", lambda: events.append("enable"))
    monkeypatch.setattr(
        feeder,
        "_set_state",
        lambda state: events.append(("state", state)),
    )
    monkeypatch.setattr(
        feeder,
        "_submit_move",
        lambda distance, speed: events.append(("submit", distance, speed)),
    )

    feeder._resume_after_overflow()

    assert events == []
    assert feeder._overflow_interrupted_state is None
    assert feeder._overflow_resume_mm == 0.0
    assert feeder._grip_follow_active is False


def test_resume_after_overflow_restarts_initial_grip_follow(monkeypatch):
    feeder = make_feeder()
    events = []
    feeder._overflow_interrupted_state = buffer_feeder.STATE_INITIAL_GRIP
    feeder._overflow_interrupted_follow = True
    feeder._overflow_resume_mm = 12.5
    feeder._overflow_resume_dir = -1
    feeder._overflow_resume_spd = 7.5
    feeder._grip_follow_active = False

    monkeypatch.setattr(feeder, "_enable_stepper", lambda: events.append("enable"))
    monkeypatch.setattr(
        feeder,
        "_set_state",
        lambda state: events.append(("state", state)),
    )
    monkeypatch.setattr(
        feeder,
        "_submit_move",
        lambda distance, speed: events.append(("submit", distance, speed)),
    )

    feeder._resume_after_overflow()

    assert events == [
        "enable",
        ("state", buffer_feeder.STATE_INITIAL_GRIP),
        ("submit", -12.5, 7.5),
    ]
    assert feeder._overflow_interrupted_state is None
    assert feeder._overflow_interrupted_follow is False
    assert feeder._overflow_resume_mm == 0.0
    assert feeder._grip_follow_active is True
