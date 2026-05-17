from helpers import FakeGCmd, set_sensor_active
from klipper_extras import buffer_feeder


def test_buffer_prep_baseline_feeds_from_h3_to_neutral(feeder, monkeypatch):
    set_sensor_active(feeder, "hall_empty", True)
    set_sensor_active(feeder, "hall_full", False)
    set_sensor_active(feeder, "hall_overflow", False)

    moves = []

    def fake_submit(distance, speed, forced_t0=None, streaming=False, submit_chunk_cap=None):
        moves.append((distance, speed))
        set_sensor_active(feeder, "hall_empty", False)

    monkeypatch.setattr(feeder, "_submit_move", fake_submit)

    feeder.cmd_BUFFER_PREP_BASELINE(FakeGCmd({
        "CHUNK_MM": 5,
        "SPEED": 15,
        "MAX_DISTANCE": 50,
        "SETTLE_MS": 0,
    }))

    assert moves == [(5.0, 15.0)]
    assert feeder._state == buffer_feeder.STATE_IDLE
    assert feeder.hall_empty is False
    assert feeder.hall_full is False
    assert feeder.hall_overflow is False


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
