"""P7-49 — LOAD always ends in AUTO when entrance is detected.

Hardware-Test 2026-04-27 (operator note): "wenn der load beendet ist
fehlt der wechsel in den automatik modus".

Diagnosis:
1. Phase 3 stable-Exit set state=IDLE when _print_running=False (the
   _print_running guard was added to avoid spontaneous bang-bang
   trigger from manual toolhead pulls — but it left manual LOAD
   workflows without bang-bang arming).
2. Macro-end BUFFER_AUTO_ON_IF_READY then skipped because HALL1 was
   still asserted ("AUTO not engaged: HALL1 overflow active").
3. Net effect: state=IDLE after a successful operator-LOAD, no
   bang-bang for the next manual extrusion → buffer not refilled
   when HALL3 triggers.

Two fixes:

A) Phase 3 stable-Exit (HALL2 and HALL1 paths) now sets state=AUTO
   whenever entrance is detected and the operator hasn't explicitly
   blocked AUTO. _print_running is no longer a guard — a deliberate
   LOAD always implies "I want bang-bang armed when it's done".

B) BUFFER_AUTO_ON_IF_READY accepts AUTO-engage despite HALL1 active
   when _post_load_overflow_grace is set. The grace flag (set by
   Phase 3 stable-HALL1 exit) is the explicit signal that HALL1
   active is the LOAD-success state, not a fault.

Both fixes are independent — either one alone would solve the bug
for one path, but they cover different macro flows:
- A handles the case where the macro doesn't reach AUTO_ON_IF_READY
  (e.g. a future flow that just expects state=AUTO post-LOAD).
- B handles the macro-end AUTO_ON_IF_READY call that runs after
  UNSYNC, which is the actual current LOAD_FILAMENT path.

_main_tick is still HALL1-bypassed via _post_load_overflow_grace
(P7-46 Fix C) so state=AUTO with HALL1 asserted does NOT bounce back
to STATE_OVERFLOW.
"""

from fakes_klipper import FakeConfig, FakePrinter
from klipper_extras import buffer_feeder


def make_feeder(values=None):
    printer = FakePrinter()
    config = FakeConfig(printer=printer, values=values)
    feeder = buffer_feeder.BufferFeeder(config)
    feeder._startup_grace_done = True
    return printer, feeder


def set_sensor_active(feeder, sensor_name, active):
    polarity_flip = feeder._pin_polarity_flip[sensor_name]
    raw = (not active) if polarity_flip else active
    feeder._pin_stable_state[sensor_name] = raw
    feeder._pin_raw_state[sensor_name] = raw


class FakeGCmd:
    def get(self, k, d=None):
        return d
    def get_int(self, k, d=None, **kw):
        return int(d)
    def get_float(self, k, d=None, **kw):
        return float(d)


# ---------------------------------------------------------------------------
# Fix A — Phase 3 exits to AUTO regardless of _print_running
# ---------------------------------------------------------------------------

def test_phase3_hall1_exit_goes_to_auto_when_print_not_running():
    """Operator-LOAD outside a print: stable-HALL1 → STATE_AUTO."""
    _, feeder = make_feeder()
    feeder._state = buffer_feeder.STATE_LOAD_PHASE_3
    feeder._load_phase3_overflow_ok = True
    feeder._load_phase3_stable_timeout = 1.0
    feeder._load_phase3_max_distance = 2000.0
    feeder._load_phase3_chunk_distance = 50.0
    feeder._load_phase3_speed = 30.0
    feeder._load_phase3_distance = 0.0
    feeder._load_phase3_hall_overflow_since = 0.0
    feeder._print_running = False
    set_sensor_active(feeder, 'hall_overflow', True)
    set_sensor_active(feeder, 'entrance', True)

    feeder._load_phase3_tick(eventtime=1.5)

    assert feeder._state == buffer_feeder.STATE_AUTO


def test_phase3_hall2_exit_goes_to_auto_when_print_not_running():
    """Same for the HALL2-stable Exit branch."""
    _, feeder = make_feeder()
    feeder._state = buffer_feeder.STATE_LOAD_PHASE_3
    feeder._load_phase3_overflow_ok = False
    feeder._load_phase3_stable_timeout = 1.0
    feeder._load_phase3_max_distance = 2000.0
    feeder._load_phase3_chunk_distance = 50.0
    feeder._load_phase3_speed = 30.0
    feeder._load_phase3_distance = 0.0
    feeder._load_phase3_hall_full_since = 0.0
    feeder._print_running = False
    set_sensor_active(feeder, 'hall_full', True)
    set_sensor_active(feeder, 'entrance', True)

    feeder._load_phase3_tick(eventtime=1.5)

    assert feeder._state == buffer_feeder.STATE_AUTO


def test_phase3_exit_stays_idle_when_auto_off_by_user():
    """Operator-explicit AUTO_OFF block must still hold."""
    _, feeder = make_feeder()
    feeder._state = buffer_feeder.STATE_LOAD_PHASE_3
    feeder._load_phase3_overflow_ok = True
    feeder._load_phase3_stable_timeout = 1.0
    feeder._load_phase3_max_distance = 2000.0
    feeder._load_phase3_chunk_distance = 50.0
    feeder._load_phase3_speed = 30.0
    feeder._load_phase3_distance = 0.0
    feeder._load_phase3_hall_overflow_since = 0.0
    feeder._auto_off_by_user = True
    set_sensor_active(feeder, 'hall_overflow', True)
    set_sensor_active(feeder, 'entrance', True)

    feeder._load_phase3_tick(eventtime=1.5)

    assert feeder._state == buffer_feeder.STATE_IDLE


def test_phase3_exit_stays_idle_when_no_entrance_filament():
    """Without filament at entrance, IDLE is the safe state."""
    _, feeder = make_feeder()
    feeder._state = buffer_feeder.STATE_LOAD_PHASE_3
    feeder._load_phase3_overflow_ok = True
    feeder._load_phase3_stable_timeout = 1.0
    feeder._load_phase3_max_distance = 2000.0
    feeder._load_phase3_chunk_distance = 50.0
    feeder._load_phase3_speed = 30.0
    feeder._load_phase3_distance = 0.0
    feeder._load_phase3_hall_overflow_since = 0.0
    set_sensor_active(feeder, 'hall_overflow', True)
    # entrance NOT set

    feeder._load_phase3_tick(eventtime=1.5)

    assert feeder._state == buffer_feeder.STATE_IDLE


# ---------------------------------------------------------------------------
# Fix B — AUTO_ON_IF_READY accepts HALL1 when post_load_grace
# ---------------------------------------------------------------------------

def test_auto_on_if_ready_engages_under_post_load_grace_despite_hall1():
    """grace=True means HALL1 active is the legitimate post-LOAD
    state. AUTO_ON_IF_READY must engage rather than skip."""
    printer, feeder = make_feeder()
    set_sensor_active(feeder, 'entrance', True)
    set_sensor_active(feeder, 'hall_overflow', True)
    feeder._post_load_overflow_grace = True

    feeder.cmd_BUFFER_AUTO_ON_IF_READY(FakeGCmd())

    assert feeder._state == buffer_feeder.STATE_AUTO


def test_auto_on_if_ready_still_skips_hall1_without_grace():
    """grace=False means HALL1 is a fault, not a LOAD-success state.
    AUTO_ON_IF_READY must skip with the block-reason logged."""
    printer, feeder = make_feeder()
    set_sensor_active(feeder, 'entrance', True)
    set_sensor_active(feeder, 'hall_overflow', True)
    feeder._post_load_overflow_grace = False

    feeder.cmd_BUFFER_AUTO_ON_IF_READY(FakeGCmd())

    assert feeder._state != buffer_feeder.STATE_AUTO
    gcode = printer.lookup_object('gcode')
    assert any("AUTO not engaged" in m for m in gcode.info_messages)


def test_auto_on_if_ready_still_skips_jam_even_with_grace():
    """JAM is always blocking — grace flag does not bypass it.
    Setup with HALL1 NOT active so the only block-reason is JAM."""
    printer, feeder = make_feeder()
    set_sensor_active(feeder, 'entrance', True)
    set_sensor_active(feeder, 'hall_overflow', False)
    feeder._post_load_overflow_grace = True
    feeder._jam_active = True
    state_before = feeder._state

    feeder.cmd_BUFFER_AUTO_ON_IF_READY(FakeGCmd())

    assert feeder._state == state_before, (
        "AUTO_ON_IF_READY engaged AUTO despite JAM — grace flag must "
        "not bypass JAM lockout")
    gcode = printer.lookup_object('gcode')
    assert any("JAM" in m for m in gcode.info_messages)


def test_hard_buffer_auto_on_still_raises_under_grace():
    """Hard BUFFER_AUTO_ON keeps its strict semantic — only the
    _IF_READY variant is grace-aware. Direct user invocations
    surface HALL1 as an error so the operator knows the buffer
    is full when they expected it empty."""
    import pytest

    _, feeder = make_feeder()
    set_sensor_active(feeder, 'hall_overflow', True)
    feeder._post_load_overflow_grace = True

    with pytest.raises(Exception, match="HALL1 overflow active"):
        feeder.cmd_BUFFER_AUTO_ON(FakeGCmd())
