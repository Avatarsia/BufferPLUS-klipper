"""Tests for INITIAL_GRIP-Recovery Force-Reprime (Issue #29 Follow-up).

Symmetrischer Patch zu PR #47 (STATE_LOADING_PULL-Pfad). Die
INITIAL_GRIP-grip-follow-Phase aktiviert denselben Pending-Stream-
Setup wie LOAD_PHASE_1 ueber ``_grip_follow_active=True`` +
``_submit_move(grip_follow_distance, grip_follow_speed)`` in
_main_tick. Bei HALL1-OVERFLOW mid-flight transitioniert
_enter_overflow nach STATE_OVERFLOW; HALL1 clear triggert
resume_after_overflow STATE_INITIAL_GRIP-Branch.

Ohne den Force-Reprime laesst der Resume-Submit mit primed=True den
_reprime_stepcompress_if_needed-Skip-Pfad zu (kein flush_step_-
generation, kein set_position(0)) -> Stepcompress-Cursor altert ->
selbe Race-Klasse wie c=19 i=0 / c=1 i=-2807496 aus #29.

Fix-Mechanik identisch zu PR #47:
- _stepcompress_primed = False  vor Submit
- _needs_overflow_prime = False vor Submit
- direkter _submit_move(forced_t0=None, streaming=False default)
"""

from klipper_extras import buffer_feeder


def _arm_initial_grip_overflow(feeder, resume_mm=200.0, resume_dir=1,
                               resume_spd=30.0):
    """Setze den Recovery-Zustand wie nach HALL1-OVERFLOW mid grip-
    follow. interrupted_state=INITIAL_GRIP + interrupted_follow=True
    sind die beiden Bedingungen fuer den geschuetzten Branch."""
    feeder._startup_grace_done = True
    feeder._set_state(buffer_feeder.STATE_OVERFLOW)
    feeder._overflow_interrupted_state = buffer_feeder.STATE_INITIAL_GRIP
    feeder._overflow_interrupted_follow = True
    feeder._overflow_resume_mm = resume_mm
    feeder._overflow_resume_dir = resume_dir
    feeder._overflow_resume_spd = resume_spd
    feeder._stepper_synced_to = None


def test_initial_grip_resume_force_reprime_via_primed_false(
        feeder_with_connect, monkeypatch):
    """resume_after_overflow STATE_INITIAL_GRIP-Branch muss
    _stepcompress_primed=False vor dem Submit setzen damit
    _reprime_stepcompress_if_needed im forced_t0=None-Pfad
    flush_step_generation + set_position(0) feuert."""
    feeder = feeder_with_connect
    _arm_initial_grip_overflow(feeder, resume_mm=180.0, resume_spd=25.0)
    # Pre-Condition wie Hardware-Realitaet nach OVERFLOW-Cycling
    feeder._stepcompress_primed = True
    feeder._needs_overflow_prime = True

    captured = {}

    def fake_submit_move(signed_distance, speed, forced_t0=None,
                         streaming=False, submit_chunk_cap=None):
        captured['signed_distance'] = signed_distance
        captured['speed'] = speed
        captured['forced_t0'] = forced_t0
        captured['streaming'] = streaming
        captured['primed_at_submit'] = feeder._stepcompress_primed
        captured['needs_prime_at_submit'] = feeder._needs_overflow_prime
        captured['grip_follow_at_submit'] = feeder._grip_follow_active
        captured['state_at_submit'] = feeder._state

    monkeypatch.setattr(feeder, "_submit_move", fake_submit_move)
    monkeypatch.setattr(feeder, "_enable_stepper", lambda: None)

    feeder.fault.resume_after_overflow()

    assert 'signed_distance' in captured, "_submit_move not called"
    assert captured['signed_distance'] == 180.0
    assert captured['speed'] == 25.0
    # forced_t0=None damit Reprime-Pfad mit flush_step_generation feuert
    assert captured['forced_t0'] is None
    # primed muss VOR dem Submit False sein, sonst skipt reprime
    assert captured['primed_at_submit'] is False, (
        "_stepcompress_primed MUSS False sein vor Submit. primed=True "
        "wuerde Reprime ueberspringen und Crash-Race reproduzieren.")
    # needs_overflow_prime aufgeraeumt -- wir machen die Prime selbst
    assert captured['needs_prime_at_submit'] is False
    # _grip_follow_active muss True sein wenn Submit feuert; sonst
    # detected _main_tick Follow-Completion bei pending_remaining_mm=0
    # zu frueh und droppt vorzeitig auf IDLE
    assert captured['grip_follow_at_submit'] is True
    assert captured['state_at_submit'] == buffer_feeder.STATE_INITIAL_GRIP
    assert feeder._state == buffer_feeder.STATE_INITIAL_GRIP
    assert feeder._overflow_resume_mm == 0.0
    # interrupted_follow geclear't -- naechste OVERFLOW-Recovery startet
    # mit sauberem Flag
    assert feeder._overflow_interrupted_follow is False


def test_initial_grip_resume_clears_needs_overflow_prime(
        feeder_with_connect, monkeypatch):
    """_needs_overflow_prime muss geclear't werden -- sonst feuert ein
    nachfolgender _on_mcu_flush einen redundanten Prime-Nudge der den
    sauberen Cursor wieder durcheinander bringen koennte."""
    feeder = feeder_with_connect
    _arm_initial_grip_overflow(feeder)
    feeder._needs_overflow_prime = True

    monkeypatch.setattr(feeder, "_submit_move", lambda *a, **k: None)
    monkeypatch.setattr(feeder, "_enable_stepper", lambda: None)

    feeder.fault.resume_after_overflow()

    assert feeder._needs_overflow_prime is False


def test_initial_grip_resume_passes_signed_distance(
        feeder_with_connect, monkeypatch):
    """grip-follow ist konzeptuell forward (resume_dir=+1), aber der
    signed-distance-Pfad darf das Vorzeichen nicht clobberd. Regression-
    Guard gegen versehentliche abs()-Umstellung."""
    feeder = feeder_with_connect
    _arm_initial_grip_overflow(feeder, resume_mm=120.0, resume_dir=1,
                               resume_spd=40.0)

    captured = {}

    def fake_submit_move(signed_distance, speed, forced_t0=None,
                         streaming=False, submit_chunk_cap=None):
        captured['signed_distance'] = signed_distance
        captured['speed'] = speed

    monkeypatch.setattr(feeder, "_submit_move", fake_submit_move)
    monkeypatch.setattr(feeder, "_enable_stepper", lambda: None)

    feeder.fault.resume_after_overflow()

    assert captured['signed_distance'] == 120.0
    assert captured['speed'] == 40.0


def test_initial_grip_resume_without_resume_mm_falls_to_auto_load(
        feeder_with_connect, monkeypatch):
    """Wenn resume_mm=0 (Edge-Case: HALL1 feuerte exakt am Move-Ende)
    soll der Branch _maybe_auto_load() rufen statt einen 0-Distance-
    Submit zu queuen. Force-Reprime-Patch darf diesen Fall nicht
    versehentlich aktivieren."""
    feeder = feeder_with_connect
    _arm_initial_grip_overflow(feeder, resume_mm=0.0)
    feeder._stepcompress_primed = True
    feeder._needs_overflow_prime = True
    submit_calls = []
    auto_load_calls = []

    monkeypatch.setattr(feeder, "_submit_move",
                        lambda *a, **k: submit_calls.append(a))
    monkeypatch.setattr(feeder, "_enable_stepper", lambda: None)
    monkeypatch.setattr(feeder, "_maybe_auto_load",
                        lambda: auto_load_calls.append(True))

    feeder.fault.resume_after_overflow()

    assert submit_calls == [], "no _submit_move when resume_mm=0"
    assert auto_load_calls == [True]
    # Force-Reprime-Flags duerfen im no-resume-Pfad nicht geclear't
    # werden (kein Submit faehrt -- kein Bedarf fuer Cursor-Reset).
    assert feeder._stepcompress_primed is True
    assert feeder._needs_overflow_prime is True
    assert feeder._overflow_resume_mm == 0.0
    assert feeder._overflow_interrupted_follow is False
