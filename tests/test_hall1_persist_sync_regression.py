"""Hardware-Crash 2026-07-13 (klippy (2).log): "Timer too close" beim
LOAD_FILAMENT.

Kausalkette (Codex-verifiziert, Diagnose CONFIRMED):
1. f7059e0 armt den HALL1-Persist-Timestamp auch in Bypass-Kontexten
   (phase3_overflow_ok, synced) — gewollt, er trackt die Physik.
2. Die Persist-Eskalation in _main_tick prueft aber nur STATE_AUTO +
   Timestamp und rief _enter_overflow() DIREKT — ohne die Kontext-
   Matrix _is_hall1_active(MAIN_TICK), die synced/_post_load_overflow_
   grace/Overlay exempted. Implizite Invariante ("Timestamp nie im
   Bypass gesetzt") war gebrochen.
3. _enter_overflow -> _schedule_stepper_disable() auf einem Stepper,
   der an der EXTRUDER-Trapq haengt und aktiv Steps generiert (G1 E,
   buffer_time=19.7s). Der Deferral prueft nur own-trapq _current_move
   -> motor_disable landete 0.785s in der Vergangenheit (MCU-Dump:
   queue_digital_out clock 2773153752 vs shutdown 2810821033) ->
   "Timer too close".

Fix (Codex AGREE-WITH-CHANGES): Kontext-Matrix als ZUSAETZLICHE
Bedingung der Eskalation (kein Early-Return — Safety-Timeouts und
Deferred-Disable-Wartung des Ticks laufen weiter); Timestamp bleibt
armiert, damit die Eskalation nach UNSYNC (ohne Grace) sofort greift.

Defense-in-depth (separater Commit): _schedule_stepper_disable und
der Pending-Disable-Tick sind bei _stepper_synced_to != None gesperrt.
"""

import pytest
from fakes_klipper import FakeConfig, FakePrinter
from klipper_extras import buffer_feeder


def set_sensor_active(feeder, sensor_name, active):
    polarity_flip = feeder._pin_polarity_flip[sensor_name]
    raw = (not active) if polarity_flip else active
    feeder._pin_stable_state[sensor_name] = raw
    feeder._pin_raw_state[sensor_name] = raw


def make_persist_feeder():
    """AUTO-Feeder mit aktivem HALL1 und abgelaufenem Persist-Fenster
    (Timestamp 0, reactor.now 10 >> hall1_persist_timeout 2s)."""
    printer = FakePrinter()
    config = FakeConfig(printer=printer)
    feeder = buffer_feeder.BufferFeeder(config)
    printer.fire_event('klippy:connect')
    feeder._startup_grace_done = True
    feeder._state = buffer_feeder.STATE_AUTO
    set_sensor_active(feeder, 'hall_overflow', True)
    set_sensor_active(feeder, 'hall_full', False)
    set_sensor_active(feeder, 'hall_empty', False)
    set_sensor_active(feeder, 'entrance', True)
    feeder._hall1_active_since = 0.0
    printer.reactor.now = 10.0
    return printer, feeder


def enter_overflow_spy(monkeypatch, feeder):
    calls = []
    monkeypatch.setattr(feeder, "_enter_overflow",
                        lambda: calls.append('enter_overflow'))
    return calls


# ---------------------------------------------------------------------------
# Root-Cause-Fix: Persist-Eskalation respektiert die MAIN_TICK-Matrix
# ---------------------------------------------------------------------------


def test_persist_escalation_suppressed_while_synced(monkeypatch):
    """Das Crash-Szenario: Persist-Timeout laeuft ab, WAEHREND der
    Stepper an der Extruder-Trapq haengt (LOAD Phase 3/3). Eskalation
    muss unterdrueckt sein — _enter_overflow wuerde ein Disable in
    fremde in-flight Steps schedulen (Timer too close)."""
    _, feeder = make_persist_feeder()
    feeder._stepper_synced_to = 'extruder'
    calls = enter_overflow_spy(monkeypatch, feeder)

    feeder._main_tick(eventtime=10.0)

    assert calls == [], (
        "persist escalation must respect the MAIN_TICK bypass matrix — "
        "entering OVERFLOW mid-SYNC schedules a motor_disable into "
        "in-flight extruder steps (hardware crash 2026-07-13)")
    assert feeder._hall1_active_since is not None, (
        "timestamp must stay armed so the escalation fires after UNSYNC")


def test_persist_escalation_suppressed_by_post_load_grace(monkeypatch):
    """Nach LOAD mit OVERFLOW_OK=1 ist der Buffer LEGITIM uebervoll —
    _post_load_overflow_grace unterdrueckt den Main-Tick-Re-Trigger,
    und das muss auch fuer die Persist-Eskalation gelten."""
    _, feeder = make_persist_feeder()
    feeder._post_load_overflow_grace = True
    calls = enter_overflow_spy(monkeypatch, feeder)

    feeder._main_tick(eventtime=10.0)

    assert calls == []


def test_persist_escalates_when_not_bypassed(monkeypatch):
    """Regression-Guard: ohne Sync/Grace/Overlay eskaliert der
    abgelaufene Persist weiterhin sofort (Hardware-Safety bei
    mechanisch stuck buffer, C-cont T6). Deckt auch den Moment
    direkt nach UNSYNC ab (Timestamp blieb armiert)."""
    _, feeder = make_persist_feeder()
    calls = enter_overflow_spy(monkeypatch, feeder)

    feeder._main_tick(eventtime=10.0)

    assert calls == ['enter_overflow']


# ---------------------------------------------------------------------------
# Defense-in-depth: kein Stepper-Disable waehrend Sync
# ---------------------------------------------------------------------------


def test_schedule_stepper_disable_noop_while_synced(monkeypatch):
    _, feeder = make_persist_feeder()
    feeder._stepper_synced_to = 'extruder'
    disables = []
    monkeypatch.setattr(feeder, "_disable_stepper",
                        lambda: disables.append('disable'))

    feeder._schedule_stepper_disable()

    assert disables == [], (
        "motor_disable while bound to the extruder trapq lands inside "
        "foreign in-flight steps — Timer too close")
    assert feeder._pending_disable is False, (
        "no deferred disable either — the deferral guard only watches "
        "own-trapq moves and would fire mid-sync")


def test_pending_disable_tick_deferred_while_synced(monkeypatch):
    """Ein bereits pendendes Disable (vor dem SYNC armiert) darf im
    Tick nicht feuern, solange der Sync steht; nach dem Unsync laeuft
    es regulaer."""
    _, feeder = make_persist_feeder()
    set_sensor_active(feeder, 'hall_overflow', False)  # kein Persist-Pfad
    feeder._hall1_active_since = None
    feeder._pending_disable = True
    feeder._stepper_synced_to = 'extruder'
    disables = []
    monkeypatch.setattr(feeder, "_disable_stepper",
                        lambda: disables.append('disable'))

    feeder._main_tick(eventtime=10.0)
    assert disables == []
    assert feeder._pending_disable is True

    feeder._stepper_synced_to = None
    feeder._main_tick(eventtime=10.1)
    assert disables == ['disable']
