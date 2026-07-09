"""Gruppe-1-Safety-Fixes (Logikfehler-Review 2026-07-09).

Bug A — _wait_for_move_done ist direction-blind:
  Der Loop-Break via _abort_signalled() ignoriert direction=-1. Der
  eigene Docstring verlangt: "direction=-1 (UNLOAD/Retract): OVERFLOW/
  JAM blockieren nicht — Retract ist Recovery. Nur HALT bricht ab."
  Folge: bei aktivem HALL1/JAM returnt jede per-Chunk-Wait in
  cmd_BUFFER_UNLOAD_PHASE3 sofort und die while-Schleife queued die
  volle MAX_DISTANCE (unload_fast_max=5000mm) in Millisekunden in den
  Trapq — nicht abbrechbar, falscher Overshoot-Raise.

Bug B — Continuous-Feed-Pump umgeht den HALL1-Hold in LOAD_PHASE_3:
  _load_phase3_tick haelt den Chunk-Stream bewusst bei HALL1
  ("stuffs filament ... squirting from the nozzle"), aber der
  generische Pump-Block in _main_tick springt ein sobald kein Move
  in flight ist (STATE_LOADING_PUSH war in CONTINUOUS_FEED_STATES).
  Pump-Chunks zaehlen zudem nicht in _load_phase3_distance →
  MAX_DISTANCE-Cap umgangen. _load_phase3_tick besitzt die Chunk-
  Submission in LOADING_PUSH exklusiv.

Bug C — _tick_pending_chunk nullt Retract-Pending bei HALL1:
  Der _abort_signalled()-Branch nullt den Pending-Stream direction-
  blind. Der Triple-Click-Retract-Burst (-1300mm) ist die designierte
  OVERFLOW-Recovery (retract_overflow_override) — er starb nach dem
  ersten ~50mm-Chunk, solange HALL1 noch aktiv war. Retract-Streams
  darf nur HALT beenden (analog Bug A); Forward-Streams brechen
  weiterhin auf jedes Abort-Signal ab.
"""

import pytest
from fakes_klipper import FakeConfig, FakePrinter, FakePrintStats
from klipper_extras import buffer_feeder


def make_feeder(values=None):
    printer = FakePrinter()
    printer.objects["print_stats"] = FakePrintStats(state="standby")
    config = FakeConfig(printer=printer, values=values)
    feeder = buffer_feeder.BufferFeeder(config)
    feeder._startup_grace_done = True
    set_sensor_active(feeder, 'hall_overflow', False)
    set_sensor_active(feeder, 'hall_full', False)
    set_sensor_active(feeder, 'hall_empty', False)
    set_sensor_active(feeder, 'entrance', True)
    return printer, feeder


def set_sensor_active(feeder, sensor_name, active):
    polarity_flip = feeder._pin_polarity_flip[sensor_name]
    raw = (not active) if polarity_flip else active
    feeder._pin_stable_state[sensor_name] = raw
    feeder._pin_raw_state[sensor_name] = raw


# ---------------------------------------------------------------------------
# Bug A — _wait_for_move_done direction=-1
# ---------------------------------------------------------------------------


def test_wait_for_move_done_retract_waits_through_hall1(feeder_move=None):
    """direction=-1 + aktives HALL1: der Wait MUSS den in-flight Move
    auswarten (Docstring-Kontrakt), statt sofort zu returnen."""
    printer, feeder = make_feeder()
    set_sensor_active(feeder, 'hall_overflow', True)
    feeder._current_move = {'end_time': 0.5}

    feeder._wait_for_move_done(gcmd=None, direction=-1)

    assert printer.reactor.now >= 0.5, (
        "retract wait must outlast the in-flight move despite HALL1 — "
        "instant return lets UNLOAD_PHASE3 queue the full MAX_DISTANCE")
    assert not feeder._move_in_flight()


def test_wait_for_move_done_retract_waits_through_jam():
    """direction=-1 + latched _jam_active (UNLOAD aus STATE_JAM ist der
    dokumentierte Recovery-Pfad): Wait muss den Move auswarten."""
    printer, feeder = make_feeder()
    feeder._jam_active = True
    feeder._current_move = {'end_time': 0.5}

    feeder._wait_for_move_done(gcmd=None, direction=-1)

    assert printer.reactor.now >= 0.5
    assert not feeder._move_in_flight()


def test_wait_for_move_done_retract_halt_breaks_immediately():
    """Regression-Guard: HALT bleibt der harte Abbruch auch fuer
    direction=-1 — der Wait darf den (nominell fernen) Move NICHT
    auswarten."""
    printer, feeder = make_feeder()
    feeder._halt_requested = True
    feeder._current_move = {'end_time': 1000.0}

    feeder._wait_for_move_done(gcmd=None, direction=-1)

    assert printer.reactor.now < 1.0, (
        "HALT must break the retract wait immediately")


def test_wait_for_move_done_forward_still_aborts_on_hall1():
    """Regression-Guard: forward-Waits (direction=+1) brechen bei HALL1
    weiterhin sofort ab — Motor ist disabled, Auswarten sinnlos."""
    printer, feeder = make_feeder()
    set_sensor_active(feeder, 'hall_overflow', True)
    feeder._current_move = {'end_time': 1000.0}

    feeder._wait_for_move_done(gcmd=None, direction=+1)

    assert printer.reactor.now < 1.0


# ---------------------------------------------------------------------------
# Bug B — Continuous-Feed-Pump in STATE_LOADING_PUSH
# ---------------------------------------------------------------------------


def test_main_tick_pump_never_submits_in_loading_push(monkeypatch):
    """_load_phase3_tick besitzt die Chunk-Submission in LOADING_PUSH.
    Haelt er den Stream (HALL1-Hold / Overlay-Hold), darf die generische
    Pump NICHT einspringen — sie umgeht den HALL1-Hold und zaehlt nicht
    in _load_phase3_distance (MAX_DISTANCE-Cap)."""
    _, feeder = make_feeder()
    feeder._state = buffer_feeder.STATE_LOADING_PUSH
    feeder._continuous_feed = True
    feeder._continuous_feed_direction = 1
    feeder._continuous_feed_speed = feeder.feed_speed

    submit_calls = []
    monkeypatch.setattr(feeder, "_submit_move",
                        lambda d, s, **kw: submit_calls.append((d, s)))
    # HALL1-Hold-Fall: phase3-tick submittet bewusst nichts.
    monkeypatch.setattr(feeder, "_load_phase3_tick", lambda et: None)
    monkeypatch.setattr(feeder, "_move_in_flight", lambda: False)

    feeder._main_tick(eventtime=10.0)

    assert submit_calls == [], (
        "main_tick pump must not feed during LOAD_PHASE_3 — "
        "_load_phase3_tick owns chunk submission (HALL1-hold bypass)")


def test_main_tick_pump_still_runs_in_manual_feed(monkeypatch):
    """Regression-Guard: MANUAL_FEED behaelt die Pump (einzige
    Chunk-Quelle fuer Dauerfeed)."""
    _, feeder = make_feeder()
    feeder._state = buffer_feeder.STATE_MANUAL_FEED
    feeder._continuous_feed = True
    feeder._continuous_feed_direction = 1
    feeder._continuous_feed_speed = feeder.manual_speed

    submit_calls = []
    monkeypatch.setattr(feeder, "_submit_move",
                        lambda d, s, **kw: submit_calls.append((d, s)))
    monkeypatch.setattr(feeder, "_move_in_flight", lambda: False)

    feeder._main_tick(eventtime=10.0)

    assert len(submit_calls) == 1


# ---------------------------------------------------------------------------
# Bug C — Retract-Pending-Stream bei HALL1
# ---------------------------------------------------------------------------


def test_pending_chunk_retract_survives_hall1(monkeypatch):
    """Retract-Burst (-1300mm, retract_overflow_override) ist die
    designierte OVERFLOW-Entlastung: aktives HALL1 darf den Pending-
    Stream NICHT nullen — naechster Sub-Chunk muss submitten."""
    _, feeder = make_feeder()
    feeder._state = buffer_feeder.STATE_MANUAL_RETRACT
    set_sensor_active(feeder, 'hall_overflow', True)
    feeder._pending_remaining_mm = 300.0
    feeder._pending_direction = -1
    feeder._pending_speed = 25.0
    feeder._last_move_end_time = 0.0  # gap klein -> submit faellig

    submit_calls = []
    monkeypatch.setattr(feeder, "_submit_single_trapezoid",
                        lambda d, s, **kw: submit_calls.append((d, s)))

    feeder._tick_pending_chunk(eventtime=10.0)

    assert len(submit_calls) == 1, (
        "retract pending stream must survive HALL1 — it IS the "
        "overflow recovery move")
    assert submit_calls[0][0] < 0
    assert feeder._pending_remaining_mm == pytest.approx(
        300.0 - abs(submit_calls[0][0]))


def test_pending_chunk_retract_halt_still_zeroes(monkeypatch):
    """Regression-Guard: HALT nullt auch Retract-Pending sofort."""
    _, feeder = make_feeder()
    feeder._state = buffer_feeder.STATE_MANUAL_RETRACT
    feeder._halt_requested = True
    feeder._pending_remaining_mm = 300.0
    feeder._pending_direction = -1
    feeder._pending_speed = 25.0
    feeder._last_move_end_time = 0.0

    submit_calls = []
    monkeypatch.setattr(feeder, "_submit_single_trapezoid",
                        lambda d, s, **kw: submit_calls.append((d, s)))

    feeder._tick_pending_chunk(eventtime=10.0)

    assert submit_calls == []
    assert feeder._pending_remaining_mm == 0.0
    assert feeder._pending_submit_chunk_cap is None


def test_pending_chunk_forward_still_zeroed_on_hall1(monkeypatch):
    """Regression-Guard: Forward-Pending bricht bei HALL1 weiterhin
    sofort ab (Q6b-Kontrakt: inkl. Sub-Chunk-Cap-Reset)."""
    _, feeder = make_feeder()
    feeder._state = buffer_feeder.STATE_MANUAL_FEED
    set_sensor_active(feeder, 'hall_overflow', True)
    feeder._pending_remaining_mm = 300.0
    feeder._pending_direction = 1
    feeder._pending_speed = 25.0
    feeder._pending_submit_chunk_cap = 9.0
    feeder._last_move_end_time = 0.0

    submit_calls = []
    monkeypatch.setattr(feeder, "_submit_single_trapezoid",
                        lambda d, s, **kw: submit_calls.append((d, s)))

    feeder._tick_pending_chunk(eventtime=10.0)

    assert submit_calls == []
    assert feeder._pending_remaining_mm == 0.0
    assert feeder._pending_submit_chunk_cap is None
