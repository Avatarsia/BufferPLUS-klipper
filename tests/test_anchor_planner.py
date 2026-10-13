"""Pure decisions independent of Klipper and mutable feeder state."""
from dataclasses import replace

import pytest

from klipper_extras.buffer_timing import (
    AnchorSnapshot, ToolheadAnchorRequest, ToolheadAnchorSnapshot,
    finish_toolhead_anchor, prepare_anchor,
)


def snapshot(**changes):
    values = dict(forced_t0=None, mcu_now=10.0, was_primed=True,
                  need_reprime=False, streaming=False, has_current_move=False,
                  current_move_end=0.0, last_move_end_time=9.0,
                  last_enable_schedule_time=10.2, lead_time=0.1,
                  max_t0_lookahead=2.0)
    values.update(changes)
    return AnchorSnapshot(**values)


@pytest.mark.parametrize('forced,expected,clamped', [
    (10.5, 10.5, False), (12.0, 12.0, False), (12.00001, 10.2, True),
])
def test_forced_boundary_and_floors(forced, expected, clamped):
    data = snapshot(forced_t0=forced)
    plan = prepare_anchor(data)
    assert plan.t0 == expected
    assert plan.forced_t0_clamped is clamped
    assert plan == prepare_anchor(data)


@pytest.mark.parametrize('forced,lme,reprime,has_move,expected_floor', [
    (10.5, 9.0, False, True, 0.0),
    (None, 9.0, False, True, 10.2),
    (None, 10.5, False, True, 0.0),
    (10.5, 10.5, True, True, 10.2),
    (10.5, 10.5, False, False, 10.2),
])
def test_streaming_floor_distinguishes_reactor_and_flush(
        forced, lme, reprime, has_move, expected_floor):
    result = prepare_anchor(snapshot(
        forced_t0=forced, last_move_end_time=lme, need_reprime=reprime,
        has_current_move=has_move, streaming=True))
    assert result.enable_floor == expected_floor


def test_forced_current_move_floor_survives_halt_rollback():
    plan = prepare_anchor(snapshot(forced_t0=10.1, has_current_move=True,
                                   current_move_end=10.9))
    assert plan.t0 == 10.9


@pytest.mark.parametrize('th_time,skipped', [(11.9, False), (11.90001, True)])
def test_toolhead_far_future_skips_instead_of_clamping(th_time, skipped):
    request = prepare_anchor(snapshot())
    assert isinstance(request, ToolheadAnchorRequest)
    post = ToolheadAnchorSnapshot(th_time, 9.0, 'silent', 0.1)
    plan = finish_toolhead_anchor(request, post)
    assert (plan.t0 is None) is skipped
    assert plan.rate_limit_idle_anchor is skipped
    if skipped:
        assert plan.skip_reason == 'far_future_toolhead_anchor'
        assert plan.skipped_t0 == pytest.approx(th_time + 0.1)


def test_post_lookup_stale_lme_produces_clamp_plan_without_mutation():
    before = snapshot()
    request = prepare_anchor(before)
    post = ToolheadAnchorSnapshot(10.0, 30.0, 'silent', 0.1)
    plan = finish_toolhead_anchor(request, post)
    assert plan.t0 is None
    assert plan.clamp_last_move_end_time == 10.0
    assert post.last_move_end_time == 30.0
    assert before == snapshot()
    assert plan == finish_toolhead_anchor(request, post)


def test_silent_floor_uses_post_lookup_live_lead_time():
    request = prepare_anchor(snapshot(last_enable_schedule_time=0.0))
    post = ToolheadAnchorSnapshot(1.0, 9.0, 'silent', 0.3)
    assert finish_toolhead_anchor(request, post).t0 == 10.3
    assert finish_toolhead_anchor(request, replace(post, idle_anchor_mode='move')).t0 == 9.0
