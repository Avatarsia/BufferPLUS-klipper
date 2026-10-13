#!/usr/bin/env python3
"""Generate and analyze BufferPLUS baseline test suites.

This tool is intentionally host-side and standard-library only.

Typical workflow:

1. Generate a multi-case G-code suite plus a manifest CSV:

       python3 tools/buffer_baseline_suite.py generate \
           --flows 24 30 40 \
           --feed-speed-gains 1.10 1.20 \
           --min-feed-floors 10 12 \
           --high-flow-thresholds 20 24 \
           --speeds 100 \
           --durations 60

2. Run the generated G-code on the printer. The file calls the
   BUFFER_BASELINE_RUN macro for every case and writes BFX_CASE_START /
   BFX_CASE_END markers into the Klipper log.

3. Analyze the resulting klippy.log:

       python3 tools/buffer_baseline_suite.py analyze \
           --log ~/printer_data/logs/klippy_real.log
"""

from __future__ import annotations

import argparse
import csv
import itertools
import json
import math
import re
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Iterable


CASE_START_TOKEN = "BFX_CASE_START"
CASE_END_TOKEN = "BFX_CASE_END"
MEASURE_START_TOKEN = "BFX_MEASURE_START"
MEASURE_END_TOKEN = "BFX_MEASURE_END"
SUITE_START_TOKEN = "BFX_SUITE_START"
SUITE_END_TOKEN = "BFX_SUITE_END"

DEFAULT_PRINTER_DATA_DIR = Path.home() / "printer_data"
DEFAULT_GCODE_OUTPUT = DEFAULT_PRINTER_DATA_DIR / "gcodes" / "buffer_baseline_suite.gcode"
DEFAULT_MANIFEST_OUTPUT = DEFAULT_PRINTER_DATA_DIR / "config" / "buffer_baseline_suite_manifest.csv"
DEFAULT_SUMMARY_OUTPUT = DEFAULT_PRINTER_DATA_DIR / "config" / "buffer_baseline_summary.csv"
DEFAULT_SAMPLES_OUTPUT = DEFAULT_PRINTER_DATA_DIR / "config" / "buffer_baseline_samples.csv"
DEFAULT_JSON_OUTPUT = DEFAULT_PRINTER_DATA_DIR / "config" / "buffer_baseline_summary.json"

ERROR_PATTERNS: dict[str, re.Pattern[str]] = {
    "invalid_sequence": re.compile(r"Invalid sequence", re.IGNORECASE),
    "flush_handler": re.compile(r"Exception in flush_handler", re.IGNORECASE),
    "timer_too_close": re.compile(r"Timer too close", re.IGNORECASE),
    "shutdown": re.compile(r"Transition to shutdown state", re.IGNORECASE),
    "lost_mcu": re.compile(r"Lost communication with MCU", re.IGNORECASE),
}

METRICS_RE = re.compile(
    r"buffer_metrics: state=(?P<state>\S+) "
    r"hall=\[H3:(?P<h3>on|off) H2:(?P<h2>on|off) H1:(?P<h1>on|off)\] "
    r"tracker_vel=(?P<tracker_vel>-?\d+(?:\.\d+)?)mm/s "
    r"flow=(?P<flow>-?\d+(?:\.\d+)?)mm3/s "
    r"high_flow=(?P<high_flow>True|False) "
    r"ready=(?P<ready>True|False) "
    r"target_speed=(?P<target_speed>-?\d+(?:\.\d+)?)mm/s "
    r"pending_remaining=(?P<pending_remaining>-?\d+(?:\.\d+)?)mm "
    r"hall1_persist=(?P<hall1_persist>\S+)"
)

KV_RE = re.compile(r"([A-Za-z0-9_]+)=([^\s]+)")


@dataclass(frozen=True)
class Case:
    case_id: str
    flow_mm3s: float
    duration_s: float
    speed_mm_s: float | None = None
    accel_mm_s2: float | None = None
    feed_speed_gain: float | None = None
    min_feed_floor: float | None = None
    high_flow_mm3s_threshold: float | None = None
    interrupt_chunk_mm: float | None = None
    flush_chunk_mm: float | None = None
    lead_time_s: float | None = None
    filament_diameter_mm: float | None = None

    def to_manifest_row(self) -> dict[str, str]:
        row = {}
        for key, value in asdict(self).items():
            row[key] = "" if value is None else str(value)
        return row

    @staticmethod
    def from_manifest_row(row: dict[str, str]) -> "Case":
        def parse_optional_float(name: str) -> float | None:
            value = row.get(name, "")
            return None if value == "" else float(value)

        return Case(
            case_id=row["case_id"],
            flow_mm3s=float(row["flow_mm3s"]),
            duration_s=float(row["duration_s"]),
            speed_mm_s=parse_optional_float("speed_mm_s"),
            accel_mm_s2=parse_optional_float("accel_mm_s2"),
            feed_speed_gain=parse_optional_float("feed_speed_gain"),
            min_feed_floor=parse_optional_float("min_feed_floor"),
            high_flow_mm3s_threshold=parse_optional_float(
                "high_flow_mm3s_threshold"
            ),
            interrupt_chunk_mm=parse_optional_float("interrupt_chunk_mm"),
            flush_chunk_mm=parse_optional_float("flush_chunk_mm"),
            lead_time_s=parse_optional_float("lead_time_s"),
            filament_diameter_mm=parse_optional_float("filament_diameter_mm"),
        )

    def marker(self) -> str:
        parts = [
            f"id={self.case_id}",
            f"flow={self.flow_mm3s:g}",
            f"duration={self.duration_s:g}",
        ]
        if self.speed_mm_s is not None:
            parts.append(f"speed={self.speed_mm_s:g}")
        if self.feed_speed_gain is not None:
            parts.append(f"gain={self.feed_speed_gain:g}")
        if self.min_feed_floor is not None:
            parts.append(f"floor={self.min_feed_floor:g}")
        if self.high_flow_mm3s_threshold is not None:
            parts.append(f"highflow={self.high_flow_mm3s_threshold:g}")
        return " ".join(parts)

    def macro_call(
        self,
        *,
        temp_c: float,
        x_mm: float,
        y_mm: float,
        z_mm: float,
        chunk_e_mm: float,
        travel_f: float,
        filament_diameter_mm: float,
    ) -> str:
        parts = [
            "BUFFER_BASELINE_RUN",
            f"FLOW={self.flow_mm3s:g}",
            f"DURATION={self.duration_s:g}",
            f"TEMP={temp_c:g}",
            f"X={x_mm:g}",
            f"Y={y_mm:g}",
            f"Z={z_mm:g}",
            f"CASE_ID={self.case_id}",
            f"CHUNK_E={chunk_e_mm:g}",
            f"TRAVEL_F={travel_f:g}",
            f"FILAMENT_DIAMETER={self.filament_diameter_mm or filament_diameter_mm:g}",
        ]
        if self.speed_mm_s is not None:
            parts.append(f"SPEED={self.speed_mm_s:g}")
        if self.accel_mm_s2 is not None:
            parts.append(f"ACCEL={self.accel_mm_s2:g}")
        if self.feed_speed_gain is not None:
            parts.append(f"FEED_SPEED_GAIN={self.feed_speed_gain:g}")
        if self.min_feed_floor is not None:
            parts.append(f"MIN_FEED_FLOOR={self.min_feed_floor:g}")
        if self.high_flow_mm3s_threshold is not None:
            parts.append(f"HIGH_FLOW_MM3S={self.high_flow_mm3s_threshold:g}")
        if self.interrupt_chunk_mm is not None:
            parts.append(f"INTERRUPT_CHUNK_MM={self.interrupt_chunk_mm:g}")
        if self.flush_chunk_mm is not None:
            parts.append(f"CHUNK_MM={self.flush_chunk_mm:g}")
        if self.lead_time_s is not None:
            parts.append(f"LEAD_TIME={self.lead_time_s:g}")
        return " ".join(parts)


@dataclass
class MetricSample:
    case_id: str
    line_number: int
    state: str
    hall_h3: bool
    hall_h2: bool
    hall_h1: bool
    tracker_vel_mm_s: float
    flow_mm3s: float
    high_flow: bool
    ready: bool
    target_speed_mm_s: float
    pending_remaining_mm: float
    hall1_persist: str

    def to_row(self) -> dict[str, str]:
        return {
            "case_id": self.case_id,
            "line_number": str(self.line_number),
            "state": self.state,
            "hall_h3": str(self.hall_h3),
            "hall_h2": str(self.hall_h2),
            "hall_h1": str(self.hall_h1),
            "tracker_vel_mm_s": f"{self.tracker_vel_mm_s:.6f}",
            "flow_mm3s": f"{self.flow_mm3s:.6f}",
            "high_flow": str(self.high_flow),
            "ready": str(self.ready),
            "target_speed_mm_s": f"{self.target_speed_mm_s:.6f}",
            "pending_remaining_mm": f"{self.pending_remaining_mm:.6f}",
            "hall1_persist": self.hall1_persist,
        }


@dataclass
class CaseSummary:
    case: Case
    completed: bool
    metric_samples: int
    avg_flow_mm3s: float | None
    max_flow_mm3s: float | None
    avg_target_speed_mm_s: float | None
    max_target_speed_mm_s: float | None
    floor_hit_samples: int
    floor_hit_pct: float | None
    above_floor_samples: int
    above_floor_pct: float | None
    zero_target_samples: int
    high_flow_samples: int
    hall3_samples: int
    hall2_samples: int
    overflow_samples: int
    errors: list[str]
    error_lines: list[int]

    def to_row(self) -> dict[str, str]:
        return {
            **self.case.to_manifest_row(),
            "completed": str(self.completed),
            "metric_samples": str(self.metric_samples),
            "avg_flow_mm3s": _fmt_optional(self.avg_flow_mm3s),
            "max_flow_mm3s": _fmt_optional(self.max_flow_mm3s),
            "avg_target_speed_mm_s": _fmt_optional(self.avg_target_speed_mm_s),
            "max_target_speed_mm_s": _fmt_optional(self.max_target_speed_mm_s),
            "floor_hit_samples": str(self.floor_hit_samples),
            "floor_hit_pct": _fmt_optional(self.floor_hit_pct),
            "above_floor_samples": str(self.above_floor_samples),
            "above_floor_pct": _fmt_optional(self.above_floor_pct),
            "zero_target_samples": str(self.zero_target_samples),
            "high_flow_samples": str(self.high_flow_samples),
            "hall3_samples": str(self.hall3_samples),
            "hall2_samples": str(self.hall2_samples),
            "overflow_samples": str(self.overflow_samples),
            "errors": ";".join(self.errors),
            "error_lines": ";".join(str(x) for x in self.error_lines),
        }


def _fmt_optional(value: float | None) -> str:
    return "" if value is None else f"{value:.6f}"


def _num_slug(value: float) -> str:
    if math.isclose(value, round(value), rel_tol=0.0, abs_tol=1e-9):
        return str(int(round(value)))
    text = f"{value:.3f}".rstrip("0").rstrip(".")
    return text.replace(".", "p").replace("-", "m")


def _iter_product(values: list[list[float | None]]) -> Iterable[tuple[float | None, ...]]:
    return itertools.product(*values)


def _iter_zip_broadcast(values: list[list[float | None]]) -> Iterable[tuple[float | None, ...]]:
    max_len = max(len(v) for v in values)
    for v in values:
        if len(v) not in (1, max_len):
            raise ValueError(
                "zip mode requires every parameter list to have length 1 "
                f"or {max_len}"
            )
    for idx in range(max_len):
        yield tuple(v[idx if len(v) > 1 else 0] for v in values)


def build_cases(
    *,
    flows: list[float],
    durations: list[float],
    speeds: list[float | None],
    accels: list[float | None],
    feed_speed_gains: list[float | None],
    min_feed_floors: list[float | None],
    high_flow_thresholds: list[float | None],
    interrupt_chunks: list[float | None],
    flush_chunks: list[float | None],
    lead_times: list[float | None],
    filament_diameters: list[float | None],
    mode: str,
) -> list[Case]:
    value_lists = [
        [float(x) for x in flows],
        [float(x) for x in durations],
        list(speeds),
        list(accels),
        list(feed_speed_gains),
        list(min_feed_floors),
        list(high_flow_thresholds),
        list(interrupt_chunks),
        list(flush_chunks),
        list(lead_times),
        list(filament_diameters),
    ]
    iterator = _iter_product(value_lists) if mode == "product" else _iter_zip_broadcast(value_lists)
    cases: list[Case] = []
    for idx, values in enumerate(iterator, start=1):
        (
            flow,
            duration,
            speed,
            accel,
            gain,
            floor,
            high_flow,
            interrupt_chunk,
            flush_chunk,
            lead_time,
            filament_diameter,
        ) = values
        case_id = (
            f"c{idx:03d}_f{_num_slug(flow)}"
            f"_d{_num_slug(duration)}"
            f"_g{_num_slug(gain) if gain is not None else 'na'}"
            f"_floor{_num_slug(floor) if floor is not None else 'na'}"
            f"_hf{_num_slug(high_flow) if high_flow is not None else 'na'}"
            f"_s{_num_slug(speed) if speed is not None else 'na'}"
        )
        cases.append(
            Case(
                case_id=case_id,
                flow_mm3s=float(flow),
                duration_s=float(duration),
                speed_mm_s=None if speed is None else float(speed),
                accel_mm_s2=None if accel is None else float(accel),
                feed_speed_gain=None if gain is None else float(gain),
                min_feed_floor=None if floor is None else float(floor),
                high_flow_mm3s_threshold=None if high_flow is None else float(high_flow),
                interrupt_chunk_mm=(
                    None if interrupt_chunk is None else float(interrupt_chunk)
                ),
                flush_chunk_mm=None if flush_chunk is None else float(flush_chunk),
                lead_time_s=None if lead_time is None else float(lead_time),
                filament_diameter_mm=(
                    None if filament_diameter is None else float(filament_diameter)
                ),
            )
        )
    return cases


def write_manifest(path: Path, cases: list[Case]) -> None:
    fieldnames = list(Case.__dataclass_fields__.keys())
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for case in cases:
            writer.writerow(case.to_manifest_row())


def load_manifest(path: Path) -> list[Case]:
    with path.open("r", newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        return [Case.from_manifest_row(row) for row in reader]


def generate_gcode(
    cases: list[Case],
    *,
    temp_c: float,
    x_mm: float,
    y_mm: float,
    z_mm: float,
    chunk_e_mm: float,
    travel_f: float,
    filament_diameter_mm: float,
    pause_ms: int,
) -> str:
    created = datetime.now(timezone.utc).isoformat()
    lines = [
        "; BufferPLUS baseline suite",
        f"; generated_utc={created}",
        f"BUFFER_BENCHMARK_MARK BUFFER=mellow EVENT=SUITE_START CASES={len(cases)}",
    ]
    for case in cases:
        lines.append(
            "BUFFER_BENCHMARK_MARK BUFFER=mellow EVENT=CASE_START "
            f"CASE_ID={case.case_id} FLOW={case.flow_mm3s:g} "
            f"DURATION={case.duration_s:g}"
            + (f" SPEED={case.speed_mm_s:g}" if case.speed_mm_s is not None else "")
            + (f" GAIN={case.feed_speed_gain:g}" if case.feed_speed_gain is not None else "")
            + (f" FLOOR={case.min_feed_floor:g}" if case.min_feed_floor is not None else "")
            + (f" HIGHFLOW={case.high_flow_mm3s_threshold:g}" if case.high_flow_mm3s_threshold is not None else "")
        )
        lines.append(
            case.macro_call(
                temp_c=temp_c,
                x_mm=x_mm,
                y_mm=y_mm,
                z_mm=z_mm,
                chunk_e_mm=chunk_e_mm,
                travel_f=travel_f,
                filament_diameter_mm=filament_diameter_mm,
            )
        )
        lines.append(
            f"BUFFER_BENCHMARK_MARK BUFFER=mellow EVENT=CASE_END CASE_ID={case.case_id}"
        )
        if pause_ms > 0:
            lines.append(f"G4 P{pause_ms}")
    lines.append(
        f"BUFFER_BENCHMARK_MARK BUFFER=mellow EVENT=SUITE_END CASES={len(cases)}"
    )
    return "\n".join(lines) + "\n"


def _parse_marker_values(line: str, token: str) -> dict[str, str] | None:
    if token not in line:
        return None
    suffix = line.split(token, 1)[1]
    data = {"_raw": suffix.strip()}
    for key, value in KV_RE.findall(suffix):
        data[key] = value
    return data


def _parse_metric_sample(case_id: str, line_number: int, line: str) -> MetricSample | None:
    match = METRICS_RE.search(line)
    if not match:
        return None
    data = match.groupdict()
    return MetricSample(
        case_id=case_id,
        line_number=line_number,
        state=data["state"],
        hall_h3=data["h3"] == "on",
        hall_h2=data["h2"] == "on",
        hall_h1=data["h1"] == "on",
        tracker_vel_mm_s=float(data["tracker_vel"]),
        flow_mm3s=float(data["flow"]),
        high_flow=data["high_flow"] == "True",
        ready=data["ready"] == "True",
        target_speed_mm_s=float(data["target_speed"]),
        pending_remaining_mm=float(data["pending_remaining"]),
        hall1_persist=data["hall1_persist"],
    )


def analyze_log(log_path: Path, manifest_path: Path) -> tuple[list[CaseSummary], list[MetricSample]]:
    cases = load_manifest(manifest_path)
    case_by_id = {case.case_id: case for case in cases}

    current_case_id: str | None = None
    current_measure_id: str | None = None
    completed: dict[str, bool] = {case.case_id: False for case in cases}
    measure_seen: dict[str, bool] = {case.case_id: False for case in cases}
    fallback_metrics_by_case: dict[str, list[MetricSample]] = {case.case_id: [] for case in cases}
    fallback_errors_by_case: dict[str, list[str]] = {case.case_id: [] for case in cases}
    fallback_error_lines_by_case: dict[str, list[int]] = {case.case_id: [] for case in cases}
    measure_metrics_by_case: dict[str, list[MetricSample]] = {case.case_id: [] for case in cases}
    measure_errors_by_case: dict[str, list[str]] = {case.case_id: [] for case in cases}
    measure_error_lines_by_case: dict[str, list[int]] = {case.case_id: [] for case in cases}

    with log_path.open("r", encoding="utf-8", errors="replace") as handle:
        for line_number, line in enumerate(handle, start=1):
            start_data = _parse_marker_values(line, CASE_START_TOKEN)
            if start_data is not None:
                current_case_id = start_data.get("id")
                continue

            end_data = _parse_marker_values(line, CASE_END_TOKEN)
            if end_data is not None:
                case_id = end_data.get("id")
                if case_id in completed:
                    completed[case_id] = True
                if current_case_id == case_id:
                    current_case_id = None
                continue

            measure_start_data = _parse_marker_values(line, MEASURE_START_TOKEN)
            if measure_start_data is not None:
                case_id = measure_start_data.get("id")
                if case_id in measure_seen:
                    current_measure_id = case_id
                    measure_seen[case_id] = True
                continue

            measure_end_data = _parse_marker_values(line, MEASURE_END_TOKEN)
            if measure_end_data is not None:
                case_id = measure_end_data.get("id")
                if current_measure_id == case_id:
                    current_measure_id = None
                continue

            active_case_id: str | None = None
            target_metrics = None
            target_errors = None
            target_error_lines = None
            if current_measure_id is not None and current_measure_id in case_by_id:
                active_case_id = current_measure_id
                target_metrics = measure_metrics_by_case
                target_errors = measure_errors_by_case
                target_error_lines = measure_error_lines_by_case
            elif current_case_id is not None and current_case_id in case_by_id:
                active_case_id = current_case_id
                target_metrics = fallback_metrics_by_case
                target_errors = fallback_errors_by_case
                target_error_lines = fallback_error_lines_by_case

            if active_case_id is None:
                continue

            sample = _parse_metric_sample(active_case_id, line_number, line)
            if sample is not None:
                target_metrics[active_case_id].append(sample)
                continue

            for error_name, pattern in ERROR_PATTERNS.items():
                if pattern.search(line):
                    target_errors[active_case_id].append(error_name)
                    target_error_lines[active_case_id].append(line_number)
                    break

    summaries: list[CaseSummary] = []
    all_samples: list[MetricSample] = []
    for case in cases:
        if measure_seen[case.case_id]:
            samples = measure_metrics_by_case[case.case_id]
            errors = measure_errors_by_case[case.case_id]
            error_lines = measure_error_lines_by_case[case.case_id]
        else:
            samples = fallback_metrics_by_case[case.case_id]
            errors = fallback_errors_by_case[case.case_id]
            error_lines = fallback_error_lines_by_case[case.case_id]
        all_samples.extend(samples)
        targets = [sample.target_speed_mm_s for sample in samples]
        flows = [sample.flow_mm3s for sample in samples]
        floor = case.min_feed_floor
        floor_epsilon = 0.5
        if floor is None:
            floor_hit_samples = 0
            above_floor_samples = 0
            floor_hit_pct = None
            above_floor_pct = None
        else:
            floor_hit_samples = sum(
                1 for value in targets if value <= (floor + floor_epsilon)
            )
            above_floor_samples = sum(
                1 for value in targets if value > (floor + floor_epsilon)
            )
            floor_hit_pct = (
                (floor_hit_samples / len(targets)) * 100.0 if targets else None
            )
            above_floor_pct = (
                (above_floor_samples / len(targets)) * 100.0 if targets else None
            )

        summaries.append(
            CaseSummary(
                case=case,
                completed=completed[case.case_id],
                metric_samples=len(samples),
                avg_flow_mm3s=mean(flows) if flows else None,
                max_flow_mm3s=max(flows) if flows else None,
                avg_target_speed_mm_s=mean(targets) if targets else None,
                max_target_speed_mm_s=max(targets) if targets else None,
                floor_hit_samples=floor_hit_samples,
                floor_hit_pct=floor_hit_pct,
                above_floor_samples=above_floor_samples,
                above_floor_pct=above_floor_pct,
                zero_target_samples=sum(1 for value in targets if value <= 0.5),
                high_flow_samples=sum(1 for sample in samples if sample.high_flow),
                hall3_samples=sum(1 for sample in samples if sample.hall_h3),
                hall2_samples=sum(1 for sample in samples if sample.hall_h2),
                overflow_samples=sum(1 for sample in samples if sample.hall_h1),
                errors=errors,
                error_lines=error_lines,
            )
        )
    return summaries, all_samples


def write_summary_csv(path: Path, summaries: list[CaseSummary]) -> None:
    rows = [summary.to_row() for summary in summaries]
    fieldnames = list(rows[0].keys()) if rows else []
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_samples_csv(path: Path, samples: list[MetricSample]) -> None:
    rows = [sample.to_row() for sample in samples]
    fieldnames = list(rows[0].keys()) if rows else []
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_summary_json(path: Path, summaries: list[CaseSummary]) -> None:
    payload = [summary.to_row() for summary in summaries]
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _parse_float_list(values: list[float] | None) -> list[float]:
    return values if values else []


def _parse_optional_float_list(values: list[float] | None) -> list[float | None]:
    return list(values) if values else [None]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    gen = sub.add_parser("generate", help="Generate suite G-code + manifest")
    gen.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_GCODE_OUTPUT,
        help=f"Output G-code file (default: {DEFAULT_GCODE_OUTPUT})",
    )
    gen.add_argument(
        "--manifest",
        type=Path,
        default=DEFAULT_MANIFEST_OUTPUT,
        help=f"Output manifest CSV (default: {DEFAULT_MANIFEST_OUTPUT})",
    )
    gen.add_argument("--mode", choices=("product", "zip"), default="product")
    gen.add_argument("--flows", nargs="+", type=float, required=True)
    gen.add_argument("--durations", nargs="+", type=float, default=[60.0])
    gen.add_argument("--speeds", nargs="*", type=float)
    gen.add_argument("--accels", nargs="*", type=float)
    gen.add_argument("--feed-speed-gains", nargs="*", type=float)
    gen.add_argument("--min-feed-floors", nargs="*", type=float)
    gen.add_argument("--high-flow-thresholds", nargs="*", type=float)
    gen.add_argument("--interrupt-chunks", nargs="*", type=float)
    gen.add_argument("--flush-chunks", nargs="*", type=float)
    gen.add_argument("--lead-times", nargs="*", type=float)
    gen.add_argument("--filament-diameters", nargs="*", type=float)
    gen.add_argument("--temp", type=float, default=250.0)
    gen.add_argument("--x", type=float, default=160.0)
    gen.add_argument("--y", type=float, default=160.0)
    gen.add_argument("--z", type=float, default=200.0)
    gen.add_argument("--chunk-e", type=float, default=100.0)
    gen.add_argument("--travel-f", type=float, default=12000.0)
    gen.add_argument("--pause-ms", type=int, default=3000)

    ana = sub.add_parser("analyze", help="Analyze a Klipper log against a suite manifest")
    ana.add_argument("--log", required=True, type=Path, help="klippy.log to analyze")
    ana.add_argument(
        "--manifest",
        type=Path,
        default=DEFAULT_MANIFEST_OUTPUT,
        help=f"Manifest CSV used for the run (default: {DEFAULT_MANIFEST_OUTPUT})",
    )
    ana.add_argument(
        "--summary-out",
        type=Path,
        default=DEFAULT_SUMMARY_OUTPUT,
        help=f"Summary CSV output (default: {DEFAULT_SUMMARY_OUTPUT})",
    )
    ana.add_argument(
        "--samples-out",
        type=Path,
        default=DEFAULT_SAMPLES_OUTPUT,
        help=f"Raw metric samples CSV output (default: {DEFAULT_SAMPLES_OUTPUT})",
    )
    ana.add_argument(
        "--json-out",
        type=Path,
        default=DEFAULT_JSON_OUTPUT,
        help=f"Summary JSON output (default: {DEFAULT_JSON_OUTPUT})",
    )
    return parser


def cmd_generate(args: argparse.Namespace) -> int:
    cases = build_cases(
        flows=args.flows,
        durations=args.durations,
        speeds=_parse_optional_float_list(args.speeds),
        accels=_parse_optional_float_list(args.accels),
        feed_speed_gains=_parse_optional_float_list(args.feed_speed_gains),
        min_feed_floors=_parse_optional_float_list(args.min_feed_floors),
        high_flow_thresholds=_parse_optional_float_list(args.high_flow_thresholds),
        interrupt_chunks=_parse_optional_float_list(args.interrupt_chunks),
        flush_chunks=_parse_optional_float_list(args.flush_chunks),
        lead_times=_parse_optional_float_list(args.lead_times),
        filament_diameters=_parse_optional_float_list(args.filament_diameters),
        mode=args.mode,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        generate_gcode(
            cases,
            temp_c=args.temp,
            x_mm=args.x,
            y_mm=args.y,
            z_mm=args.z,
            chunk_e_mm=args.chunk_e,
            travel_f=args.travel_f,
            filament_diameter_mm=1.75,
            pause_ms=args.pause_ms,
        ),
        encoding="utf-8",
    )
    write_manifest(args.manifest, cases)
    print(f"generated {len(cases)} cases")
    print(f"gcode:    {args.output}")
    print(f"manifest: {args.manifest}")
    return 0


def cmd_analyze(args: argparse.Namespace) -> int:
    summaries, samples = analyze_log(args.log, args.manifest)
    args.summary_out.parent.mkdir(parents=True, exist_ok=True)
    write_summary_csv(args.summary_out, summaries)
    if args.samples_out:
        args.samples_out.parent.mkdir(parents=True, exist_ok=True)
        write_samples_csv(args.samples_out, samples)
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        write_summary_json(args.json_out, summaries)
    print(f"summaries: {len(summaries)}")
    print(f"summary:   {args.summary_out}")
    if args.samples_out:
        print(f"samples:   {args.samples_out}")
    if args.json_out:
        print(f"json:      {args.json_out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "generate":
        return cmd_generate(args)
    if args.command == "analyze":
        return cmd_analyze(args)
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
