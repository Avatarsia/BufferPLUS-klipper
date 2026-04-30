"""P7-63 — Reset _feed_distance_accumulator on hall_full + halt_motion +
sustained Zwischen-Zone (Issue #26 root-cause path).

Fixes Issue #26: false-positive JAM_SAFETY_DISTANCE in AUTO mode at
high print flow.

Root cause (verified at code level prior to fix):
- _on_mcu_flush continuously streams chunks while hall_empty is active
  (P7-61). At high flow rates the buffer arm stays near the hall_empty
  threshold; hall_full never triggers; the False->True session-start
  reset (P7-57) does not fire; the accumulator grows past
  max_feed_distance and trips a false JAM after ~100s
  (3000mm / 30mm/s).

The fix touches seven surgical sites:
  1. _on_mcu_flush hall_full-Branch — accumulator now measures
     "distance since last confirmed buffer-full".
  2. _halt_motion — covers JAM/RUNOUT/PAUSE/CLEAR_JAM defense-in-depth.
  3. _on_mcu_flush Zwischen-Zone with grace-timer — once HALL3 has been
     stably inactive for >= STABLE_DROP_GRACE (0.5s), the AUTO feed
     session ends and the accumulator resets. This is the actual
     Issue #26 path: high-flow steady-state never reaches hall_full,
     but the arm leaves hall_empty for sustained periods between
     chunks. Short bouncing flicker (<0.5s) does NOT reset.
  4. _auto_between_since reset paths — hall_empty return / hall_full /
     halt_motion all clear the grace timer so the next Zwischen-Zone
     entry arms a fresh window.
  5. __init__ — _auto_between_since starts as None.
  6. _tick_safety_timeouts SAFETY_DISTANCE bypass in
     STATE_AUTO + use_flush_callback_bang_bang — the high-flow streaming
     geometry does not have a meaningful runaway-feed signal in this
     mode; SUPPLY_JAM (jam_supply_dwell_time, 1 Hz _jam_tick) is the
     correct mechanical-jam detector. SAFETY_DISTANCE remains active
     for LOAD_PHASE_3 / MANUAL_FEED / legacy AUTO (bang-bang off).
  7. _jam_tick HALL3-Drop-Grace — SUPPLY_JAM is now the sole backstop
     for AUTO+bang-bang (after stelle 6); without a drop-grace
     analogous to STABLE_DROP_GRACE in _load_phase3_tick, sub-second
     HALL3 bouncing (30-500ms false-edges, mechanically normal at
     high flow) would permanently reset _hall3_start_time before
     jam_supply_dwell_time elapses, and SUPPLY_JAM would never fire.
     Stelle 7 adds the same drop-grace pattern.

Backstop summary:
  - AUTO + use_flush_callback_bang_bang: SUPPLY_JAM (with HALL3-drop
    grace, stelle 7) is the primary backstop.
  - LOAD_PHASE_3 / MANUAL_FEED / legacy AUTO (bang-bang=False):
    SAFETY_DISTANCE remains the runaway-feed backstop.
"""

from fakes_klipper import FakeConfig, FakePrinter
from klipper_extras import buffer_feeder


def set_sensor_active(feeder, sensor_name, active):
    polarity_flip = feeder._pin_polarity_flip[sensor_name]
    raw = (not active) if polarity_flip else active
    feeder._pin_stable_state[sensor_name] = raw
    feeder._pin_raw_state[sensor_name] = raw


def make_feeder(values=None):
    base = {"use_flush_callback_bang_bang": True,
            # Empty jam_action so _trigger_jam doesn't dispatch a
            # PAUSE script during the test (would queue a reactor
            # timer; no-op for our purposes but cleaner without).
            "jam_action": ""}
    if values:
        base.update(values)
    printer = FakePrinter()
    config = FakeConfig(printer=printer, values=base)
    feeder = buffer_feeder.BufferFeeder(config)
    feeder._startup_grace_done = True
    feeder._state = buffer_feeder.STATE_AUTO
    set_sensor_active(feeder, 'hall_overflow', False)
    set_sensor_active(feeder, 'hall_full', False)
    set_sensor_active(feeder, 'hall_empty', False)
    return printer, feeder


# ---------------------------------------------------------------------------
# Test 1: hall_full edge resets accumulator
# ---------------------------------------------------------------------------

def test_hall_full_resets_accumulator():
    """When hall_full transitions to True via _on_mcu_flush, the
    accumulator must reset to 0. Pre-P7-63 only _continuous_feed was
    cleared; the accumulator persisted from the prior feed session and
    could grow past max_feed_distance over multiple hall_empty cycles
    that each saw a brief hall_full but no full reset."""
    printer, feeder = make_feeder()
    motion_q = printer.lookup_object('motion_queuing')

    # Simulate an active feed session: streaming chunks have already
    # accumulated distance (this is what _submit_single_trapezoid would
    # do per chunk).
    feeder._continuous_feed = True
    feeder._continuous_feed_direction = 1
    feeder._continuous_feed_speed = feeder.feed_speed
    feeder._feed_distance_accumulator = 1500.0
    feeder._auto_between_since = 4.5  # left over from a prior between-zone

    # Buffer arm reaches hall_full position.
    set_sensor_active(feeder, 'hall_full', True)
    motion_q.trigger_flush(flush_time=5.0, step_gen_time=5.05)

    assert feeder._continuous_feed is False, \
        "_continuous_feed must be cleared on hall_full"
    assert feeder._feed_distance_accumulator == 0.0, \
        "accumulator must be reset on hall_full (P7-63)"
    assert feeder._auto_between_since is None, \
        "_auto_between_since must be cleared on hall_full (P7-63)"


# ---------------------------------------------------------------------------
# Test 2: _halt_motion resets accumulator
# ---------------------------------------------------------------------------

def test_halt_motion_resets_accumulator():
    """_halt_motion is the central stop path for JAM/RUNOUT/PAUSE/
    CLEAR_JAM. After it runs, the accumulator must be 0 so the next
    feed session starts from a clean state — defense in depth on top
    of the session-start reset in _on_mcu_flush."""
    printer, feeder = make_feeder()

    # Simulate an active feed session in progress.
    feeder._continuous_feed = True
    feeder._continuous_feed_direction = 1
    feeder._continuous_feed_speed = feeder.feed_speed
    feeder._feed_distance_accumulator = 2500.0
    feeder._pending_remaining_mm = 42.0
    feeder._feed_deadline_time = 999.0
    feeder._auto_between_since = 7.25

    feeder._halt_motion()

    assert feeder._continuous_feed is False
    assert feeder._continuous_feed_direction == 0
    assert feeder._continuous_feed_speed == 0.0
    assert feeder._feed_distance_accumulator == 0.0, \
        "accumulator must be reset by _halt_motion (P7-63)"
    assert feeder._pending_remaining_mm == 0.0
    assert feeder._feed_deadline_time is None
    assert feeder._auto_between_since is None, \
        "_auto_between_since must be cleared by _halt_motion (P7-63)"


# ---------------------------------------------------------------------------
# Test 3: _trigger_jam routes through _halt_motion -> accumulator reset
# ---------------------------------------------------------------------------

def test_trigger_jam_resets_accumulator_via_halt_motion():
    """_trigger_jam calls _halt_motion internally. After P7-63 the
    halt path resets the accumulator, so a JAM event leaves no stale
    accumulation behind for the next session."""
    printer, feeder = make_feeder()

    feeder._continuous_feed = True
    feeder._continuous_feed_direction = 1
    feeder._continuous_feed_speed = feeder.feed_speed
    feeder._feed_distance_accumulator = 3500.0

    feeder._trigger_jam("TEST", "test message")

    assert feeder._jam_active is True
    assert feeder._feed_distance_accumulator == 0.0, \
        "_trigger_jam must reset accumulator via _halt_motion (P7-63)"
    assert feeder._continuous_feed is False


# ---------------------------------------------------------------------------
# Test 4a: SAFETY_DISTANCE backstop fires in LEGACY AUTO
# (use_flush_callback_bang_bang=False)
# ---------------------------------------------------------------------------

def test_bouncing_safety_distance_legacy_auto():
    """Backstop coverage for the LEGACY AUTO path
    (use_flush_callback_bang_bang=False): SAFETY_DISTANCE remains the
    runaway-feed safety against an over-large accumulator. The
    P7-63 stelle 6 bypass is gated on use_flush_callback_bang_bang,
    so this legacy code path still trips the safety abort.

    The original "bouncing" scenario depended on the flush-callback
    Zwischen-Zone branch firing _on_mcu_flush; that branch is a no-op
    in the legacy path (use_flush_callback_bang_bang=False short-
    circuits _on_mcu_flush at its entry guard). The remaining
    invariant we care about for legacy AUTO is simply that
    SAFETY_DISTANCE still fires.
    """
    printer, feeder = make_feeder({"use_flush_callback_bang_bang": False})
    feeder.max_feed_distance = 100.0

    feeder._continuous_feed = True
    feeder._continuous_feed_direction = 1
    feeder._continuous_feed_speed = feeder.feed_speed
    feeder._feed_distance_accumulator = 110.0

    feeder._tick_safety_timeouts(eventtime=10.0)
    assert feeder._jam_active is True, \
        "JAM_SAFETY_DISTANCE must fire in legacy AUTO when " \
        "accumulator >= max_feed_distance"


# ---------------------------------------------------------------------------
# Test 4b: SAFETY_DISTANCE backstop fires in MANUAL_FEED
# ---------------------------------------------------------------------------

def test_bouncing_safety_distance_manual():
    """Second backstop coverage: STATE_MANUAL_FEED with
    use_flush_callback_bang_bang=True. The SAFETY_DISTANCE bypass from
    P7-63 stelle 6 is gated specifically on STATE_AUTO, so other
    states (MANUAL_FEED, LOAD_PHASE_3, ...) still use SAFETY_DISTANCE
    as a backstop against runaway accumulation.
    """
    printer, feeder = make_feeder()  # use_flush_callback_bang_bang=True
    feeder._state = buffer_feeder.STATE_MANUAL_FEED
    feeder.max_feed_distance = 100.0

    feeder._continuous_feed = True
    feeder._continuous_feed_direction = 1
    feeder._continuous_feed_speed = feeder.feed_speed
    feeder._feed_distance_accumulator = 110.0

    feeder._tick_safety_timeouts(eventtime=10.0)
    assert feeder._jam_active is True, \
        "JAM_SAFETY_DISTANCE must fire in MANUAL_FEED when " \
        "accumulator >= max_feed_distance"


# ---------------------------------------------------------------------------
# Test 5a: Issue #26 reproducer — steady-flow with grace-timer ends session
# ---------------------------------------------------------------------------

def test_issue26_steady_flow_with_grace_timer_resets_session():
    """The actual Issue #26 path: a steady-flow print never asserts
    hall_full. The accumulator grows on every chunk in the hall_empty
    branch. Without the grace-timer reset, after ~100s of streaming
    (3000mm at 30mm/s default) JAM_SAFETY_DISTANCE fires falsely.

    With P7-63 stelle 3 the AUTO session ends as soon as HALL3 has
    been stably inactive (Zwischen-Zone) for >= STABLE_DROP_GRACE,
    resetting the accumulator BEFORE it can reach max_feed_distance.
    """
    printer, feeder = make_feeder()
    motion_q = printer.lookup_object('motion_queuing')

    # Phase A: hall_empty=True, accumulator grows over several chunks.
    set_sensor_active(feeder, 'hall_empty', True)
    set_sensor_active(feeder, 'hall_full', False)

    # Open the AUTO feed session via a hall_empty flush.
    feeder._current_move = None
    motion_q.trigger_flush(flush_time=0.0, step_gen_time=0.05)
    assert feeder._continuous_feed is True
    assert feeder._continuous_feed_direction == 1
    # Mirror per-chunk accumulation (real path: _submit_single_trapezoid
    # adds chunk_mm on each submit).
    feeder._feed_distance_accumulator = 200.0

    # Phase B: arm leaves hall_empty into the Zwischen-Zone — steady
    # flow, no hall_full event ever arrives.
    set_sensor_active(feeder, 'hall_empty', False)
    set_sensor_active(feeder, 'hall_full', False)

    # First Zwischen-Zone flush: grace-timer arms.
    motion_q.trigger_flush(flush_time=10.0, step_gen_time=10.05)
    assert feeder._auto_between_since == 10.0
    assert feeder._continuous_feed is True, \
        "still feeding while grace not elapsed"
    assert feeder._feed_distance_accumulator == 200.0, \
        "accumulator must NOT yet be reset (grace not elapsed)"

    # Second Zwischen-Zone flush still within grace — no reset.
    motion_q.trigger_flush(flush_time=10.3, step_gen_time=10.35)
    assert feeder._continuous_feed is True
    assert feeder._feed_distance_accumulator == 200.0

    # Third flush: now grace has elapsed (>= 0.5s since first arming).
    motion_q.trigger_flush(flush_time=10.55, step_gen_time=10.6)
    assert feeder._continuous_feed is False, \
        "AUTO feed session must end after grace expires (P7-63 stelle 3)"
    assert feeder._feed_distance_accumulator == 0.0, \
        "accumulator must be reset after grace expires (Issue #26 path)"
    assert feeder._auto_between_since is None, \
        "_auto_between_since must be cleared after grace-driven reset"
    assert feeder._jam_active is False, \
        "no false JAM should be triggered in the steady-flow path"


# ---------------------------------------------------------------------------
# Test 5b: Bouncing within grace does NOT reset; SAFETY_DISTANCE backstop
# still fires
# ---------------------------------------------------------------------------

def test_issue26_bouncing_does_not_reset_under_grace():
    """Bouncing scenario: arm oscillates between hall_empty and the
    Zwischen-Zone in short flicker (each phase < STABLE_DROP_GRACE).
    The grace-timer resets every time hall_empty returns, so the
    accumulator keeps growing. This is the regression guard against
    accidentally resetting on every short bounce.

    Note: with P7-63 stelle 6, SAFETY_DISTANCE is bypassed in
    STATE_AUTO + use_flush_callback_bang_bang, so we no longer assert
    a JAM at the end of this scenario. The SAFETY_DISTANCE backstop
    in non-AUTO-bang-bang configurations is covered by tests 4a, 4b,
    and 9.
    """
    printer, feeder = make_feeder()
    motion_q = printer.lookup_object('motion_queuing')
    feeder.max_feed_distance = 100.0

    # Open a feed session.
    set_sensor_active(feeder, 'hall_empty', True)
    set_sensor_active(feeder, 'hall_full', False)
    feeder._current_move = None
    motion_q.trigger_flush(flush_time=0.0, step_gen_time=0.05)
    assert feeder._continuous_feed is True
    feeder._feed_distance_accumulator = 30.0

    # Bouncing burst: arm leaves and returns repeatedly, each phase
    # well below STABLE_DROP_GRACE.
    t = 1.0
    for _ in range(5):
        # Leave hall_empty (Zwischen-Zone) — timer arms.
        set_sensor_active(feeder, 'hall_empty', False)
        motion_q.trigger_flush(flush_time=t, step_gen_time=t + 0.05)
        # Each leave-phase: the bouncing flicker keeps growing the
        # accumulator (real chunks were submitted in the hall_empty
        # phases).
        feeder._feed_distance_accumulator += 20.0
        # Quickly return to hall_empty within grace (<0.5s).
        t += 0.1
        set_sensor_active(feeder, 'hall_empty', True)
        feeder._current_move = None
        motion_q.trigger_flush(flush_time=t, step_gen_time=t + 0.05)
        # Returning to hall_empty MUST reset the timer so the next
        # leave-phase starts a fresh grace window.
        assert feeder._auto_between_since is None, \
            "_auto_between_since must reset on hall_empty return"
        assert feeder._continuous_feed is True, \
            "_continuous_feed must remain True across bouncing"
        t += 0.1

    # Accumulator should still be growing — never reset by grace because
    # hall_empty kept returning before 0.5s elapsed.
    assert feeder._feed_distance_accumulator >= 100.0, \
        "accumulator must keep growing across bouncing flicker"
    assert feeder._continuous_feed is True
    # SAFETY_DISTANCE in STATE_AUTO + use_flush_callback_bang_bang is
    # bypassed by P7-63 stelle 6 (Issue #26). Backstop coverage for
    # other state/config combinations lives in tests 4a, 4b, 9.


# ---------------------------------------------------------------------------
# Test 6: hall_empty return cancels the grace-timer
# ---------------------------------------------------------------------------

def test_grace_timer_resets_on_hall_empty_return():
    """When the arm enters the Zwischen-Zone and the timer arms, a
    return to hall_empty BEFORE STABLE_DROP_GRACE expires must clear
    _auto_between_since without touching the accumulator or
    _continuous_feed. This is the bouncing-tolerance behaviour."""
    printer, feeder = make_feeder()
    motion_q = printer.lookup_object('motion_queuing')

    # Active session.
    feeder._continuous_feed = True
    feeder._continuous_feed_direction = 1
    feeder._continuous_feed_speed = feeder.feed_speed
    feeder._feed_distance_accumulator = 75.0

    # Arm leaves hall_empty -> Zwischen-Zone.
    set_sensor_active(feeder, 'hall_empty', False)
    set_sensor_active(feeder, 'hall_full', False)
    motion_q.trigger_flush(flush_time=2.0, step_gen_time=2.05)
    assert feeder._auto_between_since == 2.0, \
        "timer must arm on first Zwischen-Zone flush"

    # Return to hall_empty before grace elapses. The hall_empty branch
    # may submit a new chunk (P7-61 streaming) — the accumulator may
    # therefore grow by exactly one chunk_mm, but it must NOT be reset
    # to 0 (no grace-driven reset). The key invariant under test is
    # _auto_between_since is cleared.
    accumulator_before = feeder._feed_distance_accumulator
    set_sensor_active(feeder, 'hall_empty', True)
    feeder._current_move = None  # so the hall_empty branch is happy
    motion_q.trigger_flush(flush_time=2.2, step_gen_time=2.25)

    assert feeder._auto_between_since is None, \
        "_auto_between_since must reset on hall_empty return"
    assert feeder._feed_distance_accumulator >= accumulator_before, \
        "accumulator must NOT be reset to 0 on hall_empty return"
    assert feeder._continuous_feed is True, \
        "_continuous_feed must remain True on hall_empty return"


# ---------------------------------------------------------------------------
# Test 7: hall_full cancels the grace-timer (combined hall_full reset)
# ---------------------------------------------------------------------------

def test_grace_timer_resets_on_hall_full():
    """When the arm enters the Zwischen-Zone and the timer arms, then
    transitions directly to hall_full, the hall_full-branch must clear
    _auto_between_since AND _continuous_feed AND the accumulator
    (combined P7-63 stelle 1 + stelle 3 reset)."""
    printer, feeder = make_feeder()
    motion_q = printer.lookup_object('motion_queuing')

    # Active session.
    feeder._continuous_feed = True
    feeder._continuous_feed_direction = 1
    feeder._continuous_feed_speed = feeder.feed_speed
    feeder._feed_distance_accumulator = 88.0

    # Arm into Zwischen-Zone first — arms grace timer.
    set_sensor_active(feeder, 'hall_empty', False)
    set_sensor_active(feeder, 'hall_full', False)
    motion_q.trigger_flush(flush_time=3.0, step_gen_time=3.05)
    assert feeder._auto_between_since == 3.0

    # Now into hall_full directly.
    set_sensor_active(feeder, 'hall_full', True)
    motion_q.trigger_flush(flush_time=3.1, step_gen_time=3.15)

    assert feeder._auto_between_since is None, \
        "_auto_between_since must be cleared on hall_full"
    assert feeder._continuous_feed is False, \
        "_continuous_feed must be cleared on hall_full"
    assert feeder._feed_distance_accumulator == 0.0, \
        "accumulator must be reset on hall_full"


# ---------------------------------------------------------------------------
# Test 8: Issue #26 reproducer — stable hall_empty in AUTO+bang-bang must
# NOT trip a false JAM_SAFETY_DISTANCE
# ---------------------------------------------------------------------------

def test_issue26_stable_hall_empty_no_false_jam():
    """The actual Issue #26 path that Codex Round 2 identified as
    NOT covered by stelle 1-3 alone:

    With use_flush_callback_bang_bang=True, when hall_empty stays
    stably True (buffer arm dauerhaft unten because the extruder pulls
    almost as fast as the feeder pushes, but hall_full never triggers
    because it never quite reaches the upper threshold), every
    _on_mcu_flush falls into the hall_empty branch. The Zwischen-Zone
    is never entered; the grace timer never arms; the accumulator
    grows unbounded chunk by chunk.

    Pre-stelle-6 this would trip JAM_SAFETY_DISTANCE around
    max_feed_distance and falsely abort a perfectly healthy print.

    With P7-63 stelle 6, SAFETY_DISTANCE is bypassed in
    STATE_AUTO + use_flush_callback_bang_bang. SUPPLY_JAM (via
    _jam_tick + jam_supply_dwell_time) is the correct detector for
    real mechanical jams in steady high-flow operation.
    """
    printer, feeder = make_feeder()  # AUTO + use_flush_callback_bang_bang=True
    feeder.max_feed_distance = 100.0

    # Steady-flow setup: hall_empty pinned True, hall_full never asserts.
    set_sensor_active(feeder, 'hall_empty', True)
    set_sensor_active(feeder, 'hall_full', False)

    # Active feed session, accumulator already past max_feed_distance —
    # this is exactly the state the legacy code would JAM on.
    feeder._continuous_feed = True
    feeder._continuous_feed_direction = 1
    feeder._continuous_feed_speed = feeder.feed_speed
    feeder._feed_distance_accumulator = 200.0

    feeder._tick_safety_timeouts(eventtime=42.0)

    assert feeder._jam_active is False, \
        "no false JAM_SAFETY_DISTANCE in AUTO+bang-bang steady-flow " \
        "(Issue #26 — SUPPLY_JAM is the correct detector here)"
    assert feeder._state == buffer_feeder.STATE_AUTO, \
        "state must remain STATE_AUTO; SAFETY_DISTANCE must not " \
        "halt motion in this mode"


# ---------------------------------------------------------------------------
# Test 9: SAFETY_DISTANCE backstop still active in non-AUTO load phase
# ---------------------------------------------------------------------------

def test_safety_distance_still_active_in_load_phase():
    """The P7-63 stelle 6 bypass is gated on STATE_AUTO. In load
    phases (LOAD_PHASE_3 etc.) and manual operation, SAFETY_DISTANCE
    remains the runaway-feed backstop. This test pins use_flush_callback_bang_bang
    to True (the production setting) and verifies the LOAD_PHASE_3
    state still trips the safety abort."""
    printer, feeder = make_feeder()
    feeder._state = buffer_feeder.STATE_LOAD_PHASE_3
    feeder.max_feed_distance = 100.0

    feeder._continuous_feed = True
    feeder._continuous_feed_direction = 1
    feeder._continuous_feed_speed = feeder.feed_speed
    feeder._feed_distance_accumulator = 150.0

    feeder._tick_safety_timeouts(eventtime=42.0)

    assert feeder._jam_active is True, \
        "SAFETY_DISTANCE must remain active in LOAD_PHASE_3 backstop " \
        "(P7-63 stelle 6 only bypasses STATE_AUTO+bang-bang)"


# ---------------------------------------------------------------------------
# Test 10: SAFETY_DISTANCE backstop still active in legacy AUTO
# (use_flush_callback_bang_bang=False)
# ---------------------------------------------------------------------------

def test_safety_distance_still_active_in_legacy_auto():
    """The P7-63 stelle 6 bypass is also gated on
    use_flush_callback_bang_bang. Without the flush-callback path,
    the legacy AUTO bang-bang (driven by _main_tick) does NOT have
    the high-flow streaming geometry that produces Issue #26, and
    SAFETY_DISTANCE must still serve as the runaway-feed backstop
    for any setup that hasn't enabled the flush-callback path."""
    printer, feeder = make_feeder({"use_flush_callback_bang_bang": False})
    # State remains STATE_AUTO from make_feeder().
    feeder.max_feed_distance = 100.0

    feeder._continuous_feed = True
    feeder._continuous_feed_direction = 1
    feeder._continuous_feed_speed = feeder.feed_speed
    feeder._feed_distance_accumulator = 150.0

    feeder._tick_safety_timeouts(eventtime=42.0)

    assert feeder._jam_active is True, \
        "SAFETY_DISTANCE must remain active in legacy AUTO backstop " \
        "(P7-63 stelle 6 only bypasses STATE_AUTO+bang-bang)"


# ---------------------------------------------------------------------------
# Test 11: _jam_tick HALL3-Drop-Grace tolerates brief bouncing
# ---------------------------------------------------------------------------

def test_jam_tick_drop_grace_tolerates_brief_bouncing():
    """P7-63 stelle 7: SUPPLY_JAM is the sole backstop for AUTO+bang-bang
    after stelle 6 bypassed SAFETY_DISTANCE there. At high mechanical
    flow the buffer arm bounces around the hall_empty threshold
    (sub-second false-edges, 30-500ms typical). Without the
    HALL3-Drop-Grace, every false-edge would reset _hall3_start_time
    immediately, and SUPPLY_JAM would never accumulate enough dwell
    to fire on a real spool jam.

    With stelle 7, brief bouncing < STABLE_DROP_GRACE (0.5s) leaves
    _hall3_start_time intact — only _hall3_drop_since is armed."""
    _, feeder = make_feeder()
    feeder._print_running = True  # _jam_tick gates on this
    feeder._continuous_feed = True
    feeder._continuous_feed_direction = 1
    feeder._continuous_feed_speed = feeder.feed_speed
    set_sensor_active(feeder, 'hall_empty', True)

    # Arm _hall3_start_time on first tick.
    feeder._jam_tick(eventtime=100.0)
    armed_at = feeder._hall3_start_time
    assert armed_at == 100.0
    assert feeder._hall3_drop_since is None

    # Second tick — still hall_empty, still under jam_supply_dwell_time
    # (default 120s), no JAM yet.
    feeder._jam_tick(eventtime=100.5)
    assert feeder._hall3_start_time == 100.0
    assert feeder._jam_active is False

    # Brief bounce: hall_empty=False for one tick (<0.5s grace).
    set_sensor_active(feeder, 'hall_empty', False)
    feeder._jam_tick(eventtime=100.7)
    assert feeder._hall3_start_time == 100.0, \
        "_hall3_start_time must NOT be reset by a brief drop (P7-63 stelle 7)"
    assert feeder._hall3_drop_since == 100.7, \
        "_hall3_drop_since must arm on first false-edge tick"
    assert feeder._jam_active is False

    # Bounce ends, hall_empty back True within grace.
    set_sensor_active(feeder, 'hall_empty', True)
    feeder._jam_tick(eventtime=101.0)
    assert feeder._hall3_start_time == 100.0, \
        "_hall3_start_time must remain the original arm time"
    assert feeder._hall3_drop_since is None, \
        "_hall3_drop_since must clear on hall_empty return"
    assert feeder._jam_active is False


# ---------------------------------------------------------------------------
# Test 12: _jam_tick HALL3-Drop-Grace resets on long drop
# ---------------------------------------------------------------------------

def test_jam_tick_drop_grace_resets_on_long_drop():
    """Once the drop is sustained beyond STABLE_DROP_GRACE (0.5s),
    the dwell tracker hard-resets — that's correct behaviour, the
    arm has genuinely left HALL3 territory and a fresh dwell window
    starts when it returns."""
    _, feeder = make_feeder()
    feeder._print_running = True
    feeder._continuous_feed = True
    feeder._continuous_feed_direction = 1
    feeder._continuous_feed_speed = feeder.feed_speed
    set_sensor_active(feeder, 'hall_empty', True)

    # Arm.
    feeder._jam_tick(eventtime=200.0)
    assert feeder._hall3_start_time == 200.0

    # Drop edge — drop-since arms.
    set_sensor_active(feeder, 'hall_empty', False)
    feeder._jam_tick(eventtime=200.2)
    assert feeder._hall3_start_time == 200.0
    assert feeder._hall3_drop_since == 200.2

    # Still dropped, < STABLE_DROP_GRACE — no reset yet.
    feeder._jam_tick(eventtime=200.5)
    assert feeder._hall3_start_time == 200.0
    assert feeder._hall3_drop_since == 200.2

    # Now beyond grace (>= 0.5s since drop_since=200.2) — hard reset.
    feeder._jam_tick(eventtime=200.8)
    assert feeder._hall3_start_time is None, \
        "_hall3_start_time must reset after sustained drop > grace"
    assert feeder._hall3_drop_since is None, \
        "_hall3_drop_since must clear together with start_time"
    assert feeder._jam_active is False


# ---------------------------------------------------------------------------
# Test 13: SUPPLY_JAM fires in AUTO+bang-bang after dwell
# ---------------------------------------------------------------------------

def test_supply_jam_fires_in_auto_bang_bang_after_dwell():
    """Backstop validation: after stelle 6 bypassed SAFETY_DISTANCE
    in STATE_AUTO+use_flush_callback_bang_bang, SUPPLY_JAM is the
    primary mechanical-jam detector. With a stable hall_empty signal
    and feeder running forward, SUPPLY_JAM must fire once dwell
    exceeds jam_supply_dwell_time (here shortened to 0.5s for
    test speed)."""
    _, feeder = make_feeder()
    feeder._print_running = True
    feeder._continuous_feed = True
    feeder._continuous_feed_direction = 1
    feeder._continuous_feed_speed = feeder.feed_speed
    feeder.jam_supply_dwell_time = 0.5  # shorten for fast test
    set_sensor_active(feeder, 'hall_empty', True)

    # Arm.
    feeder._jam_tick(eventtime=300.0)
    assert feeder._hall3_start_time == 300.0
    assert feeder._jam_active is False

    # Below dwell threshold — no JAM yet.
    feeder._jam_tick(eventtime=300.3)
    assert feeder._jam_active is False

    # Dwell exceeded (>= 0.5s) — SUPPLY_JAM must fire.
    feeder._jam_tick(eventtime=300.6)
    assert feeder._jam_active is True, \
        "SUPPLY_JAM must fire as primary backstop in AUTO+bang-bang " \
        "(P7-63 stelle 6 bypassed SAFETY_DISTANCE there)"
    assert feeder._state == buffer_feeder.STATE_JAM, \
        "_trigger_jam must transition state to STATE_JAM"
