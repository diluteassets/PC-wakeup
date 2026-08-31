"""Reachability probing, the secondary signal behind the status model.

MQTT presence tells us whether the *agent* is alive. A ping tells us whether
the *machine* is alive. Only together do they distinguish a powered-off PC
from one that is running with a dead agent.

We shell out to the system `ping` rather than opening a raw socket, because
raw ICMP needs root or CAP_NET_RAW and the hub has no business holding either.
"""

from __future__ import annotations

import asyncio
import logging
import shutil

log = logging.getLogger("pcwake.probe")

PING_TIMEOUT = 2.0
"""Seconds to wait for the ping process overall. A host on the LAN answers in
milliseconds; anything slower is indistinguishable from down for our purpose."""


def ping_available() -> bool:
    return shutil.which("ping") is not None


async def is_reachable(ip: str, timeout: float = PING_TIMEOUT) -> bool | None:
    """Return True if `ip` answers a single ping, False if it does not.

    Returns None if we could not run the probe at all (no ping binary), which
    the status model treats as "no information" rather than as "down" -- an
    unrunnable probe must not be reported to the user as a powered-off PC.
    """
    if not ping_available():
        log.warning("no ping binary found; cannot distinguish a dead agent from a dead PC")
        return None

    # -c1: one packet. -W1: wait at most a second for the reply. -n: skip the
    # reverse DNS lookup, which is pure latency here.
    argv = ["ping", "-c", "1", "-W", "1", "-n", ip]
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError as exc:
        log.warning("could not run ping for %s: %s", ip, exc)
        return None

    try:
        returncode = await asyncio.wait_for(process.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        # Overran even the ping's own deadline; treat as unreachable, but do
        # not leave the process behind.
        process.kill()
        await process.wait()
        log.debug("ping to %s timed out after %.1fs", ip, timeout)
        return False

    return returncode == 0
