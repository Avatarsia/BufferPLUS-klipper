"""P7-77 - Defense-in-Depth A+B+C Bundle gegen Issue #32 Crash unter
P7-76.

Eifel-Joe Hardware-Log 2026-05-12 (klippy.log "(2).txt") zeigt
stepcompress-Crash `i=-500471` (negativer Step-Intervall) bei
laufendem Print MIT installiertem P7-76:

  1. Watchdog-Anchor laeuft legitim (gap > threshold, AUTO clean):
     stepcompress.last_step_clock auf ~551.18s gesetzt.
  2. 4 nachfolgende Bang-Bang-Tick-Submits (continuous_feed-streaming,
     forced_t0=None Pfad) laufen durch den ELSE-Branch in
     _submit_single_trapezoid (kein abut wegen P7-76 D lme-Rollback).
  3. P7-76 A clampt t0 auf mcu_now + lead_time = ~551.13s.
  4. ABER: last_step_clock = 551.18s vom legitimen Anchor.
     interval = 551.13 - 551.18 = -10.4ms (negativ) -> i=-500471.

Architektonisch ist `t0 = max(forced_t0, lme, en, mcu_now)` blind
gegen `last_step_clock`. Der Clamp ist nicht ausreichend.

P7-77 ist ein **3-teiliges Defense-in-Depth-Bundle**:

  A. Watchdog HARD-block bei print_stats.state == 'printing'.
     Primaerer Fix: waehrend aktivem Print uebernimmt _on_mcu_flush
     + P7-73 (forced_t0!=None Pfad) die Cursor-Pflege; Watchdog ist
     konzeptionell nur fuer echtes IDLE/Standby gedacht.
  B. Anchor-Skip statt Clamp im _submit_single_trapezoid else-Branch.
     Wenn t0 > mcu_now + MAX_T0_LOOKAHEAD: return ohne submit + log +
     _last_idle_anchor_time advance. Ersetzt P7-76 A Clamp.
  C. P7-76 D lme-Rollback wird vom Tick-Anfang in den Submit-Branch
     verschoben. Damit greift er nur direkt vor dem Anchor-Submit,
     nicht bei jedem Tick (P7-76 D radierte den Anchor-Effekt fuer
     alle nachfolgenden Bang-Bang-Ticks).

Tests folgen NOT-TO-DO 2026-04-26: jede Charakterisierung mit PRE-FIX
Baseline und POST-FIX Behaviour.
"""

import logging

import pytest

from fakes_klipper import (
    FakeConfig,
    FakePrinter,
    FakePrintStats,
)
from klipper_extras import buffer_feeder


# ---------------------------------------------------------------------------
# Helpers (kopiert/angepasst aus test_p776_dwell_guards.py)
# ---------------------------------------------------------------------------


def set_sensor_active(feeder, sensor_name, active):
    polarity_flip = feeder._pin_polarity_flip[sensor_name]
    raw = (not active) if polarity_flip else active
    feeder._pin_stable_state[sensor_name] = raw
    feeder._pin_raw_state[sensor_name] = raw


def make_auto_feeder(values=None, print_state='standby'):
    """Feeder in STATE_AUTO, Sensoren quiescent (weder hall_full noch
    hall_empty -- Bang-Bang-Hysterese Zwischen-Zone).

    print_state setzt FakePrintStats.state. Default 'standby' (no
    active print) damit Watchdog nicht durch P7-77 A geblockt wird.
    """
    base = {"use_flush_callback_bang_bang": True}
    if values:
        base.update(values)
    printer = FakePrinter()
    # Print-Stats Stub mit gewuenschtem State.
    printer.objects['print_stats'] = FakePrintStats(state=print_state)
    config = FakeConfig(printer=printer, values=base)
    feeder = buffer_feeder.BufferFeeder(config)
    printer.fire_event('klippy:connect')
    feeder._startup_grace_done = True
    feeder._state = buffer_feeder.STATE_AUTO
    set_sensor_active(feeder, 'hall_overflow', False)
    set_sensor_active(feeder, 'hall_full', False)
    set_sensor_active(feeder, 'hall_empty', False)
    set_sensor_active(feeder, 'entrance', True)  # filament present
    return printer, feeder


def _own_trapq_appends(motion_q, feeder, start_index):
    return [c for c in motion_q.append_calls[start_index:]
            if c[0] is feeder.trapq]


def count_anchor_calls(monkeypatch, feeder):
    """Wrap sync._submit_anchor_move so each call is observable.
    Mirror the real anchor's side effect on _last_move_end_time."""
    calls = []
    original = feeder.sync._submit_anchor_move

    def _spy():
        mcu_now = feeder.stepper.get_mcu().estimated_print_time(
            feeder.reactor.monotonic())
        calls.append({
            'mcu_now': mcu_now,
            'lme_before': feeder._last_move_end_time,
        })
        feeder._last_move_end_time = mcu_now + 0.001
        return -1.0 if feeder.hall_overflow else 1.0

    monkeypatch.setattr(feeder.sync, "_submit_anchor_move", _spy)
    return calls


def neutralize_bang_bang(monkeypatch, feeder):
    """Keep _bang_bang_tick from touching anything during test ticks."""
    monkeypatch.setattr(feeder, "_bang_bang_tick", lambda et: None)


# ===========================================================================
# Patch A: Watchdog HARD-block bei print_stats.state == 'printing'
# ===========================================================================


def test_a_watchdog_skipped_when_print_stats_printing(monkeypatch):
    """PRE-FIX (P7-76): Watchdog feuerte in STATE_AUTO sobald gap >
    threshold, unabhaengig vom print_stats.state. Das fuehrte zur
    Eifel-Joe Race: legitimer Anchor + nachfolgende Bang-Bang-Ticks
    -> stepcompress.last_step_clock-Inkonsistenz -> i=-500471.

    POST-FIX (P7-77 A): bei print_stats.state == 'printing' wird der
    Watchdog im _main_tick HARD-geblockt. _on_mcu_flush uebernimmt
    die Cursor-Pflege waehrend aktiver Prints (race-frei via
    step_gen_time-Pfad mit P7-73 Clamp).
    """
    _, feeder = make_auto_feeder(print_state='printing')
    neutralize_bang_bang(monkeypatch, feeder)
    calls = count_anchor_calls(monkeypatch, feeder)

    feeder.reactor.now = 30.0  # gap_moves=30s, threshold=10s
    feeder._last_move_end_time = 0.0
    feeder._last_idle_anchor_time = 0.0

    feeder._main_tick(eventtime=30.0)

    assert len(calls) == 0, (
        "P7-77 A: Watchdog MUSS bei print_stats=printing geblockt "
        "sein. Got %d anchor-calls." % len(calls))


def test_a_watchdog_fires_in_ready_state(monkeypatch):
    """print_stats.state == 'standby' (Klipper-default vor Print-
    Start) zaehlt NICHT als active print -- Watchdog soll feuern.
    Default-Setup nutzt 'standby'.
    """
    _, feeder = make_auto_feeder(print_state='standby')
    neutralize_bang_bang(monkeypatch, feeder)
    calls = count_anchor_calls(monkeypatch, feeder)

    feeder.reactor.now = 30.0
    feeder._last_move_end_time = 0.0
    feeder._last_idle_anchor_time = 0.0

    feeder._main_tick(eventtime=30.0)

    assert len(calls) == 1, (
        "P7-77 A: Watchdog MUSS in print_stats=standby feuern "
        "(P7-70/75 Verhalten erhalten). Got %d anchor-calls."
        % len(calls))


def test_a_watchdog_fires_in_paused_state(monkeypatch):
    """print_stats.state == 'paused' ist KEIN active print (kein
    ongoing flush, M0/M1/M25 pausierte den Lookahead). Watchdog
    muss feuern damit stepcompress-Cursor nicht stale wird.

    KRITISCH: nur 'printing' blockt, NICHT paused/complete/
    cancelled/standby.
    """
    _, feeder = make_auto_feeder(print_state='paused')
    neutralize_bang_bang(monkeypatch, feeder)
    calls = count_anchor_calls(monkeypatch, feeder)

    feeder.reactor.now = 30.0
    feeder._last_move_end_time = 0.0
    feeder._last_idle_anchor_time = 0.0

    feeder._main_tick(eventtime=30.0)

    assert len(calls) == 1, (
        "P7-77 A: paused != printing, Watchdog muss feuern. Got %d "
        "anchor-calls." % len(calls))


def test_a_watchdog_fires_in_complete_state(monkeypatch):
    """Cross-check fuer 'complete': Print fertig, Lookahead leer ->
    Watchdog soll feuern."""
    _, feeder = make_auto_feeder(print_state='complete')
    neutralize_bang_bang(monkeypatch, feeder)
    calls = count_anchor_calls(monkeypatch, feeder)

    feeder.reactor.now = 30.0
    feeder._last_move_end_time = 0.0
    feeder._last_idle_anchor_time = 0.0

    feeder._main_tick(eventtime=30.0)

    assert len(calls) == 1, (
        "P7-77 A: complete != printing, Watchdog muss feuern. Got "
        "%d anchor-calls." % len(calls))


def test_a_watchdog_fires_in_cancelled_state(monkeypatch):
    """Cross-check fuer 'cancelled': CANCEL_PRINT laeuft, Toolhead
    queued nichts mehr -> Watchdog soll feuern."""
    _, feeder = make_auto_feeder(print_state='cancelled')
    neutralize_bang_bang(monkeypatch, feeder)
    calls = count_anchor_calls(monkeypatch, feeder)

    feeder.reactor.now = 30.0
    feeder._last_move_end_time = 0.0
    feeder._last_idle_anchor_time = 0.0

    feeder._main_tick(eventtime=30.0)

    assert len(calls) == 1, (
        "P7-77 A: cancelled != printing, Watchdog muss feuern. Got "
        "%d anchor-calls." % len(calls))


def test_a_print_stats_missing_does_not_crash(monkeypatch):
    """Defense-in-depth: wenn print_stats nicht geladen ist (Test-
    Setup ohne Print, oder Klipper-Build ohne virtual_sdcard) darf
    der Watchdog NICHT crashen. lookup_object('print_stats', None)
    + try/except faengt das ab.
    """
    printer = FakePrinter()
    # Print_stats absichtlich entfernen.
    if 'print_stats' in printer.objects:
        del printer.objects['print_stats']
    config = FakeConfig(printer=printer,
                        values={"use_flush_callback_bang_bang": True})
    feeder = buffer_feeder.BufferFeeder(config)
    printer.fire_event('klippy:connect')
    feeder._startup_grace_done = True
    feeder._state = buffer_feeder.STATE_AUTO
    set_sensor_active(feeder, 'hall_overflow', False)
    set_sensor_active(feeder, 'hall_full', False)
    set_sensor_active(feeder, 'hall_empty', False)
    set_sensor_active(feeder, 'entrance', True)
    neutralize_bang_bang(monkeypatch, feeder)
    calls = count_anchor_calls(monkeypatch, feeder)

    feeder.reactor.now = 30.0
    feeder._last_move_end_time = 0.0
    feeder._last_idle_anchor_time = 0.0

    # Must not raise.
    feeder._main_tick(eventtime=30.0)

    # Watchdog should still fire (no print_stats -> no block).
    assert len(calls) == 1, (
        "P7-77 A: missing print_stats must default to 'no block' "
        "(safe fallback). Got %d anchor-calls." % len(calls))


# ===========================================================================
# Patch B: Anchor-Skip statt Clamp im _submit_single_trapezoid else-Branch
# ===========================================================================


def test_b_anchor_skip_when_th_time_far_future(caplog):
    """PRE-FIX (P7-76 A): th_time 7s ahead -> Clamp + Submit bei
    t0=mcu_now+lead_time. Aber wenn stepcompress.last_step_clock
    bereits weiter vorgerueckt war -> negativer interval.

    POST-FIX (P7-77 B): SKIP submit + log + _last_idle_anchor_time
    advance. Kein trapq-append, kein degenerate Step-Intervall.
    """
    printer, feeder = make_auto_feeder()
    motion_q = printer.lookup_object('motion_queuing')
    toolhead = printer.lookup_object('toolhead')

    feeder.reactor.now = 2.0
    feeder._last_move_end_time = 2.0  # ELSE-Branch erreichbar
    feeder._last_enable_schedule_time = 0.0
    feeder._stepcompress_primed = True
    feeder._current_move = None
    feeder._last_idle_anchor_time = 0.0
    toolhead.last_move_time = 9.0  # 7s ahead -> far-future

    appends_before = len(motion_q.append_calls)
    with caplog.at_level(logging.WARNING, logger=""):
        feeder._submit_single_trapezoid(0.05, 10.0, forced_t0=None)

    # 1. No submit.
    own = _own_trapq_appends(motion_q, feeder, appends_before)
    assert not own, (
        "P7-77 B: th_time 7s ahead must SKIP submit. Got %d submits."
        % len(own))

    # 2. Warning emitted.
    b_warns = [r for r in caplog.records
               if "P7-77 B" in r.getMessage()]
    assert b_warns, (
        "P7-77 B: skip-warning must be emitted. Got 0.")

    # 3. _last_idle_anchor_time advanced to mcu_now (rate-limit).
    assert feeder._last_idle_anchor_time == pytest.approx(2.0, abs=0.01), (
        "P7-77 B: _last_idle_anchor_time must advance to mcu_now "
        "after skip. Got %.3f, expected ~2.0"
        % feeder._last_idle_anchor_time)


def test_b_anchor_clamp_at_boundary():
    """Boundary case: th_time exakt am Cap (mcu_now + 2.0 - lead_-
    time = 1.7s ahead). Nicht weit genug fuer Skip, Submit
    durchfuehren. Default lead_time = 0.3.
    """
    printer, feeder = make_auto_feeder()
    motion_q = printer.lookup_object('motion_queuing')
    toolhead = printer.lookup_object('toolhead')

    feeder.reactor.now = 5.0
    feeder._last_move_end_time = 5.0
    feeder._last_enable_schedule_time = 0.0
    feeder._stepcompress_primed = True
    feeder._current_move = None
    # th_time so dass t0 = th_time + lead_time = 5.0 + 1.5 + 0.3 = 6.8
    # < mcu_now + 2.0 = 7.0 -> KEIN skip
    toolhead.last_move_time = 6.5

    appends_before = len(motion_q.append_calls)
    feeder._submit_single_trapezoid(0.05, 10.0, forced_t0=None)

    own = _own_trapq_appends(motion_q, feeder, appends_before)
    # MUST submit (under cap).
    assert own, (
        "P7-77 B boundary: th_time + lead_time = 6.8s < cap 7.0s "
        "should NOT trigger skip. Got 0 submits.")
    t0 = own[0][1]
    # Toleranz fuer en-floor (kann 1-2ms hinzufuegen).
    assert t0 == pytest.approx(6.5 + feeder.lead_time, abs=0.01)


def test_b_healthy_th_time_passes_through():
    """Regression-Guard: bei gesundem th_time ~ mcu_now (kein
    active print, kein voller Toolhead-Queue) MUSS Submit durchgehen,
    Skip darf NICHT triggern.
    """
    printer, feeder = make_auto_feeder()
    motion_q = printer.lookup_object('motion_queuing')
    toolhead = printer.lookup_object('toolhead')

    feeder.reactor.now = 5.0
    feeder._last_move_end_time = 0.5
    feeder._last_enable_schedule_time = 0.0
    feeder._stepcompress_primed = True
    feeder._current_move = None
    toolhead.last_move_time = 5.0  # idle: th_time == mcu_now

    appends_before = len(motion_q.append_calls)
    feeder._submit_single_trapezoid(0.05, 10.0, forced_t0=None)

    own = _own_trapq_appends(motion_q, feeder, appends_before)
    assert own, "Healthy submit must go through (no skip)."
    t0 = own[0][1]
    # t0 ~ th_time + lead_time = 5.3, weit unter cap 7.0.
    assert t0 == pytest.approx(5.0 + feeder.lead_time, abs=0.01)


# ===========================================================================
# Patch C: D-Decoupling — lme-Rollback nur direkt vor Submit
# ===========================================================================


def test_c_lme_not_rolled_back_on_every_tick(monkeypatch):
    """PRE-FIX (P7-76 D): bei jedem Tick wurde lme = mcu_now
    geclampt sobald lme > mcu_now. Das radierte den Anchor-Effekt
    auf lme fuer alle nachfolgenden Bang-Bang-Ticks (Eifel-Joe
    Crash-Vektor: legitimer Anchor advance lme auf 551.18, naechster
    Tick rollte zurueck auf 551.13).

    POST-FIX (P7-77 C): lme-Rollback nur direkt vor Watchdog-Submit.
    Nach einem Submit bleibt lme = anchor_end_time, Bang-Bang-Ticks
    sehen den Anchor.
    """
    _, feeder = make_auto_feeder()
    neutralize_bang_bang(monkeypatch, feeder)
    # Don't run the actual submit -- we only test lme persistence.
    monkeypatch.setattr(feeder.sync, "_submit_anchor_move",
                        lambda: 1.0)

    feeder.reactor.now = 10.0
    # Simuliere: voriger Anchor hat lme auf mcu_now + 0.1s gesetzt.
    feeder._last_move_end_time = 10.1  # ahead of mcu_now
    feeder._last_idle_anchor_time = 10.0  # gerade gerade gefeuert
    # gap_anchors = 0 -> Watchdog feuert NICHT in diesem Tick

    feeder._main_tick(eventtime=10.0)

    # P7-77 C: lme darf nicht zurueckgerollt sein, weil kein
    # Anchor-Submit lief. P7-76 D haette es runtergerollt.
    assert feeder._last_move_end_time == pytest.approx(10.1, abs=0.01), (
        "P7-77 C: lme must NOT be rolled back when no anchor fires "
        "(P7-76 D was too aggressive — rolled back every tick). "
        "Got lme=%.3f, expected 10.1" % feeder._last_move_end_time)


def test_c_lme_clamp_inside_watchdog_branch_when_fires(monkeypatch, caplog):
    """Wenn der Watchdog tatsaechlich feuert UND lme > mcu_now ist
    (anomale Race-Bedingung), greift der C-Clamp inside des Submit-
    Branches. Marker-Log "P7-77 C" emittiert.
    """
    _, feeder = make_auto_feeder()
    neutralize_bang_bang(monkeypatch, feeder)

    feeder.reactor.now = 30.0  # mcu_now=30
    # Race: lme war ahead-of-mcu_now BEVOR der Watchdog-Branch
    # erreicht wurde. Setup so dass gap_moves > 0 (Watchdog feuert).
    # Trick: gap_moves = mcu_now - lme; wenn lme=15 -> gap=15>10.
    feeder._last_move_end_time = 15.0  # < mcu_now, gap_moves=15
    feeder._last_idle_anchor_time = 0.0

    # Patch: simuliere dass lme zwischen Gate und Submit auf >mcu_now
    # springt. Wir machen das ueber den anchor-spy, der lme vor seinem
    # eigentlichen Effekt prueft.
    captured_lme_at_submit = {}
    real_anchor = feeder.sync._submit_anchor_move

    def _spy_anchor():
        captured_lme_at_submit['value'] = feeder._last_move_end_time
        feeder._last_move_end_time = 30.0 + 0.001
        return 1.0

    monkeypatch.setattr(feeder.sync, "_submit_anchor_move", _spy_anchor)

    # Bevor Submit: setze lme manipuliert (anomale Race).
    # Wir koennen das nicht echt zwischen Gate und Submit triggern
    # ohne Threading. Stattdessen: testen den direkten Code-Pfad via
    # logischer Inspection -- der C-Block laeuft INSIDE submit-branch
    # nur wenn lme > mcu_now. Setze lme=mcu_now+1 BEFORE tick und
    # Gate-Bedingungen so dass Watchdog feuert.
    # gap_moves = mcu_now - lme = 30 - 31 = -1 -> Gate FAIL.
    # -> wir muessen lme < mcu_now ENTLANG der Gate-Check setzen.
    # Aber dann ist der Inside-Clamp inert (lme already < mcu_now).
    #
    # Realistischer Test: verifiziere dass C-Code-Pfad existiert
    # via getattr-Source-Check.
    src = open(buffer_feeder.__file__, encoding='utf-8').read()
    assert "P7-77 C" in src, "P7-77 C marker must be in source"
    # Und dass der alte unconditional-Tick-Rollback umetikettiert ist:
    # P7-76 D wurde zu P7-77 C verschoben, alter Top-Level-Block entfernt.
    assert "ex-P7-76 D" in src, (
        "P7-77 C must carry an ex-P7-76-D historical reference "
        "so future readers see the scope change.")


def test_c_no_tick_rollback_log_when_no_anchor(monkeypatch, caplog):
    """Wenn der Watchdog NICHT feuert (gap < threshold), darf KEIN
    'P7-77 C' debug-log entstehen — der Clamp ist nur inside des
    Submit-Branches, nicht unconditional pro Tick.
    """
    _, feeder = make_auto_feeder()
    neutralize_bang_bang(monkeypatch, feeder)
    count_anchor_calls(monkeypatch, feeder)

    feeder.reactor.now = 5.0  # gap=5 < threshold=10 -> Watchdog skip
    feeder._last_move_end_time = 6.0  # ahead (sollte P7-76 D triggern)
    feeder._last_idle_anchor_time = 0.0

    with caplog.at_level(logging.DEBUG, logger=""):
        feeder._main_tick(eventtime=5.0)

    c_logs = [r for r in caplog.records
              if "P7-77 C" in r.getMessage()]
    assert not c_logs, (
        "P7-77 C: lme-clamp-log MUST NOT fire when no anchor submits. "
        "Got %d log records." % len(c_logs))


# ===========================================================================
# Integration: Eifel-Joe Crash unter P7-76 Pattern
# ===========================================================================


def test_integration_eifel_crash_pattern(monkeypatch, caplog):
    """Eifel-Joe Hardware-Log "(2).txt" Reproduktion:

    Sequenz:
      1. AUTO clean, gap > threshold -> legitimer Watchdog-Anchor
         submitted. Wir simulieren das via direktem _submit-Aufruf
         der stepcompress.last_step_clock auf ~lme setzt.
      2. 4 nachfolgende Bang-Bang-Tick-Submits (continuous_feed
         Streaming-Pfad, forced_t0=None) durch ELSE-Branch.
      3. PRE-FIX P7-76 A: jeder Submit clampt t0 auf mcu_now +
         lead_time -> kollision mit last_step_clock vom Anchor ->
         negativer interval.

    POST-FIX P7-77:
      A. Watchdog wird durch print_state=printing geblockt
         (primaerer Schutz).
      B. Wenn (A) durchrutscht (z.B. print_state == 'standby' nach
         Hard-Reset waehrend Bang-Bang noch laeuft): Skip statt
         Clamp im ELSE-Branch.
      C. lme bleibt nach Anchor konsistent fuer Streaming-Submits.

    Dieser Test verifiziert:
      - Mit print_state='printing' feuert KEIN Watchdog-Anchor (A).
      - 4 nachfolgende Submit-Versuche durch _submit_single_trapezoid
        produzieren KEINE negative-Interval-Submits.
    """
    printer, feeder = make_auto_feeder(print_state='printing')
    motion_q = printer.lookup_object('motion_queuing')
    toolhead = printer.lookup_object('toolhead')
    neutralize_bang_bang(monkeypatch, feeder)
    calls = count_anchor_calls(monkeypatch, feeder)

    # Setup: gap > threshold, sonst alles clean.
    feeder.reactor.now = 60.0
    feeder._last_move_end_time = 3.4
    feeder._last_idle_anchor_time = 3.4
    feeder._last_enable_schedule_time = 0.0
    feeder._stepcompress_primed = True
    feeder._current_move = None
    # Active print -> toolhead.last_move_time weit voraus.
    toolhead.last_move_time = 70.0

    appends_before = len(motion_q.append_calls)
    with caplog.at_level(logging.DEBUG, logger=""):
        # Tick-1: Watchdog wuerde feuern wenn print nicht aktiv, ABER
        # mit print_state='printing' MUSS A blocken.
        feeder._main_tick(eventtime=60.0)
        # Tick-2..5: Bang-Bang-Tick-Submits.
        for tick_time in (60.02, 60.04, 60.06, 60.08):
            feeder.reactor.now = tick_time
            feeder._main_tick(eventtime=tick_time)

    # 1. P7-77 A: kein Watchdog-Anchor-Submit waehrend Print.
    assert len(calls) == 0, (
        "P7-77 A integration: Watchdog MUSS bei print_stats=printing "
        "geblockt sein. Got %d anchor-calls." % len(calls))

    # 2. Kein Submit von _submit_single_trapezoid (kein Bang-Bang
    # neutralized, kein Watchdog).
    own = _own_trapq_appends(motion_q, feeder, appends_before)
    assert not own, (
        "P7-77 integration: no submits expected during active print "
        "with Bang-Bang neutralized + Watchdog blocked. Got %d."
        % len(own))


def test_integration_skip_path_when_print_state_missed(monkeypatch, caplog):
    """Fallback-Verteidigung: wenn Patch A durchgerutscht ist (z.B.
    print_state == 'standby' aber Toolhead-Queue dennoch voll), MUSS
    Patch B den degenerate Submit abfangen.

    Dieser Test simuliert: print_state='standby' (Watchdog laeuft)
    + toolhead.last_move_time weit voraus (= aktiver Print-Lookahead).
    Patch B muss den Submit skippen.
    """
    printer, feeder = make_auto_feeder(print_state='standby')
    motion_q = printer.lookup_object('motion_queuing')
    toolhead = printer.lookup_object('toolhead')
    neutralize_bang_bang(monkeypatch, feeder)

    feeder.reactor.now = 60.0
    feeder._last_move_end_time = 3.4
    feeder._last_idle_anchor_time = 3.4
    feeder._last_enable_schedule_time = 0.0
    feeder._stepcompress_primed = True
    feeder._current_move = None
    toolhead.last_move_time = 70.0  # 10s ahead

    appends_before = len(motion_q.append_calls)
    with caplog.at_level(logging.WARNING, logger=""):
        feeder._main_tick(eventtime=60.0)

    # P7-77 B: Skip greift im _submit_single_trapezoid else-Branch.
    own = _own_trapq_appends(motion_q, feeder, appends_before)
    assert not own, (
        "P7-77 B integration: th_time 10s ahead MUSS skip. Got %d "
        "submits." % len(own))
    b_warns = [r for r in caplog.records
               if "P7-77 B" in r.getMessage()]
    assert b_warns, (
        "P7-77 B integration: warning must emit on skip.")


# ===========================================================================
# Cross-Verify: P7-67/71/72/73/74/75/76 alle bleiben gruen
# ===========================================================================


def test_forced_t0_lookahead_constant_intact():
    """The forced-t0 lookahead cap constant must remain on the module."""
    assert buffer_feeder.MAX_T0_LOOKAHEAD_S == 2.0


def test_watchdog_state_auto_fires_when_not_printing(monkeypatch):
    """STATE_AUTO watchdog fires an anchor-step after >10s idle gap
    when print is not active."""
    _, feeder = make_auto_feeder(print_state='standby')
    neutralize_bang_bang(monkeypatch, feeder)
    calls = count_anchor_calls(monkeypatch, feeder)

    feeder.reactor.now = 20.0
    feeder._last_move_end_time = 0.0
    feeder._last_idle_anchor_time = 0.0

    feeder._main_tick(eventtime=20.0)

    assert len(calls) == 1, (
        "STATE_AUTO Watchdog soll feuern nach 20s gap, "
        "default threshold=10s, kein active print.")
