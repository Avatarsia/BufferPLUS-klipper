from dataclasses import dataclass


@dataclass
class BufferConfigValues:
    feed_speed: float
    manual_speed: float
    burst_speed: float
    load_fast_speed: float
    load_slow_speed: float
    unload_fast_speed: float
    unload_phase3_speed: float
    grip_speed: float
    accel: float
    manual_chunk_distance: float
    burst_distance: float
    grip_duration: float
    grip_follow_distance: float
    grip_follow_speed: float
    load_fast_distance: float
    load_slow_distance: float
    load_buffer_max: float
    unload_sync_distance: float
    unload_fast_max: float
    max_feed_time: float
    max_feed_distance: float
    hall_debounce_ms: int
    lead_time: float
    max_move_chunk_mm: float
    flush_callback_chunk_mm: float
    interrupt_chunk_mm: float
    max_feed_speed: float
    hall1_persist_timeout: float
    buffer_debug_events: bool
    buffer_debug_metrics: bool
    strict_print_start_guard: bool
    critical_action_guard_s: float
    buffer_conservative_mode: bool
    min_feed_floor: float
    feed_speed_gain: float
    hall3_demand_gain: float
    high_flow_mm3s_threshold: float
    feed_hysteresis_stop_factor: float
    idle_anchor_gap: float
    idle_motor_disable: bool
    park_full_on_print_end: bool
    park_full_max_mm: float
    jam_detection_enabled: bool
    jam_clog_dwell_time: float
    jam_clog_extrude_min: float
    jam_supply_dwell_time: float
    jam_action: str
    runout_pause: bool
    runout_follow_mm: float
    triple_click_window: float
    feed_burst_enabled: bool
    reenable_cooldown: float
    reenable_cooldown_fast: float
    auto_load_after_follow: bool
    auto_engage_on_print_start: bool
    auto_engage_on_boot: bool
    min_temp: float
    use_overflow_overlay: bool
    use_flush_callback_bang_bang: bool
    filament_diameter: float

    @classmethod
    def from_config(cls, config):
        feed_speed = config.getfloat('feed_speed', 30., above=0.)
        unload_fast_speed = config.getfloat('unload_fast_speed', 50., above=0.)
        max_move_chunk_mm = config.getfloat('max_move_chunk_mm', 50.0, above=0.)
        interrupt_chunk_mm = config.getfloat('interrupt_chunk_mm', 9.0, above=0.)
        if interrupt_chunk_mm > max_move_chunk_mm:
            interrupt_chunk_mm = max_move_chunk_mm
        max_feed_speed = config.getfloat('max_feed_speed', 100.0, above=0.)
        if max_feed_speed < feed_speed:
            raise config.error(
                "max_feed_speed (%.1f) must be >= feed_speed (%.1f)"
                % (max_feed_speed, feed_speed))
        return cls(
            feed_speed=feed_speed,
            manual_speed=config.getfloat('manual_speed', 15., above=0.),
            burst_speed=config.getfloat('burst_speed', 50., above=0.),
            load_fast_speed=config.getfloat('load_fast_speed', 50., above=0.),
            load_slow_speed=config.getfloat('load_slow_speed', 5., above=0.),
            unload_fast_speed=unload_fast_speed,
            unload_phase3_speed=config.getfloat(
                'unload_phase3_speed', unload_fast_speed, above=0.),
            grip_speed=config.getfloat('grip_speed', 55., above=0.),
            accel=config.getfloat('accel', 1000., above=0.),
            manual_chunk_distance=config.getfloat(
                'manual_chunk_distance', 10., above=0.),
            burst_distance=config.getfloat('burst_distance', 1300., above=0.),
            grip_duration=config.getfloat('grip_duration', 10., above=0.),
            grip_follow_distance=config.getfloat(
                'grip_follow_distance', 0., minval=0.),
            grip_follow_speed=config.getfloat(
                'grip_follow_speed', 30., above=0.),
            load_fast_distance=config.getfloat(
                'load_fast_distance', 1000., above=0.),
            load_slow_distance=config.getfloat(
                'load_slow_distance', 180., above=0.),
            load_buffer_max=config.getfloat(
                'load_buffer_max', 2000., above=0.),
            unload_sync_distance=config.getfloat(
                'unload_sync_distance', 400., above=0.),
            unload_fast_max=config.getfloat(
                'unload_fast_max', 5000., above=0.),
            max_feed_time=config.getfloat('max_feed_time', 60., above=0.),
            max_feed_distance=config.getfloat(
                'max_feed_distance', 3000., above=0.),
            hall_debounce_ms=config.getint('hall_debounce_ms', 50, minval=0),
            lead_time=config.getfloat('lead_time', 0.3, above=0.),
            max_move_chunk_mm=max_move_chunk_mm,
            flush_callback_chunk_mm=config.getfloat(
                'flush_callback_chunk_mm', 15.0, above=0.),
            interrupt_chunk_mm=interrupt_chunk_mm,
            max_feed_speed=max_feed_speed,
            hall1_persist_timeout=config.getfloat(
                'hall1_persist_timeout', 2.0, above=0.),
            buffer_debug_events=config.getboolean(
                'buffer_debug_events', False),
            buffer_debug_metrics=config.getboolean(
                'buffer_debug_metrics', False),
            strict_print_start_guard=config.getboolean(
                'strict_print_start_guard', True),
            critical_action_guard_s=config.getfloat(
                'critical_action_guard_s', 0.35, minval=0.0),
            buffer_conservative_mode=config.getboolean(
                'buffer_conservative_mode', False),
            min_feed_floor=config.getfloat('min_feed_floor', 15.0, above=0.),
            feed_speed_gain=config.getfloat(
                'feed_speed_gain', 1.10, minval=1.0),
            hall3_demand_gain=config.getfloat(
                'hall3_demand_gain', 1.5, minval=1.0),
            high_flow_mm3s_threshold=config.getfloat(
                'high_flow_mm3s_threshold', 24.0, minval=0.0),
            feed_hysteresis_stop_factor=config.getfloat(
                'feed_hysteresis_stop_factor', 0.7, minval=0.1, maxval=1.0),
            idle_anchor_gap=config.getfloat('idle_anchor_gap', 10.0, above=0.),
            idle_motor_disable=config.getboolean(
                'idle_motor_disable', False),
            park_full_on_print_end=config.getboolean(
                'park_full_on_print_end', True),
            park_full_max_mm=config.getfloat(
                'park_full_max_mm', 150.0, above=0.),
            jam_detection_enabled=config.getboolean(
                'jam_detection_enabled', True),
            jam_clog_dwell_time=config.getfloat(
                'jam_clog_dwell_time', 60., above=0.),
            jam_clog_extrude_min=config.getfloat(
                'jam_clog_extrude_min', 30., above=0.),
            jam_supply_dwell_time=config.getfloat(
                'jam_supply_dwell_time', 120., above=0.),
            jam_action=config.get('jam_action', 'PAUSE').strip(),
            runout_pause=config.getboolean('runout_pause', False),
            runout_follow_mm=config.getfloat(
                'runout_follow_mm', 100., minval=0.),
            triple_click_window=config.getfloat(
                'triple_click_window', 1.5, above=0.),
            feed_burst_enabled=config.getboolean('feed_burst_enabled', False),
            reenable_cooldown=config.getfloat(
                'reenable_cooldown', 1.0, minval=0.),
            reenable_cooldown_fast=config.getfloat(
                'reenable_cooldown_fast', 0.5, minval=0.),
            auto_load_after_follow=config.getboolean(
                'auto_load_after_follow', False),
            auto_engage_on_print_start=config.getboolean(
                'auto_engage_on_print_start', True),
            auto_engage_on_boot=config.getboolean(
                'auto_engage_on_boot', True),
            min_temp=config.getfloat('min_temp', 180., minval=0.),
            use_overflow_overlay=config.getboolean('use_fault_overlay', False),
            use_flush_callback_bang_bang=config.getboolean(
                'use_flush_callback_bang_bang', False),
            filament_diameter=config.getfloat(
                'filament_diameter', 1.75, above=0.),
        )

    def apply(self, owner):
        for key, value in vars(self).items():
            setattr(owner, key, value)
