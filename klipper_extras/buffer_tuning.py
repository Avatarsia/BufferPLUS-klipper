"""Shared scalar tuning rules; dependent changes stay explicit hooks."""

from dataclasses import dataclass


@dataclass(frozen=True)
class TuningParameter:
    gcode_key: str
    attribute: str
    config_default: object
    bounds: tuple = (("above", 0.0),)
    kind: str = "float"
    unit: str = ""
    status_key: str = None
    precision: int = 3
    audit_gap: str = " "
    dump_width: int = 24

    def read_config(self, config):
        # Config booleans accept words; runtime flags accept only 0/1.
        if self.kind == "bool":
            return config.getboolean(self.attribute, self.config_default)
        if self.kind == "string":
            return config.get(self.attribute, self.config_default).strip()
        return config.getfloat(self.attribute, self.config_default,
                               **dict(self.bounds))

    def read_runtime(self, gcmd):
        if self.kind == "string":
            return gcmd.get(self.gcode_key, None)
        parser = gcmd.get_int if self.kind == "bool" else gcmd.get_float
        return parser(self.gcode_key, None, **dict(self.bounds))

    def value_format(self):
        if self.kind == "bool":
            return "%s"
        if self.kind == "string":
            return "%r"
        return "%%.%df" % self.precision

    def suffix(self):
        return " " + self.unit if self.unit else ""

    def audit(self, old, new):
        value_format = self.value_format()
        return ("BUFFER_SET: " + self.attribute + self.audit_gap
                + value_format + " -> " + value_format + self.suffix()) % (old, new)

    def current_value(self, owner):
        return ("  " + self.attribute.ljust(self.dump_width) + "= "
                + self.value_format() + self.suffix()) % getattr(owner, self.attribute)


# Parser/no-argument display order is the historical BUFFER_SET order.
# These are config fallbacks, not the independently tuned lll.cfg profile.
TUNING_PARAMETERS = (
    TuningParameter("CHUNK_MM", "flush_callback_chunk_mm", 15.0,
                    unit="mm", status_key="flush_callback_chunk_mm", audit_gap="  "),
    TuningParameter("SPEED", "feed_speed", 30.0,
                    unit="mm/s", status_key="feed_speed", audit_gap="  "),
    TuningParameter("ACCEL", "accel", 1000.0,
                    unit="mm/s^2", status_key="accel", audit_gap="  "),
    TuningParameter("INTERRUPT_CHUNK_MM", "interrupt_chunk_mm", 9.0,
                    unit="mm", status_key="interrupt_chunk_mm", audit_gap="  "),
    TuningParameter("LEAD_TIME", "lead_time", 0.3,
                    unit="s", precision=4, audit_gap="  "),
    TuningParameter("MAX_MOVE_CHUNK_MM", "max_move_chunk_mm", 50.0,
                    unit="mm", status_key="max_move_chunk_mm", audit_gap="  "),
    TuningParameter("FEED_SPEED_GAIN", "feed_speed_gain", 1.10,
                    bounds=(("minval", 1.0),)),
    TuningParameter("HALL3_DEMAND_GAIN", "hall3_demand_gain", 1.5,
                    bounds=(("minval", 1.0),), status_key="hall3_demand_gain"),
    TuningParameter("JAM_ACTION", "jam_action", "PAUSE",
                    bounds=(), kind="string", status_key="jam_action"),
    TuningParameter("MIN_FEED_FLOOR", "min_feed_floor", 15.0, unit="mm/s"),
    TuningParameter("FILAMENT_DIAMETER", "filament_diameter", 1.75, unit="mm"),
    TuningParameter("DEBUG_EVENTS", "buffer_debug_events", False,
                    bounds=(("minval", 0), ("maxval", 1)), kind="bool",
                    status_key="buffer_debug_events", audit_gap="  "),
    TuningParameter("DEBUG_METRICS", "buffer_debug_metrics", False,
                    bounds=(("minval", 0), ("maxval", 1)), kind="bool",
                    status_key="buffer_debug_metrics"),
    TuningParameter("STRICT_START_GUARD", "strict_print_start_guard", True,
                    bounds=(("minval", 0), ("maxval", 1)), kind="bool",
                    status_key="strict_print_start_guard"),
    TuningParameter("CRITICAL_GUARD_S", "critical_action_guard_s", 0.35,
                    bounds=(("minval", 0.0),), unit="s",
                    status_key="critical_action_guard_s"),
    TuningParameter("CONSERVATIVE_MODE", "buffer_conservative_mode", False,
                    bounds=(("minval", 0), ("maxval", 1)), kind="bool",
                    status_key="buffer_conservative_mode"),
    TuningParameter("HIGH_FLOW_MM3S", "high_flow_mm3s_threshold", 24.0,
                    bounds=(("minval", 0.0),), unit="mm3/s", dump_width=25),
)
_BY_ATTRIBUTE = {parameter.attribute: parameter for parameter in TUNING_PARAMETERS}


def read_tuning_config(config, attribute):
    return _BY_ATTRIBUTE[attribute].read_config(config)


def tuning_status(owner):
    return {parameter.status_key: getattr(owner, parameter.attribute)
            for parameter in TUNING_PARAMETERS if parameter.status_key is not None}


def _cap_existing_interrupt(owner, gc):
    if owner.interrupt_chunk_mm > owner.max_move_chunk_mm:
        old = owner.interrupt_chunk_mm
        owner.interrupt_chunk_mm = owner.max_move_chunk_mm
        gc.respond_info(
            "BUFFER_SET: interrupt_chunk_mm capped %.3f -> %.3f mm "
            "(<= max_move_chunk_mm=%.3f)"
            % (old, owner.interrupt_chunk_mm, owner.max_move_chunk_mm))


def _cap_new_interrupt(owner, gc, value):
    if value > owner.max_move_chunk_mm:
        gc.respond_info(
            "BUFFER_SET: INTERRUPT_CHUNK_MM=%.3f exceeds "
            "max_move_chunk_mm=%.3f — capping"
            % (value, owner.max_move_chunk_mm))
        return owner.max_move_chunk_mm
    return value


def _normalize_jam_action(value):
    # Config only strips whitespace. NONE/DISABLED are runtime aliases.
    normalized = (value or "").strip()
    return "" if normalized.upper() in ("NONE", "DISABLED") else normalized


def _warn_lead_time(gc, value):
    if value > 1.0 or value < 0.05:
        gc.respond_info(
            "BUFFER_SET: WARNING lead_time=%.4f outside typical "
            "hardware range 0.05..1.0 s — proceed with caution" % value)


def apply_runtime_tuning(owner, gcmd):
    # Parse every argument before mutating, including arguments applied last.
    values = {parameter.attribute: parameter.read_runtime(gcmd)
              for parameter in TUNING_PARAMETERS}
    gc = owner.printer.lookup_object("gcode")
    changed = False
    # The new maximum must precede the interrupt cap, even when raising both.
    ordered = (_BY_ATTRIBUTE["max_move_chunk_mm"],
               _BY_ATTRIBUTE["interrupt_chunk_mm"]) + tuple(
                   parameter for parameter in TUNING_PARAMETERS
                   if parameter.attribute not in ("max_move_chunk_mm", "interrupt_chunk_mm"))
    for parameter in ordered:
        attribute = parameter.attribute
        value = values[attribute]
        if value is None:
            continue
        old = getattr(owner, attribute)
        if attribute == "interrupt_chunk_mm":
            value = _cap_new_interrupt(owner, gc, value)
        elif attribute == "jam_action":
            value = _normalize_jam_action(value)
        elif parameter.kind == "bool":
            value = bool(value)
        setattr(owner, attribute, value)
        if attribute == "filament_diameter":
            owner.velocity_tracker.set_filament_diameter(value)
        gc.respond_info(parameter.audit(old, value))
        if attribute == "max_move_chunk_mm":
            _cap_existing_interrupt(owner, gc)
        elif attribute == "lead_time":
            _warn_lead_time(gc, value)
        changed = True
    if not changed:
        gc.respond_info("BUFFER_SET: no args — current values:")
        for parameter in TUNING_PARAMETERS:
            gc.respond_info(parameter.current_value(owner))
