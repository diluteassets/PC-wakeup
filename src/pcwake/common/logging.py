"""Logging setup, shared so both roles produce the same format.

Under systemd, stderr already lands in the journal with its own timestamps,
so the format stays lean and the journal does the rest.

Windows is the awkward one. The agent installs as a scheduled task launched
with pythonw.exe, which has no console -- so `sys.stderr` is None and every
log line is silently discarded. That leaves the agent undebuggable on the
platform where it is hardest to debug by other means, so when there is no
console to write to we fall back to a file rather than to nothing.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from pathlib import Path

FORMAT = "%(asctime)s %(levelname)-7s %(name)-18s %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

MAX_BYTES = 1_000_000
BACKUP_COUNT = 3
"""A megabyte apiece, three rotations. The agent runs for months at a time;
an unbounded log on someone's desktop is its own bug."""


def default_log_file() -> Path:
    """Where to log when there is no console and nothing was configured.

    Only consulted in that case -- a normal foreground run still goes to
    stderr, where you can see it.
    """
    if sys.platform == "win32":
        base = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "pcwake"
    else:
        base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "pcwake"
    return base / "pcwake.log"


def _file_handler(path: Path) -> logging.Handler | None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
        )
    except OSError:
        # An unwritable log location must not stop the agent from running --
        # losing the log is bad, refusing to wake the PC is worse.
        return None
    handler.setFormatter(logging.Formatter(FORMAT, datefmt=DATE_FORMAT))
    return handler


def setup(verbose: bool = False, log_file: str | os.PathLike[str] | None = None) -> None:
    """Configure logging for this process.

    Writes to stderr when there is one, to `log_file` when given, and to a
    default file when there is no console at all -- the pythonw case, where
    the alternative is no log anywhere.
    """
    level = logging.DEBUG if verbose or os.environ.get("PCWAKE_DEBUG") else logging.INFO

    handlers: list[logging.Handler] = []
    if sys.stderr is not None:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(logging.Formatter(FORMAT, datefmt=DATE_FORMAT))
        handlers.append(stream)

    path = Path(log_file) if log_file is not None else None
    if path is None and sys.stderr is None:
        path = default_log_file()
    if path is not None:
        handler = _file_handler(path)
        if handler is not None:
            handlers.append(handler)

    root = logging.getLogger()
    root.setLevel(level)
    # Replace rather than append, so calling setup twice (tests, doctor then
    # run) does not double every line.
    for existing in root.handlers[:]:
        root.removeHandler(existing)
    for handler in handlers:
        root.addHandler(handler)
    if not handlers:
        # No console and no writable file. Better silent than crashing on
        # every log call.
        root.addHandler(logging.NullHandler())

    # These two are chatty at DEBUG and drown out our own lines.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram.ext.ExtBot").setLevel(logging.INFO)
