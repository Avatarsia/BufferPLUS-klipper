# buffer_modulator.py — Extruder-Velocity-Tracker for the SpeedModulator.
#
# Sub-module of buffer_feeder; kein Klipper-load_config-Eintrypoint.

import collections
from dataclasses import dataclass
import math


HIGH_FLOW_EXIT_HYSTERESIS_MM3S = 1.0
HIGH_FLOW_CARRY_GRACE_S = 0.75
POST_FULL_H3_DWELL_S = 0.50
POST_FULL_RECOVERY_S = 1.00
POST_FULL_RECOVERY_CHUNK_MM = 3.0

class ExtruderVelocityTracker:
    """Read-only passive tracker for extruder velocity.

    Uses extruder.find_past_position(print_time) — no flush_step_-
    generation, no SYNC, no lockstep with toolhead pipeline. Pure
    observer pattern. Output drives SpeedModulator.
    """

    def __init__(self, owner, printer, *,
                 sample_interval=0.025,
                 window_size=0.3,
                 filament_diameter=1.75):
        self.owner = owner
        self.printer = printer
        self.sample_interval = sample_interval
        self.window_size = window_size
        self._cross_section = math.pi * (filament_diameter / 2.0) ** 2
        # round() avoids float-precision truncation (e.g. 0.3 / 0.025
        # = 11.999... -> int() would yield 11 instead of expected 12).
        self._max_samples = max(2, int(round(window_size / sample_interval)))
        self._samples = collections.deque(maxlen=self._max_samples)
        self._extruder = None
        self._mcu = None
        self._last_sample_time = None

    def _get_extruder(self):
        if self._extruder is not None:
            return self._extruder
        self._extruder = self.printer.lookup_object('extruder', None)
        return self._extruder

    def _get_mcu(self):
        if self._mcu is not None:
            return self._mcu
        try:
            self._mcu = self.printer.lookup_object('mcu', None)
        except (KeyError, AttributeError):
            self._mcu = None
        return self._mcu

    def _read_position(self, ext, eventtime):
        """Liest die tatsaechliche Extruder-Position.

        Idiomatisch in Klipper (siehe filament_motion_sensor.py): via
        find_past_position(print_time) rekonstruiert aus den MCU-Steps
        — das ist die "real-time" Position, nicht die geplante
        last_position. Fallback auf last_position wenn find_past_position
        nicht vorhanden ist (z.B. PrinterDummyExtruder).
        """
        find_past = getattr(ext, 'find_past_position', None)
        if find_past is not None:
            mcu = self._get_mcu()
            if mcu is not None:
                try:
                    print_time = mcu.estimated_print_time(eventtime)
                    return float(find_past(print_time))
                except (AttributeError, TypeError, ValueError):
                    pass
            else:
                try:
                    return float(find_past(eventtime))
                except (AttributeError, TypeError, ValueError):
                    pass
        return float(getattr(ext, 'last_position', 0.0))

    def tick(self, eventtime):
        """Call from _main_tick (50Hz reactor). Throttles internally
        to sample_interval (default 25ms / 40Hz). Uses 1us tolerance
        to absorb float-accumulation drift in periodic callers."""
        if (self._last_sample_time is not None
                and (eventtime - self._last_sample_time
                     < self.sample_interval - 1e-6)):
            return
        ext = self._get_extruder()
        if ext is None:
            return
        try:
            position = self._read_position(ext, eventtime)
        except (AttributeError, TypeError):
            return
        self._samples.append((eventtime, position))
        self._last_sample_time = eventtime

    def get_velocity(self):
        """Returns linear filament velocity (mm/s, non-negative).
        0.0 if fewer than 2 samples or negative dp."""
        if len(self._samples) < 2:
            return 0.0
        (t0, p0), (t1, p1) = self._samples[0], self._samples[-1]
        dt = t1 - t0
        if dt < 1e-6:
            return 0.0
        return max(0.0, (p1 - p0) / dt)

    def get_volumetric_flow(self):
        """Returns volumetric flow (mm^3/s)."""
        return self.get_velocity() * self._cross_section

    def set_filament_diameter(self, filament_diameter):
        """Update volumetric conversion for live tuning."""
        self._cross_section = math.pi * (filament_diameter / 2.0) ** 2

    def is_ready(self):
        """True after sliding window has filled (window_size seconds
        of samples accumulated)."""
        return len(self._samples) == self._max_samples

    def reset(self):
        """Clear all samples. Call on klippy:disconnect / BUFFER_RESET."""
        self._samples.clear()
        self._last_sample_time = None


@dataclass(frozen=True)
class FeedDemandSnapshot:
    """One observation in monotonic time; no Klipper object references."""

    eventtime: float
    hall_overflow: bool
    hall_full: bool
    hall_empty: bool
    velocity_ready: bool
    extruder_velocity: float
    volumetric_flow: float


@dataclass(frozen=True)
class FeedSpeedSettings:
    """Live tuning copied by the adapter for each control decision."""

    min_feed_floor: float
    feed_hysteresis_stop_factor: float
    feed_speed: float
    hall3_demand_gain: float
    feed_speed_gain: float
    high_flow_mm3s_threshold: float


class FeedSpeedController:
    """Owns carry, hysteresis and post-full recovery without motion access.

    Demand, dwell and carry use the monotonic snapshot timestamp.
    Recovery starts at the actual clamp clear via an optional lazy clock
    argument; data-only callers may use the snapshot time instead.
    The optional debug callback emits diagnostics only.
    """

    def __init__(self, debug_event=None):
        self._debug_event = debug_event or (lambda *args, **kwargs: None)
        self._modulator_feeding = False
        self._high_flow_active_latched = False
        self._high_flow_carry_armed_until = 0.0
        self._post_full_bias_clamp = False
        self._post_full_h3_since = None
        self._post_full_recovery_until = 0.0

    def _arm_high_flow_carry(self, eventtime, reason):
        """Keep the high-flow carry session alive briefly after real
        feed demand.

        The velocity tracker uses a sliding window. After HALL3 drops,
        the volumetric estimate may cross the high-flow threshold only a
        few hundred milliseconds later. This grace window lets that
        lagging estimate continue the same feed episode, but prevents a
        cold restart later in a hall-neutral quiet zone.
        """
        until = eventtime + HIGH_FLOW_CARRY_GRACE_S
        if until > self._high_flow_carry_armed_until + 1e-6:
            self._high_flow_carry_armed_until = until
            self._debug_event(
                'high_flow_arm',
                "arm until %.3f reason=%s",
                until, reason, min_interval=0.25)

    def _disarm_high_flow_carry(self, reason):
        if self._high_flow_carry_armed_until <= 0.0:
            return
        self._high_flow_carry_armed_until = 0.0
        self._debug_event(
            'high_flow_disarm',
            "disarm reason=%s",
            reason, min_interval=0.25)

    def _is_high_flow_carry_armed(self, eventtime):
        if self._high_flow_carry_armed_until <= 0.0:
            return False
        if eventtime < self._high_flow_carry_armed_until:
            return True
        self._disarm_high_flow_carry('grace_expired')
        return False

    def _is_high_flow_active(self, flow_mm3_s, threshold):
        """Latched high-flow decision with a small exit hysteresis."""
        if threshold <= 0.0:
            active = False
        elif self._high_flow_active_latched:
            active = flow_mm3_s >= max(
                0.0, threshold - HIGH_FLOW_EXIT_HYSTERESIS_MM3S)
        else:
            active = flow_mm3_s >= threshold
        if active != self._high_flow_active_latched:
            self._debug_event(
                'high_flow_state',
                "active=%s flow=%.1f threshold=%.1f",
                active, flow_mm3_s, threshold, min_interval=0.0)
            self._high_flow_active_latched = active
        return active

    def _arm_post_full_bias_clamp(self, reason):
        """Suppress positive neutral-zone bias after HALL2.

        Once HALL2 has proven the buffer is already full, resuming the
        normal neutral-zone carry (`vel * feed_speed_gain`) can slowly
        walk the arm back up into H1 over long steady-flow stretches.
        Keep the clamp armed until a fresh real HALL3 demand occurs.
        """
        if self._post_full_bias_clamp:
            return
        self._post_full_bias_clamp = True
        self._post_full_h3_since = None
        self._post_full_recovery_until = 0.0
        self._debug_event(
            'post_full_bias_on',
            "clamp neutral bias reason=%s",
            reason, min_interval=0.25)

    def _clear_post_full_bias_clamp(self, reason, eventtime, recovery_time=None):
        if not self._post_full_bias_clamp:
            return
        self._post_full_bias_clamp = False
        self._post_full_h3_since = None
        if reason == 'hall3_demand':
            # Start the full recovery interval after preceding demand
            # reads and diagnostics, as in the original feeder helper.
            clear_time = recovery_time() if recovery_time is not None else eventtime
            self._post_full_recovery_until = (
                clear_time + POST_FULL_RECOVERY_S)
        self._debug_event(
            'post_full_bias_off',
            "release neutral bias clamp reason=%s",
            reason, min_interval=0.25)

    def _post_full_recovery_active(self, eventtime):
        if self._post_full_recovery_until <= 0.0:
            return False
        if eventtime < self._post_full_recovery_until:
            return True
        self._post_full_recovery_until = 0.0
        return False

    def effective_interrupt_chunk_mm(self, eventtime, configured_chunk):
        if (self._post_full_bias_clamp
                or self._post_full_recovery_active(eventtime)):
            return min(configured_chunk, POST_FULL_RECOVERY_CHUNK_MM)
        return configured_chunk

    def compute_target_speed(self, snapshot, settings, *, recovery_time=None):
        """SpeedModulator — bestimmt target_feed_speed in STATE_AUTO.

        Portiert die zentrale Hardware-Erkenntnis aus C-cont Hotfix 7:
        ein fixer HALL3-Refill mit voller feed_speed ueberschiesst beim
        Mellow-Buffer die sehr kleine HALL2->HALL1-Sicherheitsmarge.
        Deshalb skaliert HALL3 jetzt mit dem realen Extruder-Verbrauch
        und nutzt nur noch einen sanften MIN_FLOOR als Unterkante.

        Wurzel-C-Praevention (γ, 2026-05-14): HALL3=True hat zwei
        Bedeutungen:
          1. Aktiver Print: Extruder zieht Filament -> Arm hochgezogen
             -> echter Demand fuer Refill
          2. Idle / Pre-Print-Phase: Arm liegt natuerlich oben durch
             fehlende Zugkraft -> KEIN Demand, Buffer ist in Ruhe
        Vor Fix: beide Faelle gleich -> HALL3-only-demand triggert
        streaming-submit beim Druckstart. Daher gilt HALL3 jetzt nur
        noch mit echter Extruderbewegung als Demand.

        High-Flow-Korrektur (2026-05-15): 24 mm^3/s sind bei 1.75 mm
        Filament nur rund 10 mm/s linear. Die alte Zwischenzonen-Grenze
        `vel < min_feed_floor -> 0.0` kappte damit reale High-Flow-
        Prints vollstaendig aus dem Carry-Pfad. Oberhalb der
        volumetrischen Schwelle bleibt die Zwischenzone deshalb
        proportional aktiv statt auf den naechsten HALL3-Fall zu
        warten.

          HALL1 (overflow)  -> 0.0
          HALL2 (full)      -> 0.0
          HALL3 (empty):
            ext_vel <= 0                    -> 0.0
            0 < ext_vel < floor             -> floor
            ext_vel >= floor                -> min(max(vel * 1.5, floor),
                                                   feed_speed)
          Zwischenzone:
            tracker not_ready / vel <= 0    -> 0.0
            vel*gain < floor and flow below high_flow_mm3s_threshold
                                             -> 0.0
            sonst                           -> min(vel * feed_speed_gain,
                                                   feed_speed)
        """
        floor = settings.min_feed_floor
        floor_epsilon = 1e-6
        stop_floor = floor * settings.feed_hysteresis_stop_factor
        eventtime = snapshot.eventtime
        if snapshot.hall_overflow:
            self._modulator_feeding = False
            self._high_flow_active_latched = False
            self._disarm_high_flow_carry('hall_overflow')
            self._clear_post_full_bias_clamp('hall_overflow', eventtime)
            return 0.0
        if snapshot.hall_full:
            self._modulator_feeding = False
            self._high_flow_active_latched = False
            self._disarm_high_flow_carry('hall_full')
            self._arm_post_full_bias_clamp('hall_full')
            return 0.0
        vel_ready = snapshot.velocity_ready
        extruder_vel = snapshot.extruder_velocity if vel_ready else 0.0
        flow = snapshot.volumetric_flow if vel_ready else 0.0
        high_flow_active = self._is_high_flow_active(flow, settings.high_flow_mm3s_threshold)
        carry_armed = self._is_high_flow_carry_armed(eventtime)
        if snapshot.hall_empty:
            # Wurzel-C-Praevention γ: HALL3 ohne aktiven Extruder ist
            # kein Demand-Signal sondern Idle-Resting-Position.
            if not vel_ready or extruder_vel <= floor_epsilon:
                self._modulator_feeding = False
                return 0.0
            if self._post_full_bias_clamp:
                if self._post_full_h3_since is None:
                    self._post_full_h3_since = eventtime
                if (eventtime - self._post_full_h3_since) < POST_FULL_H3_DWELL_S:
                    self._debug_event(
                        'post_full_h3_hold',
                        "limit H3 boost after hall2 vel=%.3f dwell=%.3f/%.3f",
                        extruder_vel,
                        eventtime - self._post_full_h3_since,
                        POST_FULL_H3_DWELL_S,
                        min_interval=0.25)
                    self._modulator_feeding = True
                    return min(max(extruder_vel, floor), settings.feed_speed)
            self._clear_post_full_bias_clamp(
                'hall3_demand', eventtime, recovery_time=recovery_time)
            self._arm_high_flow_carry(eventtime, 'hall3_demand')
            if extruder_vel < floor - floor_epsilon:
                self._modulator_feeding = True
                # feed_speed-Clamp wie in allen Nachbar-Pfaden (Review
                # 2026-07-09): min_feed_floor > feed_speed ist per
                # Config moeglich (kein Cross-Check, BUFFER_SET
                # ungeprueft) — der Floor darf den globalen Speed-Cap
                # nicht ueberfahren.
                return min(floor, settings.feed_speed)
            self._modulator_feeding = True
            return min(max(extruder_vel * settings.hall3_demand_gain, floor),
                       settings.feed_speed)
        if not vel_ready or extruder_vel <= floor_epsilon:
            self._modulator_feeding = False
            return 0.0
        if self._post_full_bias_clamp:
            self._post_full_h3_since = None
            # After HALL2 a neutral-zone carry may at most match
            # current consumption. Positive bias is only allowed again
            # after a fresh HALL3 demand proves the buffer needs it.
            if extruder_vel < floor - floor_epsilon:
                self._modulator_feeding = False
                return 0.0
            self._modulator_feeding = True
            return min(extruder_vel, settings.feed_speed)
        proposed = extruder_vel * settings.feed_speed_gain
        if proposed >= floor - floor_epsilon:
            self._arm_high_flow_carry(eventtime, 'betweenzone_above_floor')
            self._modulator_feeding = True
            return min(proposed, settings.feed_speed)
        if (self._modulator_feeding and carry_armed
                and proposed >= stop_floor - floor_epsilon):
            self._debug_event(
                'high_flow_hysteresis_hold',
                "hold below-floor carry flow=%.1fmm3/s vel=%.3f "
                "target=%.3f stop_floor=%.3f",
                flow, extruder_vel, proposed, stop_floor,
                min_interval=1.0)
            return min(proposed, settings.feed_speed)
        if proposed < floor - floor_epsilon and high_flow_active and carry_armed:
            self._arm_high_flow_carry(eventtime, 'high_flow_carry')
            self._debug_event(
                'high_flow_carry',
                "allow below-floor carry flow=%.1fmm3/s vel=%.3f "
                "target=%.3f threshold=%.1f armed_left=%.3f",
                flow, extruder_vel, proposed, settings.high_flow_mm3s_threshold,
                max(0.0, self._high_flow_carry_armed_until - eventtime),
                min_interval=1.0)
            self._modulator_feeding = True
            return min(proposed, settings.feed_speed)
        if proposed < floor - floor_epsilon and high_flow_active and not carry_armed:
            self._debug_event(
                'high_flow_block_unarmed',
                "block below-floor restart flow=%.1fmm3/s vel=%.3f "
                "target=%.3f threshold=%.1f",
                flow, extruder_vel, proposed, settings.high_flow_mm3s_threshold,
                min_interval=1.0)
        self._modulator_feeding = False
        return 0.0
