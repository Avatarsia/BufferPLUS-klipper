"""Gruppe-4 (Review 2026-07-09): BUFFER_BASELINE_RUN muss jam_action /
hall3_demand_gain am Macro-Start snapshotten koennen, um sie nach dem
Run korrekt zu restaurieren (vorher hardcoded PAUSE bzw. kein Restore
-> kalibrierte Config-Werte bis Klipper-Restart ueberschrieben).
Jinja liest via printer['buffer_feeder mellow'] = get_status()."""

from fakes_klipper import FakeConfig, FakePrinter
from klipper_extras import buffer_feeder


def test_get_status_exposes_jam_action_and_hall3_gain():
    printer = FakePrinter()
    feeder = buffer_feeder.BufferFeeder(FakeConfig(printer=printer))

    status = feeder.get_status(eventtime=0.0)

    assert status.get('jam_action') == feeder.jam_action
    assert status.get('hall3_demand_gain') == feeder.hall3_demand_gain
