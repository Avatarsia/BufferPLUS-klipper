"""P7-53 — Jam-detection runs only during active print.

Hardware-Test 2026-04-27: User triggered _CLIENT_LINEAR_MOVE E=50
F=480 (e.g. for PA tuning) outside an active print. After 99s of
HALL2-active + 50mm extruder progress, the buffer-feeder reported:

  *** JAM CLOG: HALL2 active 99s, extruder +50.0mm —
      nozzle clog suspected ***

False positive: HALL2 is naturally active for many seconds during
manual workflows, and the extruder is moving as part of the manual
command. This isn't a clog — it's the operator deliberately
extruding outside a print job.

Fix: gate the entire jam_tick on _print_running. The flag is set/
cleared by idle_timeout:printing/ready event handlers, so the
behavior matches operator expectation: jam-detection is a print-
safety, not a workflow-tester.

Tests cover:
  - CLOG-detection runs during active print (positive control)
  - CLOG-detection skipped when not printing (the bug fix)
  - SUPPLY-detection has the same gate
  - Tracker variables reset on transition into not-printing
"""

import pytest
from fakes_klipper import FakeConfig, FakePrinter
from klipper_extras import buffer_feeder


def make_feeder(values=None):
    printer = FakePrinter()
    config = FakeConfig(printer=printer, values=values)
    feeder = buffer_feeder.BufferFeeder(config)
    feeder._startup_grace_done = True
    feeder._state = buffer_feeder.STATE_AUTO
    return printer, feeder


def set_sensor_active(feeder, sensor_name, active):
    polarity_flip = feeder._pin_polarity_flip[sensor_name]
    raw = (not active) if polarity_flip else active
    feeder._pin_stable_state[sensor_name] = raw
    feeder._pin_raw_state[sensor_name] = raw


def setup_clog_scenario(feeder):
    """HALL2 active + extruder progressing — the input shape that the
    CLOG detector watches."""
    set_sensor_active(feeder, 'hall_full', True)
    set_sensor_active(feeder, 'hall_empty', False)
    # Pretend 50mm of extruder movement happened in the dwell window
    feeder._hall2_start_time = 0.0
    feeder._hall2_start_extruder_pos = 0.0
    # Stub _read_extruder_position so dwell+progress trigger threshold
    feeder._read_extruder_position = lambda: 50.0


def setup_supply_jam_scenario(feeder):
    """HALL3 active + feeder running forward — the input shape that
    the SUPPLY-jam detector watches."""
    set_sensor_active(feeder, 'hall_empty', True)
    set_sensor_active(feeder, 'hall_full', False)
    feeder._continuous_feed = True
    feeder._continuous_feed_direction = 1
    feeder._hall3_start_time = 0.0


# ---------------------------------------------------------------------------
# CLOG-detection
# ---------------------------------------------------------------------------

def test_clog_does_not_trigger_during_manual_extrude():
    """The exact hardware bug: manual G1 E50 outside a print should
    NOT trigger CLOG even after 99s + 50mm progress."""
    _, feeder = make_feeder()
    setup_clog_scenario(feeder)
    feeder._print_running = False

    # 99s have elapsed since hall2_start_time=0, HALL2 still active,
    # 50mm progress — pre-fix this would trigger _trigger_jam.
    feeder._jam_tick(eventtime=99.0)

    assert feeder._jam_active is False
    assert feeder._state == buffer_feeder.STATE_AUTO


def test_clog_triggers_during_active_print():
    """Positive control: same conditions during an active print
    must still fire CLOG-detection (real safety preserved)."""
    _, feeder = make_feeder()
    setup_clog_scenario(feeder)
    feeder._print_running = True

    feeder._jam_tick(eventtime=99.0)

    assert feeder._jam_active is True


def test_clog_does_not_trigger_below_threshold_even_in_print():
    """Threshold guard intact: dwell < 60s OR progress < 30mm must
    not trigger even during print."""
    _, feeder = make_feeder()
    setup_clog_scenario(feeder)
    feeder._print_running = True

    # 30s dwell — below threshold
    feeder._jam_tick(eventtime=30.0)

    assert feeder._jam_active is False


# ---------------------------------------------------------------------------
# SUPPLY-jam detection
# ---------------------------------------------------------------------------

def test_supply_jam_does_not_trigger_outside_print():
    """SUPPLY-jam (HALL3 active + feeder running forward) is also
    print-only. Operator could be manually feeding via
    BUFFER_FEED — that is not a supply jam."""
    _, feeder = make_feeder()
    setup_supply_jam_scenario(feeder)
    feeder._print_running = False

    # 130s elapsed (jam_supply_dwell_time default is 120s)
    feeder._jam_tick(eventtime=130.0)

    assert feeder._jam_active is False


def test_supply_jam_triggers_during_active_print():
    """Positive control."""
    _, feeder = make_feeder()
    setup_supply_jam_scenario(feeder)
    feeder._print_running = True

    feeder._jam_tick(eventtime=130.0)

    assert feeder._jam_active is True


# ---------------------------------------------------------------------------
# Tracker reset on transition out of print
# ---------------------------------------------------------------------------

def test_tracker_variables_reset_when_print_ends():
    """If a print ends mid-jam-watching, the trackers must be
    cleared so a subsequent print starts with a fresh state."""
    _, feeder = make_feeder()
    setup_clog_scenario(feeder)
    feeder._print_running = True
    feeder._jam_tick(eventtime=10.0)
    # Tracker is populated mid-print
    assert feeder._hall2_start_time is not None

    # Print ends — _print_running flips to False
    feeder._print_running = False
    feeder._jam_tick(eventtime=11.0)

    assert feeder._hall2_start_time is None
    assert feeder._hall3_start_time is None
