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


# ---------------------------------------------------------------------------
# P7-56f follow-up: lazy stale-suspend recovery
# Codex review found a second stuck-state path: PAUSE → CANCEL.
# idle_timeout:ready only fires once, so a CANCEL after PAUSE leaves
# _bang_bang_suspended=True forever. _clear_stale_suspend_if_print_-
# inactive heals it lazily at decision points.
# ---------------------------------------------------------------------------

def test_clear_stale_suspend_paused_keeps_lock():
    """Genuine PAUSE state must NOT clear the suspend — RESUME is
    legitimately expected."""
    printer, feeder = make_feeder()
    printer.objects['print_stats'] = FakePrintStats(state='paused')
    feeder._bang_bang_suspended = True

    cleared = feeder._clear_stale_suspend_if_print_inactive(0.0)

    assert cleared is False
    assert feeder._bang_bang_suspended is True


def test_clear_stale_suspend_cancelled_clears_lock():
    """PAUSE → CANCEL leaves state='cancelled' — must heal."""
    printer, feeder = make_feeder()
    printer.objects['print_stats'] = FakePrintStats(state='cancelled')
    feeder._bang_bang_suspended = True

    cleared = feeder._clear_stale_suspend_if_print_inactive(0.0)

    assert cleared is True
    assert feeder._bang_bang_suspended is False


def test_clear_stale_suspend_error_clears_lock():
    """PAUSE → ERROR leaves state='error' — must heal."""
    printer, feeder = make_feeder()
    printer.objects['print_stats'] = FakePrintStats(state='error')
    feeder._bang_bang_suspended = True

    cleared = feeder._clear_stale_suspend_if_print_inactive(0.0)

    assert cleared is True
    assert feeder._bang_bang_suspended is False


def test_clear_stale_suspend_complete_clears_lock():
    """PAUSE → finished print: also healable."""
    printer, feeder = make_feeder()
    printer.objects['print_stats'] = FakePrintStats(state='complete')
    feeder._bang_bang_suspended = True

    cleared = feeder._clear_stale_suspend_if_print_inactive(0.0)

    assert cleared is True
    assert feeder._bang_bang_suspended is False


def test_clear_stale_suspend_no_lock_no_op():
    """If suspend isn't set, do nothing (no print_stats lookup)."""
    printer, feeder = make_feeder()
    # Replace print_stats with one that raises if accessed
    class TrapPrintStats:
        def get_status(self, et):
            raise AssertionError("should not be called")
    printer.objects['print_stats'] = TrapPrintStats()
    feeder._bang_bang_suspended = False

    cleared = feeder._clear_stale_suspend_if_print_inactive(0.0)

    assert cleared is False


def test_clear_stale_suspend_missing_print_stats_keeps_lock():
    """No print_stats object available — defensive: do not clear
    (can't tell if it's a legitimate paused state). The Codex review
    flagged this as the safer default."""
    printer, feeder = make_feeder()
    if 'print_stats' in printer.objects:
        del printer.objects['print_stats']
    feeder._bang_bang_suspended = True

    cleared = feeder._clear_stale_suspend_if_print_inactive(0.0)

    assert cleared is False
    assert feeder._bang_bang_suspended is True


def test_entrance_insert_heals_stale_suspend_after_cancel():
    """End-to-end: PAUSE → CANCEL leaves _bang_bang_suspended=True.
    Operator inserts new filament — entrance handler now lazily
    clears the stale flag and proceeds to auto-grip (rather than
    rejecting with 'Reinsert during paused print')."""
    printer, feeder = make_feeder()
    printer.objects['print_stats'] = FakePrintStats(state='cancelled')
    feeder._bang_bang_suspended = True
    feeder._state = buffer_feeder.STATE_IDLE
    feeder._entrance_was_empty = True
    grip_calls = []
    feeder._start_initial_grip = lambda et: grip_calls.append(et)

    feeder._on_entrance_insert(eventtime=42.0)

    assert feeder._bang_bang_suspended is False
    assert len(grip_calls) == 1, "expected auto-grip after stale-clear"


def test_entrance_insert_during_real_pause_still_blocks():
    """Genuine PAUSE (state='paused') still suppresses auto-grip."""
    printer, feeder = make_feeder()
    printer.objects['print_stats'] = FakePrintStats(state='paused')
    feeder._bang_bang_suspended = True
    feeder._state = buffer_feeder.STATE_IDLE
    feeder._entrance_was_empty = True
    grip_calls = []
    feeder._start_initial_grip = lambda et: grip_calls.append(et)

    feeder._on_entrance_insert(eventtime=42.0)

    assert feeder._bang_bang_suspended is True
    assert len(grip_calls) == 0, "must not grip during real pause"
