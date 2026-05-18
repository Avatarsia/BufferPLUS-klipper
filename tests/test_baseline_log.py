"""BaselineLogfile — separate FileHandler for benchmark runs.

The logger writes only buffer-relevant lines (buffer_event[*],
buffer_metrics, buffer_benchmark, buffer_feeder, BufferFeeder) so the
resulting file stays small and parseable by tools/buffer_baseline_suite.
"""

import logging
import os

from klipper_extras.buffer_baseline_log import (
    BaselineLogfile,
    PREFIX_FILTERS,
    _PrefixFilter,
)


def test_default_path_under_printer_data():
    log = BaselineLogfile()
    # Normalize separators: expanduser may mix forward/back slashes on
    # Windows (HOME with backslashes + literal forward slashes in the
    # template). The structural claim is the directory hierarchy.
    normalized = log.path.replace("\\", "/")
    assert normalized.endswith("printer_data/logs/buffer_baseline.log")


def test_custom_path_expanded(tmp_path):
    target = tmp_path / "custom.log"
    log = BaselineLogfile(str(target))
    assert log.path == str(target)


def test_attach_creates_parent_dir(tmp_path):
    target = tmp_path / "subdir" / "x.log"
    log = BaselineLogfile(str(target))
    try:
        assert log.attach() is True
        assert os.path.isdir(os.path.dirname(str(target)))
        assert log.is_attached()
    finally:
        log.detach()


def test_attach_detach_idempotent(tmp_path):
    target = tmp_path / "x.log"
    log = BaselineLogfile(str(target))
    try:
        assert log.attach() is True
        assert log.attach() is True  # second attach no-op
    finally:
        log.detach()
        log.detach()  # second detach no-op
    assert not log.is_attached()


def test_prefix_filter_passes_buffer_events():
    flt = _PrefixFilter()
    record = logging.LogRecord(
        "root", logging.INFO, "x", 0,
        "buffer_event[flush_submit]: foo", None, None)
    assert flt.filter(record) is True


def test_prefix_filter_blocks_unknown_message():
    flt = _PrefixFilter()
    record = logging.LogRecord(
        "root", logging.INFO, "x", 0,
        "Stats 12345.6: gcodein=0 mcu_awake=...", None, None)
    assert flt.filter(record) is False


def test_prefix_filter_covers_all_documented_prefixes():
    flt = _PrefixFilter()
    for prefix in PREFIX_FILTERS:
        record = logging.LogRecord(
            "root", logging.INFO, "x", 0,
            prefix + "rest", None, None)
        assert flt.filter(record) is True, "filter dropped %r" % prefix


def test_attached_handler_writes_only_buffer_lines(tmp_path):
    target = tmp_path / "out.log"
    log = BaselineLogfile(str(target))
    root = logging.getLogger()
    saved_level = root.level
    root.setLevel(logging.INFO)
    log.attach()
    try:
        logging.info("buffer_event[unit_test]: hello world")
        logging.info("Stats 99.9: gcodein=0 should_not_appear")
        logging.info("buffer_metrics: state=AUTO hall=[H3:on H2:off H1:off]")
    finally:
        log.detach()
        root.setLevel(saved_level)

    content = target.read_text(encoding="utf-8")
    assert "buffer_event[unit_test]" in content
    assert "buffer_metrics: state=AUTO" in content
    assert "should_not_appear" not in content
    assert "gcodein=0" not in content


def test_write_one_appends_without_attach(tmp_path):
    target = tmp_path / "marks.log"
    log = BaselineLogfile(str(target))
    assert not log.is_attached()

    log.write_one("buffer_benchmark: BFX_SUITE_START cases=12")
    log.write_one("buffer_benchmark: BFX_CASE_START id=c001")

    content = target.read_text(encoding="utf-8")
    assert "BFX_SUITE_START cases=12" in content
    assert "BFX_CASE_START id=c001" in content
    # Two lines (each with timestamp prefix)
    assert content.count("\n") == 2


def test_write_one_works_after_detach(tmp_path):
    target = tmp_path / "marks.log"
    log = BaselineLogfile(str(target))
    log.attach()
    log.detach()
    log.write_one("buffer_benchmark: BFX_SUITE_END cases=12")

    content = target.read_text(encoding="utf-8")
    assert "BFX_SUITE_END cases=12" in content


def test_write_one_creates_parent_dir(tmp_path):
    target = tmp_path / "deep" / "nested" / "marks.log"
    log = BaselineLogfile(str(target))
    log.write_one("buffer_benchmark: BFX_CASE_START id=c001")
    assert target.exists()
    assert "BFX_CASE_START id=c001" in target.read_text(encoding="utf-8")


def test_detach_closes_file_handler_cleanly(tmp_path):
    target = tmp_path / "out.log"
    log = BaselineLogfile(str(target))
    log.attach()
    root_handlers_with = len(logging.getLogger().handlers)
    log.detach()
    root_handlers_without = len(logging.getLogger().handlers)
    assert root_handlers_without == root_handlers_with - 1
