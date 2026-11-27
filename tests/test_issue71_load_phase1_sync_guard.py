"""Issue #71: LOAD_PHASE1 must reject an active extruder SYNC."""

import pytest

from helpers import FakeGCmd, set_sensor_active
from klipper_extras import buffer_feeder


def _clear_hall_lockouts(feeder):
    set_sensor_active(feeder, 'hall_overflow', False)
    set_sensor_active(feeder, 'hall_full', False)
    set_sensor_active(feeder, 'hall_empty', False)


def test_load_phase1_rejects_active_sync_before_any_mutation(feeder_factory):
    printer, feeder = feeder_factory(
        state=buffer_feeder.STATE_IDLE, fire_connect=True)
    _clear_hall_lockouts(feeder)
    gcode = printer.lookup_object('gcode')
    extruder_trapq = printer.lookup_object('extruder').get_trapq()

    gcode.mux_commands['BUFFER_SYNC_TO_EXTRUDER'][0]['handler'](
        FakeGCmd({'EXTRUDER': 'extruder'}))
    assert feeder.stepper.trapq is extruder_trapq
    assert feeder._stepper_synced_to == 'extruder'

    feeder._halt_requested = True
    feeder._continuous_feed = True
    before = {
        'state': feeder._state,
        'halt': feeder._halt_requested,
        'continuous': feeder._continuous_feed,
        'commanded': feeder._commanded_pos,
        'pending': feeder._pending_remaining_mm,
    }

    with pytest.raises(RuntimeError, match=(
            "LOAD_PHASE1 rejected.*synced to 'extruder'.*BUFFER_UNSYNC")):
        gcode.mux_commands['BUFFER_LOAD_PHASE1'][0]['handler'](
            FakeGCmd({'DISTANCE': 10, 'SPEED': 20}))

    assert feeder.stepper.trapq is extruder_trapq
    assert feeder._stepper_synced_to == 'extruder'
    assert {
        'state': feeder._state,
        'halt': feeder._halt_requested,
        'continuous': feeder._continuous_feed,
        'commanded': feeder._commanded_pos,
        'pending': feeder._pending_remaining_mm,
    } == before


def test_load_phase1_unsynced_queues_requested_distance(
        feeder_factory, monkeypatch):
    printer, feeder = feeder_factory(
        state=buffer_feeder.STATE_IDLE, fire_connect=True)
    _clear_hall_lockouts(feeder)
    gcode = printer.lookup_object('gcode')
    own_trapq = feeder.sync.trapq
    motion_q = printer.lookup_object('motion_queuing')

    # The real move is queued below. Avoid waiting for fake reactor ticks;
    # production Klipper advances the trapq asynchronously during this wait.
    monkeypatch.setattr(
        feeder, '_wait_for_move_done_resume_on_overflow', lambda gcmd: None)
    appends_before = len(motion_q.append_calls)

    gcode.mux_commands['BUFFER_LOAD_PHASE1'][0]['handler'](
        FakeGCmd({'DISTANCE': 10, 'SPEED': 20}))

    own_appends = [call for call in motion_q.append_calls[appends_before:]
                   if call[0] is own_trapq]
    assert len(own_appends) == 1
    assert feeder.stepper.trapq is own_trapq
    assert feeder._stepper_synced_to is None
    assert feeder._commanded_pos == pytest.approx(10.0)
    assert feeder._pending_remaining_mm == 0.0
