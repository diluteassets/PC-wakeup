"""Linux power actions, via systemd.

On a desktop with an active seat, logind authorises these for the logged-in
user without any extra configuration. Headless or over SSH there is no active
session to authorise against, which is what the shipped polkit rule is for
(install/linux/99-pcwake-power.rules).
"""

from __future__ import annotations

import logging
import shutil

from ..common.protocol import Action
from .power import PowerActionError, PowerBackend, run_command

log = logging.getLogger("pcwake.linux")


class LinuxBackend(PowerBackend):
    name = "linux"

    def sleep(self) -> None:
        run_command(["systemctl", "suspend"], "suspend")

    def shutdown(self) -> None:
        run_command(["systemctl", "poweroff"], "poweroff")

    def restart(self) -> None:
        run_command(["systemctl", "reboot"], "reboot")

    def lock(self) -> None:
        """Lock the screen.

        Desktop environments disagree about who owns the lock screen, so we
        try logind first (it works wherever a session is registered) and fall
        back to the freedesktop helper.
        """
        errors = []
        for argv, what in (
            (["loginctl", "lock-session"], "loginctl lock-session"),
            (["xdg-screensaver", "lock"], "xdg-screensaver lock"),
        ):
            if shutil.which(argv[0]) is None:
                errors.append(f"{argv[0]} not installed")
                continue
            try:
                run_command(argv, what)
                return
            except PowerActionError as exc:
                errors.append(str(exc))
        raise PowerActionError(
            "could not lock the screen: " + "; ".join(errors)
        )

    def preflight(self, action: Action) -> None:
        if action is Action.LOCK:
            if shutil.which("loginctl") is None and shutil.which("xdg-screensaver") is None:
                raise PowerActionError(
                    "neither loginctl nor xdg-screensaver is installed"
                )
            return
        if shutil.which("systemctl") is None:
            raise PowerActionError(
                "systemctl not found; this backend needs systemd"
            )
