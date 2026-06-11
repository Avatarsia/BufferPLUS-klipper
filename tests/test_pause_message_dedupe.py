"""Pause-/Print-ended-Meldungen duerfen nur EINMAL pro Episode
erscheinen, nicht im 10s-Takt gespammt.

Hardware-Beleg 2026-06-10 klippy.log: Waehrend einer durch den
encoder_sensor ausgeloesten PAUSE erschien
``BufferFeeder: Print paused — bang-bang suspended until RESUME``
1128 Mal (564 logisch, alle ~10s).

Wurzel (verifiziert gegen Klipper-Core idle_timeout.py + virtual_-
sdcard.py): Der Buffer feuert waehrend der Pause alle 10s seinen
Idle-Anchor-Move (idle_anchor_gap, noetig gegen stepcompress-Clock-
Drift). Jeder Anchor bumpt ueber ``toolhead:sync_print_time`` Klippers
idle_timeout-Statemachine printing->ready. Dadurch feuern
``idle_timeout:printing`` (_on_idle_printing) und ``idle_timeout:ready``
(_on_idle_ready) alle 10s erneut; _on_idle_ready sendete die Pause-
Meldung bei jedem Durchlauf.

Fix-Architektur (PR-Review-Runde 2):
- Meldung wird in _on_idle_ready per Latch ``_pause_msg_shown``
  dedupliziert (einmal pro Pause-Episode).
- Der Latch-RESET sitzt NICHT in den idle_timeout-Handlern: Resume
  und Anchor-Flap sind im Event-Moment nicht unterscheidbar (beide
  lesen print_stats.state=='paused', weil note_start deferred im
  work_handler-Timer laeuft). Ein Reset dort wuerde entweder den
  Resume-Trigger mit-unterdruecken oder bei jedem Flap re-armen.
- Stattdessen Reset in _refresh_print_phase sobald ps_state !=
  'paused' gelesen wird. Dieser Pfad laeuft in der Realitaet
  garantiert: via _auto_submit_permission bei jedem Flush-Callback
  UND via get_status bei jedem Moonraker-Statuspoll (~1x/s). Die
  Tests simulieren diesen Tick explizit (siehe _flush_tick).
- PAUSE->CANCEL (kein ready-Event mehr): _clear_stale_suspend_if_-
  print_inactive raeumt den Latch zusammen mit dem Suspend-Flag.

Should-fix derselben Klasse: ``Print ended — buffer ready ...`` haengt
am identischen Flap (bei state='complete'/'cancelled' greift der nur-
'standby'-Fruehausstieg in _on_idle_printing nicht) und bekommt
denselben Latch (``_print_end_msg_shown``), Reset bei printing/paused.
"""

from fakes_klipper import FakeConfig, FakePrinter, FakePrintStats
from klipper_extras import buffer_feeder


PAUSE_MSG_FRAGMENT = "bang-bang suspended until RESUME"
END_MSG_FRAGMENT = "Print ended"


def make_feeder(values=None):
    printer = FakePrinter()
    config = FakeConfig(printer=printer, values=values)
    feeder = buffer_feeder.BufferFeeder(config)
    # Enable-Pfad verdrahten, damit Faelle mit _continuous_feed/
    # _halt_motion (Review-Nit PR #49) ohne Setup-Drift ergaenzbar sind.
    printer.fire_event('klippy:connect')
    feeder._startup_grace_done = True
    feeder._state = buffer_feeder.STATE_AUTO  # kein JAM/auto-engage in _on_idle_printing
    return printer, feeder


def _msgs(printer, fragment):
    gc = printer.objects['gcode']
    return [m for m in gc.info_messages if fragment in m]


def _flush_tick(feeder):
    """Ein Flush-/Statuspoll-Tick. In der Realitaet laeuft
    _refresh_print_phase kontinuierlich: via _auto_submit_permission
    bei jedem _on_mcu_flush-Callback und via get_status bei jedem
    Moonraker-Poll (~1x/s, auch waehrend Pause — siehe
    flush_skip_permission-Events im Hardware-Log 2026-06-10)."""
    feeder._refresh_print_phase(feeder.reactor.monotonic())


def _simulate_pause_cycle(feeder):
    """Ein 10s-Anchor-Zyklus waehrend einer Pause: idle_timeout kippt
    printing->ready, dazwischen/danach laufen Flush-/Statuspoll-Ticks
    mit ps_state='paused'. Reproduziert die echte Event-Reihenfolge."""
    feeder._on_idle_printing()  # idle_timeout:printing (state='paused'!)
    feeder._on_idle_ready()     # idle_timeout:ready (Pause-Branch)
    _flush_tick(feeder)         # Poll-Tick liest 'paused' -> darf NICHT resetten


def test_pause_message_emitted_once_across_repeated_idle_ready():
    """Sechs Anchor-Zyklen (inkl. Flush-Ticks) waehrend EINER Pause
    -> genau EINE Meldung."""
    printer, feeder = make_feeder()
    printer.objects['print_stats'] = FakePrintStats(state='paused')

    for _ in range(6):
        _simulate_pause_cycle(feeder)

    msgs = _msgs(printer, PAUSE_MSG_FRAGMENT)
    assert len(msgs) == 1, (
        "Pause-Meldung soll genau einmal pro Pause erscheinen, "
        "war aber %d mal da: %s" % (len(msgs), msgs))


def test_second_pause_of_same_print_announces_again():
    """Maintainer-Review Must-fix 1 (PR #49): Pause #2 desselben Drucks
    muss wieder gemeldet werden. Echte Event-Sequenz: beim RESUME feuert
    nur idle_timeout:printing (waehrend print_stats noch 'paused' ist,
    note_start deferred im work_handler); KEIN idle_timeout:ready bis
    zur naechsten Pause (lookahead busy / gcode-Mutex blockt die
    Printing->Ready-Transition waehrend des Drucks)."""
    printer, feeder = make_feeder()
    ps = FakePrintStats(state='paused')
    printer.objects['print_stats'] = ps

    # ---- Pause 1: drei Anchor-Flaps ----
    for _ in range(3):
        _simulate_pause_cycle(feeder)
    assert len(_msgs(printer, PAUSE_MSG_FRAGMENT)) == 1

    # ---- Echtes RESUME ----
    # idle_timeout:printing feuert waehrend print_stats noch 'paused'
    # ist (note_start deferred im work_handler).
    feeder._on_idle_printing()
    ps.state = 'printing'
    # Druck laeuft kontinuierlich: KEIN idle_timeout:ready bis zur
    # naechsten Pause — aber Flush-Callbacks und Moonraker-Statuspolls
    # laufen weiter und lesen jetzt 'printing'.
    _flush_tick(feeder)

    # ---- Pause 2 desselben Drucks ----
    ps.state = 'paused'
    for _ in range(3):
        _simulate_pause_cycle(feeder)

    assert len(_msgs(printer, PAUSE_MSG_FRAGMENT)) == 2, (
        "Pause #2 desselben Drucks muss wieder gemeldet werden")


def test_pause_cancel_heals_latch_for_next_print():
    """PAUSE -> CANCEL: kein weiteres ready-Event (idle_timeout bleibt
    in Ready). _clear_stale_suspend_if_print_inactive heilt den Suspend
    lazy — und muss den Pause-Latch mitraeumen, sonst bleibt die erste
    Pause des NAECHSTEN Drucks stumm."""
    printer, feeder = make_feeder()
    ps = FakePrintStats(state='paused')
    printer.objects['print_stats'] = ps

    # Pause mit Meldung
    _simulate_pause_cycle(feeder)
    assert len(_msgs(printer, PAUSE_MSG_FRAGMENT)) == 1

    # CANCEL statt RESUME — lazy-heal laeuft an einem Decision-Point
    ps.state = 'cancelled'
    cleared = feeder._clear_stale_suspend_if_print_inactive(
        feeder.reactor.monotonic())
    assert cleared is True

    # Naechster Druck, erste Pause -> muss wieder melden
    ps.state = 'paused'
    feeder._print_running = True
    feeder._on_idle_ready()

    assert len(_msgs(printer, PAUSE_MSG_FRAGMENT)) == 2, (
        "Erste Pause des naechsten Drucks nach PAUSE->CANCEL muss "
        "gemeldet werden")


def test_second_pause_announces_via_main_tick_only():
    """Adversarial-Review-Befund (Runde 2): Der Flush-Pfad erreicht
    _refresh_print_phase NUR in STATE_AUTO mit Feed-Demand — nach einer
    Pause ist der Buffer typisch voll (hall_full -> target_speed=0) und
    _flush_submit_streaming_chunk returnt VOR dem Permission-Check.
    get_status haengt am Moonraker-Poll (out-of-process, headless ggf.
    nie). Der Latch-Reset braucht daher einen in-process garantierten
    Anker: _main_tick (50Hz-Reactor-Timer, laeuft immer — feuert ja
    auch den Anchor waehrend der Pause), throttled auf ~1x/s.

    Szenario: Resume ohne JEDEN Flush-/Statuspoll-Tick — nur
    _main_tick laeuft. Pause #2 muss trotzdem melden."""
    printer, feeder = make_feeder()
    ps = FakePrintStats(state='paused')
    printer.objects['print_stats'] = ps

    # Pause 1
    for _ in range(3):
        feeder._on_idle_printing()
        feeder._on_idle_ready()
    assert len(_msgs(printer, PAUSE_MSG_FRAGMENT)) == 1

    # Echtes RESUME (idle_timeout:printing liest noch 'paused'),
    # danach laeuft NUR der Reactor-Tick — kein Flush, kein get_status.
    feeder._on_idle_printing()
    ps.state = 'printing'
    feeder.reactor.now = 50.0
    feeder._main_tick(eventtime=50.0)

    # Pause 2
    ps.state = 'paused'
    for _ in range(3):
        feeder._on_idle_printing()
        feeder._on_idle_ready()

    assert len(_msgs(printer, PAUSE_MSG_FRAGMENT)) == 2, (
        "Pause #2 muss auch ohne Flush-Demand/Moonraker-Poll gemeldet "
        "werden — _main_tick ist der in-process garantierte Reset-Anker")


def test_main_tick_during_pause_keeps_latch():
    """Gegenprobe: _main_tick liest waehrend der Pause 'paused' und
    darf den Latch NICHT freigeben (sonst kehrt der 10s-Spam zurueck)."""
    printer, feeder = make_feeder()
    printer.objects['print_stats'] = FakePrintStats(state='paused')

    feeder._on_idle_printing()
    feeder._on_idle_ready()
    for t in (50.0, 52.0, 54.0):
        feeder.reactor.now = t
        feeder._main_tick(eventtime=t)
        feeder._on_idle_printing()
        feeder._on_idle_ready()

    assert len(_msgs(printer, PAUSE_MSG_FRAGMENT)) == 1, (
        "Tick waehrend der Pause darf den Dedupe-Latch nicht aufheben")


def test_pause_suspends_bangbang_on_every_cycle_despite_dedupe():
    """Regressions-Schutz: Der Dedupe-Latch darf NUR die Meldung
    unterdruecken, nicht die Suspend-Logik. _bang_bang_suspended muss
    nach jedem Pause-ready True sein (sonst koennte ein Feed durch-
    rutschen)."""
    printer, feeder = make_feeder()
    printer.objects['print_stats'] = FakePrintStats(state='paused')

    for _ in range(3):
        _simulate_pause_cycle(feeder)
        assert feeder._bang_bang_suspended is True, (
            "Suspend-Flag muss nach jedem Pause-ready gesetzt sein")


def test_print_end_message_emitted_once_across_flaps():
    """Should-fix (PR-#49-Review): Nach Druckende (state='complete')
    passiert der Anchor-Flap den nur-'standby'-Guard in
    _on_idle_printing -> "Print ended"-Meldung spammte im selben
    10s-Muster. Gleicher Latch, gleches Einmal-Verhalten."""
    printer, feeder = make_feeder()
    printer.objects['print_stats'] = FakePrintStats(state='complete')

    for _ in range(5):
        feeder._on_idle_printing()  # 'complete' != 'standby' -> kein Fruehausstieg
        feeder._on_idle_ready()
        _flush_tick(feeder)

    msgs = _msgs(printer, END_MSG_FRAGMENT)
    assert len(msgs) == 1, (
        "Print-ended-Meldung soll genau einmal erscheinen, "
        "war aber %d mal da: %s" % (len(msgs), msgs))


def test_print_end_message_reannounced_after_next_print():
    """Latch-Reset fuer die Print-ended-Meldung: ein neuer Druck
    (ps_state='printing' am Flush-/Poll-Tick) gibt den Latch frei,
    das Ende des naechsten Drucks wird wieder gemeldet."""
    printer, feeder = make_feeder()
    ps = FakePrintStats(state='complete')
    printer.objects['print_stats'] = ps

    feeder._print_running = True
    feeder._on_idle_ready()
    assert len(_msgs(printer, END_MSG_FRAGMENT)) == 1

    # Neuer Druck laeuft an
    ps.state = 'printing'
    feeder._on_idle_printing()
    _flush_tick(feeder)

    # Druck #2 endet
    ps.state = 'complete'
    feeder._on_idle_ready()

    assert len(_msgs(printer, END_MSG_FRAGMENT)) == 2, (
        "Ende des naechsten Drucks muss wieder gemeldet werden")
