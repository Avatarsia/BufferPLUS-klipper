from helpers import FakeGCmd, set_sensor_active
from klipper_extras import buffer_feeder


def test_buffer_prep_baseline_feeds_from_h3_to_neutral(feeder, monkeypatch):
    set_sensor_active(feeder, "hall_empty", True)
    set_sensor_active(feeder, "hall_full", False)
    set_sensor_active(feeder, "hall_overflow", False)

    moves = []

    def fake_submit(distance, speed, forced_t0=None, streaming=False, submit_chunk_cap=None):
        moves.append((distance, speed, submit_chunk_cap))
        set_sensor_active(feeder, "hall_empty", False)

    monkeypatch.setattr(feeder, "_submit_move", fake_submit)

    feeder.cmd_BUFFER_PREP_BASELINE(FakeGCmd({
        "CHUNK_MM": 5,
        "SPEED": 15,
        "MAX_DISTANCE": 50,
        "SETTLE_MS": 0,
    }))

    # submit_chunk_cap is intentionally NOT passed (see comment in
    # cmd_BUFFER_PREP_BASELINE about the c002 hang).
    assert moves == [(5.0, 15.0, None)]
    assert feeder._state == buffer_feeder.STATE_IDLE
    assert feeder.hall_empty is False
    assert feeder.hall_full is False
    assert feeder.hall_overflow is False


def test_buffer_prep_baseline_clamps_stale_lme_before_submit(feeder, monkeypatch):
    """Regression for stepcompress c=21 race (2026-05-18 klippy.log):
    watchdog-anchor leaves _last_move_end_time slightly in the future
    (sub-MAX_T0_LOOKAHEAD_S so the sanitizer does not clamp). A direct
    BUFFER_PREP_BASELINE submit then computed t0 before last_step_clock
    -> Invalid sequence. The fix clamps lme back to mcu_now when no
    move is genuinely in flight before each prep sub-chunk."""
    set_sensor_active(feeder, "hall_empty", True)
    set_sensor_active(feeder, "hall_full", False)
    set_sensor_active(feeder, "hall_overflow", False)

    # Simulate watchdog-anchor residue: lme set 0.3 s ahead of mcu_now,
    # no in-flight move. The previous code path would let lme survive.
    mcu = feeder.stepper.get_mcu()
    mcu_now = mcu.estimated_print_time(feeder.reactor.monotonic())
    feeder._last_move_end_time = mcu_now + 0.3
    feeder._current_move = None
    lme_before_each_submit = []

    def fake_submit(distance, speed, forced_t0=None, streaming=False, submit_chunk_cap=None):
        lme_before_each_submit.append(feeder._last_move_end_time)
        set_sensor_active(feeder, "hall_empty", False)

    monkeypatch.setattr(feeder, "_submit_move", fake_submit)

    feeder.cmd_BUFFER_PREP_BASELINE(FakeGCmd({
        "CHUNK_MM": 5,
        "SPEED": 15,
        "MAX_DISTANCE": 50,
        "SETTLE_MS": 0,
    }))

    assert lme_before_each_submit, "_submit_move must have been called"
    # The fix sets lme <= mcu_now before each submit when no live move.
    # Allow tiny float drift from the second estimated_print_time call.
    for lme in lme_before_each_submit:
        assert lme <= mcu_now + 1e-3, (
            "lme not clamped: %.6f > mcu_now %.6f" % (lme, mcu_now))


def test_buffer_prep_baseline_keeps_lme_when_move_in_flight(feeder, monkeypatch):
    """When a move is genuinely in flight (e.g. residual streaming),
    lme is a real future timestamp and must NOT be clobbered."""
    set_sensor_active(feeder, "hall_empty", True)
    set_sensor_active(feeder, "hall_full", False)
    set_sensor_active(feeder, "hall_overflow", False)

    mcu = feeder.stepper.get_mcu()
    mcu_now = mcu.estimated_print_time(feeder.reactor.monotonic())
    future_end = mcu_now + 0.5
    feeder._last_move_end_time = future_end
    feeder._current_move = {'end_time': future_end}
    lme_before_each_submit = []

    def fake_submit(distance, speed, forced_t0=None, streaming=False, submit_chunk_cap=None):
        lme_before_each_submit.append(feeder._last_move_end_time)
        set_sensor_active(feeder, "hall_empty", False)

    monkeypatch.setattr(feeder, "_submit_move", fake_submit)

    feeder.cmd_BUFFER_PREP_BASELINE(FakeGCmd({
        "CHUNK_MM": 5,
        "SPEED": 15,
        "MAX_DISTANCE": 50,
        "SETTLE_MS": 0,
    }))

    assert lme_before_each_submit
    # lme preserved because _move_in_flight() returned True.
    assert lme_before_each_submit[0] == future_end


def test_buffer_prep_baseline_retracts_from_h1_to_neutral(feeder, monkeypatch):
    set_sensor_active(feeder, "hall_empty", False)
    set_sensor_active(feeder, "hall_full", False)
    set_sensor_active(feeder, "hall_overflow", True)

    moves = []

    def fake_submit(distance, speed, forced_t0=None, streaming=False, submit_chunk_cap=None):
        moves.append((distance, speed))
        set_sensor_active(feeder, "hall_overflow", False)

    monkeypatch.setattr(feeder, "_submit_move", fake_submit)

    feeder.cmd_BUFFER_PREP_BASELINE(FakeGCmd({
        "CHUNK_MM": 5,
        "SPEED": 15,
        "MAX_DISTANCE": 50,
        "SETTLE_MS": 0,
    }))

    assert moves == [(-5.0, 15.0)]
    assert feeder._state == buffer_feeder.STATE_IDLE
    assert feeder.hall_empty is False
    assert feeder.hall_full is False
    assert feeder.hall_overflow is False
