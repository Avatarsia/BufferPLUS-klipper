from dataclasses import dataclass
from typing import Any, Dict, Optional

from ._buffer_common import STATE_INIT


@dataclass
class BufferRuntimeState:
    _stepper_synced_to: Optional[str] = None
    _jam_active: bool = False
    _hall2_start_time: Optional[float] = None
    _hall2_start_extruder_pos: float = 0.0
    _hall3_start_time: Optional[float] = None
    _hall3_drop_since: Optional[float] = None
    _overflow_interrupted_follow: bool = False
    _overflow_resume_mm: float = 0.0
    _overflow_resume_dir: int = 0
    _overflow_resume_spd: float = 0.0
    _overflow_interrupted_state: Optional[str] = None
    _commanded_pos: float = 0.0
    _last_move_end_time: float = 0.0
    _current_move: Optional[Dict[str, Any]] = None
    _feed_distance_accumulator: float = 0.0
    _accumulated_feed_distance: float = 0.0
    _stepcompress_primed: bool = False
    _last_enable_schedule_time: float = 0.0
    _stepper_enable: Any = None
    _state: str = STATE_INIT
    _bang_bang_suspended: bool = False
    _initial_grip_end_time: Optional[float] = None
    _grip_follow_active: bool = False
    _load_phase3_distance: float = 0.0
    _load_phase3_max_distance: float = 0.0
    _load_phase3_speed: float = 0.0
    _load_phase3_stable_timeout: float = 0.0
    _load_phase3_overflow_ok: bool = False
    _load_phase3_chunk_distance: float = 10.0
    _load_phase3_hall_full_since: Optional[float] = None
    _load_phase3_hall_overflow_since: Optional[float] = None
    _load_phase3_hall_full_drop_since: Optional[float] = None
    _load_phase3_hall_overflow_drop_since: Optional[float] = None
    _pending_remaining_mm: float = 0.0
    _pending_direction: float = 0.0
    _pending_speed: float = 0.0
    _pending_submit_chunk_cap: Optional[float] = None
    _continuous_feed: bool = False
    _continuous_feed_direction: int = 0
    _continuous_feed_speed: float = 0.0
    _auto_between_since: Optional[float] = None
    _pending_disable: bool = False
    _last_idle_anchor_time: float = 0.0
    _last_mcu_flush_time: float = 0.0
    _hall1_active_since: Optional[float] = None
    _last_metrics_log_time: float = 0.0
    _modulator_feeding: bool = False
    _high_flow_active_latched: bool = False
    _high_flow_carry_armed_until: float = 0.0
    _post_full_bias_clamp: bool = False
    _needs_overflow_prime: bool = False
    _feed_deadline_time: Optional[float] = None
    _measure_load_active: bool = False
    _measure_load_distance: float = 0.0
    _measure_feeding: bool = False
    _print_running: bool = False
    _benchmark_mode_until: float = 0.0
    _benchmark_mode_reason: str = ""
    _print_phase: str = "inactive"
    _print_phase_since: float = 0.0
    _print_extrusion_seen: bool = False
    _critical_action_guard_until: float = 0.0
    _critical_action_guard_reason: str = ""
    _fault_overflow: bool = False
    _post_load_overflow_grace: bool = False
    _runout_filament_ref: Optional[float] = None
    _runout_follow_active: bool = False
    _runout_recovery_pending: bool = False
    _cooldown_deadline: Optional[float] = None
    _auto_off_by_user: bool = False
    _retract_burst_done: bool = False
    _startup_grace_seconds: float = 2.0
    _startup_grace_done: bool = False
    _entrance_was_empty: bool = False
    _macro_state_saved: bool = False
    _halt_requested: bool = False
    _main_timer: Any = None
    _jam_timer: Any = None

    def apply(self, owner):
        for key, value in vars(self).items():
            setattr(owner, key, value)
