# buffer_feeder.py — Klipper extension for the Mellow LLL Plus Filament Buffer
#
# Architecture: Variante 3 (Python-Ansatz).
#
# Owns a single extruder-stepper-compatible stepper via its own trapq,
# independent of the main toolhead motion queue. Sensor-driven bang-bang
# control (HALL-based hysteresis) + explicit GCode commands for manual,
# LOAD, UNLOAD, and calibration flows.
#
# Time-base for normal feed submits is toolhead.get_last_move_time() +
# lead_time (anchored against the MCU step-gen cursor — same safeguard
# Klipper's manual_stepper uses for first-step-after-idle). The
# flush-callback bang-bang path (use_flush_callback_bang_bang=1) uses
# step_gen_time + lead_time directly from motion_queuing's flush
# notification.
# flush_step_generation() is called explicitly in three places:
#   - sync_to_extruder / unsync_if_synced (trapq-binding swaps must
#     drain pending generation before the swap),
#   - REPRIME path in _submit_single_trapezoid when the feeder has been
#     idle longer than CLOCK_DIFF_MAX (~17s) so the stepcompress cursor
#     wouldn't overflow on the next move.
# It is NOT called per-move during normal feed streaming.

import collections
import inspect
import logging
import math

import stepper

from . import _buffer_common
from ._buffer_common import (
    ANCHOR_NUDGE_MM, BUSY_PHASE_STATES, BUTTON_FEED, BUTTON_RETRACT,
    CLICK_DOUBLE, CLICK_SINGLE, CLICK_TRIPLE,
    CONTINUOUS_FEED_STATES, JAM_TICK_INTERVAL, JAM_WATCH_STATES,
    MAIN_TICK_INTERVAL, MAX_T0_LOOKAHEAD_S, REPRIME_GAP_S,
    STABLE_DROP_GRACE,
    STATE_AUTO, STATE_IDLE, STATE_INIT, STATE_INITIAL_GRIP,
    STATE_JAM, STATE_LOADING_PULL, STATE_LOADING_PUSH,
    STATE_MANUAL_FEED, STATE_MANUAL_RETRACT, STATE_OVERFLOW,
    STATE_RUNOUT, STATE_UNLOADING,
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

from .buffer_baseline_log import BaselineLogfile
from .buffer_fault import FaultManager
from .buffer_cleanup import CleanupCoordinator, CleanupPolicy
from .buffer_config import BufferConfigValues
from .buffer_tuning import apply_runtime_tuning, tuning_status
from .buffer_modulator import (
    ExtruderVelocityTracker, FeedDemandSnapshot, FeedSpeedController,
    FeedSpeedSettings, HIGH_FLOW_EXIT_HYSTERESIS_MM3S, HIGH_FLOW_CARRY_GRACE_S,
    POST_FULL_H3_DWELL_S, POST_FULL_RECOVERY_S, POST_FULL_RECOVERY_CHUNK_MM,
)
from .buffer_pending import PendingStreamLifecycle, SubmitResult
from .buffer_motor import MotorLifecycle
from .buffer_diagnostics import BufferDiagnostics
from .buffer_session import PrintSessionCoordinator
from .buffer_legacy import LegacyFilamentWorkflow
from .buffer_watchdog import IdleWatchdogCoordinator
from .buffer_streaming import StreamingCoordinator
from .buffer_timing import (
    AnchorSnapshot, ToolheadAnchorRequest, ToolheadAnchorSnapshot,
    finish_toolhead_anchor, prepare_anchor,
)
from .buffer_sensors import HallSensorMonitor, LoadEndstopMonitor
from .buffer_state import BufferRuntimeState
from .buffer_stepper import SyncCoordinator
from .buffer_toolhead import ToolheadSensorWorkflow
from .buffer_types import AnchorPlan, CleanupOptions, Hall1Context


DEFAULT_BENCHMARK_MODE_S = 900.0


class BufferFeeder:
    # Klipper-facing adapter and owner of live config/runtime attributes.
    # Dedicated helpers initialize those attributes from startup snapshots;
    # subsequent commands and events mutate the feeder, not the snapshots.
    # Fault overlays are explicitly scoped to OVERFLOW handling.
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.name = config.get_name().split()[1]   # "mellow"
        self.initial_settings = BufferConfigValues.from_config(config)
        self.settings = self.initial_settings  # Legacy startup snapshot alias.
        self.initial_settings.apply(self)
        self._load_endstop_sensor = None
        self._load_endstop_move = None
        if self.load_endstop_sensor:
            if not self.load_endstop_sensor.startswith('filament_switch_sensor '):
                raise config.error(
                    'load_endstop_sensor must name a filament_switch_sensor')
            try:
                self._load_endstop_sensor = self.printer.load_object(
                    config, self.load_endstop_sensor)
            except Exception as exc:
                raise config.error(
                    "Druckkopfsensor '%s' konnte nicht geladen werden. "
                    "Section [%s] mit switch_pin in printer.cfg oder einer "
                    "eingebundenen Datei anlegen/pruefen. Ohne Sensor: "
                    "load_endstop_sensor entfernen fuer den bisherigen "
                    "distanzbasierten Ablauf. Ursache: %s"
                    % (self.load_endstop_sensor, self.load_endstop_sensor, exc))
        self.diagnostics = BufferDiagnostics(self)
        self.print_session = PrintSessionCoordinator(self)
        self.legacy_workflow = LegacyFilamentWorkflow(self)
        self.idle_watchdog = IdleWatchdogCoordinator(self)
        self.streaming = StreamingCoordinator(self)
        self.toolhead_sensor = ToolheadSensorWorkflow(self)
        self.feed_controller = FeedSpeedController(self._debug_event)
        self.initial_runtime_state = BufferRuntimeState()
        self.runtime_state = self.initial_runtime_state  # Legacy snapshot alias.
        self.initial_runtime_state.apply(self)
        self.pending_stream = PendingStreamLifecycle(self)
        self.motor = MotorLifecycle(self)
        self._debug_event_last = {}
        baseline_log_path = config.get(
            'baseline_log_path', BaselineLogfile.DEFAULT_PATH)
        self._baseline_logfile = BaselineLogfile(baseline_log_path)

        # ----- Stepper + trapq -----
        self.sync = SyncCoordinator(self)
        self._setup_trapq(config)
        self.motion_queuing = self.sync.motion_queuing
        self.trapq = self.sync.trapq
        self.trapq_append = self.sync.trapq_append

        self.stepper = stepper.PrinterStepper(config, units_in_radians=False)
        self.stepper.setup_itersolve('cartesian_stepper_alloc', b'x')
        self.stepper.set_trapq(self.trapq)
        self.motion_queuing.check_step_generation_scan_windows()

        # ----- Sensors + buttons -----
        self.sensors = HallSensorMonitor(self, config)
        self._pin_raw_state = self.sensors._pin_raw_state
        self._pin_change_time = self.sensors._pin_change_time
        self._pin_stable_state = self.sensors._pin_stable_state
        self._pin_polarity_flip = self.sensors._pin_polarity_flip
        self._click_count = self.sensors._click_count
        self._last_click_time = self.sensors._last_click_time
        self._button_held = self.sensors._button_held
        self._pending_click_msg = self.sensors._pending_click_msg
        self._click_settle_timer = self.sensors._click_settle_timer

        self.fault = FaultManager(self)
        self.cleanup = CleanupCoordinator(self)
        self.velocity_tracker = ExtruderVelocityTracker(
            owner=self, printer=self.printer,
            sample_interval=0.025,
            window_size=0.3,
            filament_diameter=self.filament_diameter)

        # ----- Event handlers -----
        self.printer.register_event_handler('klippy:connect',  self._handle_connect)
        self.printer.register_event_handler('klippy:ready',    self._handle_ready)
        self.printer.register_event_handler('klippy:shutdown', self._handle_shutdown)

        # Flush-driven bang-bang is optional. Older motion_queuing
        # revisions may lack this callback API or the can_add_trapq
        # keyword. Keep the legacy reactor-tick path available and
        # raise a clear config error only when the user explicitly
        # enables flush-driven bang-bang on an unsupported build.
        self._register_flush_callback_if_supported(config)

        self._register_gcode_commands()

        logging.info("buffer_feeder '%s' initialised", self.name)

    @property
    def use_fault_overlay(self):
        # Backwards-compatible alias for the legacy config/status name.
        return self.use_overflow_overlay

    @use_fault_overlay.setter
    def use_fault_overlay(self, value):
        self.use_overflow_overlay = bool(value)

    def _supports_flush_callback_can_add_trapq(self, register_flush):
        """Best-effort feature-detection for newer motion_queuing
        callback signatures.

        Returns:
          True  -> signature explicitly supports can_add_trapq or **kwargs
          False -> signature is introspectable and does not support it
          None  -> signature is not introspectable; caller may probe by call
        """
        try:
            signature = inspect.signature(register_flush)
        except (TypeError, ValueError):
            return None
        if any(param.kind == inspect.Parameter.VAR_KEYWORD
               for param in signature.parameters.values()):
            return True
        return 'can_add_trapq' in signature.parameters

    def _is_missing_can_add_trapq_typeerror(self, exc):
        """True only for signature-mismatch TypeErrors caused by
        can_add_trapq on older motion_queuing implementations."""
        message = str(exc)
        return (
            ("can_add_trapq" in message and "keyword" in message)
            or "takes no keyword arguments" in message
        )

    def _debug_event(self, key, message, *args, level=logging.INFO,
                     min_interval=1.0):
        """Rate-limited handler/event tracing controlled by
        buffer_debug_events.

        Intended for incident diagnostics. Emits concise, readable
        breadcrumbs without changing motion logic and can be fully
        disabled in normal operation.
        """
        return self.diagnostics._debug_event(
            key, message, *args, level=level, min_interval=min_interval)

    @property
    def _modulator_feeding(self):
        return self.feed_controller._modulator_feeding

    @_modulator_feeding.setter
    def _modulator_feeding(self, value):
        self.feed_controller._modulator_feeding = value

    @property
    def _high_flow_active_latched(self):
        return self.feed_controller._high_flow_active_latched

    @_high_flow_active_latched.setter
    def _high_flow_active_latched(self, value):
        self.feed_controller._high_flow_active_latched = value

    @property
    def _high_flow_carry_armed_until(self):
        return self.feed_controller._high_flow_carry_armed_until

    @_high_flow_carry_armed_until.setter
    def _high_flow_carry_armed_until(self, value):
        self.feed_controller._high_flow_carry_armed_until = value

    @property
    def _post_full_bias_clamp(self):
        return self.feed_controller._post_full_bias_clamp

    @_post_full_bias_clamp.setter
    def _post_full_bias_clamp(self, value):
        self.feed_controller._post_full_bias_clamp = value

    @property
    def _post_full_h3_since(self):
        return self.feed_controller._post_full_h3_since

    @_post_full_h3_since.setter
    def _post_full_h3_since(self, value):
        self.feed_controller._post_full_h3_since = value

    @property
    def _post_full_recovery_until(self):
        return self.feed_controller._post_full_recovery_until

    @_post_full_recovery_until.setter
    def _post_full_recovery_until(self, value):
        self.feed_controller._post_full_recovery_until = value

    def _arm_high_flow_carry(self, eventtime, reason):
        return self.feed_controller._arm_high_flow_carry(eventtime, reason)

    def _disarm_high_flow_carry(self, reason):
        return self.feed_controller._disarm_high_flow_carry(reason)

    def _is_high_flow_carry_armed(self, eventtime):
        return self.feed_controller._is_high_flow_carry_armed(eventtime)

    def _arm_post_full_bias_clamp(self, reason):
        return self.feed_controller._arm_post_full_bias_clamp(reason)

    def _post_full_recovery_active(self, eventtime):
        return self.feed_controller._post_full_recovery_active(eventtime)

    def _is_high_flow_active(self, flow_mm3_s):
        return self.feed_controller._is_high_flow_active(
            flow_mm3_s, self.high_flow_mm3s_threshold)

    def _clear_post_full_bias_clamp(self, reason):
        return self.feed_controller._clear_post_full_bias_clamp(
            reason, 0.0, recovery_time=self.reactor.monotonic)

    def _effective_interrupt_chunk_mm(self, eventtime):
        return self.feed_controller.effective_interrupt_chunk_mm(
            eventtime, self.interrupt_chunk_mm)

    def _benchmark_mode_remaining(self, eventtime=None):
        if eventtime is None:
            eventtime = self.reactor.monotonic()
        return max(0.0, self._benchmark_mode_until - eventtime)

    def _benchmark_mode_active(self, eventtime=None):
        if eventtime is None:
            eventtime = self.reactor.monotonic()
        if self._benchmark_mode_until <= 0.0:
            return False
        if eventtime < self._benchmark_mode_until:
            return True
        expired_reason = self._benchmark_mode_reason or "timer_expired"
        self._benchmark_mode_until = 0.0
        self._benchmark_mode_reason = ""
        self._baseline_logfile.detach(reason="expired_was_%s" % expired_reason)
        self._debug_event(
            'bench_mode_off',
            "benchmark mode expired (was: %s)",
            expired_reason,
            min_interval=0.0)
        return False

    def _set_benchmark_mode(self, enabled, duration_s=None, reason="",
                            notify=True):
        now = self.reactor.monotonic()
        if enabled:
            if duration_s is None:
                duration_s = DEFAULT_BENCHMARK_MODE_S
            duration_s = max(float(duration_s), 0.0)
            self._benchmark_mode_until = now + duration_s
            self._benchmark_mode_reason = reason or "manual"
            self._hall2_start_time = None
            self._hall3_start_time = None
            self._hall3_drop_since = None
            self._baseline_logfile.attach(reason=self._benchmark_mode_reason)
            self._debug_event(
                'bench_mode_on',
                "benchmark mode enabled for %.1fs reason=%s",
                duration_s, self._benchmark_mode_reason,
                min_interval=0.0)
            if notify:
                self._respond(
                    "Benchmark mode enabled for %.0fs — JAM/CLOG detection suppressed"
                    % duration_s)
            return

        was_active = self._benchmark_mode_until > 0.0
        off_reason = reason or "manual"
        self._benchmark_mode_until = 0.0
        self._benchmark_mode_reason = ""
        self._hall2_start_time = None
        self._hall3_start_time = None
        self._hall3_drop_since = None
        if was_active:
            self._debug_event(
                'bench_mode_off',
                "benchmark mode disabled reason=%s",
                off_reason,
                min_interval=0.0)
            self._baseline_logfile.detach(reason=off_reason)
            if notify:
                self._respond(
                    "Benchmark mode disabled — JAM/CLOG detection restored")

    def _register_flush_callback_if_supported(self, config):
        register_flush = getattr(
            self.motion_queuing, 'register_flush_callback', None)
        if register_flush is None:
            if self.use_flush_callback_bang_bang:
                raise config.error(
                    "use_flush_callback_bang_bang requires Klipper's "
                    "motion_queuing.register_flush_callback() API. "
                    "Update to a recent mainline Klipper build.")
            logging.info(
                "buffer_feeder: motion_queuing has no register_flush_callback; "
                "flush-driven bang-bang unavailable, legacy reactor-tick path stays active")
            return
        supports_can_add_trapq = self._supports_flush_callback_can_add_trapq(
            register_flush)
        if supports_can_add_trapq is False:
            if self.use_flush_callback_bang_bang:
                raise config.error(
                    "use_flush_callback_bang_bang requires "
                    "register_flush_callback(..., can_add_trapq=True). "
                    "Update to a recent mainline Klipper build.")
            logging.info(
                "buffer_feeder: register_flush_callback lacks can_add_trapq support; "
                "flush-driven bang-bang unavailable, legacy reactor-tick path stays active")
            return
        try:
            register_flush(self._on_mcu_flush, can_add_trapq=True)
        except TypeError as exc:
            if not self._is_missing_can_add_trapq_typeerror(exc):
                raise
            if self.use_flush_callback_bang_bang:
                raise config.error(
                    "use_flush_callback_bang_bang requires "
                    "register_flush_callback(..., can_add_trapq=True). "
                    "Update to a recent mainline Klipper build.")
            logging.info(
                "buffer_feeder: register_flush_callback lacks can_add_trapq support; "
                "flush-driven bang-bang unavailable, legacy reactor-tick path stays active")

    def _register_gcode_commands(self):
        """Register all gcode commands as mux-commands on key 'BUFFER'.

        P7-40: register_command → register_mux_command. Mux-Key 'BUFFER'
        folgt der Klipper-Mainline-Konvention fuer load_config_prefix-
        Module mit Single-Type-Identifier. User-Aufruf:
          BUFFER_AUTO_ON BUFFER=mellow
        Mux-Value = self.name (z.B. "mellow" aus [buffer_feeder mellow]).
        Mehrere Instanzen koennen denselben Command-Namen registrieren,
        Dispatcher waehlt via BUFFER=...

        P7-62: Beim Single-Instance-Setup (was die uebliche Konfiguration
        ist) registriert _handle_ready zusaetzlich den BUFFER=None-
        default fuer JEDEN command, sodass der User die Befehle ohne
        BUFFER=mellow aufrufen kann (z.B. einfach "BUFFER_FEED").
        Multi-Instance-Setups behalten den Pflicht-Mux-Key.
        """
        gcode = self.printer.lookup_object('gcode')
        # (gcode_name, handler, help_text). help_text=None zieht den
        # *_help-Class-Attr; sonst inline-String wenn der Befehl keinen
        # _help hat.
        commands = [
            ('BUFFER_FEED',                 self.cmd_BUFFER_FEED,                 None),
            ('BUFFER_RETRACT',              self.cmd_BUFFER_RETRACT,              None),
            ('BUFFER_HALT',                 self.cmd_BUFFER_HALT,                 None),
            ('BUFFER_AUTO_ON',              self.cmd_BUFFER_AUTO_ON,              None),
            ('BUFFER_AUTO_ON_IF_READY',     self.cmd_BUFFER_AUTO_ON_IF_READY,     None),
            ('BUFFER_AUTO_OFF',             self.cmd_BUFFER_AUTO_OFF,             None),
            ('BUFFER_WAIT_IDLE',            self.cmd_BUFFER_WAIT_IDLE,            None),
            ('BUFFER_LOAD_PHASE1',          self.cmd_BUFFER_LOAD_PHASE1,          None),
            # BUFFER_LOAD_PHASE2 entfernt (durch SYNC_TO_EXTRUDER ersetzt)
            ('BUFFER_LOAD_PHASE3',          self.cmd_BUFFER_LOAD_PHASE3,          None),
            ('BUFFER_UNLOAD_FILAMENT',      self.cmd_BUFFER_UNLOAD_FILAMENT,      None),
            ('BUFFER_LOAD_FILAMENT',        self.cmd_BUFFER_LOAD_FILAMENT,        None),
            ('BUFFER_LOAD_SYNCED',         self.cmd_BUFFER_LOAD_SYNCED,         None),
            ('BUFFER_UNLOAD_PHASE3',        self.cmd_BUFFER_UNLOAD_PHASE3,        None),
            ('BUFFER_SYNC_TO_EXTRUDER',     self.cmd_BUFFER_SYNC_TO_EXTRUDER,     None),
            ('BUFFER_UNSYNC',               self.cmd_BUFFER_UNSYNC,               None),
            ('FORCE_BUFFER_FILL',           self.cmd_FORCE_BUFFER_FILL,           None),
            ('STOP_BUFFER_FILL',            self.cmd_STOP_BUFFER_FILL,            None),
            ('BUFFER_STATE_DUMP',           self.cmd_BUFFER_STATE_DUMP,           None),
            ('BUFFER_SET',                  self.cmd_BUFFER_SET,                  None),
            ('BUFFER_BENCHMARK_MARK',       self.cmd_BUFFER_BENCHMARK_MARK,       None),
            ('BUFFER_BENCH_MODE',           self.cmd_BUFFER_BENCH_MODE,           None),
            ('BUFFER_PREP_BASELINE',        self.cmd_BUFFER_PREP_BASELINE,        None),
            ('CALIBRATE_FEEDER_SYNC',       self.cmd_CALIBRATE_FEEDER_SYNC,       None),
            ('MEASURE_LOAD_START',          self.cmd_MEASURE_LOAD_START,          None),
            ('MEASURE_LOAD_STOP',           self.cmd_MEASURE_LOAD_STOP,           None),
            ('ENABLE_RUNOUT_SENSOR',        self.cmd_ENABLE_RUNOUT_SENSOR,
                "Set print_running=1 — enable runout PAUSE"),
            ('DISABLE_RUNOUT_SENSOR',       self.cmd_DISABLE_RUNOUT_SENSOR,
                "Set print_running=0 — disable runout PAUSE"),
            ('BUFFER_CLEAR_JAM',            self.cmd_BUFFER_CLEAR_JAM,
                "Clear JAM state after operator intervention"),
            ('BUFFER_RESTORE_STATE',        self.cmd_BUFFER_RESTORE_STATE,
                "Best-effort restore of gcode-state saved by a failed LOAD/UNLOAD"),
            ('BUFFER_SAVE_MACRO_STATE',     self.cmd_BUFFER_SAVE_MACRO_STATE,
                "Internal: mark gcode-state as saved (used by _SAVE_E_MODE)"),
            ('BUFFER_RESTORE_MACRO_STATE',  self.cmd_BUFFER_RESTORE_MACRO_STATE,
                "Internal: restore + clear gcode-state save (used by _RESTORE_E_MODE)"),
        ]
        # Save the table so _handle_ready can register a default-mux
        # fallback (BUFFER=None) when this is the only buffer_feeder
        # instance. Resolve help_text inline so the second-pass
        # registration uses identical descriptions.
        self._command_table = []
        for name, handler, help_text in commands:
            if help_text is None:
                help_text = getattr(self, 'cmd_' + name + '_help', None)
            self._command_table.append((name, handler, help_text))
            gcode.register_mux_command(name, 'BUFFER', self.name,
                                       handler, desc=help_text)

    def _register_default_mux_if_only_instance(self):
        """P7-62: When this is the only [buffer_feeder ...] section,
        register every command a SECOND time with BUFFER=None as the
        default-fallback. The user can then call commands without the
        BUFFER=mellow argument:
            BUFFER_FEED                  (no mux)
            BUFFER_FEED BUFFER=mellow    (explicit, also works)

        Multi-instance setups keep the mandatory mux-key (calling
        BUFFER_FEED without BUFFER= would be ambiguous).

        Called from _handle_ready so all instances have already
        registered their __init__ via Klipper's load_config_prefix.
        """
        instances = [obj for name, obj in self.printer.lookup_objects()
                     if name.startswith('buffer_feeder ')]
        if len(instances) != 1:
            return
        gcode = self.printer.lookup_object('gcode')
        for name, handler, help_text in self._command_table:
            try:
                gcode.register_mux_command(name, 'BUFFER', None,
                                           handler, desc=help_text)
            except Exception:
                # Already registered or other error — log + continue.
                # Not fatal: explicit BUFFER=name still works.
                logging.exception(
                    "buffer_feeder: default-mux register failed for %s",
                    name)

    # -----------------------------------------------------------------------
    # Pin registration helper
    # -----------------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------------

    def _handle_connect(self):
        # Resolve stepper enable once all MCUs are connected.
        try:
            se = self.printer.lookup_object('stepper_enable')
            self._stepper_enable = se.lookup_enable(self.stepper.get_name())
            self._install_stepper_disable_guard(se)
        except Exception:
            logging.exception("buffer_feeder: could not look up stepper_enable")

        # Track print_running via idle_timeout events (best effort).
        try:
            self.printer.register_event_handler('idle_timeout:printing',
                                                self._on_idle_printing)
            self.printer.register_event_handler('idle_timeout:ready',
                                                self._on_idle_ready)
            self.printer.register_event_handler('idle_timeout:idle',
                                                self._on_idle_ready)
        except Exception:
            logging.exception("buffer_feeder: could not register idle events")

    def _handle_ready(self):
        # Optional default-mux fallback so single-instance
        # setups can use commands without BUFFER=<name>. Done here
        # (not in __init__) because all buffer_feeder sections must
        # have completed their __init__ before we count instances.
        self._register_default_mux_if_only_instance()

        # Anchor _last_move_end_time to the toolhead's current print_time
        # rather than mcu.estimated_print_time. The two diverge after
        # long idle periods, and our submissions must live in the same
        # print-time space that stepcompress anchors against (which
        # only advances via toolhead-driven flushes).
        toolhead = self.printer.lookup_object('toolhead')
        self._last_move_end_time = toolhead.get_last_move_time() + self.lead_time

        # Stay in STATE_INIT during startup grace. Bang-bang, insert
        # handling and OVERFLOW transitions are all gated on the grace
        # period — the first 2s just passively accumulate sensor
        # callbacks so we learn the real hardware state without
        # acting on boot-time edges.

        # Start reactor timers. Main tick silently updates debounce;
        # all higher-level logic (bang-bang, phase ticks, continuous
        # feed, safety) early-exits while _startup_grace_done is False.
        self._main_timer = self.reactor.register_timer(self._main_tick,
                                                       self.reactor.NOW)
        self._jam_timer = self.reactor.register_timer(self._jam_tick,
                                                      self.reactor.NOW)
        # Schedule grace-period completion.
        self.reactor.register_callback(
            self._end_startup_grace,
            self.reactor.monotonic() + self._startup_grace_seconds)
        self._respond("BufferFeeder: ready — entering %.1fs sensor-settle grace" %
                      self._startup_grace_seconds)

    def _end_startup_grace(self, eventtime):
        self._startup_grace_done = True
        # If no filament is present at the entrance on boot, arm the
        # edge-detect flag so the first real insert triggers auto-grip
        # without requiring a pull-and-reinsert cycle. Without this,
        # _entrance_was_empty stays False (its init value) and the first
        # insert is silently ignored because no "empty" edge was ever seen.
        if not self.entrance_detected:
            self._entrance_was_empty = True
        # Log the settled sensor picture so operators can sanity-check
        # polarity against physical reality on a fresh boot.
        self._respond(
            "Startup grace done — hall_empty=%s hall_full=%s "
            "hall_overflow=%s entrance=%s"
            % (self.hall_empty, self.hall_full,
               self.hall_overflow, self.entrance_detected))
        # P7-18/19: Anchor-Step beim Boot — etabliert stepcompress
        # last_step_clock auf einen echten Wert. Hintergrund: ohne
        # ersten Step seit Klipper-Boot bleibt last_step_clock=0
        # (allocator init-state). Spaeter, wenn der erste echte Move
        # (z.B. UNLOAD Phase 2 retract oder Bang-Bang feed) >17s nach
        # Boot kommt, scheitert das Re-Prime via flush+set_position
        # zuverlaessig zurueckzusetzen → "stepcompress Invalid sequence"
        # im flush_handler. Solange wir den ersten Step beim Boot
        # machen (MCU-Clock noch klein, last_step_clock=0 → gap klein),
        # laeuft der Move ohne Crash und last_step_clock ist etabliert.
        # 0.05mm = ~250 Steps, physisch kaum spuerbar. Forward (Filament-
        # Foerderrichtung) bei normalem Boot — entspricht der User-
        # Erwartung "Buffer fettet kurz an". Bei HALL1-Boot retract,
        # weil _submit_move forward-rejects bei aktivem hall_overflow.
        try:
            self._anchor_step()
        except Exception:
            logging.exception("buffer_feeder: boot anchor failed")
        # Drop into normal operation. If HALL1 is currently active,
        # main_tick will immediately transition to OVERFLOW.
        # optional direkt zu AUTO wenn Filament da ist — manuelle
        # Mainsail-Extrusionen brauchen Bang-Bang um nicht nach ~30 mm
        # leer zu laufen. Bei aktivem Overflow oder fehlender Filament-
        # Praesenz fallen wir auf IDLE zurueck (uebliches Verhalten).
        if (self.auto_engage_on_boot
                and self.entrance_detected
                and not self._is_hall1_active(Hall1Context.AUTO_ON)):
            self._set_state(STATE_AUTO)
            self._respond("AUTO engaged on boot — filament at entrance, "
                          "buffer follows extruder demand")
        else:
            self._set_state(STATE_IDLE)

    def _handle_shutdown(self):
        # Stop timers and halt motion.
        self._set_benchmark_mode(False, reason='shutdown', notify=False)
        if self._main_timer is not None:
            try:
                self.reactor.unregister_timer(self._main_timer)
            except Exception:
                pass
            self._main_timer = None
        if self._jam_timer is not None:
            try:
                self.reactor.unregister_timer(self._jam_timer)
            except Exception:
                pass
            self._jam_timer = None
        self._continuous_feed = False
        try:
            self._disable_stepper()
        except Exception:
            logging.exception("buffer_feeder: shutdown stepper disable failed")

    def _on_idle_printing(self, *args):
        return self.print_session._on_idle_printing(*args)

    def _clear_stale_suspend_if_print_inactive(self, eventtime):
        """Lazy stale-suspend recovery (P7-56f follow-up).

        idle_timeout:ready only fires once per printing→ready transition.
        The PAUSE → CANCEL pathway is therefore stuck:
          1. PAUSE → :ready fires → _bang_bang_suspended=True
          2. CANCEL_PRINT runs → print_stats.state='cancelled', no new
             :ready event (we're already in ready)
          3. _bang_bang_suspended stays True forever
        Same trap exists for PAUSE → ERROR.

        This helper polls print_stats.state at decision points (entrance
        insert, _check_auto_ready) and clears the stale flag when the
        print is no longer paused/running. Returns True if it cleared
        something so the caller can re-evaluate any guard that depends
        on the flag."""
        return self.print_session._clear_stale_suspend_if_print_inactive(eventtime)

    def _is_active_print_state(self, eventtime=None):
        """Best-effort check whether Klipper currently reports an
        active print.

        Flush-driven auto-streaming must only run while a print is
        actually active. During Klipper idle/standby the watchdog may
        still refresh the stepcompress cursor with tiny anchor moves,
        but _on_mcu_flush must not turn those anchor refreshes into a
        self-sustaining filament stream.
        """
        return self.print_session._is_active_print_state(eventtime)

    def _get_print_stats_state(self, eventtime=None):
        return self.print_session._get_print_stats_state(eventtime)

    def _get_tracker_velocity(self):
        return self.print_session._get_tracker_velocity()

    def _arm_critical_action_guard(self, reason, duration=None, eventtime=None):
        return self.print_session._arm_critical_action_guard(reason, duration, eventtime)

    def _critical_action_guard_remaining(self, eventtime=None):
        return self.print_session._critical_action_guard_remaining(eventtime)

    def _set_print_phase(self, phase, eventtime=None, reason=None):
        return self.print_session._set_print_phase(phase, eventtime, reason)

    def _maintain_msg_latches(self, ps_state):
        """Reset der Meldungs-Dedupe-Latches (PR-#49-Review, Runde 2).

        Der Reset darf NICHT in den idle_timeout-Handlern sitzen:
        Resume und Anchor-Flap sind im Event-Moment nicht
        unterscheidbar (beide lesen print_stats.state=='paused', weil
        note_start deferred im work_handler laeuft), und waehrend des
        Drucks feuert kein idle_timeout:ready (lookahead busy /
        gcode-Mutex blocken die Printing->Ready-Transition) — der
        else-Zweig in _on_idle_ready ist fuer Pause #2+ unerreichbar.

        Aufruf-Stellen (drei Beine, vom Haeufigsten zum Garantierten):
        1. _refresh_print_phase via Flush-Callback — ABER nur in
           STATE_AUTO mit Feed-Demand (_flush_submit_streaming_chunk
           returnt bei target_speed<=0 VOR dem Permission-Check; nach
           einer Pause ist der Buffer typisch voll -> kein Demand).
        2. _refresh_print_phase via get_status — Moonraker-Poll,
           out-of-process (headless/ohne Client-Subscription: nie).
        3. _main_tick, throttled auf ~1x/s — der in-process
           GARANTIERTE Anker (Reactor-Timer laeuft immer; er feuert
           ja auch den Idle-Anchor waehrend der Pause).

        Bekanntes, irreduzibles Fenster: RESUME -> erneute PAUSE
        innerhalb < 1s ohne dazwischenliegenden Tick laesst den Latch
        stehen (Meldung der Folge-Pause unterdrueckt, selbstheilend
        beim naechsten nicht-paused Tick). Event-basiert nicht
        schliessbar, da Resume im Event-Moment nicht erkennbar ist.

        Siehe test_pause_message_dedupe.py::
        test_second_pause_announces_via_main_tick_only."""
        return self.print_session._maintain_msg_latches(ps_state)

    def _refresh_print_phase(self, eventtime=None):
        return self.print_session._refresh_print_phase(eventtime)

    def _auto_submit_permission(self, eventtime=None):
        return self.print_session._auto_submit_permission(eventtime)

    def _on_idle_ready(self, *args):
        return self.print_session._on_idle_ready(*args)

    def _maybe_park_full_on_print_end(self, eventtime, ps_state):
        """Park the buffer at HALL2 (full) once after a print ends.

        Between prints the buffer often sits in the HALL hysteresis
        dead-zone. There the watchdog fires a 0.05mm anchor move every
        idle_anchor_gap seconds (audible as a periodic tick). With
        hall_full active the watchdog anchor is hard-gated (see the
        AUTO sub-gates in _main_tick), so a one-shot fill up to HALL2
        right after print end keeps the feeder silent and lets the
        AUTO-idle-disable power the motor down.

        Stop edge: _tick_pending_chunk aborts the forward stream in
        STATE_AUTO on hall_full between sub-chunks (P7-66b). The
        streamer queues the next chunk when the current one has half
        its duration left, so the worst-case unabortable overshoot is
        ~1.5 x interrupt_chunk_mm (current remainder + one lookahead
        chunk; Codex-Verify 2026-06-11) — which also parks the arm
        solidly inside the full zone instead of on the sensor edge.
        HALL1 stays as the hard safety behind it (_submit_move
        forward-reject + persist escalation to OVERFLOW).

        The stream runs with _park_full_active=True so the AUTO demand
        modulator in _tick_pending_chunk (which reports 0 after print
        end) does not kill it after the first sub-chunk; the flag is
        cleared on every path that ends the pending stream.

        One-shot per print end via _park_full_attempted, latched
        BEFORE the submit — without the latch a fill that exhausts
        park_full_max_mm short of HALL2 would re-trigger on every
        subsequent anchor flap and creep toward overflow. Re-armed in
        _on_idle_printing when print_stats reports printing/paused."""
        return self.print_session._maybe_park_full_on_print_end(eventtime, ps_state)

    # -----------------------------------------------------------------------
    # Sensor: raw pin change + debounce
    # -----------------------------------------------------------------------

    def _check_debounce(self, eventtime):
        """Promote raw->stable after hall_debounce_ms."""
        return self.sensors.check_debounce(eventtime)

    # Convenience accessors (always up-to-date with debounced state).
    @property
    def hall_empty(self):
        return self.sensors.hall_empty

    @property
    def hall_full(self):
        return self.sensors.hall_full

    @property
    def hall_overflow(self):
        return self.sensors.hall_overflow

    @property
    def entrance_detected(self):
        return self.sensors.entrance_detected

    @property
    def feed_button_pressed(self):
        return self.sensors.feed_button_pressed

    @property
    def retract_button_pressed(self):
        return self.sensors.retract_button_pressed

    def _is_hall1_active(self, context):
        return self.fault.is_hall1_active(Hall1Context.coerce(context))

    def _on_stable_sensor_change(self, eventtime, name, raw_state):
        """Dispatch stable sensor change to the right handler."""
        return self.sensors.on_stable_sensor_change(eventtime, name, raw_state)

    # -----------------------------------------------------------------------
    # Overflow (HALL1) — hard priority
    # -----------------------------------------------------------------------

    def _mark_hall1_active(self):
        """C-cont T5 + Hotfix3: HALL1-Edge im STATE_AUTO.

        Wenn HALL2 gleichzeitig active (mechanisch eindeutig: Arm
        bereits in End-Position, kein Sensorblitzer mehr moeglich) ->
        instant _enter_overflow (kein Persist-Wait). Sonst Soft-Trigger
        via Timestamp; _main_tick prueft Persist > hall1_persist_-
        timeout fuer den echten OVERFLOW-Safety-Trigger.

        Begruendung: Hardware-Test 2026-05-13 klippy(9), 30/30 Cycles
        HALL1-Overshoot-Storm zeigte, dass HALL2+HALL1 simultan
        ausschliesslich beim Buffer-Arm-Maximalanschlag auftritt — kein
        Bouncing-Szenario. Soft-Wait waere hier nur Filament-Grind-
        Verlaengerung.

        Idempotent: bereits gesetzten Timestamp NICHT ueberschreiben,
        damit Persist-Dauer korrekt akkumuliert."""
        if self.hall_full:
            # Mechanically unambiguous: arm at maximum stop
            self._enter_overflow()
            return
        if self._hall1_active_since is None:
            self._hall1_active_since = self.reactor.monotonic()

    def _mark_hall1_cleared(self):
        """C-cont T5: HALL1 falling-edge — Timestamp loeschen, Persist-
        Counter zurueck auf None. Auch bei _exit_overflow muss diese
        Methode gerufen werden damit ein neuer HALL1-Edge sauber tracked
        wird."""
        self._hall1_active_since = None

    def _enter_overflow(self):
        return self.fault.enter_overflow()

    def _clear_recovery_flags(self):
        """Clear jam-related recovery flags reused by cleanup paths."""
        return self.fault.clear_recovery_flags()

    def _prepare_post_jam_recovery(self):
        """Conservatively refresh motion/cursor state after JAM exit.

        JAM can be cleared either explicitly (BUFFER_CLEAR_JAM) or
        implicitly via the RESUME/print-start path in _on_idle_printing.
        Both exits must leave the feeder in a state where the next
        watchdog/submit recalculates stepcompress state conservatively
        instead of trusting a potentially stale pre-JAM cursor.
        """
        mcu = self.stepper.get_mcu()
        mcu_now = mcu.estimated_print_time(self.reactor.monotonic())
        current_end = (self._current_move['end_time']
                       if self._current_move is not None
                       else 0.0)
        self._last_move_end_time = max(current_end, mcu_now)
        self._stepcompress_primed = False
        # KEIN eventtime=mcu_now: _critical_action_guard_until wird
        # ueberall gegen reactor.monotonic() verglichen — ein Arm in
        # der print_time-Domaene liefe sofort ab (Review 2026-07-09,
        # Zeitdomaenen-Bug im BUFFER_CLEAR_JAM-Pfad).
        self._arm_critical_action_guard('jam_exit')

    def _resume_after_overflow(self):
        """Restore the pre-overflow workflow if it is still resumable."""
        return self.fault.resume_after_overflow()

    def _exit_overflow(self):
        return self.fault.exit_overflow()

    # -----------------------------------------------------------------------
    # Entrance (buffer_entrance) events
    # -----------------------------------------------------------------------

    def _on_entrance_insert(self, eventtime):
        return self.sensors.on_entrance_insert(eventtime)

    def _on_entrance_runout(self, eventtime):
        return self.sensors.on_entrance_runout(eventtime)

    # -----------------------------------------------------------------------
    # Button events
    # -----------------------------------------------------------------------

    def _on_button_change(self, button_name, pressed, eventtime):
        return self.sensors.on_button_change(button_name, pressed, eventtime)

    def _ensure_click_settle_timer(self, button_name):
        return self.sensors.ensure_click_settle_timer(button_name)

    def _set_pending_click_msg(self, button_name, msg):
        return self.sensors.set_pending_click_msg(button_name, msg)

    def _click_settle_fire(self, button_name, eventtime):
        return self.sensors.click_settle_fire(button_name, eventtime)

    def _on_button_press(self, button_name, eventtime):
        return self.sensors.on_button_press(button_name, eventtime)

    def _on_button_release(self, button_name, eventtime):
        return self.sensors.on_button_release(button_name, eventtime)

    def _action_manual_start(self, button_name):
        direction = +1 if button_name == BUTTON_FEED else -1
        target_state = STATE_MANUAL_FEED if button_name == BUTTON_FEED else STATE_MANUAL_RETRACT
        self._start_continuous_motion(direction, self.manual_speed, None)
        self._set_state(target_state)
        self._set_pending_click_msg(button_name, "%s: Dauerlauf" % button_name)

    def _action_manual_pulse(self, button_name):
        direction = +1 if button_name == BUTTON_FEED else -1
        target_state = STATE_MANUAL_FEED if button_name == BUTTON_FEED else STATE_MANUAL_RETRACT
        self._set_state(target_state)
        self._submit_move(direction * self.manual_chunk_distance, self.manual_speed)
        self._schedule_return_to_auto_after_move()
        self._set_pending_click_msg(button_name,
            "%s: %d mm Puls" % (button_name, self.manual_chunk_distance))

    def _action_burst(self, button_name):
        direction = +1 if button_name == BUTTON_FEED else -1
        target_state = STATE_MANUAL_FEED if button_name == BUTTON_FEED else STATE_MANUAL_RETRACT
        self._set_state(target_state)
        self._submit_move(direction * self.burst_distance, self.burst_speed)
        if direction < 0:
            # Retract burst: operator is deliberately pulling filament back.
            # Stay IDLE afterwards — jam timer must not race against an empty
            # buffer. Operator calls BUFFER_AUTO_ON to re-engage.
            self._retract_burst_done = True
        self._schedule_return_to_auto_after_move(cooldown=self.reenable_cooldown_fast)
        self._set_pending_click_msg(button_name,
            "%s: Triple-Burst %d mm @ %d mm/s"
            % (button_name, self.burst_distance, self.burst_speed))

    # -----------------------------------------------------------------------
    # Initial grip phase
    # -----------------------------------------------------------------------

    def _estimate_sequence_duration(self, distance, speed):
        """Upper-bound wall-time for an async-streamed distance.

        Each chunk in the streamer does accel → cruise → decel to 0,
        so the true chunk time is `2*accel_time + cruise_time`, not
        just `chunk_dist/speed`. Summing across all chunks yields a
        per-move overhead of `chunks * 2 * accel_time`. Returning
        this upper bound ensures callers that set a state-deadline
        (INITIAL_GRIP, cooldown) don't flip out of the phase while
        the final chunk is still playing.
        """
        if speed <= 0 or distance <= 0:
            return 0.0
        accel_time = speed / self.accel
        chunks = int(math.ceil(distance / self.max_move_chunk_mm))
        return distance / speed + chunks * 2.0 * accel_time

    def _start_initial_grip(self, eventtime):
        self._enable_stepper()
        self._set_state(STATE_INITIAL_GRIP)
        distance = self.grip_speed * self.grip_duration
        self._respond("Initial grip: %.0f mm @ %.0f mm/s"
                      % (distance, self.grip_speed))
        # Submit first, then compute end_time from the ACTUAL queued
        # chunk's end plus the pending-stream remainder. This accounts
        # for the case where _last_move_end_time was in the future
        # from a prior aborted move (trapq can't overwrite; new move
        # starts at max(now+lead, _last_move_end_time)).
        self._submit_move(distance, self.grip_speed)
        pending_duration = 0.0
        if self._pending_remaining_mm > 0 and self._pending_speed > 0:
            pending_duration = self._estimate_sequence_duration(
                self._pending_remaining_mm, self._pending_speed)
        self._initial_grip_end_time = self._last_move_end_time + pending_duration

    # -----------------------------------------------------------------------
    # Main tick — sensor debounce + bang-bang + state progression
    # -----------------------------------------------------------------------

    def _main_tick(self, eventtime):
        """Reactor-tick dispatcher. Order matters — HALL1 lockout has
        absolute priority, then safety-timers fire jam-detection, then
        late-disable, then state-completion handlers, then submit
        helpers (bang-bang / phase3 / continuous / pending-chunks)."""
        try:
            self._tick_velocity_observation(eventtime)

            self._check_debounce(eventtime)

            # During startup grace, only sensor polling runs. No state
            # transitions, no bang-bang, no continuous feed — we wait
            # for Klipper to deliver initial sensor callbacks so we
            # learn the real hardware picture.
            if not self._startup_grace_done:
                return eventtime + MAIN_TICK_INTERVAL

            self._tick_message_latches(eventtime)

            if self._tick_hall1_priority(eventtime):
                return eventtime + MAIN_TICK_INTERVAL

            self._tick_safety_timeouts(eventtime)

            self._tick_deferred_disable()

            self._tick_idle_watchdog(eventtime)

            self._tick_cooldown_end(eventtime)
            self._tick_grip_completion(eventtime)

            self._tick_auto_feed(eventtime)

            self._tick_runout_follow(eventtime)

            # LOAD Phase 3 — feed until HALL2 or max distance.
            if self._state == STATE_LOADING_PUSH:
                self._load_phase3_tick(eventtime)

            self._tick_continuous_feed()

            self._tick_pending_chunk(eventtime)

            self._tick_debug_metrics(eventtime)

        except Exception:
            logging.exception("buffer_feeder main_tick error")

        return eventtime + MAIN_TICK_INTERVAL


    def _tick_velocity_observation(self, eventtime):
        # C-cont T2: Velocity-Tracker tick (50Hz, intern throttled
        # auf sample_interval=0.025s = 40Hz). Read-only passiver
        # Observer ueber extruder.get_status — kein flush, kein
        # SYNC, kein Side-Effect auf den Druckkopf.
        self.velocity_tracker.tick(eventtime)
        # Stempel der juengsten Extruder-Bewegung (Monotonic) —
        # sekundaere Print-Detektion fuer den Watchdog-Hard-Block
        # (Serial-/OctoPrint-Drucke, Review 2026-07-09 F5).
        _trk_ready, _trk_vel = self._get_tracker_velocity()
        if _trk_ready and _trk_vel > 1e-3:
            self._last_extruder_motion_time = eventtime

    def _tick_message_latches(self, eventtime):
        # Meldungs-Latch-Wartung, throttled auf ~1x/s (Tick laeuft
        # 50Hz; ein print_stats.get_status pro Sekunde ist billig).
        # In-process garantierter Reset-Anker — Flush-Pfad und
        # get_status-Poll sind beide konditional, siehe
        # _maintain_msg_latches-Docstring.
        if eventtime >= self._last_msg_latch_maint + 1.0:
            self._last_msg_latch_maint = eventtime
            self._maintain_msg_latches(
                self._get_print_stats_state(eventtime))

    def _tick_hall1_priority(self, eventtime):
        # C-cont T6: HALL1-Persist-Check. In STATE_AUTO loest HALL1-
        # Edge nicht mehr direkt _enter_overflow aus (siehe T5). Erst
        # wenn HALL1 laenger als hall1_persist_timeout aktiv ist,
        # eskaliere zu echtem _enter_overflow (Hardware-Safety-State).
        # In der Zwischenzeit setzt SpeedModulator (T4) bereits
        # target_speed=0, der Stepper foerdert nicht. Damit ist HALL1
        # nicht mehr ein 'instant-State-Wechsel' aber bleibt eine
        # harte Safety-Eskalation bei mechanisch stuck buffer.
        if (self._state == STATE_AUTO
                and self._hall1_active_since is not None):
            persist_duration = (
                self.reactor.monotonic() - self._hall1_active_since)
            # Kontext-Matrix als ZUSAETZLICHE Eskalations-Bedingung
            # (Hardware-Crash 2026-07-13, Codex-verifiziert): seit
            # f7059e0 wird der Timestamp auch in Bypass-Kontexten
            # armiert (Physik-Tracking). Die Eskalation selbst muss
            # synced/_post_load_overflow_grace/Overlay respektieren
            # — _enter_overflow waehrend SYNC schedult ein motor_-
            # disable in fremde in-flight Extruder-Steps ("Timer
            # too close", queue_digital_out -0.785s im MCU-Dump).
            # Timestamp bleibt armiert: nach UNSYNC (ohne Grace)
            # eskaliert derselbe Persist sofort — dann ist der
            # Stepper zurueck auf der eigenen Trapq, Disable safe.
            # Kein Early-Return im Suppress-Fall: Safety-Timeouts
            # und Deferred-Disable-Wartung des Ticks laufen weiter.
            if (persist_duration >= self.hall1_persist_timeout
                    and self._is_hall1_active(Hall1Context.MAIN_TICK)):
                if self.buffer_debug_metrics:
                    logging.info(
                        "buffer_feeder: HALL1-Persist %.2fs >= "
                        "%.2fs threshold — entering OVERFLOW state "
                        "(C-cont T6)",
                        persist_duration, self.hall1_persist_timeout)
                self._enter_overflow()
                return True
            # Persist innerhalb Timeout: kein Hard-Trigger noetig,
            # der Hard-Pfad unten greift in STATE_AUTO ohnehin nicht
            # (siehe T6-cleanup-Guard). Fall-through zum naechsten
            # Tick.

        # HALL1 has absolute priority — AUSSER bei aktivem Manual-
        # Retract oder einer UNLOAD-Phase: dann lassen wir den
        # Operator/das Macro den Buffer entlasten. Sobald die
        # Retract-Sequenz endet, greift der Reassert wieder normal.
        # OVERFLOW_OK=1 in Phase 3: _is_hall1_active kapselt
        # die caller-spezifischen Bypasses (siehe FaultManager).
        # C-cont T6 cleanup: HALL1-Hard-Trigger nur noch fuer nicht-
        # AUTO-States (LOAD/MANUAL/UNLOAD/etc.). In STATE_AUTO
        # uebernimmt der Persist-Check (Z.~2049ff.) die HALL1-
        # Behandlung mit Soft-Timer-Eskalation. Toter Code (AUTO-
        # Pfad) sichtbar entfernt.
        if (self._state != STATE_AUTO
                and self._is_hall1_active(Hall1Context.MAIN_TICK)):
            self._enter_overflow()
            return True
        return False

    def _tick_deferred_disable(self):
        # Deferred disable: motor_disable must not be called while
        # steps are unprocessed in the trapq (step-gen fires
        # motor_enable with a past time via add_active_callback →
        # Timer too close). Waehrend Sync grundsaetzlich deferren —
        # _move_in_flight() sieht nur own-trapq-Moves, nicht die
        # in-flight Extruder-Steps (Defense-in-depth, Codex
        # 2026-07-13); nach dem Unsync feuert das Pending regulaer.
        if (self._pending_disable
                and self._stepper_synced_to is None
                and not self._move_in_flight()):
            self._pending_disable = False
            self._disable_stepper()

    def _tick_idle_watchdog(self, eventtime):
        return self.idle_watchdog._tick_idle_watchdog(eventtime)

    def _log_idle_watchdog_blockers(self, eventtime):
        return self.idle_watchdog._log_idle_watchdog_blockers(eventtime)

    def _idle_watchdog_print_activity(self, eventtime):
        return self.idle_watchdog._idle_watchdog_print_activity(eventtime)

    def _submit_idle_watchdog_anchor(
            self, mcu_now, gap_moves, _p778_override, _print_state_known):
        return self.idle_watchdog._submit_idle_watchdog_anchor(
            mcu_now, gap_moves, _p778_override, _print_state_known)

    def _tick_auto_feed(self, eventtime):
        # Bang-bang nur in AUTO. (P7-16 erweiterte das auf
        # UNLOAD_PHASE_1, aber hat den Tip-Forming-Pfad
        # auf SYNC_TO_EXTRUDER umgestellt — UNLOAD_PHASE_1 wird
        # nicht mehr betreten.)
        if self._state == STATE_AUTO:
            # Post-OVERFLOW cursor resync. After OVERFLOW →
            # IDLE → AUTO the stepcompress cursor is stale.
            # when use_flush_callback_bang_bang is active,
            # the prime-anchor MUST go through _on_mcu_flush so it
            # gets a race-free step_gen_time anchor. The legacy
            # forced_t0=None path here calls flush_step_generation
            # mid-print + set_position, which rips itersolve under
            # in-flight steps if _stepcompress_primed=False (which
            # it is post-OVERFLOW because of deferred-disable).
            # Hardware-Crash 2026-04-29 (klippy.log #6: c=13,
            # gap=-0.6s) reproduced that exact path.
            if self._needs_overflow_prime:
                if not self.use_flush_callback_bang_bang:
                    self._needs_overflow_prime = False
                    self._submit_move(ANCHOR_NUDGE_MM, self.feed_speed,
                                      forced_t0=None)
                # else: leave the flag set — _on_mcu_flush picks
                # it up on the next flush-cycle and submits with
                # forced_t0=step_gen_time+lead_time.
            self._bang_bang_tick(eventtime)

    def _tick_continuous_feed(self):
        # Continuous feed: keep chunks streaming, but only in
        # states where continuous motion is the intended behavior
        # (CONTINUOUS_FEED_STATES). Otherwise stale _continuous_feed
        # would leak into LOAD_PHASE_1 single-shot moves.
        #
        # When flush_callback_bang_bang is active and we're
        # in STATE_AUTO, _on_mcu_flush owns chunk submission with
        # race-free step_gen_time anchors. Streaming a parallel
        # chunk here with forced_t0=None races against the flush-
        # callback anchor — the result is a negative gap (last_-
        # move_end_time > mcu_now) plus a stale _stepcompress_-
        # primed flag, which triggers a mid-print flush_step_-
        # generation() + set_position((0,0,0)) and rips itersolve
        # under in-flight steps → "Invalid sequence" MCU shutdown.
        # Hardware-Crash 2026-04-29 (klippy.log #5: c=6, gap=-0.6s).
        # Manual + LOAD/UNLOAD phases keep using this reactor-tick
        # streaming path because _on_mcu_flush bails on non-AUTO.
        if (self._continuous_feed
                and self._state in CONTINUOUS_FEED_STATES
                and not (self.use_flush_callback_bang_bang
                         and self._state == STATE_AUTO)
                and not self._move_in_flight()):
            chunk_dist = max(self.manual_chunk_distance,
                             self._continuous_feed_speed * 0.5)
            self._submit_move(self._continuous_feed_direction * chunk_dist,
                              self._continuous_feed_speed)

    def _tick_debug_metrics(self, eventtime):
        # C-cont T10: Diagnostik-Logs (alle 1s wenn buffer_debug_metrics).
        return self.diagnostics._tick_debug_metrics(eventtime)

    def _tick_safety_timeouts(self, eventtime):
        """Hard-safety aborts route through _trigger_jam: phase
        commands raise via WAIT_IDLE, recovery requires explicit
        BUFFER_CLEAR_JAM / BUFFER_AUTO_OFF / STOP_BUFFER_FILL."""
        if self._feed_deadline_time is not None and not self._continuous_feed:
            # Stale Deadline einer beendeten Session (Review 2026-07-09):
            # Kommando-Einstiege (PHASE1/UNLOAD_PHASE3/BUFFER_FEED
            # DISTANCE/PREP_BASELINE) setzen nur _continuous_feed=False —
            # die alte max_feed_time-Deadline wuerde sonst mitten im
            # neuen, legitimen Workflow einen falschen SAFETY_TIMEOUT-
            # JAM feuern.
            self._feed_deadline_time = None
        if (self._feed_deadline_time is not None
                and eventtime >= self._feed_deadline_time):
            self._feed_deadline_time = None
            self._trigger_jam(
                "SAFETY_TIMEOUT",
                "max_feed_time %ds reached without HALL2 — motor stall, "
                "empty spool, or value too low for setup (typical 2m "
                "bowden+buffer fill at %dmm/s needs ~90s; bump "
                "max_feed_time in lll.cfg if first-fill is legit)"
                % (int(self.max_feed_time), int(self.feed_speed)))

        # max_feed_distance is a forward-feed safety only. Manual
        # retract (Retract-Taster Dauerlauf, BUFFER_RETRACT without
        # DISTANCE) legitimately accumulates large distances in the
        # opposite direction; tripping a JAM on those is a bug.
        #
        # In STATE_AUTO with use_flush_callback_bang_bang, the
        # buffer arm can rest near the hall_empty threshold for long
        # stretches at high print flow without ever triggering hall_full.
        # The accumulator then grows past max_feed_distance and trips a
        # false JAM_SAFETY_DISTANCE while the system is operating
        # correctly (Issue #26). SUPPLY_JAM (via _jam_tick,
        # jam_supply_dwell_time) is the correct detector for genuine
        # mechanical jams in this mode. SAFETY_DISTANCE remains active
        # for LOAD/MANUAL phases and for legacy AUTO without bang-bang.
        if (self._continuous_feed
                and self._continuous_feed_direction == 1
                and not (self.use_flush_callback_bang_bang
                         and self._state == STATE_AUTO)
                and self._feed_distance_accumulator >= self.max_feed_distance):
            self._trigger_jam(
                "SAFETY_DISTANCE",
                "max_feed_distance %dmm reached without HALL2 — slipping "
                "drive gear, kinked filament, or value too low for setup "
                "(bowden+buffer path; bump max_feed_distance in lll.cfg "
                "if first-fill is legit)"
                % int(self.max_feed_distance))

    def _tick_cooldown_end(self, eventtime):
        """Cooldown end: back to AUTO if entrance present AND the
        operator hasn't explicitly disabled AUTO."""
        if self._cooldown_deadline is None or eventtime < self._cooldown_deadline:
            return
        if self._state in (STATE_MANUAL_FEED, STATE_MANUAL_RETRACT,
                           STATE_INITIAL_GRIP):
            # Guard: estimate may fire early (per-chunk gap not
            # accounted for). Only transition once the move is truly
            # done.
            if self._move_in_flight() or self._pending_remaining_mm > 0:
                self._cooldown_deadline = eventtime + 0.05
                return
            self._cooldown_deadline = None
            if (self.entrance_detected
                    and not self._is_hall1_active(Hall1Context.AUTO_ON)
                    and not self._auto_off_by_user
                    and not self._bang_bang_suspended
                    and not self._retract_burst_done):
                self._set_state(STATE_AUTO)
            else:
                self._retract_burst_done = False
                self._set_state(STATE_IDLE)
        else:
            self._cooldown_deadline = None

    def _tick_grip_completion(self, eventtime):
        """Initial grip done → follow-feed (if configured) or IDLE.
        Follow-feed done → IDLE. Both branches optionally schedule
        LOAD_FILAMENT via _maybe_auto_load."""
        if (self._state == STATE_INITIAL_GRIP
                and self._initial_grip_end_time is not None):
            mcu = self.stepper.get_mcu()
            now_pt = mcu.estimated_print_time(eventtime)
            if now_pt >= self._initial_grip_end_time:
                self._initial_grip_end_time = None
                if self._auto_off_by_user or self._bang_bang_suspended:
                    self._set_state(STATE_IDLE)
                    self._respond("Initial grip done — staying IDLE "
                                  "(AUTO off by operator or print paused)")
                elif self.grip_follow_distance > 0:
                    self._grip_follow_active = True
                    self._respond(
                        "Initial grip done — follow feed: %.0f mm @ %.0f mm/s"
                        % (self.grip_follow_distance, self.grip_follow_speed))
                    self._submit_move(self.grip_follow_distance,
                                      self.grip_follow_speed)
                    # State stays STATE_INITIAL_GRIP; pending streaming
                    # handles chunk queuing. Completion detected below.
                else:
                    self._set_state(STATE_IDLE)
                    self._respond("Initial grip done — IDLE")
                    self._maybe_auto_load()

        # Follow-feed completion: grip + follow done, drop to IDLE.
        if (self._state == STATE_INITIAL_GRIP
                and self._grip_follow_active
                and self._initial_grip_end_time is None
                and not self._move_in_flight()
                and self._pending_remaining_mm <= 0):
            self._grip_follow_active = False
            self._set_state(STATE_IDLE)
            self._respond("Grip follow done — IDLE")
            self._maybe_auto_load()

    def _tick_runout_follow(self, eventtime):
        """RUNOUT-follow (runout_pause=0 mode): bang-bang keeps
        running in AUTO; we just track extruder distance here."""
        if not (self._runout_follow_active
                and self._runout_filament_ref is not None):
            return
        try:
            ps = self.printer.lookup_object('print_stats')
            cur = ps.get_status(eventtime).get('filament_used', 0.0)
            if cur - self._runout_filament_ref >= self.runout_follow_mm:
                self._respond("Runout-follow %dmm reached — stepper off"
                              % int(self.runout_follow_mm))
                self._continuous_feed = False
                self._halt_motion()
                self._runout_filament_ref = None
                self._runout_follow_active = False
                self._set_state(STATE_IDLE)  # calls _schedule_stepper_disable
        except Exception:
            pass

    def _tick_pending_chunk(self, eventtime):
        """Pending-chunk streaming for long single-shot moves.
        Schedule the next chunk when the current one is within
        half-a-chunk-duration of ending, so chunks abut without a
        visible gap in motion. Abort signals zero out the pending
        counter — already-queued trapezoids drain on the MCU."""
        return self.streaming._tick_pending_chunk(eventtime)

    def _bang_bang_tick(self, eventtime):
        """HALL-based bang-bang with hysteresis. Reactor-tick driven —
        anchors submits via toolhead.get_last_move_time which fights
        against active toolhead-moves (lag during manual G1 E50). The
        flush-callback path (_on_mcu_flush, P7-52) is the preferred
        replacement when use_flush_callback_bang_bang is enabled."""
        if self._bang_bang_suspended:
            # Print is paused — do nothing until idle_timeout:printing.
            return
        # An explicit BUFFER_SYNC_TO_EXTRUDER (macro
        # path) has bound the stepper to the extruder trapq. Submitting
        # any move via the reactor-tick path while synced would queue
        # trapezoids on the wrong trapq AND can trip the gap>5s reprime
        # in _submit_single_trapezoid → mid-print toolhead.flush_step_-
        # generation() → extruder stop. Mirror the guard from
        # _on_mcu_flush (the legacy reactor-tick path was missing it).
        if self._stepper_synced_to is not None:
            return
        # when flush-callback bang-bang is active, the
        # reactor-tick path becomes a no-op so we don't double-submit.
        # Stop-on-HALL2 is still needed via flush-callback path itself.
        if self.use_flush_callback_bang_bang:
            return
        if self.hall_full:
            # Buffer voll: stop feeding.
            if self._continuous_feed:
                self._continuous_feed = False
                self._halt_motion()
        elif self.hall_empty:
            # Buffer leer: feed.
            if not self._continuous_feed:
                self._start_continuous_motion(+1, self.feed_speed, self.max_feed_time)
        else:
            # Zwischen-Zone: halte letzten Zustand (Hysterese).
            # Nichts tun — _continuous_feed bleibt wie es ist.
            pass

    def _compute_target_feed_speed(self):
        """Adapt current sensors, demand and live tuning to the controller."""
        eventtime = self.reactor.monotonic()
        hall_overflow, hall_full = self.hall_overflow, self.hall_full
        # Safety paths must not depend on a working velocity tracker.
        ready = (self.velocity_tracker.is_ready()
                 if not hall_overflow and not hall_full else False)
        snapshot = FeedDemandSnapshot(
            eventtime=eventtime, hall_overflow=hall_overflow,
            hall_full=hall_full, hall_empty=self.hall_empty,
            velocity_ready=ready,
            extruder_velocity=self.velocity_tracker.get_velocity() if ready else 0.0,
            volumetric_flow=self.velocity_tracker.get_volumetric_flow() if ready else 0.0)
        settings = FeedSpeedSettings(
            min_feed_floor=self.min_feed_floor,
            feed_hysteresis_stop_factor=self.feed_hysteresis_stop_factor,
            feed_speed=self.feed_speed, hall3_demand_gain=self.hall3_demand_gain,
            feed_speed_gain=self.feed_speed_gain,
            high_flow_mm3s_threshold=self.high_flow_mm3s_threshold)
        return self.feed_controller.compute_target_speed(
            snapshot, settings, recovery_time=self.reactor.monotonic)

    def _on_mcu_flush(self, flush_time, step_gen_time):
        """Flush-callback driven continuous-streaming submit.

        Klipper's motion_queuing module fires this synchronously inside
        the MCU flush cycle (klippy/extras/motion_queuing.py
        flush_handler dispatch). The caller supplies:

          flush_time     — last time steps were sent to the MCU
          step_gen_time  — last time Klipper generated steps
                           (>= flush_time)

        Anchoring submits at step_gen_time + lead_time guarantees the
        move lands in the very next flush iteration without racing
        against any toolhead-anchor or stale stepcompress cursor —
        Klipper itself dictates the anchor time, which is the
        architectural advantage over the reactor-tick path.
        """
        # Track flush-callback activity for the watchdog stale-
        # detection in _main_tick. Set BEFORE early-returns so even
        # filtered ticks (state != AUTO, suspended) keep the timestamp
        # fresh — what matters is that the LLL_PLUS MCU is generating
        # steps somewhere, not whether we decided to act on this tick.
        self._last_mcu_flush_time = flush_time
        if (getattr(self, '_stepper_disable_in_progress', False)
                or getattr(self, '_global_disable_in_progress', False)):
            return
        if not self.use_flush_callback_bang_bang:
            self._debug_event(
                'flush_skip_disabled',
                "skip use_flush_callback_bang_bang=0 flush=%.3f step_gen=%.3f",
                flush_time, step_gen_time, min_interval=5.0)
            return
        if self._bang_bang_suspended:
            self._debug_event(
                'flush_skip_suspended',
                "skip bang_bang_suspended=1 flush=%.3f step_gen=%.3f",
                flush_time, step_gen_time, min_interval=5.0)
            return
        if self._state != STATE_AUTO:
            # Macros and operator commands own non-AUTO states.
            self._debug_event(
                'flush_skip_state',
                "skip state=%s flush=%.3f step_gen=%.3f",
                self._state, flush_time, step_gen_time, min_interval=5.0)
            return
        if self._flush_should_defer_pending_itersolve(step_gen_time):
            self._debug_event(
                'flush_defer_itersolve',
                "defer step_gen=%.3f current_end=%.3f primed=%s",
                step_gen_time,
                (self._current_move['end_time']
                 if self._current_move is not None
                 else self._last_move_end_time),
                self._stepcompress_primed,
                min_interval=1.0)
            return
        if self._needs_overflow_prime:
            self._debug_event(
                'flush_overflow_prime',
                "prime via flush step_gen=%.3f lead=%.3f",
                step_gen_time, self.lead_time, min_interval=0.0)
            self._handle_overflow_prime_via_flush(step_gen_time)
            return
        if self._stepper_synced_to is not None:
            # Explicit BUFFER_SYNC_TO_EXTRUDER macro path — would queue
            # trapezoids on the wrong trapq.
            self._debug_event(
                'flush_skip_synced',
                "skip synced_to_extruder=%s",
                self._stepper_synced_to, min_interval=5.0)
            return
        if self._is_hall1_active(Hall1Context.SUBMIT_MOVE):
            # Hard-safety: never feed forward into an overfilled buffer.
            self._debug_event(
                'flush_skip_hall1',
                "skip hall1_active=1 state=%s",
                self._state, min_interval=1.0)
            return
        callback_now = self.reactor.monotonic()
        self._flush_submit_streaming_chunk(step_gen_time, callback_now)

    def _flush_should_defer_pending_itersolve(self, step_gen_time):
        """Defer flush-callback submit while itersolve still has pending
        steps from the pre-disable move.

        Scenario: a streaming chunk is in flight; HALL1 fires; the move
        is halted + scheduled for stepper-disable; _main_tick runs
        _disable_stepper (clears _stepcompress_primed) while itersolve
        still has pending steps for that chunk. If we now submit a new
        prime-move via the forced_t0 path (set_position(0)), the next
        _advance_flush_time call processes the pending pre-disable
        steps AND the new prime in the same batch → reverse step
        catch-up → "Invalid sequence" MCU shutdown.

        Anchor on `_current_move['end_time']` rather than
        `_last_move_end_time`, because _halt_motion clamps lme to
        mcu_now during mid-flight overflow but leaves _current_move
        intact.
        """
        itersolve_end = (self._current_move['end_time']
                         if self._current_move is not None
                         else self._last_move_end_time)
        return (not self._stepcompress_primed
                and itersolve_end > step_gen_time)

    def _flush_move_in_flight(self, step_gen_time):
        """Flush-callback specific in-flight detection.

        `reactor.monotonic()` can run slightly ahead of the callback's
        `step_gen_time`. In that window `_move_in_flight()` may already
        say False even though the step-generator cursor has not yet
        advanced past the previously submitted trapezoid. Treat that as
        still active so the next submit stays on the streaming path
        (no extra motor_enable, no first-chunk re-anchor).
        """
        if self._current_move is None:
            return False
        current_end = self._current_move.get('end_time', 0.0)
        if current_end <= step_gen_time:
            return False
        if self._move_in_flight():
            return True
        if current_end > step_gen_time:
            self._debug_event(
                'flush_cursor_lag',
                "treat in-flight by step_gen_time current_end=%.3f "
                "step_gen=%.3f lme=%.3f",
                current_end, step_gen_time, self._last_move_end_time,
                min_interval=1.0)
            return True
        return False

    def _handle_overflow_prime_via_flush(self, step_gen_time):
        """Post-OVERFLOW prime via the flush-callback path.

        Refreshes the stepcompress cursor with a tiny 0.05mm move
        anchored at step_gen_time + lead_time — race-free against the
        toolhead pipeline (no flush_step_generation needed).
        Follow-up bang-bang fills then ride on the synchronised
        cursor.
        """
        self._needs_overflow_prime = False
        anchor = step_gen_time + self.lead_time
        self._submit_move(ANCHOR_NUDGE_MM, self.feed_speed,
                          forced_t0=anchor)

    def _flush_submit_streaming_chunk(self, step_gen_time, eventtime=None):
        """Continuous-streaming submit driven by the SpeedModulator.

        Every flush callback computes target_speed via
        _compute_target_feed_speed from the HALL state and the
        ExtruderVelocityTracker. Submit a single sub-chunk
        (interrupt_chunk_mm) when target_speed > 0 and no
        sub-chunk pipeline is already running. Otherwise no move —
        either HALL1/HALL2 says stop, or the SpeedModulator decided
        there is no feed demand right now.
        """
        return self.streaming._flush_submit_streaming_chunk(
            step_gen_time, eventtime=eventtime)

    def _exit_phase3_stable(self, *, set_grace, respond_text):
        """Common exit-sequence when HALL2 (buffer full) or HALL1 (with
        OVERFLOW_OK=1) has been stable for the configured dwell.

        Halts streaming, clears all four Phase 3 trackers, optionally
        arms _post_load_overflow_grace (P7-46 bounce-suppression for
        HALL1-stable exit), and chooses STATE_AUTO when entrance is
        present + operator hasn't issued AUTO_OFF/HALT, else IDLE
        (P7-49: AUTO is the natural post-LOAD state — bang-bang
        re-fills on next extrusion regardless of print_running)."""
        return self.legacy_workflow._exit_phase3_stable(set_grace=set_grace, respond_text=respond_text)

    def _load_phase3_tick(self, eventtime):
        return self.legacy_workflow._load_phase3_tick(eventtime)

    # -----------------------------------------------------------------------
    # Jam detection tick
    # -----------------------------------------------------------------------

    def _jam_tick(self, eventtime):
        try:
            if not self.jam_detection_enabled or self._jam_active:
                return eventtime + JAM_TICK_INTERVAL

            if self._benchmark_mode_active(eventtime):
                self._hall2_start_time = None
                self._hall3_start_time = None
                self._hall3_drop_since = None
                return eventtime + JAM_TICK_INTERVAL

            # Jam-detection is a
            # PRINT-only safety. The CLOG detector triggers when HALL2
            # stays active while the extruder accumulates extrusion —
            # but during manual workflows (PA-tuning's
            # _CLIENT_LINEAR_MOVE E=50 F=480, manual purge, BUFFER_FEED
            # tests) HALL2 is naturally active for many seconds AND
            # the extruder is moving. That's normal behaviour, not a
            # clog. Only run jam-detection when idle_timeout signals
            # an active print.
            if not self._print_running:
                self._hall2_start_time = None
                self._hall3_start_time = None
                self._hall3_drop_since = None
                return eventtime + JAM_TICK_INTERVAL

            if self._state not in JAM_WATCH_STATES:
                # Reset trackers.
                self._hall2_start_time = None
                self._hall3_start_time = None
                self._hall3_drop_since = None
                return eventtime + JAM_TICK_INTERVAL

            # --- Jam-Typ 1: Nozzle-Clog (HALL2 stays active while extruding) ---
            if self.hall_full and not self.hall_empty:
                if self._hall2_start_time is None:
                    self._hall2_start_time = eventtime
                    self._hall2_start_extruder_pos = self._read_extruder_position()
                else:
                    dwell = eventtime - self._hall2_start_time
                    progress = self._read_extruder_position() - self._hall2_start_extruder_pos
                    if dwell >= self.jam_clog_dwell_time and progress >= self.jam_clog_extrude_min:
                        self._trigger_jam("CLOG",
                            "HALL2 active %.0fs, extruder +%.1fmm — nozzle clog suspected"
                            % (dwell, progress))
            else:
                self._hall2_start_time = None

            # --- Jam-Typ 2: Supply-Jam (HALL3 stays active while feeder running) ---
            # P7-63 stelle 7: HALL3-Drop-Grace. Without it, brief bouncing
            # flicker (30-500ms HALL3 false-edges, mechanical normal at high
            # flow) would permanently reset _hall3_start_time before
            # jam_supply_dwell_time elapses. Same STABLE_DROP_GRACE pattern
            # as _load_phase3_tick uses for HALL1/HALL2. Required because
            # SUPPLY_JAM is now the sole backstop for AUTO+bang-bang
            # (SAFETY_DISTANCE bypassed there by stelle 6).
            feeder_running_fwd = self._continuous_feed and self._continuous_feed_direction == 1
            if self.hall_empty and feeder_running_fwd:
                self._hall3_drop_since = None
                if self._hall3_start_time is None:
                    self._hall3_start_time = eventtime
                else:
                    dwell = eventtime - self._hall3_start_time
                    if dwell >= self.jam_supply_dwell_time:
                        self._trigger_jam("SUPPLY",
                            "HALL3 active %.0fs with feeder running — spool/supply jam suspected"
                            % dwell)
            else:
                if self._hall3_start_time is None:
                    self._hall3_drop_since = None
                elif self._hall3_drop_since is None:
                    self._hall3_drop_since = eventtime
                elif eventtime - self._hall3_drop_since >= STABLE_DROP_GRACE:
                    self._hall3_start_time = None
                    self._hall3_drop_since = None
        except Exception:
            logging.exception("buffer_feeder jam_tick error")

        return eventtime + JAM_TICK_INTERVAL

    def _trigger_jam(self, kind, message):
        if self._jam_active:
            return
        self._set_benchmark_mode(False, reason='jam_%s' % kind.lower(),
                                 notify=False)
        self._jam_active = True
        self._respond("*** JAM %s: %s ***" % (kind, message))
        self._continuous_feed = False
        self._halt_motion()
        self._set_state(STATE_JAM)
        if self.jam_action:
            # Defer jam_action via 1ms timer — _trigger_jam runs from
            # _jam_tick (reactor timer). Direct run_script() would block
            # the reactor for the full macro duration.
            self._schedule_gcode_script(self.jam_action)

    def _read_extruder_position(self):
        try:
            ex = self.printer.lookup_object('extruder')
            return ex.last_position
        except Exception:
            return 0.0

    # -----------------------------------------------------------------------
    # Stepper control (flush-free move submit)
    # -----------------------------------------------------------------------

    def _schedule_time_for_enable_toggle(self):
        return self.motor.schedule_toggle_time()

    def _schedule_stepper_disable(self):
        return self.motor.schedule_disable()

    def _enable_stepper(self):
        return self.motor.enable()

    def _install_stepper_disable_guard(self, stepper_enable=None):
        return self.motor.install_guard(stepper_enable)

    def _guard_global_stepper_disable(self, stepper_names, enable):
        se = self.printer.lookup_object('stepper_enable')
        return se._buffer_disable_coordinator.set_motors_enable(stepper_names, enable)

    def _guard_motor_disable(self, print_time):
        return self.motor.guard_disable(print_time)

    def _disable_stepper(self):
        return self.motor.disable()

    def _submit_move(self, signed_distance, speed, forced_t0=None,
                     streaming=False, submit_chunk_cap=None,
                     skip_enable=False, retain_skipped_distance=True):
        """Submit a move. Chunks long moves asynchronously.

        Flush-free. For distances ≤ max_move_chunk_mm this queues
        one trapezoid and returns. For longer distances it queues
        the first chunk only and records the remainder in
        _pending_remaining_mm; main_tick streams subsequent chunks
        as prior ones approach completion.

        The async streaming is what keeps HALT responsive. A
        synchronous loop would queue the whole sequence to the MCU
        at once — _last_move_end_time would land at the end of the
        full distance, and HALT could no longer prevent chunks that
        are already in the MCU step queue from playing out. By
        only ever holding ~1.5 chunks ahead in the trapq, HALT can
        zero out _pending_remaining_mm and let the in-flight chunk
        drain out — max latency one chunk duration.

        streaming (P7-66): set by _on_mcu_flush lookahead-submits when
        a move is still in-flight. Skips _enable_stepper() (motor is
        already energised from the in-flight chunk) and removes the
        _last_enable_schedule_time floor on t0. Without this, the
        enable-floor pushes the streaming-anchor forward by lead_time
        → inter-chunk gap reopens.

        submit_chunk_cap (P7-66b): caps the size of the FIRST submitted
        trapezoid for hardware-safe HALL-interrupt latency. The full
        signed_distance is honoured — anything beyond the cap is queued
        into _pending_remaining_mm and streamed by _tick_pending_chunk,
        which re-checks HALL2/HALL1 between sub-chunks. Default None
        falls back to max_move_chunk_mm (legacy behaviour).

        retain_skipped_distance: retain unqueued filament distance for
        the pending stream. Synthetic cursor anchors pass False so a
        skipped anchor keeps its existing non-retrying behaviour.
        """
        return self.streaming._submit_move(
            signed_distance, speed, forced_t0=forced_t0,
            streaming=streaming, submit_chunk_cap=submit_chunk_cap,
            skip_enable=skip_enable,
            retain_skipped_distance=retain_skipped_distance)

    def _submit_single_trapezoid(self, signed_distance, speed,
                                  forced_t0=None, streaming=False,
                                  skip_enable=False):
        """Append one trapezoid to the buffer-stepper's own trapq.

        forced_t0: when not None, overrides the t0 anchor. Used by the
        flush-callback path, which receives step_gen_time from Klipper
        and computes a race-free anchor at step_gen_time + lead_time.
        Default (None) keeps the toolhead-anchor logic for the reactor-
        tick path.

        streaming: set by lookahead-submits during a still-in-flight
        previous chunk. Suppresses _enable_stepper() (motor already on)
        and drops the _last_enable_schedule_time floor from the t0 max,
        so chunks abut without an inter-chunk lead_time gap.

        skip_enable: queue the trapezoid WITHOUT energizing the motor
        (idle-watchdog anchor under idle_motor_disable=True / Weg 2).
        The host-side last_step_clock still advances because steps are
        queued regardless of the (independent) enable GPIO; the
        de-energized TMC ignores the pulses → no movement, no enable-
        snap, no holding current. Unlike `streaming`, it does NOT touch
        the t0 anchor logic: the en-floor stays as a harmless past value
        and the reprime/set_position(0) keeps the cursor fresh as usual.

        Returns SubmitResult.QUEUED after appending a trapezoid,
        or SubmitResult.SKIPPED when a guard prevents queuing.
        Exceptions propagate without reporting queued distance.
        """
        return self.streaming._submit_single_trapezoid(
            signed_distance, speed, forced_t0=forced_t0,
            streaming=streaming, skip_enable=skip_enable)

    def _sanitize_forced_t0_floors(self, mcu_now):
        """Drop stale future floors before a forced_t0 submit.

        The flush-callback path passes an explicit anchor based on
        step_gen_time. If no move is actually in flight, a far-future
        `_last_move_end_time` or `_last_enable_schedule_time` can only
        be stale internal state. Letting those values survive into
        `_enable_stepper()` or the forced_t0 max() would override the
        safe flush anchor and re-open timer/sequence faults.
        """
        live_move = (self._current_move is not None
                     and self._current_move.get('end_time', 0.0) > mcu_now)
        if live_move:
            return

        if self._last_move_end_time > mcu_now + MAX_T0_LOOKAHEAD_S:
            logging.warning(
                "buffer_feeder: forced_t0 guard clamped stale "
                "_last_move_end_time %.2fs ahead (no in-flight move)",
                self._last_move_end_time - mcu_now)
            self._last_move_end_time = mcu_now
        if self._last_enable_schedule_time > mcu_now + MAX_T0_LOOKAHEAD_S:
            logging.warning(
                "buffer_feeder: forced_t0 guard clamped stale "
                "_last_enable_schedule_time %.2fs ahead "
                "(no in-flight move)",
                self._last_enable_schedule_time - mcu_now)
            self._last_enable_schedule_time = mcu_now

    def _reprime_stepcompress_if_needed(self, forced_t0, gap):
        """Re-prime stepcompress when the MCU step-gen cursor is stale.

        Klipper's stepcompress maintains a last_step_clock; once
        wall-clock moves more than CLOCK_DIFF_MAX (~16.7s @ 48 MHz)
        beyond it, compress_bisect_add hits a degenerate sequence and
        the MCU shuts down. Re-prime via toolhead.flush_step_generation
        + set_position(0).

        Two code-paths:
          forced_t0=None (reactor-tick): reprime on not-primed OR
            gap > REPRIME_GAP_S. Allowed to call flush_step_generation.
          forced_t0!=None (flush-callback): reprime only on not-primed;
            MUST NOT call flush_step_generation (raises ReactorError
            inside the flush callback context).

        Returns True when a reprime occurred (caller uses this as a
        signal to keep the en-floor even if was_primed=True).
        """
        if forced_t0 is None:
            need_reprime = (
                not self._stepcompress_primed or gap > REPRIME_GAP_S)
        else:
            need_reprime = not self._stepcompress_primed
        if not need_reprime:
            return False
        if forced_t0 is None:
            try:
                toolhead = self.printer.lookup_object('toolhead')
                toolhead.flush_step_generation()
                self._arm_critical_action_guard('flush_step_generation')
                logging.info(
                    "buffer_feeder: stepcompress re-primed via "
                    "flush_step_generation (gap=%.1fs)", gap)
            except Exception:
                logging.exception(
                    "buffer_feeder: flush_step_generation failed")
        self.stepper.set_position((0., 0., 0.))
        self._commanded_pos = 0.0
        self._stepcompress_primed = True
        return True

    def _plan_t0_anchor(self, forced_t0, mcu_now,
                        was_primed, need_reprime, streaming):
        snapshot = AnchorSnapshot(
            forced_t0=forced_t0, mcu_now=mcu_now, was_primed=was_primed,
            need_reprime=need_reprime, streaming=streaming,
            has_current_move=self._current_move is not None,
            current_move_end=(self._current_move.get('end_time', 0.0)
                              if self._current_move is not None else 0.0),
            last_move_end_time=self._last_move_end_time,
            last_enable_schedule_time=self._last_enable_schedule_time,
            lead_time=self.lead_time, max_t0_lookahead=MAX_T0_LOOKAHEAD_S)
        plan = prepare_anchor(snapshot)
        if isinstance(plan, ToolheadAnchorRequest):
            # Lookup may flush/reenter: keep earlier en/current-end floors,
            # but read lme, mode and lead_time afterwards as before.
            th_time = self.printer.lookup_object('toolhead').get_last_move_time()
            plan = finish_toolhead_anchor(plan, ToolheadAnchorSnapshot(
                toolhead_time=th_time,
                last_move_end_time=self._last_move_end_time,
                idle_anchor_mode=self.idle_anchor_mode, lead_time=self.lead_time))
        if plan.forced_t0_clamped:
            logging.warning(
                "buffer_feeder: forced_t0 clamped — was %.2fs "
                "ahead of mcu_now (far-future flush guard)",
                forced_t0 - mcu_now)
        if plan.skip_reason == "far_future_toolhead_anchor":
            logging.warning(
                "buffer_feeder: anchor skipped — th_time %.2fs "
                "ahead, would corrupt last_step_clock "
                "(th_time=%.3f lme=%.3f en=%.3f mcu_now=%.3f)",
                plan.skipped_t0 - mcu_now, plan.toolhead_time,
                self._last_move_end_time, plan.enable_floor, mcu_now)
        return plan

    def _compute_t0_anchor(self, forced_t0, mcu_now,
                           was_primed, need_reprime, streaming):
        """Compute the trapezoid start-time anchor and apply the
        side-effects needed for skipped far-future toolhead anchors."""
        plan = self._plan_t0_anchor(
            forced_t0, mcu_now, was_primed, need_reprime, streaming)
        if plan.t0 is not None:
            return plan.t0
        if plan.skip_reason == "far_future_toolhead_anchor":
            logging.warning(
                "buffer_feeder: anchor skipped — th_time %.2fs "
                "ahead, would corrupt last_step_clock "
                "(th_time=%.3f lme=%.3f en=%.3f mcu_now=%.3f)",
                max(0.0, plan.toolhead_time + self.lead_time - mcu_now),
                plan.toolhead_time, self._last_move_end_time,
                plan.enable_floor, mcu_now)
        if plan.rate_limit_idle_anchor:
            self._last_idle_anchor_time = mcu_now
        if plan.clamp_last_move_end_time is not None:
            self._last_move_end_time = plan.clamp_last_move_end_time
        return None

    def _append_trapezoid_and_record(self, t0, signed_distance, speed):
        """Compute the trapezoid profile, append to trapq, update state."""
        return self.streaming._append_trapezoid_and_record(t0, signed_distance, speed)

    def _move_in_flight(self):
        if self._current_move is None:
            return False
        mcu = self.stepper.get_mcu()
        now_pt = mcu.estimated_print_time(self.reactor.monotonic())
        return now_pt < self._current_move['end_time']

    def _halt_motion(self, preserve_load=False):
        """Stop the feeder at the next opportunity.

        We cannot abort a move in-flight on the trapq without a flush
        (which we refuse — that's the whole point of the architecture).
        Instead we: (a) stop submitting new chunks, (b) leave
        `_current_move` intact so `_move_in_flight` can still report
        accurately until the last submitted chunk plays out. For
        emergency stops, `_disable_stepper` is called separately to
        cut motor power on the MCU-level.

        Clears `_pending_remaining_mm` so a long async-streamed move
        stops the moment halt_motion is called. Without this clear,
        OVERFLOW / JAM would suspend streaming only as long as
        _abort_signalled() returned True — a subsequent AUTO_ON or
        HALL1-release would re-enable streaming of the leftover
        distance mid-recovery.

        Also clears `_feed_deadline_time` so a deadline that was
        armed for a since-finished continuous feed does not later
        trip SAFETY_TIMEOUT on a quiescent feeder.
        """
        if self._load_endstop_move is not None and not preserve_load:
            self._load_endstop_move.error = 'load endstop move interrupted'
        self._continuous_feed = False
        self._continuous_feed_direction = 0
        self._continuous_feed_speed = 0.0
        # Clear the Schmitt-trigger latch on every hard stop so the
        # next AUTO session re-arms from live sensor/tracker state,
        # not from a stale "was feeding" hysteresis decision.
        self._modulator_feeding = False
        self._high_flow_active_latched = False
        self._disarm_high_flow_carry('halt_motion')
        self._clear_post_full_bias_clamp('halt_motion')
        self._post_full_h3_since = None
        self._post_full_recovery_until = 0.0
        # Reset accumulator on every halt. After a halt the
        # accumulator is stale — leaving it set would cause a false
        # JAM_SAFETY_DISTANCE on the very first chunk of the next
        # session if _on_mcu_flush hasn't yet reset it (it does at
        # session start, but defense in depth covers all stop paths
        # including JAM/RUNOUT/PAUSE/CLEAR_JAM).
        self._feed_distance_accumulator = 0.0
        self._auto_between_since = None
        self.pending_stream.finish()
        self._feed_deadline_time = None
        # clamp
        # `_last_move_end_time` to mcu_now when it sits in the
        # future. _halt_motion in the AUTO-Streaming-Cycling-Pfad
        # is called mid-flight (HALL1 fires bei ~9mm gefahren in
        # einem 45mm-Chunk → _enter_overflow → _halt_motion). Pre-
        # P7-74 ließ das `_last_move_end_time` auf dem geplanten
        # Chunk-Ende stehen (mcu_now + 0.55s), obwohl der Stepper
        # tatsächlich nur 9mm gefahren ist. Der NÄCHSTE Streaming-
        # Submit (nach HALL1-Bounce-Clear, _stepcompress_primed
        # bleibt True) ankert dann via `t0 = max(forced_t0,
        # _last_move_end_time, en, mcu_now)` auf dieser Fake-Future
        # — stepcompress.last_step_clock ist aber auf dem echten
        # letzten Step (innerhalb der ersten 9mm). MCU sieht
        # last_step_clock → t0-Sprung als inkonsistent → "Invalid
        # sequence c=29" Shutdown (Eifel-Joe Hardware-Log 21:52
        # UTC, Issue #29 Kommentar).
        #
        # P7-72 stale_anchor=(_last_move_end_time <= mcu_now) fängt
        # NUR den Past-Anchor-Fall. Hier liegt der Anker in der
        # falschen ZUKUNFT, würde stale_anchor=False sagen
        # und en-Floor droppen → Fake-Future wird abuttment-Anker.
        #
        # Clamp ist einseitig: nur `> mcu_now` wird auf mcu_now
        # heruntergesetzt. Damit ist die tatsächliche Halt-Position
        # konsistent reflektiert, und stale_anchor wird beim
        # nächsten Submit True (weil `<=`) → en-Floor aktiv → safe.
        mcu = self.stepper.get_mcu()
        mcu_now = mcu.estimated_print_time(self.reactor.monotonic())
        if self._last_move_end_time > mcu_now:
            self._last_move_end_time = mcu_now

    def _start_continuous_motion(self, direction, speed, max_duration_s):
        self._continuous_feed = True
        self._continuous_feed_direction = direction
        self._continuous_feed_speed = speed
        self._feed_distance_accumulator = 0.0
        if max_duration_s is not None:
            self._feed_deadline_time = self.reactor.monotonic() + max_duration_s
        else:
            self._feed_deadline_time = None

    def _schedule_return_to_auto_after_move(self, cooldown=None):
        if cooldown is None:
            cooldown = self.reenable_cooldown
        # Account for BOTH the already-queued trapezoid and any
        # pending chunks still to be streamed. Use the sequence
        # estimator for the pending part so per-chunk accel/decel
        # overhead is included — otherwise long manual/burst moves
        # would see the cooldown fire ~1.3s before the last chunk
        # finishes (1300mm burst at 50mm/s over 26 chunks).
        delay = 0.1 + cooldown
        if self._current_move is not None:
            mcu = self.stepper.get_mcu()
            now_pt = mcu.estimated_print_time(self.reactor.monotonic())
            remaining_current = max(0.0, self._current_move['end_time'] - now_pt)
            remaining_pending = 0.0
            if self._pending_remaining_mm > 0 and self._pending_speed > 0:
                remaining_pending = self._estimate_sequence_duration(
                    self._pending_remaining_mm, self._pending_speed)
            delay = remaining_current + remaining_pending + cooldown
        self._cooldown_deadline = self.reactor.monotonic() + delay

    def _start_cooldown(self):
        self._cooldown_deadline = self.reactor.monotonic() + self.reenable_cooldown

    # -----------------------------------------------------------------------
    # State management
    # -----------------------------------------------------------------------

    def _set_state(self, new_state):
        if new_state == self._state:
            return
        old = self._state
        self._state = new_state
        logging.info("buffer_feeder: %s -> %s", old, new_state)
        # Reset jam trackers on state exit.
        if old in JAM_WATCH_STATES and new_state not in JAM_WATCH_STATES:
            self._hall2_start_time = None
            self._hall3_start_time = None
            self._hall3_drop_since = None
        # IDLE semantic per spec/README: stopped AND disabled. Enforce.
        if new_state == STATE_IDLE:
            self._halt_motion()
            self._schedule_stepper_disable()
            # ensure overlay flag is not stale across an abort
            # path that bypasses _exit_overflow (STOP_BUFFER_FILL,
            # BUFFER_HALT, BUFFER_AUTO_OFF). _exit_overflow only fires
            # on HALL1 fall-edge — direct state transitions to IDLE
            # while HALL1 is still asserted would otherwise leave
            # _fault_overflow=True, blocking later main_tick re-entry.
            self._fault_overflow = False

    # -----------------------------------------------------------------------
    # Helper: gcode interactions
    # -----------------------------------------------------------------------

    def _schedule_gcode_script(self, script):
        """Run a gcode script from a deferred one-shot reactor timer.

        gc.run_script() blocks until the script finishes. Calling it
        directly from a reactor timer callback (like _main_tick) prevents
        that timer from returning — so _main_tick never reschedules
        itself, and the pending-chunk streaming block starves. Deferring
        by 1ms lets _main_tick return first, so it fires normally on its
        20ms cadence while the gcode script runs in a separate timer.
        """
        def _cb(eventtime):
            self._gcode_run_script(script)
            return self.reactor.NEVER
        self.reactor.register_timer(_cb, self.reactor.monotonic() + 0.001)

    def _gcode_run_script(self, script, from_command=False):
        """Run a gcode script, choosing the mutex-safe variant.

        Inside a gcode command handler, we already hold the gcode
        mutex — `run_script_from_command` avoids re-acquire issues.
        Outside (reactor timer / event handler), use `run_script`
        which acquires the mutex.
        """
        try:
            gc = self.printer.lookup_object('gcode')
            if from_command:
                gc.run_script_from_command(script)
            else:
                gc.run_script(script)
        except Exception:
            logging.exception("buffer_feeder: gcode run_script failed (%s)", script)

    def _gcode_run_script_checked(self, script, from_command=False):
        """Run a gcode script and propagate failures to the caller."""
        gc = self.printer.lookup_object('gcode')
        if from_command:
            gc.run_script_from_command(script)
        else:
            gc.run_script(script)

    def _respond(self, message):
        # Log + console echo. M117 wird hier bewusst NICHT emittiert:
        # _respond wird sowohl aus reactor-event handlers als auch
        # aus gcode command handlers gerufen, und gc.run_script
        # re-acquired die gcode mutex. Aus einem command handler
        # (wo die Mutex bereits gehalten wird) wuerde der Aufruf
        # Klippers ganze gcode pipeline deadlocken.
        return self.diagnostics._respond(message)

    def _hotend_temp(self):
        try:
            ex = self.printer.lookup_object('extruder')
            return ex.get_heater().get_temp(self.reactor.monotonic())[0]
        except Exception:
            return 0.0

    def _hotend_warm(self):
        return self._hotend_temp() >= self.min_temp

    def _maybe_auto_load(self):
        """If auto_load_after_follow=1 + hotend warm, schedule
        LOAD_FILAMENT via deferred timer. Otherwise log skip-message
        with actual/min temp. Called from grip/follow completion
        and from _resume_after_overflow."""
        if not self.auto_load_after_follow:
            return
        if self._hotend_warm():
            self._schedule_gcode_script("LOAD_FILAMENT")
        else:
            self._respond(
                "Auto-Load übersprungen: Hotend zu kalt"
                " (%.0f/%.0f °C)" % (self._hotend_temp(), self.min_temp))

    def _full_reset_to_idle(self, *, label,
                            full=False,
                            sticky_auto_off=False,
                            preserve_lockout=False,
                            set_halt_requested=True):
        return self.cleanup.full_reset_to_idle(CleanupOptions(
            label=label,
            full=full,
            sticky_auto_off=sticky_auto_off,
            preserve_lockout=preserve_lockout,
            set_halt_requested=set_halt_requested,
        ))

    def _measure_report(self):
        self._respond("MEASURE_LOAD result: %.1f mm" % self._measure_load_distance)

    # -----------------------------------------------------------------------
    # GCode command implementations
    # -----------------------------------------------------------------------

    cmd_BUFFER_FEED_help = "Feed filament forward. DISTANCE=mm SPEED=mm/s TIMEOUT=s (no DISTANCE => continuous)"
    def cmd_BUFFER_FEED(self, gcmd):
        distance = gcmd.get_float('DISTANCE', 0., minval=0.)
        speed    = gcmd.get_float('SPEED',    self.manual_speed, above=0.)
        timeout  = gcmd.get_float('TIMEOUT',  self.max_feed_time, above=0.)
        self._cmd_feed_common(+1, distance, speed, timeout)

    cmd_BUFFER_RETRACT_help = "Retract filament. DISTANCE=mm SPEED=mm/s TIMEOUT=s"
    def cmd_BUFFER_RETRACT(self, gcmd):
        distance = gcmd.get_float('DISTANCE', 0., minval=0.)
        speed    = gcmd.get_float('SPEED',    self.manual_speed, above=0.)
        timeout  = gcmd.get_float('TIMEOUT',  self.max_feed_time, above=0.)
        self._cmd_feed_common(-1, distance, speed, timeout)

    def _cmd_feed_common(self, direction, distance, speed, timeout):
        if self._state in (STATE_OVERFLOW, STATE_JAM):
            raise self._cmd_error("BufferFeeder: state=%s blocks feed" % self._state)
        if self.hall_overflow:
            raise self._cmd_error("BufferFeeder: HALL1 overflow physically active — blocked")
        if self._state in BUSY_PHASE_STATES:
            raise self._cmd_error(
                "BufferFeeder: busy (state=%s) — call STOP_BUFFER_FILL "
                "or wait for LOAD/UNLOAD to finish" % self._state)
        # Fresh manual command = operator acknowledges any stale HALT
        # AND any pending runout-recovery auto-grip. Operator picked a
        # different recovery path (manual feed/retract) — RESUME should
        # not later queue a surprise grip.
        self._halt_requested = False
        self._runout_recovery_pending = False
        # Always start from a clean continuous-feed state — don't let
        # leftover bang-bang / old dauerfeed pump chunks into (or past)
        # this new command.
        self._continuous_feed = False
        target_state = STATE_MANUAL_FEED if direction > 0 else STATE_MANUAL_RETRACT
        if distance > 0:
            if distance > self.max_feed_distance:
                raise self._cmd_error("DISTANCE exceeds max_feed_distance=%.0f"
                                      % self.max_feed_distance)
            self._set_state(target_state)
            self._submit_move(direction * distance, speed)
            self._schedule_return_to_auto_after_move()
        else:
            self._set_state(target_state)
            self._start_continuous_motion(direction, speed, timeout)

    cmd_BUFFER_HALT_help = "Immediately stop any feeder motion (sticky — aborts active workflow)"
    def cmd_BUFFER_HALT(self, gcmd):
        # Halt must be sticky across AUTO / INITIAL_GRIP / LOAD_PHASE_3
        # (which would otherwise re-submit chunks from the tick loop)
        # AND across any non-locked state so an ongoing LOAD_FILAMENT /
        # UNLOAD_FILAMENT macro aborts instead of silently continuing.
        # preserve_lockout=True keeps OVERFLOW/JAM intact (safety
        # supersedes user halt), no full-reset (no recovery-flag clear,
        # no E-mode-restore — operator may want to inspect state).
        self._set_benchmark_mode(False, reason='halt', notify=False)
        self._full_reset_to_idle(label="HALT", preserve_lockout=True)
        self._respond("HALT — workflow will abort at next wait")

    def _check_auto_ready(self, allow_jam=False):
        """Pruefe Voraussetzungen fuer AUTO-Eintritt. Liefert None wenn OK,
        sonst eine User-faced Fehlermeldung. allow_jam=True wird von
        BUFFER_CLEAR_JAM genutzt, das den JAM-Lockout selbst bereits
        aufloest und nur die anderen Guards weiter abfragen will.
        """
        return self.fault.check_auto_ready(allow_jam=allow_jam)

    cmd_BUFFER_AUTO_ON_help = "Enable bang-bang auto mode"
    def cmd_BUFFER_AUTO_ON(self, gcmd):
        reason = self._check_auto_ready()
        if reason is not None:
            raise self._cmd_error("Cannot enable AUTO while " + reason)
        # Clear transient flags — user is explicitly starting fresh.
        # Also consume any pending RUNOUT-recovery: operator chose
        # to engage AUTO directly, so RESUME should not later insert
        # a grip on top.
        self._halt_requested = False
        self._auto_off_by_user = False
        self._runout_recovery_pending = False
        self._enable_stepper()
        self._set_state(STATE_AUTO)
        self._respond("AUTO engaged")

    cmd_BUFFER_AUTO_ON_IF_READY_help = ("Enable bang-bang auto mode if precondition guard "
                                         "passes. Otherwise log skip-reason and return without "
                                         "raising. Used by macros where the AUTO call follows a "
                                         "LOAD/UNLOAD that may legitimately leave HALL1 active.")
    def cmd_BUFFER_AUTO_ON_IF_READY(self, gcmd):
        # macro-render-time vs runtime fix.
        # Klipper-Jinja-macros render the whole macro body once at
        # macro-start. A `{% if bf.hall_overflow %}`-guard around
        # BUFFER_AUTO_ON evaluates the snapshot from macro-start —
        # the actual sensor reading at the time AUTO is reached can
        # be different (e.g. LOAD Phase 3 ends with HALL1 active).
        # This command does the runtime-check in Python, returning
        # quietly if the guard rejects, so the macro continues.
        reason = self._check_auto_ready()
        # Phase 3 stable-HALL1-Exit setzt _post_load_overflow_
        # grace=True, signalisiert "HALL1 active ist legitim, gerade
        # erfolgreich beendet". Akzeptiere AUTO-engage trotz HALL1
        # in genau diesem Fenster — _main_tick respektiert grace
        # separat und laesst kein _enter_overflow durch. Bei
        # HALL1-fall (Filament durch Extruder gepullt) wird grace
        # via sensor_callback geclearet, normales Regime resumed.
        if (reason is not None
                and "HALL1 overflow active" in reason
                and self._post_load_overflow_grace):
            reason = None
        if reason is not None:
            self._respond("AUTO not engaged: " + reason)
            return
        self._halt_requested = False
        self._auto_off_by_user = False
        self._runout_recovery_pending = False
        self._enable_stepper()
        self._set_state(STATE_AUTO)
        self._respond("AUTO engaged")

    cmd_BUFFER_AUTO_OFF_help = "Disable bang-bang auto mode (also clears JAM/runout-follow/pause-suspend)"
    def cmd_BUFFER_AUTO_OFF(self, gcmd):
        # Full-reset semantic: AUTO_OFF is the operator's "stop
        # everything and take control" lever. Clears recovery flags
        # AND the print-PAUSE suspension, so the user isn't stuck
        # (e.g. if the print ended uncleanly and idle_timeout never
        # fired :printing again). sticky_auto_off=True blocks
        # reinsert auto-grip until an explicit BUFFER_AUTO_ON.
        abort_workflow = 1 if gcmd is None else gcmd.get_int(
            'ABORT_WORKFLOW', 1, minval=0, maxval=1)
        self._full_reset_to_idle(label="AUTO_OFF",
                                 full=True,
                                 sticky_auto_off=True,
                                 set_halt_requested=bool(abort_workflow))
        if abort_workflow:
            self._respond("AUTO off — workflow will abort at next wait; recovery flags cleared")
        else:
            self._respond("AUTO off — state=IDLE, workflow abort suppressed")

    def _abort_signalled(self):
        """True if a wait should cut short — HALT armed or safety lockout."""
        return (self._halt_requested
                or self._state == STATE_OVERFLOW
                or self._state == STATE_JAM
                or self._jam_active
                or self.hall_overflow)

    def _wait_for_move_done(self, gcmd=None, direction=+1,
                            allow_overflow=False):
        """Internal: block until both in-flight and pending-stream
        moves are done, OR an emergency condition trips.

        Used by blocking phase commands that legitimately hold the
        busy-phase state during the wait. External callers should
        use cmd_BUFFER_WAIT_IDLE instead, which additionally waits
        for the busy-phase state to be vacated.

        Early-exits on HALT / OVERFLOW / JAM because at that point
        the motor has already been disabled (or is about to be) and
        waiting out the nominal trapq end_time is pointless.

        direction=-1 (UNLOAD/Retract): OVERFLOW/JAM blockieren nicht
        — Retract ist Recovery. Nur HALT bricht ab.

        allow_overflow=True (P7-12): forward-direction wait der den
        HALL1-Overflow-Check skipt. Genutzt von LOAD_PHASE_3 mit
        OVERFLOW_OK=1 — die Stable-Exit-Logik haendelt HALL1 selbst,
        und der Standard _raise_if_locked_out wuerde den Wait am
        Ende mit "HALL1 OVERFLOW active — aborting" raisen, bevor
        die Stable-Logik je laufen kann. JAM bleibt absolut.
        """
        while self._move_in_flight() or self._pending_remaining_mm > 0:
            if direction < 0:
                # Retract ist Recovery: OVERFLOW/JAM beenden den Wait
                # nicht (Docstring-Kontrakt oben) — nur HALT bricht ab.
                # Sonst wird jede per-Chunk-Wait in UNLOAD_PHASE3 bei
                # aktivem HALL1/JAM zum No-op und die Schleife queued
                # die volle MAX_DISTANCE ungebremst in den Trapq.
                if self._halt_requested:
                    break
            elif self._abort_signalled():
                break
            self.reactor.pause(self.reactor.monotonic() + 0.05)
        if gcmd is not None:
            if allow_overflow:
                self._raise_if_jam()
            else:
                self._raise_if_locked_out(gcmd, direction=direction)

    def _wait_for_move_done_resume_on_overflow(self, gcmd=None):
        """Like _wait_for_move_done but waits out HALL1 overflow instead of
        aborting. Used in LOAD_PHASE_1 so a HALL1 event mid-phase pauses the
        feeder and resumes automatically after HALL1 clears (_exit_overflow
        restores _pending_remaining_mm so streaming continues).
        Only hard aborts (HALT, JAM) terminate the wait early.
        """
        while (self._move_in_flight()
               or self._pending_remaining_mm > 0
               or self.hall_overflow
               or self._state == STATE_OVERFLOW):
            if self._halt_requested or self._jam_active or self._state == STATE_JAM:
                break
            self.reactor.pause(self.reactor.monotonic() + 0.1)
        if gcmd is not None:
            self._raise_if_locked_out(gcmd)

    cmd_BUFFER_WAIT_IDLE_help = ("Block until the feeder's current move is complete "
                                 "AND state has exited busy-phase (IDLE / AUTO / RUNOUT / lockout)")
    def cmd_BUFFER_WAIT_IDLE(self, gcmd):
        # Public contract (README / spec): wait for move-fertig AND
        # state=IDLE/AUTO. Also wait for pending-streamed chunks
        # to drain so the full logical move is done.
        # Early-exit on emergency conditions so abort propagates fast.
        while (self._move_in_flight()
               or self._pending_remaining_mm > 0
               or self._state in BUSY_PHASE_STATES):
            if self._abort_signalled():
                break
            self.reactor.pause(self.reactor.monotonic() + 0.05)
        self._raise_if_locked_out(gcmd)

    def _wait_for_move_drain_allowing_lockout(self):
        """Wait until current trapq/pending motion drains.

        Used by bench/prep helpers that may intentionally move while
        HALL1/HALL2 transitions are active. Unlike _wait_for_move_done,
        this helper ignores OVERFLOW/HALL1 lockout while the queued move
        is draining, but still honours an explicit HALT.
        """
        while self._move_in_flight() or self._pending_remaining_mm > 0:
            if self._halt_requested:
                break
            self.reactor.pause(self.reactor.monotonic() + 0.05)
        if self._halt_requested:
            self._halt_requested = False
            raise self._cmd_error(
                "BufferFeeder: HALT requested — aborting workflow")

    def _baseline_prep_direction(self):
        if self.hall_overflow or self.hall_full:
            return -1
        if self.hall_empty:
            return +1
        return 0

    cmd_BUFFER_PREP_BASELINE_help = (
        "Bring buffer into neutral sensor zone for bench tests. "
        "Feeds from H3 or retracts from H2/H1 in small chunks until all halls are off."
    )
    def cmd_BUFFER_PREP_BASELINE(self, gcmd):
        chunk_mm = gcmd.get_float('CHUNK_MM', 5.0, above=0.0)
        speed = gcmd.get_float('SPEED', self.manual_speed, above=0.0)
        max_distance = gcmd.get_float('MAX_DISTANCE', 200.0, above=0.0)
        settle_ms = gcmd.get_int('SETTLE_MS', 150, minval=0)

        if self._state in BUSY_PHASE_STATES:
            raise self._cmd_error(
                "BUFFER_PREP_BASELINE rejected — feeder busy "
                "(state=%s)" % self._state)
        self._raise_if_jam()

        # Stop any stale auto/manual continuation before centering.
        self._continuous_feed = False
        self.pending_stream.finish()
        moved = 0.0
        settle_s = settle_ms / 1000.0

        while moved < max_distance:
            direction = self._baseline_prep_direction()
            if direction == 0:
                self._set_state(STATE_IDLE)
                self._respond(
                    "BASELINE_PREP: neutral zone reached after %.1f mm "
                    "(H3=%s H2=%s H1=%s)"
                    % (moved, self.hall_empty, self.hall_full,
                       self.hall_overflow))
                return
            self._set_state(
                STATE_MANUAL_FEED if direction > 0 else STATE_MANUAL_RETRACT)
            # Hotfix 2026-05-18 (stepcompress c=21 race):
            # During the idle phase between cases the watchdog-anchor
            # path keeps lme = mcu_now + lead_time slightly in the
            # future (sub-MAX_T0_LOOKAHEAD_S so the sanitizer does not
            # clamp). A direct _submit_move with forced_t0=None then
            # computes t0 = th_time + lead_time which can land BEFORE
            # last_step_clock from the watchdog-anchor's queued steps,
            # producing "stepcompress o=0 i=0 c=N: Invalid sequence".
            # Clamp lme back to mcu_now before each manual sub-chunk
            # when nothing is genuinely in flight.
            #
            # NOT passing submit_chunk_cap here: the second run (c002
            # PREP) hung when the 5 mm move was split into 3+2 mm —
            # the pending-remaining stream is not consistently drained
            # in MANUAL_FEED state, and _wait_for_move_drain blocked
            # forever. Stay with a single trapezoid per chunk_mm; HALL
            # latency in PREP is uncritical (low speed, manual context).
            mcu_now = self.stepper.get_mcu().estimated_print_time(
                self.reactor.monotonic())
            if (not self._move_in_flight()
                    and self._last_move_end_time > mcu_now):
                self._last_move_end_time = mcu_now
            self._submit_move(direction * chunk_mm, speed)
            self._wait_for_move_drain_allowing_lockout()
            moved += chunk_mm
            if settle_s > 0:
                self.reactor.pause(self.reactor.monotonic() + settle_s)

        self._set_state(STATE_IDLE)
        raise self._cmd_error(
            "BUFFER_PREP_BASELINE failed — no neutral zone after %.1f mm "
            "(H3=%s H2=%s H1=%s)"
            % (moved, self.hall_empty, self.hall_full, self.hall_overflow))

    cmd_BUFFER_BENCHMARK_MARK_help = (
        "Write a stable benchmark marker into klippy.log. "
        "EVENT={SUITE_START|SUITE_END|CASE_START|CASE_END|MEASURE_START|MEASURE_END}"
    )
    def cmd_BUFFER_BENCHMARK_MARK(self, gcmd):
        event = (gcmd.get('EVENT', '') or '').upper()
        token_map = {
            'SUITE_START': 'BFX_SUITE_START',
            'SUITE_END': 'BFX_SUITE_END',
            'CASE_START': 'BFX_CASE_START',
            'CASE_END': 'BFX_CASE_END',
            'MEASURE_START': 'BFX_MEASURE_START',
            'MEASURE_END': 'BFX_MEASURE_END',
        }
        token = token_map.get(event)
        if token is None:
            raise self._cmd_error(
                "BUFFER_BENCHMARK_MARK: invalid EVENT=%r" % event)

        parts = [token]

        def append_field(param_name, key_name):
            value = gcmd.get(param_name, None)
            if value is not None:
                parts.append("%s=%s" % (key_name, value))

        append_field('CASE_ID', 'id')
        append_field('FLOW', 'flow')
        append_field('DURATION', 'duration')
        append_field('SPEED', 'speed')
        append_field('GAIN', 'gain')
        append_field('FLOOR', 'floor')
        append_field('HIGHFLOW', 'highflow')
        append_field('CASES', 'cases')

        rendered = " ".join(parts)
        logging.info("buffer_benchmark: %s", rendered)
        # SUITE_START/CASE_START fire BEFORE BUFFER_BENCH_MODE enables
        # the FileHandler, and SUITE_END/CASE_END fire AFTER it
        # disables. The handler-filtered tail of these markers would
        # never land in the baseline logfile. Write them directly so
        # the analyzer sees the complete suite envelope.
        if not self._baseline_logfile.is_attached():
            self._baseline_logfile.write_one("buffer_benchmark: " + rendered)

    cmd_BUFFER_BENCH_MODE_help = (
        "Enable/disable benchmark mode with auto-expiry. "
        "Suppresses JAM/CLOG detection for bench runs."
    )
    def cmd_BUFFER_BENCH_MODE(self, gcmd):
        enable = bool(gcmd.get_int('ENABLE', 1, minval=0, maxval=1))
        duration_s = None
        if enable:
            duration_s = gcmd.get_float(
                'DURATION', DEFAULT_BENCHMARK_MODE_S, above=0.0)
        reason = gcmd.get('REASON', None)
        self._set_benchmark_mode(
            enabled=enable,
            duration_s=duration_s,
            reason=reason or ('gcode_enable' if enable else 'gcode_disable'),
            notify=True)

    def _check_phase_entry(self, cmd_name, allowed_states):
        """Reject a phase command if the current state isn't in the
        allow-list. Callers pass exactly the states from which a legit
        progression (or idempotent re-entry) is permitted — e.g. each
        phase command accepts its own STATE_* for retry idempotence,
        plus IDLE/AUTO/RUNOUT for the normal entry path. UNLOAD-phase
        commands also accept OVERFLOW/JAM because UNLOAD is the
        recovery operation for those lockouts.
        """
        if self._state in allowed_states:
            return
        raise self._cmd_error(
            "%s rejected — wrong state (state=%s, expected one of %s). "
            "Use BUFFER_HALT or BUFFER_CLEAR_JAM/BUFFER_AUTO_OFF to clear, "
            "or BUFFER_STATE_DUMP to inspect."
            % (cmd_name, self._state, sorted(allowed_states)))

    def cmd_BUFFER_LOAD_FILAMENT(self, gcmd):
        self.toolhead_sensor.load(gcmd)

    cmd_BUFFER_LOAD_PHASE1_help = "LOAD Phase 1 — feeder alone fast to toolhead. DISTANCE=mm"
    def cmd_BUFFER_LOAD_PHASE1(self, gcmd):
        return self.legacy_workflow.cmd_BUFFER_LOAD_PHASE1(gcmd)

    def _load_endstop_present(self):
        return self.legacy_workflow._load_endstop_present()

    def _load_to_endstop(self, gcmd, distance, speed, minimum_ratio=0.9,
                         overflow_timeout=0.):
        return self.legacy_workflow._load_to_endstop(gcmd, distance, speed, minimum_ratio, overflow_timeout)

    # cmd_BUFFER_LOAD_PHASE2 entfernt. Das parallele Feeder+
    # Extruder-Pattern wurde durch SYNC_TO_EXTRUDER abgeloest (P7-44 in
    # LOAD_FILAMENT Phase 3/3, in UNLOAD-Tip-Forming). Der alte
    # Befehl war seit dem nicht mehr in lll.cfg / tests / Macros
    # aufgerufen, nur als Public-G-Code-Endpoint registriert. Externe
    # Custom-Macros, die `BUFFER_LOAD_PHASE2` direkt aufgerufen haben,
    # muessen auf `BUFFER_SYNC_TO_EXTRUDER` + `G1 E` + `BUFFER_UNSYNC`
    # umgestellt werden.

    cmd_BUFFER_LOAD_PHASE3_help = ("LOAD Phase 3 — feed until HALL2 or MAX_DISTANCE. "
                                    "Optional: STABLE_TIMEOUT (s, 0=instant), "
                                    "OVERFLOW_OK (0/1, treat stable HALL1 as success), "
                                    "CHUNK_DISTANCE (mm per tick chunk).")
    def cmd_BUFFER_LOAD_PHASE3(self, gcmd):
        return self.legacy_workflow.cmd_BUFFER_LOAD_PHASE3(gcmd)

    # cmd_BUFFER_UNLOAD_PHASE1 und cmd_BUFFER_UNLOAD_PHASE2 entfernt.
    # P7-20 hat das UNLOAD_FILAMENT-Macro auf SYNC_TO_EXTRUDER umgestellt —
    # Tip-Forming und parallele sync-distance laufen jetzt im Macro selbst
    # via G1 E waehrend BUFFER_SYNC_TO_EXTRUDER aktiv ist.

    cmd_BUFFER_LOAD_SYNCED_help = (
        "Final LOAD push with guaranteed UNSYNC/RESTORE. "
        "DISTANCE=mm SPEED=mm/s EXTRUDER=name")
    def cmd_BUFFER_LOAD_SYNCED(self, gcmd):
        if not self._startup_grace_done:
            raise self._cmd_error("BufferFeeder: startup grace not finished")
        self._check_phase_entry('LOAD_SYNCED', {
            STATE_IDLE, STATE_AUTO, STATE_RUNOUT, STATE_OVERFLOW,
        })
        self._raise_if_jam()
        if self._halt_requested:
            raise self._cmd_error("BufferFeeder: HALT requested")
        if self._stepper_synced_to is not None:
            raise self._cmd_error("Buffer already synced; call BUFFER_UNSYNC first")
        extruder = gcmd.get('EXTRUDER', 'extruder')
        active = self.printer.lookup_object('toolhead').get_extruder().get_name()
        if extruder != active:
            raise self._cmd_error(
                "EXTRUDER=%s differs from active extruder %s" % (extruder, active))
        distance = gcmd.get_float('DISTANCE', self.load_slow_distance, above=0.)
        speed = gcmd.get_float('SPEED', self.load_slow_speed, above=0.)
        with self.cleanup.gcode_state_scope(
                policy=CleanupPolicy.STRICT, adopt_existing=True):
            with self.cleanup.sync_scope(
                    lambda: self._gcode_run_script_checked(
                        "BUFFER_SYNC_TO_EXTRUDER BUFFER=%s EXTRUDER=%s"
                        % (self.name, extruder), from_command=True),
                    policy=CleanupPolicy.STRICT):
                self._gcode_run_script_checked(
                    "G1 E%g F%d\nM400" % (distance, int(speed * 60)),
                    from_command=True)

    cmd_BUFFER_UNLOAD_FILAMENT_help = "UNLOAD_FILAMENT als Python-Workflow mit garantiertem Cleanup"
    def cmd_BUFFER_UNLOAD_FILAMENT(self, gcmd):
        return self.legacy_workflow.cmd_BUFFER_UNLOAD_FILAMENT(gcmd)

    cmd_BUFFER_UNLOAD_PHASE3_help = "UNLOAD Phase 3 — chunked retract until entrance free"
    def cmd_BUFFER_UNLOAD_PHASE3(self, gcmd):
        return self.legacy_workflow.cmd_BUFFER_UNLOAD_PHASE3(gcmd)

    def _setup_trapq(self, config):
        return self.sync.setup_trapq(config)

    def _anchor_step(self):
        return self.sync.anchor_step()

    def _sync_to_extruder(self, extruder_name):
        return self.sync.sync_to_extruder(extruder_name)

    cmd_BUFFER_SYNC_TO_EXTRUDER_help = ("Sync buffer-feeder stepper to the named "
                                         "extruder's trapq for parallel motion. "
                                         "Optional: EXTRUDER=<name> (default: extruder)")
    def cmd_BUFFER_SYNC_TO_EXTRUDER(self, gcmd):
        # bindet den Buffer-Feeder-Stepper an den Trapq eines
        # anderen Extruder-Steppers, sodass jeder G1 E Move den Buffer-
        # Stepper synchron mitzieht. Pattern aus
        # klippy/kinematics/extruder.py:ExtruderStepper.sync_to_extruder
        # (klippy/kinematics/extruder.py: ExtruderStepper.sync_to_
        # extruder).
        # Anwendungsfall: UNLOAD-Tip-Forming. Der Hauptextruder pusht/pullt
        # Filament durchs Hotend, der Buffer-Stepper folgt mit derselben
        # Geschwindigkeit — Filament-Strang fliesst durch den Buffer ohne
        # Stau (HALL2/HALL1) oder Leerlauf (HALL3). Auch loest die
        # Stepcompress-Cursor-Etablierung: solange der gemeinsame Trapq
        # aktiv ist, ist der Buffer-Stepper-last_step_clock immer frisch.
        extruder_name = gcmd.get('EXTRUDER', 'extruder')
        self._sync_to_extruder(extruder_name)

    def _unsync_if_synced(self):
        """Idempotent unsync helper. Cleanup-Pfade (BUFFER_HALT,
        BUFFER_AUTO_OFF, STOP_BUFFER_FILL) rufen das auf damit ein
        zwischen SYNC_TO_EXTRUDER und UNSYNC abgebrochenes Macro nicht
        den Stepper am Extruder-Trapq zurueck laesst (P7-24).
        """
        return self.sync.unsync_if_synced()

    cmd_BUFFER_UNSYNC_help = "Unsync buffer-feeder stepper back to its own trapq"
    def cmd_BUFFER_UNSYNC(self, gcmd):
        # kehrt SYNC_TO_EXTRUDER um — Stepper bekommt seinen
        # eigenen Trapq zurueck. Buffer-eigene Move-Logik (cmd_BUFFER_FEED,
        # BUFFER_UNLOAD_PHASE3 etc.) laeuft danach sauber weiter.
        if self._unsync_if_synced():
            self._respond("Buffer-Feeder unsynced — own trapq active")
        else:
            self._respond("Buffer-Feeder is not synced — no-op")

    cmd_FORCE_BUFFER_FILL_help = "Manually trigger initial grip + fill cycle"
    def cmd_FORCE_BUFFER_FILL(self, gcmd):
        if not self.entrance_detected:
            raise self._cmd_error("FORCE_BUFFER_FILL aborted: no filament at entrance")
        if self.hall_overflow or self._state == STATE_OVERFLOW:
            raise self._cmd_error("FORCE_BUFFER_FILL aborted: HALL1 overflow active")
        if self._state == STATE_JAM or self._jam_active:
            raise self._cmd_error("FORCE_BUFFER_FILL aborted: JAM active. Use BUFFER_CLEAR_JAM first.")
        # Refuse during print-PAUSE — FORCE_BUFFER_FILL is meant as a
        # full "initial grip + fill" cycle, and issuing a real grip
        # move while the printer is paused would queue unexpected
        # motion (same reason the entrance-insert handler suppresses
        # auto-grip during suspension).
        if self._bang_bang_suspended:
            raise self._cmd_error(
                "FORCE_BUFFER_FILL aborted: print is paused (bang-bang "
                "suspended). RESUME the print first, or use AUTO_OFF + "
                "AUTO_ON to take manual control.")
        # State guard per spec §5: only valid transition from IDLE
        # or RUNOUT into INITIAL_GRIP. Reject otherwise — accidentally
        # re-entering while AUTO / MANUAL_* / a LOAD-UNLOAD phase is
        # running would stomp over the active motion, and in
        # LOAD_PHASE_3 would pop the blocking caller out of its loop
        # into a surprise grip move.
        if self._state not in (STATE_IDLE, STATE_RUNOUT):
            raise self._cmd_error(
                "FORCE_BUFFER_FILL aborted: feeder busy (state=%s). "
                "Call STOP_BUFFER_FILL or BUFFER_AUTO_OFF first."
                % self._state)
        # Operator explicitly invoked the full fill cycle. Clear the
        # stale HALT flag AND the AUTO_OFF-by-user flag. Initial-grip
        # itself drops to STATE_IDLE on completion (or stays in
        # INITIAL_GRIP for the optional follow-feed if grip_follow_
        # distance > 0); STATE_AUTO is then engaged automatically by
        # the auto-engage hooks (auto_engage_on_print_start /
        # on_entrance_insert) — but ONLY if _auto_off_by_user is
        # cleared. Without that clear, BUFFER_AUTO_OFF →
        # FORCE_BUFFER_FILL would grip 10s and then stay IDLE
        # forever, the "fill" part never running.
        # Also consume any pending RUNOUT-recovery: this manual fill
        # IS the recovery, no need for RESUME to re-trigger.
        self._halt_requested = False
        self._auto_off_by_user = False
        self._runout_recovery_pending = False
        # Wait for any lingering in-flight chunk from a prior aborted
        # move to drain. Otherwise the initial-grip's end_time would
        # undershoot by the old chunk's remaining trapq duration
        # (since _halt_motion leaves _last_move_end_time intact).
        self._wait_for_move_done(gcmd)
        self._start_initial_grip(self.reactor.monotonic())

    cmd_STOP_BUFFER_FILL_help = "Abort any ongoing fill/grip/manual and return to IDLE"
    def cmd_STOP_BUFFER_FILL(self, gcmd):
        # Like BUFFER_AUTO_OFF (full reset, sticky auto-off), but
        # additionally clears phase-specific scratch state for the
        # active grip / follow / phase3 workflow that AUTO_OFF leaves
        # alone. STOP_BUFFER_FILL is "abort the current fill cycle".
        self._full_reset_to_idle(label="STOP_BUFFER_FILL",
                                 full=True,
                                 sticky_auto_off=True)
        # Phase-spezifische Cleanup-Flags NACH dem Helper, weil
        # _unsync_if_synced (im Helper) ueber _exit_overflow →
        # _resume_after_overflow im Edge-Case _grip_follow_active=True
        # setzen kann. Inverse Reihenfolge wuerde das wieder ueber-
        # schreiben (verifiziert von Sonnet/Codex Phase D Review).
        self._initial_grip_end_time = None
        self._grip_follow_active = False
        self._load_phase3_distance = 0.0
        self._respond("All feed loops stopped (workflow will abort at next wait)")

    cmd_BUFFER_STATE_DUMP_help = "Dump full buffer_feeder state to console"
    def cmd_BUFFER_STATE_DUMP(self, gcmd):
        return self.diagnostics.cmd_BUFFER_STATE_DUMP(gcmd)

    # ---- runtime parameter tuning (Issue #28) ----
    # Hot-swap setter for the five hardware-discovery parameters. All
    # writes are picked up by the next read on the streaming/auto path
    # (no Klipper restart). NO persistence: the operator manually
    # transfers a confirmed value to lll.cfg.
    cmd_BUFFER_SET_help = (
        "Live-tune buffer parameters without restart. All args optional:\n"
        "  CHUNK_MM              flush_callback_chunk_mm (mm,  default 15, lll.cfg 45)\n"
        "  SPEED                 feed_speed              (mm/s, default 30, lll.cfg 70)\n"
        "  ACCEL                 accel                   (mm/s^2, move ramp for new chunks)\n"
        "  INTERRUPT_CHUNK_MM    interrupt_chunk_mm      (mm,  default 9, cap <= MAX_MOVE_CHUNK_MM)\n"
        "  LEAD_TIME             lead_time               (s,   default 0.3, lll.cfg 0.12; warn outside 0.05..1.0)\n"
        "  MAX_MOVE_CHUNK_MM     max_move_chunk_mm       (mm,  default 50)\n"
        "  FEED_SPEED_GAIN       feed_speed_gain         (x,   between-zone multiplier)\n"
        "  HALL3_DEMAND_GAIN     hall3_demand_gain       (x,   HALL3 over-push multiplier, default 1.5)\n"
        "  MIN_FEED_FLOOR        min_feed_floor          (mm/s, low-speed minimum for HALL3)\n"
        "  FILAMENT_DIAMETER     filament_diameter       (mm,  volumetric flow conversion)\n"
        "  DEBUG_EVENTS          buffer_debug_events     (0/1, handler trace logs)\n"
        "  DEBUG_METRICS         buffer_debug_metrics    (0/1, per-second metrics)\n"
        "  STRICT_START_GUARD    strict_print_start_guard(0/1, block AUTO until real extrusion)\n"
        "  CRITICAL_GUARD_S      critical_action_guard_s (s,   quiet window after risky actions)\n"
        "  CONSERVATIVE_MODE     buffer_conservative_mode(0/1, longer safety guards)\n"
        "  HIGH_FLOW_MM3S        high_flow_mm3s_threshold(mm3/s, allow carry below min_feed_floor)\n"
        "  JAM_ACTION            jam_action              (gcode macro on JAM; pass empty string to disable)\n"
        "Without args: prints current values. No persistence — copy "
        "the final value into lll.cfg manually."
    )
    def cmd_BUFFER_SET(self, gcmd):
        return apply_runtime_tuning(self, gcmd)

    cmd_CALIBRATE_FEEDER_SYNC_help = ("No-op under python-ansatz — feeder is not synced "
                                      "to extruder. Use MEASURE_LOAD_START for distance calibration.")
    def cmd_CALIBRATE_FEEDER_SYNC(self, gcmd):
        gc = self.printer.lookup_object('gcode')
        gc.respond_info(
            "CALIBRATE_FEEDER_SYNC: not applicable in python-ansatz.\n"
            "The feeder is decoupled from the extruder — no rotation_distance\n"
            "modulation to calibrate. For distance-per-revolution accuracy,\n"
            "use MEASURE_LOAD_START, feed a known amount, verify at the feeder."
        )

    cmd_MEASURE_LOAD_START_help = "Start MEASURE_LOAD toggle mode — feed button toggles feeder"
    def cmd_MEASURE_LOAD_START(self, gcmd):
        if self._state not in (STATE_IDLE, STATE_AUTO):
            raise self._cmd_error("MEASURE_LOAD_START requires IDLE or AUTO state")
        # If AUTO was already actively feeding (HALL3-triggered
        # bang-bang), stop it and reset to IDLE so the first button
        # press is unambiguously "start measurement feed".
        self._continuous_feed = False
        self._halt_motion()
        # Operator is entering a distinct calibration workflow —
        # consume any pending RUNOUT-recovery so RESUME afterwards
        # doesn't surprise-grip on top of the measurement.
        self._runout_recovery_pending = False
        self._set_state(STATE_IDLE)
        self._measure_load_active = True
        self._measure_feeding = False
        self._measure_load_distance = 0.0
        self._respond("MEASURE_LOAD active — press feed button to start/stop")

    cmd_MEASURE_LOAD_STOP_help = "Stop MEASURE_LOAD mode and print distance"
    def cmd_MEASURE_LOAD_STOP(self, gcmd):
        self._continuous_feed = False
        self._halt_motion()
        self._measure_report()
        self._measure_load_active = False
        self._measure_feeding = False
        # Always return to IDLE — the operator can explicitly
        # BUFFER_AUTO_ON again if they want the bang-bang loop back.
        self._set_state(STATE_IDLE)

    def cmd_ENABLE_RUNOUT_SENSOR(self, gcmd):
        self._print_running = True
        # Bench-Re-Arm (Review Runde 2): auf einem Rig das nie via
        # virtual_sdcard druckt, erreicht print_stats nie
        # 'printing'/'paused' — der Print-ended-Latch wuerde nach der
        # ersten Bench-Session fuer immer haengen. Manuelles Armen von
        # _print_running ist der Session-Start-Marker, also hier mit
        # freigeben.
        self._print_end_msg_shown = False
        self._respond("print_running=1 (runout PAUSE will fire)")

    def cmd_DISABLE_RUNOUT_SENSOR(self, gcmd):
        self._print_running = False
        self._respond("print_running=0 (runout PAUSE suppressed)")

    def _try_restore_gcode_state(self, from_command=False):
        return self.cleanup.try_restore_gcode_state(
            from_command=from_command)

    def cmd_BUFFER_RESTORE_STATE(self, gcmd):
        if self._try_restore_gcode_state(from_command=True):
            self._respond("Restored gcode-state from 'buffer_feeder_op'")
        else:
            self._respond("No 'buffer_feeder_op' gcode-state to restore")

    def cmd_BUFFER_SAVE_MACRO_STATE(self, gcmd):
        """Invoked by the _SAVE_E_MODE macro. Saves gcode state AND
        marks it as valid-to-restore. Running again before a restore
        simply overwrites the slot."""
        # SAVE is a precondition for the macro's following M83/moves.
        self.cleanup.save_gcode_state(from_command=True)

    def cmd_BUFFER_RESTORE_MACRO_STATE(self, gcmd):
        """Restore and consume the macro slot, retaining it on failure."""
        self.cleanup.try_restore_gcode_state(from_command=True)

    def cmd_BUFFER_CLEAR_JAM(self, gcmd):
        return self.cleanup.clear_jam()

    # -----------------------------------------------------------------------
    # Utilities
    # -----------------------------------------------------------------------

    def _cmd_error(self, msg):
        gc = self.printer.lookup_object('gcode')
        return gc.error(msg)

    def _raise_if_jam(self):
        """Hard JAM lockout: raise gcmd_error if state==JAM or _jam_active.
        Used by entry-checks that allow OVERFLOW (UNLOAD-recovery,
        LOAD_PHASE_3 with OVERFLOW_OK) but never JAM."""
        if self._state == STATE_JAM or self._jam_active:
            raise self._cmd_error(
                "BufferFeeder: JAM active — aborting. "
                "Use BUFFER_CLEAR_JAM after inspection. (UNLOAD is allowed.)")

    def _raise_if_locked_out(self, gcmd=None, direction=+1):
        """Abort a caller if the feeder is in a safety lockout.

        Called from blocking phase commands and from BUFFER_WAIT_IDLE so
        that OVERFLOW / JAM / user-abort events propagate out of macros
        as errors rather than silently letting the macro run into the
        next phase.

        _halt_requested auto-clears after raising so that the next
        command issued by the operator starts from a clean slate.
        """
        if self._halt_requested:
            self._halt_requested = False
            raise self._cmd_error("BufferFeeder: HALT requested — aborting workflow")
        # Forward-Operationen (LOAD/feed) werden bei OVERFLOW/JAM
        # geblockt. UNLOAD ist Retract — die einzige sinnvolle Recovery
        # bei Overflow oder Jam. Daher direction=-1 fuer Retract-Pfade,
        # die Lockout durchbrechen duerfen. HALT bleibt absolut.
        if direction > 0:
            # hall_overflow direkt pruefen, nicht nur state — catched
            # die Race, wenn AUTO_OFF / STOP_BUFFER_FILL state schon nach
            # IDLE gesetzt hat, der naechste main_tick aber erst noch
            # OVERFLOW reasserten wird.
            if self._state == STATE_OVERFLOW or self.hall_overflow:
                raise self._cmd_error(
                    "BufferFeeder: HALL1 OVERFLOW active — aborting. "
                    "Clear overflow, then retry. (UNLOAD is allowed.)")
            self._raise_if_jam()

    # -----------------------------------------------------------------------
    # Status API
    # -----------------------------------------------------------------------

    def get_status(self, eventtime):
        print_phase = self._refresh_print_phase(eventtime)
        return {
            # Live state
            'state':                    self._state,
            'hall_empty':               self.hall_empty,
            'hall_full':                self.hall_full,
            'hall_overflow':            self.hall_overflow,
            'entrance_detected':        self.entrance_detected,
            'feed_button_pressed':      self.feed_button_pressed,
            'retract_button_pressed':   self.retract_button_pressed,
            'continuous_feed':          self._continuous_feed,
            'feed_direction':           self._continuous_feed_direction,
            'feed_distance_acc_mm':     self._feed_distance_accumulator,
            'total_accumulated_mm':     self._accumulated_feed_distance,
            'commanded_pos_mm':         self._commanded_pos,
            'print_running':            self._print_running,
            'benchmark_mode_active':    self._benchmark_mode_active(eventtime),
            'benchmark_mode_left_s':    self._benchmark_mode_remaining(eventtime),
            'jam_active':               self._jam_active,
            'fault_overflow':           self._fault_overflow,
            'overflow_overlay_enabled': self.use_overflow_overlay,
            'post_load_overflow_grace': self._post_load_overflow_grace,
            'bang_bang_suspended':      self._bang_bang_suspended,
            'halt_requested':           self._halt_requested,
            'runout_follow_active':     self._runout_follow_active,
            'runout_recovery_pending':  self._runout_recovery_pending,
            'measure_load_active':      self._measure_load_active,
            'measure_load_distance_mm': self._measure_load_distance,
            'macro_state_saved':        self._macro_state_saved,
            'synced_to_extruder':       self._stepper_synced_to,
            'print_phase':              print_phase,
            'print_extrusion_seen':     self._print_extrusion_seen,
            'critical_action_guard_remaining_s': self._critical_action_guard_remaining(eventtime),
            'critical_action_guard_reason': self._critical_action_guard_reason,
            # Config values (exposed so LOAD/UNLOAD macros don't hardcode)
            'manual_speed':             self.manual_speed,
            'burst_speed':              self.burst_speed,
            'load_fast_speed':          self.load_fast_speed,
            'load_slow_speed':          self.load_slow_speed,
            'unload_fast_speed':        self.unload_fast_speed,
            'unload_phase3_speed':      self.unload_phase3_speed,
            'load_fast_distance':       self.load_fast_distance,
            'load_endstop_sensor':      self.load_endstop_sensor,
            'load_sensor_to_extruder':  self.load_sensor_to_extruder,
            'load_slow_distance':       self.load_slow_distance,
            'load_buffer_max':          self.load_buffer_max,
            'unload_sync_distance':     self.unload_sync_distance,
            'unload_fast_max':          self.unload_fast_max,
            'min_temp':                 self.min_temp,
            'use_fault_overlay':        self.use_fault_overlay,
            # Snapshot-faehige Tuning-Werte fuer Macros (Review
            # 2026-07-09): BUFFER_BASELINE_RUN restauriert jam_action/
            # hall3_demand_gain nach dem Run auf die Vorher-Werte.
            **tuning_status(self),
        }


# ---------------------------------------------------------------------------
# Config hook
# ---------------------------------------------------------------------------

def load_config_prefix(config):
    return BufferFeeder(config)
