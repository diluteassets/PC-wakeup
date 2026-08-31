"""One-line logging setup, shared so both roles produce the same format.

Under systemd, stderr already lands in the journal with its own timestamps,
so we keep the format lean and let the journal do the rest.
"""

from __future__ import annotations

import logging
import os
import sys

FORMAT = "%(asctime)s %(levelname)-7s %(name)-18s %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def setup(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose or os.environ.get("PCWAKE_DEBUG") else logging.INFO
    logging.basicConfig(
        level=level, format=FORMAT, datefmt=DATE_FORMAT, stream=sys.stderr
    )
    # These two are chatty at DEBUG and drown out our own lines.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram.ext.ExtBot").setLevel(logging.INFO)
