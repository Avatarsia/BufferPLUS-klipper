"""Silent-Idle-Disable unabhaengig vom P7-78-Override.

Ausgangslage: Das aeussere Anchor-Gate in _main_tick verlangt
`not _print_active`. Waehrend eines laufenden Drucks ist das nur dadurch
erfuellbar, dass der P7-78-Override _print_active auf False setzt. Der
Override ist damit der einzige Tueroeffner fuer den Silent-Idle-Disable —
obwohl der mit Anchor-Bewegung nichts zu tun hat und keine Steps erzeugt.

Folge: Solange kein Flush-Callback ausgeblieben ist (also der Override gar
nicht greift), bleibt der Motor waehrend eines Drucks bestromt, auch wenn
der Buffer laengst still steht. Genau dagegen wurde idle_motor_disable
eingefuehrt (User-Report 2026-07-13: Motor kochend heiss im Standby).

Der Fix loest den Disable aus dem Anchor-Gate heraus: er behaelt die
Sicherheitsbedingungen, die er wirklich braucht, aber nicht
`not _print_active`.

Siehe docs/superpowers/specs/2026-09-10-p778-logflut-design.md.
"""
import logging

from klipper_extras import buffer_feeder
from helpers import set_sensor_active

DISABLE = 'silent idle-disable'
ARMED = 'print-block stale override armed'


def _feeder(feeder_factory, mode='silent', state=None):
    printer, feeder = feeder_factory(
        values={'idle_anchor_mode': mode,
                'idle_motor_disable': True,
                'idle_anchor_gap': 10.0},
        state=state or buffer_feeder.STATE_IDLE)
    printer.objects['print_stats'].state = 'printing'
    feeder.reactor.now = 1000.0
    # Sensoren explizit in Ruhelage: sonst greift der HALL1-Persist-Pfad
    # und der Feeder wechselt nach OVERFLOW statt in IDLE zu bleiben.
    for sensor in ('hall_overflow', 'hall_full', 'hall_empty'):
        set_sensor_active(feeder, sensor, False)
    set_sensor_active(feeder, 'entrance', True)
    return printer, feeder


def _msgs(caplog):
    return [r.getMessage() for r in caplog.records]


def test_disable_greift_bei_laufendem_druck_ohne_override(feeder_factory,
                                                          caplog):
    """Kernfall: Buffer still, Druck laeuft, Flush-Callback ist FRISCH —
    der Override greift also nicht. Der Disable muss trotzdem ausloesen.

    Vor dem Fix blockiert `not _print_active` das Gate und der Motor
    bleibt bestromt."""
    _, feeder = _feeder(feeder_factory)
    mcu_now = feeder.stepper.get_mcu().estimated_print_time(
        feeder.reactor.monotonic())
    feeder._last_mcu_flush_time = mcu_now      # frisch -> kein Override
    feeder._last_move_end_time = mcu_now - 60.0  # 60 s Bewegungsstille

    with caplog.at_level(logging.DEBUG, logger=""):
        for _ in range(5):
            feeder._main_tick(feeder.reactor.monotonic())

    msgs = _msgs(caplog)
    assert not [m for m in msgs if ARMED in m], (
        "Override haette hier gar nicht greifen duerfen: %s" % msgs[:3])
    assert [m for m in msgs if DISABLE in m], (
        "Silent-Idle-Disable hat nicht ausgeloest. Alle Meldungen: %s"
        % [m for m in msgs if 'buffer_feeder' in m][:5])


def test_disable_bleibt_one_shot(feeder_factory, caplog):
    """Der Latch muss erhalten bleiben: bei 50 Ticks genau eine Ausloesung,
    sonst wuerde jeder Tick _last_enable_schedule_time fortschieben."""
    _, feeder = _feeder(feeder_factory)
    mcu_now = feeder.stepper.get_mcu().estimated_print_time(
        feeder.reactor.monotonic())
    feeder._last_mcu_flush_time = mcu_now
    feeder._last_move_end_time = mcu_now - 60.0

    with caplog.at_level(logging.DEBUG, logger=""):
        for _ in range(50):
            feeder._main_tick(feeder.reactor.monotonic())

    treffer = [m for m in _msgs(caplog) if DISABLE in m]
    assert len(treffer) == 1, (
        "genau eine Ausloesung erwartet, %d bekommen" % len(treffer))


def test_move_modus_anchor_unveraendert(feeder_factory, caplog):
    """Regressionsschutz: Im move-Modus darf der Disable NICHT ueber den
    neuen Pfad feuern — dort haengt er am Anchor."""
    _, feeder = _feeder(feeder_factory, mode='move')
    mcu_now = feeder.stepper.get_mcu().estimated_print_time(
        feeder.reactor.monotonic())
    feeder._last_mcu_flush_time = mcu_now
    feeder._last_move_end_time = mcu_now - 60.0

    with caplog.at_level(logging.DEBUG, logger=""):
        for _ in range(20):
            feeder._main_tick(feeder.reactor.monotonic())

    assert not [m for m in _msgs(caplog) if DISABLE in m], (
        "Silent-Disable darf im move-Modus nicht feuern")


def test_kein_disable_bei_laufender_bewegung(feeder_factory, caplog):
    """Sicherheitsbedingung: Bewegungsstille kuerzer als idle_anchor_gap
    darf keinen Disable ausloesen."""
    _, feeder = _feeder(feeder_factory)
    mcu_now = feeder.stepper.get_mcu().estimated_print_time(
        feeder.reactor.monotonic())
    feeder._last_mcu_flush_time = mcu_now
    feeder._last_move_end_time = mcu_now - 1.0   # nur 1 s Stille

    with caplog.at_level(logging.DEBUG, logger=""):
        for _ in range(20):
            feeder._main_tick(feeder.reactor.monotonic())

    assert not [m for m in _msgs(caplog) if DISABLE in m], (
        "Disable trotz zu kurzer Bewegungsstille")
