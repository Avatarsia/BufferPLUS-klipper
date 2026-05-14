import logging

from fakes_klipper import FakeConfig, FakePrinter
from helpers import set_sensor_active
from klipper_extras import buffer_feeder


def make_feeder(values=None, state=None):
    printer = FakePrinter()
    config = FakeConfig(printer=printer, values=values)
    feeder = buffer_feeder.BufferFeeder(config)
    feeder._startup_grace_done = True
    feeder._state = (state if state is not None else buffer_feeder.STATE_IDLE)
    set_sensor_active(feeder, 'hall_overflow', False)
    set_sensor_active(feeder, 'hall_full', False)
    set_sensor_active(feeder, 'hall_empty', False)
    return printer, feeder


def test_debug_event_logging_disabled_by_default(caplog):
    _, feeder = make_feeder(values={"use_flush_callback_bang_bang": True})

    with caplog.at_level(logging.INFO, logger=""):
        feeder._on_mcu_flush(flush_time=5.0, step_gen_time=5.05)

    assert not [r for r in caplog.records if "buffer_event[" in r.getMessage()]


def test_debug_event_logging_emits_flush_skip_reason_when_enabled(caplog):
    _, feeder = make_feeder(
        values={"use_flush_callback_bang_bang": True, "buffer_debug_events": True},
        state=buffer_feeder.STATE_IDLE,
    )

    with caplog.at_level(logging.INFO, logger=""):
        feeder._on_mcu_flush(flush_time=5.0, step_gen_time=5.05)

    messages = [r.getMessage() for r in caplog.records]
    assert any("buffer_event[flush_skip_state]" in m for m in messages)


def test_debug_event_logging_emits_flush_submit_when_enabled(caplog):
    printer, feeder = make_feeder(
        values={"use_flush_callback_bang_bang": True, "buffer_debug_events": True},
        state=buffer_feeder.STATE_AUTO,
    )
    set_sensor_active(feeder, 'hall_empty', True)
    feeder.reactor.now = 5.0

    with caplog.at_level(logging.INFO, logger=""):
        feeder._on_mcu_flush(flush_time=5.0, step_gen_time=5.05)

    messages = [r.getMessage() for r in caplog.records]
    assert any("buffer_event[flush_submit]" in m for m in messages)
    motion_q = printer.lookup_object('motion_queuing')
    assert motion_q.append_calls
