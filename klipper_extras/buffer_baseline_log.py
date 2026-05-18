# buffer_baseline_log.py — Dedicated logfile for baseline benchmark runs.
#
# Attaches a FileHandler to the root logger while benchmark mode is
# active. Filters everything that is not buffer_event[*], buffer_metrics,
# buffer_benchmark or buffer_feeder so the resulting file is small and
# easy to feed into tools/buffer_baseline_suite.py analyze.
#
# This file is intentionally NOT a Klipper config object — it loads
# nothing from printer.cfg / lll.cfg by itself. The owning BufferFeeder
# wires it via attach()/detach() in cmd_BUFFER_BENCH_MODE.

import logging
import os


PREFIX_FILTERS = (
    "buffer_event[",
    "buffer_metrics:",
    "buffer_benchmark:",
    "buffer_feeder:",
    "BufferFeeder:",
)


class _PrefixFilter(logging.Filter):
    """Pass only records whose rendered message starts with one of the
    buffer-related prefixes. Unknown content stays out of the file."""

    def filter(self, record):
        try:
            message = record.getMessage()
        except Exception:
            return False
        for prefix in PREFIX_FILTERS:
            if message.startswith(prefix):
                return True
        return False


class BaselineLogfile:
    """Owns the FileHandler lifecycle for one baseline run.

    Usage:
        log = BaselineLogfile(path)
        log.attach()      # opens file, hooks into root logger
        ...
        log.detach()      # closes file, unhooks
    """

    DEFAULT_PATH = "~/printer_data/logs/buffer_baseline.log"

    def __init__(self, path=None):
        self.path = os.path.expanduser(path or self.DEFAULT_PATH)
        self._handler = None
        self._formatter = logging.Formatter(
            fmt="%(asctime)s.%(msecs)03d %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

    def attach(self):
        """Open the file and attach to the root logger. Idempotent.

        Creates the parent directory if missing. On any IO error this
        method swallows the exception and returns False so a faulty path
        does not block benchmark mode from enabling."""
        if self._handler is not None:
            return True
        try:
            parent = os.path.dirname(self.path)
            if parent and not os.path.isdir(parent):
                os.makedirs(parent, exist_ok=True)
            handler = logging.FileHandler(self.path, mode="a",
                                          encoding="utf-8", delay=False)
            handler.setLevel(logging.INFO)
            handler.setFormatter(self._formatter)
            handler.addFilter(_PrefixFilter())
            logging.getLogger().addHandler(handler)
            self._handler = handler
            logging.info(
                "buffer_feeder: baseline logfile attached -> %s", self.path)
            return True
        except (OSError, IOError, ValueError):
            self._handler = None
            return False

    def detach(self):
        """Detach the handler and close the file. Idempotent."""
        if self._handler is None:
            return
        handler = self._handler
        self._handler = None
        try:
            logging.getLogger().removeHandler(handler)
        finally:
            try:
                handler.close()
            except Exception:
                pass
        logging.info(
            "buffer_feeder: baseline logfile detached <- %s", self.path)

    def is_attached(self):
        return self._handler is not None
