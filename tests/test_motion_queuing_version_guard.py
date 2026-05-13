import pytest

from fakes_klipper import FakeConfig, FakeMotionQueuing, FakePrinter
from klipper_extras import buffer_feeder


class MotionQueuingWithoutFlushCallback:
    def __init__(self):
        self.trapqs = []
        self.append_calls = []
        self.scan_window_checks = 0
        self.note_mcu_movequeue_activity_calls = []

    def allocate_trapq(self):
        trapq = object()
        self.trapqs.append(trapq)
        return trapq

    def lookup_trapq_append(self):
        def _append(*args):
            self.append_calls.append(args)
        return _append

    def check_step_generation_scan_windows(self):
        self.scan_window_checks += 1

    def note_mcu_movequeue_activity(self, end_time, is_step_gen=True):
        del is_step_gen
        self.note_mcu_movequeue_activity_calls.append(end_time)


class MotionQueuingWithoutCanAddTrapq(FakeMotionQueuing):
    def register_flush_callback(self, callback):
        self.flush_callbacks.append((callback, False))


class MotionQueuingMissingAppendLookup:
    def allocate_trapq(self):
        return object()

    def check_step_generation_scan_windows(self):
        return None

    def note_mcu_movequeue_activity(self, end_time, is_step_gen=True):
        del end_time, is_step_gen
        return None


def test_missing_motion_queuing_raises_clear_error():
    printer = FakePrinter()
    printer.objects.pop("motion_queuing")
    config = FakeConfig(printer=printer)

    with pytest.raises(RuntimeError, match="requires Klipper's motion_queuing module"):
        buffer_feeder.BufferFeeder(config)


def test_missing_motion_queuing_method_raises_clear_error():
    printer = FakePrinter()
    printer.objects["motion_queuing"] = MotionQueuingMissingAppendLookup()
    config = FakeConfig(printer=printer)

    with pytest.raises(RuntimeError, match="Missing: lookup_trapq_append"):
        buffer_feeder.BufferFeeder(config)


def test_legacy_motion_queuing_without_flush_callback_uses_legacy_path():
    printer = FakePrinter()
    printer.objects["motion_queuing"] = MotionQueuingWithoutFlushCallback()
    config = FakeConfig(printer=printer)

    feeder = buffer_feeder.BufferFeeder(config)

    assert feeder.motion_queuing is printer.objects["motion_queuing"]


def test_flush_callback_mode_requires_register_flush_callback_api():
    printer = FakePrinter()
    printer.objects["motion_queuing"] = MotionQueuingWithoutFlushCallback()
    config = FakeConfig(
        printer=printer,
        values={"use_flush_callback_bang_bang": True},
    )

    with pytest.raises(RuntimeError, match="register_flush_callback\\(\\) API"):
        buffer_feeder.BufferFeeder(config)


def test_flush_callback_mode_requires_can_add_trapq_support():
    printer = FakePrinter()
    printer.objects["motion_queuing"] = MotionQueuingWithoutCanAddTrapq()
    config = FakeConfig(
        printer=printer,
        values={"use_flush_callback_bang_bang": True},
    )

    with pytest.raises(RuntimeError, match="can_add_trapq=True"):
        buffer_feeder.BufferFeeder(config)
