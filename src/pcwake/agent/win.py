"""Windows power actions.

Everything here goes through ctypes against user32/powrprof plus the built-in
shutdown.exe, so installing the agent on a fresh Windows box is one
`pip install` with no compiled dependency.
"""

from __future__ import annotations

import ctypes
import logging

from ..common.protocol import Action
from .power import PowerActionError, PowerBackend, run_command

log = logging.getLogger("pcwake.win")


class WindowsBackend(PowerBackend):
    name = "windows"

    def __init__(self, force_shutdown: bool = False) -> None:
        # /f closes applications without asking. Off by default: a shutdown
        # that did not happen is a smaller problem than an unsaved document
        # that did not survive.
        self._force = force_shutdown

    def sleep(self) -> None:
        """Suspend to RAM.

        SetSuspendState(bHibernate, bForce, bWakeupEventsDisabled). We pass
        bHibernate=0 for sleep -- but Windows still hibernates instead if
        hibernation is enabled on the machine, which is why `doctor` checks
        for it and SETUP tells you to run `powercfg /h off`.
        """
        try:
            powrprof = ctypes.windll.powrprof
        except (AttributeError, OSError) as exc:
            raise PowerActionError(f"cannot load powrprof.dll: {exc}") from exc
        # Returns zero on failure, and unlike most Win32 calls does not set a
        # useful last error, so there is nothing more specific to report.
        if powrprof.SetSuspendState(0, 1, 0) == 0:
            raise PowerActionError(
                "SetSuspendState failed; check that sleep is available "
                "(`powercfg /a`)"
            )

    def shutdown(self) -> None:
        run_command(self._shutdown_argv("/s"), "shutdown")

    def restart(self) -> None:
        run_command(self._shutdown_argv("/r"), "restart")

    def lock(self) -> None:
        """Lock the workstation.

        This needs an interactive session, which is why the agent installs as
        a per-user scheduled task at logon rather than as a SYSTEM service.
        """
        try:
            user32 = ctypes.windll.user32
        except (AttributeError, OSError) as exc:
            raise PowerActionError(f"cannot load user32.dll: {exc}") from exc
        if user32.LockWorkStation() == 0:
            error = ctypes.get_last_error() if hasattr(ctypes, "get_last_error") else 0
            raise PowerActionError(
                f"LockWorkStation failed (error {error}); the agent may not be "
                "running in an interactive session"
            )

    def _shutdown_argv(self, flag: str) -> list[str]:
        argv = ["shutdown.exe", flag, "/t", "0"]
        if self._force:
            argv.append("/f")
        return argv

    def preflight(self, action: Action) -> None:
        if action in (Action.SHUTDOWN, Action.RESTART):
            import shutil

            if shutil.which("shutdown.exe") is None:
                raise PowerActionError("shutdown.exe not found on PATH")
