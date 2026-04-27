"""P7-48 — Phase 3 stops feeding while HALL1 is active.

Hardware-Test 2026-04-27: User reported that during LOAD, "extrem viel
Filament wird in die Düse gedrückt bevor der Extruder das Filament
reinzieht". Diagnosis traced to _load_phase3_tick: while HALL1 was
asserted (buffer overfilled), the stable-timer counted up — but the
chunk-submission path had no HALL1 guard, so additional 50mm chunks
were submitted at feed_speed=30mm/s. Over the 10s STABLE_TIMEOUT,
that's up to 6 extra chunks (~300mm) of filament being pushed against
the extruder clamp through the bowden — which then bled through the
heatbreak into the nozzle as the bowden compressed and the clamp
gave way.

Two fixes:

1. STABLE_TIMEOUT lowered from 10s to 1s (lll.cfg) — quick mitigation,
   reduces the worst-case overshoot from ~300mm to ~30mm.

2. _load_phase3_tick skips chunk-submission while hall_overflow is
   active (this file's fix). The stable-timer continues counting; if
   HALL1 falls (drop-grace), submission resumes naturally.
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
    feeder._pin_stable_state[sensor_name] = (not active) if polarity_flip else active


def test_phase3_tick_does_not_submit_while_hall1_active():
    """The bug: while HALL1 stable-timer counts up, chunks were still
    submitted, stuffing 300mm of filament against the extruder clamp.

    Fix: hall_overflow=True → no submit, stable-timer keeps counting.
    """
    printer, feeder = make_feeder()
    motion_q = printer.lookup_object('motion_queuing')

    feeder._state = buffer_feeder.STATE_LOAD_PHASE_3
    feeder._load_phase3_overflow_ok = True
    feeder._load_phase3_stable_timeout = 5.0   # would let ~3 chunks through pre-fix
    feeder._load_phase3_max_distance = 2000.0
    feeder._load_phase3_chunk_distance = 50.0
    feeder._load_phase3_speed = 30.0
    feeder._load_phase3_distance = 0.0
    feeder._load_phase3_hall_overflow_since = None

    # HALL1 active throughout
    set_sensor_active(feeder, 'hall_overflow', True)

    appends_before = len(motion_q.append_calls)

    # Tick a few times during the stable-timer window.
    feeder._load_phase3_tick(eventtime=0.0)
    feeder._load_phase3_tick(eventtime=0.5)
    feeder._load_phase3_tick(eventtime=1.0)
    feeder._load_phase3_tick(eventtime=1.5)
    # Verify timer is counting (not yet at threshold)
    dwell = 1.5 - feeder._load_phase3_hall_overflow_since
    assert dwell > 0, "stable timer should be counting"

    # No new appends on own_trapq during the HALL1-stable window.
    new_appends = motion_q.append_calls[appends_before:]
    own_appends = [c for c in new_appends if c[0] is feeder.trapq]
    assert not own_appends, (
        "P7-48 broken: %d chunk(s) submitted during HALL1-stable window"
        % len(own_appends))


def test_phase3_tick_resumes_submission_when_hall1_drops():
    """If HALL1 transient-falls during the stable-timer window, the
    submit path must arm again so Phase 3 keeps making progress."""
    printer, feeder = make_feeder()
    motion_q = printer.lookup_object('motion_queuing')

    feeder._state = buffer_feeder.STATE_LOAD_PHASE_3
    feeder._load_phase3_overflow_ok = True
    feeder._load_phase3_stable_timeout = 5.0
    feeder._load_phase3_max_distance = 2000.0
    feeder._load_phase3_chunk_distance = 50.0
    feeder._load_phase3_speed = 30.0
    feeder._load_phase3_distance = 0.0
    feeder._load_phase3_hall_overflow_since = None

    set_sensor_active(feeder, 'hall_overflow', True)
    feeder._load_phase3_tick(eventtime=0.0)
    appends_after_stable = len(motion_q.append_calls)

    # HALL1 falls (transient spike was over)
    set_sensor_active(feeder, 'hall_overflow', False)
    feeder._load_phase3_tick(eventtime=0.5)

    new_appends = motion_q.append_calls[appends_after_stable:]
    own_appends = [c for c in new_appends if c[0] is feeder.trapq]
    assert own_appends, (
        "Phase 3 stopped feeding entirely after HALL1 fell — should "
        "resume the chunk stream")


def test_phase3_tick_exits_when_hall1_stable_timeout_reached():
    """The original happy path stays intact: HALL1 stable for >=
    threshold seconds → state transition to AUTO/IDLE."""
    printer, feeder = make_feeder()
    feeder._state = buffer_feeder.STATE_LOAD_PHASE_3
    feeder._load_phase3_overflow_ok = True
    feeder._load_phase3_stable_timeout = 1.0   # P7-48 default
    feeder._load_phase3_max_distance = 2000.0
    feeder._load_phase3_chunk_distance = 50.0
    feeder._load_phase3_speed = 30.0
    feeder._load_phase3_distance = 0.0
    feeder._load_phase3_hall_overflow_since = 0.0
    feeder._print_running = False
    set_sensor_active(feeder, 'hall_overflow', True)
    set_sensor_active(feeder, 'entrance', True)

    # Tick at eventtime = stable_timeout → exit triggers
    feeder._load_phase3_tick(eventtime=1.5)

    # P7-49: Phase 3 exit goes to AUTO when entrance is detected,
    # regardless of _print_running. This is the new "deliberate-LOAD-
    # ends-in-AUTO" semantic so bang-bang refills the buffer for the
    # next manual extrusion or print start.
    assert feeder._state == buffer_feeder.STATE_AUTO, (
        "Phase 3 should exit to AUTO after stable timeout when "
        "entrance is detected — got %s" % feeder._state)
    assert feeder._post_load_overflow_grace is True
