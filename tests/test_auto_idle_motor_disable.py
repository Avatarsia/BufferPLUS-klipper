"""AUTO-Idle-Motor-Disable — Stepper stromlos schalten zwischen Drucken.

Hardware-Beobachtung 2026-05-21: Nach einem Druck bleibt der Buffer
bei eingelegtem Filament in STATE_AUTO (nicht IDLE). Der Watchdog-
Disable nach jedem Anchor lief bisher NUR in STATE_IDLE
(buffer_feeder.py:~1499), daher blieb der Feeder-Stepper in AUTO
stundenlang bestromt — hoerbares Spulenfiepen, unnoetiger Strom/
Hitze.

Fix: Der Disable-nach-Anchor laeuft jetzt auch in STATE_AUTO, ABER
nur wenn wirklich kein Druck aktiv ist. Diskriminante ist
`_p778_override`:
- _p778_override=False (im Block) -> wirklich kein Druck -> disable
- _p778_override=True -> echter Druck in HALL2-Hysterese-Totzone
  (Z.1428 flippt _print_active nur lokal) -> NICHT disablen, sonst
  Race mit zurueckkehrendem bang-bang.

Wake-Safety (Issue #29): _disable_stepper setzt _stepcompress_primed
=False; der naechste Submit (Anchor in 10s oder erster _on_mcu_flush
bei Druckstart) reprimt via set_position(0).
"""

from fakes_klipper import FakeConfig, FakePrinter, FakePrintStats
from klipper_extras import buffer_feeder


def set_sensor_active(feeder, sensor_name, active):
    polarity_flip = feeder._pin_polarity_flip[sensor_name]
    raw = (not active) if polarity_flip else active
    feeder._pin_stable_state[sensor_name] = raw
    feeder._pin_raw_state[sensor_name] = raw


def make_auto_feeder(values=None, print_state='standby'):
    """Feeder in STATE_AUTO, Sensoren quiescent, Filament eingelegt."""
    base = {"use_flush_callback_bang_bang": True}
    if values:
        base.update(values)
    printer = FakePrinter()
    printer.objects['print_stats'] = FakePrintStats(state=print_state)
    config = FakeConfig(printer=printer, values=base)
    feeder = buffer_feeder.BufferFeeder(config)
    printer.fire_event('klippy:connect')
    feeder._startup_grace_done = True
    feeder._state = buffer_feeder.STATE_AUTO
    set_sensor_active(feeder, 'hall_overflow', False)
    set_sensor_active(feeder, 'hall_full', False)
    set_sensor_active(feeder, 'hall_empty', False)
    set_sensor_active(feeder, 'entrance', True)
    return printer, feeder


def make_idle_feeder(values=None):
    base = {"use_flush_callback_bang_bang": True}
    if values:
        base.update(values)
    printer = FakePrinter()
    config = FakeConfig(printer=printer, values=base)
    feeder = buffer_feeder.BufferFeeder(config)
    feeder._startup_grace_done = True
    feeder._state = buffer_feeder.STATE_IDLE
    set_sensor_active(feeder, 'hall_overflow', False)
    set_sensor_active(feeder, 'hall_full', False)
    set_sensor_active(feeder, 'hall_empty', False)
    set_sensor_active(feeder, 'entrance', False)
    return printer, feeder


def spy_anchor(monkeypatch, feeder):
    """Neutralisiere den echten Anchor, spiegle nur den lme-Side-Effect."""
    calls = []

    def _spy(**kwargs):
        mcu_now = feeder.stepper.get_mcu().estimated_print_time(
            feeder.reactor.monotonic())
        calls.append(mcu_now)
        feeder._last_move_end_time = mcu_now + 0.001
        return 1.0

    monkeypatch.setattr(feeder.sync, "_submit_anchor_move", _spy)
    return calls


def spy_disable(monkeypatch, feeder):
    """Zaehle _schedule_stepper_disable-Aufrufe."""
    calls = []
    monkeypatch.setattr(feeder, "_schedule_stepper_disable",
                        lambda: calls.append(True))
    return calls


# ---------------------------------------------------------------------------
# Kern-Verhalten: AUTO ohne Druck schaltet ab
# ---------------------------------------------------------------------------


def test_auto_no_print_disables_after_anchor(monkeypatch):
    """STATE_AUTO + print=standby + gap > idle_anchor_gap:
    Anchor feuert UND der Stepper-Disable wird scheduled.

    Nur bei idle_motor_disable=True (Opt-in). Default ist False
    (Motor bleibt in AUTO an, StealthChop haelt ihn leise — kein
    Enable-Snap)."""
    _, feeder = make_auto_feeder(print_state='standby',
                                 values={'idle_motor_disable': True})
    monkeypatch.setattr(feeder, "_bang_bang_tick", lambda et: None)
    anchors = spy_anchor(monkeypatch, feeder)
    disables = spy_disable(monkeypatch, feeder)

    feeder.reactor.now = 20.0
    feeder._last_move_end_time = 0.0
    feeder._last_idle_anchor_time = 0.0
    feeder._last_mcu_flush_time = 0.0  # boot -> kein Override

    feeder._main_tick(eventtime=20.0)

    assert len(anchors) == 1, "Anchor muss in AUTO/standby feuern"
    assert len(disables) == 1, (
        "STATE_AUTO ohne Druck mit idle_motor_disable=True MUSS den "
        "Stepper-Disable schedulen. Got %d." % len(disables))


def test_auto_default_flag_off_keeps_motor_enabled(monkeypatch):
    """Default idle_motor_disable=False: in STATE_AUTO feuert der Anchor
    (Cursor-Pflege), aber der Stepper wird NICHT disabled — der Motor
    bleibt an, StealthChop haelt ihn leise, kein Enable-Snap-Tick.
    Das ist das Default-Verhalten (Weg 1)."""
    _, feeder = make_auto_feeder(print_state='standby')  # kein Flag -> False
    assert feeder.idle_motor_disable is False
    monkeypatch.setattr(feeder, "_bang_bang_tick", lambda et: None)
    anchors = spy_anchor(monkeypatch, feeder)
    disables = spy_disable(monkeypatch, feeder)

    feeder.reactor.now = 20.0
    feeder._last_move_end_time = 0.0
    feeder._last_idle_anchor_time = 0.0
    feeder._last_mcu_flush_time = 0.0

    feeder._main_tick(eventtime=20.0)

    assert len(anchors) == 1, "Anchor feuert weiterhin (Cursor frisch)"
    assert disables == [], (
        "Default (idle_motor_disable=False) darf in AUTO NICHT disablen "
        "-- Motor bleibt an. Got %d." % len(disables))


def test_auto_print_override_does_not_disable(monkeypatch):
    """STATE_AUTO + print=printing + stale flush (_p778_override aktiv):
    Anchor feuert zur Cursor-Pflege, aber der Stepper-Disable wird
    NICHT scheduled — echter Druck laeuft, ein Disable wuerde mit dem
    zurueckkehrenden bang-bang racen. Flag True, damit der Override
    (nicht der Default) der einzige Blocker ist."""
    _, feeder = make_auto_feeder(print_state='printing',
                                 values={'idle_motor_disable': True})
    monkeypatch.setattr(feeder, "_bang_bang_tick", lambda et: None)
    anchors = spy_anchor(monkeypatch, feeder)
    disables = spy_disable(monkeypatch, feeder)

    mcu_now = 30.0
    feeder.reactor.now = mcu_now
    # Stille = 15s > idle_anchor_gap (10s) -> Override feuert,
    # _print_active wird lokal auf False geflippt (Z.1428).
    feeder._last_mcu_flush_time = mcu_now - 15.0
    feeder._last_move_end_time = 0.0
    feeder._last_idle_anchor_time = 0.0

    feeder._main_tick(eventtime=mcu_now)

    assert len(anchors) == 1, (
        "Override muss den Anchor zur Cursor-Pflege feuern lassen.")
    assert len(disables) == 0, (
        "Bei _p778_override (echter Druck) darf KEIN Stepper-Disable "
        "scheduled werden. Got %d." % len(disables))


def test_idle_still_disables_after_anchor(monkeypatch):
    """Regression-Guard: STATE_IDLE schaltet weiterhin ab (Verhalten
    vor dem Patch unveraendert)."""
    _, feeder = make_idle_feeder()
    anchors = spy_anchor(monkeypatch, feeder)
    disables = spy_disable(monkeypatch, feeder)

    feeder.reactor.now = 20.0
    feeder._last_move_end_time = 0.0
    feeder._last_idle_anchor_time = 0.0

    feeder._main_tick(eventtime=20.0)

    assert len(anchors) == 1
    assert len(disables) == 1, (
        "STATE_IDLE muss weiterhin disablen (Regression-Guard). "
        "Got %d." % len(disables))


def test_auto_no_disable_without_anchor(monkeypatch):
    """Kein Anchor (gap < idle_anchor_gap) -> auch kein Disable. Der
    Disable haengt am Anchor-Branch, nicht an jedem Tick — sonst
    wuerde der Motor bei jedem 50Hz-Tick neu disable-scheduled."""
    _, feeder = make_auto_feeder(print_state='standby')
    monkeypatch.setattr(feeder, "_bang_bang_tick", lambda et: None)
    anchors = spy_anchor(monkeypatch, feeder)
    disables = spy_disable(monkeypatch, feeder)

    feeder.reactor.now = 5.0  # gap 5s < idle_anchor_gap 10s
    feeder._last_move_end_time = 0.0
    feeder._last_idle_anchor_time = 0.0
    feeder._last_mcu_flush_time = 0.0

    feeder._main_tick(eventtime=5.0)

    assert anchors == [], "kein Anchor unter idle_anchor_gap"
    assert disables == [], (
        "ohne Anchor darf kein Disable scheduled werden. Got %d."
        % len(disables))


def test_auto_no_disable_when_print_stats_missing(monkeypatch):
    """STATE_AUTO ohne print_stats-Objekt: Druckstatus unbekannt ->
    KEIN Disable. Konservativ, damit ein Disable nicht mid-print
    durchrutscht wenn print_stats (transient) nicht verfuegbar ist.
    Der Motor bleibt dann bestromt wie vor dem Patch."""
    printer, feeder = make_auto_feeder(
        print_state='standby', values={'idle_motor_disable': True})
    # print_stats entfernen -> lookup_object('print_stats', None) wirft
    # KeyError -> except-Pfad -> _print_state_known bleibt False.
    del printer.objects['print_stats']
    monkeypatch.setattr(feeder, "_bang_bang_tick", lambda et: None)
    anchors = spy_anchor(monkeypatch, feeder)
    disables = spy_disable(monkeypatch, feeder)

    feeder.reactor.now = 20.0
    feeder._last_move_end_time = 0.0
    feeder._last_idle_anchor_time = 0.0
    feeder._last_mcu_flush_time = 0.0

    feeder._main_tick(eventtime=20.0)

    assert len(anchors) == 1, (
        "Anchor feuert weiterhin (Cursor-Pflege unabhaengig vom "
        "Druckstatus).")
    assert disables == [], (
        "Ohne lesbares print_stats (Status unbekannt) darf in AUTO "
        "KEIN Disable scheduled werden. Got %d." % len(disables))


def test_auto_no_disable_when_print_stats_raises(monkeypatch):
    """STATE_AUTO + print_stats.get_status() wirft Exception waehrend
    echtem Druck: der except-Pfad setzt _print_active=False, aber
    _print_state_known bleibt False -> KEIN Disable. Das ist der von
    Codex-Verify gefundene Mid-Print-Disable-Schutz."""
    printer, feeder = make_auto_feeder(
        print_state='printing', values={'idle_motor_disable': True})

    class _RaisingPrintStats:
        def get_status(self, eventtime):
            raise RuntimeError("print_stats transient failure")

    printer.objects['print_stats'] = _RaisingPrintStats()
    monkeypatch.setattr(feeder, "_bang_bang_tick", lambda et: None)
    anchors = spy_anchor(monkeypatch, feeder)
    disables = spy_disable(monkeypatch, feeder)

    feeder.reactor.now = 20.0
    feeder._last_move_end_time = 0.0
    feeder._last_idle_anchor_time = 0.0
    feeder._last_mcu_flush_time = 0.0

    feeder._main_tick(eventtime=20.0)

    assert disables == [], (
        "Bei print_stats-Exception (Status unbekannt) darf in AUTO "
        "KEIN Disable scheduled werden — Mid-Print-Disable-Schutz. "
        "Got %d." % len(disables))


def test_disable_stepper_unprimes_for_wake_safety(monkeypatch):
    """Wake-Safety-Contract (Issue #29): _disable_stepper MUSS
    _stepcompress_primed=False setzen, damit der naechste Submit nach
    dem AUTO-Disable garantiert ueber den Reprime-Pfad (set_position(0))
    laeuft. Ohne das altert der Cursor und der Wake-Submit crasht."""
    _, feeder = make_auto_feeder(print_state='standby')
    feeder._stepcompress_primed = True

    feeder._disable_stepper()

    assert feeder._stepcompress_primed is False, (
        "_disable_stepper MUSS _stepcompress_primed=False setzen — das "
        "ist die Wake-Safety-Garantie auf die der AUTO-Disable baut.")
