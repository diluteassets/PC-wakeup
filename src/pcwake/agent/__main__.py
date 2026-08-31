"""Entry point for the agent, which runs on the PC being controlled."""

from __future__ import annotations

import argparse
import logging
import signal

from ..common import logging as pcwake_logging
from ..common.config import AgentConfig, ConfigError, load_agent_config
from .power import PowerActionError, select_backend

log = logging.getLogger("pcwake.agent")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pcwake-agent",
        description="Runs on the PC: reports presence and performs power commands.",
    )
    parser.add_argument("-c", "--config", help="path to config.toml")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    parser.add_argument(
        "--log-file",
        help="also write logs here (rotating). Used automatically when there "
             "is no console, as when launched with pythonw.exe",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="log power commands instead of performing them",
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="run",
        choices=["run", "doctor"],
        help="run the agent (default), or check this machine's setup",
    )
    args = parser.parse_args(argv)

    pcwake_logging.setup(args.verbose, args.log_file)

    try:
        config = load_agent_config(args.config)
    except ConfigError as exc:
        log.error("%s", exc)
        return 2
    if args.dry_run:
        config = AgentConfig(
            host=config.host,
            broker=config.broker,
            dry_run=True,
            force_shutdown=config.force_shutdown,
        )

    if args.command == "doctor":
        from .doctor import report, run_checks

        return report(run_checks(config))
    return _run_agent(config)


def _run_agent(config: AgentConfig) -> int:
    from .client import Agent

    try:
        backend = select_backend(config.dry_run)
    except PowerActionError as exc:
        log.error("%s", exc)
        return 2

    # Windows shutdown wants /f threaded through; the other backends take no
    # options, so this stays a narrow special case rather than a config object
    # passed everywhere.
    if backend.name == "windows" and config.force_shutdown:
        from .win import WindowsBackend

        backend = WindowsBackend(force_shutdown=True)

    agent = Agent(config, backend)

    def handle_signal(signum, frame):
        # Publish an accurate offline status on the way out, so the hub does
        # not keep offering power buttons for an agent that has stopped.
        log.info("received signal %s, shutting down", signum)
        agent.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, handle_signal)
        except (ValueError, OSError, AttributeError):
            # Not the main thread, or a platform without this signal.
            pass

    try:
        agent.run()
    except KeyboardInterrupt:
        agent.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
