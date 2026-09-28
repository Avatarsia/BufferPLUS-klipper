"""P7-68 — BUFFER_SET runtime parameter tuning (Issue #28).

Hot-swap setter for five buffer parameters without Klipper restart.
Issue explicitly REJECTS persistence — operator copies the final value
to lll.cfg manually after hardware test.

Covered parameters:
- CHUNK_MM            -> flush_callback_chunk_mm
- SPEED               -> feed_speed
- ACCEL               -> accel
- INTERRUPT_CHUNK_MM  -> interrupt_chunk_mm (capped <= max_move_chunk_mm)
- LEAD_TIME           -> lead_time (warn outside 0.05..1.0)
- MAX_MOVE_CHUNK_MM   -> max_move_chunk_mm
- FEED_SPEED_GAIN     -> feed_speed_gain
- MIN_FEED_FLOOR      -> min_feed_floor
- FILAMENT_DIAMETER   -> filament_diameter
- DEBUG_EVENTS        -> buffer_debug_events
- DEBUG_METRICS       -> buffer_debug_metrics
- STRICT_START_GUARD  -> strict_print_start_guard
- CRITICAL_GUARD_S    -> critical_action_guard_s
- CONSERVATIVE_MODE   -> buffer_conservative_mode
- HIGH_FLOW_MM3S      -> high_flow_mm3s_threshold

Also: default-mux registration so BUFFER_SET works without BUFFER=mellow
on single-instance setups (NOT-TO-DO 2026-04-26: register_mux_command with
BUFFER= selector is mandatory; the default-mux fallback re-registers each
command with mux_value=None when only one instance exists).
"""

from fakes_klipper import FakeConfig, FakePrinter
from klipper_extras import buffer_feeder


# ---------------------------------------------------------------------------
# Local FakeGCmd: returns the default UNCHANGED when key is absent (the
# conftest copy unconditionally float(default) and would crash on None).
# ---------------------------------------------------------------------------

class FakeGCmd:
    def __init__(self, values=None):
        self.values = {key.upper(): value for key, value in (values or {}).items()}

    def get(self, key, default=None):
        return self.values.get(key.upper(), default)

    def get_int(self, key, default=None, **kwargs):
        v = self.values.get(key.upper(), default)
        return None if v is None else int(v)

    def get_float(self, key, default=None, **kwargs):
        v = self.values.get(key.upper(), default)
        return None if v is None else float(v)


def make_feeder(values=None):
    printer = FakePrinter()
    config = FakeConfig(printer=printer, values=values)
    feeder = buffer_feeder.BufferFeeder(config)
    feeder._startup_grace_done = True
    feeder._state = buffer_feeder.STATE_IDLE
    return printer, feeder


# ---------------------------------------------------------------------------
# Single-parameter hot-swap tests
# ---------------------------------------------------------------------------

def test_buffer_set_chunk_mm_updates_flush_callback_chunk():
    printer, feeder = make_feeder()
    old = feeder.flush_callback_chunk_mm
    feeder.cmd_BUFFER_SET(FakeGCmd({"CHUNK_MM": 20}))
    assert feeder.flush_callback_chunk_mm == 20.0
    gcode = printer.lookup_object('gcode')
    # Audit-trail: old -> new line emitted
    assert any('flush_callback_chunk_mm' in m and '%.3f' % old in m
               for m in gcode.info_messages)


def test_buffer_set_speed_updates_feed_speed():
    printer, feeder = make_feeder()
    feeder.cmd_BUFFER_SET(FakeGCmd({"SPEED": 50}))
    assert feeder.feed_speed == 50.0
    gcode = printer.lookup_object('gcode')
    assert any('feed_speed' in m for m in gcode.info_messages)


def test_buffer_set_accel_updates_accel():
    printer, feeder = make_feeder()
    feeder.cmd_BUFFER_SET(FakeGCmd({"ACCEL": 1500}))
    assert feeder.accel == 1500.0
    gcode = printer.lookup_object('gcode')
    assert any('accel' in m for m in gcode.info_messages)


def test_buffer_set_lead_time_updates_lead_time():
    printer, feeder = make_feeder()
    feeder.cmd_BUFFER_SET(FakeGCmd({"LEAD_TIME": 0.3}))
    assert feeder.lead_time == 0.3
    gcode = printer.lookup_object('gcode')
    assert any('lead_time' in m for m in gcode.info_messages)


def test_buffer_set_interrupt_chunk_mm_updates_value():
    printer, feeder = make_feeder()
    feeder.cmd_BUFFER_SET(FakeGCmd({"INTERRUPT_CHUNK_MM": 12}))
    assert feeder.interrupt_chunk_mm == 12.0


def test_buffer_set_max_move_chunk_mm_updates_value():
    printer, feeder = make_feeder()
    feeder.cmd_BUFFER_SET(FakeGCmd({"MAX_MOVE_CHUNK_MM": 80}))
    assert feeder.max_move_chunk_mm == 80.0


# ---------------------------------------------------------------------------
# Cap behaviour: INTERRUPT_CHUNK_MM must not exceed max_move_chunk_mm.
# Chosen variant: CAP (not raise) so the operator can issue a single
# command. Mirrors buffer_feeder.py:826 init-cap behaviour.
# ---------------------------------------------------------------------------

def test_buffer_set_interrupt_above_max_caps_to_max():
    printer, feeder = make_feeder()
    # default max_move_chunk_mm = 50.0 (from Python default)
    assert feeder.max_move_chunk_mm == 50.0
    feeder.cmd_BUFFER_SET(FakeGCmd({"INTERRUPT_CHUNK_MM": 100}))
    assert feeder.interrupt_chunk_mm == 50.0  # capped
    gcode = printer.lookup_object('gcode')
    assert any('cap' in m.lower() for m in gcode.info_messages)


def test_buffer_set_raising_max_then_interrupt_in_one_call():
    """Operator can raise both in a single command — MAX_MOVE_CHUNK_MM
    is applied first so the new INTERRUPT_CHUNK_MM ceiling is in place."""
    printer, feeder = make_feeder()
    feeder.cmd_BUFFER_SET(FakeGCmd({
        "MAX_MOVE_CHUNK_MM": 100,
        "INTERRUPT_CHUNK_MM": 80,
    }))
    assert feeder.max_move_chunk_mm == 100.0
    assert feeder.interrupt_chunk_mm == 80.0  # NOT capped — 80 <= 100


def test_buffer_set_lowering_max_caps_existing_interrupt():
    """If MAX_MOVE_CHUNK_MM is lowered below current interrupt, the
    interrupt is auto-capped down (mirrors init-time invariant)."""
    printer, feeder = make_feeder()
    # Seed a state where interrupt is at 40 and max is 50.
    feeder.interrupt_chunk_mm = 40.0
    feeder.max_move_chunk_mm = 50.0
    feeder.cmd_BUFFER_SET(FakeGCmd({"MAX_MOVE_CHUNK_MM": 20}))
    assert feeder.max_move_chunk_mm == 20.0
    assert feeder.interrupt_chunk_mm == 20.0  # auto-capped


# ---------------------------------------------------------------------------
# Lead-time hardware warning (not raise)
# ---------------------------------------------------------------------------

def test_buffer_set_lead_time_above_1_warns_but_applies():
    printer, feeder = make_feeder()
    feeder.cmd_BUFFER_SET(FakeGCmd({"LEAD_TIME": 1.5}))
    assert feeder.lead_time == 1.5  # value applied
    gcode = printer.lookup_object('gcode')
    assert any('WARNING' in m and 'lead_time' in m
               for m in gcode.info_messages)


def test_buffer_set_lead_time_below_0_05_warns_but_applies():
    printer, feeder = make_feeder()
    feeder.cmd_BUFFER_SET(FakeGCmd({"LEAD_TIME": 0.01}))
    assert feeder.lead_time == 0.01
    gcode = printer.lookup_object('gcode')
    assert any('WARNING' in m and 'lead_time' in m
               for m in gcode.info_messages)


def test_buffer_set_lead_time_in_range_no_warning():
    printer, feeder = make_feeder()
    feeder.cmd_BUFFER_SET(FakeGCmd({"LEAD_TIME": 0.12}))
    assert feeder.lead_time == 0.12
    gcode = printer.lookup_object('gcode')
    assert not any('WARNING' in m for m in gcode.info_messages)


# ---------------------------------------------------------------------------
# Multi-parameter call
# ---------------------------------------------------------------------------

def test_buffer_set_multiple_params_at_once():
    printer, feeder = make_feeder()
    feeder.cmd_BUFFER_SET(FakeGCmd({
        "CHUNK_MM": 25,
        "SPEED": 60,
        "LEAD_TIME": 0.15,
    }))
    assert feeder.flush_callback_chunk_mm == 25.0
    assert feeder.feed_speed == 60.0
    assert feeder.lead_time == 0.15


def test_buffer_set_debug_flags_toggle():
    printer, feeder = make_feeder()
    assert feeder.buffer_debug_events is False
    assert feeder.buffer_debug_metrics is False

    feeder.cmd_BUFFER_SET(FakeGCmd({
        "DEBUG_EVENTS": 1,
        "DEBUG_METRICS": 1,
    }))

    assert feeder.buffer_debug_events is True
    assert feeder.buffer_debug_metrics is True
    gcode = printer.lookup_object('gcode')
    joined = "\n".join(gcode.info_messages)
    assert 'buffer_debug_events' in joined
    assert 'buffer_debug_metrics' in joined


def test_buffer_set_transition_guard_flags_toggle():
    printer, feeder = make_feeder()
    assert feeder.strict_print_start_guard is True
    assert feeder.critical_action_guard_s == 0.35
    assert feeder.buffer_conservative_mode is False

    feeder.cmd_BUFFER_SET(FakeGCmd({
        "STRICT_START_GUARD": 0,
        "CRITICAL_GUARD_S": 0.8,
        "CONSERVATIVE_MODE": 1,
    }))

    assert feeder.strict_print_start_guard is False
    assert feeder.critical_action_guard_s == 0.8
    assert feeder.buffer_conservative_mode is True
    joined = "\n".join(printer.lookup_object('gcode').info_messages)
    assert 'strict_print_start_guard' in joined
    assert 'critical_action_guard_s' in joined
    assert 'buffer_conservative_mode' in joined


def test_buffer_set_high_flow_threshold_updates_value():
    printer, feeder = make_feeder()
    assert feeder.high_flow_mm3s_threshold == 24.0

    feeder.cmd_BUFFER_SET(FakeGCmd({
        "HIGH_FLOW_MM3S": 28.5,
    }))

    assert feeder.high_flow_mm3s_threshold == 28.5
    joined = "\n".join(printer.lookup_object('gcode').info_messages)
    assert 'high_flow_mm3s_threshold' in joined


def test_buffer_set_feed_gain_updates_value():
    printer, feeder = make_feeder()
    assert feeder.feed_speed_gain == 1.10

    feeder.cmd_BUFFER_SET(FakeGCmd({"FEED_SPEED_GAIN": 1.25}))

    assert feeder.feed_speed_gain == 1.25
    joined = "\n".join(printer.lookup_object('gcode').info_messages)
    assert 'feed_speed_gain' in joined


def test_buffer_set_min_feed_floor_updates_value():
    printer, feeder = make_feeder()
    assert feeder.min_feed_floor == 15.0

    feeder.cmd_BUFFER_SET(FakeGCmd({"MIN_FEED_FLOOR": 12.5}))

    assert feeder.min_feed_floor == 12.5
    joined = "\n".join(printer.lookup_object('gcode').info_messages)
    assert 'min_feed_floor' in joined


def test_buffer_set_jam_action_disabled_by_none_keyword():
    printer, feeder = make_feeder()
    feeder.jam_action = "PAUSE"

    feeder.cmd_BUFFER_SET(FakeGCmd({"JAM_ACTION": "NONE"}))

    assert feeder.jam_action == ""
    joined = "\n".join(printer.lookup_object('gcode').info_messages)
    assert 'jam_action' in joined


def test_buffer_set_jam_action_disabled_by_disabled_keyword():
    printer, feeder = make_feeder()
    feeder.jam_action = "PAUSE"

    feeder.cmd_BUFFER_SET(FakeGCmd({"JAM_ACTION": "DISABLED"}))

    assert feeder.jam_action == ""


def test_buffer_set_jam_action_restored_to_pause():
    printer, feeder = make_feeder()
    feeder.jam_action = ""

    feeder.cmd_BUFFER_SET(FakeGCmd({"JAM_ACTION": "PAUSE"}))

    assert feeder.jam_action == "PAUSE"


def test_buffer_set_filament_diameter_updates_tracker_cross_section():
    printer, feeder = make_feeder()
    before = feeder.velocity_tracker._cross_section

    feeder.cmd_BUFFER_SET(FakeGCmd({"FILAMENT_DIAMETER": 2.85}))

    assert feeder.filament_diameter == 2.85
    assert feeder.velocity_tracker._cross_section != before
    joined = "\n".join(printer.lookup_object('gcode').info_messages)
    assert 'filament_diameter' in joined


# ---------------------------------------------------------------------------
# No-op without args: emit current values
# ---------------------------------------------------------------------------

def test_buffer_set_no_args_dumps_current_values():
    printer, feeder = make_feeder()
    feeder.cmd_BUFFER_SET(FakeGCmd({}))
    gcode = printer.lookup_object('gcode')
    # All five params must appear in the info dump
    joined = "\n".join(gcode.info_messages)
    assert 'flush_callback_chunk_mm' in joined
    assert 'feed_speed' in joined
    assert 'accel' in joined
    assert 'interrupt_chunk_mm' in joined
    assert 'lead_time' in joined
    assert 'max_move_chunk_mm' in joined
    assert 'feed_speed_gain' in joined
    assert 'min_feed_floor' in joined
    assert 'filament_diameter' in joined
    assert 'buffer_debug_events' in joined
    assert 'buffer_debug_metrics' in joined
    assert 'strict_print_start_guard' in joined
    assert 'critical_action_guard_s' in joined
    assert 'buffer_conservative_mode' in joined
    assert 'high_flow_mm3s_threshold' in joined


def test_buffer_set_no_args_does_not_change_state():
    printer, feeder = make_feeder()
    before = (feeder.flush_callback_chunk_mm, feeder.feed_speed,
              feeder.accel, feeder.interrupt_chunk_mm, feeder.lead_time,
              feeder.max_move_chunk_mm, feeder.feed_speed_gain,
              feeder.min_feed_floor, feeder.filament_diameter)
    feeder.cmd_BUFFER_SET(FakeGCmd({}))
    after = (feeder.flush_callback_chunk_mm, feeder.feed_speed,
             feeder.accel, feeder.interrupt_chunk_mm, feeder.lead_time,
             feeder.max_move_chunk_mm, feeder.feed_speed_gain,
             feeder.min_feed_floor, feeder.filament_diameter)
    assert before == after


# ---------------------------------------------------------------------------
# Mux registration: mandatory BUFFER= + default-mux fallback for single
# instance (NOT-TO-DO 2026-04-26).
# ---------------------------------------------------------------------------

def test_buffer_set_registered_with_mandatory_buffer_mux():
    """BUFFER_SET must be registered via register_mux_command with key
    'BUFFER' so multi-instance setups dispatch correctly."""
    printer, feeder = make_feeder()
    gcode = printer.lookup_object('gcode')
    assert 'BUFFER_SET' in gcode.mux_commands
    regs = gcode.mux_commands['BUFFER_SET']
    # First (mandatory) registration uses the feeder's name as mux_value
    assert any(r['mux_key'] == 'BUFFER' and r['mux_value'] == feeder.name
               for r in regs)


def test_buffer_set_default_mux_registered_when_single_instance():
    """P7-62 default-mux: when only one buffer_feeder instance exists,
    every command (incl. BUFFER_SET) gets a second registration with
    mux_value=None so the user can call BUFFER_SET without BUFFER=."""
    printer = FakePrinter()
    config = FakeConfig(printer=printer)
    feeder = buffer_feeder.BufferFeeder(config)
    # Inject this single instance into printer.objects so
    # _register_default_mux_if_only_instance can count it.
    printer.objects['buffer_feeder ' + feeder.name] = feeder
    printer.fire_event('klippy:ready')

    gcode = printer.lookup_object('gcode')
    regs = gcode.mux_commands['BUFFER_SET']
    assert any(r['mux_key'] == 'BUFFER' and r['mux_value'] is None
               for r in regs), \
        "default-mux (BUFFER=None) fallback missing for BUFFER_SET"


# ---------------------------------------------------------------------------
# Audit-trail / help-text smoke
# ---------------------------------------------------------------------------

def test_buffer_set_help_text_lists_all_params():
    help_text = buffer_feeder.BufferFeeder.cmd_BUFFER_SET_help
    for token in ('CHUNK_MM', 'SPEED', 'ACCEL', 'INTERRUPT_CHUNK_MM',
                  'LEAD_TIME', 'MAX_MOVE_CHUNK_MM',
                  'FEED_SPEED_GAIN', 'MIN_FEED_FLOOR',
                  'FILAMENT_DIAMETER',
                  'DEBUG_EVENTS', 'DEBUG_METRICS',
                  'STRICT_START_GUARD', 'CRITICAL_GUARD_S',
                  'HIGH_FLOW_MM3S',
                  'CONSERVATIVE_MODE'):
        assert token in help_text, "help missing %s" % token


def test_buffer_set_emits_old_to_new_audit_line():
    printer, feeder = make_feeder()
    feeder.feed_speed = 30.0
    feeder.cmd_BUFFER_SET(FakeGCmd({"SPEED": 70}))
    gcode = printer.lookup_object('gcode')
    audit = [m for m in gcode.info_messages if 'feed_speed' in m]
    assert audit, "no audit line emitted for feed_speed change"
    # Must contain both old and new value
    line = audit[0]
    assert '30.000' in line and '70.000' in line
