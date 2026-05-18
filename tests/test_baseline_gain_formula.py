"""Linear gain formula for BUFFER_BASELINE_RUN macro.

The macro computes hall3_demand_gain per case via:

    auto = 1.5                              if flow <= 50
    auto = 1.5 - 0.01 * (flow - 50)         if flow > 50
    auto = max(auto, 1.2)                   # floor

Replicated here as plain Python so the formula stays under unit-test
coverage independently of Jinja2. Hardware anchor 2026-05-18:
flow=50 mm3/s clean (44/45 samples, 0 HALL1), flow=60 mm3/s
7-8 HALL1 overflows. Lower gain at higher flow = smaller absolute
over-push to fit the 3.6 mm HALL2->HALL1 mechanical margin.
"""

import pytest


def baseline_hall3_gain(flow_mm3s):
    """Replicate the lll.cfg BUFFER_BASELINE_RUN expression."""
    if flow_mm3s <= 50.0:
        auto = 1.5
    else:
        auto = 1.5 - 0.01 * (flow_mm3s - 50.0)
    if auto < 1.2:
        auto = 1.2
    return auto


@pytest.mark.parametrize("flow,expected", [
    (5.0, 1.50),     # Trivial low
    (24.0, 1.50),    # high_flow_mm3s_threshold edge
    (30.0, 1.50),    # Hardware c001 attempt (caveats apply)
    (45.0, 1.50),    # Still in safe zone
    (50.0, 1.50),    # Sweet-spot anchor (c009/c010 clean)
    (51.0, 1.49),    # Above the knee, linear ramp begins
    (55.0, 1.45),
    (60.0, 1.40),    # Hardware c011/c012 (overflow zone) — reduced
    (70.0, 1.30),
    (80.0, 1.20),    # Floor reached exactly
    (100.0, 1.20),   # Floor clamped, would otherwise be 1.0
    (200.0, 1.20),   # Far above floor: still clamped
])
def test_baseline_gain_matches_macro(flow, expected):
    assert baseline_hall3_gain(flow) == pytest.approx(expected, abs=1e-9)


def test_gain_is_monotonically_decreasing_above_knee():
    flows = [50.0, 55.0, 60.0, 65.0, 70.0, 75.0, 80.0]
    gains = [baseline_hall3_gain(f) for f in flows]
    for prev, cur in zip(gains, gains[1:]):
        assert cur <= prev, ("non-monotonic: %s" % gains)


def test_gain_floored_at_1_2():
    for flow in (80.0, 100.0, 500.0, 1000.0):
        assert baseline_hall3_gain(flow) == pytest.approx(1.2, abs=1e-9)


def test_gain_constant_below_knee():
    for flow in (0.1, 10.0, 20.0, 30.0, 40.0, 50.0):
        assert baseline_hall3_gain(flow) == pytest.approx(1.5, abs=1e-9)


def test_absolute_over_push_shrinks_with_higher_flow():
    """The intent: keep absolute over-push (vel*(gain-1)) below the
    mechanical HALL2->HALL1 threshold. Sanity check: at flow=60 the
    over-push must be measurably smaller than at flow=50."""
    AREA = 3.141592653589793 * (1.75 / 2.0) ** 2  # ~2.405 mm^2
    over_push_50 = (50.0 / AREA) * (baseline_hall3_gain(50.0) - 1.0)
    over_push_60 = (60.0 / AREA) * (baseline_hall3_gain(60.0) - 1.0)
    over_push_80 = (80.0 / AREA) * (baseline_hall3_gain(80.0) - 1.0)
    # 50 mm3/s: vel=20.8 mm/s * 0.5 = 10.4 mm/s baseline.
    # 60 mm3/s: vel=24.95 * 0.4 = 9.98 mm/s.
    # 80 mm3/s: vel=33.27 * 0.2 = 6.65 mm/s.
    assert over_push_60 < over_push_50
    assert over_push_80 < over_push_60
