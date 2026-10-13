"""Idle-Anchor-Speed separat konfigurierbar (User-Request 2026-07-14).

Der Watchdog-Anchor ist hoerbar (Enable-Klack + Mikro-Move alle
~idle_anchor_gap). Der Step-Anteil wird leiser mit niedriger Speed —
NUR fuer die Idle-Watchdog-Anchors (IDLE + quiescent AUTO). Boot-
Anchor, Pre-Sync-REPRIME und der P7-78-Print-Override behalten
10 mm/s (dort ist Geraeusch irrelevant bzw. der Move soll schnell
durch sein).
"""

from fakes_klipper import FakeConfig, FakePrinter
from klipper_extras import buffer_feeder


def set_sensor_active(feeder, sensor_name, active):
    polarity_flip = feeder._pin_polarity_flip[sensor_name]
    raw = (not active) if polarity_flip else active
    feeder._pin_stable_state[sensor_name] = raw
    feeder._pin_raw_state[sensor_name] = raw


def make_idle_feeder(values=None):
    printer = FakePrinter()
    feeder = buffer_feeder.BufferFeeder(FakeConfig(printer=printer, values=values))
    printer.fire_event('klippy:connect')
    feeder._startup_grace_done = True
    feeder._state = buffer_feeder.STATE_IDLE
    for name in ('hall_overflow', 'hall_full', 'hall_empty', 'entrance'):
        set_sensor_active(feeder, name, False)
    feeder.reactor.now = 20.0
    feeder._last_move_end_time = 0.0
    return printer, feeder


def submit_spy(monkeypatch, feeder):
    calls = []

    def _spy(dist, speed, **kw):
        calls.append((dist, speed, kw))
        feeder._last_move_end_time = 20.001
    monkeypatch.setattr(feeder, "_submit_move", _spy)
    return calls


def test_idle_watchdog_anchor_uses_idle_anchor_speed(monkeypatch):
    _, feeder = make_idle_feeder()
    calls = submit_spy(monkeypatch, feeder)

    feeder._main_tick(eventtime=20.0)

    assert len(calls) == 1
    assert calls[0][1] == feeder.idle_anchor_speed == 2.0, (
        "idle watchdog anchor must use the (quieter) idle_anchor_speed "
        "default 2.0 mm/s")


def test_idle_anchor_speed_configurable(monkeypatch):
    _, feeder = make_idle_feeder(values={'idle_anchor_speed': '1.0'})
    calls = submit_spy(monkeypatch, feeder)

    feeder._main_tick(eventtime=20.0)

    assert calls[0][1] == 1.0


def test_boot_anchor_keeps_default_speed(monkeypatch):
    _, feeder = make_idle_feeder()
    calls = submit_spy(monkeypatch, feeder)

    feeder.sync.anchor_step()

    assert len(calls) == 1
    assert calls[0][1] == 10.0, (
        "boot anchor / pre-sync REPRIME keep 10 mm/s — only the idle "
        "watchdog is the audible repeating one")
