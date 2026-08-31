"""Preflight checks for the agent's machine.

Nearly every "Wake-on-LAN doesn't work" report comes down to one of a handful
of settings that are invisible until you go looking for them. This turns that
hunt into one command that names the setting and the fix.
"""

from __future__ import annotations

import platform
import re
import socket
import subprocess
from dataclasses import dataclass
from enum import Enum

from ..common.config import AgentConfig
from .power import PowerActionError, select_backend


class Result(Enum):
    OK = "ok"
    WARN = "warn"
    FAIL = "fail"

    @property
    def marker(self) -> str:
        return {Result.OK: "[ ok ]", Result.WARN: "[warn]", Result.FAIL: "[FAIL]"}[self]


@dataclass(frozen=True)
class Check:
    name: str
    result: Result
    detail: str
    fix: str | None = None

    def render(self) -> str:
        lines = [f"{self.result.marker} {self.name}: {self.detail}"]
        if self.fix and self.result is not Result.OK:
            lines.append(f"        fix: {self.fix}")
        return "\n".join(lines)


def _run(argv: list[str], timeout: float = 10.0) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return -1, str(exc)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def check_backend(config: AgentConfig) -> Check:
    try:
        backend = select_backend(config.dry_run)
    except PowerActionError as exc:
        return Check("power backend", Result.FAIL, str(exc))
    if backend.name == "dry-run":
        return Check(
            "power backend",
            Result.WARN,
            "dry-run: commands will be logged, not performed",
            "set dry_run = false in [agent] once you have tested the path",
        )
    return Check("power backend", Result.OK, f"{backend.name} backend loaded")


def check_broker(config: AgentConfig) -> Check:
    """Prove the broker is reachable *and* that the credentials work.

    A TCP connect alone would pass with a wrong password, which is exactly the
    failure this check exists to catch.
    """
    broker = config.broker
    endpoint = f"{broker.host}:{broker.port}"
    try:
        with socket.create_connection((broker.host, broker.port), timeout=5):
            pass
    except OSError as exc:
        return Check(
            "broker reachable",
            Result.FAIL,
            f"cannot reach {endpoint}: {exc}",
            "check the hub is running and that broker.host in the config points at the Pi",
        )

    try:
        import paho.mqtt.client as mqtt
    except ImportError:
        return Check("broker auth", Result.WARN, f"{endpoint} accepts connections "
                     "(paho not installed, so credentials were not checked)")

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="pcwake-doctor")
    if broker.username:
        client.username_pw_set(broker.username, broker.password)
    outcome: list[int] = []
    client.on_connect = lambda c, u, f, rc, p=None: outcome.append(int(rc.value if hasattr(rc, "value") else rc))
    try:
        client.connect(broker.host, broker.port, keepalive=10)
        deadline = 5.0
        while not outcome and deadline > 0:
            client.loop(timeout=0.25)
            deadline -= 0.25
        client.disconnect()
    except (OSError, ValueError) as exc:
        return Check("broker auth", Result.FAIL, f"connection to {endpoint} failed: {exc}")

    if not outcome:
        return Check("broker auth", Result.WARN, f"{endpoint} did not answer in time")
    if outcome[0] != 0:
        return Check(
            "broker auth",
            Result.FAIL,
            f"broker refused the connection (code {outcome[0]})",
            "check broker.username / broker.password against the Pi's mosquitto passwd file",
        )
    return Check("broker auth", Result.OK, f"connected and authenticated to {endpoint}")


def check_windows_fast_startup() -> Check:
    """Fast Startup is the single most common reason WoL fails on Windows.

    With it on, "shut down" is really a partial hibernate, and the NIC is
    typically left in a state that will not wake.
    """
    code, output = _run(
        [
            "reg",
            "query",
            r"HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Power",
            "/v",
            "HiberbootEnabled",
        ]
    )
    if code != 0:
        return Check("fast startup", Result.WARN, "could not read the setting")
    match = re.search(r"HiberbootEnabled\s+REG_DWORD\s+0x([0-9a-fA-F]+)", output)
    if match is None:
        return Check("fast startup", Result.WARN, "could not parse the setting")
    if int(match.group(1), 16) != 0:
        return Check(
            "fast startup",
            Result.FAIL,
            "enabled, so a shut-down PC will most likely not wake",
            "run `powercfg /h off` in an admin prompt",
        )
    return Check("fast startup", Result.OK, "disabled")


def check_windows_hibernation() -> Check:
    """With hibernation available, SetSuspendState hibernates rather than
    sleeping -- the machine still comes back, but not the way you asked."""
    code, output = _run(["powercfg", "/a"])
    if code != 0:
        return Check("sleep state", Result.WARN, "could not run `powercfg /a`")
    available = output.split("The following sleep states are not available")[0]
    hibernate_available = "Hibernate" in available
    standby_available = "Standby" in available
    if not standby_available:
        return Check(
            "sleep state",
            Result.FAIL,
            "no standby state available, so /sleep cannot work",
            "check `powercfg /a` output and BIOS sleep settings",
        )
    if hibernate_available:
        return Check(
            "sleep state",
            Result.WARN,
            "hibernation is enabled, so /sleep will hibernate instead of sleeping",
            "run `powercfg /h off` if you want true sleep",
        )
    return Check("sleep state", Result.OK, "standby available, hibernation off")


def check_windows_wake_armed() -> Check:
    code, output = _run(["powercfg", "/devicequery", "wake_armed"])
    if code != 0:
        return Check("wake armed", Result.WARN, "could not run powercfg")
    devices = [line.strip() for line in output.splitlines() if line.strip()]
    real = [d for d in devices if "NONE" not in d.upper()]
    if not real:
        return Check(
            "wake armed",
            Result.FAIL,
            "no device is allowed to wake this PC",
            "Device Manager > your network adapter > Power Management > "
            "tick 'Allow this device to wake the computer'",
        )
    return Check("wake armed", Result.OK, f"{len(real)} device(s) armed: {', '.join(real[:3])}")


def _default_interface() -> str | None:
    code, output = _run(["ip", "route", "get", "1.1.1.1"])
    if code != 0:
        return None
    match = re.search(r"\bdev\s+(\S+)", output)
    return match.group(1) if match else None


def check_linux_wol() -> Check:
    """`Wake-on: g` means the NIC will act on a magic packet. Note that this
    setting does not survive a reboot on its own -- see SETUP."""
    interface = _default_interface()
    if interface is None:
        return Check("wake-on-lan", Result.WARN, "could not determine the default interface")
    if interface.startswith("wl"):
        return Check(
            "wake-on-lan",
            Result.WARN,
            f"default interface {interface} looks like Wi-Fi; WoWLAN is unreliable",
            "use a wired connection for dependable waking",
        )
    code, output = _run(["ethtool", interface])
    if code != 0:
        return Check(
            "wake-on-lan",
            Result.WARN,
            f"could not query {interface} (ethtool usually needs root)",
            f"run `sudo ethtool {interface} | grep Wake-on`",
        )
    setting = _parse_wake_on(output)
    if setting is None:
        return Check("wake-on-lan", Result.WARN, f"{interface} reports no Wake-on setting")
    if "g" not in setting:
        return Check(
            "wake-on-lan",
            Result.FAIL,
            f"{interface} has Wake-on: {setting}, so magic packets are ignored",
            f"sudo ethtool -s {interface} wol g (and make it persistent, see SETUP.md)",
        )
    return Check("wake-on-lan", Result.OK, f"{interface} has Wake-on: {setting}")


def _parse_wake_on(output: str) -> str | None:
    """Pull the *current* Wake-on setting out of `ethtool <iface>` output.

    ethtool prints two lines that both contain "Wake-on:":

        Supports Wake-on: pumbg     <- what the hardware can do
        Wake-on: d                  <- what it is actually set to

    A substring search finds the first, whose value almost always contains
    "g" -- so it reports that magic packets will work on a machine where they
    are switched off. Matching whole lines is what keeps this honest.
    """
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith("Wake-on:"):
            value = stripped.split(":", 1)[1].strip()
            return value or None
    return None


def run_checks(config: AgentConfig) -> list[Check]:
    checks = [check_backend(config), check_broker(config)]
    system = platform.system()
    if system == "Windows":
        checks += [
            check_windows_fast_startup(),
            check_windows_hibernation(),
            check_windows_wake_armed(),
        ]
    elif system == "Linux":
        checks.append(check_linux_wol())
    else:
        checks.append(
            Check("platform", Result.WARN, f"no WoL checks implemented for {system}")
        )
    return checks


def report(checks: list[Check]) -> int:
    """Print the checks. Returns a shell exit code: non-zero if anything failed."""
    for check in checks:
        print(check.render())
    failures = sum(1 for c in checks if c.result is Result.FAIL)
    warnings = sum(1 for c in checks if c.result is Result.WARN)
    print()
    if failures:
        print(f"{failures} check(s) failed, {warnings} warning(s). "
              "Wake or power control will not work reliably until these are fixed.")
        return 1
    if warnings:
        print(f"All checks passed with {warnings} warning(s).")
        return 0
    print("All checks passed.")
    return 0
