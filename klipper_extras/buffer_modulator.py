# buffer_modulator.py — Extruder-Velocity-Tracker for the SpeedModulator.
#
# Sub-module of buffer_feeder; kein Klipper-load_config-Eintrypoint.

import collections
import math


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
