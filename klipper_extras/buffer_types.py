from dataclasses import dataclass
from enum import Enum
from typing import Optional


class Hall1Context(Enum):
    SENSOR_CALLBACK = "sensor_callback"
    MAIN_TICK = "main_tick"
    SUBMIT_MOVE = "submit_move"
    AUTO_ON = "auto_on"
    PHASE3_ENTRY = "phase3_entry"

    @classmethod
    def coerce(cls, value):
        if isinstance(value, cls):
            return value
        try:
            return cls(value)
        except ValueError:
            raise ValueError("Unknown HALL1 context: %s" % (value,))


@dataclass(frozen=True)
class AnchorPlan:
    t0: Optional[float]
    skip_reason: Optional[str] = None
    rate_limit_idle_anchor: bool = False
    clamp_last_move_end_time: Optional[float] = None
    toolhead_time: Optional[float] = None
    enable_floor: Optional[float] = None


@dataclass(frozen=True)
class CleanupOptions:
    label: str
    full: bool = False
    sticky_auto_off: bool = False
    preserve_lockout: bool = False
