"""Entry point for the hub, which runs on the always-on Pi."""

from __future__ import annotations

import argparse
import logging

from ..common import logging as pcwake_logging
from ..common.config import ConfigError, load_hub_config

log = logging.getLogger("pcwake.hub")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pcwake-hub",
        description="Telegram bot and MQTT client that wakes and controls a PC.",
    )
    parser.add_argument("-c", "--config", help="path to config.toml")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    args = parser.parse_args(argv)

    pcwake_logging.setup(args.verbose)

    try:
        config = load_hub_config(args.config)
    except ConfigError as exc:
        log.error("%s", exc)
        return 2

    # Imported after the config check so a bad config reports cleanly without
    # first paying for the Telegram library import.
    from .bot import PcWakeBot

    application = PcWakeBot(config).build()
    log.info("polling Telegram for updates")
    # run_polling owns the event loop and handles SIGINT/SIGTERM, so a
    # `systemctl stop` unwinds the background tasks through post_shutdown.
    application.run_polling()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
