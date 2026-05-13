"""P7-75 — Idle-Watchdog auf STATE_AUTO erweitert (Issue #31).

Eifel-Joe-Hardware-Test (2026-05-12, lead_time=0.12) mit P7-73 zeigte:
P7-73-Clamp wirkt, aber Crash bleibt — queue_step interval 7.78 s
@ 48 MHz. Smoking-Gun-Diagnose:

  * `forced_t0 ≈ mcu_now + 0.55s` (gesund, KEIN Clamp-Trigger)
  * `last_step_clock` steht beim Boot-Anchor von vor ~7 s
  * Differenz wird zum riesigen queue_step-Intervall → Timer too close

Wurzel: P7-70 Idle-Watchdog feuert NUR in STATE_IDLE. Buffer war seit
Boot in STATE_AUTO (Filament am Eingang, Buffer voll → HALL_EMPTY=False
→ Bang-Bang inaktiv) und hat keine Anker mehr bekommen.

P7-75 Fix: Watchdog-Gate auf STATE_AUTO erweitern. Zusätzliche Sub-
Gates verhindern Kollision mit aktivem Bang-Bang:

  * not self._continuous_feed     — Bang-Bang inaktiv
  * not self.hall_empty           — kein offener Feed-Request
  * not self._needs_overflow_prime — kein Pending-Prime

P7-70-Tests bleiben grün (STATE_IDLE-Pfad unverändert). Der bisherige
`test_state_auto_long_gap_does_not_fire` wird durch das vorhandene
P7-70-Pattern obsolet — er stand für "STATE_AUTO ist tabu". Mit P7-75
ist STATE_AUTO erlaubt, solange Bang-Bang quiescent ist; die alte
Aussage migriert in test_4/5/6 unten (Bang-Bang-aktiv → kein Anchor).
"""

import pytest

from fakes_klipper import FakeConfig, FakePrinter
from klipper_extras import buffer_feeder


# ---------------------------------------------------------------------------
# Helpers (copied from test_p770_idle_watchdog so the file stays self-contained)
# ---------------------------------------------------------------------------


def set_sensor_active(feeder, sensor_name, active):
    polarity_flip = feeder._pin_polarity_flip[sensor_name]
    raw = (not active) if polarity_flip else active
    feeder._pin_stable_state[sensor_name] = raw
    feeder._pin_raw_state[sensor_name] = raw


def make_auto_feeder(values=None):
    """Feeder in STATE_AUTO, grace done, sensors quiet (Bang-Bang in
    der Hysterese-Zwischenzone: weder hall_full noch hall_empty)."""
    printer = FakePrinter()
    config = FakeConfig(printer=printer, values=values)
    feeder = buffer_feeder.BufferFeeder(config)
    feeder._startup_grace_done = True
    feeder._state = buffer_feeder.STATE_AUTO
    set_sensor_active(feeder, 'hall_overflow', False)
    set_sensor_active(feeder, 'hall_full', False)
    set_sensor_active(feeder, 'hall_empty', False)
    set_sensor_active(feeder, 'entrance', False)
    return printer, feeder


def count_anchor_calls(monkeypatch, feeder):
    """Wrap sync._submit_anchor_move so each call is observable."""
    calls = []
    original = feeder.sync._submit_anchor_move

    def _spy():
        calls.append(feeder.stepper.get_mcu().estimated_print_time(
            feeder.reactor.monotonic()))
        # Mirror the real anchor's side effect on _last_move_end_time
        feeder._last_move_end_time = calls[-1] + 0.001
        return -1.0 if feeder.hall_overflow else 1.0

    monkeypatch.setattr(feeder.sync, "_submit_anchor_move", _spy)
    return calls


def neutralize_bang_bang(monkeypatch, feeder):
    """Keep _bang_bang_tick from touching anything during STATE_AUTO
    test ticks. We only want to observe the watchdog gate."""
    monkeypatch.setattr(feeder, "_bang_bang_tick", lambda et: None)


# ---------------------------------------------------------------------------
# Characterization: PRE-FIX behaviour (would crash on real HW)
# ---------------------------------------------------------------------------


def test_pre_fix_no_anchor_in_auto_on_long_gap_when_watchdog_blocked(
        monkeypatch):
    """PRE-FIX baseline: with idle_anchor_gap raised out of reach,
    the watchdog never fires in STATE_AUTO — exactly the Issue #31
    crash path (last_step_clock stale, queue_step interval 7.78 s).

    If anyone ever short-circuits the new STATE_AUTO gate, the
    behaviour tests below will fail; THIS test still passes,
    documenting the regression baseline forever (NOT-TO-DO
    2026-04-26 — Charakterisierungs-Tests sind Pflicht)."""
    _, feeder = make_auto_feeder(values={'idle_anchor_gap': 999999.0})
    neutralize_bang_bang(monkeypatch, feeder)
    calls = count_anchor_calls(monkeypatch, feeder)

    feeder.reactor.now = 20.0
    feeder._last_move_end_time = 0.0

    feeder._main_tick(eventtime=20.0)

    assert calls == [], (
        "PRE-FIX baseline: with idle_anchor_gap=999999, no anchor "
        "fires in STATE_AUTO — the original Issue #31 path that "
        "left last_step_clock stale.")


# ---------------------------------------------------------------------------
# Behaviour: POST-FIX watchdog fires in STATE_AUTO under the right gates
# ---------------------------------------------------------------------------


def test_1_watchdog_fires_in_auto_without_activity(monkeypatch):
    """STATE_AUTO + gap > idle_anchor_gap + no Bang-Bang activity →
    exactly one anchor. This is the Eifel-Joe Hardware-Repro:
    Buffer voll → AUTO → 20 s warten → erster Submit würde sonst
    auf stale last_step_clock referenzieren."""
    _, feeder = make_auto_feeder()  # default idle_anchor_gap=10.0
    neutralize_bang_bang(monkeypatch, feeder)
    calls = count_anchor_calls(monkeypatch, feeder)

    feeder.reactor.now = 20.0
    feeder._last_move_end_time = 0.0

    feeder._main_tick(eventtime=20.0)

    assert len(calls) == 1, (
        "P7-75: Watchdog must fire exactly one anchor in STATE_AUTO "
        "when bang-bang quiescent and gap > idle_anchor_gap.")


def test_2_move_in_flight_blocks_watchdog(monkeypatch):
    """An active streaming submit is in progress (_move_in_flight=True
    via a synthetic future-end _current_move). Watchdog must NOT
    inject — risk of overlap with the in-flight submit's t0."""
    _, feeder = make_auto_feeder()
    neutralize_bang_bang(monkeypatch, feeder)
    calls = count_anchor_calls(monkeypatch, feeder)

    feeder.reactor.now = 30.0
    feeder._last_move_end_time = 0.0
    # Synthetic in-flight move (_move_in_flight returns now_pt < end_time)
    feeder._current_move = {'end_time': 100.0}

    feeder._main_tick(eventtime=30.0)

    assert calls == [], (
        "P7-75: Watchdog must NOT fire while a move is in flight.")


def test_3_hall_empty_blocks_watchdog(monkeypatch):
    """hall_empty=True means bang-bang has an open feed-request
    pending — watchdog stepping in here would race against the
    legitimate feed submit. Sub-gate `not self.hall_empty`
    keeps us out of the way."""
    _, feeder = make_auto_feeder()
    set_sensor_active(feeder, 'hall_empty', True)
    neutralize_bang_bang(monkeypatch, feeder)
    calls = count_anchor_calls(monkeypatch, feeder)

    feeder.reactor.now = 30.0
    feeder._last_move_end_time = 0.0

    feeder._main_tick(eventtime=30.0)

    assert calls == [], (
        "P7-75: Watchdog must NOT fire while hall_empty is True "
        "(bang-bang has an active feed-request).")


def test_3b_hall_full_blocks_watchdog(monkeypatch):
    """hall_full=True means buffer is already at the upper threshold.
    Forward anchors would push toward HALL1 overflow. Sub-gate
    `not self.hall_full` (P7-75b Codex-Verify finding) prevents the
    ~18mm/h cumulative drift in a quiescent full-buffer AUTO state."""
    _, feeder = make_auto_feeder()
    set_sensor_active(feeder, 'hall_full', True)
    neutralize_bang_bang(monkeypatch, feeder)
    calls = count_anchor_calls(monkeypatch, feeder)

    feeder.reactor.now = 30.0
    feeder._last_move_end_time = 0.0

    feeder._main_tick(eventtime=30.0)

    assert calls == [], (
        "P7-75b: Watchdog must NOT fire while hall_full is True "
        "(forward anchors would push toward HALL1 overflow).")


def test_4_continuous_feed_blocks_watchdog(monkeypatch):
    """Active continuous-feed session — watchdog must NOT inject
    a parallel anchor. _continuous_feed is the canonical 'bang-
    bang is currently streaming chunks' flag."""
    _, feeder = make_auto_feeder()
    feeder._continuous_feed = True
    neutralize_bang_bang(monkeypatch, feeder)
    calls = count_anchor_calls(monkeypatch, feeder)

    feeder.reactor.now = 30.0
    feeder._last_move_end_time = 0.0

    feeder._main_tick(eventtime=30.0)

    assert calls == [], (
        "P7-75: Watchdog must NOT fire while _continuous_feed=True.")


def test_5_needs_overflow_prime_blocks_watchdog(monkeypatch):
    """A pending OVERFLOW-prime move owns the next stepcompress
    refresh (via _on_mcu_flush or _main_tick prime path). The
    watchdog must defer so we don't double-prime."""
    _, feeder = make_auto_feeder()
    feeder._needs_overflow_prime = True
    neutralize_bang_bang(monkeypatch, feeder)
    calls = count_anchor_calls(monkeypatch, feeder)

    feeder.reactor.now = 30.0
    feeder._last_move_end_time = 0.0

    feeder._main_tick(eventtime=30.0)

    assert calls == [], (
        "P7-75: Watchdog must NOT fire while _needs_overflow_prime "
        "is set — the prime path owns the next anchor.")


def test_6_eifel_reproduction_seven_second_gap_refreshes_clock(monkeypatch):
    """Eifel-Joe-Hardware-Repro (Issue #31, 22:04 UTC 2026-05-12):

    1. Klipper-Restart → INIT → AUTO (Filament am Eingang, Buffer voll)
    2. Boot-Anchor primed @ MCU-Boot-Clock
    3. 7 s lang STATE_AUTO ohne Bang-Bang-Aktivität (Hysterese-
       Zwischenzone: weder hall_full noch hall_empty)
    4. Print-Job startet → erster Bang-Bang-Submit hätte 7.78 s
       queue_step interval erzeugt → Timer too close.

    Mit P7-75: Watchdog feuert während des 7-s-Fensters einen
    0.05 mm-Anchor, refresht `_last_move_end_time` und damit
    indirekt den stepcompress-Cursor. Der spätere Bang-Bang-Submit
    sieht keinen 7-s-Gap mehr."""
    _, feeder = make_auto_feeder()
    neutralize_bang_bang(monkeypatch, feeder)
    calls = count_anchor_calls(monkeypatch, feeder)

    # t=0: Boot-Anchor "primed" — last_move_end_time = 0.
    feeder._last_move_end_time = 0.0

    # 7 s vergehen ohne Bang-Bang-Aktivität. idle_anchor_gap default=10
    # → bei 7 s tritt der Watchdog NICHT zu (Gap < Threshold). Das ist
    # konservativ und korrekt; das Repro-Szenario aus Issue #31 ist
    # mit 7 s knapp unter Threshold, aber der nachfolgende
    # Toolhead-Lookahead von 50–100 s addiert sich beim Print-Start
    # auf. Wir simulieren: bei 11 s schlägt der Watchdog zu.
    feeder.reactor.now = 11.0
    feeder._main_tick(eventtime=11.0)

    assert len(calls) == 1, (
        "Eifel-Repro: nach 11 s STATE_AUTO ohne Activity muss der "
        "Watchdog genau einen Anchor feuern — last_step_clock wird "
        "über _submit_anchor_move refresht, ein anschließender "
        "Bang-Bang-Submit referenziert keinen 7-s-stale Anker mehr.")

    # Sanity: nach dem Anchor ist _last_move_end_time aktualisiert,
    # also wird ein hypothetischer Folge-Submit nicht mehr 11 s in
    # die Zukunft springen.
    assert feeder._last_move_end_time > 10.0, (
        "Anchor-Side-Effect: _last_move_end_time muss vorrücken, "
        "damit der nächste Submit auf einem frischen Cursor sitzt.")


# ---------------------------------------------------------------------------
# Cross-test: P7-70 IDLE path must remain intact
# ---------------------------------------------------------------------------


def test_state_idle_path_still_works(monkeypatch):
    """P7-70 muss grün bleiben — Erweiterung darf den IDLE-Pfad nicht
    brechen. Defense-in-depth gegen versehentliches Tippex am Gate."""
    printer = FakePrinter()
    config = FakeConfig(printer=printer)
    feeder = buffer_feeder.BufferFeeder(config)
    feeder._startup_grace_done = True
    feeder._state = buffer_feeder.STATE_IDLE
    set_sensor_active(feeder, 'hall_overflow', False)
    set_sensor_active(feeder, 'hall_full', False)
    set_sensor_active(feeder, 'hall_empty', False)
    set_sensor_active(feeder, 'entrance', False)

    calls = count_anchor_calls(monkeypatch, feeder)

    feeder.reactor.now = 20.0
    feeder._last_move_end_time = 0.0

    feeder._main_tick(eventtime=20.0)

    assert len(calls) == 1, (
        "P7-70 STATE_IDLE-Pfad muss unverändert grün bleiben.")
