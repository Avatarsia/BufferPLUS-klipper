"""Idle-watchdog coordination; live state and hooks stay on the feeder."""
import logging

from ._buffer_common import MAX_T0_LOOKAHEAD_S, STATE_AUTO, STATE_IDLE


class IdleWatchdogCoordinator:
    def __init__(self, owner):
        self.owner = owner

    def _tick_idle_watchdog(self, eventtime):
        owner = self.owner
        owner._log_idle_watchdog_blockers(eventtime)
        _print_active, _print_state_known, _p778_override = (
            owner._idle_watchdog_print_activity(eventtime))
        hall_empty_block = (owner.hall_empty
                            and not owner.use_flush_callback_bang_bang)
        # hall_full blockt nur noch Weg-1-Anchors (enabled: ~18mm/h
        # Drift Richtung HALL1, Codex-Verify) und den P7-78-Print-
        # Override (submittet ebenfalls enabled). Bei Weg 2
        # (idle_motor_disable) laeuft der Anchor enable-los — der
        # Treiber ignoriert die Pulse, kein Drift — und NUR ueber
        # diesen Anchor erreicht AUTO den Idle-Disable. Vorher
        # blieb der Stepper nach LOAD (Buffer voll -> hall_full)
        # dauerhaft bestromt (User-Report 2026-07-13: Motor
        # kochend heiss im Standby). Der allererste Weg-2-Anchor
        # kann den noch enabled Motor einmalig 0.05mm bewegen —
        # danach ist er disabled und alle Folge-Anchors sind
        # bewegungslos.
        hall_full_block = (owner.hall_full
                           and (not owner.idle_motor_disable
                                or _p778_override))
        if (owner._state in (STATE_IDLE, STATE_AUTO)
                and not owner._stepper_synced_to
                and not owner._pending_disable
                and not owner._move_in_flight()
                and owner._pending_remaining_mm == 0.0
                and not owner._continuous_feed
                and not hall_empty_block
                and not hall_full_block
                and not _print_active):  # P7-77 A + P7-78 Override
            # _needs_overflow_prime blockt den Watchdog NICHT mehr
            # (Review 2026-07-09 F3): das Flag leakte in IDLE (kein
            # Clear-Pfad ausserhalb AUTO) und sperrte den Anchor
            # dauerhaft aus — Issue-#31-Pathologie. Der Anchor IST
            # der Cursor-Refresh, den die Prime leisten sollte; er
            # konsumiert das Flag unten bei erfolgreichem Queue.
            mcu = owner.stepper.get_mcu()
            mcu_now = mcu.estimated_print_time(
                owner.reactor.monotonic())
            gap_moves = mcu_now - owner._last_move_end_time
            gap_anchors = mcu_now - owner._last_idle_anchor_time
            # Silent-Modus (Option 4, Quellcode-Recherche + Codex
            # 2026-07-14): KEINE Watchdog-Anchor-Moves — auch nicht
            # der P7-78-Override. Ein idle Stepper mit leerer
            # stepcompress-Queue kann beim Background-Flush nicht
            # fehlschlagen, und der erste Step nach beliebig langer
            # Stille laeuft in Mainline automatisch durch den
            # Far-Path (queue_append_far, seit 2017). Erhalten
            # bleibt nur der Idle-Motor-Disable als One-shot
            # (Latch, re-armed in _enable_stepper — sonst wuerde
            # jeder Tick _last_enable_schedule_time fortschieben).
            if (owner.idle_anchor_mode == 'silent'
                    and gap_moves > owner.idle_anchor_gap
                    and not owner._silent_idle_disabled
                    and (owner._state == STATE_IDLE
                         or (owner._state == STATE_AUTO
                             and not _p778_override
                             and _print_state_known
                             and owner.idle_motor_disable))):
                owner._silent_idle_disabled = True
                owner._schedule_stepper_disable()
                logging.info(
                    "buffer_feeder: silent idle-disable "
                    "(state=%s gap=%.1fs, no anchor move)",
                    owner._state.lower(), gap_moves)
            if (owner.idle_anchor_mode != 'silent'
                    and gap_moves > owner.idle_anchor_gap
                    and gap_anchors > owner.idle_anchor_gap):
                owner._submit_idle_watchdog_anchor(
                    mcu_now, gap_moves, _p778_override, _print_state_known)

    def _log_idle_watchdog_blockers(self, eventtime):
        owner = self.owner
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
        if (owner._state in (STATE_IDLE, STATE_AUTO)
                and not owner._stepper_synced_to
                and not owner._pending_disable
                and not owner._move_in_flight()
                and owner._pending_remaining_mm == 0.0):
            _mcu = owner.stepper.get_mcu()
            _mcu_now = _mcu.estimated_print_time(
                owner.reactor.monotonic())
            _gap_moves_diag = _mcu_now - owner._last_move_end_time
            if _gap_moves_diag > owner.idle_anchor_gap * 1.5:
                _blocking = []
                if owner._continuous_feed:
                    _blocking.append("_continuous_feed")
                if owner.hall_empty:
                    _blocking.append("hall_empty")
                if owner.hall_full and not owner.idle_motor_disable:
                    # Bei Weg 2 blockt hall_full nicht mehr (siehe
                    # hall_full_block unten) — nicht als Blocker
                    # loggen.
                    _blocking.append("hall_full")
                # separate watermark for log-rate (not
                # _last_idle_anchor_time — that is only updated when
                # an anchor actually fires; in the blocked path no
                # anchor fires so using it for rate-limiting would
                # spam every tick).
                _last_skip_log = getattr(
                    owner, '_last_watchdog_skip_log_time', 0.0)
                if _blocking and (
                        _mcu_now - _last_skip_log
                        > owner.idle_anchor_gap):
                    logging.debug(
                        "buffer_feeder: watchdog skip "
                        "(state=%s gap=%.1fs > %.1fs threshold) "
                        "blocked by: %s (P7-76 C diagnostic)",
                        owner._state, _gap_moves_diag,
                        owner.idle_anchor_gap,
                        ",".join(_blocking))
                    owner._last_watchdog_skip_log_time = _mcu_now

    def _idle_watchdog_print_activity(self, eventtime):
        owner = self.owner
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
        # _print_state_known: True nur wenn print_stats erfolgreich
        # gelesen wurde. Der AUTO-Idle-Disable (unten) verlangt
        # POSITIVE Bestaetigung dass nicht gedruckt wird — bei
        # fehlendem print_stats oder einer Exception bleibt der
        # Status unbekannt und es wird NICHT disabled (sonst koennte
        # ein Disable mid-print durchrutschen, weil der except-Pfad
        # _print_active=False setzt ohne _p778_override zu markieren).
        _print_state_known = False
        try:
            _ps = owner.printer.lookup_object('print_stats', None)
            if _ps is not None:
                _ps_status = _ps.get_status(eventtime)
                _print_active = (
                    _ps_status.get('state') == 'printing')
                _print_state_known = True
        except Exception:
            _print_active = False
            _print_state_known = False

        # Sekundaere Print-Detektion (Review 2026-07-09 F5):
        # Serial-/OctoPrint-Drucke melden print_stats.state=
        # 'standby' — der Hard-Block wuerde nicht greifen und der
        # Watchdog feuerte forced_t0=None-Anchors mid-print
        # (P7-77-A-Klasse). Juengste Extruder-Bewegung (Stempel aus
        # diesem Tick, Monotonic-Domaene) zaehlt daher ebenfalls
        # als aktiver Druck. Buffer-eigene Moves bewegen den
        # Extruder nicht — kein Selbst-Block.
        if (not _print_active
                and (eventtime - owner._last_extruder_motion_time)
                    < owner.idle_anchor_gap):
            _print_active = True

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
        if _print_active and owner._last_mcu_flush_time > 0.0:
            _mcu_p778 = owner.stepper.get_mcu()
            _mcu_now_p778 = _mcu_p778.estimated_print_time(
                owner.reactor.monotonic())
            _flush_silence = (
                _mcu_now_p778 - owner._last_mcu_flush_time)
            if _flush_silence > owner.idle_anchor_gap:
                logging.info(
                    "buffer_feeder: print-block stale override "
                    "(flush silent for %.1fs > %.1fs threshold, "
                    "P7-78)",
                    _flush_silence, owner.idle_anchor_gap)
                _print_active = False
                _p778_override = True
        return _print_active, _print_state_known, _p778_override

    def _submit_idle_watchdog_anchor(
            self, mcu_now, gap_moves, _p778_override, _print_state_known):
        owner = self.owner
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
        if owner._last_move_end_time > mcu_now:
            logging.debug(
                "buffer_feeder: pre-anchor lme-clamp "
                "(was %.3fs ahead of mcu_now, P7-77 C; "
                "ex-P7-76 D, scope reduced)",
                owner._last_move_end_time - mcu_now)
            owner._last_move_end_time = mcu_now
        _lme_before_anchor = owner._last_move_end_time
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
                    + min(owner.lead_time,
                          MAX_T0_LOOKAHEAD_S))
                owner.sync._submit_anchor_move(
                    forced_t0=_p778_forced_t0)
            elif owner.idle_motor_disable:
                # Weg 2: enable-loser Idle-Anchor — Motor
                # bleibt stromlos, last_step_clock wird
                # trotzdem aufgefrischt. Hardware-Realitaet
                # (User 2026-07-14): die gequeueten Steps
                # re-enablen den Motor via add_active_-
                # callback kurz (Enable-Klack + Mikro-Move,
                # dann wieder Disable). idle_anchor_speed
                # (Default 2 mm/s) macht den Move-Anteil
                # leise; nur die Idle-Watchdog-Anchors —
                # Boot/Sync/P7-78 bleiben bei 10 mm/s.
                owner.sync._submit_anchor_move(
                    skip_enable=True,
                    speed=owner.idle_anchor_speed)
            else:
                # Weg 1 (Default): Motor bleibt in AUTO an,
                # Anchor enabled normal.
                owner.sync._submit_anchor_move(
                    speed=owner.idle_anchor_speed)
            owner._last_idle_anchor_time = mcu_now
            if (owner._needs_overflow_prime
                    and owner._last_move_end_time
                        != _lme_before_anchor):
                # Anchor real gequeued (lme bewegt) — die
                # pendende Overflow-Prime ist damit
                # erledigt; Flag konsumieren, sonst bleibt
                # der Flush-Pfad in AUTO auf dem Prime-
                # Handler haengen (F3, Review 2026-07-09).
                owner._needs_overflow_prime = False
            # Motor stromlos schalten sobald der Anchor-Move
            # gedrained ist (IDLE-Semantik: stopped AND
            # disabled). In IDLE immer.
            #
            # In AUTO ebenfalls — ABER nur wenn wirklich KEIN
            # Druck laeuft. Hintergrund: Nach einem Druck
            # bleibt der Buffer bei eingelegtem Filament in
            # STATE_AUTO (nicht IDLE), und ohne diesen Zweig
            # bliebe der Stepper stundenlang bestromt (hoer-
            # bares Spulenfiepen). Der Watchdog-Anchor haelt
            # den stepcompress-Cursor ueber den 10s-Heartbeat
            # frisch; _disable_stepper setzt _stepcompress_-
            # primed=False, der naechste Submit (naechster
            # Anchor oder erster _on_mcu_flush bei Druckstart)
            # reprimt via set_position(0) → kein Issue-#29-
            # Race.
            #
            # NICHT disablen wenn _p778_override aktiv: dann
            # laeuft echter Druck (Z.1428 flippt _print_active
            # nur lokal auf False, um den Cursor in der HALL2-
            # Hysterese-Totzone zu pflegen). Ein Disable wuerde
            # mit dem zurueckkehrenden bang-bang racen.
            #
            # _print_state_known verlangt POSITIVE Bestaetigung
            # dass nicht gedruckt wird — bei unlesbarem print_-
            # stats (Exception/fehlend) bleibt der Motor lieber
            # bestromt als mid-print abzuschalten. IDLE ist
            # davon unabhaengig (tritt nie waehrend Druck auf).
            #
            # AUTO-Disable per `idle_motor_disable`:
            # - False (Default, Weg 1): Motor bleibt in AUTO an;
            #   StealthChop haelt ihn lautlos. Der Idle-Anchor
            #   enabled normal (no-op, Motor schon an).
            # - True (Weg 2): Motor wird stromlos geschaltet UND
            #   der Idle-Anchor laeuft enable-los (skip_enable,
            #   siehe oben) — kein Enable-Snap, kein Strom,
            #   last_step_clock bleibt trotzdem frisch.
            # IDLE schaltet immer ab (Spec: stopped AND disabled).
            if owner._state == STATE_IDLE or (
                    owner._state == STATE_AUTO
                    and not _p778_override
                    and _print_state_known
                    and owner.idle_motor_disable):
                owner._schedule_stepper_disable()
            logging.info(
                "buffer_feeder: %s anchor fired "
                "(gap=%.1fs, threshold=%.1fs)",
                owner._state.lower(),
                gap_moves, owner.idle_anchor_gap)
        except Exception:
            logging.exception(
                "buffer_feeder: idle/auto anchor failed")
