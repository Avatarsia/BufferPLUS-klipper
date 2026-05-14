"""Cursor-Freshness-Contract (D2: Deferred-Streaming) — Pre-Anchor und
Streaming-Submit auf zwei aufeinanderfolgende flush-callbacks aufteilen.

Hardware-Beleg V1 (Pre-D2-Version, klippy_refactor_*_repro.log):
  Pre-Anchor fired (cursor_age=6.74s > 1.0s threshold),
  Streaming-Submit folgte IM GLEICHEN flush-callback,
  → beide queue_step-Bursts landen zusammen mit TMC-UART-Reads in
    einem USB-Burst (Sent 83-97 alle bei T=1365910.806-.864).
  → MCU empfaengt scheduled-step-clock bereits in der Vergangenheit
    (lead_time=0.120s wird durch USB+MCU-CPU-Latenz aufgefressen).
  → MCU 'LLL_PLUS' shutdown: Timer too close

Wurzel V2 (D2): Pre-Anchor + Streaming-Submit in einem flush-callback
fuehrt zu USB-Burst-Race; selbst mit garantiert frischem _last_move_-
end_time hat die MCU keinen Buffer um den Pre-Anchor-Step durchzu-
schedulen bevor der Streaming-Step queued wird.

Fix D2: Pre-Anchor submitten und SOFORT return (kein streaming-Submit
in derselben flush-callback). Naechster flush-callback (~10-25ms
spaeter via motion_queuing.flush_handler) findet _last_move_end_time
frisch (vom Pre-Anchor), _move_in_flight() korrekt True solange
Pre-Anchor laeuft, und faellt in den existierenden P7-66-Lookahead-
Pfad zurueck (streaming abuttend an lme). MCU bekommt Pre-Anchor-
Bytes und Streaming-Bytes in zwei separaten USB-Bursts mit
ausreichendem Abstand — keine Race-Anfaelligkeit mehr.

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


def test_freshness_anchor_fires_alone_when_cursor_stale_d2_deferred():
    """D2 Wurzel-Fall: bei stale cursor submittet _flush_submit_-
    streaming_chunk nur den Pre-Anchor und KEHRT ZURUECK (kein
    streaming-Submit in derselben flush-callback). Der streaming-
    Submit wird auf den NAECHSTEN flush-callback verschoben — so
    landen die queue_step-Bytes in zwei separaten USB-Bursts mit
    ausreichendem MCU-Verarbeitungs-Buffer dazwischen.

    Pre-D2 (PR #40 V1): 2 appends (Anchor + Streaming sofort)
    -> USB-Burst-Race -> Timer too close.

    D2: 1 append (nur Anchor diese flush-callback).
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

    assert len(own) == 1, (
        "D2: Pre-Anchor allein in dieser flush-callback. "
        "Streaming-Submit erst beim naechsten flush-callback. "
        "Erhalten: %d appends" % len(own))
    # Sole append ist der Anchor (kleine Distanz)
    anchor_call = own[0]
    # Anchor-Distanz aus Trapezoid-Profil rekonstruieren
    accel_time = anchor_call[2]
    cruise_time = anchor_call[3]
    decel_time = anchor_call[4]
    cruise_v = anchor_call[12]
    total_dist = (cruise_v * cruise_time
                  + 0.5 * cruise_v * (accel_time + decel_time))
    assert total_dist == pytest.approx(ANCHOR_NUDGE_MM, abs=0.001), (
        "Sole append in D2-deferred ist der Anchor. "
        "Erwartete Distanz: %.3fmm, erhalten: %.4fmm"
        % (ANCHOR_NUDGE_MM, total_dist))


def test_streaming_follows_pre_anchor_on_next_flush_callback():
    """D2 Vertrag: nach dem Pre-Anchor (1. flush-callback) erfolgt
    der streaming-Submit beim NAECHSTEN flush-callback. Da der
    Pre-Anchor _last_move_end_time auf mcu_now+lead_time gesetzt
    hat, ist der naechste flush-callback im move_in_flight=True
    Pfad (P7-66 Lookahead-Submit, abuttend an lme)."""
    printer, feeder = make_streaming_feeder()
    motion_q = printer.lookup_object('motion_queuing')

    # 1. flush-callback: stale cursor
    feeder.reactor.now = 10.0
    feeder._last_move_end_time = 0.0
    feeder._current_move = None

    appends_before = len(motion_q.append_calls)
    motion_q.trigger_flush(flush_time=10.0, step_gen_time=10.0)
    own_first = own_trapq_appends(motion_q, feeder, appends_before)
    assert len(own_first) == 1, "1. flush: nur Pre-Anchor"

    # Pre-Anchor hat lme auf naehe mcu_now gesetzt:
    assert feeder._last_move_end_time > 10.0, (
        "Pre-Anchor muss _last_move_end_time setzen. "
        "Erhalten: %.3f" % feeder._last_move_end_time)
    pre_anchor_end = feeder._last_move_end_time

    # 2. flush-callback (10ms spaeter): pre-anchor in flight,
    # remaining = lme - step_gen_time < lead_time -> Lookahead
    # streaming-submit abuttend an lme.
    appends_before = len(motion_q.append_calls)
    # step_gen_time so dass remaining = pre_anchor_end - sg klein
    motion_q.trigger_flush(flush_time=10.01,
                           step_gen_time=pre_anchor_end - 0.05)
    own_second = own_trapq_appends(motion_q, feeder, appends_before)
    assert len(own_second) == 1, (
        "2. flush: streaming-Submit nach Pre-Anchor. "
        "Erhalten: %d appends" % len(own_second))
    # Streaming-Submit muss abuttend an pre_anchor_end starten
    streaming_t0 = own_second[0][1]
    assert streaming_t0 == pytest.approx(pre_anchor_end, abs=0.001), (
        "Streaming-Submit muss abuttend an Pre-Anchor-Ende starten "
        "(P7-66 Lookahead). t0=%.3f erwartet=%.3f"
        % (streaming_t0, pre_anchor_end))


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
    physisch spuerbar, aber genug fuer cursor refresh. Distanz aus
    Trapezoid-Profil rekonstruieren."""
    printer, feeder = make_streaming_feeder()
    motion_q = printer.lookup_object('motion_queuing')

    feeder.reactor.now = 10.0
    feeder._last_move_end_time = 0.0
    feeder._current_move = None

    appends_before = len(motion_q.append_calls)
    motion_q.trigger_flush(flush_time=10.0, step_gen_time=10.0)
    own = own_trapq_appends(motion_q, feeder, appends_before)

    # D2: 1 append in dieser flush-callback (nur Pre-Anchor)
    assert len(own) == 1, "Anchor muss gefeuert haben (D2: sole append)"
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
    in feed-direction (positiv)."""
    printer, feeder = make_streaming_feeder()
    motion_q = printer.lookup_object('motion_queuing')
    # Default: hall_overflow=False bereits via make_streaming_feeder

    feeder.reactor.now = 10.0
    feeder._last_move_end_time = 0.0
    feeder._current_move = None

    appends_before = len(motion_q.append_calls)
    motion_q.trigger_flush(flush_time=10.0, step_gen_time=10.0)
    own = own_trapq_appends(motion_q, feeder, appends_before)

    # D2: 1 append in dieser flush-callback (nur Pre-Anchor)
    assert len(own) == 1, "D2: Pre-Anchor allein"
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

    # D2: nur Pre-Anchor in dieser flush-callback. Streaming kommt
    # erst beim naechsten flush-callback.
    assert len(own) == 1, "D2: nur Pre-Anchor"
    assert feeder._last_move_end_time > lme_before, (
        "_last_move_end_time muss nach dem anchor vorruecken. "
        "Vorher %.3f Nachher %.3f" % (lme_before,
                                       feeder._last_move_end_time))
    # Pre-Anchor: 0.05mm @ feed_speed (~3ms duration). forced_t0
    # wird intern auf mcu_now+lead_time geclampt. lme nach Anchor
    # = mcu_now+lead_time+anchor_dur ≈ mcu_now + 0.125s.
    assert feeder._last_move_end_time < 10.0 + 0.5, (
        "_last_move_end_time nach Pre-Anchor sollte nahe mcu_now "
        "(forced_t0-Clamp greift, Anchor ist 3-5ms). "
        "Erhalten: %.3f" % feeder._last_move_end_time)
