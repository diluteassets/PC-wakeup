"""Per-host status tracking and in-flight command correlation.

The interesting part is `HostStatus.resolve`, which folds two independent
signals -- the agent's MQTT presence and a plain network probe -- into the
single answer the user sees. Presence alone would conflate "the PC is off"
with "the agent crashed", which are completely different problems that look
identical from the broker's point of view.

Everything here is pure state plus asyncio futures; nothing touches a broker
or a network, so the whole table below is exercised by unit tests.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum

from ..common.protocol import Ack, AgentInfo, Presence

log = logging.getLogger("pcwake.state")


class HostState(Enum):
    """What we tell the user, in the order of certainty we have about it."""

    ONLINE = "online"
    WAKING = "waking"
    AGENT_DOWN = "agent_down"
    OFFLINE = "offline"
    UNKNOWN = "unknown"

    @property
    def emoji(self) -> str:
        return {
            HostState.ONLINE: "\N{LARGE GREEN CIRCLE}",
            HostState.WAKING: "\N{LARGE BLUE CIRCLE}",
            HostState.AGENT_DOWN: "\N{LARGE YELLOW CIRCLE}",
            HostState.OFFLINE: "\N{MEDIUM BLACK CIRCLE}",
            HostState.UNKNOWN: "\N{MEDIUM WHITE CIRCLE}",
        }[self]

    @property
    def label(self) -> str:
        return {
            HostState.ONLINE: "Online",
            HostState.WAKING: "Waking\N{HORIZONTAL ELLIPSIS}",
            HostState.AGENT_DOWN: "Powered on, agent not running",
            HostState.OFFLINE: "Offline",
            HostState.UNKNOWN: "Unknown",
        }[self]

    @property
    def accepts_commands(self) -> bool:
        """Only a host with a live agent can act on a power command."""
        return self is HostState.ONLINE

    def describe(self) -> str:
        return f"{self.emoji} {self.label}"


@dataclass
class HostStatus:
    """Mutable per-host view, updated from MQTT and from the prober."""

    name: str

    # From the retained MQTT status topic / the agent's last will. None means
    # we have never heard anything, which is different from hearing "offline".
    presence: Presence | None = None
    presence_since: float | None = None

    # From the reachability probe. None means we cannot probe (no IP
    # configured) -- in which case the agent-down state is simply never
    # reported, rather than guessed at.
    reachable: bool | None = None
    reachable_at: float | None = None

    info: AgentInfo | None = None

    # Set when a magic packet goes out; the host reads as WAKING until either
    # it comes online or this deadline passes.
    wake_deadline: float | None = None

    def resolve(self, now: float | None = None) -> HostState:
        """Fold the signals into the single state shown to the user.

        | MQTT presence | probe   | result                        |
        |---------------|---------|-------------------------------|
        | online        | any     | ONLINE                        |
        | not online    | pending | WAKING (inside the window)    |
        | not online    | yes     | AGENT_DOWN                    |
        | not online    | no      | OFFLINE                       |
        | offline       | unknown | OFFLINE (trust the last will) |
        | never heard   | unknown | UNKNOWN                       |
        """
        now = time.time() if now is None else now

        # A live agent is unambiguous, and outranks everything else.
        if self.presence is Presence.ONLINE:
            return HostState.ONLINE

        # A wake is in flight: the machine is mid-boot, so neither "offline"
        # nor "agent down" is a useful thing to say yet.
        if self.wake_deadline is not None and now < self.wake_deadline:
            return HostState.WAKING

        # Answering pings without an agent connection is the interesting case:
        # the machine is up but the agent is dead or not yet started.
        if self.reachable is True:
            return HostState.AGENT_DOWN
        if self.reachable is False:
            return HostState.OFFLINE

        # No probe available. The last will is the only evidence we have.
        if self.presence is Presence.OFFLINE:
            return HostState.OFFLINE
        return HostState.UNKNOWN

    def apply_presence(self, presence: Presence, now: float | None = None) -> bool:
        """Record a presence update. Returns True if the value changed."""
        now = time.time() if now is None else now
        changed = presence is not self.presence
        self.presence = presence
        if changed:
            self.presence_since = now
        if presence is Presence.ONLINE:
            # The wake succeeded (or the machine was woken by hand); either
            # way the waking window has served its purpose.
            self.wake_deadline = None
        return changed

    def begin_wake(self, timeout: float, now: float | None = None) -> None:
        now = time.time() if now is None else now
        self.wake_deadline = now + timeout

    def wake_expired(self, now: float | None = None) -> bool:
        """True if a wake window has elapsed without the host coming online."""
        now = time.time() if now is None else now
        return self.wake_deadline is not None and now >= self.wake_deadline

    def clear_wake(self) -> None:
        self.wake_deadline = None


class PendingAcks:
    """Correlates outgoing commands with the acks that answer them.

    Without this the user gets no feedback at all: a command either produces
    a visible result or a visible timeout, never silence.
    """

    def __init__(self) -> None:
        self._futures: dict[str, asyncio.Future[Ack]] = {}

    def __len__(self) -> int:
        return len(self._futures)

    def register(self, command_id: str) -> asyncio.Future[Ack]:
        if command_id in self._futures:
            raise RuntimeError(f"command id {command_id!r} is already in flight")
        future: asyncio.Future[Ack] = asyncio.get_running_loop().create_future()
        self._futures[command_id] = future
        return future

    def resolve(self, ack: Ack) -> bool:
        """Deliver an ack to whoever is waiting. Returns False if nobody is --
        a late ack after a timeout, or a duplicate from a QoS 1 redelivery."""
        future = self._futures.pop(ack.id, None)
        if future is None or future.done():
            log.debug("ack %s had no waiter (late, duplicate, or already timed out)", ack.id)
            return False
        future.set_result(ack)
        return True

    def discard(self, command_id: str) -> None:
        """Stop waiting, after a timeout or a cancellation."""
        self._futures.pop(command_id, None)


@dataclass
class HubState:
    """Everything the hub knows, keyed by host name."""

    hosts: dict[str, HostStatus] = field(default_factory=dict)
    pending: PendingAcks = field(default_factory=PendingAcks)

    @classmethod
    def for_hosts(cls, names: list[str] | tuple[str, ...]) -> HubState:
        return cls(hosts={name: HostStatus(name=name) for name in names})

    def get(self, name: str) -> HostStatus | None:
        """Look up a host. Returns None for a name we are not configured for,
        so a stray retained message on some other host's topic is ignored
        rather than silently creating a phantom entry."""
        return self.hosts.get(name)
