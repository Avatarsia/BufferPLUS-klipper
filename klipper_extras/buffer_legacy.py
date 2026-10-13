"""Legacy distance/phase workflows; state and callback hooks stay on the feeder."""
import logging
from dataclasses import dataclass

from ._buffer_common import (
    STABLE_DROP_GRACE, STATE_AUTO, STATE_IDLE, STATE_INIT, STATE_JAM,
    STATE_LOADING_PULL, STATE_LOADING_PUSH, STATE_OVERFLOW,
    STATE_RUNOUT, STATE_UNLOADING,
)
from .buffer_cleanup import CleanupPolicy
from .buffer_sensors import LoadEndstopMonitor
from .buffer_types import Hall1Context


@dataclass(frozen=True)
class Phase1Options:
    distance: float
    speed: float


def _parse_phase1_options(owner, gcmd):
    distance = gcmd.get_float('DISTANCE', owner.load_fast_distance, above=0.)
    speed    = gcmd.get_float('SPEED',    owner.load_fast_speed,    above=0.)
    return Phase1Options(
        distance=distance,
        speed=speed,
    )


@dataclass(frozen=True)
class Phase3Options:
    max_distance: float
    speed: float
    stable_timeout: float
    overflow_ok: bool
    chunk_distance: float


def _parse_phase3_options(owner, gcmd):
    max_distance = gcmd.get_float('MAX_DISTANCE', owner.load_buffer_max, above=0.)
    speed        = gcmd.get_float('SPEED',        owner.feed_speed,      above=0.)
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
    return Phase3Options(
        max_distance=max_distance,
        speed=speed,
        stable_timeout=stable_timeout,
        overflow_ok=overflow_ok,
        chunk_distance=chunk_distance,
    )


@dataclass(frozen=True)
class LegacyUnloadOptions:
    tip_cycles: int
    tip_push: float
    tip_pull: float
    tip_speed: float
    tip_final_retract: float
    tip_final_speed: float
    use_cooling_move: int
    cool_temp: float
    cool_temp_max: float
    sync_dist: float
    fast_spd: float
    max_distance: float
    heat_to: float
    temp_window: float
    extruder_name: str


def _parse_legacy_unload_options(owner, gcmd):
    tip_cycles = gcmd.get_int('TIP_CYCLES', 6, minval=0)
    tip_push = gcmd.get_float('TIP_PUSH', 8.0, above=0.)
    tip_pull = gcmd.get_float('TIP_PULL', 14.0, above=0.)
    tip_speed = gcmd.get_float('TIP_SPEED', 20.0, above=0.)
    tip_final_retract = gcmd.get_float('TIP_FINAL_RETRACT', 50.0, above=0.)
    tip_final_speed = gcmd.get_float('TIP_FINAL_SPEED', 50.0, above=0.)
    use_cooling_move = gcmd.get_int('USE_COOLING_MOVE', 1, minval=0, maxval=1)
    # COOL_TEMP-Default 170 (nicht 150): die post_cool_moves sind
    # zwingend extruder-getriebene `G1 E`-Retracts, die Klipper unter
    # min_extrude_temp (Mainline-Default 170 C) sperrt. Ein cool_temp
    # < min_extrude_temp lies den Cooling-Move zwar laufen, blockierte
    # danach aber den Retract -> UNLOAD bricht stumm ab (Issue #48).
    # Bei eigenem min_extrude_temp > 170 muss COOL_TEMP entsprechend
    # hochgesetzt werden.
    cool_temp = gcmd.get_float('COOL_TEMP', 170.0, above=0.)
    cool_temp_max = gcmd.get_float('COOL_TEMP_MAX', cool_temp + 10.0, above=cool_temp)
    sync_dist = gcmd.get_float('SYNC_DIST', owner.unload_sync_distance, above=0.)
    fast_spd = gcmd.get_float('FAST_SPD', owner.unload_fast_speed, above=0.)
    max_distance = gcmd.get_float('MAX_DISTANCE', owner.unload_fast_max, above=0.)
    heat_to = gcmd.get_float('AUTO_HEAT_TARGET', 250.0, above=0.)
    temp_window = gcmd.get_float('TEMP_WINDOW', 5.0, minval=0.)
    extruder_name = gcmd.get('EXTRUDER', 'extruder')
    return LegacyUnloadOptions(
        tip_cycles=tip_cycles,
        tip_push=tip_push,
        tip_pull=tip_pull,
        tip_speed=tip_speed,
        tip_final_retract=tip_final_retract,
        tip_final_speed=tip_final_speed,
        use_cooling_move=use_cooling_move,
        cool_temp=cool_temp,
        cool_temp_max=cool_temp_max,
        sync_dist=sync_dist,
        fast_spd=fast_spd,
        max_distance=max_distance,
        heat_to=heat_to,
        temp_window=temp_window,
        extruder_name=extruder_name,
    )



class LegacyFilamentWorkflow:
    def __init__(self, owner):
        self.owner = owner

    def cmd_BUFFER_LOAD_PHASE1(self, gcmd):
        owner = self.owner
        owner._halt_requested = False    # ack any stale console HALT
        if (owner._load_endstop_sensor is not None
                and owner._load_endstop_present()):
            # Repeated LOAD may start with both endstop and HALL1 active.
            # Skip without submitting motion or releasing the HALL1 lockout.
            owner._raise_if_jam()
            owner._check_phase_entry('LOAD_PHASE1', {
                STATE_IDLE, STATE_AUTO, STATE_RUNOUT, STATE_LOADING_PULL,
                STATE_OVERFLOW,
            })
            owner._halt_motion()
            owner.pending_stream.discard_overflow_resume(
                clear_interrupted_state=True)
            owner._wait_for_move_drain_allowing_lockout()
            owner._respond('LOAD: endstop already active - skip fast load')
            return
        owner._raise_if_locked_out(gcmd)
        owner._check_phase_entry('LOAD_PHASE1', {
            STATE_IDLE, STATE_AUTO, STATE_RUNOUT, STATE_LOADING_PULL,
        })
        options = _parse_phase1_options(owner, gcmd)
        distance = options.distance
        speed = options.speed
        # Stop any inherited bang-bang / manual dauerfeed and drain
        # any in-flight chunk so residual motion doesn't extend Phase 1.
        owner._continuous_feed = False
        owner._wait_for_move_done(gcmd)
        if owner._load_endstop_sensor is not None:
            owner._load_to_endstop(gcmd, distance, speed)
            return
        owner._set_state(STATE_LOADING_PULL)
        owner._enable_stepper()
        owner._submit_move(+distance, speed)
        # Blocking: wait for move done, but pause-and-resume on HALL1 overflow
        # instead of aborting — _exit_overflow restores pending state so
        # streaming continues naturally. BUFFER_WAIT_IDLE would deadlock
        # because it also waits for state != busy-phase.
        try:
            owner._wait_for_move_done_resume_on_overflow(gcmd)
        except Exception:
            # Release the phase state on error so it doesn't stay sticky.
            # Nur den EIGENEN Phase-State loesen — JAM/OVERFLOW, die ein
            # Trigger mid-wait gesetzt hat, nicht mit IDLE stampfen
            # (Review 2026-07-09: state=IDLE + _jam_active=True sperrte
            # BUFFER_CLEAR_JAM aus).
            if owner._state == STATE_LOADING_PULL:
                owner._set_state(STATE_IDLE)
            raise
        owner._set_state(STATE_IDLE)

    def _load_endstop_present(self):
        owner = self.owner
        try:
            status = owner._load_endstop_sensor.get_status(owner.reactor.monotonic())
            if not status['enabled']:
                raise owner._cmd_error('load endstop sensor disabled')
            return status['filament_detected']
        except Exception as exc:
            raise owner._cmd_error('load endstop sensor unavailable: %s' % exc)

    def _load_to_endstop(self, gcmd, distance, speed, minimum_ratio=0.9,
                         overflow_timeout=0.):
        owner = self.owner
        if owner._load_endstop_present():
            owner._respond('LOAD: endstop already active - skip fast load')
            return 0.0
        monitor = LoadEndstopMonitor(owner, owner._load_endstop_sensor, distance,
                                     minimum_ratio=minimum_ratio,
                                     overflow_timeout=overflow_timeout)
        owner._set_state(STATE_LOADING_PULL)
        owner._load_endstop_move = monitor
        try:
            # Cap first and resumed chunks. Only the macro sensor workflow
            # permits a bounded HALL1 pause; HALT/JAM still abort immediately.
            owner._submit_move(monitor.maximum, speed,
                              submit_chunk_cap=min(3.0, owner.interrupt_chunk_mm))
            while True:
                if overflow_timeout:
                    owner._raise_if_locked_out(gcmd, direction=-1)
                    owner._raise_if_jam()
                    if owner.hall_overflow and monitor.overflow_since is None:
                        owner._enter_overflow()
                    if not owner.hall_overflow and monitor.overflow_since is not None:
                        owner._exit_overflow()
                    monitor.poll(owner.reactor.monotonic())
                    if monitor.error is not None:
                        raise owner._cmd_error(monitor.error)
                    if monitor.overflow_since is not None:
                        owner.reactor.pause(owner.reactor.monotonic() + 0.01)
                        continue
                    if monitor.resume_needed:
                        if owner._move_in_flight():
                            owner.reactor.pause(owner.reactor.monotonic() + 0.01)
                            continue
                        monitor.resume_needed = False
                        remaining = monitor.remaining_distance()
                        if monitor.detected_distance is None and remaining > 0:
                            owner._stepcompress_primed = False
                            owner._needs_overflow_prime = False
                            owner._enable_stepper()
                            owner._set_state(STATE_LOADING_PULL)
                            owner._respond('LOAD: HALL1 frei — Schnellladen wird fortgesetzt')
                            owner._submit_move(remaining, speed,
                                              submit_chunk_cap=min(3., owner.interrupt_chunk_mm))
                owner._raise_if_locked_out(gcmd)
                monitor.poll(owner.reactor.monotonic())
                if not owner._move_in_flight() and owner._pending_remaining_mm <= 0:
                    break
                owner.reactor.pause(owner.reactor.monotonic() + 0.01)
            if monitor.error is not None:
                raise owner._cmd_error(monitor.error)
            if monitor.detected_distance is None:
                raise owner._cmd_error(
                    'load endstop not reached within %.2f mm' % monitor.maximum)
            owner._respond('LOAD: endstop reached at %.2f mm (expected %.2f..%.2f)' % (
                monitor.detected_distance, monitor.minimum, monitor.maximum))
            mcu_now = owner.stepper.get_mcu().estimated_print_time(
                owner.reactor.monotonic())
            return max(0.0, monitor.distance_at(mcu_now) - monitor.detected_distance)
        except Exception:
            owner._halt_motion()
            # An aborted sensor load must never resume on a later HALL1-clear.
            owner.pending_stream.discard_overflow_resume(
                clear_interrupted_state=True)
            raise
        finally:
            owner._load_endstop_move = None
            if owner._state == STATE_LOADING_PULL:
                owner._set_state(STATE_IDLE)

    def cmd_BUFFER_LOAD_PHASE3(self, gcmd):
        owner = self.owner
        owner._halt_requested = False
        # Parameter ZUERST parsen — overflow_ok beeinflusst den Lockout-
        # Check (P7-8/9). Wenn der Standard _raise_if_locked_out vorher
        # liefe, wuerde HALL1-aktiv sofort raisen, bevor wir den
        # OVERFLOW_OK=1 Modus auswerten koennen.
        options = _parse_phase3_options(owner, gcmd)
        max_distance = options.max_distance
        speed = options.speed
        stable_timeout = options.stable_timeout
        overflow_ok = options.overflow_ok
        chunk_distance = options.chunk_distance
        # Lockout-Check: JAM ist immer absolut. OVERFLOW nur wenn nicht
        # OVERFLOW_OK gesetzt — sonst handhabt _load_phase3_tick den
        # HALL1-Stable-Exit selbst.
        owner._raise_if_jam()
        if not overflow_ok:
            if (owner._state == STATE_OVERFLOW
                    or owner._is_hall1_active(Hall1Context.PHASE3_ENTRY)):
                raise owner._cmd_error(
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
        owner._check_phase_entry('LOAD_PHASE3', allowed_states)
        # Clean start: stop any inherited continuous feed and wait for
        # any in-flight manual move to finish before we begin chunk
        # streaming. Prevents residual motion from tacking onto Phase 3.
        # allow_overflow=overflow_ok: bei OVERFLOW_OK=1 darf der Wait
        # nicht am internen _raise_if_locked_out kippen — sonst rueckt
        # die Stable-Logic nie zur Geltung. JAM raised weiterhin.
        owner._continuous_feed = False
        owner._wait_for_move_done(gcmd, allow_overflow=overflow_ok)
        owner._load_phase3_distance = 0.0
        owner._load_phase3_max_distance = max_distance
        owner._load_phase3_speed = speed
        owner._load_phase3_stable_timeout = stable_timeout
        owner._load_phase3_overflow_ok = overflow_ok
        owner._load_phase3_chunk_distance = chunk_distance
        owner._load_phase3_hall_full_since = None
        owner._load_phase3_hall_overflow_since = None
        owner._load_phase3_hall_full_drop_since = None
        owner._load_phase3_hall_overflow_drop_since = None
        # Wenn wir aus STATE_OVERFLOW heraus eintreten (overflow_ok=1),
        # die _overflow_interrupted_*-Felder clearen — sonst wuerde ein
        # spaeteres _exit_overflow versuchen, einen "interrupted" Move
        # zu resumen, der gar nicht mehr passt.
        if overflow_ok:
            owner.pending_stream.discard_overflow_resume(
                clear_interrupted_state=True, clear_motion_details=True,
                clear_follow=True)
            # Stale Overlay-Flag mit raeumen (Review 2026-07-09): ein
            # aus AUTO geerbtes _fault_overflow=True liesse die
            # while-Schleife unten nach 0 Iterationen mit Silent-
            # Success terminieren, waehrend der Tick asynchron im
            # Hintergrund weiterarbeitet. OVERFLOW_OK=1 toleriert
            # HALL1 explizit — der Stable-Exit uebernimmt.
            owner._fault_overflow = False
        logging.info("buffer_feeder: P3 start threshold=%.1fs overflow_ok=%s "
                     "chunk=%.1f hall1=%s hall2=%s state=%s",
                     stable_timeout, overflow_ok, chunk_distance,
                     owner.hall_overflow, owner.hall_full, owner._state)
        owner._enable_stepper()
        owner._set_state(STATE_LOADING_PUSH)
        owner._start_continuous_motion(+1, speed, owner.max_feed_time)
        # Block until the tick-driven state machine exits STATE_LOADING_PUSH.
        # P7-35 fault-overlay: in overlay mode HALL1 sets _fault_overflow
        # without state change, so the overlay flag is an additional exit
        # condition. _exit_overflow clears it; postcheck below raises if
        # HALL1 is still asserted.
        while (owner._state == STATE_LOADING_PUSH
               and not (owner.use_overflow_overlay and owner._fault_overflow)):
            owner.reactor.pause(owner.reactor.monotonic() + 0.1)
        # Overlay-Abort: der Loop terminierte via _fault_overflow bei
        # unveraendertem _state=LOAD_PHASE_3. Vor dem Raise in den
        # Legacy-Lockout-State konvertieren — sonst bleibt der Tick-
        # getriebene Phase-State ohne Kommando-Owner zurueck und
        # _load_phase3_tick fuettert nach HALL1-Fall autonom weiter
        # (Zombie-Feed, Review 2026-07-09).
        if (owner.use_overflow_overlay and owner._fault_overflow
                and owner._state == STATE_LOADING_PUSH):
            owner._set_state(STATE_OVERFLOW)
        # Postcheck: HALT und JAM bleiben absolut — auch bei
        # overflow_ok (Review 2026-07-09: BUFFER_HALT wurde im
        # OVERFLOW_OK=1-Pfad verschluckt, das LOAD-Macro lief weiter).
        # Bei overflow_ok haben wir den HALL1-Stable-Exit selbst
        # gemacht — sonst alte Lockout-Logik.
        if owner._halt_requested:
            owner._halt_requested = False
            raise owner._cmd_error(
                "BufferFeeder: HALT requested — aborting workflow")
        owner._raise_if_jam()
        if not overflow_ok:
            owner._raise_if_locked_out(gcmd)

    def _load_phase3_tick(self, eventtime):
        owner = self.owner
        threshold = owner._load_phase3_stable_timeout
        # HALL2 (full) Stabilitaets-Tracking mit Drop-Toleranz:
        # kurze False-Edges (<STABLE_DROP_GRACE) lassen die Stable-Uhr
        # weiterlaufen — der Bowden-Spring drueckt den Arm zurueck, der
        # Stepper foerdert weiter, der Arm geht wieder hoch. Hard-Reset
        # nur wenn der Sensor laenger als die Grace komplett aus bleibt.
        if owner.hall_full:
            owner._load_phase3_hall_full_drop_since = None
            if owner._load_phase3_hall_full_since is None:
                owner._load_phase3_hall_full_since = eventtime
            full_dwell = eventtime - owner._load_phase3_hall_full_since
            if full_dwell >= threshold:
                if threshold > 0:
                    msg = ("LOAD Phase 3: HALL2 stable %.1fs, buffer full"
                           % full_dwell)
                else:
                    msg = "LOAD Phase 3: HALL2 reached, buffer full"
                owner._exit_phase3_stable(set_grace=False, respond_text=msg)
                return
        elif owner._load_phase3_hall_full_since is not None:
            # Sensor gerade abgefallen — Grace-Window starten/checken.
            if owner._load_phase3_hall_full_drop_since is None:
                owner._load_phase3_hall_full_drop_since = eventtime
            elif (eventtime - owner._load_phase3_hall_full_drop_since
                  >= STABLE_DROP_GRACE):
                owner._load_phase3_hall_full_since = None
                owner._load_phase3_hall_full_drop_since = None
        # HALL1 (overflow) Stabilitaets-Tracking — nur wenn OVERFLOW_OK=1
        # gesetzt wurde. Sonst ist der HALL1-Pfad weiterhin via
        # _main_tick → _enter_overflow abgewickelt: legacy-Pfad
        # state=OVERFLOW + raise im cmd_BUFFER_LOAD_PHASE3-Postcheck;
        # use_overflow_overlay-Pfad belaesst state=LOAD_PHASE_3 und setzt
        # nur _fault_overflow=True, raise dann im selben Postcheck via
        # fault_overflow-Flag.
        if owner._load_phase3_overflow_ok:
            if owner.hall_overflow:
                owner._load_phase3_hall_overflow_drop_since = None
                if owner._load_phase3_hall_overflow_since is None:
                    owner._load_phase3_hall_overflow_since = eventtime
                overflow_dwell = eventtime - owner._load_phase3_hall_overflow_since
                if overflow_dwell >= threshold:
                    msg = ("LOAD Phase 3: HALL1 stable %.1fs, "
                           "buffer overfilled (treating as full)"
                           % overflow_dwell)
                    # set_grace=True: bounce-suppression — _main_tick
                    # would otherwise re-trigger _enter_overflow on the
                    # next cycle since HALL1 stays asserted.
                    owner._exit_phase3_stable(set_grace=True, respond_text=msg)
                    return
            elif owner._load_phase3_hall_overflow_since is not None:
                # Same drop-tolerance pattern wie bei HALL2.
                if owner._load_phase3_hall_overflow_drop_since is None:
                    owner._load_phase3_hall_overflow_drop_since = eventtime
                elif (eventtime - owner._load_phase3_hall_overflow_drop_since
                      >= STABLE_DROP_GRACE):
                    owner._load_phase3_hall_overflow_since = None
                    owner._load_phase3_hall_overflow_drop_since = None
        if owner._load_phase3_distance >= owner._load_phase3_max_distance:
            owner._continuous_feed = False
            owner._halt_motion()
            # Route through _trigger_jam so the blocking LOAD_PHASE3
            # command's post-loop _raise_if_locked_out raises and
            # aborts the LOAD_FILAMENT macro instead of letting it
            # print "LOAD abgeschlossen".
            owner._trigger_jam(
                "LOAD_TIMEOUT",
                "LOAD Phase 3: max_distance %dmm reached without HALL2 — check sensor/buffer"
                % int(owner._load_phase3_max_distance))
            return
        if not owner._move_in_flight():
            # P7-35 fault-overlay: pause move submission while overlay
            # flag is set. _enter_overflow already halted current motion;
            # re-submitting here would immediately re-saturate HALL1.
            if owner.use_overflow_overlay and owner._fault_overflow:
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
            if owner.hall_overflow:
                return
            # Clip chunk so the per-call MAX_DISTANCE is a hard cap.
            remaining = owner._load_phase3_max_distance - owner._load_phase3_distance
            chunk = min(owner._load_phase3_chunk_distance, remaining)
            if chunk > 0:
                owner._submit_move(chunk, owner._load_phase3_speed)
                owner._load_phase3_distance += chunk

    def _exit_phase3_stable(self, *, set_grace, respond_text):
        """Common exit-sequence when HALL2 (buffer full) or HALL1 (with
        OVERFLOW_OK=1) has been stable for the configured dwell.

        Halts streaming, clears all four Phase 3 trackers, optionally
        arms _post_load_overflow_grace (P7-46 bounce-suppression for
        HALL1-stable exit), and chooses STATE_AUTO when entrance is
        present + operator hasn't issued AUTO_OFF/HALT, else IDLE
        (P7-49: AUTO is the natural post-LOAD state — bang-bang
        re-fills on next extrusion regardless of print_running)."""
        owner = self.owner
        owner._continuous_feed = False
        owner._halt_motion()
        owner._load_phase3_hall_full_since = None
        owner._load_phase3_hall_overflow_since = None
        owner._load_phase3_hall_full_drop_since = None
        owner._load_phase3_hall_overflow_drop_since = None
        owner._respond(respond_text)
        if set_grace:
            owner._post_load_overflow_grace = True
        if (owner.entrance_detected
                and not owner._auto_off_by_user
                and not owner._halt_requested):
            owner._set_state(STATE_AUTO)
        else:
            owner._set_state(STATE_IDLE)

    def cmd_BUFFER_UNLOAD_FILAMENT(self, gcmd):
        owner = self.owner
        if owner._load_endstop_sensor is not None:
            return owner.toolhead_sensor.unload(gcmd)
        # Entry-Guard (Review 2026-07-09): vorher ohne jeden State-
        # Check — ein UNLOAD waehrend INITIAL_GRIP/LOAD swappte den
        # Trapq mitten im Grip und das nested BUFFER_UNLOAD_PHASE3
        # raiste erst NACH Tip-Forming + Final-Retract (Filamentende
        # undefiniert im Bowden). OVERFLOW/JAM bleiben erlaubt —
        # UNLOAD ist deren Recovery-Pfad.
        # Codex-Review 2026-07-13: waehrend der Boot-Grace ist das
        # Sensorbild nicht settled und die Safety-Logik suspendiert —
        # keine Sync-/Extruder-/Buffer-Moves starten.
        if not owner._startup_grace_done:
            raise owner._cmd_error(
                "BufferFeeder: startup grace not finished — sensors "
                "settling, retry in a moment")
        # STATE_INIT (nach Grace nur transient) ist kein Busy-State —
        # der Guard zielt auf GRIP/LOAD/MANUAL.
        owner._check_phase_entry('UNLOAD_FILAMENT', {
            STATE_INIT, STATE_IDLE, STATE_AUTO, STATE_RUNOUT,
            STATE_UNLOADING, STATE_OVERFLOW, STATE_JAM,
        })
        options = _parse_legacy_unload_options(owner, gcmd)
        tip_cycles = options.tip_cycles
        tip_push = options.tip_push
        tip_pull = options.tip_pull
        tip_speed = options.tip_speed
        tip_final_retract = options.tip_final_retract
        tip_final_speed = options.tip_final_speed
        use_cooling_move = options.use_cooling_move
        cool_temp = options.cool_temp
        cool_temp_max = options.cool_temp_max
        sync_dist = options.sync_dist
        fast_spd = options.fast_spd
        max_distance = options.max_distance
        heat_to = options.heat_to
        temp_window = options.temp_window
        extruder_name = options.extruder_name

        temp = owner._hotend_temp()
        if temp < owner.min_temp:
            # M104 + TEMPERATURE_WAIT MINIMUM statt M109 (User-Request
            # 2026-07-13): M109 wartet bis exakt eingeregelt (inkl.
            # Runter-Warten bei Overshoot) — hier reicht "warm genug".
            # Weiterlauf sobald Ziel - TEMP_WINDOW erreicht ist; Clamp
            # auf min_temp, damit die G1-E-Moves nicht unter der
            # Extrude-Schwelle starten. Der Heater regelt waehrenddessen
            # weiter auf das volle Ziel.
            wait_min = max(owner.min_temp, heat_to - temp_window)
            owner._gcode_run_script_checked(
                "M118 Hotend zu kalt (%d/%d C) - heize automatisch auf %d C "
                "(weiter ab %d C)\n"
                "M104 S%d\n"
                "TEMPERATURE_WAIT SENSOR=%s MINIMUM=%d"
                % (int(temp), int(owner.min_temp), int(heat_to),
                   int(wait_min), int(heat_to), extruder_name,
                   int(wait_min)),
                from_command=True)

        with owner.cleanup.gcode_state_scope(policy=CleanupPolicy.STRICT):
            with owner.cleanup.sync_scope(
                    lambda: owner._gcode_run_script_checked(
                        "BUFFER_SYNC_TO_EXTRUDER BUFFER=%s EXTRUDER=%s"
                        % (owner.name, extruder_name), from_command=True),
                    policy=CleanupPolicy.STRICT):
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
                    owner._gcode_run_script_checked("\n".join(pre_cool_moves),
                                                    from_command=True)

                if use_cooling_move:
                    owner._gcode_run_script_checked(
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
                owner._gcode_run_script_checked("\n".join(post_cool_moves),
                                                from_command=True)

            owner._gcode_run_script_checked(
                "BUFFER_UNLOAD_PHASE3 BUFFER=%s MAX_DISTANCE=%g SPEED=%g"
                % (owner.name, max_distance, owner.unload_phase3_speed),
                from_command=True)

        owner._respond("UNLOAD abgeschlossen (Python workflow)")

    def cmd_BUFFER_UNLOAD_PHASE3(self, gcmd):
        owner = self.owner
        owner._halt_requested = False
        owner._raise_if_locked_out(gcmd, direction=-1)
        # OVERFLOW/JAM erlaubt fuer Retract-Recovery.
        owner._check_phase_entry('UNLOAD_PHASE3', {
            STATE_IDLE, STATE_AUTO, STATE_RUNOUT, STATE_UNLOADING,
            STATE_OVERFLOW, STATE_JAM,
        })
        max_distance = gcmd.get_float('MAX_DISTANCE', owner.unload_fast_max, above=0.)
        speed        = gcmd.get_float('SPEED',        owner.unload_phase3_speed, above=0.)
        nominal_chunk = owner.max_move_chunk_mm
        # Clean start: cancel any inherited continuous feed and drain
        # any in-flight move so residual motion doesn't join the retract.
        owner._continuous_feed = False
        owner._wait_for_move_done(gcmd, direction=-1)
        owner._set_state(STATE_UNLOADING)
        owner._enable_stepper()
        retracted = 0.0
        overshoot = False
        while retracted < max_distance:
            # Abort immediately on HALT. OVERFLOW/JAM duerfen weiterlaufen
            # (UNLOAD ist Recovery, direction=-1).
            owner._raise_if_locked_out(gcmd, direction=-1)
            if not owner.entrance_detected:
                owner._respond("UNLOAD Phase 3: entrance clear after %.0f mm" % retracted)
                break
            # Clip last chunk so MAX_DISTANCE is a HARD cap, not a
            # best-effort ceiling (previously could overshoot by up
            # to one full chunk).
            chunk = min(nominal_chunk, max_distance - retracted)
            if chunk <= 0:
                overshoot = True
                break
            owner._submit_move(-chunk, speed)
            retracted += chunk
            # Wait on move-only; state stays UNLOAD_PHASE_3 until we
            # exit the loop. UNLOAD ist Retract → OVERFLOW/JAM erlaubt.
            owner._wait_for_move_done(gcmd, direction=-1)
        else:
            overshoot = True
        owner._disable_stepper()
        if overshoot:
            # Explicit failure — do not let UNLOAD_FILAMENT print
            # "UNLOAD abgeschlossen" after an unsuccessful retract.
            # JAM/OVERFLOW aus einem Mid-Loop-Trigger nicht mit IDLE
            # stampfen (Review 2026-07-09) — nur den eigenen
            # Phase-State loesen.
            if owner._state == STATE_UNLOADING:
                owner._set_state(STATE_IDLE)
            raise owner._cmd_error(
                "UNLOAD Phase 3: MAX_DISTANCE %dmm reached without "
                "entrance clear — check buffer / filament path"
                % int(max_distance))
        owner._set_state(STATE_IDLE)
        # UNLOAD ist semantisch der JAM-/OVERFLOW-Recovery-Pfad —
        # bei erfolgreichem Exit auch sticky Lockout-Flags clearen.
        # Sonst raised der naechste LOAD_FILAMENT mit "JAM active" weil
        # _jam_active=True von einem frueheren LOAD_TIMEOUT haengt.
        # _set_state(STATE_IDLE) oben cleart nur _state, nicht die
        # Begleit-Flags. (P7-31 review: nutzt jetzt _clear_recovery_flags
        # konsistent mit den anderen vier Recovery-Pfaden.)
        owner._clear_recovery_flags()
