import logging

from helpers import FakeGCmd


def test_buffer_benchmark_mark_logs_stable_marker(feeder, monkeypatch):
    events = []

    def fake_info(message, *args, **kwargs):
        rendered = message % args if args else message
        events.append(rendered)

    monkeypatch.setattr(logging, "info", fake_info)

    feeder.cmd_BUFFER_BENCHMARK_MARK(FakeGCmd({
        "EVENT": "CASE_START",
        "CASE_ID": "c001",
        "FLOW": "24",
        "DURATION": "45",
        "SPEED": "100",
        "GAIN": "1.1",
        "FLOOR": "10",
        "HIGHFLOW": "20",
    }))

    assert events == [
        "buffer_benchmark: BFX_CASE_START id=c001 flow=24 duration=45 speed=100 gain=1.1 floor=10 highflow=20"
    ]
