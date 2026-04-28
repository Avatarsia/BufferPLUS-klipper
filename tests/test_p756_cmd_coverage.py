"""P7-56c — Coverage tests for previously-untested cmd_ methods.

Identified by Codex+Sonnet refactor review as having 0 direct tests:
- cmd_BUFFER_LOAD_PHASE1
- cmd_BUFFER_FEED / cmd_BUFFER_RETRACT (via _cmd_feed_common)
- cmd_FORCE_BUFFER_FILL
- cmd_BUFFER_CLEAR_JAM

These are refactor-blockers — touching the cleanup-flag logic without
tests would break recovery flows silently. This file fixes the gap
with focused happy-path + reject-path coverage.
"""

import pytest
from fakes_klipper import FakeConfig, FakePrinter
from klipper_extras import buffer_feeder


def make_feeder(values=None):
    printer = FakePrinter()
    config = FakeConfig(printer=printer, values=values)
    feeder = buffer_feeder.BufferFeeder(config)
    feeder._startup_grace_done = True
    feeder._state = buffer_feeder.STATE_IDLE
    return printer, feeder


def set_sensor_active(feeder, sensor_name, active):
    polarity_flip = feeder._pin_polarity_flip[sensor_name]
    raw = (not active) if polarity_flip else active
    feeder._pin_stable_state[sensor_name] = raw
    feeder._pin_raw_state[sensor_name] = raw


class FakeGCmd:
    def __init__(self, values=None):
        self.values = {key.upper(): value for key, value in (values or {}).items()}

    def get(self, key, default=None):
        return self.values.get(key.upper(), default)

    def get_int(self, key, default=None, **kwargs):
        return int(self.values.get(key.upper(), default))

    def get_float(self, key, default=None, **kwargs):
        return float(self.values.get(key.upper(), default))


# ---------------------------------------------------------------------------
# cmd_BUFFER_LOAD_PHASE1
# ---------------------------------------------------------------------------

def test_load_phase1_rejects_in_overflow_state(monkeypatch):
    _, feeder = make_feeder()
    feeder._state = buffer_feeder.STATE_OVERFLOW
    monkeypatch.setattr(feeder, "_wait_for_move_done", lambda *a, **k: None)
    monkeypatch.setattr(feeder, "_wait_for_move_done_resume_on_overflow", lambda *a: None)

    with pytest.raises(Exception):
        feeder.cmd_BUFFER_LOAD_PHASE1(FakeGCmd({"DISTANCE": 100}))


def test_load_phase1_rejects_when_hall1_active(monkeypatch):
    """HALL1 active triggers _raise_if_locked_out at phase entry."""
    _, feeder = make_feeder()
    set_sensor_active(feeder, 'hall_overflow', True)
    monkeypatch.setattr(feeder, "_wait_for_move_done", lambda *a, **k: None)
    monkeypatch.setattr(feeder, "_wait_for_move_done_resume_on_overflow", lambda *a: None)

    with pytest.raises(Exception):
        feeder.cmd_BUFFER_LOAD_PHASE1(FakeGCmd({"DISTANCE": 100}))


def test_load_phase1_happy_path_transitions_through_phase1(monkeypatch):
    """IDLE → LOAD_PHASE_1 → IDLE on clean completion."""
    _, feeder = make_feeder()
    set_sensor_active(feeder, 'hall_overflow', False)
    states = []
    monkeypatch.setattr(feeder, "_wait_for_move_done", lambda *a, **k: None)
    monkeypatch.setattr(feeder, "_wait_for_move_done_resume_on_overflow", lambda *a: None)
    monkeypatch.setattr(feeder, "_submit_move", lambda dist, spd: None)
    monkeypatch.setattr(feeder, "_enable_stepper", lambda: None)
    orig_set = feeder._set_state
    monkeypatch.setattr(feeder, "_set_state",
                        lambda s: (states.append(s), orig_set(s)))

    feeder.cmd_BUFFER_LOAD_PHASE1(FakeGCmd({"DISTANCE": 100, "SPEED": 50}))

    assert buffer_feeder.STATE_LOAD_PHASE_1 in states
    assert feeder._state == buffer_feeder.STATE_IDLE


def test_load_phase1_releases_state_on_exception(monkeypatch):
    """Phase 1 must drop back to IDLE if _wait_for_move_done_resume
    raises (e.g. _halt_requested mid-move). Otherwise the phase
    state stays sticky and BUFFER_AUTO_ON would refuse forever."""
    _, feeder = make_feeder()
    set_sensor_active(feeder, 'hall_overflow', False)
    monkeypatch.setattr(feeder, "_wait_for_move_done", lambda *a, **k: None)
    monkeypatch.setattr(feeder, "_submit_move", lambda dist, spd: None)
    monkeypatch.setattr(feeder, "_enable_stepper", lambda: None)

    def boom(gcmd):
        raise RuntimeError("simulated halt")
    monkeypatch.setattr(feeder, "_wait_for_move_done_resume_on_overflow", boom)

    with pytest.raises(RuntimeError):
        feeder.cmd_BUFFER_LOAD_PHASE1(FakeGCmd({"DISTANCE": 100}))

    assert feeder._state == buffer_feeder.STATE_IDLE


# ---------------------------------------------------------------------------
# cmd_BUFFER_FEED / cmd_BUFFER_RETRACT (_cmd_feed_common)
# ---------------------------------------------------------------------------

def test_feed_rejects_in_overflow(monkeypatch):
    _, feeder = make_feeder()
    feeder._state = buffer_feeder.STATE_OVERFLOW

    with pytest.raises(Exception):
        feeder.cmd_BUFFER_FEED(FakeGCmd({"DISTANCE": 50}))


def test_feed_rejects_in_jam():
    _, feeder = make_feeder()
    feeder._state = buffer_feeder.STATE_JAM

    with pytest.raises(Exception):
        feeder.cmd_BUFFER_FEED(FakeGCmd({"DISTANCE": 50}))


def test_feed_rejects_when_hall1_physically_active():
    _, feeder = make_feeder()
    set_sensor_active(feeder, 'hall_overflow', True)

    with pytest.raises(Exception):
        feeder.cmd_BUFFER_FEED(FakeGCmd({"DISTANCE": 50}))


def test_feed_rejects_in_busy_phase(monkeypatch):
    """LOAD_PHASE_1, LOAD_PHASE_3 etc. block manual feed — operator
    must explicitly STOP_BUFFER_FILL first."""
    _, feeder = make_feeder()
    feeder._state = buffer_feeder.STATE_LOAD_PHASE_3

    with pytest.raises(Exception):
        feeder.cmd_BUFFER_FEED(FakeGCmd({"DISTANCE": 50}))


def test_feed_rejects_distance_above_max(monkeypatch):
    _, feeder = make_feeder(values={"max_feed_distance": 10})
    set_sensor_active(feeder, 'hall_overflow', False)

    with pytest.raises(Exception):
        feeder.cmd_BUFFER_FEED(FakeGCmd({"DISTANCE": 999}))


def test_feed_clears_recovery_flags(monkeypatch):
    """Fresh manual command = operator acknowledged stale state.
    _runout_recovery_pending and _halt_requested must be cleared."""
    _, feeder = make_feeder()
    set_sensor_active(feeder, 'hall_overflow', False)
    feeder._runout_recovery_pending = True
    feeder._halt_requested = True
    monkeypatch.setattr(feeder, "_submit_move", lambda d, s: None)
    monkeypatch.setattr(feeder, "_schedule_return_to_auto_after_move", lambda: None)

    feeder.cmd_BUFFER_FEED(FakeGCmd({"DISTANCE": 50}))

    assert feeder._runout_recovery_pending is False
    assert feeder._halt_requested is False
    assert feeder._continuous_feed is False
    assert feeder._state == buffer_feeder.STATE_MANUAL_FEED


def test_feed_submits_correct_distance_and_speed(monkeypatch):
    """Argument-level test for _submit_move so Phase D's reset-helper
    refactor cannot silently break the actual feed motion."""
    _, feeder = make_feeder()
    set_sensor_active(feeder, 'hall_overflow', False)
    submit_calls = []
    monkeypatch.setattr(feeder, "_submit_move",
                        lambda d, s: submit_calls.append((d, s)))
    monkeypatch.setattr(feeder, "_schedule_return_to_auto_after_move", lambda: None)

    feeder.cmd_BUFFER_FEED(FakeGCmd({"DISTANCE": 75, "SPEED": 30}))

    assert submit_calls == [(75.0, 30.0)]


def test_retract_uses_negative_direction(monkeypatch):
    _, feeder = make_feeder()
    set_sensor_active(feeder, 'hall_overflow', False)
    submit_calls = []
    monkeypatch.setattr(feeder, "_submit_move",
                        lambda d, s: submit_calls.append((d, s)))
    monkeypatch.setattr(feeder, "_schedule_return_to_auto_after_move", lambda: None)

    feeder.cmd_BUFFER_RETRACT(FakeGCmd({"DISTANCE": 30, "SPEED": 20}))

    assert submit_calls == [(-30.0, 20.0)]
    assert feeder._state == buffer_feeder.STATE_MANUAL_RETRACT


def test_feed_distance_zero_starts_continuous(monkeypatch):
    """DISTANCE=0 (or omitted) → continuous-feed mode with timeout."""
    _, feeder = make_feeder()
    set_sensor_active(feeder, 'hall_overflow', False)
    cont_calls = []
    monkeypatch.setattr(feeder, "_start_continuous_motion",
                        lambda d, s, t: cont_calls.append((d, s, t)))

    feeder.cmd_BUFFER_FEED(FakeGCmd({"DISTANCE": 0, "SPEED": 25, "TIMEOUT": 5}))

    assert cont_calls == [(1, 25.0, 5.0)]
    assert feeder._state == buffer_feeder.STATE_MANUAL_FEED


# ---------------------------------------------------------------------------
# cmd_FORCE_BUFFER_FILL
# ---------------------------------------------------------------------------

def test_force_fill_rejects_without_entrance():
    _, feeder = make_feeder()
    set_sensor_active(feeder, 'entrance', False)

    with pytest.raises(Exception):
        feeder.cmd_FORCE_BUFFER_FILL(FakeGCmd())


def test_force_fill_rejects_when_hall1_active():
    _, feeder = make_feeder()
    set_sensor_active(feeder, 'entrance', True)
    set_sensor_active(feeder, 'hall_overflow', True)

    with pytest.raises(Exception):
        feeder.cmd_FORCE_BUFFER_FILL(FakeGCmd())


def test_force_fill_rejects_during_jam():
    _, feeder = make_feeder()
    set_sensor_active(feeder, 'entrance', True)
    feeder._state = buffer_feeder.STATE_JAM
    feeder._jam_active = True

    with pytest.raises(Exception):
        feeder.cmd_FORCE_BUFFER_FILL(FakeGCmd())


def test_force_fill_rejects_when_print_paused():
    """bang_bang_suspended (= print PAUSE) blocks FORCE_BUFFER_FILL."""
    _, feeder = make_feeder()
    set_sensor_active(feeder, 'entrance', True)
    feeder._bang_bang_suspended = True

    with pytest.raises(Exception):
        feeder.cmd_FORCE_BUFFER_FILL(FakeGCmd())


def test_force_fill_rejects_when_busy():
    """LOAD_PHASE_3 / MANUAL_FEED etc. — only IDLE/RUNOUT allowed."""
    _, feeder = make_feeder()
    set_sensor_active(feeder, 'entrance', True)
    feeder._state = buffer_feeder.STATE_LOAD_PHASE_3

    with pytest.raises(Exception):
        feeder.cmd_FORCE_BUFFER_FILL(FakeGCmd())


def test_force_fill_happy_path_clears_flags_and_starts_grip(monkeypatch):
    """IDLE + entrance OK → clear stale recovery flags, then
    _start_initial_grip."""
    _, feeder = make_feeder()
    set_sensor_active(feeder, 'entrance', True)
    set_sensor_active(feeder, 'hall_overflow', False)
    feeder._halt_requested = True
    feeder._auto_off_by_user = True
    feeder._runout_recovery_pending = True
    grip_calls = []
    monkeypatch.setattr(feeder, "_wait_for_move_done", lambda *a, **k: None)
    monkeypatch.setattr(feeder, "_start_initial_grip",
                        lambda et: grip_calls.append(et))

    feeder.cmd_FORCE_BUFFER_FILL(FakeGCmd())

    assert feeder._halt_requested is False
    assert feeder._auto_off_by_user is False
    assert feeder._runout_recovery_pending is False
    assert len(grip_calls) == 1


# ---------------------------------------------------------------------------
# cmd_BUFFER_CLEAR_JAM
# ---------------------------------------------------------------------------

def test_clear_jam_rejects_when_not_in_jam():
    _, feeder = make_feeder()
    feeder._state = buffer_feeder.STATE_IDLE

    with pytest.raises(Exception):
        feeder.cmd_BUFFER_CLEAR_JAM(FakeGCmd())


def test_clear_jam_returns_to_idle_when_no_entrance(monkeypatch):
    _, feeder = make_feeder()
    feeder._state = buffer_feeder.STATE_JAM
    feeder._jam_active = True
    set_sensor_active(feeder, 'entrance', False)
    monkeypatch.setattr(feeder, "_try_restore_gcode_state",
                        lambda from_command=False: False)

    feeder.cmd_BUFFER_CLEAR_JAM(FakeGCmd())

    assert feeder._state == buffer_feeder.STATE_IDLE
    assert feeder._jam_active is False
    assert feeder._halt_requested is False


def test_clear_jam_returns_to_auto_when_ready(monkeypatch):
    """JAM → AUTO when entrance present and no other block-reason."""
    _, feeder = make_feeder()
    feeder._state = buffer_feeder.STATE_JAM
    feeder._jam_active = True
    set_sensor_active(feeder, 'entrance', True)
    set_sensor_active(feeder, 'hall_overflow', False)
    feeder._auto_off_by_user = False
    feeder._bang_bang_suspended = False
    monkeypatch.setattr(feeder, "_try_restore_gcode_state",
                        lambda from_command=False: False)

    feeder.cmd_BUFFER_CLEAR_JAM(FakeGCmd())

    assert feeder._state == buffer_feeder.STATE_AUTO


def test_clear_jam_stays_idle_when_auto_off_by_user(monkeypatch):
    """Even if _check_auto_ready passes, _auto_off_by_user keeps
    us in IDLE so the operator's explicit AUTO_OFF is respected."""
    _, feeder = make_feeder()
    feeder._state = buffer_feeder.STATE_JAM
    feeder._jam_active = True
    set_sensor_active(feeder, 'entrance', True)
    set_sensor_active(feeder, 'hall_overflow', False)
    feeder._auto_off_by_user = True
    monkeypatch.setattr(feeder, "_try_restore_gcode_state",
                        lambda from_command=False: False)

    feeder.cmd_BUFFER_CLEAR_JAM(FakeGCmd())

    assert feeder._state == buffer_feeder.STATE_IDLE


# ---------------------------------------------------------------------------
# _clear_recovery_flags helper (Phase D refactor target)
# ---------------------------------------------------------------------------

def test_clear_recovery_flags_resets_jam_state():
    """Phase D will likely consolidate cleanup into a single helper —
    this test pins down the existing _clear_recovery_flags semantics
    so the refactor cannot silently change them."""
    _, feeder = make_feeder()
    feeder._jam_active = True
    feeder._hall2_start_time = 12.5
    feeder._hall3_start_time = 8.0

    feeder._clear_recovery_flags()

    assert feeder._jam_active is False
    assert feeder._hall2_start_time is None
    assert feeder._hall3_start_time is None


# ---------------------------------------------------------------------------
# Lifecycle hooks: _handle_ready / _end_startup_grace
# ---------------------------------------------------------------------------

def test_handle_ready_registers_main_and_jam_timers(monkeypatch):
    """_handle_ready must register both reactor timers + grace callback."""
    _, feeder = make_feeder()
    feeder._startup_grace_done = False  # simulate fresh boot
    monkeypatch.setattr(feeder, "_anchor_step", lambda: None)
    initial_timer_count = len(feeder.reactor.timers)
    initial_cb_count = len(feeder.reactor.callback_registrations)

    feeder._handle_ready()

    assert len(feeder.reactor.timers) == initial_timer_count + 2
    assert len(feeder.reactor.callback_registrations) == initial_cb_count + 1


def test_end_startup_grace_arms_entrance_edge_when_empty():
    """Without filament at boot, _entrance_was_empty must be armed
    so the first real insert triggers auto-grip."""
    _, feeder = make_feeder()
    set_sensor_active(feeder, 'entrance', False)
    feeder._entrance_was_empty = False
    feeder._anchor_step = lambda: None

    feeder._end_startup_grace(eventtime=2.0)

    assert feeder._startup_grace_done is True
    assert feeder._entrance_was_empty is True


def test_end_startup_grace_auto_engages_when_configured(monkeypatch):
    """auto_engage_on_boot=1 + entrance present → AUTO at end of grace."""
    _, feeder = make_feeder(values={"auto_engage_on_boot": True})
    set_sensor_active(feeder, 'entrance', True)
    set_sensor_active(feeder, 'hall_overflow', False)
    feeder._state = buffer_feeder.STATE_INIT
    monkeypatch.setattr(feeder, "_anchor_step", lambda: None)

    feeder._end_startup_grace(eventtime=2.0)

    assert feeder._state == buffer_feeder.STATE_AUTO


def test_end_startup_grace_skips_auto_when_hall1_active(monkeypatch):
    """auto_engage_on_boot=1 but HALL1 active → stay in non-AUTO state
    (overflow lockout takes precedence)."""
    _, feeder = make_feeder(values={"auto_engage_on_boot": True})
    set_sensor_active(feeder, 'entrance', True)
    set_sensor_active(feeder, 'hall_overflow', True)
    feeder._state = buffer_feeder.STATE_INIT
    monkeypatch.setattr(feeder, "_anchor_step", lambda: None)

    feeder._end_startup_grace(eventtime=2.0)

    assert feeder._state != buffer_feeder.STATE_AUTO
