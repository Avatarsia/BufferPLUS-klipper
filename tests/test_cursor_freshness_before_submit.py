"""Cursor-Freshness-Contract — garantierte last_step_clock-Frische vor
jedem streaming-Submit aus _on_mcu_flush.

Hardware-Beleg (2026-05-14 klippy_refactor_*_repro.log):

  buffer_feeder: auto anchor fired (4× in der Idle-Phase mit hall_empty=True,
                                    Watchdog-Fix aus PR #39 wirkt)
  ... 8× buffer_metrics (idle continues)
  Starting SD card print (position 0)
  buffer_event[flush_submit]: anchor=58.698 move_active=False chunk=9.000
  SET_KINEMATIC_POSITION pos=0.000,0.000,0.000 set_homed=xyz
  → MCU 'LLL_PLUS' shutdown: Timer too close

Send-Queue-Beleg: queue_step oid=0 interval=332180349 count=1 add=0
  (6.92s zwischen letztem Watchdog-Anchor und erstem Submit-Step). Mit
  USB-Sende-Latenz + MCU-busy (TMC-UART-Storm in derselben Send-Sequenz)
  landet der erste Step in der Vergangenheit relativ zur MCU-Clock zum
  Receive-Zeitpunkt → Timer too close.

Wurzel: _on_mcu_flush hat keinen Vertrag wie alt last_step_clock sein
darf, bevor ein streaming-Submit ausgeloest wird. Watchdog feuert alle
~10s als best-effort, schliesst aber das Race-Window nicht. Wenn
streaming-Demand zwischen zwei Watchdog-Anchors landet (z.B. PRINT_START
oeffnet das Idle-Suppression-Gate via state='printing'), schickt
_flush_submit_streaming_chunk einen Move auf einen 7-10s alten Cursor.

Fix-Vertrag (Phase 3 TDD): _flush_submit_streaming_chunk sichert vor
JEDEM eigentlichen Streaming-Submit einen garantierten anchor-Step,
wenn _last_move_end_time aelter als MIN_CURSOR_FRESHNESS_S ist und kein
move_in_flight laeuft. Effekt: queue_step interval ist nach dem
Pre-Anchor << 1s — keine Race-Anfaelligkeit mehr fuer Timer-too-close
in dieser Wurzel-Klasse.

Komplementaer zu:
  - PR #39 (Watchdog feuert bei hall_empty=True im flush_callback-Pfad)
    schuetzt vor Idle-Stale-Cursor in STATE_AUTO ausserhalb von Prints.
  - Maintainer 97e97e7 (sanitize_forced_t0_floors) schuetzt vor
    stale-Future-Floors die forced_t0 ueberschreiben.

Defense-in-depth — drei orthogonale Schutzschichten.
"""

import pytest

from fakes_klipper import FakeConfig, FakePrinter, FakePrintStats
from klipper_extras import buffer_feeder
from klipper_extras._buffer_common import (
    ANCHOR_NUDGE_MM, MIN_CURSOR_FRESHNESS_S,
)


# ---------------------------------------------------------------------------
# Helpers (Pattern aus test_streaming_pipeline_auto.py)
# ---------------------------------------------------------------------------


def set_sensor_active(feeder, sensor_name, active):
    polarity_flip = feeder._pin_polarity_flip[sensor_name]
    raw = (not active) if polarity_flip else active
    feeder._pin_stable_state[sensor_name] = raw
    feeder._pin_raw_state[sensor_name] = raw


def make_streaming_feeder(values=None, print_state='printing'):
    """Feeder bereit fuer flush-callback streaming submits."""
    base = {"use_flush_callback_bang_bang": True}
    if values:
        base.update(values)
    printer = FakePrinter()
    printer.objects["print_stats"] = FakePrintStats(state=print_state)
    config = FakeConfig(printer=printer, values=base)
    feeder = buffer_feeder.BufferFeeder(config)
    printer.fire_event('klippy:connect')
    feeder._startup_grace_done = True
    feeder._state = buffer_feeder.STATE_AUTO
    set_sensor_active(feeder, 'hall_overflow', False)
    set_sensor_active(feeder, 'hall_full', False)
    set_sensor_active(feeder, 'hall_empty', True)  # demand triggers MIN_FLOOR
    # ExtruderVelocityTracker zu ready bringen damit target_speed > 0
    fake_ext = printer.objects['extruder']
    fake_ext.last_position = 0.0
    t = 0.0
    for _ in range(12):
        fake_ext.last_position = t * 15.0  # 15 mm/s extruder velocity
        feeder.velocity_tracker.tick(t)
        t += 0.025
    feeder._stepcompress_primed = True
    return printer, feeder


def own_trapq_appends(motion_q, feeder, before_count):
    """Filter trapq_append-Calls die auf feeder.trapq gingen."""
    return [c for c in motion_q.append_calls[before_count:]
            if c[0] is feeder.trapq]


# ---------------------------------------------------------------------------
# Phase 1: Wurzel-Fall — stale cursor + streaming demand
# ---------------------------------------------------------------------------


def test_freshness_anchor_fires_when_cursor_stale_and_no_move_in_flight():
    """Wurzel-Fall: _last_move_end_time ist >MIN_CURSOR_FRESHNESS_S
    alt, kein move in flight, streaming demand vorhanden
    (hall_empty=True). Erwartung: ZWEI trapq_append-Calls — zuerst
    ein Anchor-Step (Cursor-Refresh, 0.05mm), dann der eigentliche
    streaming chunk (interrupt_chunk_mm).

    Pre-Fix: nur EIN append (streaming submit mit interval > 1s).
    Hardware-Crash: queue_step interval=332M Ticks (6.92s) → Timer
    too close beim ersten Submit nach PRINT_START.
    """
    printer, feeder = make_streaming_feeder()
    motion_q = printer.lookup_object('motion_queuing')

    # mcu_now = 10.0, lme = 0.0 -> age = 10s > 1.0s
    feeder.reactor.now = 10.0
    feeder._last_move_end_time = 0.0
    feeder._current_move = None  # nicht in flight

    appends_before = len(motion_q.append_calls)
    motion_q.trigger_flush(flush_time=10.0, step_gen_time=10.0)
    own = own_trapq_appends(motion_q, feeder, appends_before)

    assert len(own) == 2, (
        "Erwartung: Cursor-Freshness-Anchor zuerst, dann streaming "
        "submit (2 own_trapq appends). Erhalten: %d" % len(own))
    # Erstes append = Anchor (0.05mm, kleine Distanz im accel/cruise/decel)
    anchor_call = own[0]
    streaming_call = own[1]
    # axes_r ist Argument 9 in trapq_append signature (x-Komponente):
    # (trapq, t0, accel_time, cruise_time, decel_time,
    #  start_pos_x, _, _, axes_r_x, _, _, start_v, cruise_v, accel)
    # Anchor sollte deutlich kleinere Bewegungszeit haben als Streaming.
    anchor_total_time = anchor_call[2] + anchor_call[3] + anchor_call[4]
    streaming_total_time = (streaming_call[2] + streaming_call[3]
                            + streaming_call[4])
    assert anchor_total_time < streaming_total_time, (
        "Anchor-Move soll kuerzer sein als streaming chunk. "
        "Anchor=%.4fs Streaming=%.4fs" % (anchor_total_time,
                                          streaming_total_time))


def test_no_freshness_anchor_when_cursor_fresh():
    """Cursor < MIN_CURSOR_FRESHNESS_S alt: kein Pre-Anchor noetig,
    nur normaler streaming submit."""
    printer, feeder = make_streaming_feeder()
    motion_q = printer.lookup_object('motion_queuing')

    # mcu_now = 10.0, lme = 9.5 -> age = 0.5s < 1.0s
    feeder.reactor.now = 10.0
    feeder._last_move_end_time = 9.5
    feeder._current_move = None

    appends_before = len(motion_q.append_calls)
    motion_q.trigger_flush(flush_time=10.0, step_gen_time=10.0)
    own = own_trapq_appends(motion_q, feeder, appends_before)

    assert len(own) == 1, (
        "Bei frischem Cursor (age<%.1fs): nur streaming submit, kein "
        "Pre-Anchor. Erhalten: %d appends"
        % (MIN_CURSOR_FRESHNESS_S, len(own)))


def test_no_freshness_anchor_when_move_in_flight():
    """move_in_flight: Cursor wird durch laufenden Move ohnehin
    frisch gehalten — kein Pre-Anchor noetig. Falls Stream-Demand
    da ist, normaler Lookahead-Submit (abuttend an lme)."""
    printer, feeder = make_streaming_feeder()
    motion_q = printer.lookup_object('motion_queuing')

    # move in flight: lme > mcu_now
    feeder.reactor.now = 10.0
    feeder._last_move_end_time = 10.20
    feeder._current_move = {
        'end_time': 10.20, 'direction': 1.0,
        'distance': 9.0, 'speed': feeder.feed_speed,
    }

    appends_before = len(motion_q.append_calls)
    # step_gen_time so dass remaining = lme - sg = 0.10 <= lead_time
    motion_q.trigger_flush(flush_time=10.0, step_gen_time=10.10)
    own = own_trapq_appends(motion_q, feeder, appends_before)

    assert len(own) == 1, (
        "move_in_flight=True: nur streaming submit (Lookahead), "
        "kein Pre-Anchor. Erhalten: %d appends" % len(own))


def test_no_freshness_anchor_when_target_speed_zero():
    """Kein Demand (target_speed=0, hall_full -> drain-Branch): kein
    streaming submit, daher auch kein Pre-Anchor. Cursor-Freshness
    bleibt ausschliesslich in der Verantwortung des Watchdogs in
    diesem Pfad. hall_full=True zwingt _compute_target_feed_speed
    auf 0 (Buffer voll, kein Push)."""
    printer, feeder = make_streaming_feeder()
    motion_q = printer.lookup_object('motion_queuing')

    # hall_full=True -> _compute_target_feed_speed=0 (drain via toolhead)
    set_sensor_active(feeder, 'hall_empty', False)
    set_sensor_active(feeder, 'hall_full', True)
    set_sensor_active(feeder, 'hall_overflow', False)
    # Sanity check: tracker liefert weiter velocity, aber hall_full
    # Branch dominiert -> target_speed=0.
    assert feeder._compute_target_feed_speed() == 0.0

    feeder.reactor.now = 10.0
    feeder._last_move_end_time = 0.0  # stale
    feeder._current_move = None

    appends_before = len(motion_q.append_calls)
    motion_q.trigger_flush(flush_time=10.0, step_gen_time=10.0)
    own = own_trapq_appends(motion_q, feeder, appends_before)

    assert len(own) == 0, (
        "Bei target_speed=0 (kein demand): weder Pre-Anchor noch "
        "streaming submit. Erhalten: %d appends" % len(own))


# ---------------------------------------------------------------------------
# Phase 2: Anchor-Eigenschaften — direction, distance, refresh
# ---------------------------------------------------------------------------


def test_freshness_anchor_distance_is_anchor_nudge_mm():
    """Der Pre-Anchor verwendet ANCHOR_NUDGE_MM (0.05mm) — minimal
    physisch spuerbar, aber genug fuer cursor refresh. axes_r_x +
    Trapezoid-Profile lassen sich daraus rekonstruieren: distance =
    0.5*cruise_v*(accel_time+decel_time) + cruise_v*cruise_time."""
    printer, feeder = make_streaming_feeder()
    motion_q = printer.lookup_object('motion_queuing')

    feeder.reactor.now = 10.0
    feeder._last_move_end_time = 0.0
    feeder._current_move = None

    appends_before = len(motion_q.append_calls)
    motion_q.trigger_flush(flush_time=10.0, step_gen_time=10.0)
    own = own_trapq_appends(motion_q, feeder, appends_before)

    assert len(own) >= 1, "Anchor muss gefeuert haben"
    anchor_call = own[0]
    # Trapq-Append-Signatur, accel_time=arg[2], cruise_time=arg[3],
    # decel_time=arg[4], cruise_v=arg[12], accel=arg[13].
    accel_time = anchor_call[2]
    cruise_time = anchor_call[3]
    decel_time = anchor_call[4]
    cruise_v = anchor_call[12]
    accel_dist = 0.5 * cruise_v * accel_time
    decel_dist = 0.5 * cruise_v * decel_time
    cruise_dist = cruise_v * cruise_time
    total_dist = accel_dist + cruise_dist + decel_dist
    assert total_dist == pytest.approx(ANCHOR_NUDGE_MM, abs=0.001), (
        "Anchor-Distanz muss ANCHOR_NUDGE_MM=%.3fmm sein. "
        "Erhalten: %.4fmm" % (ANCHOR_NUDGE_MM, total_dist))


def test_freshness_anchor_direction_positive_in_normal_demand():
    """Im normalen Demand-Pfad (kein hall_overflow): Anchor laeuft
    in feed-direction (positiv). hall_overflow=True ist ein
    separater Pfad — der flush-callback hat dafuer einen
    Hall1Context.SUBMIT_MOVE early-return weiter oben."""
    printer, feeder = make_streaming_feeder()
    motion_q = printer.lookup_object('motion_queuing')
    # Default: hall_overflow=False bereits via make_streaming_feeder

    feeder.reactor.now = 10.0
    feeder._last_move_end_time = 0.0
    feeder._current_move = None

    appends_before = len(motion_q.append_calls)
    motion_q.trigger_flush(flush_time=10.0, step_gen_time=10.0)
    own = own_trapq_appends(motion_q, feeder, appends_before)

    assert len(own) == 2, "Anchor + streaming expected"
    anchor_call = own[0]
    # direction (axes_r_x) = Argument 8 (0-indexed) in trapq_append:
    # (trapq, t0, accel_time, cruise_time, decel_time,
    #  start_pos_x, _, _, axes_r_x, _, _, start_v, cruise_v, accel)
    axes_r_x = anchor_call[8]
    assert axes_r_x > 0, (
        "Im normalen Demand-Pfad (hall_overflow=False) muss anchor "
        "feed-direction (positiv) laufen. axes_r_x=%.2f" % axes_r_x)


def test_freshness_anchor_advances_last_move_end_time():
    """Nach dem Pre-Anchor muss _last_move_end_time auf
    anchor_t0 + anchor_duration vorruecken. Der direkt darauf
    folgende streaming submit kann sich dann an einem frischen lme
    als Floor ankern."""
    printer, feeder = make_streaming_feeder()
    motion_q = printer.lookup_object('motion_queuing')

    feeder.reactor.now = 10.0
    feeder._last_move_end_time = 0.0
    feeder._current_move = None
    lme_before = feeder._last_move_end_time

    appends_before = len(motion_q.append_calls)
    motion_q.trigger_flush(flush_time=10.0, step_gen_time=10.0)
    own = own_trapq_appends(motion_q, feeder, appends_before)

    assert len(own) == 2, "Anchor + streaming expected"
    assert feeder._last_move_end_time > lme_before, (
        "_last_move_end_time muss nach dem anchor vorruecken. "
        "Vorher %.3f Nachher %.3f" % (lme_before,
                                       feeder._last_move_end_time))
    # Anchor + streaming chunk enden in naher Zukunft nach mcu_now.
    # Anchor: 0.05mm @ feed_speed (~3ms duration). Streaming chunk:
    # interrupt_chunk_mm=9mm @ feed_speed (~0.6s + accel/decel ~0.9s).
    # Total <= 1.5s nach mcu_now+lead_time. lme darf NICHT mehr in
    # far-future Toolhead-Time zeigen (step_gen_time = 10.0 hier,
    # aber mit forced_t0-Clamp auf mcu_now+lead_time landet lme bei
    # ~mcu_now + lead_time + 1s = 11.x — NICHT bei 58+s wie im
    # Crash-Pattern).
    assert feeder._last_move_end_time < 10.0 + 2.0, (
        "_last_move_end_time nach Anchor+Streaming sollte nahe "
        "mcu_now (forced_t0-Clamp greift). Erhalten: %.3f"
        % feeder._last_move_end_time)
