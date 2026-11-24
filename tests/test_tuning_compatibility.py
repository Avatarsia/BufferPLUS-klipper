"""Behavioral contracts for the config/runtime/status tuning refactor."""

import configparser
import math
from pathlib import Path

import pytest

from fakes_klipper import FakeConfig
from helpers import FakeGCmd, build_feeder
from klipper_extras.buffer_config import BufferConfigValues


RUNTIME_VALUES = {
    "CHUNK_MM": ("flush_callback_chunk_mm", 22.0),
    "SPEED": ("feed_speed", 110.0),
    "ACCEL": ("accel", 1500.0),
    "INTERRUPT_CHUNK_MM": ("interrupt_chunk_mm", 12.0),
    "LEAD_TIME": ("lead_time", 0.2),
    "MAX_MOVE_CHUNK_MM": ("max_move_chunk_mm", 80.0),
    "FEED_SPEED_GAIN": ("feed_speed_gain", 1.2),
    "HALL3_DEMAND_GAIN": ("hall3_demand_gain", 1.8),
    "JAM_ACTION": ("jam_action", "M117 JAM"),
    "MIN_FEED_FLOOR": ("min_feed_floor", 18.0),
    "FILAMENT_DIAMETER": ("filament_diameter", 2.85),
    "DEBUG_EVENTS": ("buffer_debug_events", True),
    "DEBUG_METRICS": ("buffer_debug_metrics", True),
    "STRICT_START_GUARD": ("strict_print_start_guard", False),
    "CRITICAL_GUARD_S": ("critical_action_guard_s", 0.0),
    "CONSERVATIVE_MODE": ("buffer_conservative_mode", True),
    "HIGH_FLOW_MM3S": ("high_flow_mm3s_threshold", 0.0),
}


@pytest.mark.parametrize("key", RUNTIME_VALUES)
def test_runtime_key_updates_live_value_and_audit(key):
    printer, feeder = build_feeder()
    attr, value = RUNTIME_VALUES[key]
    feeder.cmd_BUFFER_SET(FakeGCmd({key: int(value) if isinstance(value, bool)
                                    else value}))
    assert getattr(feeder, attr) == value
    assert any(attr in message and "->" in message
               for message in printer.lookup_object("gcode").info_messages)


@pytest.mark.parametrize("key,value", [
    (key, 0) for key in ("CHUNK_MM", "SPEED", "ACCEL", "INTERRUPT_CHUNK_MM",
                        "LEAD_TIME", "MAX_MOVE_CHUNK_MM", "MIN_FEED_FLOOR",
                        "FILAMENT_DIAMETER")
] + [("FEED_SPEED_GAIN", 0.99), ("HALL3_DEMAND_GAIN", 0.99),
     ("CRITICAL_GUARD_S", -0.01), ("HIGH_FLOW_MM3S", -0.01)] + [
    (key, value) for key in ("DEBUG_EVENTS", "DEBUG_METRICS",
                            "STRICT_START_GUARD", "CONSERVATIVE_MODE")
    for value in (-1, 2, "true")
])
def test_invalid_runtime_bound_or_parser_leaves_values_unchanged(key, value):
    printer, feeder = build_feeder()
    before = {attr: getattr(feeder, attr) for attr, _ in RUNTIME_VALUES.values()}
    with pytest.raises(ValueError):
        feeder.cmd_BUFFER_SET(FakeGCmd({"SPEED": 80, key: value}))
    assert {attr: getattr(feeder, attr) for attr in before} == before
    assert not printer.lookup_object("gcode").info_messages


@pytest.mark.parametrize("maximum,interrupt,expected", [(100, 80, 80),
                                                       (20, 30, 20),
                                                       (20, 12, 12)])
def test_combined_caps_use_new_maximum(maximum, interrupt, expected):
    printer, feeder = build_feeder(values={"interrupt_chunk_mm": 40})
    feeder.cmd_BUFFER_SET(FakeGCmd({"MAX_MOVE_CHUNK_MM": maximum,
                                    "INTERRUPT_CHUNK_MM": interrupt}))
    assert feeder.interrupt_chunk_mm == expected
    assert feeder.get_status(0)["interrupt_chunk_mm"] == expected
    messages = printer.lookup_object("gcode").info_messages
    assert messages[0].startswith("BUFFER_SET: max_move_chunk_mm")


def test_config_cap_and_speed_validation_are_preserved():
    values = BufferConfigValues.from_config(FakeConfig(values={
        "max_move_chunk_mm": 5, "interrupt_chunk_mm": 9}))
    assert values.interrupt_chunk_mm == 5
    with pytest.raises(RuntimeError, match="must be >= feed_speed"):
        BufferConfigValues.from_config(FakeConfig(values={"feed_speed": 110}))


def test_config_fallbacks_and_example_hardware_profile_remain_distinct():
    config = FakeConfig()
    config.values.clear()
    defaults = BufferConfigValues.from_config(config)
    assert (defaults.feed_speed, defaults.flush_callback_chunk_mm,
            defaults.lead_time) == (30.0, 15.0, 0.3)
    example = configparser.ConfigParser(interpolation=None,
                                        inline_comment_prefixes=("#",))
    example.read(Path(__file__).parents[1] / "lll.cfg", encoding="utf-8")
    profile = example["buffer_feeder mellow"]
    assert tuple(float(profile[key]) for key in (
        "feed_speed", "flush_callback_chunk_mm", "lead_time")) == (32, 45, 0.12)


def test_config_boolean_words_and_runtime_integer_parser_remain_distinct():
    settings = BufferConfigValues.from_config(FakeConfig(values={
        "buffer_debug_events": "true", "buffer_debug_metrics": "0",
        "strict_print_start_guard": "false", "buffer_conservative_mode": "yes"}))
    assert settings.buffer_debug_events is True
    assert settings.buffer_debug_metrics is False
    assert settings.strict_print_start_guard is False
    assert settings.buffer_conservative_mode is True


@pytest.mark.parametrize("action", ["", " none ", " DisAbLeD "])
def test_jam_disable_keywords_apply_only_at_runtime(action):
    _, feeder = build_feeder(values={"jam_action": action})
    assert feeder.jam_action == action.strip()
    feeder.cmd_BUFFER_SET(FakeGCmd({"JAM_ACTION": action}))
    assert feeder.get_status(0)["jam_action"] == ""


def test_runtime_diameter_updates_first_tracker_consumer():
    _, feeder = build_feeder()
    tracker = feeder.velocity_tracker
    tracker._samples.extend([(0, 0), (0.1, 1)])
    feeder.cmd_BUFFER_SET(FakeGCmd({"FILAMENT_DIAMETER": 2.85}))
    expected = 10 * math.pi * (2.85 / 2) ** 2
    assert tracker.get_volumetric_flow() == pytest.approx(expected)
    assert tracker.get_velocity() == 10


def test_status_retains_public_names_and_live_tuning_without_new_keys():
    _, feeder = build_feeder(values={"use_fault_overlay": "true"})
    before = feeder.get_status(0)
    assert before["use_fault_overlay"] is True
    assert before["overflow_overlay_enabled"] is True
    for attr in ("feed_speed_gain", "min_feed_floor", "filament_diameter",
                 "lead_time", "high_flow_mm3s_threshold"):
        assert attr not in before
    feeder.cmd_BUFFER_SET(FakeGCmd({key: int(value) if isinstance(value, bool)
                                    else value
                                    for key, (_, value) in RUNTIME_VALUES.items()}))
    after = feeder.get_status(0)
    assert set(after) == set(before)
    for attr, value in RUNTIME_VALUES.values():
        if attr in after:
            assert after[attr] == value
    feeder.use_fault_overlay = False
    assert feeder.use_overflow_overlay is False
    assert feeder.get_status(0)["use_fault_overlay"] is False


def test_no_argument_dump_preserves_high_flow_label_spacing():
    printer, feeder = build_feeder()
    feeder.cmd_BUFFER_SET(FakeGCmd())
    assert "  high_flow_mm3s_threshold = 24.000 mm3/s" in (
        printer.lookup_object("gcode").info_messages)
