"""Owner-bound diagnostics; live state and callback hooks stay on the feeder."""
import logging

from ._buffer_common import BUTTON_FEED, BUTTON_RETRACT


class BufferDiagnostics:
    def __init__(self, owner):
        self.owner = owner

    def _debug_event(self, key, message, *args, level=logging.INFO,
                     min_interval=1.0):
        """Rate-limited handler/event tracing controlled by
        buffer_debug_events.

        Intended for incident diagnostics. Emits concise, readable
        breadcrumbs without changing motion logic and can be fully
        disabled in normal operation.
        """
        owner = self.owner
        if not owner.buffer_debug_events:
            return
        now = None
        try:
            now = owner.reactor.monotonic()
        except Exception:
            now = None
        if min_interval and now is not None:
            last = owner._debug_event_last.get(key)
            if last is not None and (now - last) < min_interval:
                return
            owner._debug_event_last[key] = now
        logging.log(level, "buffer_event[%s]: " + message, key, *args)

    def _tick_debug_metrics(self, eventtime):
        # C-cont T10: Diagnostik-Logs (alle 1s wenn buffer_debug_metrics).
        owner = self.owner
        if owner.buffer_debug_metrics:
            if (eventtime - owner._last_metrics_log_time) >= 1.0:
                target_speed = owner._compute_target_feed_speed()
                flow = owner.velocity_tracker.get_volumetric_flow()
                hall1_persist_info = (
                    "%.2fs" % (owner.reactor.monotonic()
                               - owner._hall1_active_since)
                    if owner._hall1_active_since is not None
                    else "off")
                logging.info(
                    "buffer_metrics: state=%s hall=[H3:%s H2:%s H1:%s] "
                    "tracker_vel=%.1fmm/s flow=%.1fmm3/s high_flow=%s "
                    "ready=%s target_speed=%.1fmm/s "
                    "pending_remaining=%.1fmm hall1_persist=%s",
                    owner._state,
                    'on' if owner.hall_empty else 'off',
                    'on' if owner.hall_full else 'off',
                    'on' if owner.hall_overflow else 'off',
                    owner.velocity_tracker.get_velocity(),
                    flow,
                    owner._is_high_flow_active(flow),
                    owner.velocity_tracker.is_ready(),
                    target_speed,
                    owner._pending_remaining_mm,
                    hall1_persist_info)
                owner._last_metrics_log_time = eventtime

    def cmd_BUFFER_STATE_DUMP(self, gcmd):
        owner = self.owner
        lines = [
            "---- BUFFER STATE ----",
            "state              = %s" % owner._state,
            "hall_empty (HALL3) = %s" % owner.hall_empty,
            "hall_full  (HALL2) = %s" % owner.hall_full,
            "hall_overflow(HALL1)= %s" % owner.hall_overflow,
            "entrance_detected  = %s" % owner.entrance_detected,
            "feed_button        = %s" % owner.feed_button_pressed,
            "retract_button     = %s" % owner.retract_button_pressed,
            "continuous_feed    = %s dir=%d" % (owner._continuous_feed,
                                                owner._continuous_feed_direction),
            "pending_remaining  = %.1f mm" % owner._pending_remaining_mm,
            "feed_distance_acc  = %.1f mm" % owner._feed_distance_accumulator,
            "accumulated total  = %.1f mm" % owner._accumulated_feed_distance,
            "commanded_pos      = %.1f mm" % owner._commanded_pos,
            "print_running      = %s" % owner._print_running,
            "bang_bang_suspended= %s" % owner._bang_bang_suspended,
            "auto_off_by_user   = %s" % owner._auto_off_by_user,
            "cooldown_deadline  = %s" % (owner._cooldown_deadline,),
            "halt_requested     = %s" % owner._halt_requested,
            "jam_active         = %s" % owner._jam_active,
            "overflow overlay  = active=%s enabled=%s" % (
                owner._fault_overflow, owner.use_overflow_overlay),
            "runout_follow      = %s ref=%s" % (owner._runout_follow_active,
                                                owner._runout_filament_ref),
            "runout_recov_pending= %s (RESUME will grip+fill if armed)" % owner._runout_recovery_pending,
            "macro_state_saved  = %s (buffer_feeder_op slot consumable)" % owner._macro_state_saved,
            "synced_to_extruder = %s" % owner._stepper_synced_to,
            "debug_flags        = events=%s metrics=%s" % (
                owner.buffer_debug_events, owner.buffer_debug_metrics),
            "post_full_bias_clamp= %s" % owner._post_full_bias_clamp,
            "post_full_h3_since = %s" % (owner._post_full_h3_since,),
            "post_full_recovery = %.3fs" % max(
                0.0, owner._post_full_recovery_until - owner.reactor.monotonic()),
            "benchmark_mode     = active=%s left=%.1fs reason=%s" % (
                owner._benchmark_mode_active(),
                owner._benchmark_mode_remaining(),
                owner._benchmark_mode_reason or '-'),
            "print_phase        = %s seen_extrusion=%s guard_left=%.3fs reason=%s" % (
                owner._refresh_print_phase(owner.reactor.monotonic()),
                owner._print_extrusion_seen,
                owner._critical_action_guard_remaining(owner.reactor.monotonic()),
                (owner._critical_action_guard_reason or '-')),
            "guard_config       = strict_start=%s critical_action=%.3fs conservative=%s" % (
                owner.strict_print_start_guard,
                owner.critical_action_guard_s,
                owner.buffer_conservative_mode),
            "measure_load       = active=%s feeding=%s dist=%.1f mm" % (
                owner._measure_load_active, owner._measure_feeding,
                owner._measure_load_distance),
            "click_count        = feed=%d retract=%d" % (owner._click_count[BUTTON_FEED],
                                                         owner._click_count[BUTTON_RETRACT]),
            "---- END STATE ----",
        ]
        gc = owner.printer.lookup_object('gcode')
        for line in lines:
            gc.respond_info(line)

    def _respond(self, message):
        # Log + console echo. M117 wird hier bewusst NICHT emittiert:
        # _respond wird sowohl aus reactor-event handlers als auch
        # aus gcode command handlers gerufen, und gc.run_script
        # re-acquired die gcode mutex. Aus einem command handler
        # (wo die Mutex bereits gehalten wird) wuerde der Aufruf
        # Klippers ganze gcode pipeline deadlocken.
        owner = self.owner
        logging.info("buffer_feeder: %s", message)
        try:
            gc = owner.printer.lookup_object('gcode')
            gc.respond_info("BufferFeeder: %s" % message)
        except Exception:
            pass
