"""Platform dispatch for the four power actions.

`select_backend` picks the implementation once at startup. `FakeBackend`
records calls instead of performing them, which is what lets the entire
command path -- broker, protocol, correlation, acking -- be tested without
ever suspending a real machine, and what `--dry-run` uses on a first install.
"""

from __future__ import annotations

import abc
import logging
import platform
import subprocess

from ..common.protocol import Action

log = logging.getLogger("pcwake.power")

COMMAND_TIMEOUT = 15.0
"""Seconds to wait on a helper process. `systemctl poweroff` returns almost
immediately -- if it has not after this long, something is wrong."""


class PowerActionError(RuntimeError):
    """A power action could not be performed. The message reaches the user."""


class PowerBackend(abc.ABC):
    """The four actions, and nothing else.

    v1 has no arbitrary command execution by design: the set of things the
    bot can do to the PC is fixed here, in code, not by whatever arrives on
    the wire.
    """

    name: str = "unknown"

    def perform(self, action: Action) -> None:
        """Run one action. Raises PowerActionError on failure."""
        method = {
            Action.SLEEP: self.sleep,
            Action.SHUTDOWN: self.shutdown,
            Action.RESTART: self.restart,
            Action.LOCK: self.lock,
        }[action]
        method()

    @abc.abstractmethod
    def sleep(self) -> None: ...

    @abc.abstractmethod
    def shutdown(self) -> None: ...

    @abc.abstractmethod
    def restart(self) -> None: ...

    @abc.abstractmethod
    def lock(self) -> None: ...

    def preflight(self, action: Action) -> None:
        """Cheap check that `action` is likely to succeed, run *before* the
        ack for a host-downing action is sent.

        The ack for a sleep or shutdown has to be flushed before the action
        runs, so it cannot report the outcome. Checking first is what keeps
        the gap between "accepted" and "actually happened" small.
        """
        return None


class FakeBackend(PowerBackend):
    """Records what it was asked to do. Used by --dry-run and by the tests."""

    name = "dry-run"

    def __init__(self) -> None:
        self.calls: list[Action] = []

    def perform(self, action: Action) -> None:
        self.calls.append(action)
        log.warning("DRY RUN: would %s now (no action taken)", action.value)

    def sleep(self) -> None:
        self.perform(Action.SLEEP)

    def shutdown(self) -> None:
        self.perform(Action.SHUTDOWN)

    def restart(self) -> None:
        self.perform(Action.RESTART)

    def lock(self) -> None:
        self.perform(Action.LOCK)


def run_command(argv: list[str], what: str) -> None:
    """Run a helper process, turning any failure into a PowerActionError whose
    message is worth showing to someone holding a phone."""
    log.info("%s: running %s", what, " ".join(argv))
    try:
        result = subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT,
        )
    except FileNotFoundError as exc:
        raise PowerActionError(f"{argv[0]} not found on this system") from exc
    except subprocess.TimeoutExpired as exc:
        raise PowerActionError(f"{argv[0]} did not return within {COMMAND_TIMEOUT:.0f}s") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        reason = detail[-1] if detail else f"exit code {result.returncode}"
        raise PowerActionError(f"{what} failed: {reason}")


def select_backend(dry_run: bool = False) -> PowerBackend:
    """Choose the backend for this machine.

    Raises PowerActionError on an unsupported platform rather than starting an
    agent that would fail on every command it received.
    """
    if dry_run:
        log.warning("dry-run mode: power commands will be logged, not performed")
        return FakeBackend()

    system = platform.system()
    if system == "Windows":
        from .win import WindowsBackend

        return WindowsBackend()
    if system == "Linux":
        from .linux import LinuxBackend

        return LinuxBackend()
    raise PowerActionError(
        f"unsupported platform {system!r}: pcwake supports Windows and Linux. "
        "Run with --dry-run to exercise everything but the power actions."
    )
