"""Gruppe-3-Fixes (falsche Alarme / haengende Zustaende, Review 2026-07-09).

- JAM-Preservation: PHASE1-except und UNLOAD_PHASE3-Overshoot stampften
  STATE_JAM mit IDLE; BUFFER_CLEAR_JAM verlangte strikt state==JAM →
  dokumentierter Recovery-Pfad tot (state=IDLE + _jam_active=True).
- resume_after_overflow promotete zu AUTO trotz latched _jam_active.
- _on_idle_printing guardete nur 'standby' — idle_timeout-Flaps nach
  'complete'/'cancelled' armten _print_running (Runout-PAUSE auf
  fertigem Druck, Spontan-Grip nach CANCEL).
- Stale _feed_deadline_time feuerte SAFETY_TIMEOUT-JAM in Folge-
  Workflows (Kommando-Einstiege clearen nur _continuous_feed).
- PHASE3: stale _fault_overflow beim OVERFLOW_OK=1-Entry (Silent-
  Success nach 0 Iterationen), verschluckter HALT im OVERFLOW_OK=1-
  Postcheck, Zombie-State LOADING_PUSH nach Overlay-Abort.
- UNLOAD_FILAMENT ohne Entry-Guard (Trapq-Swap waehrend INITIAL_GRIP).
- HALL1-Rising-Edge im Bypass-Kontext (synced/UNLOAD/phase3_ok) lief in
  den Cleared-Zweig → Persist-Timestamp nie gesetzt → Eskalation tot.
- _prepare_post_jam_recovery armte den Guard in der print_time-Domaene
  (mcu_now), verglichen wird ueberall gegen reactor.monotonic().
- Modulator-HALL3-Floor war der einzige Pfad ohne feed_speed-Clamp.
"""

import pytest
from fakes_klipper import FakeConfig, FakePrinter, FakePrintStats
from helpers import FakeGCmd
from klipper_extras import buffer_feeder


def set_sensor_active(feeder, sensor_name, active):
    polarity_flip = feeder._pin_polarity_flip[sensor_name]
    raw = (not active) if polarity_flip else active
    feeder._pin_stable_state[sensor_name] = raw
    feeder._pin_raw_state[sensor_name] = raw


def make_feeder(values=None, state=None, ps_state="standby"):
    printer = FakePrinter()
    printer.objects["print_stats"] = FakePrintStats(state=ps_state)
    config = FakeConfig(printer=printer, values=values)
    feeder = buffer_feeder.BufferFeeder(config)
    printer.fire_event('klippy:connect')
    feeder._startup_grace_done = True
    if state is not None:
        feeder._state = state
    set_sensor_active(feeder, 'hall_overflow', False)
    set_sensor_active(feeder, 'hall_full', False)
    set_sensor_active(feeder, 'hall_empty', False)
    set_sensor_active(feeder, 'entrance', True)
    return printer, feeder


# ---------------------------------------------------------------------------
# JAM-Preservation + CLEAR_JAM
# ---------------------------------------------------------------------------


def test_phase1_error_preserves_jam_state(monkeypatch):
    """JAM waehrend des Phase-1-Waits: der except-Handler darf
    STATE_JAM nicht mit IDLE ueberschreiben."""
    _, feeder = make_feeder(state=buffer_feeder.STATE_IDLE)
    monkeypatch.setattr(feeder, "_submit_move", lambda *a, **k: None)

    def _jam_then_raise(gcmd=None):
        feeder._jam_active = True
        feeder._set_state(buffer_feeder.STATE_JAM)
        raise RuntimeError("JAM SUPPLY")
    monkeypatch.setattr(
        feeder, "_wait_for_move_done_resume_on_overflow", _jam_then_raise)

    with pytest.raises(RuntimeError):
        feeder.cmd_BUFFER_LOAD_PHASE1(FakeGCmd())

    assert feeder._state == buffer_feeder.STATE_JAM, (
        "except-handler must release only its own phase state — "
        "stamping IDLE over JAM locks BUFFER_CLEAR_JAM out")
    assert feeder._jam_active is True


def test_unload_phase3_overshoot_preserves_jam_state(monkeypatch):
    """JAM mid-Loop + Overshoot: state muss JAM bleiben."""
    _, feeder = make_feeder(state=buffer_feeder.STATE_IDLE)

    def _fake_submit(dist, speed, **kw):
        feeder._jam_active = True
        feeder._state = buffer_feeder.STATE_JAM
    monkeypatch.setattr(feeder, "_submit_move", _fake_submit)

    with pytest.raises(Exception, match="MAX_DISTANCE"):
        feeder.cmd_BUFFER_UNLOAD_PHASE3(FakeGCmd({'MAX_DISTANCE': 60.0}))

    assert feeder._state == buffer_feeder.STATE_JAM, (
        "overshoot exit must not stamp IDLE over a mid-loop JAM")


def test_clear_jam_recovers_from_idle_with_latched_jam():
    """state=IDLE + _jam_active=True (durch fruehere Overwrite-Pfade
    erreichbar): BUFFER_CLEAR_JAM muss den Latch loesen statt
    'Not in JAM state' zu raisen."""
    _, feeder = make_feeder(state=buffer_feeder.STATE_IDLE)
    feeder._jam_active = True

    feeder.cmd_BUFFER_CLEAR_JAM(FakeGCmd())

    assert feeder._jam_active is False
    assert feeder._state in (buffer_feeder.STATE_IDLE,
                             buffer_feeder.STATE_AUTO)


def test_resume_after_overflow_respects_latched_jam():
    """OVERFLOW-Blip waehrend JAM (HALL1-Hard-Trigger ueberschreibt
    STATE_JAM): der Resume darf NICHT zu AUTO promoten solange
    _jam_active latched ist — JAM verlangt explizites CLEAR_JAM."""
    _, feeder = make_feeder(state=buffer_feeder.STATE_IDLE)
    feeder._jam_active = True
    feeder._overflow_interrupted_state = None

    feeder.fault.resume_after_overflow()

    assert feeder._state == buffer_feeder.STATE_JAM, (
        "resume must restore STATE_JAM instead of promoting to AUTO "
        "with jam-detection permanently disabled")


# ---------------------------------------------------------------------------
# _on_idle_printing Post-Print-Flaps
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ps_state", ["complete", "cancelled"])
def test_idle_printing_flap_after_print_end_is_ignored(ps_state):
    _, feeder = make_feeder(ps_state=ps_state)
    feeder._print_running = False

    feeder._on_idle_printing()

    assert feeder._print_running is False, (
        "idle_timeout flap after '%s' must not arm _print_running — "
        "it re-enables runout-PAUSE on a finished print" % ps_state)


def test_idle_printing_real_print_still_arms():
    _, feeder = make_feeder(ps_state="printing")
    feeder._print_running = False

    feeder._on_idle_printing()

    assert feeder._print_running is True


# ---------------------------------------------------------------------------
# Stale _feed_deadline_time
# ---------------------------------------------------------------------------


def test_stale_feed_deadline_does_not_jam(monkeypatch):
    _, feeder = make_feeder(state=buffer_feeder.STATE_IDLE)
    jams = []
    monkeypatch.setattr(feeder, "_trigger_jam",
                        lambda *a, **k: jams.append(a))
    feeder._feed_deadline_time = 5.0
    feeder._continuous_feed = False  # Session laengst beendet

    feeder._tick_safety_timeouts(eventtime=10.0)

    assert jams == [], (
        "a deadline armed for a finished continuous session must not "
        "fire SAFETY_TIMEOUT in an unrelated later workflow")
    assert feeder._feed_deadline_time is None


def test_active_feed_deadline_still_jams(monkeypatch):
    _, feeder = make_feeder(state=buffer_feeder.STATE_MANUAL_FEED)
    jams = []
    monkeypatch.setattr(feeder, "_trigger_jam",
                        lambda *a, **k: jams.append(a))
    feeder._feed_deadline_time = 5.0
    feeder._continuous_feed = True

    feeder._tick_safety_timeouts(eventtime=10.0)

    assert len(jams) == 1


# ---------------------------------------------------------------------------
# LOAD_PHASE3: stale _fault_overflow / HALT / Zombie-State
# ---------------------------------------------------------------------------


def test_phase3_overflow_ok_entry_clears_stale_fault_overflow(monkeypatch):
    _, feeder = make_feeder(
        values={'use_fault_overlay': True},
        state=buffer_feeder.STATE_OVERFLOW)
    feeder._fault_overflow = True
    seen = {}

    def _fake_start(direction, speed, max_dur):
        seen['fault_at_start'] = feeder._fault_overflow
        feeder._state = buffer_feeder.STATE_IDLE  # Loop-Exit ohne Hook
    monkeypatch.setattr(feeder, "_start_continuous_motion", _fake_start)

    feeder.cmd_BUFFER_LOAD_PHASE3(FakeGCmd({'OVERFLOW_OK': 1}))

    assert seen['fault_at_start'] is False, (
        "OVERFLOW_OK=1 entry from STATE_OVERFLOW must clear the stale "
        "overlay flag — otherwise the while-loop exits after 0 "
        "iterations with silent success")


def test_phase3_overflow_ok_postcheck_honours_halt(monkeypatch):
    _, feeder = make_feeder(state=buffer_feeder.STATE_IDLE)

    def _fake_start(direction, speed, max_dur):
        # BUFFER_HALT mid-phase: full_reset setzt IDLE + halt_requested.
        feeder._halt_requested = True
        feeder._state = buffer_feeder.STATE_IDLE
    monkeypatch.setattr(feeder, "_start_continuous_motion", _fake_start)

    with pytest.raises(Exception, match="HALT"):
        feeder.cmd_BUFFER_LOAD_PHASE3(FakeGCmd({'OVERFLOW_OK': 1}))


def test_phase3_overlay_abort_leaves_no_zombie_state(monkeypatch):
    """Overlay-Abort (HALL1 → _fault_overflow, state bleibt
    LOADING_PUSH): nach dem Postcheck-Raise darf der State nicht
    LOADING_PUSH bleiben — sonst fuettert _load_phase3_tick nach
    HALL1-Fall autonom weiter (Zombie-Feed)."""
    _, feeder = make_feeder(
        values={'use_fault_overlay': True},
        state=buffer_feeder.STATE_IDLE)

    def _fake_start(direction, speed, max_dur):
        set_sensor_active(feeder, 'hall_overflow', True)
        feeder._fault_overflow = True  # Overlay: state bleibt LOADING_PUSH
    monkeypatch.setattr(feeder, "_start_continuous_motion", _fake_start)

    with pytest.raises(Exception):
        feeder.cmd_BUFFER_LOAD_PHASE3(FakeGCmd())

    assert feeder._state != buffer_feeder.STATE_LOADING_PUSH, (
        "aborted PHASE3 must not leave the tick-driven LOADING_PUSH "
        "state behind (zombie feed after HALL1 falls)")
    assert feeder._state == buffer_feeder.STATE_OVERFLOW


# ---------------------------------------------------------------------------
# UNLOAD_FILAMENT Entry-Guard
# ---------------------------------------------------------------------------


def test_unload_filament_rejected_during_initial_grip():
    printer, feeder = make_feeder(state=buffer_feeder.STATE_INITIAL_GRIP)

    with pytest.raises(Exception, match="UNLOAD_FILAMENT rejected"):
        feeder.cmd_BUFFER_UNLOAD_FILAMENT(FakeGCmd())

    gcode = printer.objects['gcode']
    assert gcode.script_invocations == [], (
        "guard must fire BEFORE tip-forming/SAVE_GCODE_STATE — "
        "aborting after the final retract leaves the filament end "
        "undefined in the bowden")


# ---------------------------------------------------------------------------
# HALL1-Rising-Edge im Bypass-Kontext
# ---------------------------------------------------------------------------


def test_hall1_rising_edge_during_sync_sets_persist_timestamp():
    _, feeder = make_feeder(state=buffer_feeder.STATE_AUTO)
    feeder._stepper_synced_to = 'extruder'  # Bypass-Kontext
    set_sensor_active(feeder, 'hall_overflow', True)

    feeder.sensors.on_stable_sensor_change(1.0, 'hall_overflow', True)

    assert feeder._hall1_active_since is not None, (
        "physical HALL1 rising edge must arm the persist timestamp "
        "even in bypass contexts — otherwise the escalation never "
        "fires once the sync is released")
    assert feeder._state == buffer_feeder.STATE_AUTO, (
        "bypass context must still suppress the immediate "
        "_enter_overflow")


def test_hall1_falling_edge_still_clears():
    _, feeder = make_feeder(state=buffer_feeder.STATE_AUTO)
    feeder._hall1_active_since = 1.0
    set_sensor_active(feeder, 'hall_overflow', False)

    feeder.sensors.on_stable_sensor_change(2.0, 'hall_overflow', False)

    assert feeder._hall1_active_since is None


# ---------------------------------------------------------------------------
# Zeitdomaene JAM-Exit-Guard
# ---------------------------------------------------------------------------


def test_post_jam_guard_armed_in_monotonic_domain(monkeypatch):
    _, feeder = make_feeder(state=buffer_feeder.STATE_IDLE)
    seen = {}

    def _spy(reason, duration=None, eventtime=None):
        seen['reason'] = reason
        seen['eventtime'] = eventtime
    monkeypatch.setattr(feeder, "_arm_critical_action_guard", _spy)

    feeder._prepare_post_jam_recovery()

    assert seen['reason'] == 'jam_exit'
    assert seen['eventtime'] is None, (
        "guard-until is compared against reactor.monotonic() — arming "
        "it with mcu print_time makes the jam-exit window a no-op")


# ---------------------------------------------------------------------------
# Modulator: HALL3-Floor auf feed_speed clampen
# ---------------------------------------------------------------------------


def test_hall3_floor_clamped_to_feed_speed():
    printer, feeder = make_feeder(values={'feed_speed': '10'},
                                  state=buffer_feeder.STATE_AUTO)
    set_sensor_active(feeder, 'hall_empty', True)
    ext = printer.objects['extruder']
    t = 0.0
    for _ in range(14):
        ext.last_position = t * 5.0   # 5 mm/s < min_feed_floor (15)
        feeder.velocity_tracker.tick(t)
        t += 0.025

    target = feeder._compute_target_feed_speed()

    assert target <= feeder.feed_speed, (
        "HALL3 floor path is the only modulator branch without a "
        "feed_speed clamp — floor=15 must not override feed_speed=10")
    assert target == pytest.approx(10.0)
