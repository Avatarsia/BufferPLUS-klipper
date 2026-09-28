"""Gruppe-2-Fixes (Invalid-sequence-Crash-Linie, Logikfehler-Review 2026-07-09).

F1 — t0-Anchor ohne _current_move-Floor:
  t0 = max(forced_t0, lme, en, mcu_now) enthaelt NICHT das Ende des
  noch spielenden Chunks. Nach HALL1-Bounce rollt _halt_motion lme auf
  mcu_now zurueck, laesst _current_move aber intakt (Architektur-
  Kontrakt). Der naechste Submit kann dann VOR den bereits generierten
  Steps des in-flight Chunks anchorn -> stepcompress last_step_clock
  Rueckwaertssprung -> "Invalid sequence" MCU-Shutdown. Gilt fuer den
  forced_t0-Branch UND den forced_t0=None First-Chunk-Branch.

F2 — Pre-Sync-REPRIME-Anchor silently geskippt:
  sync_to_extruder submittete den Gap-Anchor mit forced_t0=None; bei
  gefuellter Toolhead-Queue (>MAX_T0_LOOKAHEAD_S voraus) greift der
  B-Skip, nichts wird gequeued, kein Caller merkt es — der Trapq-Swap
  laeuft mit stale last_step_clock durch. Fix: forced_t0=mcu_now+lead
  (Feeder war >REPRIME_GAP_S idle, also ist mcu_now+lead garantiert
  hinter keinem Step). Plus Already-Synced-Guard (Doppel-SYNC machte
  den Anchor zum Silent-No-Op).

F3 — _needs_overflow_prime leakt in IDLE:
  resume_after_overflow-Bottom-Branch cleart das Flag nicht; das
  Watchdog-Gate enthielt "not _needs_overflow_prime" -> Anchor fuer
  immer geblockt, Cursor altert (Issue-#31-Klasse). Fix: Watchdog darf
  bei gesetztem Flag feuern — der Anchor IST die Cursor-Refresh-
  Operation, die der Prime leisten sollte — und cleart das Flag.

F4 — _continuous_feed nie geraeumt bei Demand-0:
  Flush-Pfad setzt das Flag beim Submit, cleart es aber nie wenn der
  Modulator 0 liefert. Folgen: falscher SUPPLY-JAM auf stehendem
  Feeder (M109/Heat-Soak) + Watchdog-Gate dauerhaft geblockt.

F5 — Watchdog-Print-Block nur an print_stats:
  Serial-/OctoPrint-Drucke melden state='standby' -> Watchdog feuert
  forced_t0=None-Anchors mid-print (P7-77-A-Klasse). Fix: sekundaere
  Print-Detektion ueber juengste Extruder-Bewegung (velocity_tracker).

F6/F7 — sync_to_extruder-Rollback unvollstaendig / unsync ohne Rollback.
"""

import pytest
from fakes_klipper import FakeConfig, FakePrinter, FakePrintStats
from klipper_extras import buffer_feeder


def set_sensor_active(feeder, sensor_name, active):
    polarity_flip = feeder._pin_polarity_flip[sensor_name]
    raw = (not active) if polarity_flip else active
    feeder._pin_stable_state[sensor_name] = raw
    feeder._pin_raw_state[sensor_name] = raw


def make_feeder(values=None, state=None, connect=True):
    printer = FakePrinter()
    config = FakeConfig(printer=printer, values=values)
    feeder = buffer_feeder.BufferFeeder(config)
    if connect:
        printer.fire_event('klippy:connect')
    feeder._startup_grace_done = True
    if state is not None:
        feeder._state = state
    set_sensor_active(feeder, 'hall_overflow', False)
    set_sensor_active(feeder, 'hall_full', False)
    set_sensor_active(feeder, 'hall_empty', False)
    set_sensor_active(feeder, 'entrance', False)
    return printer, feeder


def anchor_spy(monkeypatch, feeder):
    """Wrap sync._submit_anchor_move (kwargs-tolerant) fuer Call-Counts."""
    calls = []

    def _spy(**kw):
        now = feeder.stepper.get_mcu().estimated_print_time(
            feeder.reactor.monotonic())
        calls.append(kw)
        feeder._last_move_end_time = now + 0.001
        return -1.0 if feeder.hall_overflow else 1.0

    monkeypatch.setattr(feeder.sync, "_submit_anchor_move", _spy)
    return calls


def submitted_t0s(printer):
    """t0 jedes real gequeueten Trapezoids (trapq_append arg #2)."""
    mq = printer.objects['motion_queuing']
    return [args[1] for args in mq.append_calls]


# ---------------------------------------------------------------------------
# F1 — current_move-Floor im t0-Anchor
# ---------------------------------------------------------------------------


def test_forced_t0_floors_on_inflight_move_end():
    """HALL1-Bounce-Szenario: lme von _halt_motion auf mcu_now geclampt,
    _current_move (Steps bis end_time generiert) intakt. Ein forced_t0-
    Submit darf NICHT vor dem in-flight Chunk-Ende anchorn."""
    printer, feeder = make_feeder(state=buffer_feeder.STATE_AUTO)
    feeder._stepcompress_primed = True
    feeder._current_move = {'end_time': 1.5, 'direction': 1.0,
                            'distance': 45.0, 'speed': 30.0}
    feeder._last_move_end_time = 0.0  # halt-clamped

    feeder._submit_move(5.0, 25.0, forced_t0=0.2)

    t0s = submitted_t0s(printer)
    assert len(t0s) == 1
    assert t0s[0] >= 1.5, (
        "forced_t0 submit must anchor at/after the in-flight move end "
        "(steps up to end_time already generated) — got t0=%.3f" % t0s[0])


def test_reactor_tick_t0_floors_on_inflight_move_end():
    """Gleiches Szenario im forced_t0=None First-Chunk-Branch (Legacy-
    Prime <=20ms nach Bounce-Clear waehrend der 45mm-Chunk drained)."""
    printer, feeder = make_feeder(state=buffer_feeder.STATE_AUTO)
    feeder._stepcompress_primed = True
    feeder._current_move = {'end_time': 1.5, 'direction': 1.0,
                            'distance': 45.0, 'speed': 30.0}
    feeder._last_move_end_time = 0.0
    printer.objects['toolhead'].last_move_time = 0.0

    feeder._submit_move(5.0, 25.0)

    t0s = submitted_t0s(printer)
    assert len(t0s) == 1
    assert t0s[0] >= 1.5, (
        "reactor-tick submit must anchor at/after the in-flight move "
        "end — got t0=%.3f" % t0s[0])


# ---------------------------------------------------------------------------
# F2 — Pre-Sync-Anchor + Doppel-SYNC
# ---------------------------------------------------------------------------


def test_sync_gap_anchor_queues_despite_far_future_toolhead():
    """gap > REPRIME_GAP_S + Toolhead-Queue weit voraus (aktiver Druck):
    der Pre-Sync-Anchor MUSS trotzdem real queuen (forced_t0=mcu_now+
    lead), sonst swappt SYNC mit stale last_step_clock."""
    printer, feeder = make_feeder()
    feeder.reactor.now = 20.0
    feeder._last_move_end_time = 0.0        # 20s idle
    printer.objects['toolhead'].last_move_time = 100.0  # far future

    feeder._sync_to_extruder('extruder')

    t0s = submitted_t0s(printer)
    assert len(t0s) == 1, (
        "pre-sync REPRIME anchor must actually queue a move — silent "
        "far-future skip leaves the cursor stale for the trapq swap")
    assert feeder._stepper_synced_to == 'extruder'


def test_double_sync_same_extruder_is_noop():
    printer, feeder = make_feeder()
    feeder._sync_to_extruder('extruder')
    trapq_sets_before = list(feeder.stepper.trapq_sets)

    feeder._sync_to_extruder('extruder')  # darf nicht crashen/mutieren

    assert feeder._stepper_synced_to == 'extruder'
    assert feeder.stepper.trapq_sets == trapq_sets_before, (
        "double-SYNC must be a no-op, not a second swap with a "
        "silently-skipped anchor")


def test_sync_failure_rolls_back_position_and_prime(monkeypatch):
    """Exception NACH set_position(_ext_pos): Rollback muss Position/
    _commanded_pos/_stepcompress_primed mitherstellen — sonst rechnet
    itersolve beim naechsten Own-Submit ab _ext_pos (Step-Burst)."""
    printer, feeder = make_feeder()
    printer.objects['extruder'].last_position = 180.0
    feeder._stepcompress_primed = True

    # Nur der ERSTE scan-window-Call (Haupt-Pfad) raist — der Rollback
    # laeuft durch. Der Double-Failure-Fall (Rollback raist auch) ist
    # in test_codex_round1_fixes.py::test_sync_rollback_failure_arms_
    # latch gepinnt (Latch bleibt dann bewusst armiert).
    calls = {'n': 0}

    def _boom_once():
        calls['n'] += 1
        if calls['n'] == 1:
            raise RuntimeError("scan window recompute failed")
    monkeypatch.setattr(
        printer.objects['motion_queuing'],
        "check_step_generation_scan_windows", _boom_once)

    with pytest.raises(RuntimeError):
        feeder._sync_to_extruder('extruder')

    assert feeder._stepper_synced_to is None
    assert feeder.stepper.position == (0.0, 0.0, 0.0), (
        "rollback must restore stepper position, not leave it at "
        "extruder.last_position=180")
    assert feeder._commanded_pos == 0.0
    assert feeder._stepcompress_primed is False, (
        "rollback must force a reprime on the next own-trapq submit")


def test_unsync_failure_forces_own_trapq_and_clears_sync(monkeypatch):
    """Exception mitten im Unsync darf _stepper_synced_to nicht gesetzt
    lassen (blockt sonst alle Submits/Bang-Bang/deferred _exit_overflow
    fuer immer). Forced completion: own trapq + primed=False."""
    printer, feeder = make_feeder()
    feeder._sync_to_extruder('extruder')

    def _boom():
        raise RuntimeError("flush failed")
    monkeypatch.setattr(
        printer.objects['toolhead'], "flush_step_generation", _boom)

    with pytest.raises(RuntimeError):
        feeder._unsync_if_synced()

    assert feeder._stepper_synced_to is None, (
        "failed unsync must not leave the sync latch set")
    assert feeder.stepper.trapq is feeder.sync.trapq
    assert feeder._stepcompress_primed is False


# ---------------------------------------------------------------------------
# F3 — _needs_overflow_prime blockt den Watchdog nicht mehr
# ---------------------------------------------------------------------------


def test_watchdog_fires_and_clears_needs_overflow_prime(monkeypatch):
    """IDLE + Flag gesetzt (OVERFLOW-Exit ohne AUTO-Promotion): Watchdog
    muss feuern — der Anchor IST der Cursor-Refresh — und das Flag
    clearen, damit kein Deadlock (Prime braucht Flush, Flush braucht
    Steps, Steps brauchen Watchdog) entsteht."""
    _, feeder = make_feeder(state=buffer_feeder.STATE_IDLE)
    feeder._needs_overflow_prime = True
    calls = anchor_spy(monkeypatch, feeder)

    feeder.reactor.now = 20.0
    feeder._last_move_end_time = 0.0
    feeder._main_tick(eventtime=20.0)

    assert len(calls) == 1, (
        "watchdog must fire despite _needs_overflow_prime — blocking "
        "it forever is the Issue-#31 stale-cursor pathology")
    assert feeder._needs_overflow_prime is False, (
        "successful watchdog anchor satisfies the prime — flag must "
        "be consumed")


# ---------------------------------------------------------------------------
# F4 — _continuous_feed Demand-0-Clear
# ---------------------------------------------------------------------------


def test_flush_demand_zero_clears_continuous_feed(monkeypatch):
    _, feeder = make_feeder(
        values={'use_flush_callback_bang_bang': True},
        state=buffer_feeder.STATE_AUTO)
    feeder._continuous_feed = True
    feeder._continuous_feed_direction = 1
    monkeypatch.setattr(feeder, "_compute_target_feed_speed", lambda: 0.0)

    feeder._flush_submit_streaming_chunk(10.0, 10.0)

    assert feeder._continuous_feed is False, (
        "demand-0 must end the feed session — stale flag causes false "
        "SUPPLY-JAM on a standing feeder and blocks the watchdog anchor")


def test_pending_modulated_zero_clears_continuous_feed(monkeypatch):
    _, feeder = make_feeder(state=buffer_feeder.STATE_AUTO)
    feeder._continuous_feed = True
    feeder._pending_remaining_mm = 30.0
    feeder._pending_direction = 1
    feeder._pending_speed = 25.0
    feeder._last_move_end_time = 0.0
    monkeypatch.setattr(feeder, "_compute_target_feed_speed", lambda: 0.0)

    feeder._tick_pending_chunk(eventtime=10.0)

    assert feeder._pending_remaining_mm == 0.0
    assert feeder._continuous_feed is False, (
        "modulated<=0 stream end must clear _continuous_feed like the "
        "HALL2 branch does")


# ---------------------------------------------------------------------------
# F5 — Extrusion-basierter Watchdog-Print-Block (Serial/OctoPrint)
# ---------------------------------------------------------------------------


def test_watchdog_blocked_by_recent_extrusion(monkeypatch):
    """print_stats sagt 'standby' (OctoPrint/Serial-Druck), aber der
    Extruder hat sich gerade bewegt: Watchdog muss blocken (P7-77-A —
    Anchor mid-print crasht via last_step_clock-Kollision)."""
    _, feeder = make_feeder(state=buffer_feeder.STATE_IDLE)
    calls = anchor_spy(monkeypatch, feeder)

    feeder.reactor.now = 20.0
    feeder._last_move_end_time = 0.0
    feeder._last_extruder_motion_time = 19.5  # extrusion 0.5s ago

    feeder._main_tick(eventtime=20.0)

    assert calls == [], (
        "recent extruder motion means an active print regardless of "
        "print_stats — the watchdog hard-block must hold")


def test_watchdog_fires_when_extrusion_stale(monkeypatch):
    _, feeder = make_feeder(state=buffer_feeder.STATE_IDLE)
    calls = anchor_spy(monkeypatch, feeder)

    feeder.reactor.now = 40.0
    feeder._last_move_end_time = 20.0
    feeder._last_extruder_motion_time = 5.0  # 35s ago — kein Druck

    feeder._main_tick(eventtime=40.0)

    assert len(calls) == 1


def test_main_tick_tracks_extruder_motion():
    """_main_tick muss juengste Extruder-Bewegung im Monotonic-Stempel
    festhalten (Basis der sekundaeren Print-Detektion)."""
    printer, feeder = make_feeder(state=buffer_feeder.STATE_IDLE)
    ext = printer.objects['extruder']
    t = 0.0
    for _ in range(14):
        ext.last_position = t * 15.0
        feeder.velocity_tracker.tick(t)
        t += 0.025
    before = feeder._last_extruder_motion_time

    feeder._main_tick(eventtime=t)

    assert feeder._last_extruder_motion_time > before, (
        "moving extruder must refresh _last_extruder_motion_time")
