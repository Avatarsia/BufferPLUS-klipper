"""Diagnose-Build (Regel #11a, NICHT Production) für Issue #50.

Hardware-Crash 2026-06-12 klippy.log Z.50622: ``stepcompress o=0 i=0
c=10 a=0: Invalid sequence`` auf dem Buffer-Stepper während eines
rapiden OVERFLOW↔LOAD_PHASE_1-Cyclings (HALL1 durch SFS-V2-Gegendruck).

Der queue_step-Dump (Z.51477-51498) zeigt eine valide Trapez-Queue —
der Crash trat beim HINZUFÜGEN der nächsten Step-Charge auf, deren
Clock bei/hinter dem Queue-Ende landet (Anchor-hinter-Queue,
Issue-#29-Klasse). Der laufende Code hat keine Submit-Diagnose-Events;
WARUM der Anchor unter dem Cycling hinter die Queue fällt, ist
unbewiesen.

Diese Events capturen am Submit den finalen ``t0`` gegen
``_current_move['end_time']`` (= die noch gequeueten Steps des von
halt_motion getrunkten Vorgänger-Chunks). t0 < cur_end ist der direkte
Beleg für "Submit landet hinter der Queue". min_interval=0.0 damit im
Sturm keine Events verloren gehen.

Nach bewiesener Wurzel: per git revert zurücknehmen.
"""

import logging

from klipper_extras import buffer_feeder


def _enable_diag(feeder):
    feeder.buffer_debug_events = True
    feeder._startup_grace_done = True


def test_submit_emits_diag_submit_with_anchor_inputs(feeder, monkeypatch,
                                                     caplog):
    """diag_submit am Submit-Eingang: kompletter Anchor-Input-State
    inkl. _current_move['end_time'] (Queue-Ende) und Reprime-Entscheidung."""
    _enable_diag(feeder)
    monkeypatch.setattr(feeder, "trapq_append", lambda *a, **k: None)
    monkeypatch.setattr(feeder, "_reprime_stepcompress_if_needed",
                        lambda forced_t0, gap: False)
    monkeypatch.setattr(feeder, "_enable_stepper", lambda: None)
    feeder._current_move = {'end_time': 1234.5}

    with caplog.at_level(logging.INFO, logger=""):
        feeder._submit_single_trapezoid(50.0, 50.0,
                                        forced_t0=None, streaming=False)

    msgs = [r.getMessage() for r in caplog.records]
    diag = [m for m in msgs if "diag_submit" in m]
    assert diag, "diag_submit-Event fehlt. Alle: %s" % msgs
    for field in ("state=", "dist=", "speed=", "forced_t0=", "streaming=",
                  "mcu_now=", "lme=", "en=", "gap=", "was_primed=",
                  "need_reprime=", "cur_end="):
        assert field in diag[0], "Feld '%s' fehlt: %s" % (field, diag[0])


def test_append_emits_diag_append_with_t0_vs_queue(feeder, monkeypatch,
                                                   caplog):
    """diag_append direkt vor trapq_append: finaler t0 gegen lme und
    cur_end (Queue-Ende) — die kritischen Deltas, die "hinter der Queue"
    beweisen."""
    _enable_diag(feeder)
    monkeypatch.setattr(feeder, "trapq_append", lambda *a, **k: None)
    feeder._current_move = {'end_time': 999.0}
    feeder._last_move_end_time = 10.0

    with caplog.at_level(logging.INFO, logger=""):
        feeder._append_trapezoid_and_record(42.0, 50.0, 50.0)

    msgs = [r.getMessage() for r in caplog.records]
    diag = [m for m in msgs if "diag_append" in m]
    assert diag, "diag_append-Event fehlt. Alle: %s" % msgs
    for field in ("t0=", "lme_in=", "cur_end=", "mcu_now=",
                  "t0-lme=", "t0-curend="):
        assert field in diag[0], "Feld '%s' fehlt: %s" % (field, diag[0])


def test_diag_append_reports_t0_behind_queue_delta(feeder, monkeypatch,
                                                   caplog):
    """Kernbeweis-Fähigkeit: wenn t0 < cur_end, muss t0-curend negativ
    sein (Submit landet hinter der noch gequeueten Bewegung)."""
    _enable_diag(feeder)
    monkeypatch.setattr(feeder, "trapq_append", lambda *a, **k: None)
    feeder._current_move = {'end_time': 100.0}
    feeder._last_move_end_time = 5.0

    with caplog.at_level(logging.INFO, logger=""):
        feeder._append_trapezoid_and_record(30.0, 50.0, 50.0)  # t0=30 < 100

    diag = [r.getMessage() for r in caplog.records if "diag_append" in r.getMessage()]
    assert diag, "diag_append fehlt"
    # t0-curend = 30 - 100 = -70 -> negatives Delta muss sichtbar sein
    assert "t0-curend=-70.0" in diag[0] or "t0-curend=-70" in diag[0], (
        "negatives t0-curend-Delta nicht korrekt geloggt: %s" % diag[0])


def test_plan_anchor_emits_diag_anchor_with_th_time(feeder, caplog):
    """diag_anchor im First-Chunk/Recovery-Pfad: th_time
    (toolhead.get_last_move_time() = die Anchor-Quelle) gegen den
    berechneten t0. Damit sehen wir, ob der Recovery-Anchor unter
    Tight-Cycling hinter die Queue lappt (th_time lagged).

    Hardware-Befund 2026-06-12 (gesunder Lauf): Recovery-Submits hatten
    t0-curend=+0.14; der Crash-Lauf cyclete tighter. th_time ist der
    fehlende Wert zwischen Anchor-Quelle und Queue-Ende."""
    _enable_diag(feeder)
    feeder.printer.objects['toolhead'].last_move_time = 100.0
    feeder._last_move_end_time = 0.0
    feeder._last_enable_schedule_time = 0.0

    with caplog.at_level(logging.INFO, logger=""):
        # First-Chunk-Pfad: forced_t0=None, lme(0) <= mcu_now+lead,
        # streaming=False -> _plan_t0_anchor liest th_time.
        t0 = feeder._compute_t0_anchor(
            None, 100.0, was_primed=False, need_reprime=True,
            streaming=False)

    assert t0 is not None
    diag = [r.getMessage() for r in caplog.records if "diag_anchor" in r.getMessage()]
    assert diag, "diag_anchor-Event fehlt"
    for field in ("th_time=", "lead=", "lme=", "en=", "mcu_now=", "t0=",
                  "t0-th="):
        assert field in diag[0], "Feld '%s' fehlt: %s" % (field, diag[0])
    assert "th_time=100.0" in diag[0], (
        "th_time falsch geloggt: %s" % diag[0])


def test_diag_events_off_when_debug_disabled(feeder, monkeypatch, caplog):
    """Ohne buffer_debug_events: keine diag-Events (Production-no-op)."""
    feeder.buffer_debug_events = False
    feeder._startup_grace_done = True
    monkeypatch.setattr(feeder, "trapq_append", lambda *a, **k: None)

    with caplog.at_level(logging.INFO, logger=""):
        feeder._append_trapezoid_and_record(42.0, 50.0, 50.0)

    msgs = [r.getMessage() for r in caplog.records]
    assert not [m for m in msgs if "diag_append" in m], (
        "diag-Events dürfen bei buffer_debug_events=False nicht feuern")
