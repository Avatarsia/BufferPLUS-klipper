"""Regression tests for stale _continuous_feed after normal print end."""

from fakes_klipper import FakeConfig, FakePrinter, FakePrintStats
from klipper_extras import buffer_feeder


def set_sensor_active(feeder, sensor_name, active):
    polarity_flip = feeder._pin_polarity_flip[sensor_name]
    raw = (not active) if polarity_flip else active
    feeder._pin_stable_state[sensor_name] = raw
    feeder._pin_raw_state[sensor_name] = raw


def make_print_ended_feeder(print_state='complete'):
    printer = FakePrinter()
    printer.objects["print_stats"] = FakePrintStats(state=print_state)
    config = FakeConfig(
        printer=printer,
        values={"use_flush_callback_bang_bang": True},
    )
    feeder = buffer_feeder.BufferFeeder(config)
    printer.fire_event('klippy:connect')
    feeder._startup_grace_done = True
    feeder._state = buffer_feeder.STATE_AUTO
    set_sensor_active(feeder, 'hall_overflow', False)
    set_sensor_active(feeder, 'hall_full', False)
    set_sensor_active(feeder, 'hall_empty', False)
    return printer, feeder


def test_continuous_feed_resets_on_print_ended_normally():
    _, feeder = make_print_ended_feeder(print_state='complete')
    feeder._print_running = True
    feeder._continuous_feed = True
    feeder._continuous_feed_direction = 1

    feeder._on_idle_ready()

    assert feeder._continuous_feed is False


def test_continuous_feed_direction_resets_on_print_ended_normally():
    _, feeder = make_print_ended_feeder(print_state='complete')
    feeder._print_running = True
    feeder._continuous_feed = True
    feeder._continuous_feed_direction = 1

    feeder._on_idle_ready()

    assert feeder._continuous_feed_direction == 0


def test_continuous_feed_resets_on_standby_state():
    _, feeder = make_print_ended_feeder(print_state='standby')
    feeder._print_running = True
    feeder._continuous_feed = True
    feeder._continuous_feed_direction = 1

    feeder._on_idle_ready()

    assert feeder._continuous_feed is False
    assert feeder._continuous_feed_direction == 0


def test_continuous_feed_still_resets_on_pause_unchanged():
    _, feeder = make_print_ended_feeder(print_state='paused')
    feeder._print_running = True
    feeder._continuous_feed = True
    feeder._continuous_feed_direction = 1

    feeder._on_idle_ready()

    assert feeder._continuous_feed is False
    assert feeder._bang_bang_suspended is True


def test_no_false_positive_jam_supply_after_print_end_then_idle():
    _, feeder = make_print_ended_feeder(print_state='complete')
    feeder._print_running = True
    feeder._continuous_feed = True
    feeder._continuous_feed_direction = 1
    set_sensor_active(feeder, 'hall_empty', True)

    feeder._on_idle_ready()

    assert feeder._continuous_feed is False
    feeder._print_running = True
    feeder_running_fwd = (
        feeder._continuous_feed and feeder._continuous_feed_direction == 1
    )
    assert feeder_running_fwd is False
