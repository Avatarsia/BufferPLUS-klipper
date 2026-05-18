"""Constant hall3_demand_gain in BUFFER_BASELINE_RUN macro.

The 2026-05-18 baseline run with the linear gain-down strategy showed
that gain reduction at higher flows did NOT prevent HALL1 overflow
(flow=60/70/80 all hit reason=overflow). Subagent + main analyzer
agreed: gain is irrelevant for HALL1 avoidance because queued sub-
chunks are unabbrechbar — _halt_motion cannot abort an in-flight move
on the trapq. The real lever is interrupt_chunk_mm (now 3 mm in cfg).

This test pins the constant-gain behaviour so a future linear or
flow-dependent strategy must consciously re-introduce it.
"""

import pytest


def baseline_hall3_gain(flow_mm3s, user_override=None):
    """Replicate the lll.cfg BUFFER_BASELINE_RUN expression."""
    if user_override is not None:
        return float(user_override)
    return 1.5


@pytest.mark.parametrize("flow", [5.0, 24.0, 30.0, 45.0, 50.0, 60.0, 70.0, 80.0, 100.0])
def test_gain_is_constant_1_5_across_flow_range(flow):
    """No flow-dependent reduction — gain stays 1.5 unconditionally.

    Hardware fact (2026-05-18 run): flow=50 ran clean with gain=1.5,
    flow=60+ hit HALL1 overflow regardless of gain. Reducing gain at
    high flow brought no benefit. interrupt_chunk_mm=3 (cfg) is the
    actual mitigation for HALL1 latency."""
    assert baseline_hall3_gain(flow) == pytest.approx(1.5, abs=1e-9)


def test_user_override_wins():
    """params.HALL3_DEMAND_GAIN= explicit user override bypasses the
    constant default. Lets the operator experiment without editing the
    macro source."""
    assert baseline_hall3_gain(60.0, user_override=1.2) == pytest.approx(1.2)
    assert baseline_hall3_gain(60.0, user_override=2.0) == pytest.approx(2.0)


def test_high_flow_gain_not_lowered():
    """Regression guard: never re-introduce a flow-dependent reduction
    without a corresponding hardware-validated reason. The linear ramp
    from 2026-05-18 morning was reverted same-day after it failed to
    prevent HALL1 overflow."""
    for flow in (60.0, 70.0, 80.0, 100.0):
        assert baseline_hall3_gain(flow) >= 1.5
