from fakes_klipper import FakeConfig, FakePrinter, FakePrintStats
from klipper_extras import buffer_feeder


class FakeGCmd:
    def get(self, key, default=None):
        return default

    def get_int(self, key, default=None, **kwargs):
        return default

    def get_float(self, key, default=None, **kwargs):
        return default


def set_sensor_active(feeder, sensor_name, active):
    polarity_flip = feeder._pin_polarity_flip[sensor_name]
    raw = (not active) if polarity_flip else active
    feeder._pin_stable_state[sensor_name] = raw
    feeder._pin_raw_state[sensor_name] = raw


def make_jam_feeder(print_state='printing'):
    printer = FakePrinter()
    printer.objects["print_stats"] = FakePrintStats(state=print_state)
    config = FakeConfig(
        printer=printer,
        values={"use_flush_callback_bang_bang": True},
    )
    feeder = buffer_feeder.BufferFeeder(config)
    printer.fire_event('klippy:connect')
    feeder._startup_grace_done = True
    feeder._state = buffer_feeder.STATE_JAM
    feeder._jam_active = True
    set_sensor_active(feeder, 'entrance', True)
    set_sensor_active(feeder, 'hall_overflow', False)
    set_sensor_active(feeder, 'hall_full', False)
    set_sensor_active(feeder, 'hall_empty', False)
    return printer, feeder


def test_on_idle_printing_jam_recovery_refreshes_cursor_state():
    _, feeder = make_jam_feeder()
    feeder.reactor.now = 5000.0
    feeder._last_move_end_time = 0.0
    feeder._stepcompress_primed = True
    feeder._current_move = None

    feeder._on_idle_printing()

    assert feeder._jam_active is False
    assert feeder._state == buffer_feeder.STATE_AUTO
    assert feeder._stepcompress_primed is False
    assert feeder._last_move_end_time >= 5000.0
    assert feeder._critical_action_guard_reason == 'jam_exit'


def test_on_idle_printing_jam_recovery_prevents_immediate_watchdog_anchor(
        monkeypatch):
    _, feeder = make_jam_feeder()
    feeder.reactor.now = 5000.0
    feeder._last_move_end_time = 0.0
    feeder._stepcompress_primed = True
    feeder._current_move = None

    anchor_calls = []
    monkeypatch.setattr(
        feeder.sync,
        "_submit_anchor_move",
        lambda **kwargs: anchor_calls.append(kwargs),
    )

    feeder._on_idle_printing()
    feeder._main_tick(eventtime=5000.0)

    assert anchor_calls == []


def test_clear_jam_refreshes_cursor_state_before_returning_to_auto():
    _, feeder = make_jam_feeder(print_state='paused')
    feeder.reactor.now = 6000.0
    feeder._last_move_end_time = 0.0
    feeder._stepcompress_primed = True
    feeder._current_move = None
    feeder._auto_off_by_user = False
    feeder._bang_bang_suspended = False

    feeder.cmd_BUFFER_CLEAR_JAM(FakeGCmd())

    assert feeder._jam_active is False
    assert feeder._state == buffer_feeder.STATE_AUTO
    assert feeder._stepcompress_primed is False
    assert feeder._last_move_end_time >= 6000.0
    assert feeder._critical_action_guard_reason == 'jam_exit'
