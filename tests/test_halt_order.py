from fakes_klipper import FakeConfig, FakePrinter
from klipper_extras import buffer_feeder


def make_feeder():
    printer = FakePrinter()
    config = FakeConfig(printer=printer)
    return buffer_feeder.BufferFeeder(config)


def test_cmd_buffer_halt_unsyncs_before_halting_motion(monkeypatch):
    feeder = make_feeder()
    feeder._state = buffer_feeder.STATE_AUTO
    calls = []
    seen = {
        "unsync_called": False,
        "halt_called": False,
    }

    def fake_unsync():
        calls.append(("unsync", seen["halt_called"]))
        seen["unsync_called"] = True
        return False

    def fake_halt_motion():
        calls.append(("halt_motion", seen["unsync_called"]))
        seen["halt_called"] = True

    monkeypatch.setattr(feeder, "_unsync_if_synced", fake_unsync)
    monkeypatch.setattr(feeder, "_halt_motion", fake_halt_motion)
    monkeypatch.setattr(feeder, "_set_state", lambda state: None)
    monkeypatch.setattr(feeder, "_respond", lambda message: None)

    feeder.cmd_BUFFER_HALT(None)

    assert calls == [
        ("unsync", False),
        ("halt_motion", True),
    ]
