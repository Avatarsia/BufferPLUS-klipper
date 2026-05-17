from pathlib import Path

from tools import buffer_baseline_suite as suite


def test_generate_gcode_and_manifest(tmp_path: Path):
    cases = suite.build_cases(
        flows=[24.0, 30.0],
        durations=[45.0],
        speeds=[100.0],
        accels=[None],
        feed_speed_gains=[1.10, 1.20],
        min_feed_floors=[10.0],
        high_flow_thresholds=[20.0],
        interrupt_chunks=[None],
        flush_chunks=[None],
        lead_times=[None],
        filament_diameters=[1.75],
        mode="product",
    )

    assert len(cases) == 4
    manifest = tmp_path / "manifest.csv"
    gcode = tmp_path / "suite.gcode"
    suite.write_manifest(manifest, cases)
    gcode.write_text(
        suite.generate_gcode(
            cases,
            temp_c=250.0,
            x_mm=160.0,
            y_mm=160.0,
            z_mm=200.0,
            chunk_e_mm=100.0,
            travel_f=12000.0,
            filament_diameter_mm=1.75,
            pause_ms=3000,
        ),
        encoding="utf-8",
    )

    manifest_cases = suite.load_manifest(manifest)
    assert manifest_cases == cases

    text = gcode.read_text(encoding="utf-8")
    assert suite.SUITE_START_TOKEN in text
    assert suite.CASE_START_TOKEN in text
    assert "BUFFER_BASELINE_RUN FLOW=24" in text
    assert "BUFFER_BASELINE_RUN FLOW=30" in text
    assert "MIN_FEED_FLOOR=10" in text
    assert "FEED_SPEED_GAIN=1.2" in text


def test_analyze_log_summarizes_metrics_and_errors(tmp_path: Path):
    case = suite.Case(
        case_id="c001",
        flow_mm3s=30.0,
        duration_s=60.0,
        speed_mm_s=100.0,
        feed_speed_gain=1.20,
        min_feed_floor=10.0,
        high_flow_mm3s_threshold=20.0,
    )
    manifest = tmp_path / "manifest.csv"
    suite.write_manifest(manifest, [case])

    log = tmp_path / "klippy.log"
    log.write_text(
        "\n".join(
            [
                "something before",
                "M118 BFX_CASE_START id=c001 flow=30 duration=60 speed=100 gain=1.2 floor=10 highflow=20",
                "buffer_metrics: state=AUTO hall=[H3:on H2:off H1:off] tracker_vel=12.5mm/s flow=30.0mm3/s high_flow=True ready=True target_speed=18.7mm/s pending_remaining=0.0mm hall1_persist=off",
                "buffer_metrics: state=AUTO hall=[H3:off H2:off H1:off] tracker_vel=11.0mm/s flow=26.4mm3/s high_flow=True ready=True target_speed=9.8mm/s pending_remaining=0.0mm hall1_persist=off",
                "stepcompress o=0 i=0 c=0 a=0: Invalid sequence",
                "M118 BFX_CASE_END id=c001",
            ]
        ),
        encoding="utf-8",
    )

    summaries, samples = suite.analyze_log(log, manifest)

    assert len(samples) == 2
    assert len(summaries) == 1

    summary = summaries[0]
    assert summary.completed is True
    assert summary.metric_samples == 2
    assert round(summary.avg_flow_mm3s, 3) == 28.2
    assert round(summary.max_target_speed_mm_s, 3) == 18.7
    assert summary.floor_hit_samples == 1
    assert summary.above_floor_samples == 1
    assert "invalid_sequence" in summary.errors
