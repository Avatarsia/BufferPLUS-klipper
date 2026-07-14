"""idle_anchor_mode: silent — Watchdog ohne Anchor-Moves (Option 4).

Quellcode-Recherche Klipper-Mainline (2026-07-14, 2 Agenten +
Codex-Design-Review AGREE-WITH-CHANGES):
- Ein idle Stepper mit leerer stepcompress-Queue kann beim Background-
  Flush NICHT fehlschlagen (queue_flush returnt sofort).
- Der erste Step nach beliebig langer Stille laeuft automatisch durch
  den Far-Path (queue_append_far/flush_far, seit 2017; req_clock=
  first_clock haelt die 32-bit-Arithmetik gueltig) — so ueberleben
  X/Y/Z lange Idle-Phasen. Der periodische Anchor ist dafuer unnoetig.
- Die reale historische Crash-Klasse ist past-anchored t0 (t0 hinter
  last_step_clock -> uint32-Wrap -> "Invalid sequence"), abgedeckt
  durch die t0-Floors; im Silent-Modus kommt ein expliziter
  mcu_now+lead-Floor im forced_t0=None-First-Chunk-Branch dazu
  (stale th_time/lme nach Idle), mit FRISCHEM mcu_now nach dem ggf.
  blockierenden Reprime (Codex-Finding: flush_step_generation kann
  ~100ms+ dauern, ein vorher gelesenes mcu_now waere abgelaufen ->
  Timer too close).
- Silent behaelt die Idle-Disable-Semantik als One-shot (Latch,
  re-armed durch _enable_stepper) — ohne Latch wuerde jeder Tick
  _disable_stepper rufen und _last_enable_schedule_time fortschieben.
"""

import pytest
from fakes_klipper import FakeConfig, FakePrinter, FakePrintStats
from klipper_extras import buffer_feeder


def set_sensor_active(feeder, sensor_name, active):
    polarity_flip = feeder._pin_polarity_flip[sensor_name]
    raw = (not active) if polarity_flip else active
    feeder._pin_stable_state[sensor_name] = raw
    feeder._pin_raw_state[sensor_name] = raw


def make_feeder(values=None, state=buffer_feeder.STATE_IDLE,
                ps_state="standby"):
    printer = FakePrinter()
    printer.objects["print_stats"] = FakePrintStats(state=ps_state)
    feeder = buffer_feeder.BufferFeeder(
        FakeConfig(printer=printer, values=values))
    printer.fire_event('klippy:connect')
    feeder._startup_grace_done = True
    feeder._state = state
    for name in ('hall_overflow', 'hall_full', 'hall_empty', 'entrance'):
        set_sensor_active(feeder, name, False)
    feeder.reactor.now = 20.0
    feeder._last_move_end_time = 0.0
    return printer, feeder


def spies(monkeypatch, feeder):
    anchors, disables = [], []

    def _anchor_spy(**kw):
        anchors.append(kw)
        feeder._last_move_end_time = feeder.reactor.now + 0.001
        return 1.0
    monkeypatch.setattr(feeder.sync, "_submit_anchor_move", _anchor_spy)
    monkeypatch.setattr(feeder, "_schedule_stepper_disable",
                        lambda: disables.append('d'))
    return anchors, disables


SILENT = {'idle_anchor_mode': 'silent'}


def test_silent_idle_no_anchor_but_one_shot_disable(monkeypatch):
    _, feeder = make_feeder(values=dict(SILENT))
    anchors, disables = spies(monkeypatch, feeder)

    for t in (20.0, 20.02, 20.04, 35.0):
        feeder.reactor.now = t
        feeder._main_tick(eventtime=t)

    assert anchors == [], "silent mode must never submit anchor moves"
    assert disables == ['d'], (
        "disable must fire exactly ONCE (one-shot latch) — repeating "
        "it every tick would keep pushing _last_enable_schedule_time")


def test_silent_auto_disable_requires_idle_motor_disable(monkeypatch):
    _, feeder = make_feeder(
        values=dict(SILENT, idle_motor_disable=True),
        state=buffer_feeder.STATE_AUTO)
    anchors, disables = spies(monkeypatch, feeder)

    feeder._main_tick(eventtime=20.0)

    assert anchors == []
    assert disables == ['d']


def test_silent_auto_no_disable_during_print(monkeypatch):
    _, feeder = make_feeder(
        values=dict(SILENT, idle_motor_disable=True),
        state=buffer_feeder.STATE_AUTO, ps_state="printing")
    anchors, disables = spies(monkeypatch, feeder)

    feeder._main_tick(eventtime=20.0)

    assert anchors == [] and disables == []


def test_silent_latch_rearms_on_enable(monkeypatch):
    _, feeder = make_feeder(values=dict(SILENT))
    _, disables = spies(monkeypatch, feeder)

    feeder._main_tick(eventtime=20.0)
    assert disables == ['d']

    feeder._enable_stepper()  # neue Aktivitaet re-armt den Latch
    feeder.reactor.now = 40.0
    feeder._main_tick(eventtime=40.0)

    assert disables == ['d', 'd']


def test_move_mode_still_anchors(monkeypatch):
    _, feeder = make_feeder()  # default idle_anchor_mode='move'
    anchors, _ = spies(monkeypatch, feeder)

    feeder._main_tick(eventtime=20.0)

    assert len(anchors) == 1


def test_silent_first_chunk_floors_on_fresh_mcu_now():
    """Nach Idle: th_time/lme stale (0), Reprime-Flush dauert 0.2s.
    t0 muss auf dem FRISCHEN mcu_now (nach Flush) + lead floored sein."""
    printer, feeder = make_feeder(values=dict(SILENT))
    feeder._state = buffer_feeder.STATE_MANUAL_FEED
    feeder._stepcompress_primed = True  # gap>5s erzwingt Reprime trotzdem
    printer.objects['toolhead'].last_move_time = 0.0
    real_flush = printer.objects['toolhead'].flush_step_generation

    def _slow_flush():
        printer.reactor.now += 0.2  # Host-Last waehrend flush
        real_flush()
    printer.objects['toolhead'].flush_step_generation = _slow_flush

    feeder._submit_move(5.0, 25.0)

    mq = printer.objects['motion_queuing']
    t0 = mq.append_calls[-1][1]
    assert t0 >= 20.2 + feeder.lead_time - 1e-6, (
        "t0 must floor on the POST-reprime mcu_now — a pre-flush "
        "snapshot would already be in the past (Timer too close)")


def test_move_mode_first_chunk_keeps_legacy_anchor():
    """Regression-Guard: im move-Modus bleibt der First-Chunk-Anchor
    unveraendert (kein mcu_now-Floor — hardware-erprobtes Verhalten)."""
    printer, feeder = make_feeder()
    feeder._state = buffer_feeder.STATE_MANUAL_FEED
    feeder._stepcompress_primed = True
    feeder._last_move_end_time = 19.0  # gap < 5s: kein Reprime
    printer.objects['toolhead'].last_move_time = 0.0

    feeder._submit_move(5.0, 25.0)

    mq = printer.objects['motion_queuing']
    t0 = mq.append_calls[-1][1]
    # Non-Streaming-Submits sind schon heute via Enable-Floor
    # (_last_enable_schedule_time ~ now+lead, _submit_move Z.~2886)
    # gegen past-t0 gefloort — der silent-Floor ist Defense-in-depth.
    assert t0 >= 20.0, (
        "move mode: enable-floor keeps first chunk at/after now")
