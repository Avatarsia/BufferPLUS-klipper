"""Issue 68: physical button gestures are mutually exclusive."""

import pytest

from klipper_extras import buffer_feeder
from helpers import set_sensor_active


BUTTON_PIN = {
    buffer_feeder.BUTTON_FEED: "fake:feed_button",
    buffer_feeder.BUTTON_RETRACT: "fake:retract_button",
}


def _edge(printer, feeder, button, pressed, eventtime):
    buttons = printer.lookup_object("buttons")
    buttons._next_eventtime = eventtime
    buttons.trigger_pin(BUTTON_PIN[button], pressed)
    feeder._check_debounce(eventtime + 0.011)


def _click(printer, feeder, button, start):
    _edge(printer, feeder, button, True, start)
    _edge(printer, feeder, button, False, start + 0.100)


def _fire_reusable_timer(timer, eventtime):
    """Klipper-faithful fire: NEVER disarms but retains timer handle."""
    timer["when"] = timer["callback"](eventtime)


@pytest.fixture
def gesture_feeder(feeder_factory, monkeypatch):
    printer, feeder = feeder_factory(grace_done=True)
    feeder._state = buffer_feeder.STATE_IDLE
    feeder._halt_requested = False
    set_sensor_active(feeder, "hall_overflow", False)
    set_sensor_active(feeder, "hall_full", False)
    set_sensor_active(feeder, "hall_empty", False)
    events = []
    monkeypatch.setattr(feeder, "_action_manual_start",
                        lambda button: events.append(("hold", button)))
    monkeypatch.setattr(feeder, "_action_manual_pulse",
                        lambda button: events.append(("pulse", button)))
    monkeypatch.setattr(feeder, "_action_burst",
                        lambda button: events.append(("burst", button)))
    return printer, feeder, events


def test_40ms_release_is_debounced_separately_from_hall_sensors(gesture_feeder):
    printer, feeder, events = gesture_feeder
    _edge(printer, feeder, buffer_feeder.BUTTON_FEED, True, 1.000)
    _edge(printer, feeder, buffer_feeder.BUTTON_FEED, False, 1.040)

    assert feeder.feed_button_pressed is False
    assert feeder._click_count[buffer_feeder.BUTTON_FEED] == 1
    assert events == []


def test_hold_starts_only_after_350ms_and_matching_release_stops(gesture_feeder,
                                                                  monkeypatch):
    printer, feeder, events = gesture_feeder
    stops = []
    monkeypatch.setattr(feeder, "_halt_motion", lambda: stops.append("stop"))
    _edge(printer, feeder, buffer_feeder.BUTTON_RETRACT, True, 2.000)
    feeder._check_debounce(2.349)
    assert events == []

    feeder._check_debounce(2.361)
    assert events == [("hold", buffer_feeder.BUTTON_RETRACT)]
    feeder._continuous_feed = True
    feeder._continuous_feed_direction = -1
    feeder._state = buffer_feeder.STATE_MANUAL_RETRACT
    _edge(printer, feeder, buffer_feeder.BUTTON_RETRACT, False, 2.500)
    assert stops == ["stop"]
    assert feeder._click_count[buffer_feeder.BUTTON_RETRACT] == 0


def test_double_waits_for_window_and_triple_has_no_intermediate_motion(
        gesture_feeder):
    printer, feeder, events = gesture_feeder
    _click(printer, feeder, buffer_feeder.BUTTON_RETRACT, 3.000)
    _click(printer, feeder, buffer_feeder.BUTTON_RETRACT, 3.140)
    assert events == []

    _click(printer, feeder, buffer_feeder.BUTTON_RETRACT, 3.280)
    assert events == [("burst", buffer_feeder.BUTTON_RETRACT)]


def test_double_dispatches_once_after_window_and_timer_rearms(
        gesture_feeder):
    printer, feeder, events = gesture_feeder
    for base in (4.000, 7.000):
        _click(printer, feeder, buffer_feeder.BUTTON_FEED, base)
        _click(printer, feeder, buffer_feeder.BUTTON_FEED, base + 0.140)
        timer = feeder._click_settle_timer[buffer_feeder.BUTTON_FEED]
        _fire_reusable_timer(timer, base + 1.800)

    assert events == [
        ("pulse", buffer_feeder.BUTTON_FEED),
        ("pulse", buffer_feeder.BUTTON_FEED),
    ]


def test_hold_on_followup_press_cancels_pending_clicks(gesture_feeder):
    printer, feeder, events = gesture_feeder
    _click(printer, feeder, buffer_feeder.BUTTON_FEED, 10.000)
    _edge(printer, feeder, buffer_feeder.BUTTON_FEED, True, 10.140)
    feeder._check_debounce(10.511)

    timer = feeder._click_settle_timer[buffer_feeder.BUTTON_FEED]
    _fire_reusable_timer(timer, 12.000)
    assert events == [("hold", buffer_feeder.BUTTON_FEED)]


def test_disabled_feed_triple_reports_without_fallback_motion(
        feeder_factory, monkeypatch):
    printer, feeder = feeder_factory(
        values={"feed_burst_enabled": False}, grace_done=True)
    feeder._state = buffer_feeder.STATE_IDLE
    set_sensor_active(feeder, "hall_overflow", False)
    events = []
    monkeypatch.setattr(feeder, "_action_manual_start",
                        lambda button: events.append(("hold", button)))
    monkeypatch.setattr(feeder, "_action_manual_pulse",
                        lambda button: events.append(("pulse", button)))
    monkeypatch.setattr(feeder, "_action_burst",
                        lambda button: events.append(("burst", button)))

    for base in (13.000, 13.140, 13.280):
        _click(printer, feeder, buffer_feeder.BUTTON_FEED, base)

    timer = feeder._click_settle_timer[buffer_feeder.BUTTON_FEED]
    _fire_reusable_timer(timer, 15.000)
    assert events == []
    assert "disabled" in printer.lookup_object("gcode").info_messages[-1]


def test_delayed_double_is_discarded_after_jam_and_hall_lockout(
        gesture_feeder):
    printer, feeder, events = gesture_feeder
    _click(printer, feeder, buffer_feeder.BUTTON_FEED, 16.000)
    _click(printer, feeder, buffer_feeder.BUTTON_FEED, 16.140)
    feeder._state = buffer_feeder.STATE_JAM
    set_sensor_active(feeder, "hall_overflow", True)

    timer = feeder._click_settle_timer[buffer_feeder.BUTTON_FEED]
    _fire_reusable_timer(timer, 18.000)
    assert events == []


@pytest.mark.parametrize("invalidator", ["halt", "sync", "workflow", "state"])
def test_delayed_double_is_discarded_when_context_changes(
        gesture_feeder, invalidator):
    printer, feeder, events = gesture_feeder
    _click(printer, feeder, buffer_feeder.BUTTON_RETRACT, 18.200)
    _click(printer, feeder, buffer_feeder.BUTTON_RETRACT, 18.340)
    if invalidator == "halt":
        feeder._halt_requested = True
    elif invalidator == "sync":
        feeder._stepper_synced_to = "extruder"
    elif invalidator == "workflow":
        feeder._measure_load_active = True
    else:
        feeder._state = buffer_feeder.STATE_AUTO

    timer = feeder._click_settle_timer[buffer_feeder.BUTTON_RETRACT]
    _fire_reusable_timer(timer, 20.000)
    assert events == []


def test_halt_motion_cancels_pending_double(gesture_feeder):
    printer, feeder, events = gesture_feeder
    _click(printer, feeder, buffer_feeder.BUTTON_FEED, 20.100)
    _click(printer, feeder, buffer_feeder.BUTTON_FEED, 20.240)
    timer = feeder._click_settle_timer[buffer_feeder.BUTTON_FEED]

    feeder._halt_motion()
    _fire_reusable_timer(timer, 22.000)
    assert events == []


def test_hall1_blocks_feed_but_allows_retract_hold(gesture_feeder):
    printer, feeder, events = gesture_feeder
    set_sensor_active(feeder, "hall_overflow", True)

    _edge(printer, feeder, buffer_feeder.BUTTON_FEED, True, 20.200)
    feeder._check_debounce(20.600)
    _edge(printer, feeder, buffer_feeder.BUTTON_RETRACT, True, 20.700)
    feeder._check_debounce(21.061)

    assert events == [("hold", buffer_feeder.BUTTON_RETRACT)]


def test_opposite_button_release_does_not_stop_active_direction(
        gesture_feeder, monkeypatch):
    printer, feeder, _ = gesture_feeder
    stops = []
    monkeypatch.setattr(feeder, "_halt_motion", lambda: stops.append("stop"))
    _edge(printer, feeder, buffer_feeder.BUTTON_RETRACT, True, 19.000)
    feeder._continuous_feed = True
    feeder._continuous_feed_direction = 1

    _edge(printer, feeder, buffer_feeder.BUTTON_RETRACT, False, 19.100)
    assert stops == []


def test_button_bounce_under_10ms_does_not_create_click(gesture_feeder):
    printer, feeder, events = gesture_feeder
    buttons = printer.lookup_object("buttons")
    buttons._next_eventtime = 20.000
    buttons.trigger_pin("fake:feed_button", True)
    buttons._next_eventtime = 20.006
    buttons.trigger_pin("fake:feed_button", False)
    feeder._check_debounce(20.020)

    assert feeder._click_count[buffer_feeder.BUTTON_FEED] == 0
    assert events == []


def _physical_click_20ms(printer, feeder, button, start):
    buttons = printer.lookup_object("buttons")
    buttons._next_eventtime = start
    buttons.trigger_pin(BUTTON_PIN[button], True)
    for tick in (start + 0.020, start + 0.040, start + 0.060,
                 start + 0.080, start + 0.100):
        feeder.reactor.now = tick
        feeder._main_tick(tick)
    buttons._next_eventtime = start + 0.100
    buttons.trigger_pin(BUTTON_PIN[button], False)
    feeder.reactor.now = start + 0.120
    feeder._main_tick(start + 0.120)


def test_real_20ms_double_queues_only_pulse_after_settle(feeder_factory):
    printer, feeder = feeder_factory(grace_done=True, fire_connect=True)
    feeder._state = buffer_feeder.STATE_IDLE
    feeder._halt_requested = False
    set_sensor_active(feeder, "hall_overflow", False)
    motion_q = printer.lookup_object("motion_queuing")

    _physical_click_20ms(printer, feeder, buffer_feeder.BUTTON_RETRACT, 1.000)
    _physical_click_20ms(printer, feeder, buffer_feeder.BUTTON_RETRACT, 1.140)
    assert [c for c in motion_q.append_calls if c[0] is feeder.trapq] == []

    timer = feeder._click_settle_timer[buffer_feeder.BUTTON_RETRACT]
    _fire_reusable_timer(timer, 2.800)
    own = [c for c in motion_q.append_calls if c[0] is feeder.trapq]
    assert len(own) == 1
    assert feeder._current_move["direction"] == -1
    assert feeder._current_move["distance"] == feeder.manual_chunk_distance
    assert feeder._state == buffer_feeder.STATE_MANUAL_RETRACT


def test_real_20ms_triple_queues_only_burst_after_third_release(feeder_factory):
    printer, feeder = feeder_factory(
        values={"feed_burst_enabled": True}, grace_done=True,
        fire_connect=True)
    feeder._state = buffer_feeder.STATE_IDLE
    feeder._halt_requested = False
    set_sensor_active(feeder, "hall_overflow", False)
    motion_q = printer.lookup_object("motion_queuing")

    _physical_click_20ms(printer, feeder, buffer_feeder.BUTTON_FEED, 1.000)
    _physical_click_20ms(printer, feeder, buffer_feeder.BUTTON_FEED, 1.140)
    assert [c for c in motion_q.append_calls if c[0] is feeder.trapq] == []
    _physical_click_20ms(printer, feeder, buffer_feeder.BUTTON_FEED, 1.280)

    own = [c for c in motion_q.append_calls if c[0] is feeder.trapq]
    assert len(own) == 1
    assert feeder._current_move["direction"] == 1
    assert (feeder._current_move["distance"]
            + feeder._pending_remaining_mm) == feeder.burst_distance
    assert feeder._state == buffer_feeder.STATE_MANUAL_FEED


def test_measure_load_keeps_press_to_start_and_press_to_stop(
        gesture_feeder, monkeypatch):
    printer, feeder, events = gesture_feeder
    feeder._measure_load_active = True
    feeder._measure_feeding = False
    measure_events = []
    monkeypatch.setattr(feeder, "_submit_move",
                        lambda distance, speed: measure_events.append("start"))
    monkeypatch.setattr(feeder, "_halt_motion",
                        lambda: measure_events.append("stop"))
    monkeypatch.setattr(feeder, "_measure_report",
                        lambda: measure_events.append("report"))

    _edge(printer, feeder, buffer_feeder.BUTTON_FEED, True, 50.000)
    _edge(printer, feeder, buffer_feeder.BUTTON_FEED, False, 50.100)
    _edge(printer, feeder, buffer_feeder.BUTTON_FEED, True, 50.200)

    assert measure_events[0] == "start"
    assert "stop" in measure_events
    assert "report" in measure_events
    assert events == []
    assert feeder._measure_load_active is False


@pytest.mark.parametrize("gesture", ["double", "triple"])
def test_opposite_gesture_stops_hold_before_dispatch_and_old_release_is_safe(
        feeder_factory, monkeypatch, gesture):
    printer, feeder = feeder_factory(
        values={"feed_burst_enabled": True}, grace_done=True,
        fire_connect=True)
    feeder._state = buffer_feeder.STATE_IDLE
    feeder._halt_requested = False
    set_sensor_active(feeder, "hall_overflow", False)
    set_sensor_active(feeder, "hall_full", False)
    set_sensor_active(feeder, "hall_empty", False)

    _edge(printer, feeder, buffer_feeder.BUTTON_FEED, True, 60.000)
    feeder._check_debounce(60.371)
    assert feeder._continuous_feed is True
    assert feeder._continuous_feed_direction == 1
    submitted = []
    original_submit = feeder._submit_move

    def record_submit(distance, speed, *args, **kwargs):
        submitted.append(distance)
        return original_submit(distance, speed, *args, **kwargs)

    monkeypatch.setattr(feeder, "_submit_move", record_submit)

    _physical_click_20ms(
        printer, feeder, buffer_feeder.BUTTON_RETRACT, 60.500)
    _physical_click_20ms(
        printer, feeder, buffer_feeder.BUTTON_RETRACT, 60.640)
    if gesture == "triple":
        _physical_click_20ms(
            printer, feeder, buffer_feeder.BUTTON_RETRACT, 60.780)
    else:
        timer = feeder._click_settle_timer[buffer_feeder.BUTTON_RETRACT]
        _fire_reusable_timer(timer, 62.300)

    expected_distance = (feeder.manual_chunk_distance if gesture == "double"
                         else feeder.burst_distance)
    assert feeder._continuous_feed is False
    assert feeder._current_move["direction"] == -1
    assert submitted[-1] == -expected_distance
    pending_after_dispatch = feeder._pending_remaining_mm

    _edge(printer, feeder, buffer_feeder.BUTTON_FEED, False, 62.500)
    assert feeder._current_move["direction"] == -1
    assert feeder._pending_remaining_mm == pending_after_dispatch
