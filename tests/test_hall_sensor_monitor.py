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


def test_single_feed_click_starts_manual_feed_and_arms_click_settle_timer():
    # Current code counts clicks on button-press and single FEED click
    # enters the manual-start path, which also arms the settle timer.
    printer, feeder = make_feeder()
    feeder._state = buffer_feeder.STATE_IDLE
    set_sensor_active(feeder, "hall_overflow", False)

    feeder._on_button_change(buffer_feeder.BUTTON_FEED, True, 1.0)

    assert feeder._click_count[buffer_feeder.BUTTON_FEED] == 1
    assert feeder._state == buffer_feeder.STATE_MANUAL_FEED
    assert feeder._continuous_feed is True
    assert feeder._continuous_feed_direction == 1
    assert feeder._continuous_feed_speed == feeder.manual_speed
    assert feeder._pending_click_msg[buffer_feeder.BUTTON_FEED] == "feed: Dauerlauf"
    assert feeder._click_settle_timer[buffer_feeder.BUTTON_FEED] in printer.reactor.timers
    assert printer.reactor.update_timer_calls == [
        (
            feeder._click_settle_timer[buffer_feeder.BUTTON_FEED],
            feeder.triple_click_window,
        )
    ]


def test_click_settle_fire_responds_with_pending_message_and_clears_it():
    # The settle callback emits the deferred click summary and clears it.
    printer, feeder = make_feeder()

    feeder._set_pending_click_msg(buffer_feeder.BUTTON_FEED, "feed summary")
    result = feeder._click_settle_fire(buffer_feeder.BUTTON_FEED, 12.0)

    assert result == printer.reactor.NEVER
    assert feeder._pending_click_msg[buffer_feeder.BUTTON_FEED] is None
    assert printer.lookup_object("gcode").info_messages[-1] == "BufferFeeder: feed summary"


def test_double_feed_click_halts_then_triggers_manual_pulse(monkeypatch):
    # The second FEED click within the window halts first, then dispatches
    # to the manual-pulse branch.
    _, feeder = make_feeder()
    feeder._state = buffer_feeder.STATE_IDLE
    set_sensor_active(feeder, "hall_overflow", False)
    events = []

    monkeypatch.setattr(
        feeder,
        "_action_manual_start",
        lambda button: events.append(("manual_start", button)),
    )
    monkeypatch.setattr(
        feeder,
        "_halt_motion",
        lambda: events.append("halt_motion"),
    )
    monkeypatch.setattr(
        feeder,
        "_action_manual_pulse",
        lambda button: events.append(("manual_pulse", button)),
    )

    feeder._on_button_change(buffer_feeder.BUTTON_FEED, True, 1.0)
    feeder._on_button_change(buffer_feeder.BUTTON_FEED, True, 1.1)

    assert events == [
        ("manual_start", buffer_feeder.BUTTON_FEED),
        "halt_motion",
        ("manual_pulse", buffer_feeder.BUTTON_FEED),
    ]
    assert feeder._click_count[buffer_feeder.BUTTON_FEED] == 2


def test_triple_feed_click_with_burst_disabled_halts_then_restarts_manual_start(monkeypatch):
    # With feed_burst_enabled=0, the third FEED click halts and falls back
    # to manual-start instead of the burst branch.
    _, feeder = make_feeder(values={"feed_burst_enabled": False})
    feeder._state = buffer_feeder.STATE_IDLE
    set_sensor_active(feeder, "hall_overflow", False)
    events = []

    monkeypatch.setattr(
        feeder,
        "_action_manual_start",
        lambda button: events.append(("manual_start", button)),
    )
    monkeypatch.setattr(
        feeder,
        "_halt_motion",
        lambda: events.append("halt_motion"),
    )
    monkeypatch.setattr(
        feeder,
        "_action_manual_pulse",
        lambda button: events.append(("manual_pulse", button)),
    )
    monkeypatch.setattr(
        feeder,
        "_action_burst",
        lambda button: events.append(("burst", button)),
    )

    feeder._on_button_change(buffer_feeder.BUTTON_FEED, True, 1.0)
    feeder._on_button_change(buffer_feeder.BUTTON_FEED, True, 1.1)
    feeder._on_button_change(buffer_feeder.BUTTON_FEED, True, 1.2)

    assert events == [
        ("manual_start", buffer_feeder.BUTTON_FEED),
        "halt_motion",
        ("manual_pulse", buffer_feeder.BUTTON_FEED),
        "halt_motion",
        ("manual_start", buffer_feeder.BUTTON_FEED),
    ]
    assert feeder._click_count[buffer_feeder.BUTTON_FEED] == 0


def test_triple_feed_click_with_burst_enabled_halts_then_triggers_burst(monkeypatch):
    # With feed_burst_enabled=1, the third FEED click halts and takes the
    # burst branch instead of restarting manual-start.
    _, feeder = make_feeder(values={"feed_burst_enabled": True})
    feeder._state = buffer_feeder.STATE_IDLE
    set_sensor_active(feeder, "hall_overflow", False)
    events = []

    monkeypatch.setattr(
        feeder,
        "_action_manual_start",
        lambda button: events.append(("manual_start", button)),
    )
    monkeypatch.setattr(
        feeder,
        "_halt_motion",
        lambda: events.append("halt_motion"),
    )
    monkeypatch.setattr(
        feeder,
        "_action_manual_pulse",
        lambda button: events.append(("manual_pulse", button)),
    )
    monkeypatch.setattr(
        feeder,
        "_action_burst",
        lambda button: events.append(("burst", button)),
    )

    feeder._on_button_change(buffer_feeder.BUTTON_FEED, True, 1.0)
    feeder._on_button_change(buffer_feeder.BUTTON_FEED, True, 1.1)
    feeder._on_button_change(buffer_feeder.BUTTON_FEED, True, 1.2)

    assert events == [
        ("manual_start", buffer_feeder.BUTTON_FEED),
        "halt_motion",
        ("manual_pulse", buffer_feeder.BUTTON_FEED),
        "halt_motion",
        ("burst", buffer_feeder.BUTTON_FEED),
    ]
    assert feeder._click_count[buffer_feeder.BUTTON_FEED] == 0


def test_entrance_insert_from_idle_after_empty_edge_starts_initial_grip(monkeypatch):
    # The insert handler only auto-grips when IDLE and the feeder has seen
    # an empty entrance state first.
    _, feeder = make_feeder()
    feeder._state = buffer_feeder.STATE_IDLE
    feeder._entrance_was_empty = True
    events = []

    monkeypatch.setattr(
        feeder,
        "_start_initial_grip",
        lambda eventtime: events.append(("start_initial_grip", eventtime)),
    )

    feeder._on_entrance_insert(5.0)

    assert events == [("start_initial_grip", 5.0)]
    assert feeder._entrance_was_empty is False


def test_entrance_insert_while_auto_and_already_filled_triggers_no_extra_actions(monkeypatch):
    # When entrance filament was already present and state is AUTO, the
    # handler only emits its responses and does not start grip or scripts.
    _, feeder = make_feeder()
    feeder._state = buffer_feeder.STATE_AUTO
    feeder._entrance_was_empty = False
    responses = []
    actions = []

    monkeypatch.setattr(feeder, "_respond", lambda message: responses.append(message))
    monkeypatch.setattr(
        feeder,
        "_start_initial_grip",
        lambda eventtime: actions.append(("start_initial_grip", eventtime)),
    )
    monkeypatch.setattr(
        feeder,
        "_set_state",
        lambda state: actions.append(("set_state", state)),
    )
    monkeypatch.setattr(
        feeder,
        "_gcode_run_script",
        lambda script, from_command=False: actions.append(("script", script, from_command)),
    )

    feeder._on_entrance_insert(6.0)

    assert actions == []
    assert responses == [
        "Filament at entrance detected",
        "Entrance already had filament at boot — no auto-grip. Use FORCE_BUFFER_FILL to fill the buffer manually.",
    ]
    assert feeder._entrance_was_empty is False


def test_entrance_runout_during_print_with_runout_pause_triggers_pause_script(monkeypatch):
    # During a print with runout_pause enabled, the runout handler halts,
    # disables the stepper, enters RUNOUT, and runs the PAUSE script.
    _, feeder = make_feeder(values={"runout_pause": True})
    feeder._state = buffer_feeder.STATE_AUTO
    feeder._print_running = True
    feeder._continuous_feed = True
    events = []

    monkeypatch.setattr(feeder, "_respond", lambda message: events.append(("respond", message)))
    monkeypatch.setattr(feeder, "_halt_motion", lambda: events.append("halt_motion"))
    monkeypatch.setattr(
        feeder,
        "_schedule_stepper_disable",
        lambda: events.append("schedule_stepper_disable"),
    )
    monkeypatch.setattr(
        feeder,
        "_set_state",
        lambda state: events.append(("set_state", state)),
    )
    monkeypatch.setattr(
        feeder,
        "_schedule_gcode_script",
        lambda script: events.append(("schedule_script", script)),
    )

    feeder._on_entrance_runout(7.0)

    # P7-56b: PAUSE is deferred via _schedule_gcode_script (1ms timer)
    # so the sensor callback returns immediately.
    assert events == [
        ("respond", "Runout during print — PAUSE (runout_pause=1)"),
        "halt_motion",
        "schedule_stepper_disable",
        ("set_state", buffer_feeder.STATE_RUNOUT),
        ("schedule_script", "PAUSE"),
    ]
    assert feeder._continuous_feed is False
    assert feeder._entrance_was_empty is True
