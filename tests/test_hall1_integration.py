import pytest

from klipper_extras import buffer_feeder


class FakeGCmdLocal:
    def __init__(self, values=None):
        self.values = {key.upper(): value for key, value in (values or {}).items()}

    def get(self, key, default=None):
        return self.values.get(key.upper(), default)

    def get_int(self, key, default=None, **kwargs):
        return int(self.values.get(key.upper(), default))

    def get_float(self, key, default=None, **kwargs):
        return float(self.values.get(key.upper(), default))


def set_sensor_active(feeder, sensor_name, active):
    polarity_flip = feeder._pin_polarity_flip[sensor_name]
    feeder._pin_stable_state[sensor_name] = (not active) if polarity_flip else active


def test_check_debounce_dispatch_enters_overflow_when_hall1_stabilizes(fake_printer, feeder, monkeypatch):
    # The debounce caller promotes a stable HALL1 edge and dispatches into
    # the sensor_callback overflow path when no bypass applies.
    buttons = fake_printer.lookup_object("buttons")
    feeder._startup_grace_done = True
    feeder._state = buffer_feeder.STATE_IDLE
    feeder._pin_raw_state["hall_overflow"] = True
    feeder._pin_stable_state["hall_overflow"] = True
    events = []

    monkeypatch.setattr(feeder, "_enter_overflow", lambda: events.append("enter_overflow"))
    monkeypatch.setattr(feeder, "_exit_overflow", lambda: events.append("exit_overflow"))

    eventtime = buttons.trigger_pin("fake:hall_overflow", 0)
    feeder._check_debounce(eventtime + feeder.hall_debounce_ms / 1000.0)

    assert events == ["enter_overflow"]
    assert feeder.hall_overflow is True


def test_main_tick_enters_overflow_when_hall1_is_active(feeder, monkeypatch):
    # The main tick checks HALL1 before other work and immediately routes
    # through the overflow path when the active state is not bypassed.
    feeder._startup_grace_done = True
    feeder._state = buffer_feeder.STATE_IDLE
    set_sensor_active(feeder, "hall_overflow", True)
    events = []

    monkeypatch.setattr(
        feeder,
        "_check_debounce",
        lambda eventtime: events.append(("check_debounce", eventtime)),
    )
    monkeypatch.setattr(feeder, "_enter_overflow", lambda: events.append("enter_overflow"))

    result = feeder._main_tick(1.25)

    assert result == 1.25 + buffer_feeder.MAIN_TICK_INTERVAL
    assert events == [("check_debounce", 1.25), "enter_overflow"]


def test_submit_move_rejects_forward_motion_when_hall1_is_active(feeder, monkeypatch):
    # The submit_move caller site rejects positive-distance motion under
    # active HALL1 and clears pending streaming state instead of queuing.
    feeder._state = buffer_feeder.STATE_IDLE
    feeder._continuous_feed = True
    feeder._pending_remaining_mm = 123.0
    set_sensor_active(feeder, "hall_overflow", True)
    submitted = []

    monkeypatch.setattr(
        feeder,
        "_submit_single_trapezoid",
        lambda distance, speed: submitted.append((distance, speed)),
    )

    feeder._submit_move(10.0, 5.0)

    assert submitted == []
    assert feeder._continuous_feed is False
    assert feeder._pending_remaining_mm == 0.0


def test_check_auto_ready_returns_hall1_overflow_reason(feeder):
    # The auto-ready helper surfaces the HALL1 lockout reason by calling
    # the real auto_on context check.
    feeder._state = buffer_feeder.STATE_IDLE
    set_sensor_active(feeder, "hall_overflow", True)

    reason = feeder._check_auto_ready()

    assert reason == "HALL1 overflow active"


def test_cmd_buffer_auto_on_raises_when_hall1_is_active(feeder, monkeypatch):
    # The AUTO_ON command aborts before enabling or changing state when
    # the underlying auto_on context sees HALL1 active.
    feeder._state = buffer_feeder.STATE_IDLE
    set_sensor_active(feeder, "hall_overflow", True)
    events = []

    monkeypatch.setattr(feeder, "_enable_stepper", lambda: events.append("enable_stepper"))
    monkeypatch.setattr(
        feeder,
        "_set_state",
        lambda state: events.append(("set_state", state)),
    )

    with pytest.raises(RuntimeError, match="Cannot enable AUTO while HALL1 overflow active"):
        feeder.cmd_BUFFER_AUTO_ON(None)

    assert events == []


def test_cmd_buffer_load_phase3_rejects_entry_when_hall1_is_active(feeder, monkeypatch):
    # Phase 3 without OVERFLOW_OK hits the phase3_entry lockout before any
    # phase setup work runs.
    feeder._state = buffer_feeder.STATE_IDLE
    set_sensor_active(feeder, "hall_overflow", True)
    events = []

    monkeypatch.setattr(
        feeder,
        "_check_phase_entry",
        lambda cmd_name, allowed_states: events.append(("check_phase_entry", cmd_name, allowed_states)),
    )

    with pytest.raises(RuntimeError, match="HALL1 OVERFLOW active"):
        feeder.cmd_BUFFER_LOAD_PHASE3(FakeGCmdLocal())

    assert events == []


def test_cmd_buffer_load_phase3_with_overflow_ok_skips_hall1_entry_lockout(feeder, monkeypatch):
    # With OVERFLOW_OK=1, the command skips the phase3_entry HALL1 check and
    # proceeds into its normal setup path even while HALL1 stays active.
    feeder._state = buffer_feeder.STATE_IDLE
    set_sensor_active(feeder, "hall_overflow", True)
    events = []

    monkeypatch.setattr(
        feeder,
        "_check_phase_entry",
        lambda cmd_name, allowed_states: events.append(("check_phase_entry", cmd_name, allowed_states)),
    )
    monkeypatch.setattr(
        feeder,
        "_wait_for_move_done",
        lambda gcmd=None, direction=+1, allow_overflow=False: events.append(
            ("wait_for_move_done", allow_overflow)
        ),
    )
    monkeypatch.setattr(feeder, "_enable_stepper", lambda: events.append("enable_stepper"))
    monkeypatch.setattr(
        feeder,
        "_set_state",
        lambda state: events.append(("set_state", state)),
    )
    monkeypatch.setattr(
        feeder,
        "_start_continuous_motion",
        lambda direction, speed, timeout: events.append(
            ("start_continuous_motion", direction, speed, timeout)
        ),
    )

    feeder.cmd_BUFFER_LOAD_PHASE3(FakeGCmdLocal({"OVERFLOW_OK": 1}))

    assert events == [
        (
            "check_phase_entry",
            "LOAD_PHASE3",
            {
                buffer_feeder.STATE_IDLE,
                buffer_feeder.STATE_AUTO,
                buffer_feeder.STATE_RUNOUT,
                buffer_feeder.STATE_LOADING_PUSH,
                buffer_feeder.STATE_OVERFLOW,
            },
        ),
        ("wait_for_move_done", True),
        "enable_stepper",
        ("set_state", buffer_feeder.STATE_LOADING_PUSH),
        ("start_continuous_motion", 1, feeder.feed_speed, feeder.max_feed_time),
    ]
    assert feeder._load_phase3_overflow_ok is True
