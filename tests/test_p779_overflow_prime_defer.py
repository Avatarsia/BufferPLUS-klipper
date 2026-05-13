"""P7-79 — Overflow-Prime Defer gegen Position-Mismatch-Crash (Issue #29).

Eifel-Joe Hardware-Reproduktion 2026-05-13 (~24 mm^3/s):
  print_time=817.590s, buffer_time=1.071
  stepcompress o=0 i=0 c=14 a=0: Invalid sequence
  Error in syncemitter 'mellow' step generation

Analyse (Code-Trace, verifiziert gegen buffer_feeder.py):

  1. Bang-Bang submittet Chunk M1 (start_pos=0, end_pos=9 mm) bei t0=T,
     lme = T + 0.13 s. (`_on_mcu_flush` Z.2647-2651)

  2. HALL1 OVERFLOW -> `_enter_overflow` -> `_halt_motion`
     (Z.1719) + `_schedule_stepper_disable` (Z.1720). Da Move noch
     in-flight, deferred Disable; `_pending_disable=True`.

  3. Im naechsten _main_tick mit M1 abgelaufen (zeit-basiert):
     `if self._pending_disable and not self._move_in_flight():`
     -> `_disable_stepper` -> `_stepcompress_primed = False`
     (Z.1915-1917 + Z.2949).

  4. Kritisch: itersolve hat M1 noch NICHT vollstaendig prozessiert
     wenn step_gen_time < M1.end_time. `_move_in_flight()` returnt
     False (Z.3393: now_pt < end_time, mit now_pt = mcu wall_clock),
     aber Step-Generation fuer M1 ist noch pending in itersolve.

  5. HALL1 cleared -> `_resume_after_overflow` -> `_enable_stepper`
     -> `_needs_overflow_prime = True` (Z.1772).

  6. Naechster `_on_mcu_flush` (step_gen_time < lme von M1!):
     `_needs_overflow_prime`-Block (Z.2504-2518) feuert
     `_submit_move(0.05, ..., forced_t0=step_gen_time + lead_time)`.

  7. In `_submit_single_trapezoid` Z.3138: `need_reprime = not
     self._stepcompress_primed = True`. Im `forced_t0!=None`-Pfad
     wird `flush_step_generation()` uebersprungen, aber
     `self.stepper.set_position((0,0,0))` laeuft (Z.3148)
     -> itersolve_pos = 0.

  8. `trapq_append(M2 prime: start=0, end=0.05, t0=anchor)` Z.3367.

  9. Gleicher `_advance_flush_time`-Call in Klipper:
     `itersolve_gen_steps` prozessiert M1 (start=0, end=9)
     -> itersolve_pos = 9. Dann M2 (start=0, end=0.05) — itersolve
     war bei 9, M2 sagt start=0 -> catch-up REVERSE 9->0 = ~14 Steps
     auf demselben Clock -> c=14 i=0 Invalid sequence.

Fix P7-79
---------
Im `_on_mcu_flush` direkt nach den drei existierenden Early-Returns
(`use_flush_callback_bang_bang`, `_bang_bang_suspended`,
`state != STATE_AUTO`) und VOR dem `_needs_overflow_prime`-Block
einen Defer einbauen, der genau das dirty Window erkennt:

    # P7-79: Defer flush-callback submits when post-disable reprime
    # would race with itersolve still processing the pre-disable
    # move. _move_in_flight() ist zeit-basiert; itersolve kann
    # hinterherhinken (step_gen_time < lme). Ein set_position(0)
    # im need_reprime-Pfad waehrend itersolve M1 noch nicht
    # ausgespielt hat fuehrt zu Position-Mismatch c=N i=0 Crash.
    if (not self._stepcompress_primed
            and self._last_move_end_time > step_gen_time):
        return

Worst-case Defer-Delay: ~lead_time + Chunk-Duration. Bei
flush_callback_chunk_mm=15 und feed_speed=30 sind das ~0.5 s, plus
lead_time 0.3 s, also ~0.8 s. Eifel-Joe schaetzt ~125 ms — beides
ist im akzeptablen Bereich fuer Overflow-Recovery.

Tags: Eifel-Joe c=14, post-disable reprime race, itersolve lag,
overflow-prime defer.
"""

import pytest

from fakes_klipper import FakeConfig, FakePrinter
from klipper_extras import buffer_feeder


def set_sensor_active(feeder, sensor_name, active):
    polarity_flip = feeder._pin_polarity_flip[sensor_name]
    raw = (not active) if polarity_flip else active
    feeder._pin_stable_state[sensor_name] = raw
    feeder._pin_raw_state[sensor_name] = raw


def make_feeder(values=None):
    """Build feeder in STATE_AUTO, sensors quiescent."""
    base = {"use_flush_callback_bang_bang": True}
    if values:
        base.update(values)
    printer = FakePrinter()
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


def _own_trapq_appends(motion_q, feeder, start_index):
    """Subset der trapq.append_calls die dem Feeder-Trapq gehoeren."""
    return [c for c in motion_q.append_calls[start_index:]
            if c[0] is feeder.trapq]


# ===========================================================================
# P7-79: Core defer behaviour
# ===========================================================================


def test_p779_defer_when_stepcompress_dirty():
    """Dirty State: _stepcompress_primed=False + _last_move_end_time
    > step_gen_time. _on_mcu_flush mit aktivem _needs_overflow_prime
    MUSS deferren (kein trapq.append, _needs_overflow_prime bleibt
    True fuer den naechsten Tick).
    """
    printer, feeder = make_feeder()
    motion_q = printer.lookup_object('motion_queuing')

    # Dirty Window: M1 noch in itersolve, _disable_stepper hat
    # _stepcompress_primed geclearrt, _resume_after_overflow setzt
    # _needs_overflow_prime.
    feeder._stepcompress_primed = False
    feeder._last_move_end_time = 10.13   # M1 end_time
    feeder._needs_overflow_prime = True
    set_sensor_active(feeder, 'hall_empty', True)

    # step_gen_time = 10.00 (< lme=10.13) -> itersolve laeuft hinterher.
    appends_before = len(motion_q.append_calls)
    feeder._on_mcu_flush(flush_time=10.00, step_gen_time=10.00)

    own = _own_trapq_appends(motion_q, feeder, appends_before)
    assert len(own) == 0, (
        "P7-79: Defer MUSS trapq.append unterdruecken im dirty Window. "
        "Got %d." % len(own))
    assert feeder._needs_overflow_prime is True, (
        "P7-79: _needs_overflow_prime MUSS True bleiben fuer naechsten "
        "Tick (Defer != Cancel).")


def test_p779_no_defer_when_primed():
    """Sauberer State: _stepcompress_primed=True. Defer-Guard greift
    NICHT, normaler Flow laeuft. Regression-Guard fuer den healthy
    Streaming-Pfad."""
    printer, feeder = make_feeder()
    motion_q = printer.lookup_object('motion_queuing')

    feeder._stepcompress_primed = True
    feeder._last_move_end_time = 10.13   # would defer if not primed
    feeder._needs_overflow_prime = True
    set_sensor_active(feeder, 'hall_empty', True)

    appends_before = len(motion_q.append_calls)
    feeder._on_mcu_flush(flush_time=10.00, step_gen_time=10.00)

    own = _own_trapq_appends(motion_q, feeder, appends_before)
    # Overflow-prime fires (0.05mm submit) and clears the flag.
    assert len(own) == 1, (
        "P7-79: primed=True darf NICHT deferren — Overflow-Prime "
        "Submit MUSS feuern. Got %d." % len(own))
    assert feeder._needs_overflow_prime is False, (
        "P7-79: primed=True Pfad muss _needs_overflow_prime clearen "
        "(normaler Flow).")


def test_p779_no_defer_when_clean_lme():
    """Edge: _stepcompress_primed=False ABER _last_move_end_time <=
    step_gen_time (itersolve hat voll aufgeholt). Defer greift NICHT
    — set_position(0) ist safe weil keine pending steps in itersolve.
    """
    printer, feeder = make_feeder()
    motion_q = printer.lookup_object('motion_queuing')

    feeder._stepcompress_primed = False
    feeder._last_move_end_time = 9.50   # itersolve hat aufgeholt
    feeder._needs_overflow_prime = True
    set_sensor_active(feeder, 'hall_empty', True)

    appends_before = len(motion_q.append_calls)
    feeder._on_mcu_flush(flush_time=10.00, step_gen_time=10.00)

    own = _own_trapq_appends(motion_q, feeder, appends_before)
    assert len(own) == 1, (
        "P7-79: not primed + lme <= step_gen_time darf NICHT deferren "
        "— itersolve hat M1 voll prozessiert. Got %d." % len(own))
    assert feeder._needs_overflow_prime is False


def test_p779_defer_resolves_after_step_gen_advance():
    """Sequenz-Test: Erst defer (lme > step_gen_time), dann naechster
    Tick mit advanced step_gen_time -> Submit feuert. _needs_overflow_-
    prime ueberlebt den Defer.
    """
    printer, feeder = make_feeder()
    motion_q = printer.lookup_object('motion_queuing')

    feeder._stepcompress_primed = False
    feeder._last_move_end_time = 10.13
    feeder._needs_overflow_prime = True
    set_sensor_active(feeder, 'hall_empty', True)

    # Tick 1: defer.
    appends_before = len(motion_q.append_calls)
    feeder._on_mcu_flush(flush_time=10.00, step_gen_time=10.00)
    assert len(_own_trapq_appends(motion_q, feeder, appends_before)) == 0
    assert feeder._needs_overflow_prime is True

    # Tick 2: step_gen_time ist jetzt 10.20 > lme -> kein Defer mehr.
    appends_before = len(motion_q.append_calls)
    feeder._on_mcu_flush(flush_time=10.20, step_gen_time=10.20)
    own = _own_trapq_appends(motion_q, feeder, appends_before)
    assert len(own) == 1, (
        "P7-79: Nach step_gen_advance MUSS der deferrte Submit "
        "feuern. Got %d." % len(own))
    assert feeder._needs_overflow_prime is False


def test_p779_slow_overflow_recovery_no_race():
    """Lange Pause zwischen overflow-Recovery und naechstem flush.
    step_gen_time ist weit nach lme -> kein false Defer.
    """
    printer, feeder = make_feeder()
    motion_q = printer.lookup_object('motion_queuing')

    feeder._stepcompress_primed = False
    feeder._last_move_end_time = 10.13
    feeder._needs_overflow_prime = True
    set_sensor_active(feeder, 'hall_empty', True)

    # Pause: step_gen_time = 25.0, weit nach lme.
    appends_before = len(motion_q.append_calls)
    feeder._on_mcu_flush(flush_time=25.00, step_gen_time=25.00)
    own = _own_trapq_appends(motion_q, feeder, appends_before)
    assert len(own) == 1, (
        "P7-79: Slow-Recovery (step_gen_time >> lme) darf NICHT "
        "deferren. Got %d." % len(own))


def test_p779_overflow_prime_pending_defer_blocks_submit():
    """Direct: _needs_overflow_prime=True + dirty -> der Submit aus
    Z.2517 (_submit_move(0.05, ..., forced_t0=anchor)) MUSS durch
    Defer verhindert werden. _stepcompress_primed bleibt False
    (kein set_position(0) wurde gerufen).
    """
    printer, feeder = make_feeder()
    motion_q = printer.lookup_object('motion_queuing')

    feeder._stepcompress_primed = False
    feeder._last_move_end_time = 10.13
    feeder._needs_overflow_prime = True
    # _commanded_pos absichtlich != 0 setzen — wenn der Defer NICHT
    # greift, ruft Z.3148 set_position((0,0,0)) und setzt
    # _commanded_pos = 0.0 (Z.3149).
    feeder._commanded_pos = 9.0
    set_sensor_active(feeder, 'hall_empty', True)

    feeder._on_mcu_flush(flush_time=10.00, step_gen_time=10.00)

    assert feeder._stepcompress_primed is False, (
        "P7-79: Defer MUSS verhindern dass set_position(0) lauft "
        "und _stepcompress_primed setzt.")
    assert feeder._commanded_pos == 9.0, (
        "P7-79: Defer MUSS verhindern dass _commanded_pos auf 0 "
        "resetted wird (kein set_position(0)). Got %.3f."
        % feeder._commanded_pos)


def test_p779_eifel_c14_reproduction():
    """Eifel-Hardware-Pattern: M1 in itersolve, _disable_stepper
    geclearrt primed, _resume_after_overflow setzt
    _needs_overflow_prime. _on_mcu_flush mit step_gen_time < lme
    -> MUSS deferren. Verifiziert per stepper.position dass kein
    set_position((0,0,0)) lief.
    """
    printer, feeder = make_feeder()
    stepper = feeder.stepper

    # Stage M1 als laufender Streaming-Chunk.
    feeder._stepcompress_primed = True
    feeder._last_move_end_time = 10.13
    feeder._commanded_pos = 9.0
    stepper.set_position((9.0, 0.0, 0.0))

    # Schritt 1: HALL1 OVERFLOW -> _enter_overflow -> _halt_motion +
    # _schedule_stepper_disable (deferred). Wir simulieren das
    # direkt am State.
    feeder._pending_disable = True
    feeder._continuous_feed = False

    # Schritt 2: _main_tick sieht M1 zeit-basiert vorbei (now > end)
    # und ruft _disable_stepper -> primed = False.
    # Wir simulieren das ohne den ganzen tick zu fahren.
    feeder._disable_stepper()
    assert feeder._stepcompress_primed is False  # Sanity

    # Schritt 3: HALL1 cleared -> _resume_after_overflow ->
    # _needs_overflow_prime = True. Direkt-Sim.
    feeder._needs_overflow_prime = True

    # Schritt 4: itersolve laeuft noch hinterher — step_gen_time
    # noch < lme (Eifels Pattern: lme=10.13, step_gen_time=10.05).
    feeder._on_mcu_flush(flush_time=10.05, step_gen_time=10.05)

    # Erwartung: kein set_position((0,0,0)) wurde gerufen — sonst
    # ginge stepper.position auf (0,0,0).
    assert stepper.position == (9.0, 0.0, 0.0), (
        "P7-79 Eifel-Repro: stepper.position MUSS unveraendert "
        "bleiben (kein set_position(0) im Defer-Pfad). "
        "Got %r." % (stepper.position,))
    assert feeder._needs_overflow_prime is True, (
        "P7-79 Eifel-Repro: _needs_overflow_prime MUSS True bleiben "
        "fuer naechsten Tick.")


# ===========================================================================
# P7-79b: Codex-Verify HIGH (v1) — P7-74-Clamp deckt `_last_move_end_time`
#         hinter dem echten itersolve-Horizont auf. Anker MUSS via
#         `_current_move['end_time']` validiert werden, nicht via lme.
# ===========================================================================


def test_p779b_halt_motion_p774_clamp_defer():
    """P7-79b Codex-Verify HIGH-Finding (v1 -> v2):

    P7-74 (`_halt_motion`, buffer_feeder.py Z.3513-3514) clampt
    `_last_move_end_time` auf `mcu_now` waehrend mid-flight Overflow,
    laesst aber `_current_move` intakt (Z.3452 explizite Doc-Garantie:
    "leave `_current_move` intact so `_move_in_flight` can still report
    accurately"). Ein _on_mcu_flush mit step_gen_time zwischen
    `mcu_now` (= geclamptes lme) und `_current_move['end_time']`
    (= echte Move-Ende-Zeit) wuerde den ALTEN `lme > step_gen_time`-
    Check umgehen und denselben Crash wieder oeffnen.

    Setup-Assumptions (FakeReactor/FakeMCU):
      - `reactor.now` wird vor `_halt_motion` auf 10.00 fixiert.
      - `FakeMCU.estimated_print_time(eventtime) = eventtime`, somit
        clampt `_halt_motion` lme auf ~10.00x.
      - `_current_move['end_time'] = 10.13` (echtes M1-Ende).
      - step_gen_time = 10.05 liegt im "klaffenden" Fenster:
            geclamptes lme (~10.00x) < step_gen_time (10.05)
                                     < _current_move.end (10.13)
        -> Alte Logik wuerde durchwinken (lme <= step_gen_time),
           neue Logik (itersolve_end = _current_move.end_time) deferrt.

    Erwartung (P7-79b GREEN):
      Defer greift, kein trapq.append durch unsere Trapq,
      `_needs_overflow_prime` bleibt True.

    PRE-REFACTOR (alte `lme > step_gen_time`-Logik):
      lme war auf mcu_now geclamped (=10.00x) < step_gen_time 10.05
      -> Defer greift NICHT -> Overflow-Prime-Submit feuert ->
      stepper.position wird durch set_position((0,0,0)) ueberschrieben.
      Dieser Test failt dann mit "stepper.position == (0,0,0)".
    """
    printer, feeder = make_feeder()
    motion_q = printer.lookup_object('motion_queuing')
    stepper = feeder.stepper

    # Stage M1 als laufender Streaming-Chunk: itersolve sieht echtes
    # Ende bei 10.13s, stepper.position auf 9mm vorgeschoben.
    feeder._stepcompress_primed = True
    feeder._last_move_end_time = 10.13
    feeder._commanded_pos = 9.0
    stepper.set_position((9.0, 0.0, 0.0))
    # `_current_move` muss intakt sein (so wie nach _submit_single_-
    # trapezoid Z.3424-3429).
    feeder._current_move = {
        'end_time': 10.13,
        'direction': 1,
        'distance': 9.0,
        'speed': 30.0,
    }

    # mcu_now auf 10.00 fixieren — _halt_motion clampt lme auf
    # ~10.00x (reactor.monotonic advanced bei jedem Call um 0.001s,
    # aber das Ergebnis liegt sicher in [10.00, 10.05)).
    printer.reactor.now = 10.00

    # Schritt 1: HALL1 OVERFLOW -> _enter_overflow -> _halt_motion +
    # _schedule_stepper_disable. _halt_motion clampt lme.
    feeder._halt_motion()
    feeder._pending_disable = True
    assert feeder._last_move_end_time < 10.05, (
        "Setup-Sanity: P7-74-Clamp MUSS lme auf mcu_now (~10.00x) "
        "gedrueckt haben, ansonsten testet der Test das falsche "
        "Szenario. Got lme=%.4f." % feeder._last_move_end_time)
    assert feeder._current_move is not None and \
        feeder._current_move['end_time'] == 10.13, (
        "Setup-Sanity: _current_move MUSS intakt sein (P7-74 Doc-"
        "Garantie Z.3452). Got %r." % (feeder._current_move,))

    # Schritt 2: _disable_stepper -> primed = False.
    feeder._disable_stepper()
    assert feeder._stepcompress_primed is False

    # Schritt 3: _resume_after_overflow -> _needs_overflow_prime.
    feeder._needs_overflow_prime = True
    set_sensor_active(feeder, 'hall_empty', True)

    # Schritt 4: _on_mcu_flush mit step_gen_time im klaffenden Fenster
    # zwischen geclamptem lme (~10.00x) und _current_move.end (10.13).
    appends_before = len(motion_q.append_calls)
    feeder._on_mcu_flush(flush_time=10.05, step_gen_time=10.05)

    # Erwartung P7-79b: Defer greift via _current_move.end_time-Anker.
    own = _own_trapq_appends(motion_q, feeder, appends_before)
    assert len(own) == 0, (
        "P7-79b: Defer MUSS via _current_move['end_time']-Anker "
        "greifen, auch wenn P7-74 lme auf mcu_now geclamped hat. "
        "Got %d trapq.append calls." % len(own))
    assert feeder._needs_overflow_prime is True, (
        "P7-79b: _needs_overflow_prime MUSS True bleiben (Defer != "
        "Cancel).")
    assert stepper.position == (9.0, 0.0, 0.0), (
        "P7-79b: stepper.position MUSS unveraendert bleiben (kein "
        "set_position(0) im Defer-Pfad). Got %r."
        % (stepper.position,))
    assert feeder._stepcompress_primed is False, (
        "P7-79b: _stepcompress_primed MUSS False bleiben (set_"
        "position(0) wurde durch Defer verhindert).")
