# buffer_fault.py — Fault/Overflow/Jam state-machine for the buffer.
#
# Sub-module of buffer_feeder; kein Klipper-load_config-Eintrypoint.
# State (jam-flags, overflow-resume) lives on the owner BufferFeeder;
# this class holds only logic.

import logging

from ._buffer_common import (
    BUSY_PHASE_STATES,
    STATE_AUTO, STATE_INITIAL_GRIP, STATE_JAM, STATE_LOADING_PULL,
    STATE_LOADING_PUSH, STATE_MANUAL_RETRACT, STATE_OVERFLOW,
    STATE_UNLOADING,
)
from .buffer_types import Hall1Context


class FaultManager:
    def __init__(self, owner):
        self.owner = owner
        self.printer = owner.printer
        self.reactor = owner.reactor

    def is_hall1_active(self, context):
        context = Hall1Context.coerce(context)
        owner = self.owner
        if not owner.hall_overflow:
            return False

        phase3_overflow_ok = (owner._state == STATE_LOADING_PUSH
                              and owner._load_phase3_overflow_ok)
        if context is Hall1Context.SENSOR_CALLBACK:
            if phase3_overflow_ok:
                return False
            if owner._state in (STATE_UNLOADING, STATE_MANUAL_RETRACT):
                return False
            if owner._stepper_synced_to is not None:
                return False
            return True
        if context is Hall1Context.MAIN_TICK:
            if owner._state in (STATE_OVERFLOW, STATE_MANUAL_RETRACT,
                                STATE_UNLOADING):
                return False
            if phase3_overflow_ok:
                return False
            if owner._stepper_synced_to is not None:
                return False
            if owner.use_overflow_overlay and owner._fault_overflow:
                return False
            # Post-LOAD HALL1-grace: after Phase 3 exit via stable HALL1
            # the buffer is legitimately full — main_tick must not bounce
            # back to STATE_OVERFLOW. Cleared when HALL1 actually falls
            # (sensor_callback path) or via operator cleanup.
            if owner._post_load_overflow_grace:
                return False
            return True
        if context is Hall1Context.SUBMIT_MOVE:
            return not phase3_overflow_ok
        if context in (Hall1Context.AUTO_ON, Hall1Context.PHASE3_ENTRY):
            return True
        raise ValueError("Unknown HALL1 context: %s" % (context.value,))

    def clear_recovery_flags(self):
        self.owner._jam_active = False
        self.owner._hall2_start_time = None
        self.owner._hall3_start_time = None
        self.owner._hall3_drop_since = None

    def resume_after_overflow(self):
        owner = self.owner
        # Refuse to promote out of OVERFLOW while SYNC is still active.
        # _exit_overflow already short-circuits, but direct callers
        # (fault-overlay branch, future re-entry) could land here with
        # the sync binding intact. Promotion to STATE_AUTO/STATE_LOAD_-
        # PHASE_1/STATE_INITIAL_GRIP would let bang-bang or
        # _submit_move queue moves to own_trapq while the stepper is on
        # extruder_trapq → moves go live at next unsync, corrupting the
        # stepcompress cursor.
        if owner._stepper_synced_to is not None:
            return
        interrupted = owner._overflow_interrupted_state
        owner._overflow_interrupted_state = None

        if (interrupted == STATE_INITIAL_GRIP
                and owner._overflow_interrupted_follow):
            owner._overflow_interrupted_follow = False
            if owner._overflow_resume_mm > 0:
                owner._grip_follow_active = True
                owner._enable_stepper()
                owner._set_state(STATE_INITIAL_GRIP)
                owner._submit_move(
                    owner._overflow_resume_dir * owner._overflow_resume_mm,
                    owner._overflow_resume_spd)
                owner._overflow_resume_mm = 0.0
                return
            owner._overflow_resume_mm = 0.0
            owner._maybe_auto_load()
            return

        if interrupted == STATE_LOADING_PULL and owner._overflow_resume_mm > 0:
            # Force-Reprime auf OVERFLOW-Recovery (Issue #29). Hardware-
            # Crash 2026-05-19 klippy.log Z.7023 ``stepcompress o=0 i=0
            # c=19 a=0`` beim 3. von 3 rapiden HALL1-OVERFLOW-Zyklen in
            # LOAD_PHASE_1.
            #
            # Wurzel: Der vorherige Pending-Stream-Setup laesst
            # _tick_pending_chunk den ersten Resume-Submit mit streaming=
            # True ausfuehren. Bei primed=True ueberspringt
            # _reprime_stepcompress_if_needed sowohl flush_step_generation
            # als auch set_position. Klipper's step_gen_time-Lookahead-
            # Window pushpt last_step_clock auf ~mcu_now+250ms; t0 baut
            # aus dem von halt_motion geclampten lme (~mcu_now+lead) und
            # landet im Generator-Lookahead -> Zero-Interval-Steps ->
            # Crash.
            #
            # Fix: ``_stepcompress_primed = False`` vor dem Submit
            # forciert den Reprime-Pfad. ``_submit_move(forced_t0=None)``
            # laesst _reprime_stepcompress_if_needed flush_step_-
            # generation + set_position(0) feuern -> Stepcompress-Cursor
            # sauber zurueckgesetzt -> t0 landet garantiert NACH
            # last_step_clock. Repliziert den bewaehrten Initial-LOAD_-
            # PHASE_1-Pfad.
            #
            # ``_needs_overflow_prime`` wird explicit geclear't weil wir
            # die Prime-Operation jetzt selbst durchfuehren (statt auf
            # _handle_overflow_prime_via_flush in STATE_AUTO zu warten,
            # der in LOAD_PHASE_1 nie feuert).
            resume_mm = owner._overflow_resume_mm
            resume_dir = owner._overflow_resume_dir
            resume_spd = owner._overflow_resume_spd
            owner._overflow_resume_mm = 0.0
            owner._stepcompress_primed = False
            owner._needs_overflow_prime = False
            owner._enable_stepper()
            owner._set_state(STATE_LOADING_PULL)
            owner._submit_move(resume_dir * resume_mm, resume_spd)
            return

        # In overflow-overlay mode the cmd_BUFFER_LOAD_PHASE3 while-loop is
        # still spinning — keep _state=LOAD_PHASE_3 so the loop continues
        # feeding instead of falling through to STATE_AUTO and silently
        # returning success on an aborted phase 3.
        if (interrupted == STATE_LOADING_PUSH
                and owner.use_overflow_overlay
                and owner._state == STATE_LOADING_PUSH):
            owner._overflow_resume_mm = 0.0
            owner._enable_stepper()
            return

        owner._overflow_resume_mm = 0.0
        if (owner.entrance_detected
                and not owner._auto_off_by_user
                and not owner._bang_bang_suspended
                and not owner._halt_requested):
            # Defensive watchdog-gate-reset at OVERFLOW→AUTO recovery.
            # If _continuous_feed got stuck during OVERFLOW cycling
            # (rapid HALL1-flicker race between _enter_overflow and
            # _on_mcu_flush / _bang_bang_tick), the _main_tick watchdog
            # would not fire for 56s+ even while AUTO has quiescent
            # phases. _enter_overflow + _halt_motion + _set_state(IDLE)
            # should already clear all three flags — this reset is
            # defense-in-depth against unknown race paths. No-op when
            # the flags are already clean. hall_empty/hall_full/
            # _stepper_synced_to are left alone — they are sensor- /
            # architecture-driven, not stuck-flag risks.
            if owner._continuous_feed:
                logging.warning(
                    "buffer_feeder: stuck _continuous_feed cleared "
                    "at OVERFLOW→AUTO transition")
                owner._continuous_feed = False
                owner._continuous_feed_direction = 0
            owner._enable_stepper()
            owner._set_state(STATE_AUTO)

    def check_auto_ready(self, allow_jam=False):
        owner = self.owner
        if (self.is_hall1_active(Hall1Context.AUTO_ON)
                or owner._state == STATE_OVERFLOW):
            return "HALL1 overflow active"
        if not allow_jam and (owner._state == STATE_JAM or owner._jam_active):
            return ("JAM active — inspect and call BUFFER_CLEAR_JAM, "
                    "or BUFFER_AUTO_OFF first.")
        if owner._state in BUSY_PHASE_STATES:
            return ("LOAD/UNLOAD in progress (state=%s) — call "
                    "STOP_BUFFER_FILL to abort first." % owner._state)
        if owner._bang_bang_suspended:
            # Heal stale suspend before rejecting. RESUME the print
            # path doesn't fire idle_timeout:ready a second time if a
            # PAUSE was cancelled instead.
            owner._clear_stale_suspend_if_print_inactive(
                owner.reactor.monotonic())
        if owner._bang_bang_suspended:
            return ("print is paused (bang-bang suspended). RESUME the "
                    "print — bang-bang re-engages automatically. If the "
                    "print is already finished, use BUFFER_AUTO_OFF first.")
        return None
