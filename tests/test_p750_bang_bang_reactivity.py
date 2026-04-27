"""P7-50 — Bang-bang submits on mcu_now, not toolhead-end-of-move.

Hardware-Test 2026-04-27 (operator note): "ich foerdere 50mm mit dem
extruder ... aber der buffer zieht das filament erst nach, nachdem
der extruder damit fertig ist und nicht wo hall 3 (buffer leer)
betaetigt wurde".

Diagnosis:

In _submit_single_trapezoid's first-chunk-after-idle path, t0 was
anchored to toolhead.get_last_move_time(). When the user runs a
G1 E50 @ 5mm/s = 10s, that's 10s into the future. The bang-bang
move submitted on HALL3-trigger thus lands at t0 = mcu_now + 10s,
and _move_in_flight() reports True for the next 10s — bang-bang
"thinks" it already has a move queued and never submits another.

The buffer-stepper physically only starts moving once the extruder's
G1 E50 finishes — exactly the user-observed lag.

Fix: P7-50 introduces a TH_AHEAD_THRESHOLD (1s). If the toolhead
is more than 1s ahead of mcu_now, anchor t0 to mcu_now instead —
the toolhead is mid-move, and our buffer-stepper has its own
stepcompress cursor (separate from toolhead.extruder), so anchoring
to mcu_now is safe as long as the cursor isn't stale (which is
covered by the existing flush_step_generation reprime when gap > 5s).

Same pattern applied to _schedule_time_for_enable_toggle.
"""

import pytest
from fakes_klipper import FakeConfig, FakePrinter
from klipper_extras import buffer_feeder


def make_feeder(values=None):
    printer = FakePrinter()
    config = FakeConfig(printer=printer, values=values)
    feeder = buffer_feeder.BufferFeeder(config)
    feeder._startup_grace_done = True
    # HALL1 must be inactive — otherwise _submit_move rejects forward
    # chunks. Default _pin_stable_state is the polarity-flipped active
    # state which yields hall_overflow=True.
    polarity_flip = feeder._pin_polarity_flip['hall_overflow']
    feeder._pin_stable_state['hall_overflow'] = True if polarity_flip else False
    feeder._pin_raw_state['hall_overflow'] = True if polarity_flip else False
    return printer, feeder


def test_submit_anchors_to_mcu_now_when_toolhead_mid_move():
    """Toolhead is 10s in the future (active G1 E50). Buffer-stepper
    submit must NOT anchor there — would delay the move 10s."""
    printer, feeder = make_feeder()
    motion_q = printer.lookup_object('motion_queuing')
    toolhead = printer.lookup_object('toolhead')

    # Pin reactor's monotonic so mcu_now == 1.0 (FakeMCU returns
    # eventtime as-is). Set toolhead's get_last_move_time 10s ahead.
    feeder.reactor.now = 1.0
    toolhead.last_move_time = 11.0    # 10s in the future
    feeder._last_move_end_time = 0.0   # no recent buffer move

    appends_before = len(motion_q.append_calls)

    # Bang-bang submits a 10mm forward chunk
    feeder._submit_move(10.0, 30.0)

    new_appends = motion_q.append_calls[appends_before:]
    own_appends = [c for c in new_appends if c[0] is feeder.trapq]
    assert own_appends, "expected buffer-stepper trapq submit"
    # trapq_append signature: (trapq, t0, accel_t, cruise_t,
    # decel_t, start_pos_x, ...)
    t0 = own_appends[0][1]
    # P7-50: t0 should be near mcu_now (1.0 + lead_time ~= 1.3),
    # NOT near toolhead end (11.0 + lead_time ~= 11.3).
    assert t0 < 5.0, (
        "P7-50 broken: t0=%.3f but toolhead is at 11.0 — bang-bang "
        "would wait for the toolhead instead of reacting "
        "immediately to HALL3" % t0)


def test_submit_anchors_to_toolhead_when_toolhead_idle():
    """When the toolhead is at print_time = mcu_now (idle, no move
    in progress), the original anchor pattern still applies — t0
    near toolhead's last_move_time as the safeguard for first-step-
    after-idle."""
    printer, feeder = make_feeder()
    motion_q = printer.lookup_object('motion_queuing')
    toolhead = printer.lookup_object('toolhead')

    feeder.reactor.now = 5.0
    toolhead.last_move_time = 5.0     # toolhead idle, at mcu_now
    feeder._last_move_end_time = 0.0

    appends_before = len(motion_q.append_calls)
    feeder._submit_move(10.0, 30.0)
    new_appends = motion_q.append_calls[appends_before:]
    own_appends = [c for c in new_appends if c[0] is feeder.trapq]
    t0 = own_appends[0][1]
    # th_time + lead_time = 5.3, mcu_now + lead_time = 5.3
    # both paths should pick t0 ~ 5.3
    assert 5.0 <= t0 <= 6.0


def test_submit_anchors_to_mcu_now_at_threshold_boundary():
    """At exactly TH_AHEAD_THRESHOLD (1.0s), the threshold is
    inclusive of "still mid-move" — anchor to mcu_now."""
    printer, feeder = make_feeder()
    motion_q = printer.lookup_object('motion_queuing')
    toolhead = printer.lookup_object('toolhead')

    feeder.reactor.now = 5.0
    toolhead.last_move_time = 6.5     # 1.5s ahead — definitely mid-move
    feeder._last_move_end_time = 0.0

    appends_before = len(motion_q.append_calls)
    feeder._submit_move(10.0, 30.0)
    new_appends = motion_q.append_calls[appends_before:]
    own_appends = [c for c in new_appends if c[0] is feeder.trapq]
    t0 = own_appends[0][1]
    # Threshold (1s) crossed → anchor to mcu_now (5.0 + 0.3 = 5.3)
    assert t0 < 6.0


def test_submit_streaming_path_unchanged():
    """If a buffer-stepper move is already queued in the future
    (_last_move_end_time > mcu_now + lead_time), use that as the
    anchor (streaming-pfad). Toolhead-state irrelevant in this path."""
    printer, feeder = make_feeder()
    motion_q = printer.lookup_object('motion_queuing')
    toolhead = printer.lookup_object('toolhead')

    feeder.reactor.now = 5.0
    feeder._last_move_end_time = 7.0    # buffer-move ends at 7.0
    toolhead.last_move_time = 100.0    # toolhead irrelevant

    appends_before = len(motion_q.append_calls)
    feeder._submit_move(10.0, 30.0)
    new_appends = motion_q.append_calls[appends_before:]
    own_appends = [c for c in new_appends if c[0] is feeder.trapq]
    t0 = own_appends[0][1]
    # Streaming-Pfad: t0 = _last_move_end_time = 7.0
    assert t0 == 7.0


def test_enable_toggle_anchors_to_mcu_now_when_toolhead_mid_move():
    """Same pattern in _schedule_time_for_enable_toggle: when the
    toolhead is mid-move, the enable/disable toggle's print_time
    must not be deferred to the toolhead's future end."""
    printer, feeder = make_feeder()
    toolhead = printer.lookup_object('toolhead')

    feeder.reactor.now = 5.0
    toolhead.last_move_time = 15.0     # 10s ahead
    feeder._last_move_end_time = 0.0
    feeder._last_enable_schedule_time = 0.0

    pt = feeder._schedule_time_for_enable_toggle()

    # mcu_now + lead_time = 5.3 — should be the floor.
    # Without P7-50: pt would have been 15.3.
    assert pt < 7.0, (
        "P7-50 broken: enable toggle at %.3f but toolhead is at 15.0 "
        "— would defer the enable 10s into the future" % pt)
