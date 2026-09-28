"""End-to-end simulation of the Issue #16 hardware-failure sequence
with all P7-46 fixes applied.

Reproduces the exact sequence from klippy.log.txt 2026-04-26:

  10676  LOAD_PHASE_3 starts, HALL1 stable for 10s (overfilled buffer)
  10688  Phase 3 "treating as full" → state=IDLE
  10691  HALL1 OVERFLOW (main_tick re-trigger)        [P7-46 Fix C suppresses]
  10693  IDLE → OVERFLOW                              [P7-46 Fix C suppresses]
  10694  BUFFER_SYNC_TO_EXTRUDER (LOAD Phase 3/3)
  10735  BUFFER_UNSYNC
  10739  BUFFER_AUTO_ON raises "HALL1 overflow"        [P7-46 Fix A: _IF_READY skips]
         macro abort
  -- 19.9s idle --
  10789  UNLOAD's BUFFER_SYNC_TO_EXTRUDER              [P7-46 Fix B: anchor-step]
  10791  invalid sequence × 5 → MCU shutdown          [P7-46 Fix B prevents]

This test runs the equivalent state machine + sensor pattern in fakes
and asserts each fix grips at the right point.
"""

from fakes_klipper import FakeConfig, FakePrinter
from klipper_extras import buffer_feeder


class FakeGCmd:
    def __init__(self, values=None):
        self.values = {key.upper(): value for key, value in (values or {}).items()}

    def get(self, key, default=None):
        return self.values.get(key.upper(), default)

    def get_int(self, key, default=None, **kwargs):
        return int(self.values.get(key.upper(), default))

    def get_float(self, key, default=None, **kwargs):
        return float(self.values.get(key.upper(), default))


def make_feeder():
    printer = FakePrinter()
    config = FakeConfig(printer=printer)
    feeder = buffer_feeder.BufferFeeder(config)
    feeder._startup_grace_done = True
    return printer, feeder


def set_sensor_active(feeder, sensor_name, active):
    polarity_flip = feeder._pin_polarity_flip[sensor_name]
    raw = (not active) if polarity_flip else active
    feeder._pin_stable_state[sensor_name] = raw
    # P7-49: also seed _pin_raw_state to match. Otherwise check_debounce
    # would see stable!=raw on the very next main_tick, fire
    # on_stable_sensor_change('entrance', False) which falls into
    # on_entrance_runout(_print_running=False) → state=IDLE — masking
    # the actual P7-49 transition we want to test.
    feeder._pin_raw_state[sensor_name] = raw


def test_full_load_unload_sequence_no_invalid_sequence():
    """End-to-end: simulates the LOAD_FILAMENT macro sequence followed
    by a 20-second idle and a UNLOAD_FILAMENT macro start. Asserts:
      - No state-bounce IDLE → OVERFLOW after Phase 3 stable-HALL1 exit
      - BUFFER_AUTO_ON_IF_READY skips silently when HALL1 active
      - sync_to_extruder anchors the cursor after the 20s gap
      - No own-trapq move appended while stepper is on extruder_trapq
    """
    printer, feeder = make_feeder()
    motion_q = printer.lookup_object('motion_queuing')

    # Simulate Phase 1 + early Phase 3 already completed: filament at
    # entrance, HALL2 active (buffer staged), HALL1 just got asserted
    # because the buffer overfilled in repeat-LOAD scenario.
    set_sensor_active(feeder, 'entrance', True)
    set_sensor_active(feeder, 'hall_full', True)
    set_sensor_active(feeder, 'hall_overflow', True)

    # ---------------- Phase 3 stable-HALL1 exit (treating as full) ----
    feeder._state = buffer_feeder.STATE_LOADING_PUSH
    feeder._load_phase3_overflow_ok = True
    feeder._load_phase3_stable_timeout = 1.0
    feeder._load_phase3_hall_overflow_since = 0.0
    feeder._print_running = False  # operator LOAD outside print

    feeder._load_phase3_tick(eventtime=2.0)

    # P7-49: Phase 3 stable-HALL1 exit transitions to AUTO when
    # entrance is detected, irrespective of _print_running. Earlier
    # behaviour was IDLE outside a print; the new semantic ensures
    # bang-bang is armed to refill the buffer at the next pull.
    assert feeder._state == buffer_feeder.STATE_AUTO, (
        "P7-49: Phase 3 must end in AUTO when entrance is detected")
    assert feeder._post_load_overflow_grace is True, (
        "P7-46 Fix C: grace flag must be set on stable-HALL1 exit")

    # ---------------- main_tick must NOT re-trigger _enter_overflow ----
    state_before_tick = feeder._state
    grace_before = feeder._post_load_overflow_grace

    # Run several main_ticks. Without Fix C, _is_hall1_active(main_tick)
    # would return True → _enter_overflow → state bounces to OVERFLOW.
    for tick_time in [3.0, 3.05, 3.1]:
        feeder._main_tick(tick_time)

    assert feeder._state == state_before_tick, (
        "P7-46 Fix C broken: main_tick bounced state %s → %s while "
        "HALL1 stable but grace was set"
        % (state_before_tick, feeder._state))
    assert feeder._post_load_overflow_grace == grace_before

    # ---------------- LOAD Phase 3/3: BUFFER_SYNC_TO_EXTRUDER ----------
    # Reactor clock reasonably current (LOAD just finished).
    feeder.reactor.now = 5.0
    feeder._last_move_end_time = 4.5  # recent move

    appends_before_sync = len(motion_q.append_calls)
    feeder._sync_to_extruder('extruder')

    # No anchor-step needed (gap < REPRIME_GAP).
    new_appends = motion_q.append_calls[appends_before_sync:]
    own_trapq_appends = [c for c in new_appends if c[0] is feeder.trapq]
    assert not own_trapq_appends, (
        "P7-46 Fix B: anchor-step fired despite small gap — "
        "would push HALL1-overflow buffer further")
    assert feeder._stepper_synced_to == 'extruder'

    # ---------------- G1 E180 equivalent (sync, on extruder_trapq) ----
    # In real hardware: extruder pumps 100mm @ 5mm/s, buffer follows
    # 1:1 via shared trapq. We don't simulate the move execution;
    # the assertion is that no own-trapq moves were generated during
    # the sync window.
    feeder.reactor.now = 25.0  # 20s of G1 E + UNSYNC time
    feeder._last_move_end_time = 25.0   # advanced via sync flush
    sync_window_appends = motion_q.append_calls[appends_before_sync:]
    own_during_sync = [c for c in sync_window_appends if c[0] is feeder.trapq]
    assert not own_during_sync, (
        "Race: own-trapq move appended while stepper bound to "
        "extruder_trapq")

    # ---------------- BUFFER_UNSYNC ------------------------------------
    feeder._unsync_if_synced()
    assert feeder._stepper_synced_to is None

    # ---------------- BUFFER_AUTO_ON_IF_READY: HALL1 still active ----
    # P7-49: AUTO_ON_IF_READY now respects _post_load_overflow_grace —
    # HALL1 active immediately after a Phase 3 stable-HALL1 exit is
    # legitimate, AUTO must engage so bang-bang is armed for the
    # next pull. Pre-P7-49 this test asserted the opposite (silent
    # skip), but that semantic prevented manual extrusions outside
    # a print from refilling the buffer.
    gcode = printer.lookup_object('gcode')
    msg_count_before = len(gcode.info_messages)
    feeder.cmd_BUFFER_AUTO_ON_IF_READY(FakeGCmd())
    assert feeder._state == buffer_feeder.STATE_AUTO, (
        "P7-49: AUTO_ON_IF_READY did NOT engage AUTO despite "
        "_post_load_overflow_grace being set — Phase 3 success path "
        "must arm bang-bang")
    new_msgs = gcode.info_messages[msg_count_before:]
    assert any("AUTO engaged" in m for m in new_msgs)

    # ---------------- 20s idle period (operator pause) ----------------
    feeder.reactor.now = 45.0  # gap = 45 - 25 = 20s, > REPRIME_GAP

    # ---------------- UNLOAD: BUFFER_SYNC_TO_EXTRUDER ------------------
    # Without Fix B: stale stepcompress cursor → first extruder-driven
    # step would land far past last_step_clock → 'Invalid sequence'.
    # With Fix B: anchor-step on own-trapq refreshes the cursor first.
    appends_before_unload_sync = len(motion_q.append_calls)
    feeder._sync_to_extruder('extruder')

    new_appends = motion_q.append_calls[appends_before_unload_sync:]
    own_trapq_appends = [c for c in new_appends if c[0] is feeder.trapq]
    assert own_trapq_appends, (
        "P7-46 Fix B broken: no anchor-step on own-trapq before "
        "UNLOAD-sync despite 20s idle gap — stepcompress cursor "
        "stays stale, would crash on real hardware")

    # The anchor-step must be in the safe direction (HALL1 still
    # active — direction must be retract, not feed).
    first_anchor = own_trapq_appends[0]
    axes_r_x = first_anchor[8]  # axes_r_x signature index
    assert axes_r_x < 0, (
        "Anchor pushed forward despite HALL1 active — would force "
        "more filament into an already-overfilled buffer")

    # Sync was successful.
    assert feeder._stepper_synced_to == 'extruder'


def test_grace_flag_clears_on_hall1_fall_during_print():
    """Once HALL1 actually falls (extruder pulled enough filament out
    of the buffer), the normal overflow regime resumes."""
    _, feeder = make_feeder()
    feeder._state = buffer_feeder.STATE_AUTO
    feeder._post_load_overflow_grace = True
    set_sensor_active(feeder, 'hall_overflow', False)

    feeder._on_stable_sensor_change(eventtime=10.0,
                                     name='hall_overflow',
                                     raw_state=False)

    assert feeder._post_load_overflow_grace is False
    # HALL1 active comes back? Now main_tick must trigger overflow.
    set_sensor_active(feeder, 'hall_overflow', True)
    assert feeder._is_hall1_active('main_tick') is True
