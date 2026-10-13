"""Chunk planning and queue submission with live feeder state and hooks."""
import logging
import math

from ._buffer_common import STATE_AUTO, STATE_MANUAL_RETRACT
from .buffer_pending import SubmitResult
from .buffer_types import Hall1Context


class StreamingCoordinator:
    def __init__(self, owner):
        self.owner = owner

    def _tick_pending_chunk(self, eventtime):
        """Pending-chunk streaming for long single-shot moves.
        Schedule the next chunk when the current one is within
        half-a-chunk-duration of ending, so chunks abut without a
        visible gap in motion. Abort signals zero out the pending
        counter — already-queued trapezoids drain on the MCU."""
        owner = self.owner
        if (getattr(owner, '_stepper_disable_in_progress', False)
                or getattr(owner, '_global_disable_in_progress', False)):
            return
        if owner._pending_remaining_mm <= 0:
            return
        if (owner._load_endstop_move is not None
                and owner._load_endstop_move.poll(eventtime)):
            return
        if owner._abort_signalled():
            # Retract-Streams (Retract-Burst/UNLOAD-Spillover) sind die
            # OVERFLOW-/JAM-Recovery-Bewegung — nur HALT nullt sie
            # (analog _wait_for_move_done direction=-1). Forward-Streams
            # brechen weiterhin auf jedes Abort-Signal ab.
            if owner._pending_direction > 0 or owner._halt_requested:
                # Codex-Verify Q6b: HALL1-Early-Exit muss auch den Sub-Chunk-Cap
                # zuruecksetzen — sonst leakt der cap (e.g. interrupt_chunk_mm=9)
                # auf den naechsten unrelated _submit_move-Call. T8's
                # target_speed<=0-Branch macht beides; dieser Pfad muss es auch.
                owner.pending_stream.finish()
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
        if (owner._pending_direction > 0
                and owner._state == STATE_AUTO
                and owner.hall_full):
            owner.pending_stream.finish()
            owner._continuous_feed = False
            return
        if (owner._pending_direction < 0
                and owner._state == STATE_MANUAL_RETRACT
                and not owner.entrance_detected):
            owner._halt_motion()
            owner._respond("Retract-Burst gestoppt — Filament am Eingang weg")
            return
        if owner._pending_speed <= 0:
            return
        # C-cont T8: Sub-Chunk-Speed dynamisch aus SpeedModulator fuer
        # AUTO+forward-Streaming. Vorher fest auf _pending_speed
        # (eingefroren beim ersten Submit) — fuehrte zu Speed-Lag wenn
        # HALL-State zwischen Sub-Chunks wechselte (HALL3 -> Zwischen-
        # zone). Legacy-Paths (LOAD/UNLOAD/MANUAL/Retract) behalten den
        # frozen _pending_speed.
        sub_chunk_speed = owner._pending_speed
        # Park-Fill (park_full_on_print_end): fixed-speed stream. The
        # demand modulator below would return 0 after print end (no
        # extruder velocity, dead-zone HALL state) and kill the stream
        # after the first sub-chunk (Codex-Verify 2026-06-11 finding,
        # repro: pending 141.0 -> 0.0 on first tick). HALL2/HALL1
        # aborts above remain fully active for park streams.
        if (owner._state == STATE_AUTO
                and owner._pending_direction > 0
                and not owner._park_full_active):
            modulated = owner._compute_target_feed_speed()
            if modulated <= 0.0:
                # HALL1 active mid-chunk — beende Pending-Stream.
                # (_abort_signalled greift bereits weiter oben, aber
                # diese Branch deckt jeden anderen target=0-Pfad ab.)
                # Session-Latch mit raeumen — analog zum HALL2-Branch
                # oben (Review 2026-07-09 F4: stale _continuous_feed
                # → falscher SUPPLY-JAM + Watchdog-Block).
                owner.pending_stream.finish()
                owner._continuous_feed = False
                return
            sub_chunk_speed = modulated
            owner._continuous_feed_speed = sub_chunk_speed
        # honour the sub-chunk cap if the active stream was
        # opened with one. AUTO+streaming sets cap=interrupt_chunk_mm
        # so HALL-interrupt latency stays bounded; legacy paths
        # (LOAD/UNLOAD/MANUAL) leave _pending_submit_chunk_cap=None and
        # fall back to max_move_chunk_mm exactly as before.
        cap = owner._pending_submit_chunk_cap
        if cap is None or cap > owner.max_move_chunk_mm:
            cap = owner.max_move_chunk_mm
        chunk_duration = cap / sub_chunk_speed
        mcu = owner.stepper.get_mcu()
        now_pt = mcu.estimated_print_time(eventtime)
        gap = owner._last_move_end_time - now_pt
        # Submit next chunk when <= half-a-chunk remains in the
        # currently-queued move, so next trapezoid starts right at
        # the prior one's end_time.
        if gap <= chunk_duration * 0.5:
            chunk = min(owner._pending_remaining_mm, cap)
            # R1: this is the streaming continuation of an
            # already-running burst. Pass streaming=True so the
            # _enable_stepper() and _last_enable_schedule_time floor
            # are skipped — same rationale as the lookahead branch in
            # _on_mcu_flush. _move_in_flight() is implicit here: the
            # gap-check above only fires while the previous trapezoid
            # is still in the future.
            result = owner._submit_single_trapezoid(
                owner._pending_direction * chunk, sub_chunk_speed,
                streaming=True)
            if (owner._load_endstop_move is not None
                    and owner._load_endstop_move.poll(owner.reactor.monotonic())):
                return
            # C-cont T8: keep _pending_speed aligned with the latest
            # modulated speed so the next iteration's chunk_duration
            # math and any consumer of _pending_speed reflects reality.
            if result is SubmitResult.QUEUED:
                owner.pending_stream.advance(chunk, sub_chunk_speed)

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
        owner = self.owner
        target_speed = owner._compute_target_feed_speed()
        if target_speed <= 0.0:
            owner._debug_event(
                'flush_no_demand',
                "no submit target_speed=0 hall1=%s hall2=%s hall3=%s ready=%s",
                owner.hall_overflow, owner.hall_full, owner.hall_empty,
                owner.velocity_tracker.is_ready(), min_interval=2.0)
            if owner.buffer_debug_metrics:
                logging.debug(
                    "buffer_feeder: target_speed=0 - no submit "
                    "(hall1=%s ready=%s)",
                    owner.hall_overflow,
                    owner.velocity_tracker.is_ready())
            if owner._continuous_feed:
                # Demand-0 beendet die Feed-Session (Review 2026-07-09
                # F4). Das stale Flag liess (a) _jam_tick einen
                # stehenden Feeder als "running" werten → falscher
                # SUPPLY-JAM bei M109/Heat-Soak, und (b) blockte das
                # Watchdog-Anchor-Gate dauerhaft nach der ersten
                # Session (Issue-#29-Fenster wieder offen). Der
                # naechste Demand>0-Flush re-armt Session-Counter und
                # Flag regulaer.
                owner._continuous_feed = False
                owner._continuous_feed_direction = 0
            return

        if eventtime is None:
            eventtime = owner.reactor.monotonic()
        allowed, phase = owner._auto_submit_permission(eventtime)
        if not allowed:
            vel_ready, extruder_vel = owner._get_tracker_velocity()
            event_key = 'flush_skip_idle' if phase == 'inactive' \
                else 'flush_skip_permission'
            owner._debug_event(
                event_key,
                "skip phase=%s target=%.3f print_state=%s seen=%s "
                "vel_ready=%s vel=%.3f guard_left=%.3f guard_reason=%s",
                phase, target_speed, owner._get_print_stats_state(eventtime),
                owner._print_extrusion_seen, vel_ready, extruder_vel,
                owner._critical_action_guard_remaining(eventtime),
                (owner._critical_action_guard_reason or '-'),
                min_interval=1.0)
            if owner.buffer_debug_metrics:
                logging.debug(
                    "buffer_feeder: permission-suppress auto-stream "
                    "(phase=%s target=%.1f guard_left=%.3f)",
                    phase, target_speed,
                    owner._critical_action_guard_remaining(eventtime))
            return

        current_end = (owner._current_move['end_time']
                       if owner._current_move is not None
                       else owner._last_move_end_time)
        move_active = owner._flush_move_in_flight(step_gen_time)
        if move_active:
            remaining = current_end - step_gen_time
            if remaining > owner.lead_time:
                owner._debug_event(
                    'flush_skip_inflight',
                    "skip in-flight remaining=%.3f lead=%.3f",
                    remaining, owner.lead_time, min_interval=1.0)
                return  # too early, no new chunk yet
        if owner._pending_remaining_mm > 0:
            owner._debug_event(
                'flush_skip_pending',
                "skip pending_remaining_mm=%.3f",
                owner._pending_remaining_mm, min_interval=1.0)
            return  # sub-chunk pipeline already running

        submit_chunk_mm = owner._effective_interrupt_chunk_mm(eventtime)
        if move_active and submit_chunk_mm < owner.interrupt_chunk_mm:
            remaining = current_end - step_gen_time
            if remaining > 0.0:
                owner._debug_event(
                    'flush_skip_recovery_drain',
                    "skip short recovery chunk remaining=%.3f lead=%.3f "
                    "chunk=%.3f",
                    remaining, owner.lead_time, submit_chunk_mm,
                    min_interval=0.5)
                return

        if move_active:
            anchor = current_end
        else:
            anchor = step_gen_time + owner.lead_time

        if not owner._continuous_feed:
            # Real inactive -> active transition: reset safety counters.
            owner._feed_distance_accumulator = 0.0
            owner._feed_deadline_time = None
            owner._continuous_feed = True
            owner._continuous_feed_direction = 1
        owner._continuous_feed_speed = target_speed
        owner._debug_event(
            'flush_submit',
            "submit speed=%.3f anchor=%.3f move_active=%s chunk=%.3f",
            target_speed, anchor, move_active, submit_chunk_mm,
            min_interval=1.0)

        # Pipeline-Cap: one sub-chunk in-flight at a time so a HALL1/
        # HALL2 transition can stop the pipeline at the next flush
        # callback instead of letting an already-queued chunk overrun
        # the buffer arm.
        owner._submit_move(
            submit_chunk_mm,
            target_speed,
            forced_t0=anchor,
            streaming=move_active,
            submit_chunk_cap=submit_chunk_mm)

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
        owner = self.owner
        if (getattr(owner, '_stepper_disable_in_progress', False)
                or getattr(owner, '_global_disable_in_progress', False)):
            return
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
        if owner._stepper_synced_to is not None:
            return
        # OVERFLOW: nur Forward-Submits ablehnen. Retract (signed_distance < 0)
        # ist die einzige Recovery-Bewegung, die einen überfüllten Buffer
        # entlasten kann — sonst sitzt der User in der Sackgasse.
        # Ausnahme: LOAD_PHASE_3 mit OVERFLOW_OK=1 darf weiterfeeden
        # waehrend HALL1 aktiv — sonst koennte das Stable-Tracking nie
        # die Schwelle erreichen, weil der Arm bei jedem Reject zurueck-
        # faellt und HALL1 deaktiviert. _load_phase3_tick beendet die
        # Phase sauber sobald HALL1 stable lange genug ist.
        if owner._is_hall1_active(Hall1Context.SUBMIT_MOVE) and signed_distance > 0:
            logging.warning("buffer_feeder: forward move rejected — HALL1 active "
                            "(distance=%.1f speed=%.1f)", signed_distance, speed)
            owner._continuous_feed = False
            owner.pending_stream.finish()
            return

        # Cancel any previously-streaming sequence before starting new.
        owner.pending_stream.finish()

        # Ensure the first chunk starts no earlier than the last enable/disable
        # toggle scheduled on the MCU. Without this guard, a move submitted
        # right after an enable (e.g. LOAD_PHASE1 after IDLE→disable) would
        # send a trapezoid with t0 < enable_time → MCU "Timer too close".
        # R1: only apply this floor when NOT streaming. In the
        # streaming-lookahead path the stepper is already enabled and
        # _last_enable_schedule_time is stale — pushing _last_move_end_-
        # time forward would break the abuttend-anchor.
        if not streaming:
            owner._last_move_end_time = max(owner._last_move_end_time,
                                           owner._last_enable_schedule_time)

        distance_abs = abs(signed_distance)
        direction = 1.0 if signed_distance > 0 else -1.0

        # hardware-safe sub-chunking. submit_chunk_cap (typ.
        # interrupt_chunk_mm=9) limits the first trapezoid to a size
        # that lets HALL2 abort within one sub-chunk's duration. The
        # rest streams via _pending_remaining_mm with per-sub-chunk
        # HALL re-checks in _tick_pending_chunk. Falls back to
        # max_move_chunk_mm when caller does not request sub-chunking.
        chunk_cap = submit_chunk_cap if submit_chunk_cap is not None \
            else owner.max_move_chunk_mm
        if chunk_cap > owner.max_move_chunk_mm:
            chunk_cap = owner.max_move_chunk_mm
        first_chunk = min(distance_abs, chunk_cap)
        result = owner._submit_single_trapezoid(
            direction * first_chunk, speed, forced_t0=forced_t0,
            streaming=streaming, skip_enable=skip_enable)
        if (owner._load_endstop_move is not None
                and owner._load_endstop_move.poll(owner.reactor.monotonic())):
            return
        if result is SubmitResult.SKIPPED and not retain_skipped_distance:
            return
        queued_mm = first_chunk if result is SubmitResult.QUEUED else 0.
        owner.pending_stream.start(
            distance_abs - queued_mm, direction, speed, chunk_cap)

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
        owner = self.owner
        if (getattr(owner, '_stepper_disable_in_progress', False)
                or getattr(owner, '_global_disable_in_progress', False)):
            return SubmitResult.SKIPPED
        # Innermost defense-in-depth sync guard. Upstream paths already
        # guard, but the dangerous side-effects (flush_step_generation
        # + set_position(0) mid-print) live here, so this final gate
        # makes "no own-trapq submit while synced" robust against any
        # future caller.
        if owner._stepper_synced_to is not None:
            return SubmitResult.SKIPPED

        mcu = owner.stepper.get_mcu()
        mcu_now = mcu.estimated_print_time(owner.reactor.monotonic())
        if forced_t0 is not None:
            owner._sanitize_forced_t0_floors(mcu_now)
        gap = mcu_now - owner._last_move_end_time

        was_primed = owner._stepcompress_primed
        need_reprime = owner._reprime_stepcompress_if_needed(forced_t0, gap)

        # Skip motor-enable in streaming lookahead — previous chunk
        # already enabled the motor and pushed _last_enable_schedule_-
        # time forward. skip_enable additionally suppresses enable for
        # the de-energized idle-watchdog anchor (Weg 2).
        if not streaming and not skip_enable:
            owner._enable_stepper()

        # mcu_now FRISCH lesen (Codex 2026-07-14): der Reprime kann via
        # toolhead.flush_step_generation() bei Host-Last 100ms+
        # blockieren — ein vor dem Flush gelesenes mcu_now waere als
        # t0-Floor bereits abgelaufen (Timer too close).
        mcu_now = mcu.estimated_print_time(owner.reactor.monotonic())

        t0 = owner._compute_t0_anchor(
            forced_t0, mcu_now, was_primed, need_reprime, streaming)
        if t0 is None:
            return SubmitResult.SKIPPED  # far-future anchor

        if (owner._load_endstop_move is not None
                and owner._load_endstop_move.poll(owner.reactor.monotonic())):
            return SubmitResult.SKIPPED
        owner._append_trapezoid_and_record(t0, signed_distance, speed)
        return SubmitResult.QUEUED

    def _append_trapezoid_and_record(self, t0, signed_distance, speed):
        """Compute the trapezoid profile, append to trapq, update state."""
        owner = self.owner
        distance = abs(signed_distance)
        direction = 1.0 if signed_distance > 0 else -1.0

        accel = owner.accel
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

        owner.trapq_append(owner.trapq, t0,
                          accel_time, cruise_time, decel_time,
                          owner._commanded_pos, 0., 0.,
                          direction, 0., 0.,
                          0., cruise_v, accel)

        end_time = t0 + accel_time + cruise_time + decel_time
        if owner._load_endstop_move is not None:
            owner._load_endstop_move.record(
                t0, distance, cruise_v, accel_time, cruise_time, accel)
        owner._last_move_end_time = end_time
        owner._commanded_pos += direction * distance

        owner._current_move = {
            'end_time': end_time,
            'direction': direction,
            'distance': distance,
            'speed': cruise_v,
        }
        owner._feed_distance_accumulator += distance
        owner._accumulated_feed_distance += distance
        if owner._measure_load_active and direction > 0:
            owner._measure_load_distance += distance

        owner.motion_queuing.note_mcu_movequeue_activity(end_time)
