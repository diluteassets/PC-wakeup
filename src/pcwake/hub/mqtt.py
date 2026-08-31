"""The hub's broker connection.

aiomqtt rather than paho here: the Telegram library is asyncio, and paho's
callbacks arrive on its own thread, which would force every message to hop
loops via `run_coroutine_threadsafe`. Sharing one event loop removes that
bridge and the class of races that come with it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable

import aiomqtt

from ..common.config import BrokerConfig
from ..common.protocol import (
    ACK_WILDCARD,
    INFO_WILDCARD,
    KEEPALIVE,
    QOS,
    STATUS_WILDCARD,
    Ack,
    AgentInfo,
    Command,
    ProtocolError,
    cmd_topic,
    decode_presence,
    host_from_topic,
)
from .state import HostState, HubState

log = logging.getLogger("pcwake.mqtt")

RECONNECT_MIN = 1.0
RECONNECT_MAX = 30.0

StateChangeCallback = Callable[[str, HostState, HostState], Awaitable[None]]


class HubMqtt:
    """Keeps `HubState` in step with the broker, and publishes commands.

    Runs forever, reconnecting with backoff. A broker that is briefly gone
    must not take the bot down with it -- the user should still get a sensible
    "I can't reach the broker" answer rather than a crashed service.
    """

    def __init__(
        self,
        broker: BrokerConfig,
        state: HubState,
        on_state_change: StateChangeCallback | None = None,
    ) -> None:
        self._broker = broker
        self._state = state
        self._on_state_change = on_state_change
        self._client: aiomqtt.Client | None = None
        self._connected = asyncio.Event()

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    async def run(self) -> None:
        """Connect, subscribe, and pump messages forever."""
        delay = RECONNECT_MIN
        while True:
            try:
                await self._session()
            except aiomqtt.MqttError as exc:
                self._connected.clear()
                self._client = None
                log.warning(
                    "broker connection lost (%s); reconnecting in %.0fs", exc, delay
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, RECONNECT_MAX)
            except asyncio.CancelledError:
                self._connected.clear()
                self._client = None
                raise
            else:
                delay = RECONNECT_MIN

    async def _session(self) -> None:
        client = aiomqtt.Client(
            hostname=self._broker.host,
            port=self._broker.port,
            username=self._broker.username,
            password=self._broker.password,
            keepalive=self._broker.keepalive or KEEPALIVE,
            identifier="pcwake-hub",
        )
        async with client:
            self._client = client
            self._connected.set()
            log.info("connected to broker at %s:%s", self._broker.host, self._broker.port)
            # Retained status arrives immediately on subscribe, so the hub
            # knows every host's state within milliseconds of connecting --
            # even after a hub restart, and without asking the agents anything.
            for topic in (STATUS_WILDCARD, ACK_WILDCARD, INFO_WILDCARD):
                await client.subscribe(topic, qos=QOS)
            async for message in client.messages:
                try:
                    await self._handle(message)
                except ProtocolError as exc:
                    log.warning("ignoring malformed message on %s: %s", message.topic, exc)
                except Exception:  # noqa: BLE001 - one bad message must not kill the pump
                    log.exception("error handling message on %s", message.topic)

    async def _handle(self, message: aiomqtt.Message) -> None:
        topic = str(message.topic)
        host_name = host_from_topic(topic)
        host = self._state.get(host_name)
        if host is None:
            # A host we are not configured for. Ignore rather than inventing
            # an entry, so a stray retained topic cannot clutter /status.
            log.debug("ignoring message for unconfigured host %r", host_name)
            return

        payload = message.payload if isinstance(message.payload, bytes) else b""
        leaf = topic.rsplit("/", 1)[-1]

        if leaf == "status":
            before = host.resolve()
            host.apply_presence(decode_presence(payload))
            after = host.resolve()
            if before is not after:
                log.info("%s: %s -> %s", host_name, before.value, after.value)
                if self._on_state_change is not None:
                    await self._on_state_change(host_name, before, after)
        elif leaf == "ack":
            ack = Ack.decode(payload)
            if not self._state.pending.resolve(ack):
                log.debug("unmatched ack %s for %s", ack.id, host_name)
        elif leaf == "info":
            host.info = AgentInfo.decode(payload)

    async def publish_command(self, host_name: str, command: Command) -> None:
        """Publish an already-built command.

        The caller builds the Command and registers its ack waiter *before*
        calling this, which is the whole reason the command is a parameter
        rather than something constructed here. Publishing awaits the
        broker's PUBACK, and that await frees the event loop -- long enough
        for the agent to answer. An ack that arrives before its waiter exists
        is dropped, and the user is told a command timed out that in fact
        succeeded.
        """
        if self._client is None or not self._connected.is_set():
            raise aiomqtt.MqttError("not connected to the broker")
        await self._client.publish(
            cmd_topic(host_name), payload=command.encode(), qos=QOS
        )
        log.info("sent %s to %s (id=%s)", command.action.value, host_name, command.id)

    async def wait_connected(self, timeout: float = 5.0) -> bool:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._connected.wait(), timeout=timeout)
        return self._connected.is_set()
