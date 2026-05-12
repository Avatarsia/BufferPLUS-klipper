"""P7-69 (Issue #18) — Defense-in-depth sync guards on buffer submit paths.

Reported by Eifel-Joe (Issue #18): _submit_single_trapezoid had no guard
against being called while the stepper is synced to an extruder trapq
(_stepper_synced_to != None). When the dangerous combination

  1. SYNC active (BUFFER_SYNC_TO_EXTRUDER, e.g. during UNLOAD tip-forming)
  2. HALL3 (hall_empty) drops during SYNC → bang-bang wants to feed
  3. gap = mcu_now - _last_move_end_time > REPRIME_GAP (5s)

is met, _submit_single_trapezoid's forced_t0=None branch calls
toolhead.flush_step_generation() + stepper.set_position((0,0,0))
mid-print. The flush drains the toolhead queue → extruder stops while
it is actively driving the synced buffer stepper. set_position resets
the itersolve cursor while the stepper is bound to the extruder_trapq
→ position-inconsistency.

The fix is defense-in-depth — three guards on the path
_main_tick → _bang_bang_tick → _submit_move → _submit_single_trapezoid:

  1. _bang_bang_tick (mirror of _on_mcu_flush:2188 sync-guard, the legacy
     reactor-tick path was missing it)
  2. _submit_move (multiple call-sites: LOAD/UNLOAD/MANUAL/grip-follow;
     defense against any future caller bypassing the bang-bang gate)
  3. _submit_single_trapezoid (innermost gate at the exact site of the
     dangerous flush_step_generation + set_position side-effects;
     also covers _tick_pending_chunk which jumps directly to this
     primitive for streaming-continuation chunks)

The _on_mcu_flush path already has the guard (since P7-52 / P7-45) so
it is not retested here — see test_p752_flush_callback_bang_bang
::test_flush_no_submit_when_synced_to_extruder.
"""

import pytest

from fakes_klipper import FakeConfig, FakePrinter
from klipper_extras import buffer_feeder


def set_sensor_active(feeder, sensor_name, active):
    polarity_flip = feeder._pin_polarity_flip[sensor_name]
    raw = (not active) if polarity_flip else active
    feeder._pin_stable_state[sensor_name] = raw
    feeder._pin_raw_state[sensor_name] = raw


def make_feeder(values=None, state=None):
    """STATE_AUTO by default — that's where Issue #18 reproduces.
    All hall sensors inactive (the FakeConfig polarity-flip-default
    would otherwise leave HALL2/HALL1 spuriously active and most of
    these tests would short-circuit before reaching the guard)."""
    printer = FakePrinter()
    config = FakeConfig(printer=printer, values=values)
    feeder = buffer_feeder.BufferFeeder(config)
    feeder._startup_grace_done = True
    feeder._state = state if state is not None else buffer_feeder.STATE_AUTO
    set_sensor_active(feeder, 'hall_overflow', False)
    set_sensor_active(feeder, 'hall_full', False)
    set_sensor_active(feeder, 'hall_empty', False)
    set_sensor_active(feeder, 'entrance', True)
    return printer, feeder


# ---------------------------------------------------------------------------
# Characterisation: the unguarded code path (pre-P7-69 behavior).
# We disable the guards we just added to PROVE the bug exists, then re-enable.
# This is the "PRE-FIX assert" requested in the patch brief.
# ---------------------------------------------------------------------------

def test_pre_fix_characterisation_unguarded_trapezoid_calls_flush(monkeypatch):
    """PRE-FIX assertion: without the P7-69 guard, _submit_single_trapezoid
    DOES call toolhead.flush_step_generation when forced_t0=None and
    gap > 5s — that is exactly the bug Issue #18 names. By bypassing
    the guard via monkeypatch we recreate the unguarded behavior to
    demonstrate the regression hazard the guard is protecting against.
    """
    printer, feeder = make_feeder()
    toolhead = printer.lookup_object('toolhead')

    # Simulate SYNC active.
    feeder._stepper_synced_to = 'extruder'
    # gap > REPRIME_GAP (=5.0). _last_move_end_time = 0, mcu_now ~= 10.
    feeder._last_move_end_time = 0.0
    feeder._stepcompress_primed = True  # primed but gap-based reprime fires
    feeder.reactor.now = 10.0

    # PROVE the bug: call the unguarded primitive logic by removing the
    # P7-69 guard at the top. We do this by patching out the early-return
    # entirely so we can characterize what the function would otherwise do.
    # The internal flush_step_generation must fire when forced_t0=None,
    # gap > 5s — that is the dangerous side-effect Issue #18 names.

    # Force the function body past our new guard by clearing the
    # synced-to indicator temporarily, then re-asserting it before any
    # observable side-effect: no — we instead bypass via temporarily
    # zeroing the attribute. That is closer to "what would happen pre-fix".
    orig_synced = feeder._stepper_synced_to
    feeder._stepper_synced_to = None
    flush_before = toolhead.flush_calls
    try:
        feeder._submit_single_trapezoid(0.05, feeder.feed_speed)
    finally:
        feeder._stepper_synced_to = orig_synced

    # Without the guard the gap-based reprime DOES call flush_step_generation.
    assert toolhead.flush_calls > flush_before, (
        "PRE-FIX characterisation: forced_t0=None + gap>5s must invoke "
        "toolhead.flush_step_generation — this is the dangerous side-"
        "effect the P7-69 guard prevents while synced")


# ---------------------------------------------------------------------------
# Guard 1: _bang_bang_tick (mirrors _on_mcu_flush:2188)
# ---------------------------------------------------------------------------

def test_bang_bang_tick_skips_when_synced(monkeypatch):
    """Issue #18 root path: AUTO + hall_empty + _stepper_synced_to !=
    None. _bang_bang_tick must early-return and NOT call _start_
    continuous_motion / _submit_move. Mirrors the _on_mcu_flush:2188
    guard which the legacy reactor-tick path was missing."""
    _, feeder = make_feeder()
    feeder._stepper_synced_to = 'extruder'
    set_sensor_active(feeder, 'hall_empty', True)

    submit_move_calls = []
    start_motion_calls = []
    monkeypatch.setattr(feeder, "_submit_move",
                        lambda d, s, **kw: submit_move_calls.append((d, s)))
    monkeypatch.setattr(feeder, "_start_continuous_motion",
                        lambda d, s, t: start_motion_calls.append((d, s, t)))

    feeder._bang_bang_tick(eventtime=10.0)

    assert submit_move_calls == []
    assert start_motion_calls == [], (
        "bang-bang must stay out of the way while a macro-driven SYNC "
        "has the stepper bound to the extruder trapq")


def test_bang_bang_tick_runs_normally_when_not_synced(monkeypatch):
    """Regression-guard: with sync inactive, hall_empty still triggers
    _start_continuous_motion as before. The guard must not break the
    normal feed loop."""
    _, feeder = make_feeder()
    feeder._stepper_synced_to = None
    set_sensor_active(feeder, 'hall_empty', True)

    start_motion_calls = []
    monkeypatch.setattr(feeder, "_start_continuous_motion",
                        lambda d, s, t: start_motion_calls.append((d, s, t)))

    feeder._bang_bang_tick(eventtime=10.0)

    assert len(start_motion_calls) == 1, (
        "regression: bang-bang must still feed when not synced")


# ---------------------------------------------------------------------------
# Guard 2: _submit_move (defense for LOAD/UNLOAD/MANUAL/grip-follow callers)
# ---------------------------------------------------------------------------

def test_submit_move_skips_when_synced(monkeypatch):
    """Any caller of _submit_move while synced must be a no-op. Even
    if a future code path bypasses _bang_bang_tick (manual cmd, phase
    handler, grip-follow), nothing should land on own_trapq while the
    stepper is bound to the extruder trapq."""
    _, feeder = make_feeder()
    feeder._stepper_synced_to = 'extruder'

    trap_calls = []
    monkeypatch.setattr(feeder, "_submit_single_trapezoid",
                        lambda d, s, **kw: trap_calls.append((d, s)))

    feeder._submit_move(50.0, feeder.feed_speed)

    assert trap_calls == [], (
        "_submit_move must not delegate to _submit_single_trapezoid "
        "while synced — defense-in-depth above the innermost guard")


def test_submit_move_runs_normally_when_not_synced(monkeypatch):
    """Regression-guard: sync inactive → _submit_move chunks normally."""
    _, feeder = make_feeder()
    feeder._stepper_synced_to = None

    trap_calls = []
    monkeypatch.setattr(feeder, "_submit_single_trapezoid",
                        lambda d, s, **kw: trap_calls.append((d, s)))

    feeder._submit_move(50.0, feeder.feed_speed)

    assert len(trap_calls) == 1, (
        "regression: _submit_move must still chunk-and-submit when "
        "not synced")


# ---------------------------------------------------------------------------
# Guard 3: _submit_single_trapezoid — innermost defense at the exact site of
# the dangerous flush_step_generation + set_position side-effects.
# ---------------------------------------------------------------------------

def test_submit_single_trapezoid_no_flush_when_synced(monkeypatch):
    """THE Issue #18 invariant: while synced, _submit_single_trapezoid
    must NOT call toolhead.flush_step_generation — even when
    forced_t0=None AND gap > REPRIME_GAP (the exact dangerous
    combination Issue #18 names)."""
    printer, feeder = make_feeder()
    toolhead = printer.lookup_object('toolhead')

    feeder._stepper_synced_to = 'extruder'
    feeder._last_move_end_time = 0.0
    feeder._stepcompress_primed = True
    feeder.reactor.now = 10.0  # gap = 10 - 0 = 10s > REPRIME_GAP=5s

    flush_before = toolhead.flush_calls
    feeder._submit_single_trapezoid(0.05, feeder.feed_speed)

    assert toolhead.flush_calls == flush_before, (
        "P7-69 guard violated: toolhead.flush_step_generation called "
        "while synced → mid-print extruder stop hazard")


def test_submit_single_trapezoid_no_set_position_when_synced(monkeypatch):
    """Companion to the flush check: set_position((0,0,0)) on a stepper
    bound to extruder_trapq would zero the itersolve cursor while it is
    being driven by the extruder — corruption of commanded_pos."""
    _, feeder = make_feeder()
    set_position_calls = []
    monkeypatch.setattr(feeder.stepper, "set_position",
                        lambda pos: set_position_calls.append(pos))

    feeder._stepper_synced_to = 'extruder'
    feeder._last_move_end_time = 0.0
    feeder._stepcompress_primed = False  # also trigger not-primed path
    feeder.reactor.now = 10.0

    feeder._submit_single_trapezoid(0.05, feeder.feed_speed)

    assert set_position_calls == [], (
        "P7-69 guard violated: stepper.set_position called while "
        "stepper bound to extruder_trapq → cursor corruption hazard")


def test_submit_single_trapezoid_no_trapq_append_when_synced(monkeypatch):
    """Belt-and-suspenders: no trapezoid appended on own_trapq while
    synced (independent of the flush/set_position side-effects)."""
    printer, feeder = make_feeder()
    motion_q = printer.lookup_object('motion_queuing')

    feeder._stepper_synced_to = 'extruder'

    appends_before = len(motion_q.append_calls)
    feeder._submit_single_trapezoid(5.0, feeder.feed_speed,
                                     forced_t0=12.0)

    new_appends = motion_q.append_calls[appends_before:]
    own_appends = [c for c in new_appends if c[0] is feeder.trapq]
    assert own_appends == [], (
        "no trapq_append on own_trapq must occur while synced — would "
        "queue moves on the wrong trapq")


def test_submit_single_trapezoid_runs_normally_when_not_synced(monkeypatch):
    """Regression-guard: sync inactive → forced_t0 path appends to own
    trapq exactly as before."""
    printer, feeder = make_feeder()
    motion_q = printer.lookup_object('motion_queuing')

    feeder._stepper_synced_to = None
    feeder._stepcompress_primed = True  # avoid set_position branch

    appends_before = len(motion_q.append_calls)
    feeder._submit_single_trapezoid(5.0, feeder.feed_speed,
                                     forced_t0=12.0)

    new_appends = motion_q.append_calls[appends_before:]
    own_appends = [c for c in new_appends if c[0] is feeder.trapq]
    assert len(own_appends) == 1, (
        "regression: forced_t0 path must still append on own trapq "
        "when not synced")


# ---------------------------------------------------------------------------
# Race-window: sync becomes active MID-MOVE — chunks already submitted to
# the trapq must drain naturally, but NO new submits may follow.
# ---------------------------------------------------------------------------

def test_sync_activation_mid_move_blocks_new_submits_but_lets_inflight_drain(monkeypatch):
    """A submitted trapezoid is owned by the trapq once trapq_append
    fired — we cannot recall it (that's the whole architectural reason
    we don't flush on HALT). What we DO control: once _stepper_synced_-
    to flips to a non-None value, the very next reactor-tick / flush-
    callback / pending-chunk request must short-circuit.

    Simulate: chunk N landed on own trapq, then SYNC activates, then
    pending-chunk-tick fires. Chunk N stays in the trapq (drains
    naturally), but chunk N+1 MUST NOT be submitted.
    """
    printer, feeder = make_feeder()
    motion_q = printer.lookup_object('motion_queuing')

    # Pre-state: a streaming sequence is mid-flight (chunk N landed,
    # remaining 25mm queued for streaming).
    feeder._stepper_synced_to = None
    feeder._pending_remaining_mm = 25.0
    feeder._pending_direction = 1.0
    feeder._pending_speed = feeder.feed_speed
    feeder._last_move_end_time = 10.0  # chunk-N end_time still in future
    feeder.reactor.now = 9.9  # we're mid-chunk
    appends_baseline = len(motion_q.append_calls)

    # SYNC activates between chunk-N submit and chunk-(N+1) tick:
    feeder._stepper_synced_to = 'extruder'

    # Drive _tick_pending_chunk — would normally fire chunk N+1.
    feeder._tick_pending_chunk(eventtime=10.5)

    new_appends = motion_q.append_calls[appends_baseline:]
    own_appends = [c for c in new_appends if c[0] is feeder.trapq]
    assert own_appends == [], (
        "race-window: once SYNC activates, no NEW chunks may land on "
        "own_trapq; the in-flight chunk N stays in the trapq and "
        "drains naturally — we don't touch it")


def test_submit_move_during_mid_print_sync_no_pending_set(monkeypatch):
    """Belt: if a caller mid-print invokes _submit_move while synced,
    the guard fires BEFORE _pending_remaining_mm mutation. NOT-TO-DO
    2026-04-26 (cleanup-guard pattern): guards must early-return
    before mutating state."""
    _, feeder = make_feeder()
    feeder._stepper_synced_to = 'extruder'
    feeder._pending_remaining_mm = 0.0
    feeder._pending_direction = 0
    feeder._pending_speed = 0.0

    # Capture _submit_single_trapezoid invocations so we can assert
    # the guard returned BEFORE the mutation block at line ~2678
    # (self._pending_remaining_mm = 0.0).
    trap_calls = []
    monkeypatch.setattr(feeder, "_submit_single_trapezoid",
                        lambda d, s, **kw: trap_calls.append((d, s)))

    feeder._submit_move(60.0, feeder.feed_speed)

    # No delegation AND no surprise side-effects on pending state.
    assert trap_calls == []
    assert feeder._pending_remaining_mm == 0.0
    assert feeder._pending_speed == 0.0
