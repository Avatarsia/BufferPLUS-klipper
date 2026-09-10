"""P7-78-Logflut: flankengetriggerte Protokollierung statt Tick-Spam.

Die Meldung "print-block stale override" wurde bei jedem _main_tick
geschrieben (MAIN_TICK_INTERVAL = 0.02, also bis zu 50 Hz). Im Testdruck
2026-09-09 waren das 124678 von 223677 Logzeilen. Wurzel: Die Zeile sitzt
auf Tick-Ebene, aufloesen laesst sich der Zustand aber nur durch einen
echten Flush-Callback (_on_mcu_flush ist der einzige Schreiber von
_last_mcu_flush_time). Im Silent-Modus ist darunter keine Aktion
erreichbar, die einen Flush ausloest.

Siehe docs/superpowers/specs/2026-09-10-p778-logflut-design.md und
Upstream-Issue Avatarsia/BufferPLUS-klipper#59.

Hinweis zum Setup: FakeReactor startet bei now=0.0, die P7-78-Bedingung
verlangt _last_mcu_flush_time > 0.0 (Boot-Schutz). Die Reactor-Uhr muss
also vorgestellt werden, sonst wird der Block nie betreten.
"""
import logging

from klipper_extras import buffer_feeder

ARMED = 'print-block stale override armed'
ALIVE = 'print-block stale override still active'
CLEARED = 'print-block stale override cleared'
ALT = 'print-block stale override ('   # alte, ungedrosselte Fassung


def _feeder_im_override(feeder_factory, mode='silent'):
    printer, feeder = feeder_factory(
        values={'idle_anchor_mode': mode,
                'idle_motor_disable': True,
                'idle_anchor_gap': 10.0},
        state=buffer_feeder.STATE_AUTO)
    printer.objects['print_stats'].state = 'printing'
    feeder.reactor.now = 1000.0
    feeder._last_mcu_flush_time = 900.0     # 100 s Stille
    return printer, feeder


def _msgs(caplog):
    return [r.getMessage() for r in caplog.records]


def test_override_loggt_nur_einmal_beim_eintritt(feeder_factory, caplog):
    """100 Ticks im selben Override-Zustand duerfen nicht 100 Zeilen
    erzeugen. Vor dem Fix: 100 Zeilen."""
    _, feeder = _feeder_im_override(feeder_factory)

    with caplog.at_level(logging.DEBUG, logger=""):
        for _ in range(100):
            feeder._main_tick(feeder.reactor.monotonic())

    msgs = _msgs(caplog)
    armed = [m for m in msgs if ARMED in m]
    alt = [m for m in msgs if ALT in m]
    assert len(armed) == 1, (
        "genau eine Eintrittszeile erwartet, %d bekommen" % len(armed))
    assert not alt, (
        "alte ungedrosselte Fassung feuert noch: %d Zeilen" % len(alt))


def test_lebenszeichen_hoechstens_einmal_pro_fenster(feeder_factory, caplog):
    """Bleibt der Zustand ueber mehrere idle_anchor_gap-Fenster bestehen,
    kommt pro Fenster hoechstens eine Zeile — nicht pro Tick."""
    _, feeder = _feeder_im_override(feeder_factory)

    with caplog.at_level(logging.DEBUG, logger=""):
        # 3 Fenster a 10 s ueberstreichen, Uhr in 1-s-Schritten vorstellen
        for schritt in range(35):
            feeder.reactor.now = 1000.0 + schritt
            feeder._main_tick(feeder.reactor.monotonic())

    msgs = _msgs(caplog)
    gesamt = len([m for m in msgs if ARMED in m or ALIVE in m])
    assert gesamt <= 4, (
        "bei 35 s und 10-s-Fenster hoechstens 4 Zeilen erwartet, %d bekommen"
        % gesamt)
    assert gesamt >= 2, (
        "mindestens Eintritt plus ein Lebenszeichen erwartet, %d bekommen"
        % gesamt)


def test_austritt_meldet_dauer_und_tickzahl(feeder_factory, caplog):
    """Loest sich der Zustand auf, kommt genau eine Abschlusszeile mit
    Gesamtdauer und Tickzahl. Diese Groessen gibt es vor dem Fix nicht."""
    _, feeder = _feeder_im_override(feeder_factory)

    with caplog.at_level(logging.DEBUG, logger=""):
        for _ in range(20):
            feeder._main_tick(feeder.reactor.monotonic())
        # Flush-Callback simulieren: Stille aufgeloest
        mcu = feeder.stepper.get_mcu()
        feeder._last_mcu_flush_time = mcu.estimated_print_time(
            feeder.reactor.monotonic())
        feeder._main_tick(feeder.reactor.monotonic())

    msgs = _msgs(caplog)
    cleared = [m for m in msgs if CLEARED in m]
    assert len(cleared) == 1, (
        "genau eine Abschlusszeile erwartet, %d bekommen" % len(cleared))
    assert 'ticks=' in cleared[0], "Tickzahl fehlt: %s" % cleared[0]
    assert 'duration=' in cleared[0], "Gesamtdauer fehlt: %s" % cleared[0]


def test_kontrollfluss_unveraendert_kein_anchor_kein_disable(feeder_factory,
                                                             caplog):
    """Commit 1 aendert nur die Protokollierung. Im Silent-Modus darf
    weiterhin weder ein Anchor noch ein Disable ausgeloest werden."""
    _, feeder = _feeder_im_override(feeder_factory)

    with caplog.at_level(logging.DEBUG, logger=""):
        for _ in range(50):
            feeder._main_tick(feeder.reactor.monotonic())

    msgs = _msgs(caplog)
    assert not [m for m in msgs if 'anchor fired' in m], "Anchor gefeuert"
    assert not [m for m in msgs if 'silent idle-disable' in m], "Disable gefeuert"
