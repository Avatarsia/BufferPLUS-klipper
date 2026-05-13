"""P7-35: LOAD_PHASE_3 OVERFLOW fault-overlay migration.

Verifies that with use_fault_overlay=1 the HALL1-overflow path no longer
flips _state, but instead sets the _fault_overflow overlay flag while
state stays in LOAD_PHASE_3. With overlay disabled (default) the legacy
state-flip path remains untouched.
"""

from klipper_extras import buffer_feeder


def set_sensor_active(feeder, sensor_name, active):
    polarity_flip = feeder._pin_polarity_flip[sensor_name]
    feeder._pin_stable_state[sensor_name] = (not active) if polarity_flip else active


def test_enter_overflow_overlay_keeps_phase3_state(feeder_factory):
    _, feeder = feeder_factory(values={"use_fault_overlay": True}, grace_done=False)
    feeder._state = buffer_feeder.STATE_LOADING_PUSH
    set_sensor_active(feeder, "hall_overflow", True)

    feeder._enter_overflow()

    assert feeder._state == buffer_feeder.STATE_LOADING_PUSH
    assert feeder._fault_overflow is True
    assert feeder._overflow_interrupted_state == buffer_feeder.STATE_LOADING_PUSH


def test_enter_overflow_legacy_flips_state(feeder_factory):
    _, feeder = feeder_factory(values={"use_fault_overlay": False}, grace_done=False)
    feeder._state = buffer_feeder.STATE_LOADING_PUSH
    set_sensor_active(feeder, "hall_overflow", True)

    feeder._enter_overflow()

    assert feeder._state == buffer_feeder.STATE_OVERFLOW
    # Shadow-tracking flags still maintained for visibility.
    assert feeder._fault_overflow is True


def test_enter_overflow_overlay_only_for_phase3(feeder_factory):
    """Overlay path is currently scoped to LOAD_PHASE_3; other states
    keep flipping to STATE_OVERFLOW even when the flag is enabled."""
    _, feeder = feeder_factory(values={"use_fault_overlay": True}, grace_done=False)
    feeder._state = buffer_feeder.STATE_AUTO
    set_sensor_active(feeder, "hall_overflow", True)

    feeder._enter_overflow()

    assert feeder._state == buffer_feeder.STATE_OVERFLOW


def test_exit_overflow_overlay_clears_flag(feeder_factory):
    _, feeder = feeder_factory(values={"use_fault_overlay": True}, grace_done=False)
    feeder._state = buffer_feeder.STATE_LOADING_PUSH
    feeder._fault_overflow = True
    feeder._overflow_interrupted_state = buffer_feeder.STATE_LOADING_PUSH

    feeder._exit_overflow()

    assert feeder._fault_overflow is False
    # Overlay path doesn't change _state — caller's loop continues.
    assert feeder._state == buffer_feeder.STATE_LOADING_PUSH


def test_exit_overflow_legacy_flips_to_idle(feeder_factory):
    _, feeder = feeder_factory(values={"use_fault_overlay": False}, grace_done=False)
    feeder._state = buffer_feeder.STATE_OVERFLOW
    feeder._fault_overflow = True

    feeder._exit_overflow()

    assert feeder._state == buffer_feeder.STATE_IDLE
    assert feeder._fault_overflow is False


def test_main_tick_does_not_re_enter_with_overlay_set(feeder_factory):
    _, feeder = feeder_factory(values={"use_fault_overlay": True}, grace_done=False)
    feeder._state = buffer_feeder.STATE_LOADING_PUSH
    feeder._fault_overflow = True
    set_sensor_active(feeder, "hall_overflow", True)

    assert feeder._is_hall1_active('main_tick') is False


def test_main_tick_re_enters_without_overlay_flag_pending(feeder_factory):
    _, feeder = feeder_factory(values={"use_fault_overlay": True}, grace_done=False)
    feeder._state = buffer_feeder.STATE_LOADING_PUSH
    feeder._fault_overflow = False
    set_sensor_active(feeder, "hall_overflow", True)
    # phase3_overflow_ok must be False to reach the overlay branch.
    feeder._load_phase3_overflow_ok = False

    assert feeder._is_hall1_active('main_tick') is True
