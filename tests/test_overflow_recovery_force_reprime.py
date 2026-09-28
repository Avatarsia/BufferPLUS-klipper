"""Tests for OVERFLOW-Recovery Force-Reprime (Issue #29).

Hardware-Crash 2026-05-19 klippy.log Z.7023:
    b'stepcompress o=0 i=0 c=19 a=0: Invalid sequence'
beim 3. von 3 rapiden HALL1-OVERFLOW-Zyklen in LOAD_PHASE_1.

Wurzel: Pending-Stream-Setup mit primed=True liess
_reprime_stepcompress_if_needed flush_step_generation +
set_position(0) ueberspringen. Klipper's step_gen_time-Lookahead-
Window pushpt last_step_clock auf ~mcu_now+250ms; t0 baut aus
geclamptem lme (~mcu_now+lead) und landet drin -> Zero-Interval-
Steps -> Crash.

Fix: _stepcompress_primed=False vor Submit forciert vollen Reprime-
Pfad. _submit_move(forced_t0=None) laesst flush_step_generation +
set_position(0) feuern. Stepcompress-Cursor sauber zurueckgesetzt,
t0 landet garantiert NACH last_step_clock.
"""

from klipper_extras import buffer_feeder


def test_resume_after_overflow_force_reprime_via_primed_false(
        feeder_with_connect, monkeypatch):
    """resume_after_overflow muss _stepcompress_primed auf False
    setzen vor dem Submit, damit _reprime_stepcompress_if_needed im
    forced_t0=None-Pfad flush_step_generation + set_position(0)
    triggert."""
    feeder = feeder_with_connect
    feeder._startup_grace_done = True
    feeder._set_state(buffer_feeder.STATE_OVERFLOW)
    feeder._overflow_interrupted_state = buffer_feeder.STATE_LOADING_PULL
    feeder._overflow_resume_mm = 810.0
    feeder._overflow_resume_dir = 1
    feeder._overflow_resume_spd = 50.0
    feeder._stepper_synced_to = None
    # Pre-Condition wie Hardware-Realitaet nach mehreren OVERFLOW-Zyklen
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

    monkeypatch.setattr(feeder, "_submit_move", fake_submit_move)
    monkeypatch.setattr(feeder, "_enable_stepper", lambda: None)

    feeder.fault.resume_after_overflow()

    assert 'signed_distance' in captured, "_submit_move not called"
    assert captured['signed_distance'] == 810.0
    assert captured['speed'] == 50.0
    # forced_t0=None damit Reprime-Pfad mit flush_step_generation feuert
    assert captured['forced_t0'] is None
    # primed muss VOR dem Submit False sein, sonst skipt reprime
    assert captured['primed_at_submit'] is False, (
        "_stepcompress_primed MUSS False sein vor Submit. primed=True "
        "wuerde Reprime ueberspringen und Crash reproduzieren.")
    # needs_overflow_prime aufgeraeumt -- wir machen die Prime selbst
    assert captured['needs_prime_at_submit'] is False
    assert feeder._state == buffer_feeder.STATE_LOADING_PULL
    assert feeder._overflow_resume_mm == 0.0


def test_resume_after_overflow_clears_needs_overflow_prime(
        feeder_with_connect, monkeypatch):
    """_needs_overflow_prime muss geclear't werden -- sonst feuert der
    naechste _on_mcu_flush in STATE_AUTO ein zweites Mal den Prime-
    Nudge (redundant und koennte den vom expliziten Reprime gesetzten
    sauberen State wieder durcheinander bringen)."""
    feeder = feeder_with_connect
    feeder._startup_grace_done = True
    feeder._set_state(buffer_feeder.STATE_OVERFLOW)
    feeder._overflow_interrupted_state = buffer_feeder.STATE_LOADING_PULL
    feeder._overflow_resume_mm = 100.0
    feeder._overflow_resume_dir = 1
    feeder._overflow_resume_spd = 50.0
    feeder._stepper_synced_to = None
    feeder._needs_overflow_prime = True

    monkeypatch.setattr(feeder, "_submit_move", lambda *a, **k: None)
    monkeypatch.setattr(feeder, "_enable_stepper", lambda: None)

    feeder.fault.resume_after_overflow()

    assert feeder._needs_overflow_prime is False


def test_resume_after_overflow_passes_signed_distance_correctly(
        feeder_with_connect, monkeypatch):
    """Negative resume_dir muss als signed distance korrekt an
    _submit_move propagieren -- der Anker-Race ist richtungsunabhaengig
    und der Fix darf das Vorzeichen nicht clobberd."""
    feeder = feeder_with_connect
    feeder._startup_grace_done = True
    feeder._set_state(buffer_feeder.STATE_OVERFLOW)
    feeder._overflow_interrupted_state = buffer_feeder.STATE_LOADING_PULL
    feeder._overflow_resume_mm = 500.0
    feeder._overflow_resume_dir = -1
    feeder._overflow_resume_spd = 30.0
    feeder._stepper_synced_to = None

    captured = {}

    def fake_submit_move(signed_distance, speed, forced_t0=None,
                         streaming=False, submit_chunk_cap=None):
        captured['signed_distance'] = signed_distance
        captured['speed'] = speed

    monkeypatch.setattr(feeder, "_submit_move", fake_submit_move)
    monkeypatch.setattr(feeder, "_enable_stepper", lambda: None)

    feeder.fault.resume_after_overflow()

    assert captured['signed_distance'] == -500.0
    assert captured['speed'] == 30.0
