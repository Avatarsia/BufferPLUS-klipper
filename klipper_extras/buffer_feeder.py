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
#
# See docs/superpowers/specs/2026-04-23-python-ansatz-design.md for the
# full design rationale and feature mapping.

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
    MAIN_TICK_INTERVAL, MAX_T0_LOOKAHEAD_S, MIN_CURSOR_FRESHNESS_S,
    REPRIME_GAP_S, STABLE_DROP_GRACE,
    STATE_AUTO, STATE_IDLE, STATE_INIT, STATE_INITIAL_GRIP,
    STATE_JAM, STATE_LOADING_PULL, STATE_LOADING_PUSH,
    STATE_MANUAL_FEED, STATE_MANUAL_RETRACT, STATE_OVERFLOW,
    STATE_RUNOUT, STATE_UNLOADING,
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

from .buffer_fault import FaultManager
from .buffer_cleanup import CleanupCoordinator
from .buffer_config import BufferConfigValues
from .buffer_modulator import ExtruderVelocityTracker
from .buffer_sensors import HallSensorMonitor
from .buffer_state import BufferRuntimeState
from .buffer_stepper import SyncCoordinator
from .buffer_types import AnchorPlan, CleanupOptions, Hall1Context


class BufferFeeder:
    # This class remains the Klipper-facing entry-point, but the config,
    # runtime state, guard typing, and cleanup logic now live in
    # dedicated helper modules so the state machine is easier to reason
    # about. Fault overlays are explicitly scoped to OVERFLOW handling.
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.name = config.get_name().split()[1]   # "mellow"
        self.settings = BufferConfigValues.from_config(config)
        self.settings.apply(self)
        self.runtime_state = BufferRuntimeState()
        self.runtime_state.apply(self)
        self._debug_event_last = {}

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
        if not self.buffer_debug_events:
            return
        now = None
        try:
            now = self.reactor.monotonic()
        except Exception:
            now = None
        if min_interval and now is not None:
            last = self._debug_event_last.get(key)
            if last is not None and (now - last) < min_interval:
                return
            self._debug_event_last[key] = now
        logging.log(level, "buffer_event[%s]: " + message, key, *args)

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
            ('BUFFER_UNLOAD_PHASE3',        self.cmd_BUFFER_UNLOAD_PHASE3,        None),
            ('BUFFER_SYNC_TO_EXTRUDER',     self.cmd_BUFFER_SYNC_TO_EXTRUDER,     None),
            ('BUFFER_UNSYNC',               self.cmd_BUFFER_UNSYNC,               None),
            ('FORCE_BUFFER_FILL',           self.cmd_FORCE_BUFFER_FILL,           None),
            ('STOP_BUFFER_FILL',            self.cmd_STOP_BUFFER_FILL,            None),
            ('BUFFER_STATE_DUMP',           self.cmd_BUFFER_STATE_DUMP,           None),
            ('BUFFER_SET',                  self.cmd_BUFFER_SET,                  None),
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
        # Klipper fires idle_timeout:printing during MCU init even without
        # an active print (print_stats state = 'standby'). Guard against
        # this boot artifact so _print_running is only armed for real prints.
        try:
            ps = self.printer.lookup_object('print_stats', None)
            if ps is not None:
                if ps.get_status(self.reactor.monotonic()).get(
                        'state', '') == 'standby':
                    return
        except Exception:
            pass
        self._print_running = True
        # RESUME / print-start: bang-bang resumes.
        self._bang_bang_suspended = False
        # Documented RESUME-clears-JAM path (spec §10, README §Jam).
        # When Klipper transitions back to 'printing' (typically after
        # a RESUME following our PAUSE-on-jam), drop the JAM lockout
        # so the feeder resumes AUTO. HALL1 is still respected — if
        # physical overflow is still present, we fall into OVERFLOW.
        jam_recovery = self._state == STATE_JAM or self._jam_active
        if jam_recovery:
            self._respond("RESUME: clearing JAM lockout")
            self._clear_recovery_flags()
            # If the jam interrupted a LOAD/UNLOAD macro mid-flight,
            # the macro's SAVE_GCODE_STATE is still pending. Restore
            # it now so the user's E-mode isn't stuck on M83 after
            # RESUME. Parity with BUFFER_CLEAR_JAM's recovery path.
            self._try_restore_gcode_state()
            if self.hall_overflow:
                # Cannot resume while overflow physically present.
                self._enter_overflow()
            elif self.entrance_detected:
                self._enable_stepper()
                self._set_state(STATE_AUTO)
            else:
                self._set_state(STATE_IDLE)
            return

        # RUNOUT-recovery path (runout_pause=1 case):
        #   Runout → STATE_RUNOUT + PAUSE → idle_timeout:ready armed
        #       _bang_bang_suspended.
        #   Reinsert during RUNOUT → STATE_IDLE + _runout_recovery_pending.
        #   RESUME: if the flag is armed AND filament is still at the
        #       entrance AND no operator-control flag is set, run
        #       grip+fill so the buffer is full before the print
        #       resumes actual extrusion.
        #
        # Gated by _runout_recovery_pending so that RESUME for other
        # idle-state reasons (MEASURE_LOAD_STOP, an idle console
        # session, BUFFER_HALT drop-to-IDLE, AUTO_OFF) does NOT queue
        # surprise grip motion. Respects _halt_requested for the same
        # reason. Flag is consumed by the grip or by any subsequent
        # state change away from IDLE.
        if (self._runout_recovery_pending
                and self._state == STATE_IDLE
                and self.entrance_detected
                and not self._auto_off_by_user
                and not self._halt_requested
                and not self.hall_overflow):
            self._runout_recovery_pending = False
            self._respond("RESUME after runout-reinsert — starting grip + fill")
            self._start_initial_grip(self.reactor.monotonic())
            return

        # Auto-engage Bang-bang beim Print-Start.
        #
        # Wir wollen, dass der Buffer im Druck mitlaeuft, ohne dass der
        # User das Boot-autostart-Feature pflegen oder BUFFER_AUTO_ON in
        # PRINT_START selbst eintragen muss. Bedingung: Filament am
        # Eingang, State ist IDLE, kein Operator-Lockout aktiv.
        # Konfigurierbar via auto_engage_on_print_start (default True).
        if (self.auto_engage_on_print_start
                and self._state == STATE_IDLE
                and self.entrance_detected
                and not self._auto_off_by_user
                and not self._halt_requested
                and not self._is_hall1_active(Hall1Context.AUTO_ON)):
            self._respond("Print start — engaging AUTO")
            self._enable_stepper()
            self._set_state(STATE_AUTO)

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
        if not self._bang_bang_suspended:
            return False
        try:
            ps = self.printer.lookup_object('print_stats')
            ps_state = ps.get_status(eventtime).get('state')
        except Exception:
            return False
        if ps_state in ('printing', 'paused'):
            return False
        # Print is no longer pause-recoverable (complete / cancelled /
        # error / standby). Clear the stale lock so next entrance-
        # insert / AUTO_ON_IF_READY proceeds.
        self._bang_bang_suspended = False
        self._respond("Stale bang-bang-suspend cleared "
                      "(print state=%s)" % ps_state)
        return True

    def _is_active_print_state(self, eventtime=None):
        """Best-effort check whether Klipper currently reports an
        active print.

        Flush-driven auto-streaming must only run while a print is
        actually active. During Klipper idle/standby the watchdog may
        still refresh the stepcompress cursor with tiny anchor moves,
        but _on_mcu_flush must not turn those anchor refreshes into a
        self-sustaining filament stream.
        """
        if eventtime is None:
            eventtime = self.reactor.monotonic()
        try:
            ps = self.printer.lookup_object('print_stats', None)
            if ps is None:
                return False
            return ps.get_status(eventtime).get('state') == 'printing'
        except Exception:
            return False

    def _on_idle_ready(self, *args):
        # idle_timeout:ready fires for BOTH a manual PAUSE during a
        # print (RESUME erwartet) AND for the natural end of a print
        # job ("Done printing file" → no RESUME ever). read
        # print_stats.state so we differentiate. Pre-fix the buffer
        # would stay _bang_bang_suspended=True after a clean print
        # end, blocking auto-grip on the next entrance-insert and
        # forcing the operator to run BUFFER_AUTO_OFF + AUTO_ON or
        # FORCE_BUFFER_FILL just to load fresh filament.
        #
        # Guard: Klipper fires idle_timeout:printing then :ready during
        # MCU init, which would set _print_running=True and then arm
        # _bang_bang_suspended before any real print has started.
        # Ignore all idle_timeout events until the startup grace is done.
        if not self._startup_grace_done:
            self._print_running = False
            return
        if self._print_running:
            ps_state = None
            try:
                ps = self.printer.lookup_object('print_stats')
                ps_state = ps.get_status(self.reactor.monotonic()).get('state')
            except Exception:
                pass
            if ps_state == 'paused':
                # Real PAUSE — RESUME is expected, suspend bang-bang
                # so a queued G1 E in the resumed file doesn't fire
                # an unexpected feed before the print actually resumes.
                self._bang_bang_suspended = True
                if self._continuous_feed:
                    self._continuous_feed = False
                    self._halt_motion()
                self._respond("Print paused — bang-bang suspended until RESUME")
            else:
                # Print ended normally (state=complete/standby/None).
                # Buffer stays available for manual workflow + reinsert
                # auto-grip. _bang_bang_suspended stays whatever it
                # was (operator may have set it explicitly via
                # BUFFER_AUTO_OFF; we don't override).
                self._respond("Print ended — buffer ready for next "
                              "filament change or print")
        self._print_running = False

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
        self._respond("*** HALL1 OVERFLOW — Feeder disabled, lockout engaged ***")
        self._continuous_feed = False
        # Save the interrupted state and pending distance BEFORE
        # _halt_motion() zeroes _pending_remaining_mm, so _exit_overflow
        # can resume the move after HALL1 clears.
        self._overflow_interrupted_state = self._state
        self._overflow_resume_mm  = self._pending_remaining_mm
        self._overflow_resume_dir = self._pending_direction
        self._overflow_resume_spd = self._pending_speed
        self._halt_motion()
        self._schedule_stepper_disable()
        if self._grip_follow_active:
            self._overflow_interrupted_follow = True
            self._grip_follow_active = False
            self._initial_grip_end_time = None
        # P7-35 fault-overlay: in overlay mode for LOAD_PHASE_3, keep
        # _state=LOAD_PHASE_3 and only set the overlay flag. The phase3
        # cmd loop terminates via fault_overflow check, postcheck raises
        # like the legacy STATE_OVERFLOW path.
        self._fault_overflow = True
        if self.use_overflow_overlay and self._state == STATE_LOADING_PUSH:
            return
        self._set_state(STATE_OVERFLOW)

    def _clear_recovery_flags(self):
        """Clear jam-related recovery flags reused by cleanup paths."""
        return self.fault.clear_recovery_flags()

    def _resume_after_overflow(self):
        """Restore the pre-overflow workflow if it is still resumable."""
        return self.fault.resume_after_overflow()

    def _exit_overflow(self):
        # defer state-transition while SYNC is active.
        # Otherwise we'd transition OVERFLOW → IDLE → AUTO while the
        # stepper is still bound to extruder_trapq. The next bang-bang
        # tick or _submit_move would then queue moves to own_trapq —
        # those moves go live at the next BUFFER_UNSYNC and corrupt
        # the stepcompress cursor (see Eifel-Joe's hardware log: SYNC
        # #1 → HALL1-fall → OVERFLOW→IDLE→AUTO → UNSYNC → SYNC#2 →
        # 'stepcompress Invalid sequence'). The macro will call
        # BUFFER_UNSYNC itself; SyncCoordinator.unsync_if_synced
        # re-runs this method once the sync binding is released.
        if self._stepper_synced_to is not None:
            return
        # P7-35 fault-overlay: clear overlay flag without state change
        # when overlay path is active. _resume_after_overflow handles
        # restarting the interrupted phase3 move via _overflow_resume_*.
        if (self.use_overflow_overlay
                and self._fault_overflow
                and self._state == STATE_LOADING_PUSH):
            self._fault_overflow = False
            self._respond("HALL1 cleared — overflow lockout released (overlay)")
            self._resume_after_overflow()
            return
        if self._state != STATE_OVERFLOW:
            return
        self._respond("HALL1 cleared — overflow lockout released")
        # Mark cursor resync pending. _main_tick will submit a
        # 0.05mm anchor with forced_t0=None (safe reactor context) so the
        # stepcompress cursor is synchronised before the first fill-move.
        # _on_mcu_flush skips while this flag is set (see below).
        self._needs_overflow_prime = True
        # Go to IDLE (the _set_state hook calls _halt_motion + stepper-disable).
        self._set_state(STATE_IDLE)
        self._fault_overflow = False
        self._resume_after_overflow()

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
            # C-cont T2: Velocity-Tracker tick (50Hz, intern throttled
            # auf sample_interval=0.025s = 40Hz). Read-only passiver
            # Observer ueber extruder.get_status — kein flush, kein
            # SYNC, kein Side-Effect auf den Druckkopf.
            self.velocity_tracker.tick(eventtime)

            self._check_debounce(eventtime)

            # During startup grace, only sensor polling runs. No state
            # transitions, no bang-bang, no continuous feed — we wait
            # for Klipper to deliver initial sensor callbacks so we
            # learn the real hardware picture.
            if not self._startup_grace_done:
                return eventtime + MAIN_TICK_INTERVAL

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
                if persist_duration >= self.hall1_persist_timeout:
                    if self.buffer_debug_metrics:
                        logging.info(
                            "buffer_feeder: HALL1-Persist %.2fs >= "
                            "%.2fs threshold — entering OVERFLOW state "
                            "(C-cont T6)",
                            persist_duration, self.hall1_persist_timeout)
                    self._enter_overflow()
                    return eventtime + MAIN_TICK_INTERVAL
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
                return eventtime + MAIN_TICK_INTERVAL

            self._tick_safety_timeouts(eventtime)

            # Deferred disable: motor_disable must not be called while
            # steps are unprocessed in the trapq (step-gen fires
            # motor_enable with a past time via add_active_callback →
            # Timer too close).
            if self._pending_disable and not self._move_in_flight():
                self._pending_disable = False
                self._disable_stepper()

            # Idle-Watchdog.
            # In STATE_IDLE neither the bang-bang flush-callback nor any
            # other periodic move-submit runs. _last_move_end_time freezes
            # at the time the last queued move ended. Once Klipper's
            # background flush_handler fires more than CLOCK_DIFF_MAX
            # (~17s @ 48 MHz) after that anchor, compress_bisect_add
            # degenerates into an "Invalid sequence" → MCU shutdown.
            # The reactive REPRIME path in _submit_single_trapezoid runs
            # only on the NEXT submit, which never comes in IDLE.
            #
            #
            # Same stale-cursor pathology hits in STATE_AUTO when the
            # buffer sits in the bang-bang hysteresis dead-zone (neither
            # hall_full nor hall_empty). _on_mcu_flush does nothing
            # there; _bang_bang_tick does nothing there. last_step_clock
            # ages from the boot anchor until the first hall_empty
            # finally arms a submit — by then queue_step interval has
            # blown past int32 (P7-73 clamps far-future forced_t0 but
            # cannot heal the past-end). Extend the watchdog gate to
            # STATE_AUTO with extra sub-gates so it never collides with
            # an active bang-bang session.
            #
            # Fix: fire a 0.05 mm anchor (boot-anchor / SYNC-gap-anchor
            # pattern, see SyncCoordinator._submit_anchor_move) whenever
            # idle_anchor_gap seconds elapsed since the last move and
            # the last watchdog-anchor. The anchor refreshes
            # last_step_clock and re-arms _last_move_end_time, so the
            # next background flush stays inside CLOCK_DIFF_MAX.
            #
            # Gates:
            #   - state in (IDLE, AUTO): IDLE handled by; AUTO is
            #     the bang-bang dead-zone case from/Issue #31.
            #     MANUAL/LOAD/UNLOAD have their own move cadence +
            #     dedicated reprime paths and stay out.
            #   - not synced: when bound to extruder trapq, moves come
            #     from the extruder side and we must not inject our own.
            #   - not _move_in_flight / not pending: no overlap with a
            #     drain-in-progress (defense in depth; in IDLE these are
            #     typically False already).
            #   - _last_idle_anchor_time gating: a second tick right
            #     after firing must NOT submit another anchor — the same
            #     idle_anchor_gap window applies between anchors.
            #   - AUTO-specific sub-gates keep the watchdog out
            #     of any active bang-bang flow:
            #       * not _continuous_feed — bang-bang inactive
            #       * not hall_empty — no open feed request
            #       * not _needs_overflow_prime — no pending prime
            #       * not hall_full — buffer already full;
            #         further forward anchors would push toward HALL1
            #         overflow (Codex-Verify finding: ~18mm/h
            #         drift without this gate at default idle_anchor_gap=10s)
            # Diagnostic-Logging fuer Watchdog-Blocks.
            # Wenn die "harten" Move-/Sync-Gates clean sind aber ein
            # Sub-Gate (continuous_feed/hall_empty/hall_full/needs_
            # overflow_prime) den Anchor blockiert, log das aktive
            # Sub-Gate. Hilft kuenftige Issue-#32-Klassen ohne weitere
            # Hardware-Repros zu diagnostizieren (DWELL-SA3 Eifel-Joe
            # Crash #3: 56.6s ohne Anchor trotz scheinbar quiescentem
            # AUTO). Rate-limit: einmal pro idle_anchor_gap-Fenster.
            if (self._state in (STATE_IDLE, STATE_AUTO)
                    and not self._stepper_synced_to
                    and not self._pending_disable
                    and not self._move_in_flight()
                    and self._pending_remaining_mm == 0.0):
                _mcu = self.stepper.get_mcu()
                _mcu_now = _mcu.estimated_print_time(
                    self.reactor.monotonic())
                _gap_moves_diag = _mcu_now - self._last_move_end_time
                if _gap_moves_diag > self.idle_anchor_gap * 1.5:
                    _blocking = []
                    if self._continuous_feed:
                        _blocking.append("_continuous_feed")
                    if self.hall_empty:
                        _blocking.append("hall_empty")
                    if self.hall_full:
                        _blocking.append("hall_full")
                    if self._needs_overflow_prime:
                        _blocking.append("_needs_overflow_prime")
                    # separate watermark for log-rate (not
                    # _last_idle_anchor_time — that is only updated when
                    # an anchor actually fires; in the blocked path no
                    # anchor fires so using it for rate-limiting would
                    # spam every tick).
                    _last_skip_log = getattr(
                        self, '_last_watchdog_skip_log_time', 0.0)
                    if _blocking and (
                            _mcu_now - _last_skip_log
                            > self.idle_anchor_gap):
                        logging.debug(
                            "buffer_feeder: watchdog skip "
                            "(state=%s gap=%.1fs > %.1fs threshold) "
                            "blocked by: %s (P7-76 C diagnostic)",
                            self._state, _gap_moves_diag,
                            self.idle_anchor_gap,
                            ",".join(_blocking))
                        self._last_watchdog_skip_log_time = _mcu_now

            # A (Issue #32 Crash unter, Eifel-Joe Hardware-
            # Log 2026-05-12 klippy.log "(2).txt"): Watchdog HARD-block
            # waehrend aktivem Print. Diagnose:
            #   1. Watchdog-Anchor laeuft legitim (gap > threshold),
            #      schiebt stepcompress.last_step_clock auf ~551.18s.
            #   2. 4 nachfolgende Bang-Bang-Tick-Submits (continuous_-
            #      feed-streaming, forced_t0=None Pfad) clampen t0 via
            #      A auf mcu_now + lead_time = ~551.13s.
            #   3. ABER: last_step_clock = 551.18 vom legitimen Anchor
            #      -> interval = 551.13 - 551.18 = -10.4ms -> negativer
            #      interval -> stepcompress-Crash (i=-500471).
            # Architektonisch ist `t0 = max(forced_t0, lme, en, mcu_-
            # now)` blind gegen `last_step_clock`. Waehrend eines aktiven
            # Prints uebernimmt _on_mcu_flush + (forced_t0-Pfad)
            # die Cursor-Pflege; Watchdog ist konzeptionell nur fuer
            # echtes IDLE/Standby. -> Print-Stats-Check skipt Watchdog
            # bei state == 'printing'. paused/complete/cancelled/standby
            # zaehlen NICHT als active print (paused: User-Halt, kein
            # ongoing flush; complete: lookahead leer; standby/cancelled:
            # kein Print).
            #
            # P7-78 (Issue #29 Crash unter, Eifel-Joe Hardware-
            # Log 2026-05-13): Der A Hard-Block ist zu strikt.
            # In der HALL2-Hysterese-Zwischenzone laeuft _on_mcu_flush
            # minutenlang nicht — Klipper's motion_queuing.flush_handler
            # ruft den Callback nur synchron mit Step-Generation; ohne
            # Steps kein Callback. stepcompress.last_step_clock altert,
            # und der erste Bang-Bang-Submit nach Stille wirft c=7
            # Invalid sequence. Eifel-Joe Beleg: 163.2s Funkstille
            # zwischen IDLE->AUTO (Z.8706 @ 1063.5s) und Crash (Z.9895
            # @ 1226.7s). Loesung: Print-Block-Override — wenn _on_mcu_-
            # flush messbar laenger als idle_anchor_gap nicht gerufen
            # wurde, weichen wir den Hard-Block auf und lassen den
            # Watchdog feuern. Boot-Schutz: _last_mcu_flush_time == 0.0
            # zaehlt nicht (frischer Boot, noch nie ein Flush).
            _print_active = False
            try:
                _ps = self.printer.lookup_object('print_stats', None)
                if _ps is not None:
                    _ps_status = _ps.get_status(eventtime)
                    _print_active = (
                        _ps_status.get('state') == 'printing')
            except Exception:
                _print_active = False

            # Print-Block-Stale-Override: nur evaluieren wenn
            # ueberhaupt geblockt waere und mindestens ein Flush
            # bereits gesehen wurde (Boot-Schutz). Strict > damit
            # Stille == idle_anchor_gap noch geblockt bleibt.
            #
            # P7-78v2 (Codex-Verify Finding): _p778_override Flag
            # markiert den Override-Pfad, damit der innere Anchor-
            # Submit `forced_t0=mcu_now + lead_time` uebergibt und
            # den B SKIP-statt-Clamp im else-Branch umgeht.
            # Ohne den Flag wuerde der Override zwar feuern, aber
            # `_submit_anchor_move()` (ohne kwarg) faellt in den
            # forced_t0==None else-Branch -> th_time = aktive
            # Toolhead-Queue (far-future) -> B SKIP ->
            # silent return ohne realen Submit -> Bug wirkungslos.
            _p778_override = False
            if _print_active and self._last_mcu_flush_time > 0.0:
                _mcu_p778 = self.stepper.get_mcu()
                _mcu_now_p778 = _mcu_p778.estimated_print_time(
                    self.reactor.monotonic())
                _flush_silence = (
                    _mcu_now_p778 - self._last_mcu_flush_time)
                if _flush_silence > self.idle_anchor_gap:
                    logging.info(
                        "buffer_feeder: print-block stale override "
                        "(flush silent for %.1fs > %.1fs threshold, "
                        "P7-78)",
                        _flush_silence, self.idle_anchor_gap)
                    _print_active = False
                    _p778_override = True

            # Hotfix 10 (Issue #31 Refactor-Port, Hardware 2026-05-14
            # klippy.log: MCU 'LLL_PLUS' shutdown "Timer too close" beim
            # ersten Druckstart nach Klipper-Idle, 0 Watchdog-Anchor-
            # Fires in der gesamten Idle-Phase).
            #
            # P7-75 Original-Bedingung `not self.hall_empty` schuetzt
            # vor Race mit Bang-Bang-Feed-Request. Diese Race existiert
            # NUR im Reactor-Tick-Bang-Bang-Pfad (use_flush_callback_-
            # bang_bang=False). Bei use_flush_callback_bang_bang=True
            # ist _bang_bang_tick ein No-Op, Bang-Bang feuert NUR via
            # _on_mcu_flush, der waehrend Klipper-Idle gar nicht
            # gerufen wird (keine motion_queuing-Aktivitaet). Resultat:
            # hall_empty=True + Klipper-Idle + flush_callback-Pfad ->
            # kein Submit -> last_step_clock altert -> Timer too close
            # beim ersten Print-Start-Submit nach > CLOCK_DIFF_MAX
            # (~16.78s @ 48MHz).
            #
            # Fix: hall_empty-Gate nur aktiv wenn use_flush_callback_-
            # bang_bang=False (klassischer Reactor-Tick-Pfad). Im
            # flush-callback-Pfad muss Watchdog auch bei hall_empty
            # feuern. Andere Sub-Gates (_continuous_feed, hall_full,
            # _needs_overflow_prime) bleiben unveraendert — kein Race
            # mit aktivem Stream, kein Forward-Feed in vollen Buffer.
            #
            # Siehe tests/test_idle_watchdog_anchor.py:
            #   test_watchdog_fires_in_auto_with_hall_empty_when_flush_callback_bangbang
            #   test_watchdog_still_blocks_in_auto_with_hall_empty_when_classic_bangbang
            hall_empty_block = (self.hall_empty
                                and not self.use_flush_callback_bang_bang)
            if (self._state in (STATE_IDLE, STATE_AUTO)
                    and not self._stepper_synced_to
                    and not self._pending_disable
                    and not self._move_in_flight()
                    and self._pending_remaining_mm == 0.0
                    and not self._continuous_feed
                    and not hall_empty_block
                    and not self.hall_full
                    and not self._needs_overflow_prime
                    and not _print_active):  # P7-77 A + P7-78 Override
                mcu = self.stepper.get_mcu()
                mcu_now = mcu.estimated_print_time(
                    self.reactor.monotonic())
                gap_moves = mcu_now - self._last_move_end_time
                gap_anchors = mcu_now - self._last_idle_anchor_time
                if (gap_moves > self.idle_anchor_gap
                        and gap_anchors > self.idle_anchor_gap):
                    # lme-clamp NUR direkt vor dem Anchor-
                    # Submit, nicht bei jedem Tick. D rollte lme
                    # unconditional bei jedem Tick zurueck — das
                    # radierte den Anchor-Effekt fuer alle nachfolgenden
                    # Bang-Bang-Ticks (sie sahen lme=mcu_now statt
                    # lme=anchor_end_time und produzierten t0-Werte
                    # zurueck unter last_step_clock). Inside des Submit-
                    # Branches: clamp greift nur einmal pro Watchdog-
                    # Anchor, danach setzt _submit_anchor_move lme
                    # konsistent in die Zukunft.
                    if self._last_move_end_time > mcu_now:
                        logging.debug(
                            "buffer_feeder: pre-anchor lme-clamp "
                            "(was %.3fs ahead of mcu_now, P7-77 C; "
                            "ex-P7-76 D, scope reduced)",
                            self._last_move_end_time - mcu_now)
                        self._last_move_end_time = mcu_now
                    try:
                        if _p778_override:
                            # P7-78v2: aktiver Print hat typisch weit-
                            # zukuenftige toolhead.get_last_move_time().
                            # Ohne forced_t0 wuerde der Anchor-Submit in
                            # den forced_t0==None else-Branch fallen
                            # (Z.3248) und durch B SKIP (Z.3275)
                            # silent abgebrochen. Mit forced_t0=mcu_now+
                            # lead_time geht der Submit in den forced_t0
                            # !=None Branch (Z.3203), der NICHT vom
                            # B Skip betroffen ist.
                            #
                            # P7-78v3 (Codex-Verify MEDIUM): lead_time
                            # mit min(..., MAX_FORCED_T0_LOOKAHEAD) cap,
                            # damit ein via BUFFER_SET ungewoehnlich
                            # gesetzter lead_time > 2.0s den forced_t0
                            # nicht in den Clamp-Pfad zieht. Im
                            # Default-Fall (lead_time=0.3s) no-op.
                            _p778_forced_t0 = (
                                mcu_now
                                + min(self.lead_time,
                                      MAX_T0_LOOKAHEAD_S))
                            self.sync._submit_anchor_move(
                                forced_t0=_p778_forced_t0)
                        else:
                            self.sync._submit_anchor_move()
                        self._last_idle_anchor_time = mcu_now
                        # In IDLE we re-defer the disable so IDLE
                        # semantics (stopped AND disabled) are restored
                        # once the anchor-move drains. In AUTO we must
                        # NOT disable — bang-bang owns the next submit
                        # and a disable here would race with it.
                        if self._state == STATE_IDLE:
                            self._schedule_stepper_disable()
                        logging.info(
                            "buffer_feeder: %s anchor fired "
                            "(gap=%.1fs, threshold=%.1fs)",
                            self._state.lower(),
                            gap_moves, self.idle_anchor_gap)
                    except Exception:
                        logging.exception(
                            "buffer_feeder: idle/auto anchor failed")

            self._tick_cooldown_end(eventtime)
            self._tick_grip_completion(eventtime)

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

            self._tick_runout_follow(eventtime)

            # LOAD Phase 3 — feed until HALL2 or max distance.
            if self._state == STATE_LOADING_PUSH:
                self._load_phase3_tick(eventtime)

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

            self._tick_pending_chunk(eventtime)

            # C-cont T10: Diagnostik-Logs (alle 1s wenn buffer_debug_metrics).
            if self.buffer_debug_metrics:
                if (eventtime - self._last_metrics_log_time) >= 1.0:
                    target_speed = self._compute_target_feed_speed()
                    flow = self.velocity_tracker.get_volumetric_flow()
                    hall1_persist_info = (
                        "%.2fs" % (self.reactor.monotonic()
                                   - self._hall1_active_since)
                        if self._hall1_active_since is not None
                        else "off")
                    logging.info(
                        "buffer_metrics: state=%s hall=[H3:%s H2:%s H1:%s] "
                        "tracker_vel=%.1fmm/s flow=%.1fmm3/s ready=%s "
                        "target_speed=%.1fmm/s "
                        "pending_remaining=%.1fmm hall1_persist=%s",
                        self._state,
                        'on' if self.hall_empty else 'off',
                        'on' if self.hall_full else 'off',
                        'on' if self.hall_overflow else 'off',
                        self.velocity_tracker.get_velocity(),
                        flow,
                        self.velocity_tracker.is_ready(),
                        target_speed,
                        self._pending_remaining_mm,
                        hall1_persist_info)
                    self._last_metrics_log_time = eventtime

        except Exception:
            logging.exception("buffer_feeder main_tick error")

        return eventtime + MAIN_TICK_INTERVAL

    def _tick_safety_timeouts(self, eventtime):
        """Hard-safety aborts route through _trigger_jam: phase
        commands raise via WAIT_IDLE, recovery requires explicit
        BUFFER_CLEAR_JAM / BUFFER_AUTO_OFF / STOP_BUFFER_FILL."""
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
        if self._pending_remaining_mm <= 0:
            return
        if self._abort_signalled():
            # Codex-Verify Q6b: HALL1-Early-Exit muss auch den Sub-Chunk-Cap
            # zuruecksetzen — sonst leakt der cap (e.g. interrupt_chunk_mm=9)
            # auf den naechsten unrelated _submit_move-Call. T8's
            # target_speed<=0-Branch macht beides; dieser Pfad muss es auch.
            self._pending_remaining_mm = 0.0
            self._pending_submit_chunk_cap = None
            return
        # HALL2 (buffer full) MUST abort a forward streaming
        # sequence. _abort_signalled covers HALL1 (overflow) but not
        # the bang-bang stop-on-full case. Without this clamp the
        # sub-chunks of a 45mm chunk would keep flowing into a full
        # buffer until the original distance was exhausted — exactly
        # the overshoot the hardware-test 2026-05-12 hit.
        # Only forward direction + AUTO state — retract / UNLOAD must
        # still drain pending distance regardless of HALL2 (it pulls
        # filament back, doesn't push into the buffer).
        if (self._pending_direction > 0
                and self._state == STATE_AUTO
                and self.hall_full):
            self._pending_remaining_mm = 0.0
            self._continuous_feed = False
            return
        if (self._pending_direction < 0
                and self._state == STATE_MANUAL_RETRACT
                and not self.entrance_detected):
            self._halt_motion()
            self._respond("Retract-Burst gestoppt — Filament am Eingang weg")
            return
        if self._pending_speed <= 0:
            return
        # C-cont T8: Sub-Chunk-Speed dynamisch aus SpeedModulator fuer
        # AUTO+forward-Streaming. Vorher fest auf _pending_speed
        # (eingefroren beim ersten Submit) — fuehrte zu Speed-Lag wenn
        # HALL-State zwischen Sub-Chunks wechselte (HALL3 -> Zwischen-
        # zone). Legacy-Paths (LOAD/UNLOAD/MANUAL/Retract) behalten den
        # frozen _pending_speed.
        sub_chunk_speed = self._pending_speed
        if (self._state == STATE_AUTO
                and self._pending_direction > 0):
            modulated = self._compute_target_feed_speed()
            if modulated <= 0.0:
                # HALL1 active mid-chunk — beende Pending-Stream.
                # (_abort_signalled greift bereits weiter oben, aber
                # diese Branch deckt jeden anderen target=0-Pfad ab.)
                self._pending_remaining_mm = 0.0
                self._pending_submit_chunk_cap = None
                return
            sub_chunk_speed = modulated
            self._continuous_feed_speed = sub_chunk_speed
        # honour the sub-chunk cap if the active stream was
        # opened with one. AUTO+streaming sets cap=interrupt_chunk_mm
        # so HALL-interrupt latency stays bounded; legacy paths
        # (LOAD/UNLOAD/MANUAL) leave _pending_submit_chunk_cap=None and
        # fall back to max_move_chunk_mm exactly as before.
        cap = self._pending_submit_chunk_cap
        if cap is None or cap > self.max_move_chunk_mm:
            cap = self.max_move_chunk_mm
        chunk_duration = cap / sub_chunk_speed
        mcu = self.stepper.get_mcu()
        now_pt = mcu.estimated_print_time(eventtime)
        gap = self._last_move_end_time - now_pt
        # Submit next chunk when <= half-a-chunk remains in the
        # currently-queued move, so next trapezoid starts right at
        # the prior one's end_time.
        if gap <= chunk_duration * 0.5:
            chunk = min(self._pending_remaining_mm, cap)
            # R1: this is the streaming continuation of an
            # already-running burst. Pass streaming=True so the
            # _enable_stepper() and _last_enable_schedule_time floor
            # are skipped — same rationale as the lookahead branch in
            # _on_mcu_flush. _move_in_flight() is implicit here: the
            # gap-check above only fires while the previous trapezoid
            # is still in the future.
            self._submit_single_trapezoid(
                self._pending_direction * chunk, sub_chunk_speed,
                streaming=True)
            # C-cont T8: keep _pending_speed aligned with the latest
            # modulated speed so the next iteration's chunk_duration
            # math and any consumer of _pending_speed reflects reality.
            self._pending_speed = sub_chunk_speed
            self._pending_remaining_mm -= chunk
            if self._pending_remaining_mm <= 0:
                # Drop the cap so a subsequent unrelated _submit_move
                # call does not inherit it.
                self._pending_submit_chunk_cap = None

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
        """SpeedModulator — bestimmt target_feed_speed in STATE_AUTO.

        Portiert die zentrale Hardware-Erkenntnis aus C-cont Hotfix 7:
        ein fixer HALL3-Refill mit voller feed_speed ueberschiesst beim
        Mellow-Buffer die sehr kleine HALL2->HALL1-Sicherheitsmarge.
        Deshalb skaliert HALL3 jetzt mit dem realen Extruder-Verbrauch
        und nutzt nur noch einen sanften MIN_FLOOR als Unterkante.

          HALL1 (overflow)  -> 0.0
          HALL2 (full)      -> 0.0
          HALL3 (empty):
            tracker not_ready / vel < floor -> floor
            sonst                           -> min(max(vel * 1.5, floor), feed_speed)
          Zwischenzone:
            tracker not_ready / vel < floor -> 0.0
            sonst                           -> min(vel * feed_speed_gain, feed_speed)
        """
        floor = self.min_feed_floor
        floor_epsilon = 1e-6
        if self.hall_overflow:
            self._modulator_feeding = False
            return 0.0
        if self.hall_full:
            self._modulator_feeding = False
            return 0.0
        vel_ready = self.velocity_tracker.is_ready()
        extruder_vel = self.velocity_tracker.get_velocity() if vel_ready else 0.0
        if self.hall_empty:
            if not vel_ready or extruder_vel < floor - floor_epsilon:
                self._modulator_feeding = True
                return floor
            self._modulator_feeding = True
            return min(max(extruder_vel * 1.5, floor), self.feed_speed)
        if not vel_ready or extruder_vel < floor - floor_epsilon:
            self._modulator_feeding = False
            return 0.0
        self._modulator_feeding = True
        return min(extruder_vel * self.feed_speed_gain, self.feed_speed)

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
        self._flush_submit_streaming_chunk(step_gen_time)

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

    def _flush_submit_streaming_chunk(self, step_gen_time):
        """Continuous-streaming submit driven by the SpeedModulator.

        Every flush callback computes target_speed via
        _compute_target_feed_speed from the HALL state and the
        ExtruderVelocityTracker. Submit a single sub-chunk
        (interrupt_chunk_mm) when target_speed > 0 and no
        sub-chunk pipeline is already running. Otherwise no move —
        either HALL1/HALL2 says stop, or the SpeedModulator decided
        there is no feed demand right now.
        """
        target_speed = self._compute_target_feed_speed()
        if target_speed <= 0.0:
            self._debug_event(
                'flush_no_demand',
                "no submit target_speed=0 hall1=%s hall2=%s hall3=%s ready=%s",
                self.hall_overflow, self.hall_full, self.hall_empty,
                self.velocity_tracker.is_ready(), min_interval=2.0)
            if self.buffer_debug_metrics:
                logging.debug(
                    "buffer_feeder: target_speed=0 - no submit "
                    "(hall1=%s ready=%s)",
                    self.hall_overflow,
                    self.velocity_tracker.is_ready())
            return

        if (self.use_flush_callback_bang_bang
                and not self._is_active_print_state()):
            self._debug_event(
                'flush_skip_idle',
                "skip idle auto-stream target_speed=%.3f state=%s",
                target_speed, self._state, min_interval=2.0)
            if self.buffer_debug_metrics:
                logging.debug(
                    "buffer_feeder: idle-suppress auto-stream "
                    "(target=%.1f, no active print)",
                    target_speed)
            return

        move_active = self._move_in_flight()
        if move_active:
            remaining = self._last_move_end_time - step_gen_time
            if remaining > self.lead_time:
                self._debug_event(
                    'flush_skip_inflight',
                    "skip in-flight remaining=%.3f lead=%.3f",
                    remaining, self.lead_time, min_interval=1.0)
                return  # too early, no new chunk yet
        if self._pending_remaining_mm > 0:
            self._debug_event(
                'flush_skip_pending',
                "skip pending_remaining_mm=%.3f",
                self._pending_remaining_mm, min_interval=1.0)
            return  # sub-chunk pipeline already running

        # Cursor-Freshness-Contract (Hardware-Beleg 2026-05-14
        # klippy_refactor_*_repro.log): wenn kein move in flight UND
        # _last_move_end_time aelter als MIN_CURSOR_FRESHNESS_S, dann
        # ist die MCU-side last_step_clock potenziell ueber USB-Sende-
        # Latenz + MCU-busy in "Step in der Vergangenheit"-Race
        # anfaellig (queue_step interval = mcu_now + lead_time -
        # last_step_clock). Watchdog-Anchor alle 10s ist best-effort,
        # schliesst aber das Race-Window nicht; PR #39 Watchdog-Fix
        # haelt Cursor im IDLE frisch, aber das Race-Window zwischen
        # Watchdog-Anchor und erstem streaming-Submit nach PRINT_START
        # bleibt.
        #
        # Fix: pre-anchor (0.05mm @ feed_speed) als garantierter
        # Cursor-Refresh vor dem streaming-Submit, wenn cursor zu alt.
        # forced_t0 wird intern (siehe _plan_t0_anchor) auf
        # mcu_now + lead_time geclampt — anchor landet sicher in
        # naher Zukunft, last_step_clock danach <= lead_time alt.
        # Effekt: queue_step interval fuer den danach folgenden
        # streaming-Submit << 1s, race-frei unter jeder USB-Latenz.
        #
        # Komplementaer zu PR #39 (Watchdog-Hall-Empty-Bypass) und
        # 97e97e7 (sanitize stale-future-floors). Defense-in-depth.
        if not move_active:
            mcu_now = self.stepper.get_mcu().estimated_print_time(
                self.reactor.monotonic())
            cursor_age = mcu_now - self._last_move_end_time
            if cursor_age > MIN_CURSOR_FRESHNESS_S:
                self._debug_event(
                    'flush_freshness_anchor',
                    "pre-anchor cursor_age=%.2fs > %.2fs threshold",
                    cursor_age, MIN_CURSOR_FRESHNESS_S, min_interval=2.0)
                anchor_dir = -1.0 if self.hall_overflow else 1.0
                self._submit_move(
                    anchor_dir * ANCHOR_NUDGE_MM,
                    self.feed_speed,
                    forced_t0=step_gen_time + self.lead_time)

        if move_active:
            anchor = self._last_move_end_time
        else:
            anchor = step_gen_time + self.lead_time

        if not self._continuous_feed:
            # Real inactive -> active transition: reset safety counters.
            self._feed_distance_accumulator = 0.0
            self._feed_deadline_time = None
            self._continuous_feed = True
            self._continuous_feed_direction = 1
        self._continuous_feed_speed = target_speed
        self._debug_event(
            'flush_submit',
            "submit speed=%.3f anchor=%.3f move_active=%s chunk=%.3f",
            target_speed, anchor, move_active, self.interrupt_chunk_mm,
            min_interval=1.0)

        # Pipeline-Cap: one sub-chunk in-flight at a time so a HALL1/
        # HALL2 transition can stop the pipeline at the next flush
        # callback instead of letting an already-queued chunk overrun
        # the buffer arm.
        self._submit_move(
            self.interrupt_chunk_mm,
            target_speed,
            forced_t0=anchor,
            streaming=move_active,
            submit_chunk_cap=self.interrupt_chunk_mm)

    def _exit_phase3_stable(self, *, set_grace, respond_text):
        """Common exit-sequence when HALL2 (buffer full) or HALL1 (with
        OVERFLOW_OK=1) has been stable for the configured dwell.

        Halts streaming, clears all four Phase 3 trackers, optionally
        arms _post_load_overflow_grace (P7-46 bounce-suppression for
        HALL1-stable exit), and chooses STATE_AUTO when entrance is
        present + operator hasn't issued AUTO_OFF/HALT, else IDLE
        (P7-49: AUTO is the natural post-LOAD state — bang-bang
        re-fills on next extrusion regardless of print_running)."""
        self._continuous_feed = False
        self._halt_motion()
        self._load_phase3_hall_full_since = None
        self._load_phase3_hall_overflow_since = None
        self._load_phase3_hall_full_drop_since = None
        self._load_phase3_hall_overflow_drop_since = None
        self._respond(respond_text)
        if set_grace:
            self._post_load_overflow_grace = True
        if (self.entrance_detected
                and not self._auto_off_by_user
                and not self._halt_requested):
            self._set_state(STATE_AUTO)
        else:
            self._set_state(STATE_IDLE)

    def _load_phase3_tick(self, eventtime):
        threshold = self._load_phase3_stable_timeout
        # HALL2 (full) Stabilitaets-Tracking mit Drop-Toleranz:
        # kurze False-Edges (<STABLE_DROP_GRACE) lassen die Stable-Uhr
        # weiterlaufen — der Bowden-Spring drueckt den Arm zurueck, der
        # Stepper foerdert weiter, der Arm geht wieder hoch. Hard-Reset
        # nur wenn der Sensor laenger als die Grace komplett aus bleibt.
        if self.hall_full:
            self._load_phase3_hall_full_drop_since = None
            if self._load_phase3_hall_full_since is None:
                self._load_phase3_hall_full_since = eventtime
            full_dwell = eventtime - self._load_phase3_hall_full_since
            if full_dwell >= threshold:
                if threshold > 0:
                    msg = ("LOAD Phase 3: HALL2 stable %.1fs, buffer full"
                           % full_dwell)
                else:
                    msg = "LOAD Phase 3: HALL2 reached, buffer full"
                self._exit_phase3_stable(set_grace=False, respond_text=msg)
                return
        elif self._load_phase3_hall_full_since is not None:
            # Sensor gerade abgefallen — Grace-Window starten/checken.
            if self._load_phase3_hall_full_drop_since is None:
                self._load_phase3_hall_full_drop_since = eventtime
            elif (eventtime - self._load_phase3_hall_full_drop_since
                  >= STABLE_DROP_GRACE):
                self._load_phase3_hall_full_since = None
                self._load_phase3_hall_full_drop_since = None
        # HALL1 (overflow) Stabilitaets-Tracking — nur wenn OVERFLOW_OK=1
        # gesetzt wurde. Sonst ist der HALL1-Pfad weiterhin via
        # _main_tick → _enter_overflow abgewickelt: legacy-Pfad
        # state=OVERFLOW + raise im cmd_BUFFER_LOAD_PHASE3-Postcheck;
        # use_overflow_overlay-Pfad belaesst state=LOAD_PHASE_3 und setzt
        # nur _fault_overflow=True, raise dann im selben Postcheck via
        # fault_overflow-Flag.
        if self._load_phase3_overflow_ok:
            if self.hall_overflow:
                self._load_phase3_hall_overflow_drop_since = None
                if self._load_phase3_hall_overflow_since is None:
                    self._load_phase3_hall_overflow_since = eventtime
                overflow_dwell = eventtime - self._load_phase3_hall_overflow_since
                if overflow_dwell >= threshold:
                    msg = ("LOAD Phase 3: HALL1 stable %.1fs, "
                           "buffer overfilled (treating as full)"
                           % overflow_dwell)
                    # set_grace=True: bounce-suppression — _main_tick
                    # would otherwise re-trigger _enter_overflow on the
                    # next cycle since HALL1 stays asserted.
                    self._exit_phase3_stable(set_grace=True, respond_text=msg)
                    return
            elif self._load_phase3_hall_overflow_since is not None:
                # Same drop-tolerance pattern wie bei HALL2.
                if self._load_phase3_hall_overflow_drop_since is None:
                    self._load_phase3_hall_overflow_drop_since = eventtime
                elif (eventtime - self._load_phase3_hall_overflow_drop_since
                      >= STABLE_DROP_GRACE):
                    self._load_phase3_hall_overflow_since = None
                    self._load_phase3_hall_overflow_drop_since = None
        if self._load_phase3_distance >= self._load_phase3_max_distance:
            self._continuous_feed = False
            self._halt_motion()
            # Route through _trigger_jam so the blocking LOAD_PHASE3
            # command's post-loop _raise_if_locked_out raises and
            # aborts the LOAD_FILAMENT macro instead of letting it
            # print "LOAD abgeschlossen".
            self._trigger_jam(
                "LOAD_TIMEOUT",
                "LOAD Phase 3: max_distance %dmm reached without HALL2 — check sensor/buffer"
                % int(self._load_phase3_max_distance))
            return
        if not self._move_in_flight():
            # P7-35 fault-overlay: pause move submission while overlay
            # flag is set. _enter_overflow already halted current motion;
            # re-submitting here would immediately re-saturate HALL1.
            if self.use_overflow_overlay and self._fault_overflow:
                return
            # if HALL1 is asserted —
            # buffer is already full, the stable-timer is what we want
            # to elapse, NOT more filament-push. Submitting another
            # chunk while HALL1 is on stuffs filament against the
            # extruder clamp via the bowden, which then bleeds through
            # the heatbreak into the nozzle (visible as filament
            # squirting from the nozzle pre-extrude). Hold the chunk
            # stream while the timer counts up; HALL1-fall (drop-grace)
            # naturally re-arms the submit path.
            if self.hall_overflow:
                return
            # Clip chunk so the per-call MAX_DISTANCE is a hard cap.
            remaining = self._load_phase3_max_distance - self._load_phase3_distance
            chunk = min(self._load_phase3_chunk_distance, remaining)
            if chunk > 0:
                self._submit_move(chunk, self._load_phase3_speed)
                self._load_phase3_distance += chunk

    # -----------------------------------------------------------------------
    # Jam detection tick
    # -----------------------------------------------------------------------

    def _jam_tick(self, eventtime):
        try:
            if not self.jam_detection_enabled or self._jam_active:
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
        """Pick a safe print_time for the next motor_enable/disable.

        P7-58: Removed the toolhead.get_last_move_time() lookup that
        used to feed a 4th max() argument. The buffer stepper runs in
        own_trapq — toolhead's last move time has no bearing on our
        own enable scheduling, and the lookup synchronously runs the
        toolhead lookahead pipeline (_process_lookahead /
        _flush_lookahead in mainline klippy/toolhead.py) on every
        call. _enable_stepper() runs before every chunk submit, so
        each bang-bang feed forced the toolhead through a lookahead
        flush → brief extruder pause (host-side planning work) →
        visible gaps in the print.

        The remaining three floors (mcu_now, _last_move_end_time,
        _last_enable_schedule_time) are sufficient to keep the
        Buffer-Stepper's own enable→step→disable ordering correct
        and preserve the P7-56 'Timer too close' fix. The real
        toolhead-anchor for the first step lives in _submit_single_-
        trapezoid (forced_t0 / th_time path), which still uses
        toolhead.get_last_move_time() once per chunk-stream — but
        only in the gap/first-chunk path, not for every enable.
        """
        mcu = self.stepper.get_mcu()
        mcu_now = mcu.estimated_print_time(self.reactor.monotonic())
        pt = max(mcu_now + self.lead_time,
                 self._last_move_end_time + self.lead_time,
                 self._last_enable_schedule_time + self.lead_time)
        self._last_enable_schedule_time = pt
        return pt

    def _schedule_stepper_disable(self):
        """Disable stepper, deferring to tick if a move is in flight.

        Calling motor_disable while steps are still unprocessed in the
        trapq causes Klipper to register an add_active_callback that
        fires motor_enable(past_time) when the step-generator processes
        those steps.  set_digital(past_time, 1) then causes the MCU to
        raise 'Timer too close'.  Deferring until flight=False lets the
        step-generator finish before motor_disable touches the callbacks.
        """
        if self._move_in_flight():
            self._pending_disable = True
        else:
            self._disable_stepper()

    def _enable_stepper(self):
        if self._stepper_enable is None:
            return
        self._pending_disable = False   # cancel any deferred disable
        try:
            pt = self._schedule_time_for_enable_toggle()
            self._stepper_enable.motor_enable(pt)
        except Exception:
            logging.exception("buffer_feeder: enable_stepper failed")

    def _disable_stepper(self):
        # Nach Disable ist der Stepcompress-Cursor nicht mehr synchron —
        # beim naechsten Re-Enable muss set_position() aufgerufen werden
        # (partieller Reprime ohne flush_step_generation). Flag VOR dem
        # early-return setzen damit es auch ohne stepper_enable wirkt.
        self._stepcompress_primed = False
        if self._stepper_enable is None:
            return
        try:
            pt = self._schedule_time_for_enable_toggle()
            self._stepper_enable.motor_disable(pt)
        except Exception:
            logging.exception("buffer_feeder: disable_stepper failed")

    def _submit_move(self, signed_distance, speed, forced_t0=None,
                     streaming=False, submit_chunk_cap=None):
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
        """
        if signed_distance == 0 or speed <= 0:
            return
        # defense-in-depth sync guard. _bang_bang_tick
        # and _on_mcu_flush already guard, but _submit_move is reachable
        # via several other call-sites (LOAD/UNLOAD phases, manual cmds,
        # _tick_grip_completion). None of those should fire while a
        # macro-driven SYNC has the stepper bound to the extruder trapq
        # — submitting on own_trapq during that window would queue moves
        # on the wrong trapq AND can trip the gap>5s reprime in
        # _submit_single_trapezoid → toolhead.flush_step_generation()
        # mid-print → extruder stops.
        if self._stepper_synced_to is not None:
            return
        # OVERFLOW: nur Forward-Submits ablehnen. Retract (signed_distance < 0)
        # ist die einzige Recovery-Bewegung, die einen überfüllten Buffer
        # entlasten kann — sonst sitzt der User in der Sackgasse.
        # Ausnahme: LOAD_PHASE_3 mit OVERFLOW_OK=1 darf weiterfeeden
        # waehrend HALL1 aktiv — sonst koennte das Stable-Tracking nie
        # die Schwelle erreichen, weil der Arm bei jedem Reject zurueck-
        # faellt und HALL1 deaktiviert. _load_phase3_tick beendet die
        # Phase sauber sobald HALL1 stable lange genug ist.
        if self._is_hall1_active(Hall1Context.SUBMIT_MOVE) and signed_distance > 0:
            logging.warning("buffer_feeder: forward move rejected — HALL1 active "
                            "(distance=%.1f speed=%.1f)", signed_distance, speed)
            self._continuous_feed = False
            self._pending_remaining_mm = 0.0
            return

        # Cancel any previously-streaming sequence before starting new.
        self._pending_remaining_mm = 0.0

        # Ensure the first chunk starts no earlier than the last enable/disable
        # toggle scheduled on the MCU. Without this guard, a move submitted
        # right after an enable (e.g. LOAD_PHASE1 after IDLE→disable) would
        # send a trapezoid with t0 < enable_time → MCU "Timer too close".
        # R1: only apply this floor when NOT streaming. In the
        # streaming-lookahead path the stepper is already enabled and
        # _last_enable_schedule_time is stale — pushing _last_move_end_-
        # time forward would break the abuttend-anchor.
        if not streaming:
            self._last_move_end_time = max(self._last_move_end_time,
                                           self._last_enable_schedule_time)

        distance_abs = abs(signed_distance)
        direction = 1.0 if signed_distance > 0 else -1.0

        # hardware-safe sub-chunking. submit_chunk_cap (typ.
        # interrupt_chunk_mm=9) limits the first trapezoid to a size
        # that lets HALL2 abort within one sub-chunk's duration. The
        # rest streams via _pending_remaining_mm with per-sub-chunk
        # HALL re-checks in _tick_pending_chunk. Falls back to
        # max_move_chunk_mm when caller does not request sub-chunking.
        chunk_cap = submit_chunk_cap if submit_chunk_cap is not None \
            else self.max_move_chunk_mm
        if chunk_cap > self.max_move_chunk_mm:
            chunk_cap = self.max_move_chunk_mm
        first_chunk = min(distance_abs, chunk_cap)
        self._submit_single_trapezoid(direction * first_chunk, speed,
                                       forced_t0=forced_t0,
                                       streaming=streaming)
        remaining = distance_abs - first_chunk
        if remaining > 0:
            self._pending_remaining_mm = remaining
            self._pending_direction = direction
            self._pending_speed = speed
            # propagate the cap so _tick_pending_chunk uses
            # the same sub-chunk size for the streaming continuation.
            self._pending_submit_chunk_cap = chunk_cap

    def _submit_single_trapezoid(self, signed_distance, speed,
                                  forced_t0=None, streaming=False):
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

        Returns None on success; returns early (without queuing) when
        the stepper is synced to an extruder trapq or when the
        computed anchor is far-future stale.
        """
        # Innermost defense-in-depth sync guard. Upstream paths already
        # guard, but the dangerous side-effects (flush_step_generation
        # + set_position(0) mid-print) live here, so this final gate
        # makes "no own-trapq submit while synced" robust against any
        # future caller.
        if self._stepper_synced_to is not None:
            return

        mcu = self.stepper.get_mcu()
        mcu_now = mcu.estimated_print_time(self.reactor.monotonic())
        if forced_t0 is not None:
            self._sanitize_forced_t0_floors(mcu_now)
        gap = mcu_now - self._last_move_end_time

        was_primed = self._stepcompress_primed
        need_reprime = self._reprime_stepcompress_if_needed(forced_t0, gap)

        # Skip motor-enable in streaming lookahead — previous chunk
        # already enabled the motor and pushed _last_enable_schedule_-
        # time forward.
        if not streaming:
            self._enable_stepper()

        t0 = self._compute_t0_anchor(
            forced_t0, mcu_now, was_primed, need_reprime, streaming)
        if t0 is None:
            return  # anchor skipped (far-future), nothing queued

        self._append_trapezoid_and_record(t0, signed_distance, speed)

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
        stale_anchor = (self._last_move_end_time <= mcu_now)
        en = (0.0
              if (streaming and was_primed and not need_reprime
                  and not stale_anchor)
              else self._last_enable_schedule_time)

        if forced_t0 is not None:
            # Clamp far-future forced_t0. motion_queuing.flush_all_steps
            # can hand a step_gen_time = need_step_gen_time (toolhead
            # queue end, tens of seconds in the future) at print-start.
            # Letting it through grows queue_step intervals past int32
            # signed (44.7s @ 48 MHz) → "Timer too close" MCU shutdown.
            if forced_t0 > mcu_now + MAX_T0_LOOKAHEAD_S:
                logging.warning(
                    "buffer_feeder: forced_t0 clamped — was %.2fs "
                    "ahead of mcu_now (far-future flush guard)",
                    forced_t0 - mcu_now)
                forced_t0 = mcu_now + self.lead_time
            return AnchorPlan(
                t0=max(forced_t0, self._last_move_end_time, en, mcu_now),
                enable_floor=en,
            )

        if self._last_move_end_time > mcu_now + self.lead_time:
            # Streaming abut path: previous chunk is still in the future.
            return AnchorPlan(
                t0=max(self._last_move_end_time, en),
                enable_floor=en,
            )

        # First chunk / gap recovery: anchor on toolhead print_time.
        toolhead = self.printer.lookup_object('toolhead')
        th_time = toolhead.get_last_move_time()
        t0 = max(th_time + self.lead_time, self._last_move_end_time, en)
        if t0 > mcu_now + MAX_T0_LOOKAHEAD_S:
            # th_time is far ahead (active print with filled toolhead
            # queue). Clamping to mcu_now would land BEFORE
            # stepcompress.last_step_clock (already advanced by an
            # earlier anchor) → negative interval crash. The next
            # flush-callback submit will supply a race-free
            # step_gen_time anchor and take over cursor maintenance.
            logging.warning(
                "buffer_feeder: anchor skipped — th_time %.2fs "
                "ahead, would corrupt last_step_clock "
                "(th_time=%.3f lme=%.3f en=%.3f mcu_now=%.3f)",
                t0 - mcu_now, th_time, self._last_move_end_time,
                en, mcu_now)
            return AnchorPlan(
                t0=None,
                skip_reason="far_future_toolhead_anchor",
                rate_limit_idle_anchor=True,
                clamp_last_move_end_time=(
                    mcu_now
                    if self._last_move_end_time > mcu_now + MAX_T0_LOOKAHEAD_S
                    else None
                ),
                toolhead_time=th_time,
                enable_floor=en,
            )
        return AnchorPlan(t0=t0, toolhead_time=th_time, enable_floor=en)

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
        distance = abs(signed_distance)
        direction = 1.0 if signed_distance > 0 else -1.0

        accel = self.accel
        cruise_v = speed
        accel_time = cruise_v / accel
        accel_dist = 0.5 * accel_time * cruise_v

        if distance < 2. * accel_dist:
            # Triangular profile — peak velocity is reduced so the move
            # exactly fits accel + decel.
            cruise_v = math.sqrt(distance * accel)
            accel_time = cruise_v / accel
            cruise_time = 0.0
            decel_time = accel_time
        else:
            cruise_dist = distance - 2. * accel_dist
            cruise_time = cruise_dist / cruise_v
            decel_time = accel_time

        self.trapq_append(self.trapq, t0,
                          accel_time, cruise_time, decel_time,
                          self._commanded_pos, 0., 0.,
                          direction, 0., 0.,
                          0., cruise_v, accel)

        end_time = t0 + accel_time + cruise_time + decel_time
        self._last_move_end_time = end_time
        self._commanded_pos += direction * distance

        self._current_move = {
            'end_time': end_time,
            'direction': direction,
            'distance': distance,
            'speed': cruise_v,
        }
        self._feed_distance_accumulator += distance
        self._accumulated_feed_distance += distance
        if self._measure_load_active and direction > 0:
            self._measure_load_distance += distance

        self.motion_queuing.note_mcu_movequeue_activity(end_time)

    def _move_in_flight(self):
        if self._current_move is None:
            return False
        mcu = self.stepper.get_mcu()
        now_pt = mcu.estimated_print_time(self.reactor.monotonic())
        return now_pt < self._current_move['end_time']

    def _halt_motion(self):
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
        self._continuous_feed = False
        self._continuous_feed_direction = 0
        self._continuous_feed_speed = 0.0
        # Clear the Schmitt-trigger latch on every hard stop so the
        # next AUTO session re-arms from live sensor/tracker state,
        # not from a stale "was feeding" hysteresis decision.
        self._modulator_feeding = False
        # Reset accumulator on every halt. After a halt the
        # accumulator is stale — leaving it set would cause a false
        # JAM_SAFETY_DISTANCE on the very first chunk of the next
        # session if _on_mcu_flush hasn't yet reset it (it does at
        # session start, but defense in depth covers all stop paths
        # including JAM/RUNOUT/PAUSE/CLEAR_JAM).
        self._feed_distance_accumulator = 0.0
        self._auto_between_since = None
        self._pending_remaining_mm = 0.0
        # drop the streaming sub-chunk cap so a subsequent
        # LOAD/UNLOAD/MANUAL pending-stream uses its own (max_move_-
        # chunk_mm) sizing without inheriting a stale AUTO cap.
        self._pending_submit_chunk_cap = None
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
        logging.info("buffer_feeder: %s", message)
        try:
            gc = self.printer.lookup_object('gcode')
            gc.respond_info("BufferFeeder: %s" % message)
        except Exception:
            pass

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
                            preserve_lockout=False):
        return self.cleanup.full_reset_to_idle(CleanupOptions(
            label=label,
            full=full,
            sticky_auto_off=sticky_auto_off,
            preserve_lockout=preserve_lockout,
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
        self._full_reset_to_idle(label="AUTO_OFF",
                                 full=True,
                                 sticky_auto_off=True)
        self._respond("AUTO off — workflow will abort at next wait; recovery flags cleared")

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
            if self._abort_signalled():
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

    cmd_BUFFER_LOAD_PHASE1_help = "LOAD Phase 1 — feeder alone fast to toolhead. DISTANCE=mm"
    def cmd_BUFFER_LOAD_PHASE1(self, gcmd):
        self._halt_requested = False    # ack any stale console HALT
        self._raise_if_locked_out(gcmd)
        self._check_phase_entry('LOAD_PHASE1', {
            STATE_IDLE, STATE_AUTO, STATE_RUNOUT, STATE_LOADING_PULL,
        })
        distance = gcmd.get_float('DISTANCE', self.load_fast_distance, above=0.)
        speed    = gcmd.get_float('SPEED',    self.load_fast_speed,    above=0.)
        # Stop any inherited bang-bang / manual dauerfeed and drain
        # any in-flight chunk so residual motion doesn't extend Phase 1.
        self._continuous_feed = False
        self._wait_for_move_done(gcmd)
        self._set_state(STATE_LOADING_PULL)
        self._enable_stepper()
        self._submit_move(+distance, speed)
        # Blocking: wait for move done, but pause-and-resume on HALL1 overflow
        # instead of aborting — _exit_overflow restores pending state so
        # streaming continues naturally. BUFFER_WAIT_IDLE would deadlock
        # because it also waits for state != busy-phase.
        try:
            self._wait_for_move_done_resume_on_overflow(gcmd)
        except Exception:
            # Release the phase state on error so it doesn't stay sticky.
            self._set_state(STATE_IDLE)
            raise
        self._set_state(STATE_IDLE)

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
        self._halt_requested = False
        # Parameter ZUERST parsen — overflow_ok beeinflusst den Lockout-
        # Check (P7-8/9). Wenn der Standard _raise_if_locked_out vorher
        # liefe, wuerde HALL1-aktiv sofort raisen, bevor wir den
        # OVERFLOW_OK=1 Modus auswerten koennen.
        max_distance = gcmd.get_float('MAX_DISTANCE', self.load_buffer_max, above=0.)
        speed        = gcmd.get_float('SPEED',        self.feed_speed,      above=0.)
        # Stable-Exit-Optionen: Sensoren muessen N Sekunden
        # KONTINUIERLICH aktiv sein bevor Phase 3 abbricht. Default 0
        # = altes Verhalten (Instant-Exit beim ersten HALL2-Trigger).
        # OVERFLOW_OK=1 → stable HALL1 ist auch ein legitimer Exit
        # (Buffer ueberfuellt → Filament ist da → Phase 2 fertig).
        # CHUNK_DISTANCE konfiguriert die Foerder-Chunkgroesse pro Tick
        # (Default 10mm — fuer LOAD-Wiederholung kann der Macro auf 50+
        # mm hochsetzen, weniger viele kleine Submits).
        stable_timeout = gcmd.get_float('STABLE_TIMEOUT', 0.0, minval=0.)
        overflow_ok    = bool(gcmd.get_int('OVERFLOW_OK', 0, minval=0, maxval=1))
        chunk_distance = gcmd.get_float('CHUNK_DISTANCE', 10.0, above=0.)
        # Lockout-Check: JAM ist immer absolut. OVERFLOW nur wenn nicht
        # OVERFLOW_OK gesetzt — sonst handhabt _load_phase3_tick den
        # HALL1-Stable-Exit selbst.
        self._raise_if_jam()
        if not overflow_ok:
            if (self._state == STATE_OVERFLOW
                    or self._is_hall1_active(Hall1Context.PHASE3_ENTRY)):
                raise self._cmd_error(
                    "BufferFeeder: HALL1 OVERFLOW active — aborting. "
                    "Clear overflow, then retry. (UNLOAD is allowed; "
                    "use OVERFLOW_OK=1 for stable-exit semantics.)")
        # Phase Entry: bei overflow_ok ist STATE_OVERFLOW ein legitimer
        # Vorgaenger-State (das aufrufende Macro hat das vorher
        # abgesichert via Status-Check).
        allowed_states = {STATE_IDLE, STATE_AUTO, STATE_RUNOUT,
                          STATE_LOADING_PUSH}
        if overflow_ok:
            allowed_states.add(STATE_OVERFLOW)
        self._check_phase_entry('LOAD_PHASE3', allowed_states)
        # Clean start: stop any inherited continuous feed and wait for
        # any in-flight manual move to finish before we begin chunk
        # streaming. Prevents residual motion from tacking onto Phase 3.
        # allow_overflow=overflow_ok: bei OVERFLOW_OK=1 darf der Wait
        # nicht am internen _raise_if_locked_out kippen — sonst rueckt
        # die Stable-Logic nie zur Geltung. JAM raised weiterhin.
        self._continuous_feed = False
        self._wait_for_move_done(gcmd, allow_overflow=overflow_ok)
        self._load_phase3_distance = 0.0
        self._load_phase3_max_distance = max_distance
        self._load_phase3_speed = speed
        self._load_phase3_stable_timeout = stable_timeout
        self._load_phase3_overflow_ok = overflow_ok
        self._load_phase3_chunk_distance = chunk_distance
        self._load_phase3_hall_full_since = None
        self._load_phase3_hall_overflow_since = None
        self._load_phase3_hall_full_drop_since = None
        self._load_phase3_hall_overflow_drop_since = None
        # Wenn wir aus STATE_OVERFLOW heraus eintreten (overflow_ok=1),
        # die _overflow_interrupted_*-Felder clearen — sonst wuerde ein
        # spaeteres _exit_overflow versuchen, einen "interrupted" Move
        # zu resumen, der gar nicht mehr passt.
        if overflow_ok:
            self._overflow_interrupted_state = None
            self._overflow_resume_mm = 0.0
            self._overflow_resume_dir = 0
            self._overflow_resume_spd = 0.0
            self._overflow_interrupted_follow = False
        logging.info("buffer_feeder: P3 start threshold=%.1fs overflow_ok=%s "
                     "chunk=%.1f hall1=%s hall2=%s state=%s",
                     stable_timeout, overflow_ok, chunk_distance,
                     self.hall_overflow, self.hall_full, self._state)
        self._enable_stepper()
        self._set_state(STATE_LOADING_PUSH)
        self._start_continuous_motion(+1, speed, self.max_feed_time)
        # Block until the tick-driven state machine exits STATE_LOADING_PUSH.
        # P7-35 fault-overlay: in overlay mode HALL1 sets _fault_overflow
        # without state change, so the overlay flag is an additional exit
        # condition. _exit_overflow clears it; postcheck below raises if
        # HALL1 is still asserted.
        while (self._state == STATE_LOADING_PUSH
               and not (self.use_overflow_overlay and self._fault_overflow)):
            self.reactor.pause(self.reactor.monotonic() + 0.1)
        # Postcheck: JAM bleibt absolut. Bei overflow_ok haben wir den
        # HALL1-Stable-Exit selbst gemacht — sonst alte Lockout-Logik.
        self._raise_if_jam()
        if not overflow_ok:
            self._raise_if_locked_out(gcmd)

    # cmd_BUFFER_UNLOAD_PHASE1 und cmd_BUFFER_UNLOAD_PHASE2 entfernt.
    # P7-20 hat das UNLOAD_FILAMENT-Macro auf SYNC_TO_EXTRUDER umgestellt —
    # Tip-Forming und parallele sync-distance laufen jetzt im Macro selbst
    # via G1 E waehrend BUFFER_SYNC_TO_EXTRUDER aktiv ist.

    cmd_BUFFER_UNLOAD_FILAMENT_help = "UNLOAD_FILAMENT als Python-Workflow mit garantiertem Cleanup"
    def cmd_BUFFER_UNLOAD_FILAMENT(self, gcmd):
        tip_cycles = gcmd.get_int('TIP_CYCLES', 6, minval=0)
        tip_push = gcmd.get_float('TIP_PUSH', 8.0, above=0.)
        tip_pull = gcmd.get_float('TIP_PULL', 14.0, above=0.)
        tip_speed = gcmd.get_float('TIP_SPEED', 20.0, above=0.)
        tip_final_retract = gcmd.get_float('TIP_FINAL_RETRACT', 50.0, above=0.)
        tip_final_speed = gcmd.get_float('TIP_FINAL_SPEED', 50.0, above=0.)
        use_cooling_move = gcmd.get_int('USE_COOLING_MOVE', 1, minval=0, maxval=1)
        cool_temp = gcmd.get_float('COOL_TEMP', 150.0, above=0.)
        cool_temp_max = gcmd.get_float('COOL_TEMP_MAX', cool_temp + 10.0, above=cool_temp)
        sync_dist = gcmd.get_float('SYNC_DIST', self.unload_sync_distance, above=0.)
        fast_spd = gcmd.get_float('FAST_SPD', self.unload_fast_speed, above=0.)
        max_distance = gcmd.get_float('MAX_DISTANCE', self.unload_fast_max, above=0.)
        heat_to = gcmd.get_float('AUTO_HEAT_TARGET', 250.0, above=0.)
        extruder_name = gcmd.get('EXTRUDER', 'extruder')

        temp = self._hotend_temp()
        if temp < self.min_temp:
            self._gcode_run_script_checked(
                "M118 Hotend zu kalt (%d/%d C) - heize automatisch auf %d C\n"
                "M109 S%d"
                % (int(temp), int(self.min_temp), int(heat_to), int(heat_to)),
                from_command=True)

        state_saved = False
        try:
            self._gcode_run_script_checked(
                "SAVE_GCODE_STATE NAME=buffer_feeder_op",
                from_command=True)
            self._macro_state_saved = True
            state_saved = True
            self._gcode_run_script_checked("M83", from_command=True)

            try:
                # kein sync_active-Flag mehr. _unsync_if_synced
                # ist idempotent (early-return wenn _stepper_synced_to
                # is None). Damit greift der Cleanup auch wenn die
                # Exception INNERHALB des sync-Aufrufs raised — vorher
                # haette sync_active=False den finally-Cleanup
                # uebersprungen, obwohl das Sync-Command schon teilweise
                # mutiert hatte.
                self._gcode_run_script_checked(
                    "BUFFER_SYNC_TO_EXTRUDER BUFFER=%s EXTRUDER=%s"
                    % (self.name, extruder_name),
                    from_command=True)

                tip_speed_f = int(tip_speed * 60)
                fast_spd_f = int(fast_spd * 60)
                # Tip-Forming in zwei Phasen, dazwischen optional
                # Cooling-Move (M104 + TEMPERATURE_WAIT). Cooling haertet
                # die Filament-Spitze, damit sie sauber durch die Haupt-
                # extruder-Zaehne (BMG/Sherpa/Orbiter) passt und der
                # Buffer-Stepper das Ende anschliessend frei aus dem
                # Bowden ziehen kann.
                pre_cool_moves = []
                for _ in range(tip_cycles):
                    pre_cool_moves.append("G1 E%g F%d" % (tip_push, tip_speed_f))
                    pre_cool_moves.append("G1 E-%g F%d" % (tip_pull, tip_speed_f))
                if pre_cool_moves:
                    self._gcode_run_script_checked("\n".join(pre_cool_moves),
                                                    from_command=True)

                if use_cooling_move:
                    self._gcode_run_script_checked(
                        "M118 UNLOAD Cooling-Move: heize runter auf %d C\n"
                        "M104 S%d\n"
                        "TEMPERATURE_WAIT SENSOR=%s MAXIMUM=%d"
                        % (int(cool_temp), int(cool_temp), extruder_name, int(cool_temp_max)),
                        from_command=True)

                post_cool_moves = []
                post_cool_moves.append("G1 E-%g F%d" % (tip_final_retract,
                                                       int(tip_final_speed * 60)))
                post_cool_moves.append("G1 E-%g F%d" % (sync_dist, fast_spd_f))
                post_cool_moves.append("M400")
                self._gcode_run_script_checked("\n".join(post_cool_moves),
                                                from_command=True)
            finally:
                self._unsync_if_synced()

            self._gcode_run_script_checked(
                "BUFFER_UNLOAD_PHASE3 BUFFER=%s MAX_DISTANCE=%g SPEED=%g"
                % (self.name, max_distance, self.unload_phase3_speed),
                from_command=True)
        finally:
            if state_saved and self._macro_state_saved:
                self._gcode_run_script_checked(
                    "RESTORE_GCODE_STATE NAME=buffer_feeder_op MOVE=0",
                    from_command=True)
                self._macro_state_saved = False

        self._respond("UNLOAD abgeschlossen (Python workflow)")

    cmd_BUFFER_UNLOAD_PHASE3_help = "UNLOAD Phase 3 — chunked retract until entrance free"
    def cmd_BUFFER_UNLOAD_PHASE3(self, gcmd):
        self._halt_requested = False
        self._raise_if_locked_out(gcmd, direction=-1)
        # OVERFLOW/JAM erlaubt fuer Retract-Recovery.
        self._check_phase_entry('UNLOAD_PHASE3', {
            STATE_IDLE, STATE_AUTO, STATE_RUNOUT, STATE_UNLOADING,
            STATE_OVERFLOW, STATE_JAM,
        })
        max_distance = gcmd.get_float('MAX_DISTANCE', self.unload_fast_max, above=0.)
        speed        = gcmd.get_float('SPEED',        self.unload_phase3_speed, above=0.)
        nominal_chunk = self.max_move_chunk_mm
        # Clean start: cancel any inherited continuous feed and drain
        # any in-flight move so residual motion doesn't join the retract.
        self._continuous_feed = False
        self._wait_for_move_done(gcmd, direction=-1)
        self._set_state(STATE_UNLOADING)
        self._enable_stepper()
        retracted = 0.0
        overshoot = False
        while retracted < max_distance:
            # Abort immediately on HALT. OVERFLOW/JAM duerfen weiterlaufen
            # (UNLOAD ist Recovery, direction=-1).
            self._raise_if_locked_out(gcmd, direction=-1)
            if not self.entrance_detected:
                self._respond("UNLOAD Phase 3: entrance clear after %.0f mm" % retracted)
                break
            # Clip last chunk so MAX_DISTANCE is a HARD cap, not a
            # best-effort ceiling (previously could overshoot by up
            # to one full chunk).
            chunk = min(nominal_chunk, max_distance - retracted)
            if chunk <= 0:
                overshoot = True
                break
            self._submit_move(-chunk, speed)
            retracted += chunk
            # Wait on move-only; state stays UNLOAD_PHASE_3 until we
            # exit the loop. UNLOAD ist Retract → OVERFLOW/JAM erlaubt.
            self._wait_for_move_done(gcmd, direction=-1)
        else:
            overshoot = True
        self._disable_stepper()
        self._set_state(STATE_IDLE)
        if overshoot:
            # Explicit failure — do not let UNLOAD_FILAMENT print
            # "UNLOAD abgeschlossen" after an unsuccessful retract.
            raise self._cmd_error(
                "UNLOAD Phase 3: MAX_DISTANCE %dmm reached without "
                "entrance clear — check buffer / filament path"
                % int(max_distance))
        # UNLOAD ist semantisch der JAM-/OVERFLOW-Recovery-Pfad —
        # bei erfolgreichem Exit auch sticky Lockout-Flags clearen.
        # Sonst raised der naechste LOAD_FILAMENT mit "JAM active" weil
        # _jam_active=True von einem frueheren LOAD_TIMEOUT haengt.
        # _set_state(STATE_IDLE) oben cleart nur _state, nicht die
        # Begleit-Flags. (P7-31 review: nutzt jetzt _clear_recovery_flags
        # konsistent mit den anderen vier Recovery-Pfaden.)
        self._clear_recovery_flags()

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
        lines = [
            "---- BUFFER STATE ----",
            "state              = %s" % self._state,
            "hall_empty (HALL3) = %s" % self.hall_empty,
            "hall_full  (HALL2) = %s" % self.hall_full,
            "hall_overflow(HALL1)= %s" % self.hall_overflow,
            "entrance_detected  = %s" % self.entrance_detected,
            "feed_button        = %s" % self.feed_button_pressed,
            "retract_button     = %s" % self.retract_button_pressed,
            "continuous_feed    = %s dir=%d" % (self._continuous_feed,
                                                self._continuous_feed_direction),
            "pending_remaining  = %.1f mm" % self._pending_remaining_mm,
            "feed_distance_acc  = %.1f mm" % self._feed_distance_accumulator,
            "accumulated total  = %.1f mm" % self._accumulated_feed_distance,
            "commanded_pos      = %.1f mm" % self._commanded_pos,
            "print_running      = %s" % self._print_running,
            "bang_bang_suspended= %s" % self._bang_bang_suspended,
            "auto_off_by_user   = %s" % self._auto_off_by_user,
            "cooldown_deadline  = %s" % (self._cooldown_deadline,),
            "halt_requested     = %s" % self._halt_requested,
            "jam_active         = %s" % self._jam_active,
            "overflow overlay  = active=%s enabled=%s" % (
                self._fault_overflow, self.use_overflow_overlay),
            "runout_follow      = %s ref=%s" % (self._runout_follow_active,
                                                self._runout_filament_ref),
            "runout_recov_pending= %s (RESUME will grip+fill if armed)" % self._runout_recovery_pending,
            "macro_state_saved  = %s (buffer_feeder_op slot consumable)" % self._macro_state_saved,
            "synced_to_extruder = %s" % self._stepper_synced_to,
            "debug_flags        = events=%s metrics=%s" % (
                self.buffer_debug_events, self.buffer_debug_metrics),
            "measure_load       = active=%s feeding=%s dist=%.1f mm" % (
                self._measure_load_active, self._measure_feeding,
                self._measure_load_distance),
            "click_count        = feed=%d retract=%d" % (self._click_count[BUTTON_FEED],
                                                         self._click_count[BUTTON_RETRACT]),
            "---- END STATE ----",
        ]
        gc = self.printer.lookup_object('gcode')
        for line in lines:
            gc.respond_info(line)

    # ---- runtime parameter tuning (Issue #28) ----
    # Hot-swap setter for the five hardware-discovery parameters. All
    # writes are picked up by the next read on the streaming/auto path
    # (no Klipper restart). NO persistence: the operator manually
    # transfers a confirmed value to lll.cfg.
    cmd_BUFFER_SET_help = (
        "Live-tune buffer parameters without restart. All args optional:\n"
        "  CHUNK_MM              flush_callback_chunk_mm (mm,  default 15, lll.cfg 45)\n"
        "  SPEED                 feed_speed              (mm/s, default 30, lll.cfg 70)\n"
        "  INTERRUPT_CHUNK_MM    interrupt_chunk_mm      (mm,  default 9, cap <= MAX_MOVE_CHUNK_MM)\n"
        "  LEAD_TIME             lead_time               (s,   default 0.3, lll.cfg 0.12; warn outside 0.05..1.0)\n"
        "  MAX_MOVE_CHUNK_MM     max_move_chunk_mm       (mm,  default 50)\n"
        "  DEBUG_EVENTS          buffer_debug_events     (0/1, handler trace logs)\n"
        "  DEBUG_METRICS         buffer_debug_metrics    (0/1, per-second metrics)\n"
        "Without args: prints current values. No persistence — copy "
        "the final value into lll.cfg manually."
    )
    def cmd_BUFFER_SET(self, gcmd):
        # All args optional. above=0. ensures only positive values.
        new_chunk      = gcmd.get_float('CHUNK_MM',           None, above=0.)
        new_speed      = gcmd.get_float('SPEED',              None, above=0.)
        new_interrupt  = gcmd.get_float('INTERRUPT_CHUNK_MM', None, above=0.)
        new_lead       = gcmd.get_float('LEAD_TIME',          None, above=0.)
        new_max_move   = gcmd.get_float('MAX_MOVE_CHUNK_MM',  None, above=0.)
        new_debug_events = gcmd.get_int('DEBUG_EVENTS',       None, minval=0, maxval=1)
        new_debug_metrics = gcmd.get_int('DEBUG_METRICS',     None, minval=0, maxval=1)

        gc = self.printer.lookup_object('gcode')
        changed = False

        # Apply MAX_MOVE_CHUNK_MM first so INTERRUPT_CHUNK_MM cap below
        # sees the new ceiling (operator can raise both in one call).
        if new_max_move is not None:
            old = self.max_move_chunk_mm
            self.max_move_chunk_mm = new_max_move
            gc.respond_info("BUFFER_SET: max_move_chunk_mm  %.3f -> %.3f mm"
                            % (old, new_max_move))
            # Existing interrupt-chunk might now violate the new cap;
            # never raised, only lowered (mirrors __init__ cap-on-init).
            if self.interrupt_chunk_mm > self.max_move_chunk_mm:
                old_ic = self.interrupt_chunk_mm
                self.interrupt_chunk_mm = self.max_move_chunk_mm
                gc.respond_info(
                    "BUFFER_SET: interrupt_chunk_mm capped %.3f -> %.3f mm "
                    "(<= max_move_chunk_mm=%.3f)"
                    % (old_ic, self.interrupt_chunk_mm,
                       self.max_move_chunk_mm))
            changed = True

        if new_interrupt is not None:
            old = self.interrupt_chunk_mm
            # Cap at max_move_chunk_mm — mirrors the init-time check at
            # buffer_feeder.py:826. We CAP (not raise) so the
            # operator can issue a single command without micromanaging
            # ordering against MAX_MOVE_CHUNK_MM.
            capped = new_interrupt
            if capped > self.max_move_chunk_mm:
                gc.respond_info(
                    "BUFFER_SET: INTERRUPT_CHUNK_MM=%.3f exceeds "
                    "max_move_chunk_mm=%.3f — capping"
                    % (new_interrupt, self.max_move_chunk_mm))
                capped = self.max_move_chunk_mm
            self.interrupt_chunk_mm = capped
            gc.respond_info("BUFFER_SET: interrupt_chunk_mm  %.3f -> %.3f mm"
                            % (old, capped))
            changed = True

        if new_chunk is not None:
            old = self.flush_callback_chunk_mm
            self.flush_callback_chunk_mm = new_chunk
            gc.respond_info(
                "BUFFER_SET: flush_callback_chunk_mm  %.3f -> %.3f mm"
                % (old, new_chunk))
            changed = True

        if new_speed is not None:
            old = self.feed_speed
            self.feed_speed = new_speed
            gc.respond_info("BUFFER_SET: feed_speed  %.3f -> %.3f mm/s"
                            % (old, new_speed))
            changed = True

        if new_lead is not None:
            old = self.lead_time
            self.lead_time = new_lead
            gc.respond_info("BUFFER_SET: lead_time  %.4f -> %.4f s"
                            % (old, new_lead))
            if new_lead > 1.0 or new_lead < 0.05:
                gc.respond_info(
                    "BUFFER_SET: WARNING lead_time=%.4f outside typical "
                    "hardware range 0.05..1.0 s — proceed with caution"
                    % new_lead)
            changed = True

        if new_debug_events is not None:
            old = self.buffer_debug_events
            self.buffer_debug_events = bool(new_debug_events)
            gc.respond_info("BUFFER_SET: buffer_debug_events  %s -> %s"
                            % (old, self.buffer_debug_events))
            changed = True

        if new_debug_metrics is not None:
            old = self.buffer_debug_metrics
            self.buffer_debug_metrics = bool(new_debug_metrics)
            gc.respond_info("BUFFER_SET: buffer_debug_metrics %s -> %s"
                            % (old, self.buffer_debug_metrics))
            changed = True

        if not changed:
            # No-op: dump current values so the operator can read the
            # live picture without a separate command.
            gc.respond_info("BUFFER_SET: no args — current values:")
            gc.respond_info("  flush_callback_chunk_mm = %.3f mm"
                            % self.flush_callback_chunk_mm)
            gc.respond_info("  feed_speed              = %.3f mm/s"
                            % self.feed_speed)
            gc.respond_info("  interrupt_chunk_mm      = %.3f mm"
                            % self.interrupt_chunk_mm)
            gc.respond_info("  lead_time               = %.4f s"
                            % self.lead_time)
            gc.respond_info("  max_move_chunk_mm       = %.3f mm"
                            % self.max_move_chunk_mm)
            gc.respond_info("  buffer_debug_events     = %s"
                            % self.buffer_debug_events)
            gc.respond_info("  buffer_debug_metrics    = %s"
                            % self.buffer_debug_metrics)

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
        self._gcode_run_script(
            "SAVE_GCODE_STATE NAME=buffer_feeder_op",
            from_command=True)
        self._macro_state_saved = True

    def cmd_BUFFER_RESTORE_MACRO_STATE(self, gcmd):
        """Invoked by the _RESTORE_E_MODE macro on the normal success
        path. Restores and clears the flag so later cleanup paths
        don't re-apply the same stale state."""
        if not self._macro_state_saved:
            # Normal success case: macro saved then restored exactly
            # once. Silent no-op if called without a save (defensive).
            return
        self._gcode_run_script(
            "RESTORE_GCODE_STATE NAME=buffer_feeder_op MOVE=0",
            from_command=True)
        self._macro_state_saved = False

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
            # Config values (exposed so LOAD/UNLOAD macros don't hardcode)
            'feed_speed':               self.feed_speed,
            'manual_speed':             self.manual_speed,
            'burst_speed':              self.burst_speed,
            'load_fast_speed':          self.load_fast_speed,
            'load_slow_speed':          self.load_slow_speed,
            'unload_fast_speed':        self.unload_fast_speed,
            'unload_phase3_speed':      self.unload_phase3_speed,
            'load_fast_distance':       self.load_fast_distance,
            'load_slow_distance':       self.load_slow_distance,
            'load_buffer_max':          self.load_buffer_max,
            'unload_sync_distance':     self.unload_sync_distance,
            'unload_fast_max':          self.unload_fast_max,
            'min_temp':                 self.min_temp,
            'use_fault_overlay':        self.use_fault_overlay,
            'accel':                    self.accel,
            'max_move_chunk_mm':        self.max_move_chunk_mm,
            'flush_callback_chunk_mm':  self.flush_callback_chunk_mm,
            'interrupt_chunk_mm':       self.interrupt_chunk_mm,
            'buffer_debug_events':      self.buffer_debug_events,
            'buffer_debug_metrics':     self.buffer_debug_metrics,
        }


# ---------------------------------------------------------------------------
# Config hook
# ---------------------------------------------------------------------------

def load_config_prefix(config):
    return BufferFeeder(config)
