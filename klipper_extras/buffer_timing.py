"""Pure anchor decisions; the feeder owns lookup timing and plan effects."""
from dataclasses import dataclass
from typing import Optional

from .buffer_types import AnchorPlan


@dataclass(frozen=True)
class AnchorSnapshot:
    forced_t0: Optional[float]
    mcu_now: float
    was_primed: bool
    need_reprime: bool
    streaming: bool
    has_current_move: bool
    current_move_end: float
    last_move_end_time: float
    last_enable_schedule_time: float
    lead_time: float
    max_t0_lookahead: float


@dataclass(frozen=True)
class ToolheadAnchorRequest:
    mcu_now: float
    enable_floor: float
    current_end_floor: float
    max_t0_lookahead: float


@dataclass(frozen=True)
class ToolheadAnchorSnapshot:
    toolhead_time: float
    last_move_end_time: float
    idle_anchor_mode: str
    lead_time: float


def prepare_anchor(snapshot):
    """Return a plan or a request for the existing lazy toolhead lookup."""
    stale_anchor = snapshot.last_move_end_time <= snapshot.mcu_now
    drop_enable_floor = False
    if (snapshot.streaming and snapshot.has_current_move
            and snapshot.was_primed and not snapshot.need_reprime):
        # Forced streaming already proved generator progress in the caller.
        # Legacy streaming must still protect a stale wall-clock anchor.
        drop_enable_floor = (snapshot.forced_t0 is not None or not stale_anchor)
    en = 0.0 if drop_enable_floor else snapshot.last_enable_schedule_time
    current_end_floor = 0.0
    if (snapshot.has_current_move
            and snapshot.current_move_end > snapshot.mcu_now):
        # HALT may roll back lme without cancelling already generated steps.
        current_end_floor = snapshot.current_move_end

    if snapshot.forced_t0 is not None:
        forced_t0 = snapshot.forced_t0
        clamped = forced_t0 > snapshot.mcu_now + snapshot.max_t0_lookahead
        if clamped:
            forced_t0 = snapshot.mcu_now + snapshot.lead_time
        return AnchorPlan(
            t0=max(forced_t0, snapshot.last_move_end_time, en,
                   snapshot.mcu_now, current_end_floor),
            enable_floor=en, forced_t0_clamped=clamped)

    if snapshot.last_move_end_time > snapshot.mcu_now + snapshot.lead_time:
        return AnchorPlan(t0=max(snapshot.last_move_end_time, en),
                          enable_floor=en)

    return ToolheadAnchorRequest(snapshot.mcu_now, en, current_end_floor,
                                 snapshot.max_t0_lookahead)


def finish_toolhead_anchor(request, snapshot):
    """Use post-lookup live values with the pre-lookup enable/move floors."""
    t0 = max(snapshot.toolhead_time + snapshot.lead_time,
             snapshot.last_move_end_time, request.enable_floor,
             request.current_end_floor)
    if snapshot.idle_anchor_mode == 'silent':
        t0 = max(t0, request.mcu_now + snapshot.lead_time)
    if t0 > request.mcu_now + request.max_t0_lookahead:
        # Clamping would go behind an already advanced last_step_clock.
        # Skip and let the flush callback supply its race-free anchor.
        return AnchorPlan(
            t0=None, skip_reason='far_future_toolhead_anchor',
            rate_limit_idle_anchor=True,
            clamp_last_move_end_time=(
                request.mcu_now
                if snapshot.last_move_end_time > request.mcu_now + request.max_t0_lookahead
                else None),
            toolhead_time=snapshot.toolhead_time,
            enable_floor=request.enable_floor, skipped_t0=t0)
    return AnchorPlan(t0=t0, toolhead_time=snapshot.toolhead_time,
                      enable_floor=request.enable_floor)
