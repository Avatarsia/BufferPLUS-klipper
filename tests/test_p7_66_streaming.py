"""P7-66 — Streaming-Pipeline für den AUTO-Bang-Bang Feed-Pfad.

Vorher (P7-61 baseline):
  _on_mcu_flush() submitted einen neuen Chunk erst, wenn der laufende
  Chunk vollständig auf der MCU ausgespielt war (_move_in_flight()
  == False). Anker für jeden Chunk: t0 = step_gen_time + lead_time.
  Bei lead_time=0.3 s entsteht zwischen aufeinanderfolgenden Chunks
  ein sichtbarer Anker-Gap → Netto-Förderrate < nominal feed_speed
  (Motor läuft seriell, ein Chunk auf einmal, mit fester 0.3 s
  Atempause).

Nachher (P7-66):
  Wenn ein Move noch in flight ist, aber die Restzeit <= lead_time
  beträgt, submittet _on_mcu_flush() den nächsten Chunk JETZT mit
  t0 = _last_move_end_time (abuttend, kein Gap). Damit verhält sich
  AUTO-Bang-Bang wie der single-shot Streaming-Pfad in
  _tick_pending_chunk: aufeinanderfolgende Chunks fließen ohne
  Lücke. Übersteigt die Restzeit lead_time, fällt der Pfad wie
  bisher in den No-Op (warten).

P7-66 R1 (Codex-Finding):
  _enable_stepper() hat _last_enable_schedule_time auf
  _last_move_end_time + lead_time geschoben. Damit gewann der
  en-Floor in _submit_single_trapezoid und der abuttend-Anker
  brach wieder auf. Fix: streaming=True-Flag im Lookahead-Pfad
  skipt _enable_stepper() (Motor schon enabled) UND droppt den
  en-Floor. Zusätzlich: mcu_now als Floor im forced_t0-Pfad
  schützt vor "Timer too close" wenn ein vorheriger Move kurz
  vor step_gen_time endete.

P7-66b (Interrupt-on-HALL — Move-Splitting, Option III):
  Hardware-Bericht 2026-05-12: flush_callback_chunk_mm=45 grindet
  Filament weil HALL2 den laufenden Chunk nicht abbrechen kann →
  Overshoot um eine volle Chunk-Länge. Fix: interrupt_chunk_mm
  (Default 9 mm) capt die Größe eines einzelnen submittierten
  Trapezoids. Größere chunks werden in N sub-chunks zerlegt via
  _pending_remaining_mm. _tick_pending_chunk prüft HALL2/HALL1
  zwischen sub-chunks → bei Trigger wird das Restdistanz-Counter
  geleert, der gerade laufende sub-chunk spielt aus (max 9 mm
  Overshoot bei Default). Auch _on_mcu_flush hall_full-Branch
  clearisiert jetzt _pending_remaining_mm.

Tests:
  - Charakterisierungs-Tests (Suffix `_legacy`): erfassten das
    serielle Pre-Patch-Verhalten (gehalten als Regression-Guard
    für den first-chunk Pfad, der unverändert bleibt).
  - Streaming-Tests: verifizieren das neue Lookahead-Submit.
  - R1 / R6: verifizieren dass Stepper-Enable wirklich verkabelt
    ist (klippy:connect-Event gefeuert) und der en-Floor im
    Streaming-Pfad fehlt.
  - 4.2 / 4.3 / Interrupt-on-HALL: verifizieren Move-Splitting,
    HALT-während-Streaming, HALL2-Abort eines laufenden chunks.

Tag-Hinweis: NUR AUTO-Bang-Bang via _on_mcu_flush + _tick_pending_-
chunk im AUTO-State. Kein LOAD/UNLOAD/SYNC/MANUAL_FEED-Pfad wird
angefasst — _on_mcu_flush bailt auf state != STATE_AUTO weiterhin,
und _tick_pending_chunk's HALL2-Clamp ist auf STATE_AUTO und
forward-direction beschränkt.
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
    """Build a feeder ready for streaming tests.

    P7-66 R6 (Codex-Finding): explicitly fires klippy:connect so
    _handle_connect() wires up _stepper_enable. Pre-fix tests used
    a feeder where _stepper_enable was None → _enable_stepper()
    silently skipped → R1 path wasn't exercised. Real Klipper boots
    fire klippy:connect after all modules load — this mock now
    mirrors that.
    """
    base = {"use_flush_callback_bang_bang": True}
    if values:
        base.update(values)
    printer = FakePrinter()
    config = FakeConfig(printer=printer, values=base)
    feeder = buffer_feeder.BufferFeeder(config)
    # P7-66 R6: fire klippy:connect so _handle_connect runs and
    # wires _stepper_enable. Tests that monitor enable handles or
    # exercise the R1 path need this — without it the FakeStepper-
    # Enable.handles dict stays empty and motor_enable/disable
    # calls are silently dropped by the None-guard in _enable_stepper.
    printer.fire_event('klippy:connect')
    feeder._startup_grace_done = True
    feeder._state = buffer_feeder.STATE_AUTO
    set_sensor_active(feeder, 'hall_overflow', False)
    set_sensor_active(feeder, 'hall_full', False)
    set_sensor_active(feeder, 'hall_empty', False)
    return printer, feeder


def get_enable_handle(feeder):
    """Return the FakeStepperEnableHandle for the feeder's stepper."""
    return feeder._stepper_enable


# ---------------------------------------------------------------------------
# Characterisation: first-chunk Pfad (Pre-Patch UND Post-Patch identisch).
# ---------------------------------------------------------------------------

def test_first_chunk_anchors_at_step_gen_plus_lead_time():
    """Erster Chunk eines AUTO-Bang-Bang Feeds: kein vorheriger Move
    in flight, also ist der race-free Anker step_gen_time + lead_time.

    Dieses Verhalten bleibt durch P7-66 unverändert — der Lookahead-
    Pfad kickt nur ein, wenn bereits ein Chunk in flight ist."""
    printer, feeder = make_feeder()
    motion_q = printer.lookup_object('motion_queuing')
    set_sensor_active(feeder, 'hall_empty', True)

    feeder.reactor.now = 5.0
    appends_before = len(motion_q.append_calls)
    motion_q.trigger_flush(flush_time=5.0, step_gen_time=5.05)

    new_appends = motion_q.append_calls[appends_before:]
    own = [c for c in new_appends if c[0] is feeder.trapq]
    assert own, "expected first-chunk submit on HALL3-edge"
    t0 = own[0][1]
    assert t0 == pytest.approx(5.05 + feeder.lead_time, abs=0.01)


# ---------------------------------------------------------------------------
# Streaming (Lookahead-Submit, P7-66).
# ---------------------------------------------------------------------------

def test_streaming_submit_when_remaining_le_lead_time(monkeypatch):
    """P7-66 core: ein Move ist in flight, aber Restzeit <= lead_time
    → flush-callback submittet den nächsten Chunk JETZT, abuttend an
    _last_move_end_time. Pre-Patch wäre hier _move_in_flight=True →
    kein Submit (Test würde mit dem alten Pfad fehlschlagen)."""
    printer, feeder = make_feeder()
    motion_q = printer.lookup_object('motion_queuing')
    set_sensor_active(feeder, 'hall_empty', True)

    # Simuliere einen bereits laufenden Move:
    #   _current_move['end_time'] = 5.20
    #   reactor.now = 5.00         → mcu.estimated_print_time = 5.00
    #   restzeit = 0.20 s  <=  lead_time (default 0.3)
    feeder.reactor.now = 5.00
    feeder._continuous_feed = True
    feeder._continuous_feed_direction = 1
    feeder._continuous_feed_speed = feeder.feed_speed
    feeder._last_move_end_time = 5.20
    feeder._current_move = {
        'end_time': 5.20,
        'direction': 1.0,
        'distance': feeder.flush_callback_chunk_mm,
        'speed': feeder.feed_speed,
    }
    # _stepcompress_primed=True damit kein flush_step_generation läuft.
    feeder._stepcompress_primed = True

    appends_before = len(motion_q.append_calls)
    motion_q.trigger_flush(flush_time=5.00, step_gen_time=5.05)

    new_appends = motion_q.append_calls[appends_before:]
    own = [c for c in new_appends if c[0] is feeder.trapq]
    assert own, ("P7-66 broken: kein Streaming-Submit obwohl Restzeit "
                 "= 0.15 s <= lead_time = %.2f" % feeder.lead_time)
    # Abuttend: t0 == _last_move_end_time (pre-Submit value 5.20),
    # NICHT step_gen_time + lead_time = 5.35.
    t0 = own[0][1]
    assert t0 == pytest.approx(5.20, abs=0.001), (
        "Streaming anchor wrong: t0=%.3f, expected 5.20 "
        "(abuttend an _last_move_end_time). "
        "5.35 = step_gen_time+lead_time wäre der erste-Chunk-Pfad."
        % t0)


def test_no_streaming_submit_when_remaining_gt_lead_time():
    """Restzeit > lead_time → Pfad bleibt wie vorher passiv: kein
    Submit, der laufende Chunk spielt erst weiter aus. Verhindert
    dass Lookahead zu viele Chunks vorab in den trapq pumpt und
    HALT-Latenz verlängert."""
    printer, feeder = make_feeder()
    motion_q = printer.lookup_object('motion_queuing')
    set_sensor_active(feeder, 'hall_empty', True)

    # Restzeit = 0.5 s, deutlich > lead_time 0.3 s.
    feeder.reactor.now = 5.00
    feeder._continuous_feed = True
    feeder._continuous_feed_direction = 1
    feeder._continuous_feed_speed = feeder.feed_speed
    feeder._last_move_end_time = 5.50
    feeder._current_move = {
        'end_time': 5.50,
        'direction': 1.0,
        'distance': feeder.flush_callback_chunk_mm,
        'speed': feeder.feed_speed,
    }
    feeder._stepcompress_primed = True

    appends_before = len(motion_q.append_calls)
    motion_q.trigger_flush(flush_time=5.00, step_gen_time=5.05)

    new_appends = motion_q.append_calls[appends_before:]
    own = [c for c in new_appends if c[0] is feeder.trapq]
    assert not own, ("Lookahead darf NICHT submitten wenn Restzeit "
                     "(0.45 s) > lead_time (0.3 s) — würde Trapq "
                     "über die HALT-Latenz hinaus füllen.")


def test_streaming_max_two_chunks_pending():
    """HALT/OVERFLOW-Latenz-Garantie: nach einem Lookahead-Submit ist
    der nächste Chunk in flight + ggf. einer übergeben — aber NICHT
    drei. Sukzessive flush-Calls dürfen nicht beliebig viele Chunks
    vorab queuen. Konkret: nach Lookahead-Submit erhöht sich
    _last_move_end_time; der zweite flush-Call sieht entweder
    _pending_remaining_mm > 0 (Move-Splitting aktiv) oder Restzeit
    ~= Chunk-Dauer (> lead_time) — beide Fälle: no-op."""
    printer, feeder = make_feeder()
    motion_q = printer.lookup_object('motion_queuing')
    set_sensor_active(feeder, 'hall_empty', True)

    feeder.reactor.now = 5.00
    feeder._continuous_feed = True
    feeder._continuous_feed_direction = 1
    feeder._continuous_feed_speed = feeder.feed_speed
    feeder._last_move_end_time = 5.20  # Restzeit 0.20 s
    feeder._current_move = {
        'end_time': 5.20,
        'direction': 1.0,
        'distance': feeder.flush_callback_chunk_mm,
        'speed': feeder.feed_speed,
    }
    feeder._stepcompress_primed = True

    appends_before = len(motion_q.append_calls)
    # 1. flush: Lookahead-Submit erwartet.
    motion_q.trigger_flush(flush_time=5.00, step_gen_time=5.05)
    # 2. flush direkt danach (gleicher mcu_now): jetzt ist
    # _last_move_end_time deutlich in der Zukunft (5.20 + chunk_dur)
    # oder _pending_remaining_mm > 0 (Move-Splitting), beides → no-op.
    motion_q.trigger_flush(flush_time=5.00, step_gen_time=5.05)

    new_appends = [c for c in motion_q.append_calls[appends_before:]
                   if c[0] is feeder.trapq]
    assert len(new_appends) == 1, (
        "Streaming queued mehrere Chunks parallel — HALT-Latenz "
        "garantiert nicht mehr <= 1 Chunk. Anzahl Submits: %d"
        % len(new_appends))


# ---------------------------------------------------------------------------
# Sicherheits-Pfade bleiben erhalten — P7-66 darf sie nicht unterlaufen.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "gate",
    ["hall1-overflow", "wrong-state"],
)
def test_streaming_safety_gates(gate):
    """Subsumes: test_streaming_blocked_by_hall1_overflow,
    test_streaming_respects_state_auto_only
    (parametrized 2026-05-12, Audit-2 Cluster D).

    P7-66 R6 (Hardware-Bugfix): the streaming lookahead in _on_mcu_flush
    must NOT bypass either safety gate.
    """
    printer, feeder = make_feeder()
    motion_q = printer.lookup_object('motion_queuing')
    set_sensor_active(feeder, 'hall_empty', True)
    if gate == "hall1-overflow":
        # Hard-Safety: selbst wenn Lookahead greifen wuerde, blockiert
        # HALL1 (Overflow) jeden Forward-Submit. Diese Garantie kommt
        # vom `_is_hall1_active('submit_move')` Branch oben in
        # _on_mcu_flush; P7-66 darf sie nicht aushebeln.
        set_sensor_active(feeder, 'hall_overflow', True)  # HALL1 aktiv
    else:  # wrong-state
        # P7-66 darf nur im AUTO-Pfad streamen. Nicht-AUTO Zustaende
        # (LOAD/UNLOAD/MANUAL_FEED) haben eigene Submit-Pfade — Lookahead
        # via flush-callback hier waere Double-Submit-Risiko.
        feeder._state = buffer_feeder.STATE_MANUAL_FEED

    feeder.reactor.now = 5.00
    feeder._continuous_feed = True
    feeder._last_move_end_time = 5.20  # Lookahead-Bedingung waere wahr
    feeder._current_move = {
        'end_time': 5.20,
        'direction': 1.0,
        'distance': feeder.flush_callback_chunk_mm,
        'speed': feeder.feed_speed,
    }
    feeder._stepcompress_primed = True

    appends_before = len(motion_q.append_calls)
    motion_q.trigger_flush(flush_time=5.00, step_gen_time=5.05)

    own = [c for c in motion_q.append_calls[appends_before:]
           if c[0] is feeder.trapq]
    if gate == "hall1-overflow":
        assert not own, "HALL1-Lockout durchbrochen — Sicherheit verletzt"
    else:  # wrong-state
        assert not own, "Lookahead lief in STATE_MANUAL_FEED — verboten"


def test_streaming_stops_on_hall_full():
    """Wenn HALL2 (full) während Streaming aktiv wird, muss
    _continuous_feed sofort auf False fallen UND _pending_remaining_-
    mm gecleared werden. Der bereits in flight befindliche Chunk darf
    auslaufen, aber kein neuer wird submitted — auch nicht via
    Lookahead, und auch keine bereits gequeued-pending sub-chunks."""
    printer, feeder = make_feeder()
    motion_q = printer.lookup_object('motion_queuing')
    set_sensor_active(feeder, 'hall_full', True)  # full → stop

    feeder.reactor.now = 5.00
    feeder._continuous_feed = True
    feeder._last_move_end_time = 5.20  # Restzeit < lead_time
    feeder._pending_remaining_mm = 36.0   # P7-66b: pending sub-chunks
    feeder._pending_direction = 1.0
    feeder._pending_speed = feeder.feed_speed
    feeder._current_move = {
        'end_time': 5.20,
        'direction': 1.0,
        'distance': feeder.flush_callback_chunk_mm,
        'speed': feeder.feed_speed,
    }
    feeder._stepcompress_primed = True

    appends_before = len(motion_q.append_calls)
    motion_q.trigger_flush(flush_time=5.00, step_gen_time=5.05)

    own = [c for c in motion_q.append_calls[appends_before:]
           if c[0] is feeder.trapq]
    assert not own, "HALL2-Stop unterlaufen — Streaming submittet trotzdem"
    assert feeder._continuous_feed is False, (
        "HALL2 hat _continuous_feed nicht zurückgesetzt")
    # P7-66b: HALL2 in _on_mcu_flush muss _pending_remaining_mm clearen,
    # sonst tropfen weitere sub-chunks via _tick_pending_chunk durch.
    assert feeder._pending_remaining_mm == 0.0, (
        "HALL2 in _on_mcu_flush hat _pending_remaining_mm nicht "
        "gecleared — Move-Splitting overshoot Garantie gebrochen")


# ---------------------------------------------------------------------------
# R1: en-Floor darf den Streaming-Anker NICHT brechen.
# ---------------------------------------------------------------------------

def test_streaming_anchor_unaffected_by_stale_enable_schedule_time():
    """P7-66 R1 (Codex-Finding): _enable_stepper() schiebt
    _last_enable_schedule_time auf >= _last_move_end_time +
    lead_time. Wenn der streaming-pfad jetzt nochmal _enable_stepper()
    ruft und den en-Floor in t0 = max(forced_t0, _last_move_end_time,
    en) anwendet, wäre der echte Anker en = 5.20 + 0.3 = 5.50
    statt 5.20 — Inter-Chunk-Gap kehrt zurück.

    Fix: streaming=True im Submit-Pfad skipt _enable_stepper() und
    dropt den en-Floor. Wir prüfen den effektiven Anker und dass
    KEIN motor_enable-Call mehr stattgefunden hat während des
    Lookahead-Submits."""
    printer, feeder = make_feeder()
    motion_q = printer.lookup_object('motion_queuing')
    enable_handle = get_enable_handle(feeder)
    assert enable_handle is not None, (
        "R6: _stepper_enable must be wired. _handle_connect not fired?")
    set_sensor_active(feeder, 'hall_empty', True)

    # Vor-Bedingung: stepper ist bereits enabled, _last_enable_-
    # schedule_time zeigt in die Zukunft (typisch _last_move_end_time
    # + lead_time, wie nach dem ersten _submit_move).
    feeder.reactor.now = 5.00
    feeder._continuous_feed = True
    feeder._continuous_feed_direction = 1
    feeder._continuous_feed_speed = feeder.feed_speed
    feeder._last_move_end_time = 5.20
    feeder._last_enable_schedule_time = 5.20 + feeder.lead_time  # 5.50
    feeder._current_move = {
        'end_time': 5.20,
        'direction': 1.0,
        'distance': feeder.flush_callback_chunk_mm,
        'speed': feeder.feed_speed,
    }
    feeder._stepcompress_primed = True

    enables_before = len(enable_handle.enables)
    appends_before = len(motion_q.append_calls)
    motion_q.trigger_flush(flush_time=5.00, step_gen_time=5.05)

    new_appends = [c for c in motion_q.append_calls[appends_before:]
                   if c[0] is feeder.trapq]
    assert new_appends, "Lookahead-Submit fehlt"
    t0 = new_appends[0][1]
    # R1 fix: t0 == _last_move_end_time (5.20), NICHT en (5.50).
    assert t0 == pytest.approx(5.20, abs=0.001), (
        "R1 broken: t0=%.3f. Expected 5.20 (_last_move_end_time). "
        "5.50 = _last_enable_schedule_time → en-Floor hat gewonnen, "
        "Streaming-Anker gebrochen. streaming=True muss den en-Floor "
        "im _submit_single_trapezoid droppen." % t0)
    # Plus: kein neuer motor_enable-Call. Der vorherige Chunk hat
    # _stepper_enable bereits energised; streaming=True skippt das.
    assert len(enable_handle.enables) == enables_before, (
        "streaming=True path called motor_enable — must skip; stepper "
        "is already energised from the in-flight chunk")


def test_streaming_anchor_floors_at_mcu_now():
    """P7-66 R1b: wenn _last_move_end_time < step_gen_time (also der
    vorherige Move ist gerade abgelaufen, _move_in_flight() noch nicht
    aktualisiert wegen ms-Race), darf t0 nicht in der Vergangenheit
    landen. Fix: max(forced_t0, _last_move_end_time, en, mcu_now)."""
    printer, feeder = make_feeder()
    motion_q = printer.lookup_object('motion_queuing')
    set_sensor_active(feeder, 'hall_empty', True)

    # Degenerate race: end_time hat schon mcu_now überschritten.
    # Aber _move_in_flight() schaut auf _current_move['end_time'] vs.
    # mcu_now, so wir setzen end_time minimal in die Zukunft damit
    # die in-flight-Erkennung greift, aber _last_move_end_time vorher.
    feeder.reactor.now = 5.00
    feeder._continuous_feed = True
    feeder._continuous_feed_direction = 1
    feeder._continuous_feed_speed = feeder.feed_speed
    feeder._last_move_end_time = 4.90      # in der Vergangenheit
    feeder._last_enable_schedule_time = 0.0
    feeder._current_move = {
        # end_time muss > mcu_now sein damit _move_in_flight True liefert
        'end_time': 5.001,
        'direction': 1.0,
        'distance': feeder.flush_callback_chunk_mm,
        'speed': feeder.feed_speed,
    }
    feeder._stepcompress_primed = True

    appends_before = len(motion_q.append_calls)
    # step_gen_time deutlich über _last_move_end_time
    motion_q.trigger_flush(flush_time=5.00, step_gen_time=5.05)

    new_appends = [c for c in motion_q.append_calls[appends_before:]
                   if c[0] is feeder.trapq]
    # Hier setzt _on_mcu_flush forced_t0 = _last_move_end_time = 4.90.
    # Ohne mcu_now-Floor wäre t0 = 4.90 → in der Vergangenheit → MCU
    # "Timer too close". Mit Floor: t0 >= mcu_now (= 5.0).
    if new_appends:
        t0 = new_appends[0][1]
        # mcu_now ≈ 5.0 (reactor.now wird in monotonic() um MONOTONIC_STEP
        # erhöht — daher tolerieren wir leicht > 5.0).
        assert t0 >= 5.0 - 0.001, (
            "R1b broken: t0=%.4f < mcu_now ≈ 5.0. mcu_now-Floor fehlt "
            "im forced_t0-Pfad — Timer-too-close-Risiko." % t0)


# ---------------------------------------------------------------------------
# P7-66b — Interrupt-on-HALL via Move-Splitting.
# ---------------------------------------------------------------------------

def test_interrupt_cap_splits_large_chunk():
    """P7-66b: flush_callback_chunk_mm=45 + interrupt_chunk_mm=9 →
    der erste submittierte trapezoid ist 9 mm groß, der Rest landet
    in _pending_remaining_mm (36 mm) und wird über _tick_pending_-
    chunk in 4 weitere 9-mm-Sub-Chunks zerlegt. Ohne diesen Cap
    würde HALL2 erst nach 45 mm overshoot greifen."""
    printer, feeder = make_feeder(values={
        'flush_callback_chunk_mm': 45.0,
        'interrupt_chunk_mm': 9.0,
        'feed_speed': 70.0,
    })
    motion_q = printer.lookup_object('motion_queuing')
    set_sensor_active(feeder, 'hall_empty', True)

    feeder.reactor.now = 5.0
    appends_before = len(motion_q.append_calls)
    motion_q.trigger_flush(flush_time=5.0, step_gen_time=5.05)

    new_appends = [c for c in motion_q.append_calls[appends_before:]
                   if c[0] is feeder.trapq]
    assert len(new_appends) == 1, "erster sub-chunk fehlt"

    # Submitted distance entspricht dem 9-mm-Sub-Chunk, nicht 45 mm.
    # trapq_append args: (trapq, t0, accel_t, cruise_t, decel_t,
    # start_pos_x, ..., axes_r_x, ..., 0., cruise_v, accel)
    args = new_appends[0]
    accel_t, cruise_t, decel_t = args[2], args[3], args[4]
    cruise_v = args[12]
    accel = args[13]
    # distance = accel_dist + cruise_dist + decel_dist
    accel_dist = 0.5 * accel * accel_t * accel_t
    cruise_dist = cruise_v * cruise_t
    decel_dist = 0.5 * accel * decel_t * decel_t
    submitted = accel_dist + cruise_dist + decel_dist
    assert submitted == pytest.approx(9.0, abs=0.01), (
        "erster Sub-Chunk falsche Größe: %.3f mm (erwartet 9 mm). "
        "interrupt_chunk_mm-Cap greift nicht." % submitted)

    # Restdistanz: 45 - 9 = 36 mm, gequeued als pending.
    assert feeder._pending_remaining_mm == pytest.approx(36.0, abs=0.001)
    assert feeder._pending_submit_chunk_cap == pytest.approx(9.0, abs=0.001)


def test_hall_full_aborts_pending_sub_chunks_in_tick():
    """P7-66b core hardware-safety: zwischen sub-chunks prüft
    _tick_pending_chunk HALL2. Bei Trigger wird _pending_remaining_-
    mm sofort auf 0 gesetzt — der gerade in flight befindliche
    sub-chunk spielt aus, aber kein weiterer wird submittet.

    Worst-Case-Overshoot: ein interrupt_chunk_mm (Default 9 mm)
    statt einer ganzen flush_callback_chunk_mm (45 mm) — Hardware-
    Grind-Issue 2026-05-12 behoben."""
    printer, feeder = make_feeder(values={
        'flush_callback_chunk_mm': 45.0,
        'interrupt_chunk_mm': 9.0,
        'feed_speed': 70.0,
    })
    motion_q = printer.lookup_object('motion_queuing')
    feeder._state = buffer_feeder.STATE_AUTO
    set_sensor_active(feeder, 'hall_empty', False)
    set_sensor_active(feeder, 'hall_full', True)

    # Simuliere einen aktiven Sub-Chunk-Stream: pending=36 mm, der
    # erste 9-mm-Sub-Chunk läuft gerade.
    feeder._pending_remaining_mm = 36.0
    feeder._pending_direction = 1.0
    feeder._pending_speed = feeder.feed_speed
    feeder._pending_submit_chunk_cap = 9.0
    feeder._last_move_end_time = 5.10
    feeder._current_move = {
        'end_time': 5.10,
        'direction': 1.0,
        'distance': 9.0,
        'speed': feeder.feed_speed,
    }
    feeder._stepcompress_primed = True
    feeder.reactor.now = 5.05

    appends_before = len(motion_q.append_calls)
    feeder._tick_pending_chunk(eventtime=5.05)

    new_appends = [c for c in motion_q.append_calls[appends_before:]
                   if c[0] is feeder.trapq]
    assert not new_appends, (
        "HALL2 hat den nächsten Sub-Chunk nicht abgebrochen — "
        "Pending-Stream pumpt weiter trotz voller Buffer (Hardware-"
        "Grind-Bug 2026-05-12)")
    assert feeder._pending_remaining_mm == 0.0, (
        "_pending_remaining_mm nicht gecleared — HALL2-Clamp im "
        "_tick_pending_chunk fehlt oder greift nicht in AUTO+forward")
    assert feeder._continuous_feed is False, (
        "HALL2-Trigger via tick muss _continuous_feed deaktivieren")


def test_pending_sub_chunks_continue_until_hall_full():
    """Positive case: solange HALL2 inaktiv ist, streamen die sub-
    chunks munter weiter. _tick_pending_chunk submittet jeweils
    einen weiteren 9-mm-Sub-Chunk wenn der gerade laufende kurz
    vor Ende ist."""
    printer, feeder = make_feeder(values={
        'flush_callback_chunk_mm': 45.0,
        'interrupt_chunk_mm': 9.0,
        'feed_speed': 70.0,
    })
    motion_q = printer.lookup_object('motion_queuing')
    set_sensor_active(feeder, 'hall_full', False)
    set_sensor_active(feeder, 'hall_overflow', False)

    feeder._pending_remaining_mm = 36.0
    feeder._pending_direction = 1.0
    feeder._pending_speed = feeder.feed_speed
    feeder._pending_submit_chunk_cap = 9.0
    # gap-Bedingung: _last_move_end_time - mcu_now <= chunk_duration/2
    # chunk_duration = 9 mm / 70 mm/s ≈ 0.129 s, halb ≈ 0.064 s.
    feeder.reactor.now = 5.00
    feeder._last_move_end_time = 5.03   # 30 ms Restzeit → triggert
    feeder._current_move = {
        'end_time': 5.03, 'direction': 1.0, 'distance': 9.0,
        'speed': feeder.feed_speed,
    }
    feeder._stepcompress_primed = True

    appends_before = len(motion_q.append_calls)
    feeder._tick_pending_chunk(eventtime=5.00)

    new_appends = [c for c in motion_q.append_calls[appends_before:]
                   if c[0] is feeder.trapq]
    assert len(new_appends) == 1, (
        "Sub-Chunk-Stream stoppt obwohl HALL2 inaktiv — Cap-Pfad "
        "in _tick_pending_chunk fehlt oder ist falsch verkabelt")
    assert feeder._pending_remaining_mm == pytest.approx(27.0, abs=0.001)


# ---------------------------------------------------------------------------
# 4.2 — Streaming → HALL2-Full Race (Codex-Finding).
# ---------------------------------------------------------------------------

def test_streaming_then_hall2_aborts_in_flight_via_pending_clear():
    """Codex 4.2: HALL3 active, ein Chunk in flight, ein Lookahead-
    Submit (oder sub-chunk-Stream) gequeued. HALL2 schaltet auf True
    während der nächste flush feuert.

    Erwartung mit P7-66b:
      - kein weiterer Submit
      - _continuous_feed=False
      - _pending_remaining_mm=0 (sub-chunk-Stream abgebrochen)
      - der bereits in flight befindliche Chunk läuft aus — wird in
        _last_move_end_time sichtbar bleiben, weil wir ihn nicht
        zurückrollen können (Trapq schon submitted)."""
    printer, feeder = make_feeder(values={
        'flush_callback_chunk_mm': 45.0,
        'interrupt_chunk_mm': 9.0,
        'feed_speed': 70.0,
    })
    motion_q = printer.lookup_object('motion_queuing')
    set_sensor_active(feeder, 'hall_empty', True)

    # Erstes flush: HALL3 → submitted ersten 9-mm-Sub-Chunk, queues 36mm.
    feeder.reactor.now = 5.00
    motion_q.trigger_flush(flush_time=5.00, step_gen_time=5.05)
    assert feeder._pending_remaining_mm == pytest.approx(36.0, abs=0.001)
    end_time_after_first = feeder._last_move_end_time

    # HALL2 schaltet auf True (HALL3 gleichzeitig inaktiv, Mellow
    # LLL Plus hat HALL3/HALL2 mutex-Pattern).
    set_sensor_active(feeder, 'hall_empty', False)
    set_sensor_active(feeder, 'hall_full', True)

    appends_before = len(motion_q.append_calls)
    motion_q.trigger_flush(flush_time=5.06, step_gen_time=5.10)

    new_appends = [c for c in motion_q.append_calls[appends_before:]
                   if c[0] is feeder.trapq]
    assert not new_appends, "HALL2 bricht Streaming nicht ab"
    assert feeder._continuous_feed is False
    assert feeder._pending_remaining_mm == 0.0, (
        "P7-66b: HALL2 in _on_mcu_flush muss pending_remaining_mm "
        "clearen — sonst überschiesst der Sub-Chunk-Stream noch um "
        "bis zu pending mm")
    # Der bereits in flight befindliche 9-mm-Sub-Chunk ist nicht
    # zurückrollbar: _last_move_end_time bleibt = end_time_after_first.
    assert feeder._last_move_end_time == pytest.approx(
        end_time_after_first, abs=0.001), (
        "_last_move_end_time wurde unerwartet verändert — der erste "
        "Sub-Chunk darf nicht rückgängig gemacht werden")


# ---------------------------------------------------------------------------
# 4.3 — Streaming → HALT komplett (Codex-Finding).
# ---------------------------------------------------------------------------

def test_streaming_then_halt_motion_clears_all_pending():
    """Codex 4.3: Streaming aktiv (Chunk in flight + Lookahead sub-
    chunks pending), dann wird _halt_motion() gerufen. Verify:
      - _continuous_feed=False
      - _pending_remaining_mm=0
      - keine weiteren Submits via nachfolgendem flush oder tick."""
    printer, feeder = make_feeder(values={
        'flush_callback_chunk_mm': 45.0,
        'interrupt_chunk_mm': 9.0,
    })
    motion_q = printer.lookup_object('motion_queuing')
    set_sensor_active(feeder, 'hall_empty', True)

    # Aktiviere Streaming + pending sub-chunks.
    feeder.reactor.now = 5.00
    motion_q.trigger_flush(flush_time=5.00, step_gen_time=5.05)
    assert feeder._continuous_feed is True
    assert feeder._pending_remaining_mm > 0

    # HALT: simuliert OVERFLOW/JAM/User-HALT.
    feeder._halt_motion()
    assert feeder._continuous_feed is False
    assert feeder._pending_remaining_mm == 0.0
    assert feeder._pending_submit_chunk_cap is None, (
        "_halt_motion muss den sub-chunk-Cap droppen, sonst erbt "
        "ein nachfolgender unverwandter _submit_move ihn unerwartet")

    # Nachfolgender flush + tick darf nicht erneut submitten.
    appends_before = len(motion_q.append_calls)
    feeder._tick_pending_chunk(eventtime=5.05)
    # HALL3 ist noch aktiv, aber _continuous_feed=False. _on_mcu_flush
    # könnte einen neuen Stream starten — das ist erwartetes Verhalten,
    # NICHT was wir hier testen. Wir prüfen nur den tick-Pfad.
    new_appends = [c for c in motion_q.append_calls[appends_before:]
                   if c[0] is feeder.trapq]
    assert not new_appends, (
        "_tick_pending_chunk hat nach _halt_motion erneut submittet "
        "— pending state nicht sauber gecleared")


# ---------------------------------------------------------------------------
# Reaktions-Latenz-Charakterisierung (Hardware-Plausibilitäts-Check).
# ---------------------------------------------------------------------------

def test_interrupt_latency_within_one_sub_chunk():
    """Reaktions-Latenz-Bound: bei interrupt_chunk_mm=9 und feed_speed=
    70 mm/s ist die maximale Overshoot-Distanz nach HALL2-Trigger ein
    sub-chunk = 9 mm = 128 ms Trapq-Dauer. Plus tick-Polling-Periode
    (50 Hz = 20 ms) + flush-callback-Periode (typisch 50-500 ms).
    Diese Werte sind keine Code-Behauptung, sondern eine
    Charakterisierung — wenn jemand interrupt_chunk_mm später ändert,
    soll dieser Test als Bewusstseinsanker an die HW-Garantie
    erinnern."""
    _, feeder = make_feeder(values={
        'flush_callback_chunk_mm': 45.0,
        'interrupt_chunk_mm': 9.0,
        'feed_speed': 70.0,
    })
    # Max Overshoot = interrupt_chunk_mm wenn HALL2 genau VOR dem
    # nächsten sub-chunk-Submit triggert. Plus 20 ms tick = +1.4 mm.
    sub_chunk_duration_ms = feeder.interrupt_chunk_mm / feeder.feed_speed * 1000
    assert sub_chunk_duration_ms < 200, (
        "Sub-Chunk dauert > 200 ms → HALL2-Reaktion zu langsam für "
        "Mellow LLL Plus. interrupt_chunk_mm zu groß oder feed_speed "
        "zu klein. Dauer: %.1f ms" % sub_chunk_duration_ms)
    assert feeder.interrupt_chunk_mm <= feeder.flush_callback_chunk_mm, (
        "interrupt_chunk_mm > flush_callback_chunk_mm macht keinen "
        "Sinn — Move-Splitting wäre dann eine No-Op")
