"""P7-56f — _on_idle_ready differentiates PAUSE vs print-end.

User-reported bug: After a print finishes ("Done printing file"),
the buffer stayed in _bang_bang_suspended=True because _on_idle_ready
treated ALL transitions out of printing as "Print paused — RESUME
expected". That blocked auto-grip when the user inserted fresh
filament for the next print, with the only recovery being manual
BUFFER_AUTO_OFF + BUFFER_AUTO_ON or FORCE_BUFFER_FILL.

Fix: read print_stats.state in _on_idle_ready. Only state='paused'
arms the suspension; print-end (state='complete'/'standby') leaves
the buffer in its prior state so the next entrance-insert grips
normally.
"""

import pytest
from fakes_klipper import FakeConfig, FakePrinter, FakePrintStats
from klipper_extras import buffer_feeder


def make_feeder(values=None):
    printer = FakePrinter()
    config = FakeConfig(printer=printer, values=values)
    feeder = buffer_feeder.BufferFeeder(config)
    feeder._startup_grace_done = True
    feeder._print_running = True  # we test the printing→ready transition
    return printer, feeder


def test_idle_ready_pause_during_print_suspends_bang_bang():
    """state='paused' is the legitimate RESUME-expected case."""
    printer, feeder = make_feeder()
    printer.objects['print_stats'] = FakePrintStats(state='paused')
    feeder._bang_bang_suspended = False

    feeder._on_idle_ready()

    assert feeder._bang_bang_suspended is True
    assert feeder._print_running is False


def test_idle_ready_print_end_does_not_suspend_bang_bang():
    """state='complete' means print finished — no RESUME ever, so
    bang-bang must NOT lock out. This is the bug-report scenario."""
    printer, feeder = make_feeder()
    printer.objects['print_stats'] = FakePrintStats(state='complete')
    feeder._bang_bang_suspended = False

    feeder._on_idle_ready()

    assert feeder._bang_bang_suspended is False
    assert feeder._print_running is False


def test_idle_ready_print_standby_does_not_suspend_bang_bang():
    """state='standby' is the post-print idle state — same as complete:
    no RESUME, buffer must stay available."""
    printer, feeder = make_feeder()
    printer.objects['print_stats'] = FakePrintStats(state='standby')
    feeder._bang_bang_suspended = False

    feeder._on_idle_ready()

    assert feeder._bang_bang_suspended is False


def test_idle_ready_preserves_prior_suspended_flag_on_print_end():
    """If the operator set _bang_bang_suspended=True manually (e.g. via
    BUFFER_AUTO_OFF), print-end must NOT clear it. Only RESUME / next
    print should unsuspend, matching the existing _on_idle_printing
    contract."""
    printer, feeder = make_feeder()
    printer.objects['print_stats'] = FakePrintStats(state='complete')
    feeder._bang_bang_suspended = True  # operator-set

    feeder._on_idle_ready()

    assert feeder._bang_bang_suspended is True


def test_idle_ready_during_grace_ignores_event():
    """Existing guard: events before startup_grace_done don't suspend."""
    printer, feeder = make_feeder()
    printer.objects['print_stats'] = FakePrintStats(state='paused')
    feeder._startup_grace_done = False  # override
    feeder._bang_bang_suspended = False

    feeder._on_idle_ready()

    assert feeder._bang_bang_suspended is False
    assert feeder._print_running is False


def test_idle_ready_print_end_halts_continuous_feed_only_on_pause():
    """The continuous-feed halt was tied to the 'paused' branch in the
    legacy code. After P7-56f it stays in the paused-branch only —
    print-end doesn't need to halt continuous feed (it shouldn't have
    been running anyway after idle_timeout fires)."""
    printer, feeder = make_feeder()
    printer.objects['print_stats'] = FakePrintStats(state='complete')
    feeder._bang_bang_suspended = False
    feeder._continuous_feed = True  # leftover from print

    feeder._on_idle_ready()

    # On print-end we leave _continuous_feed alone. _on_idle_ready
    # is the wrong place to mass-clear motion state — that's HALT /
    # AUTO_OFF / STOP_BUFFER_FILL. _set_state(STATE_IDLE) on the
    # natural state transitions handles motion stop.
    # If state is paused this would be cleared (separate test).
    assert feeder._continuous_feed is True
