"""P7-46 — Issue #16 Re-Test fixes.

Three orthogonal fixes from Codex audit of klippy.log.txt after the
P7-45 hardware test failed:

A) BUFFER_AUTO_ON_IF_READY: macro-render-time vs runtime fix.
   Klipper renders Jinja macros once at start, so a guard like
   `{% if bf.hall_overflow %} skip {% else %} BUFFER_AUTO_ON {% endif %}`
   uses a stale snapshot. The new _IF_READY command does the
   precondition check in Python at runtime — block-reason → return
   without raising.

B) sync_to_extruder gap-reprime: an idle period > CLOCK_DIFF_MAX (~16.7s)
   between the last buffer-stepper move and the next BUFFER_SYNC_TO_
   EXTRUDER call leaves the own-trapq stepcompress cursor stale.
   The first extruder-step after the trapq-swap then has a print_time
   far ahead of last_step_clock → 'stepcompress Invalid sequence'
   crash. Fix: anchor-step on own-trapq before the swap when gap
   exceeds REPRIME_GAP=5s.

C) Post-LOAD HALL1 grace: after Phase 3 with overflow_ok=1 exits
   via stable HALL1 ("treating as full"), the buffer is legitimately
   overfilled. Without a guard, _main_tick would re-trigger
   _enter_overflow on the next cycle and bounce IDLE/AUTO →
   STATE_OVERFLOW. The grace flag suppresses that re-trigger until
   HALL1 actually falls.
"""

import pytest

from klipper_extras import buffer_feeder


def set_sensor_active(feeder, sensor_name, active):
    polarity_flip = feeder._pin_polarity_flip[sensor_name]
    feeder._pin_stable_state[sensor_name] = (not active) if polarity_flip else active


# ---------------------------------------------------------------------------
# Fix A — BUFFER_AUTO_ON_IF_READY
# ---------------------------------------------------------------------------


class FakeGCmdLocal:
    def __init__(self, values=None):
        self.values = {key.upper(): value for key, value in (values or {}).items()}

    def get(self, key, default=None):
        return self.values.get(key.upper(), default)

    def get_int(self, key, default=None, **kwargs):
        return int(self.values.get(key.upper(), default))

    def get_float(self, key, default=None, **kwargs):
        return float(self.values.get(key.upper(), default))


def test_buffer_auto_on_if_ready_engages_when_ready(feeder):
    """Happy path: HALL1 inactive, no JAM, no PAUSE — engages AUTO."""
    set_sensor_active(feeder, 'entrance', True)
    set_sensor_active(feeder, 'hall_overflow', False)

    feeder.cmd_BUFFER_AUTO_ON_IF_READY(FakeGCmdLocal())

    assert feeder._state == buffer_feeder.STATE_AUTO


def test_buffer_auto_on_if_ready_skips_silently_when_hall1_active(fake_printer, feeder):
    """Block-reason path: HALL1 active. Hard cmd_BUFFER_AUTO_ON would
    raise — _IF_READY logs and returns. State stays unchanged."""
    feeder._state = buffer_feeder.STATE_IDLE
    set_sensor_active(feeder, 'hall_overflow', True)
    gcode = fake_printer.lookup_object('gcode')

    feeder.cmd_BUFFER_AUTO_ON_IF_READY(FakeGCmdLocal())

    assert feeder._state == buffer_feeder.STATE_IDLE
    assert any("AUTO not engaged" in msg for msg in gcode.info_messages)


def test_buffer_auto_on_hard_command_still_raises_on_hall1(feeder):
    """The original BUFFER_AUTO_ON command must keep the hard-fail
    semantics so direct user invocations still surface the issue."""
    set_sensor_active(feeder, 'hall_overflow', True)

    with pytest.raises(Exception, match="HALL1 overflow active"):
        feeder.cmd_BUFFER_AUTO_ON(FakeGCmdLocal())


# ---------------------------------------------------------------------------
# Fix B — sync_to_extruder gap-reprime
# ---------------------------------------------------------------------------


def test_sync_to_extruder_anchors_cursor_after_long_idle(fake_printer, feeder):
    """If the last buffer move was > REPRIME_GAP (5s) ago, sync_to_
    extruder must anchor the own-trapq cursor before the swap to
    prevent 'Invalid sequence' on the first extruder-driven step."""
    motion_q = fake_printer.lookup_object('motion_queuing')

    # Fake a long idle: last move ended way in the past relative to
    # the FakeMCU's print-time clock. FakeMCU.estimated_print_time
    # returns eventtime as-is, FakeReactor.monotonic() advances per
    # call.
    feeder._last_move_end_time = 0.0  # very old
    # Push reactor's clock forward to simulate idle.
    feeder.reactor.now = 20.0  # 20 seconds in the future

    baseline_appends = len(motion_q.append_calls)

    feeder._sync_to_extruder('extruder')

    # Anchor-step should have appended a move on own-trapq BEFORE
    # the trapq-swap.
    new_appends = motion_q.append_calls[baseline_appends:]
    own_trapq_appends = [c for c in new_appends if c[0] is feeder.trapq]
    assert own_trapq_appends, (
        "sync_to_extruder did not anchor on own-trapq despite "
        "%.1fs gap" % feeder.reactor.now)


def test_sync_to_extruder_no_anchor_on_short_gap(fake_printer, feeder):
    """If the gap is below REPRIME_GAP, no anchor-step needed —
    avoids unnecessary buffer-stepper motion."""
    motion_q = fake_printer.lookup_object('motion_queuing')

    # Recent move — gap is small.
    feeder.reactor.now = 1.0
    feeder._last_move_end_time = 0.5  # 0.5s ago, well below 5s gap

    baseline_appends = len(motion_q.append_calls)

    feeder._sync_to_extruder('extruder')

    new_appends = motion_q.append_calls[baseline_appends:]
    own_trapq_appends = [c for c in new_appends if c[0] is feeder.trapq]
    assert not own_trapq_appends, (
        "sync_to_extruder anchored unnecessarily despite tiny gap")


def test_sync_to_extruder_anchor_direction_follows_hall1(fake_printer, feeder):
    """Anchor direction is -1 (retract) when HALL1 active, +1 (feed)
    otherwise. Avoids forward-feed when the buffer is already
    overfilled."""
    motion_q = fake_printer.lookup_object('motion_queuing')

    set_sensor_active(feeder, 'hall_overflow', True)
    feeder._last_move_end_time = 0.0
    feeder.reactor.now = 20.0

    baseline_appends = len(motion_q.append_calls)

    feeder._sync_to_extruder('extruder')

    new_appends = motion_q.append_calls[baseline_appends:]
    own_trapq_appends = [c for c in new_appends if c[0] is feeder.trapq]
    assert own_trapq_appends, "expected anchor-step"
    # The trapq_append signature: (trapq, t0, accel_t, cruise_t,
    # decel_t, start_pos_x, ..., axes_r_x, ...). axes_r_x is at
    # index 8 and carries the direction sign.
    first_anchor = own_trapq_appends[0]
    axes_r_x = first_anchor[8]
    assert axes_r_x < 0, (
        "anchor-step went forward (axes_r_x=%s) despite HALL1 "
        "active — would push the overfilled buffer further" % axes_r_x)


# ---------------------------------------------------------------------------
# Fix C — Post-LOAD HALL1 grace
# ---------------------------------------------------------------------------


def test_post_load_grace_set_on_phase3_overflow_treating_as_full(feeder):
    """When LOAD_PHASE_3 with overflow_ok=1 exits via stable HALL1,
    the grace flag must be set so main_tick doesn't bounce state
    back to OVERFLOW."""
    feeder._state = buffer_feeder.STATE_LOAD_PHASE_3
    feeder._load_phase3_overflow_ok = True
    feeder._load_phase3_stable_timeout = 1.0
    feeder._load_phase3_hall_overflow_since = 0.0
    set_sensor_active(feeder, 'hall_overflow', True)
    feeder._pin_stable_state['entrance'] = True

    # Run the tick after the stable period elapsed.
    feeder._load_phase3_tick(eventtime=2.0)

    assert feeder._post_load_overflow_grace is True


def test_post_load_grace_blocks_main_tick_re_enter_overflow(feeder):
    """With grace set, _is_hall1_active('main_tick') must return
    False even though hall_overflow is True."""
    feeder._state = buffer_feeder.STATE_AUTO
    feeder._post_load_overflow_grace = True
    set_sensor_active(feeder, 'hall_overflow', True)

    assert feeder._is_hall1_active('main_tick') is False


def test_post_load_grace_clears_on_hall1_fall(feeder):
    """Once HALL1 actually falls, the sensor-callback path clears
    the grace and returns to normal HALL1-lockout regime."""
    feeder._startup_grace_done = True   # callback only fires after grace
    feeder._state = buffer_feeder.STATE_AUTO
    feeder._post_load_overflow_grace = True
    set_sensor_active(feeder, 'hall_overflow', False)

    feeder._on_stable_sensor_change(eventtime=0.0,
                                     name='hall_overflow',
                                     raw_state=False)

    assert feeder._post_load_overflow_grace is False


def test_post_load_grace_clears_on_auto_off(feeder):
    """Operator-explicit AUTO_OFF clears the grace too — any later
    HALL1-active spike must trigger the normal lockout."""
    feeder._state = buffer_feeder.STATE_AUTO
    feeder._post_load_overflow_grace = True

    feeder.cmd_BUFFER_AUTO_OFF(FakeGCmdLocal())

    assert feeder._post_load_overflow_grace is False


def test_post_load_grace_clears_on_stop_buffer_fill(feeder):
    feeder._state = buffer_feeder.STATE_AUTO
    feeder._post_load_overflow_grace = True

    feeder.cmd_STOP_BUFFER_FILL(FakeGCmdLocal())

    assert feeder._post_load_overflow_grace is False


def test_post_load_grace_does_not_block_lockout_for_check_auto_ready(feeder):
    """The grace must NOT mask HALL1 active for _check_auto_ready
    (used by direct user-AUTO_ON). The _IF_READY variant respects
    the block-reason; only main_tick re-trigger is suppressed."""
    feeder._post_load_overflow_grace = True
    set_sensor_active(feeder, 'hall_overflow', True)

    # auto_on context bypasses the grace — still blocked.
    assert feeder._is_hall1_active('auto_on') is True


def test_post_load_grace_exposed_in_get_status(feeder):
    feeder._post_load_overflow_grace = True

    status = feeder.get_status(0.0)

    assert status['post_load_overflow_grace'] is True
