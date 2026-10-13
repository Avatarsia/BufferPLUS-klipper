"""Print-session lifecycle; live state and callback hooks stay on the feeder."""
from ._buffer_common import STATE_AUTO, STATE_IDLE, STATE_JAM
from .buffer_types import Hall1Context


class PrintSessionCoordinator:
    def __init__(self, owner):
        self.owner = owner

    def _on_idle_printing(self, *args):
        owner = self.owner
        # Klipper fires idle_timeout:printing during MCU init even without
        # an active print (print_stats state = 'standby'). Guard against
        # this boot artifact so _print_running is only armed for real prints.
        ps_state = ''
        try:
            ps = owner.printer.lookup_object('print_stats', None)
            if ps is not None:
                ps_state = ps.get_status(owner.reactor.monotonic()).get(
                    'state', '')
        except Exception:
            pass
        if ps_state in ('standby', 'complete', 'cancelled', 'error'):
            # Post-Print-Flaps (Review 2026-07-09): der buffereigene
            # Idle-Anchor kippt idle_timeout regelmaessig — nach
            # 'complete'/'cancelled' wuerde jeder Flap _print_running
            # re-armen (Runout-PAUSE auf fertigem Druck, Spontan-Grip
            # nach CANCEL via _runout_recovery_pending).
            return
        # Re-arm the one-shot park-to-full trigger on real print
        # activity (new print start / RESUME). Deliberately NOT reset
        # in the non-paused :ready branch — that edge never fires
        # between a RESUME and the next print end, so a reset there
        # would never run during an active print (PR #49 lesson).
        if ps_state in ('printing', 'paused'):
            owner._park_full_attempted = False
        owner._print_running = True
        now = owner.reactor.monotonic()
        owner._print_extrusion_seen = False
        owner._arm_critical_action_guard('print_start', eventtime=now)
        owner._set_print_phase('guarded', now, reason='idle_timeout_printing')
        # RESUME / print-start: bang-bang resumes.
        owner._bang_bang_suspended = False
        # Documented RESUME-clears-JAM path (spec §10, README §Jam).
        # When Klipper transitions back to 'printing' (typically after
        # a RESUME following our PAUSE-on-jam), drop the JAM lockout
        # so the feeder resumes AUTO. HALL1 is still respected — if
        # physical overflow is still present, we fall into OVERFLOW.
        jam_recovery = owner._state == STATE_JAM or owner._jam_active
        if jam_recovery:
            owner._respond("RESUME: clearing JAM lockout")
            owner._clear_recovery_flags()
            owner._prepare_post_jam_recovery()
            # If the jam interrupted a LOAD/UNLOAD macro mid-flight,
            # the macro's SAVE_GCODE_STATE is still pending. Restore
            # it now so the user's E-mode isn't stuck on M83 after
            # RESUME. Parity with BUFFER_CLEAR_JAM's recovery path.
            owner._try_restore_gcode_state()
            if owner.hall_overflow:
                # Cannot resume while overflow physically present.
                owner._enter_overflow()
            elif owner.entrance_detected:
                owner._enable_stepper()
                owner._set_state(STATE_AUTO)
            else:
                owner._set_state(STATE_IDLE)
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
        if (owner._runout_recovery_pending
                and owner._state == STATE_IDLE
                and owner.entrance_detected
                and not owner._auto_off_by_user
                and not owner._halt_requested
                and not owner.hall_overflow):
            owner._runout_recovery_pending = False
            owner._respond("RESUME after runout-reinsert — starting grip + fill")
            owner._start_initial_grip(owner.reactor.monotonic())
            return

        # Auto-engage Bang-bang beim Print-Start.
        #
        # Wir wollen, dass der Buffer im Druck mitlaeuft, ohne dass der
        # User das Boot-autostart-Feature pflegen oder BUFFER_AUTO_ON in
        # PRINT_START selbst eintragen muss. Bedingung: Filament am
        # Eingang, State ist IDLE, kein Operator-Lockout aktiv.
        # Konfigurierbar via auto_engage_on_print_start (default True).
        if (owner.auto_engage_on_print_start
                and owner._state == STATE_IDLE
                and owner.entrance_detected
                and not owner._auto_off_by_user
                and not owner._halt_requested
                and not owner._is_hall1_active(Hall1Context.AUTO_ON)):
            owner._respond("Print start — engaging AUTO")
            owner._enable_stepper()
            owner._set_state(STATE_AUTO)

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
        owner = self.owner
        if not owner._bang_bang_suspended:
            return False
        try:
            ps = owner.printer.lookup_object('print_stats')
            ps_state = ps.get_status(eventtime).get('state')
        except Exception:
            return False
        if ps_state in ('printing', 'paused'):
            return False
        # Print is no longer pause-recoverable (complete / cancelled /
        # error / standby). Clear the stale lock so next entrance-
        # insert / AUTO_ON_IF_READY proceeds.
        owner._bang_bang_suspended = False
        # Pause-Meldungs-Latch mitraeumen (PR-#49-Review): der
        # PAUSE->CANCEL-Pfad sieht kein weiteres ready-Event und je
        # nach Anchor-Gating (hall_full, standby via SDCARD_RESET_FILE)
        # auch keinen nicht-paused _refresh_print_phase-Tick mehr —
        # sonst bliebe die erste Pause des naechsten Drucks stumm.
        owner._pause_msg_shown = False
        owner._respond("Stale bang-bang-suspend cleared "
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
        owner = self.owner
        if eventtime is None:
            eventtime = owner.reactor.monotonic()
        try:
            ps = owner.printer.lookup_object('print_stats', None)
            if ps is None:
                return False
            return ps.get_status(eventtime).get('state') == 'printing'
        except Exception:
            return False

    def _get_print_stats_state(self, eventtime=None):
        owner = self.owner
        if eventtime is None:
            eventtime = owner.reactor.monotonic()
        try:
            ps = owner.printer.lookup_object('print_stats', None)
            if ps is None:
                return ''
            return ps.get_status(eventtime).get('state', '')
        except Exception:
            return ''

    def _get_tracker_velocity(self):
        owner = self.owner
        ready = owner.velocity_tracker.is_ready()
        velocity = owner.velocity_tracker.get_velocity() if ready else 0.0
        return ready, velocity

    def _arm_critical_action_guard(self, reason, duration=None, eventtime=None):
        owner = self.owner
        if duration is None:
            duration = owner.critical_action_guard_s
        if eventtime is None:
            eventtime = owner.reactor.monotonic()
        duration = max(duration, 0.0)
        if owner.buffer_conservative_mode:
            duration = max(duration, 0.75)
        new_until = eventtime + duration
        if new_until > owner._critical_action_guard_until:
            owner._critical_action_guard_until = new_until
        owner._critical_action_guard_reason = reason
        owner._debug_event(
            'guard_arm',
            "guard armed reason=%s duration=%.3f until=%.3f",
            reason, duration, owner._critical_action_guard_until,
            min_interval=0.0)

    def _critical_action_guard_remaining(self, eventtime=None):
        owner = self.owner
        if eventtime is None:
            eventtime = owner.reactor.monotonic()
        return max(0.0, owner._critical_action_guard_until - eventtime)

    def _set_print_phase(self, phase, eventtime=None, reason=None):
        owner = self.owner
        if eventtime is None:
            eventtime = owner.reactor.monotonic()
        if phase == owner._print_phase:
            return phase
        owner._print_phase = phase
        owner._print_phase_since = eventtime
        owner._debug_event(
            'print_phase',
            "phase=%s reason=%s print_state=%s",
            phase, (reason or '-'), owner._get_print_stats_state(eventtime),
            min_interval=0.0)
        return phase

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
        owner = self.owner
        if ps_state != 'paused':
            owner._pause_msg_shown = False
        if ps_state in ('printing', 'paused'):
            # Laufender/pausierter Druck: Print-ended-Latch freigeben,
            # damit das Ende DIESES Drucks wieder gemeldet wird.
            # 'complete'/'standby' resetten bewusst NICHT — die Post-
            # Print-Flaps lesen genau diese States und wuerden den
            # Print-ended-Spam reaktivieren.
            owner._print_end_msg_shown = False

    def _refresh_print_phase(self, eventtime=None):
        owner = self.owner
        if eventtime is None:
            eventtime = owner.reactor.monotonic()
        ps_state = owner._get_print_stats_state(eventtime)
        vel_ready, extruder_vel = owner._get_tracker_velocity()
        active_extrusion = vel_ready and extruder_vel > 1e-6

        if ps_state == 'printing' and active_extrusion:
            owner._print_extrusion_seen = True

        # Meldungs-Latch-Wartung am Phase-Refresh (Flush-Pfad mit
        # Feed-Demand + get_status-Poll) — Haupt-Garantie liegt im
        # _main_tick, siehe _maintain_msg_latches-Docstring.
        owner._maintain_msg_latches(ps_state)

        if ps_state == 'paused':
            return owner._set_print_phase(
                'paused', eventtime, reason='print_stats_paused')

        if ps_state != 'printing':
            # Phase 'manual' (Review/User-Request 2026-07-14): aktive
            # Extruder-Vorwaertsbewegung ausserhalb eines Drucks
            # (Mainsail-Extrude, ps='standby'/'complete') erlaubt das
            # Flush-Feeding — sonst laeuft der Buffer leer und der
            # Extruder blockiert nach ~30mm Arm-Weg. Retracts triggern
            # nicht (velocity_tracker clampt sie). Aktiver Critical-
            # Action-Guard (SYNC/UNSYNC/JAM-Exit-Fenster) blockt; der
            # Guard-Reset unten laeuft in diesem Fall bewusst NICHT.
            if (owner.feed_on_manual_extrusion
                    and active_extrusion
                    and not owner._bang_bang_suspended):
                if owner._critical_action_guard_until > eventtime:
                    return owner._set_print_phase(
                        'guarded', eventtime,
                        reason=owner._critical_action_guard_reason
                        or 'critical_action')
                return owner._set_print_phase(
                    'manual', eventtime, reason='manual_extrusion')
            owner._print_extrusion_seen = False
            owner._critical_action_guard_until = 0.0
            owner._critical_action_guard_reason = ""
            return owner._set_print_phase(
                'inactive', eventtime, reason='print_stats_inactive')

        if owner._critical_action_guard_until > eventtime:
            return owner._set_print_phase(
                'guarded', eventtime,
                reason=owner._critical_action_guard_reason or 'critical_action')

        if (owner.strict_print_start_guard
                and not owner._print_extrusion_seen
                and not active_extrusion):
            return owner._set_print_phase(
                'starting', eventtime, reason='awaiting_extrusion')

        owner._critical_action_guard_reason = ""
        return owner._set_print_phase(
            'active', eventtime,
            reason=('extrusion_active' if active_extrusion else 'print_running'))

    def _auto_submit_permission(self, eventtime=None):
        owner = self.owner
        phase = owner._refresh_print_phase(eventtime)
        return phase in ('active', 'manual'), phase

    def _on_idle_ready(self, *args):
        owner = self.owner
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
        if not owner._startup_grace_done:
            owner._print_running = False
            return
        now = owner.reactor.monotonic()
        if owner._print_running:
            ps_state = None
            try:
                ps = owner.printer.lookup_object('print_stats')
                ps_state = ps.get_status(now).get('state')
            except Exception:
                pass
            if ps_state == 'paused':
                # Real PAUSE — RESUME is expected, suspend bang-bang
                # so a queued G1 E in the resumed file doesn't fire
                # an unexpected feed before the print actually resumes.
                owner._bang_bang_suspended = True
                owner._print_extrusion_seen = False
                owner._set_print_phase('paused', now, reason='idle_timeout_ready')
                if owner._continuous_feed:
                    owner._continuous_feed = False
                    owner._halt_motion()
                # Meldung nur EINMAL pro Pause (Spam-Fix 2026-06-10,
                # Hardware klippy.log: 564x dieselbe Zeile in einer
                # Pause). Waehrend der Pause feuert idle_timeout:ready
                # alle ~10s erneut, weil der Buffer-eigene Idle-Anchor-
                # Move (idle_anchor_gap, noetig gegen stepcompress-Clock-
                # Drift) ueber toolhead:sync_print_time Klippers
                # idle_timeout printing->ready kippt. Der Anchor laesst
                # sich nicht abstellen, und _on_idle_printing gegen
                # 'paused' zu guarden waere unsicher (echtes RESUME
                # feuert idle_timeout:printing ebenfalls bei print_stats
                # =='paused', weil note_start deferred im work_handler
                # laeuft → wuerde den Resume-Trigger mit-unterdruecken).
                # Daher Dedupe nur auf Meldungs-Ebene; die Suspend-Logik
                # oben bleibt idempotent pro Zyklus. Latch-RESET sitzt
                # in _refresh_print_phase (ps_state != 'paused' am
                # Flush-/Statuspoll-Tick) — NICHT im else-Zweig unten,
                # der waehrend eines laufenden Drucks nie erreicht wird
                # (PR-#49-Review Must-fix 1) — plus PAUSE->CANCEL-
                # Heilung in _clear_stale_suspend_if_print_inactive.
                if not owner._pause_msg_shown:
                    owner._respond(
                        "Print paused — bang-bang suspended until RESUME")
                    owner._pause_msg_shown = True
            else:
                # Nicht-paused ready (Druckende complete/standby oder
                # Anchor-Flap nach Druckende): Pause-Latch defensiv
                # zuruecksetzen (Haupt-Reset siehe _refresh_print_phase).
                owner._pause_msg_shown = False
                owner._set_benchmark_mode(False, reason='print_ready', notify=False)
                owner._print_extrusion_seen = False
                owner._critical_action_guard_until = 0.0
                owner._critical_action_guard_reason = ""
                owner._set_print_phase('inactive', now, reason='idle_timeout_ready')
                # Reset stale continuous-feed session state after a
                # normal print end. Otherwise the next print can see
                # _continuous_feed=True even though no submit occurred
                # yet and falsely arm SUPPLY_JAM dwell tracking.
                owner._continuous_feed = False
                owner._continuous_feed_direction = 0
                # Print ended normally (state=complete/standby/None).
                # Buffer stays available for manual workflow + reinsert
                # auto-grip. _bang_bang_suspended stays whatever it
                # was (operator may have set it explicitly via
                # BUFFER_AUTO_OFF; we don't override).
                #
                # Meldung gelatcht (PR-#49-Review Should-fix): bei
                # state='complete'/'cancelled' greift der nur-'standby'-
                # Fruehausstieg in _on_idle_printing nicht — derselbe
                # Anchor-Flap wie im Pause-Fall wuerde die Zeile sonst
                # alle ~10s wiederholen, solange der Drucker so steht
                # (z.B. wenn park_full_on_print_end nicht greift).
                # Latch-Reset in _refresh_print_phase bei printing/paused.
                if not owner._print_end_msg_shown:
                    owner._respond("Print ended — buffer ready for next "
                                  "filament change or print")
                    owner._print_end_msg_shown = True
                # Park-Hook bleibt ausserhalb des Meldungs-Latches —
                # er hat sein eigenes Once-Gate (_park_full_attempted).
                owner._maybe_park_full_on_print_end(now, ps_state)
        owner._print_running = False

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
        owner = self.owner
        if not owner.park_full_on_print_end:
            return
        if ps_state not in ('complete', 'cancelled'):
            return
        if owner._park_full_attempted:
            return
        if (owner._state not in (STATE_IDLE, STATE_AUTO)
                or owner._stepper_synced_to is not None
                or owner._move_in_flight()
                or owner._pending_remaining_mm > 0.0
                or not owner.entrance_detected
                or owner.hall_full
                or owner.hall_overflow
                or owner._jam_active
                or owner._auto_off_by_user
                or owner._halt_requested):
            return
        # PAUSE → CANCEL leaves a stale _bang_bang_suspended (no
        # healing :ready event). This is a designated lazy-heal
        # decision point — but heal only when the park is otherwise
        # ready to run, so a suspend on a non-park-able feeder stays
        # untouched (contract pinned by test_idle_ready_preserves_
        # prior_suspended_flag_on_print_end). Operator-set suspends
        # are already excluded via _auto_off_by_user above.
        if owner._bang_bang_suspended:
            owner._clear_stale_suspend_if_print_inactive(eventtime)
        if owner._bang_bang_suspended:
            return
        owner._park_full_attempted = True
        owner._respond("Print end — parking buffer at full (HALL2), "
                      "idle anchor stays quiet")
        if owner._state == STATE_IDLE:
            owner._enable_stepper()
            owner._set_state(STATE_AUTO)
        owner._submit_move(
            owner.park_full_max_mm, owner.feed_speed,
            submit_chunk_cap=owner._effective_interrupt_chunk_mm(eventtime))
        # AFTER _submit_move — its session reset clears the flag. Only
        # arm it when a pending continuation actually exists; a park
        # distance <= chunk cap queues a single trapezoid and would
        # otherwise leak the flag past the move's end (Codex-Verify R2).
        owner._park_full_active = owner._pending_remaining_mm > 0.0
