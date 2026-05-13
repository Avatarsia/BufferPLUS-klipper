"""P7-78 - Print-Block-Stale-Override fuer P7-77 A Watchdog HARD-block.

Issue #29 Crash unter P7-77:
  print_stats.state == 'printing' blockt den Idle-Watchdog hart
  (P7-77 A). In HALL2-Hysterese-Zwischenzone feuert _on_mcu_flush
  minutenlang nicht — Klipper's motion_queuing.flush_handler ruft
  den Callback nur synchron mit Step-Generation; ohne Steps kein
  Callback. stepcompress.last_step_clock altert, und der erste
  Bang-Bang-Submit nach Stille produziert c=7 Invalid sequence.

Eifel-Joe Hardware-Log 2026-05-13:
  Z.8706: 1063.5s IDLE -> AUTO Transition
  Z.9895: 1226.7s stepcompress c=7 Invalid sequence
  Delta:  163.2s Funkstille zwischen den Events.

Fix P7-78:
  Watchdog-Print-Block (P7-77 A) wird durch Override aufgeweicht:
  Watchdog darf trotz print_stats=printing feuern, wenn
  _on_mcu_flush messbar laenger als idle_anchor_gap (Default 10s,
  Faktor 1.0, USER-WAHL) nicht gerufen wurde.

  Tracker: self._last_mcu_flush_time (MCU print-time), gesetzt
  als ALLERERSTE Anweisung in _on_mcu_flush — VOR allen Early-
  Returns. Damit zaehlen auch Early-Return-Ticks (state != AUTO,
  suspended, use_flush_callback_bang_bang=False) als "MCU lebt".

  Boot-Schutz: _last_mcu_flush_time == 0.0 -> kein Override
  (frischer Boot, noch nie ein Flush gesehen).

  Strict-Greater: Stille > idle_anchor_gap triggert. Stille
  exakt == idle_anchor_gap bleibt geblockt.
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
# Helpers (Pattern aus test_p777_watchdog_print_block.py uebernommen)
# ---------------------------------------------------------------------------


def set_sensor_active(feeder, sensor_name, active):
    polarity_flip = feeder._pin_polarity_flip[sensor_name]
    raw = (not active) if polarity_flip else active
    feeder._pin_stable_state[sensor_name] = raw
    feeder._pin_raw_state[sensor_name] = raw


def make_auto_feeder(values=None, print_state='printing'):
    """Feeder in STATE_AUTO, Sensoren quiescent (Hysterese-Zwischenzone).

    Default print_state='printing' fuer P7-78 — der Watchdog ist
    via P7-77 A geblockt und der Override ist genau hier zu testen.
    """
    base = {"use_flush_callback_bang_bang": True}
    if values:
        base.update(values)
    printer = FakePrinter()
    printer.objects['print_stats'] = FakePrintStats(state=print_state)
    config = FakeConfig(printer=printer, values=base)
    feeder = buffer_feeder.BufferFeeder(config)
    printer.fire_event('klippy:connect')
    feeder._startup_grace_done = True
    feeder._state = buffer_feeder.STATE_AUTO
    set_sensor_active(feeder, 'hall_overflow', False)
    set_sensor_active(feeder, 'hall_full', False)
    set_sensor_active(feeder, 'hall_empty', False)
    set_sensor_active(feeder, 'entrance', True)
    return printer, feeder


def count_anchor_calls(monkeypatch, feeder):
    """Wrap sync._submit_anchor_move so each call is observable.
    Mirror the real anchor's side effect on _last_move_end_time.

    P7-78v2: nimmt **kwargs entgegen damit der Override-Pfad
    `forced_t0=mcu_now+lead_time` uebergeben darf ohne TypeError."""
    calls = []

    def _spy(**kwargs):
        mcu_now = feeder.stepper.get_mcu().estimated_print_time(
            feeder.reactor.monotonic())
        calls.append({
            'mcu_now': mcu_now,
            'lme_before': feeder._last_move_end_time,
            'forced_t0': kwargs.get('forced_t0'),
        })
        feeder._last_move_end_time = mcu_now + 0.001
        return -1.0 if feeder.hall_overflow else 1.0

    monkeypatch.setattr(feeder.sync, "_submit_anchor_move", _spy)
    return calls


def neutralize_bang_bang(monkeypatch, feeder):
    monkeypatch.setattr(feeder, "_bang_bang_tick", lambda et: None)


# ===========================================================================
# P7-78: Print-Block-Stale-Override
# ===========================================================================


def test_p778_initial_flush_time_blocks_override(monkeypatch):
    """Fresh boot: _last_mcu_flush_time == 0.0. Even with print
    active and gap > threshold, the override MUST NOT fire — the
    Boot-Schutz guard `_last_mcu_flush_time > 0.0` keeps the
    watchdog blocked until at least one real flush was observed.
    """
    _, feeder = make_auto_feeder(print_state='printing')
    neutralize_bang_bang(monkeypatch, feeder)
    calls = count_anchor_calls(monkeypatch, feeder)

    # Boot state: never seen a flush.
    assert feeder._last_mcu_flush_time == 0.0
    feeder.reactor.now = 30.0
    feeder._last_move_end_time = 0.0
    feeder._last_idle_anchor_time = 0.0

    feeder._main_tick(eventtime=30.0)

    assert len(calls) == 0, (
        "P7-78: Boot-Schutz — Override darf bei initial flush_time "
        "== 0.0 NICHT feuern. Got %d anchor-calls." % len(calls))


def test_p778_fresh_flush_blocks_override(monkeypatch):
    """Print active + _on_mcu_flush gerade vor 2s gerufen (frischer
    Flush). Override darf NICHT feuern, Watchdog bleibt geblockt.
    """
    _, feeder = make_auto_feeder(print_state='printing')
    neutralize_bang_bang(monkeypatch, feeder)
    calls = count_anchor_calls(monkeypatch, feeder)

    feeder.reactor.now = 30.0
    # mcu_now == 30.0 (FakeMCU.estimated_print_time == eventtime).
    # Flush gerade vor 2s gesehen -> 2s < idle_anchor_gap (10s).
    feeder._last_mcu_flush_time = 28.0
    feeder._last_move_end_time = 0.0
    feeder._last_idle_anchor_time = 0.0

    feeder._main_tick(eventtime=30.0)

    assert len(calls) == 0, (
        "P7-78: Frischer Flush (2s alt < 10s threshold) — Override "
        "darf NICHT feuern. Got %d anchor-calls." % len(calls))


def test_p778_stale_flush_triggers_override(monkeypatch):
    """Print active + _on_mcu_flush vor idle_anchor_gap + 1s
    gerufen -> Override fires, Anchor-Submit laeuft.
    """
    _, feeder = make_auto_feeder(print_state='printing')
    neutralize_bang_bang(monkeypatch, feeder)
    calls = count_anchor_calls(monkeypatch, feeder)

    feeder.reactor.now = 30.0
    # Stille = 11s > idle_anchor_gap (10s).
    feeder._last_mcu_flush_time = 30.0 - (feeder.idle_anchor_gap + 1.0)
    # gap_moves > threshold damit innerer Anchor-Branch laeuft.
    feeder._last_move_end_time = 0.0
    feeder._last_idle_anchor_time = 0.0

    feeder._main_tick(eventtime=30.0)

    assert len(calls) == 1, (
        "P7-78: Stale flush (11s > 10s threshold) — Override MUSS "
        "feuern und Watchdog-Anchor submitten. Got %d anchor-calls."
        % len(calls))


def test_p778_boundary_strict_greater(monkeypatch):
    """Boundary: Stille knapp unter idle_anchor_gap bleibt
    geblockt; Stille knapp ueber idle_anchor_gap triggert Override
    (strict > in der Override-Logik).

    NOTE: FakeReactor.monotonic() inkrementiert pro Aufruf um 1ms,
    daher kann mcu_now zur Auswertungszeit leicht von feeder.
    reactor.now abweichen. Wir testen daher mit klaren Margins
    rechts und links des Gap (50ms), nicht mit exakter Gleichheit.
    """
    # Sub-1: silence = gap - 50ms -> klar unter threshold, geblockt.
    _, feeder = make_auto_feeder(print_state='printing')
    neutralize_bang_bang(monkeypatch, feeder)
    calls = count_anchor_calls(monkeypatch, feeder)

    feeder.reactor.now = 30.0
    feeder._last_mcu_flush_time = (
        30.0 - feeder.idle_anchor_gap + 0.05)
    feeder._last_move_end_time = 0.0
    feeder._last_idle_anchor_time = 0.0

    feeder._main_tick(eventtime=30.0)
    assert len(calls) == 0, (
        "P7-78 boundary: Stille knapp unter Gap (gap-50ms) MUSS "
        "geblockt bleiben. Got %d." % len(calls))

    # Sub-2: silence = gap + 50ms -> klar ueber threshold, Override.
    _, feeder2 = make_auto_feeder(print_state='printing')
    neutralize_bang_bang(monkeypatch, feeder2)
    calls2 = count_anchor_calls(monkeypatch, feeder2)

    feeder2.reactor.now = 30.0
    feeder2._last_mcu_flush_time = (
        30.0 - feeder2.idle_anchor_gap - 0.05)
    feeder2._last_move_end_time = 0.0
    feeder2._last_idle_anchor_time = 0.0

    feeder2._main_tick(eventtime=30.0)
    assert len(calls2) == 1, (
        "P7-78 boundary: Stille knapp ueber Gap (gap+50ms) MUSS "
        "Override triggern. Got %d." % len(calls2))


def test_p778_boundary_strict_greater_unit(monkeypatch):
    """Strict-Greater-Unit-Test: Direkte Inspektion der Override-
    Bedingung im Source. `> self.idle_anchor_gap` (strict, nicht
    >=) damit Stille exakt am Gap noch geblockt bleibt — wichtig
    fuer Konsistenz mit dem normalen Watchdog im Nicht-Print-Pfad,
    der ebenfalls strict `>` benutzt (siehe Z.2038-2039:
    `gap_moves > self.idle_anchor_gap`).
    """
    src = open(buffer_feeder.__file__, encoding='utf-8').read()
    # Override-Marker muss in Source sein.
    assert "P7-78" in src, "P7-78 marker must be in source"
    assert "print-block stale override" in src, (
        "P7-78 override-log must be in source")
    # Strict-Greater-Operator gegen idle_anchor_gap im Override-
    # Block: das Substring "> self.idle_anchor_gap" kommt schon
    # vom normalen Watchdog vor — wir verifizieren stattdessen
    # die Abwesenheit von ">= self.idle_anchor_gap" im P7-78-Pfad.
    # Heuristik: schau die 30 Zeilen rund um den "print-block stale
    # override"-Log an.
    idx = src.index("print-block stale override")
    window = src[max(0, idx - 1500):idx + 200]
    assert ">= self.idle_anchor_gap" not in window, (
        "P7-78: Override-Bedingung MUSS strict `>` nutzen, nicht "
        ">=. Got >= in window around override-log.")


def test_p778_no_print_unchanged(monkeypatch):
    """Print inactive (standby) — Watchdog feuert wie bisher,
    Override-Pfad nicht relevant. Regression-Guard fuer den
    Nicht-Print-Pfad.
    """
    _, feeder = make_auto_feeder(print_state='standby')
    neutralize_bang_bang(monkeypatch, feeder)
    calls = count_anchor_calls(monkeypatch, feeder)

    feeder.reactor.now = 30.0
    # Stille von 50s, aber print=standby -> Override-Branch
    # ueberhaupt nicht relevant, Watchdog feuert ueber den
    # normalen Pfad.
    feeder._last_mcu_flush_time = -20.0  # 50s alt
    feeder._last_move_end_time = 0.0
    feeder._last_idle_anchor_time = 0.0

    feeder._main_tick(eventtime=30.0)

    assert len(calls) == 1, (
        "P7-78 regression: print=standby Watchdog soll wie bisher "
        "feuern. Got %d." % len(calls))


def test_p778_eifel_163s_silence_reproduction(monkeypatch, caplog):
    """Eifel-Joe Hardware-Beleg 2026-05-13: 163.2s Funkstille
    zwischen state-change (Z.8706 IDLE -> AUTO @ 1063.5s) und
    Crash (Z.9895 stepcompress c=7 @ 1226.7s).

    Setup: print active, _last_mcu_flush_time = mcu_now - 163.2s.
    Erwartung: Override fires, Anchor-Submit laeuft, Override-
    Log emittiert, _last_idle_anchor_time wird auf mcu_now
    aktualisiert.
    """
    _, feeder = make_auto_feeder(print_state='printing')
    neutralize_bang_bang(monkeypatch, feeder)
    calls = count_anchor_calls(monkeypatch, feeder)

    mcu_now = 1226.7
    feeder.reactor.now = mcu_now
    feeder._last_mcu_flush_time = mcu_now - 163.2
    feeder._last_move_end_time = 1063.5
    feeder._last_idle_anchor_time = 1063.5

    with caplog.at_level(logging.INFO, logger=""):
        feeder._main_tick(eventtime=mcu_now)

    # 1. Override muss feuern.
    assert len(calls) == 1, (
        "P7-78 Eifel: 163.2s Funkstille MUSS Override + Anchor "
        "triggern. Got %d anchor-calls." % len(calls))

    # 2. Override-Log emittiert.
    override_logs = [r for r in caplog.records
                     if "P7-78" in r.getMessage()
                     and "stale override" in r.getMessage()]
    assert override_logs, (
        "P7-78 Eifel: Override-Log MUSS emittiert sein. Got 0.")

    # 3. _last_idle_anchor_time advanced.
    assert feeder._last_idle_anchor_time == pytest.approx(
        mcu_now, abs=0.01), (
        "P7-78 Eifel: _last_idle_anchor_time muss auf mcu_now "
        "advancen. Got %.3f." % feeder._last_idle_anchor_time)


def test_p778_flush_callback_timestamp_set_before_returns(monkeypatch):
    """Direct unit-test fuer _on_mcu_flush Timestamp-Tracking:
    Setze use_flush_callback_bang_bang=False (Early-Return-Pfad)
    und rufe _on_mcu_flush direkt — der Timestamp _last_mcu_flush_-
    time MUSS trotz Early-Return getrackt werden.

    Begruendung: Auch wenn der Feeder bang-bang deaktiviert hat,
    laeuft die LLL_PLUS-MCU weiter Steps. Das ist der "MCU lebt"-
    Beleg fuer den Override.
    """
    _, feeder = make_auto_feeder(print_state='printing')
    # Disable use_flush_callback_bang_bang -> Early-Return Pfad in
    # _on_mcu_flush.
    feeder.use_flush_callback_bang_bang = False
    # Sanity: Timestamp ist initial 0.0.
    assert feeder._last_mcu_flush_time == 0.0

    feeder._on_mcu_flush(flush_time=42.5, step_gen_time=42.5)

    assert feeder._last_mcu_flush_time == 42.5, (
        "P7-78: _on_mcu_flush MUSS _last_mcu_flush_time tracken "
        "auch bei Early-Return (use_flush_callback_bang_bang=False). "
        "Got %.3f." % feeder._last_mcu_flush_time)

    # Sub-check: auch bei _bang_bang_suspended Early-Return.
    feeder.use_flush_callback_bang_bang = True
    feeder._bang_bang_suspended = True
    feeder._on_mcu_flush(flush_time=99.9, step_gen_time=99.9)
    assert feeder._last_mcu_flush_time == 99.9, (
        "P7-78: _on_mcu_flush MUSS _last_mcu_flush_time tracken "
        "auch bei _bang_bang_suspended-Early-Return. Got %.3f."
        % feeder._last_mcu_flush_time)


def test_p778_override_log_emitted(monkeypatch, caplog):
    """Override-Pfad emittiert 'print-block stale override'-Log
    einmal pro Override-Trip.
    """
    _, feeder = make_auto_feeder(print_state='printing')
    neutralize_bang_bang(monkeypatch, feeder)
    count_anchor_calls(monkeypatch, feeder)

    feeder.reactor.now = 30.0
    feeder._last_mcu_flush_time = 30.0 - 15.0  # 15s stille > 10s
    feeder._last_move_end_time = 0.0
    feeder._last_idle_anchor_time = 0.0

    with caplog.at_level(logging.INFO, logger=""):
        feeder._main_tick(eventtime=30.0)

    override_logs = [r for r in caplog.records
                     if "print-block stale override" in r.getMessage()
                     and "P7-78" in r.getMessage()]
    assert len(override_logs) == 1, (
        "P7-78: Override-Log MUSS genau einmal pro Override-Trip "
        "emittiert sein. Got %d." % len(override_logs))


# ===========================================================================
# P7-78v2: Codex-Verify Q4+Q5 Lücke — forced_t0 im Override-Pfad
# ===========================================================================


def _own_trapq_appends(motion_q, feeder, start_index):
    """Subset der trapq.append_calls die der Feeder-Trapq gehören."""
    return [c for c in motion_q.append_calls[start_index:]
            if c[0] is feeder.trapq]


def test_p778v2_forced_t0_branch_bypasses_p777_b_skip(monkeypatch, caplog):
    """P7-78v2 Codex-Verify Q4+Q5 Unit: verifiziert dass ein Anchor-
    Submit mit forced_t0=mcu_now+lead_time den P7-77 B SKIP-Pfad
    (Z.3275 else-Branch) NICHT triggert und stattdessen einen
    realen trapq.append produziert — auch wenn toolhead.get_last_-
    move_time() weit-zukuenftig ist (active-print Bedingung).

    PRE-FIX (P7-78 v1): Override rief `_submit_anchor_move()` ohne
    forced_t0 -> Fall in forced_t0==None else-Branch (Z.3248) ->
    th_time=mcu_now+8s -> t0=mcu_now+8.3s -> P7-77 B SKIP -> return
    ohne trapq.append. Override-Log emittiert, ABER kein Submit ->
    last_step_clock bleibt stale -> Bug wirkungslos.

    POST-FIX (P7-78v2): forced_t0 kwarg im Override-Pfad -> Submit
    geht in forced_t0!=None Branch (Z.3203) -> P7-73 Clamp greift
    nicht (mcu_now+0.3 < cap mcu_now+2.0) -> realer trapq.append.

    Test bypasst _main_tick-Gate-Komplexitaet (HALL-Debounce,
    print_stats, enable_stepper-Side-Effects) und ruft die Anchor-
    Submit-Pfade direkt — wir wollen die Code-Semantik (forced_t0
    Branch != else-Branch) verifizieren, nicht den Tick-Driver.
    """
    printer, feeder = make_auto_feeder(print_state='printing')
    motion_q = printer.lookup_object('motion_queuing')
    toolhead = printer.lookup_object('toolhead')

    mcu_now = 30.0
    feeder.reactor.now = mcu_now
    feeder._last_move_end_time = 0.0
    feeder._last_enable_schedule_time = 0.0
    feeder._stepcompress_primed = True
    feeder._current_move = None
    # KRITISCH: aktiver Print hat Toolhead-Queue weit voraus —
    # genau die Bedingung die P7-77 B im else-Branch ausloest.
    toolhead.last_move_time = mcu_now + 8.0

    # --- Sub-1: Pre-fix-Simulation — Anchor OHNE forced_t0 (else-
    # Branch) MUSS durch P7-77 B SKIP abgefangen werden, kein
    # trapq.append. Das ist der Zustand den P7-78 v1 produzierte.
    appends_before = len(motion_q.append_calls)
    with caplog.at_level(logging.WARNING, logger=""):
        feeder.sync._submit_anchor_move()  # ohne forced_t0
    own_v1 = _own_trapq_appends(motion_q, feeder, appends_before)
    assert len(own_v1) == 0, (
        "P7-78 v1 baseline: Anchor ohne forced_t0 + far-future "
        "th_time MUSS durch P7-77 B SKIP abgefangen werden. "
        "Got %d trapq.appends." % len(own_v1))
    skip_warns = [r for r in caplog.records
                  if "P7-77 B" in r.getMessage()
                  and "anchor skipped" in r.getMessage()]
    assert skip_warns, (
        "P7-78 v1 baseline: P7-77 B SKIP-warning MUSS emittiert "
        "sein. Got 0.")
    caplog.clear()

    # --- Sub-2: P7-78v2 Fix — Anchor MIT forced_t0=mcu_now+lead_time
    # geht in forced_t0!=None Branch, kein SKIP, realer trapq.append.
    feeder._last_move_end_time = 0.0  # reset (P7-77 B clamped lme)
    appends_before = len(motion_q.append_calls)
    with caplog.at_level(logging.WARNING, logger=""):
        feeder.sync._submit_anchor_move(
            forced_t0=mcu_now + feeder.lead_time)
    own_v2 = _own_trapq_appends(motion_q, feeder, appends_before)
    assert len(own_v2) >= 1, (
        "P7-78v2 fix: Anchor MIT forced_t0=mcu_now+lead_time MUSS "
        "realen trapq.append produzieren (kein P7-77 B SKIP). "
        "Got %d." % len(own_v2))
    skip_warns_v2 = [r for r in caplog.records
                     if "P7-77 B" in r.getMessage()
                     and "anchor skipped" in r.getMessage()]
    assert not skip_warns_v2, (
        "P7-78v2 fix: forced_t0-Pfad darf KEIN P7-77 B SKIP "
        "triggern. Got %d skip-warnings." % len(skip_warns_v2))


def test_p778v2_non_override_path_unchanged_no_forced_t0(monkeypatch):
    """Regression-Guard: der nicht-Override-Pfad (P7-77 C, print_state
    != 'printing') ruft `_submit_anchor_move()` weiterhin OHNE kwarg.
    P7-78v2 darf KEIN API-Breaking-Change am Default-Pfad sein.
    """
    _, feeder = make_auto_feeder(print_state='standby')
    neutralize_bang_bang(monkeypatch, feeder)

    # Spy mit Argument-Capture.
    captured_kwargs = []

    def _spy(**kwargs):
        captured_kwargs.append(kwargs)
        return 1.0

    monkeypatch.setattr(feeder.sync, "_submit_anchor_move", _spy)

    feeder.reactor.now = 30.0
    feeder._last_mcu_flush_time = 0.0  # boot-state — kein Override
    feeder._last_move_end_time = 0.0
    feeder._last_idle_anchor_time = 0.0

    feeder._main_tick(eventtime=30.0)

    assert len(captured_kwargs) == 1, (
        "P7-78v2 regression: standby-Pfad muss Anchor genau einmal "
        "rufen. Got %d." % len(captured_kwargs))
    assert captured_kwargs[0] == {}, (
        "P7-78v2 regression: standby-Pfad darf KEIN forced_t0 kwarg "
        "uebergeben (kein API-Breaking-Change). Got %r."
        % captured_kwargs[0])


def test_p778v2_override_path_passes_forced_t0_kwarg(monkeypatch):
    """Verifiziert: Override-Pfad (print_state='printing' + stale
    flush) ruft `_submit_anchor_move(forced_t0=mcu_now + lead_time)`.
    """
    _, feeder = make_auto_feeder(print_state='printing')
    neutralize_bang_bang(monkeypatch, feeder)

    captured_kwargs = []

    def _spy(**kwargs):
        captured_kwargs.append(kwargs)
        return 1.0

    monkeypatch.setattr(feeder.sync, "_submit_anchor_move", _spy)

    mcu_now = 30.0
    feeder.reactor.now = mcu_now
    feeder._last_mcu_flush_time = mcu_now - 15.0  # > 10s -> Override
    feeder._last_move_end_time = 0.0
    feeder._last_idle_anchor_time = 0.0

    feeder._main_tick(eventtime=mcu_now)

    assert len(captured_kwargs) == 1, (
        "P7-78v2: Override muss Anchor genau einmal rufen. Got %d."
        % len(captured_kwargs))
    assert 'forced_t0' in captured_kwargs[0], (
        "P7-78v2: Override-Pfad MUSS forced_t0 kwarg uebergeben. "
        "Got %r." % captured_kwargs[0])
    # forced_t0 ≈ mcu_now + lead_time (FakeMCU.estimated_print_time
    # liefert eventtime, also = reactor.now). lead_time=0.3 default.
    expected = mcu_now + feeder.lead_time
    assert captured_kwargs[0]['forced_t0'] == pytest.approx(
        expected, abs=0.05), (
        "P7-78v2: forced_t0 muss mcu_now + lead_time sein. "
        "Got %.3f, expected %.3f."
        % (captured_kwargs[0]['forced_t0'], expected))


def test_p778v3_forced_t0_clamped_by_max_lookahead(monkeypatch):
    """P7-78v3 (Codex-Verify MEDIUM): lead_time wird im Override-Pfad
    auf MAX_FORCED_T0_LOOKAHEAD (2.0s) geclampt. Schuetzt vor un-
    gewoehnlich grosser lead_time (via BUFFER_SET) die sonst den
    P7-73 Clamp-Pfad triggern wuerde."""
    _, feeder = make_auto_feeder(print_state='printing')
    neutralize_bang_bang(monkeypatch, feeder)
    # Ungewoehnlich grosser lead_time (BUFFER_SET erlaubt das mit
    # einem Warn-Log).
    feeder.lead_time = 3.5

    captured_kwargs = []

    def _spy(**kwargs):
        captured_kwargs.append(kwargs)
        return 1.0

    monkeypatch.setattr(feeder.sync, "_submit_anchor_move", _spy)

    mcu_now = 30.0
    feeder.reactor.now = mcu_now
    feeder._last_mcu_flush_time = mcu_now - 15.0
    feeder._last_move_end_time = 0.0
    feeder._last_idle_anchor_time = 0.0

    feeder._main_tick(eventtime=mcu_now)

    assert len(captured_kwargs) == 1
    # Erwartet: mcu_now + min(3.5, 2.0) = mcu_now + 2.0
    expected = mcu_now + 2.0
    assert captured_kwargs[0]['forced_t0'] == pytest.approx(
        expected, abs=0.05), (
        "P7-78v3: forced_t0 muss auf mcu_now + 2.0s geclampt sein "
        "wenn lead_time > MAX_FORCED_T0_LOOKAHEAD. "
        "Got %.3f, expected %.3f."
        % (captured_kwargs[0]['forced_t0'], expected))
    # Negativ-Probe: lead_time selber bleibt unveraendert (kein
    # destruktiver Side-Effect am Plugin-State).
    assert feeder.lead_time == 3.5
