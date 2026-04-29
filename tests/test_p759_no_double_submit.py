"""P7-59 — Suppress reactor-tick continuous-feed-streaming when
flush-callback-bang-bang is the active feed-source.

Hardware-Crash 2026-04-29 (klippy.log "5"):
  buffer_feeder: stepcompress re-primed via flush_step_generation (gap=-0.6s)
  stepcompress o=0 i=0 c=6 a=0: Invalid sequence
  Error in syncemitter 'mellow' step generation
  Exception in flush_handler  →  MCU 'mcu' shutdown

Root cause: With use_flush_callback_bang_bang=True, two paths
submit chunks concurrently:

  1. _on_mcu_flush (flush-callback)   →  forced_t0 = step_gen_time + lead_time
                                         (race-free, klipper-cursor-synchron)
                                         sets _continuous_feed=True

  2. _main_tick continuous-feed-block →  forced_t0=None
                                         (legacy reactor-tick anchor)

The two anchors disagree. The flush-callback move sits in the trapq
with end_time in the future; reactor-tick computes gap = mcu_now -
_last_move_end_time and gets a NEGATIVE value (-0.6s in the log).
If _stepcompress_primed=False (after a recent _disable_stepper),
the reprime path runs flush_step_generation() + set_position(0,0,0)
mid-print, ripping itersolve out under the in-flight flush-callback
steps → Invalid sequence.

Fix: gate the reactor-tick continuous-feed-streaming with
"not (use_flush_callback_bang_bang and STATE_AUTO)". _on_mcu_flush
owns AUTO chunk submission. Manual/LOAD/UNLOAD phases keep the
reactor-tick path because _on_mcu_flush early-returns on non-AUTO.
"""

import pytest
from fakes_klipper import FakeConfig, FakePrinter
from klipper_extras import buffer_feeder


def make_feeder(values=None):
    printer = FakePrinter()
    config = FakeConfig(printer=printer, values=values)
    feeder = buffer_feeder.BufferFeeder(config)
    feeder._startup_grace_done = True
    # HALL sensors default to "active" for safety-first boot semantics
    # (see HallSensorMonitor.__init__). We need them off so _main_tick
    # passes the HALL1 lockout early-return at its top.
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


def test_main_tick_skips_streaming_in_auto_with_flush_callback(monkeypatch):
    """The exact bug fix: STATE_AUTO + use_flush_callback_bang_bang +
    _continuous_feed=True must NOT trigger a reactor-tick _submit_move.
    _on_mcu_flush is the single source of truth for AUTO chunks."""
    _, feeder = make_feeder(values={'use_flush_callback_bang_bang': True})
    feeder._state = buffer_feeder.STATE_AUTO
    feeder._continuous_feed = True
    feeder._continuous_feed_direction = 1
    feeder._continuous_feed_speed = feeder.feed_speed

    submit_calls = []
    monkeypatch.setattr(feeder, "_submit_move",
                        lambda d, s, **kw: submit_calls.append((d, s)))
    monkeypatch.setattr(feeder, "_move_in_flight", lambda: False)

    feeder._main_tick(eventtime=10.0)

    # Reactor-tick must NOT submit. _on_mcu_flush owns this path.
    assert submit_calls == [], (
        "reactor-tick continuous-feed-streaming must not run when "
        "flush-callback bang-bang is active in STATE_AUTO")


def test_main_tick_streams_in_manual_feed_with_flush_callback(monkeypatch):
    """Regression guard: STATE_MANUAL_FEED still uses reactor-tick
    streaming even with flush_callback_bang_bang=True. _on_mcu_flush
    bails on non-AUTO so reactor-tick is the ONLY chunk source for
    manual feed."""
    _, feeder = make_feeder(values={'use_flush_callback_bang_bang': True})
    feeder._state = buffer_feeder.STATE_MANUAL_FEED
    feeder._continuous_feed = True
    feeder._continuous_feed_direction = 1
    feeder._continuous_feed_speed = feeder.manual_speed

    submit_calls = []
    monkeypatch.setattr(feeder, "_submit_move",
                        lambda d, s, **kw: submit_calls.append((d, s)))
    monkeypatch.setattr(feeder, "_move_in_flight", lambda: False)

    feeder._main_tick(eventtime=10.0)

    # Reactor-tick MUST submit — manual feed doesn't go through
    # _on_mcu_flush (early-return on _state != STATE_AUTO).
    assert len(submit_calls) == 1


def test_main_tick_streams_in_auto_without_flush_callback(monkeypatch):
    """Legacy path regression: STATE_AUTO + use_flush_callback_bang_bang=False
    must still use reactor-tick streaming (flush-callback is opt-in)."""
    _, feeder = make_feeder(values={'use_flush_callback_bang_bang': False})
    feeder._state = buffer_feeder.STATE_AUTO
    feeder._continuous_feed = True
    feeder._continuous_feed_direction = 1
    feeder._continuous_feed_speed = feeder.feed_speed

    submit_calls = []
    monkeypatch.setattr(feeder, "_submit_move",
                        lambda d, s, **kw: submit_calls.append((d, s)))
    monkeypatch.setattr(feeder, "_move_in_flight", lambda: False)
    monkeypatch.setattr(feeder, "_bang_bang_tick", lambda et: None)

    feeder._main_tick(eventtime=10.0)

    assert len(submit_calls) == 1, (
        "use_flush_callback_bang_bang=False must keep the legacy "
        "reactor-tick streaming path active")


def test_on_mcu_flush_initial_feed_uses_forced_t0(monkeypatch):
    """The flush-callback path MUST use forced_t0 — that's the whole
    point of the flush-callback architecture. If forced_t0 ever ends
    up None, the same crash class returns. Pin this contract."""
    printer, feeder = make_feeder(values={'use_flush_callback_bang_bang': True})
    feeder._state = buffer_feeder.STATE_AUTO
    feeder._continuous_feed = False
    # Set hall sensors so _on_mcu_flush enters the feed branch.
    polarity_flip = feeder._pin_polarity_flip
    feeder._pin_stable_state['hall_empty'] = (
        not True if polarity_flip['hall_empty'] else True)
    feeder._pin_stable_state['hall_full'] = (
        not False if polarity_flip['hall_full'] else False)
    feeder._pin_stable_state['hall_overflow'] = (
        not False if polarity_flip['hall_overflow'] else False)

    submit_calls = []
    monkeypatch.setattr(feeder, "_submit_move",
                        lambda d, s, forced_t0=None: submit_calls.append(
                            (d, s, forced_t0)))

    # Simulate Klipper firing the flush-callback at print_time=10s,
    # step_gen_time=10.5s.
    feeder._on_mcu_flush(flush_time=10.0, step_gen_time=10.5)

    assert len(submit_calls) == 1
    distance, speed, forced_t0 = submit_calls[0]
    assert forced_t0 is not None, (
        "_on_mcu_flush must always pass forced_t0 — flush-callback "
        "race-free anchor IS the value-add over reactor-tick")
    # Anchor should be step_gen_time + lead_time
    assert forced_t0 == pytest.approx(10.5 + feeder.lead_time)


def test_main_tick_pending_chunk_still_runs_in_manual_feed(monkeypatch):
    """Belt-and-suspenders: P7-59 only gates continuous-feed-streaming.
    Pending-chunk streaming for long single-shot moves still runs in
    MANUAL_FEED — pending-chunk is for BUFFER_FEED/RETRACT/LOAD_PHASE_1
    spillover and uses its own abort/halt path; it's NOT the same as
    continuous bang-bang. _on_mcu_flush bails on non-AUTO so pending-
    chunk is the only path here."""
    _, feeder = make_feeder(values={'use_flush_callback_bang_bang': True})
    feeder._state = buffer_feeder.STATE_MANUAL_FEED
    feeder._continuous_feed = False
    feeder._pending_remaining_mm = 30.0
    feeder._pending_direction = 1
    feeder._pending_speed = 25.0

    submit_calls = []
    monkeypatch.setattr(feeder, "_submit_single_trapezoid",
                        lambda d, s: submit_calls.append((d, s)))
    monkeypatch.setattr(feeder, "_abort_signalled", lambda: False)

    feeder._last_move_end_time = 0.0  # past — small gap triggers submit

    feeder._main_tick(eventtime=10.0)

    assert len(submit_calls) == 1


def test_main_tick_pending_chunk_runs_in_auto_with_flush_callback(monkeypatch):
    """Codex-flagged edge-case: pending-chunk in STATE_AUTO with
    flush-callback active. Not reachable in normal AUTO bang-bang
    (since _on_mcu_flush submits 15mm chunks while max_move_chunk_mm
    is 50mm, _pending_remaining_mm should never accumulate). But if
    a state-leak ever set both _pending_remaining_mm>0 and AUTO,
    pending-chunk would still submit — pin that contract so a future
    refactor doesn't accidentally gate it too."""
    _, feeder = make_feeder(values={'use_flush_callback_bang_bang': True})
    feeder._state = buffer_feeder.STATE_AUTO
    feeder._continuous_feed = False
    feeder._pending_remaining_mm = 30.0
    feeder._pending_direction = 1
    feeder._pending_speed = 25.0

    submit_calls = []
    monkeypatch.setattr(feeder, "_submit_single_trapezoid",
                        lambda d, s: submit_calls.append((d, s)))
    monkeypatch.setattr(feeder, "_abort_signalled", lambda: False)

    feeder._last_move_end_time = 0.0

    feeder._main_tick(eventtime=10.0)

    assert len(submit_calls) == 1, (
        "pending-chunk path is independent of continuous-feed-streaming"
        " — P7-59 must not affect it")
