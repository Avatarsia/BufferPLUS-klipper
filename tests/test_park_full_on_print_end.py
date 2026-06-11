"""park_full_on_print_end — Buffer nach Druckende einmalig bis HALL2 fuellen.

Motivation (Hardware 2026-06-11): Zwischen Drucken steht der Buffer oft
in der HALL-Hysterese-Zwischenzone. Dort feuert der Watchdog alle
idle_anchor_gap Sekunden einen 0.05mm-Anchor-Move — hoerbar als
periodisches Knacken. Bei hall_full ist der Anchor hart gegated
(AUTO-Sub-Gates in _main_tick). Ein einmaliger Fill bis HALL2 nach
Druckende (complete UND cancelled) macht den Feeder dauerhaft still.

Latch-Semantik (Lektion aus PR #49): _park_full_attempted wird VOR dem
Submit gesetzt (sonst wuerde ein Fill, der park_full_max_mm vor HALL2
erschoepft, bei jedem Anchor-Flap erneut feuern und Richtung Overflow
kriechen) und in _on_idle_printing bei print_stats 'printing'/'paused'
re-armiert — NICHT im nicht-paused :ready-Zweig, denn diese Kante
feuert zwischen RESUME und dem naechsten Druckende nie.
"""

from fakes_klipper import FakeConfig, FakePrinter, FakePrintStats
from helpers import set_sensor_active
from klipper_extras import buffer_feeder


PARK_MSG_FRAGMENT = "parking buffer at full"


def make_feeder(values=None, state=None):
    """Lokaler Helper im Repo-Muster; fire_event NACH Instanziierung,
    weil der Park-Pfad _enable_stepper exercisen kann (NOT-TO-DO
    2026-05-12: FakePrinter ruft connect-Handler nie selbst auf)."""
    printer = FakePrinter()
    config = FakeConfig(printer=printer, values=values)
    feeder = buffer_feeder.BufferFeeder(config)
    printer.fire_event('klippy:connect')
    feeder._startup_grace_done = True
    feeder._state = state if state is not None else buffer_feeder.STATE_AUTO
    # Park-faehige Sensorlage: Filament am Eingang, Zwischenzone.
    set_sensor_active(feeder, 'entrance', True)
    set_sensor_active(feeder, 'hall_empty', False)
    set_sensor_active(feeder, 'hall_full', False)
    set_sensor_active(feeder, 'hall_overflow', False)
    return printer, feeder


def _park_msgs(printer):
    gc = printer.objects['gcode']
    return [m for m in gc.info_messages if PARK_MSG_FRAGMENT in m]


def _print_end_ready(feeder, printer, ps_state):
    printer.objects['print_stats'] = FakePrintStats(state=ps_state)
    feeder._print_running = True
    feeder._on_idle_ready()


def test_park_triggers_on_complete():
    printer, feeder = make_feeder()
    _print_end_ready(feeder, printer, 'complete')

    assert len(_park_msgs(printer)) == 1
    assert feeder._state == buffer_feeder.STATE_AUTO
    # 150mm Default minus erster Sub-Chunk steht als Pending-Stream an;
    # _tick_pending_chunk stoppt ihn bei hall_full (P7-66b-Kante).
    assert feeder._pending_remaining_mm > 0
    assert feeder._pending_direction > 0
    assert feeder._park_full_attempted is True


def test_park_triggers_on_cancelled():
    printer, feeder = make_feeder()
    _print_end_ready(feeder, printer, 'cancelled')

    assert len(_park_msgs(printer)) == 1
    assert feeder._pending_remaining_mm > 0


def test_park_heals_stale_suspend_from_pause_cancel():
    """PAUSE -> CANCEL laesst _bang_bang_suspended stale True zurueck
    (kein heilendes :ready-Event). Der Park-Trigger ist ein designierter
    Lazy-Heal-Decision-Point und muss trotzdem parken."""
    printer, feeder = make_feeder()
    feeder._bang_bang_suspended = True  # stale aus PAUSE->CANCEL
    _print_end_ready(feeder, printer, 'cancelled')

    assert feeder._bang_bang_suspended is False
    assert len(_park_msgs(printer)) == 1


def test_park_one_shot_across_anchor_flaps():
    """Nach Druckende flappt der Anchor idle_timeout printing<->ready
    alle ~10s weiter (state bleibt 'complete'). Park darf nur EINMAL
    feuern — auch wenn der Fill den Buffer nicht bis HALL2 gebracht hat
    (park_full_max_mm erschoepft), sonst Kriechen Richtung Overflow."""
    printer, feeder = make_feeder()
    printer.objects['print_stats'] = FakePrintStats(state='complete')

    for _ in range(4):
        feeder._on_idle_printing()
        feeder._on_idle_ready()
        # Fill-Ende simulieren: Pending-Stream abgelaufen, HALL2 NICHT
        # erreicht (worst case) -> Guards waeren wieder park-faehig.
        feeder._pending_remaining_mm = 0.0

    assert len(_park_msgs(printer)) == 1


def test_park_rearms_after_new_print():
    printer, feeder = make_feeder()
    _print_end_ready(feeder, printer, 'complete')
    assert len(_park_msgs(printer)) == 1
    # Fill beendet: Pending-Stream leer + in-flight Trapezoid gedraint.
    feeder._pending_remaining_mm = 0.0
    feeder._current_move = None

    # Neuer Druck: idle_timeout:printing mit state='printing' re-armiert.
    printer.objects['print_stats'] = FakePrintStats(state='printing')
    feeder._on_idle_printing()
    assert feeder._park_full_attempted is False

    _print_end_ready(feeder, printer, 'complete')
    assert len(_park_msgs(printer)) == 2


def test_park_skipped_when_hall_full():
    printer, feeder = make_feeder()
    set_sensor_active(feeder, 'hall_full', True)
    _print_end_ready(feeder, printer, 'complete')

    assert _park_msgs(printer) == []
    assert feeder._pending_remaining_mm == 0.0


def test_park_skipped_without_entrance_but_not_latched():
    """Ohne Filament am Eingang: kein Park, ABER kein Latch — steckt
    der User spaeter Filament ein und ein weiterer Flap kommt, darf
    der Park noch stattfinden (bzw. Auto-Grip uebernimmt)."""
    printer, feeder = make_feeder()
    set_sensor_active(feeder, 'entrance', False)
    _print_end_ready(feeder, printer, 'complete')

    assert _park_msgs(printer) == []
    assert feeder._park_full_attempted is False


def test_park_disabled_by_config():
    printer, feeder = make_feeder(values={'park_full_on_print_end': False})
    _print_end_ready(feeder, printer, 'complete')

    assert _park_msgs(printer) == []
    assert feeder._pending_remaining_mm == 0.0


def test_park_respects_auto_off_by_user():
    printer, feeder = make_feeder()
    feeder._auto_off_by_user = True
    _print_end_ready(feeder, printer, 'complete')

    assert _park_msgs(printer) == []


def test_park_from_idle_enables_and_engages_auto():
    printer, feeder = make_feeder(state=buffer_feeder.STATE_IDLE)
    _print_end_ready(feeder, printer, 'complete')

    assert len(_park_msgs(printer)) == 1
    assert feeder._state == buffer_feeder.STATE_AUTO
    assert feeder._pending_remaining_mm > 0


def test_park_not_triggered_by_pause():
    """paused laeuft in den Pause-Branch — Park darf dort nie feuern."""
    printer, feeder = make_feeder()
    _print_end_ready(feeder, printer, 'paused')

    assert _park_msgs(printer) == []
    assert feeder._park_full_attempted is False


def _advance_to_continuation_window(feeder):
    """reactor.now so setzen, dass _tick_pending_chunk im
    Lookahead-Fenster (gap <= halbe Chunk-Duration) submitted."""
    feeder.reactor.now = feeder._current_move['end_time'] - (
        feeder._pending_submit_chunk_cap / feeder._pending_speed) * 0.4


def test_park_stream_survives_zero_demand_modulator():
    """Codex-Verify 2026-06-11 must-fix: Nach Druckende liefert der
    AUTO-Demand-Modulator 0 (keine Extruder-Velocity, HALL-Totzone).
    Ohne _park_full_active wuerde der erste Continuation-Tick den
    Pending-Stream nullen (repro: 141.0 -> 0.0) und der Park endet
    nach einem Sub-Chunk bei bereits gesetztem Latch."""
    printer, feeder = make_feeder()
    _print_end_ready(feeder, printer, 'complete')
    before = feeder._pending_remaining_mm
    assert before > 0
    assert feeder._park_full_active is True

    _advance_to_continuation_window(feeder)
    feeder._tick_pending_chunk(feeder.reactor.now)

    assert feeder._pending_remaining_mm > 0, (
        "Park-Stream darf vom Demand-0-Modulator nicht beendet werden")
    assert feeder._pending_remaining_mm < before, (
        "Continuation-Tick muss den naechsten Sub-Chunk submitten")


def test_park_stream_aborts_on_hall_full_mid_stream():
    """HALL2 zwischen Sub-Chunks beendet den Park-Stream und raeumt
    das Active-Flag — die P7-66b-Stop-Kante gilt auch fuer Park."""
    printer, feeder = make_feeder()
    _print_end_ready(feeder, printer, 'complete')
    assert feeder._pending_remaining_mm > 0

    set_sensor_active(feeder, 'hall_full', True)
    _advance_to_continuation_window(feeder)
    feeder._tick_pending_chunk(feeder.reactor.now)

    assert feeder._pending_remaining_mm == 0.0
    assert feeder._park_full_active is False


def test_park_flag_not_leaked_for_single_trapezoid_park():
    """Codex-Verify R2: park_full_max_mm <= interrupt_chunk_mm queued
    nur ein Trapezoid ohne Pending-Stream — das Active-Flag darf dann
    nicht ueber das Move-Ende hinaus gesetzt bleiben."""
    printer, feeder = make_feeder(values={'park_full_max_mm': 2.0})
    _print_end_ready(feeder, printer, 'complete')

    assert len(_park_msgs(printer)) == 1
    assert feeder._pending_remaining_mm == 0.0
    assert feeder._park_full_active is False


def test_park_flag_cleared_by_halt_motion():
    printer, feeder = make_feeder()
    _print_end_ready(feeder, printer, 'complete')
    assert feeder._park_full_active is True

    feeder._halt_motion()

    assert feeder._park_full_active is False
    assert feeder._pending_remaining_mm == 0.0
