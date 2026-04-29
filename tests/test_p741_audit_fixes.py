"""P7-41 — fixes from the P7-37..P7-40 audit.

Codex HIGH: cmd_BUFFER_UNLOAD_FILAMENT must inject BUFFER=<name> into
nested BUFFER_SYNC_TO_EXTRUDER + BUFFER_UNLOAD_PHASE3 calls (broken by
P7-40 mux migration; not caught by existing tests because FakeGCode
doesn't dispatch mux).

Reviewer #3 HIGH F1: HALL1 + _stepper_synced_to set must skip lockout
(the bypass path used by every active UNLOAD).

Reviewer #3 HIGH F2: on_entrance_runout during LOAD/UNLOAD/MANUAL_*
phases must early-return without firing PAUSE — vertauschung of the
state list during refactor would silently pause the print.
"""

from fakes_klipper import FakeConfig, FakePrinter
from klipper_extras import buffer_feeder


def make_feeder(values=None):
    printer = FakePrinter()
    config = FakeConfig(printer=printer, values=values)
    feeder = buffer_feeder.BufferFeeder(config)
    return printer, feeder


def set_sensor_active(feeder, sensor_name, active):
    polarity_flip = feeder._pin_polarity_flip[sensor_name]
    feeder._pin_stable_state[sensor_name] = (not active) if polarity_flip else active


# ---------------------------------------------------------------------------
# Codex HIGH — nested BUFFER_* calls carry BUFFER=<name>
# ---------------------------------------------------------------------------

def test_python_unload_nested_calls_include_buffer_mux_key():
    printer, feeder = make_feeder()
    gcode = printer.lookup_object("gcode")
    printer.lookup_object("extruder").heater.temperature = 220.0

    class FakeGCmd:
        def get(self, key, default=None):
            return default

        def get_int(self, key, default=None, **kwargs):
            return int(default)

        def get_float(self, key, default=None, **kwargs):
            return float(default)

    feeder.cmd_BUFFER_UNLOAD_FILAMENT(FakeGCmd())

    nested_calls = [s for _, s in gcode.scripts if s.startswith("BUFFER_")]
    for call in nested_calls:
        assert "BUFFER=mellow" in call, (
            "nested mux command missing BUFFER selector: %s" % call)


# ---------------------------------------------------------------------------
# Reviewer #3 F1 — HALL1 + sync bypass
# ---------------------------------------------------------------------------

def test_hall1_active_bypassed_when_synced_sensor_callback():
    """HALL1 must NOT trigger overflow while the buffer stepper is
    synced to the extruder — this is the active path during UNLOAD."""
    _, feeder = make_feeder()
    feeder._state = buffer_feeder.STATE_AUTO
    set_sensor_active(feeder, "hall_overflow", True)
    feeder._stepper_synced_to = "extruder"

    assert feeder._is_hall1_active("sensor_callback") is False
    assert feeder._is_hall1_active("main_tick") is False


def test_hall1_active_resumes_after_unsync():
    """When _stepper_synced_to is cleared, the lockout re-engages
    immediately on the next sensor poll."""
    _, feeder = make_feeder()
    feeder._state = buffer_feeder.STATE_AUTO
    set_sensor_active(feeder, "hall_overflow", True)
    feeder._stepper_synced_to = "extruder"
    assert feeder._is_hall1_active("main_tick") is False

    feeder._stepper_synced_to = None

    assert feeder._is_hall1_active("main_tick") is True


# ---------------------------------------------------------------------------
# Reviewer #3 F2 — on_entrance_runout suppressed during phases
# ---------------------------------------------------------------------------

def test_runout_during_load_phase_does_not_pause():
    """on_entrance_runout must early-return without PAUSE when an
    active LOAD/UNLOAD/MANUAL phase is running. Vertauschung of the
    state list would silently pause the print mid-phase."""
    # P7-55b: STATE_LOAD_PHASE_2 entfernt mit cmd_BUFFER_LOAD_PHASE2.
    suppressed_states = (
        buffer_feeder.STATE_LOAD_PHASE_1,
        buffer_feeder.STATE_LOAD_PHASE_3,
        buffer_feeder.STATE_UNLOAD_PHASE_3,
        buffer_feeder.STATE_MANUAL_FEED,
        buffer_feeder.STATE_MANUAL_RETRACT,
    )
    for state in suppressed_states:
        printer, feeder = make_feeder(values={"runout_pause": True})
        gcode = printer.lookup_object("gcode")
        feeder._state = state
        feeder._print_running = True
        scripts_before = list(gcode.scripts)

        feeder._on_entrance_runout(eventtime=0.0)

        # No PAUSE script issued, no AUTO->IDLE state change.
        assert gcode.scripts == scripts_before
        assert feeder._state == state, (
            "runout in %s must not change state, got %s" % (state, feeder._state))


def test_runout_during_auto_with_runout_pause_does_trigger_pause():
    """Companion test: in non-suppressed states (AUTO + print_running),
    runout_pause=True must still trigger the PAUSE script."""
    printer, feeder = make_feeder(values={"runout_pause": True})
    gcode = printer.lookup_object("gcode")
    feeder._state = buffer_feeder.STATE_AUTO
    feeder._print_running = True

    feeder._on_entrance_runout(eventtime=0.0)
    # P7-56b: PAUSE is dispatched via 1ms reactor timer to avoid
    # blocking the sensor callback. Fire pending timers so the
    # deferred run_script is observed in gcode.scripts.
    feeder.reactor.fire_pending_timers()

    pause_calls = [s for _, s in gcode.scripts if "PAUSE" in s.upper()]
    assert pause_calls, "expected PAUSE script in non-suppressed runout path"
