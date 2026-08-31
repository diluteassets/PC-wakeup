"""The agent's broker connection, running on the PC.

paho rather than aiomqtt here: the agent has no event loop of its own and no
async work to do, so paho's own network thread is the whole runtime and there
is nothing to bridge.

The ordering in `_execute` is the subtle part of this file. See the comment
there before changing it.
"""

from __future__ import annotations

import collections
import logging
import platform
import threading
import time

import paho.mqtt.client as mqtt

from .. import __version__
from ..common.config import AgentConfig
from ..common.protocol import (
    KEEPALIVE,
    QOS,
    Ack,
    AgentInfo,
    Command,
    Presence,
    ProtocolError,
    ack_topic,
    cmd_topic,
    info_topic,
    status_topic,
)
from .power import PowerActionError, PowerBackend

log = logging.getLogger("pcwake.agent")

ACK_FLUSH_TIMEOUT = 5.0
"""Seconds to wait for the broker to confirm an ack before taking the host
down. Generous: on a LAN this completes in milliseconds, and the cost of
waiting is far smaller than the cost of the user never hearing back."""

ACTION_DELAY = 1.0
"""Seconds between a flushed ack and the action that ends the connection.
wait_for_publish tells us paho handed the packet to the socket; this covers
the rest of the path out."""

RECONNECT_MIN = 1
RECONNECT_MAX = 30

FLAP_COUNT = 4
FLAP_WINDOW = 60.0
"""Connecting this many times inside this many seconds is not a flaky
network -- it is almost always two agents sharing one [agent].host name.
They share an MQTT client id, and the broker is required to disconnect the
existing client whenever the second one connects, so the two kick each other
round in a loop forever. The symptom is a status that flaps and commands
that land on whichever agent happens to be connected, which is baffling
unless something names the cause."""


class Agent:
    """Connects to the broker, announces presence, and performs commands."""

    def __init__(
        self,
        config: AgentConfig,
        backend: PowerBackend,
        *,
        action_delay: float = ACTION_DELAY,
    ) -> None:
        self._config = config
        self._backend = backend
        self._action_delay = action_delay
        # Commands are serialised: two power actions racing each other is
        # never what anyone wanted.
        self._lock = threading.Lock()
        # Timestamps of recent successful connects, used to notice the
        # reconnect loop that a duplicate host name produces.
        self._connects: collections.deque[float] = collections.deque(maxlen=FLAP_COUNT)
        self._warned_about_flapping = False
        self._client = self._build_client()

    @property
    def client(self) -> mqtt.Client:
        return self._client

    def _build_client(self) -> mqtt.Client:
        host = self._config.host
        client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"pcwake-agent-{host}",
        )
        if self._config.broker.username:
            client.username_pw_set(
                self._config.broker.username, self._config.broker.password
            )

        # The last will is the entire offline-detection mechanism. The broker
        # publishes it when this connection dies for any reason -- a clean
        # shutdown, a crashed agent, or a yanked power cable -- so the hub
        # learns the PC is gone without polling anything.
        client.will_set(
            status_topic(host), Presence.OFFLINE.value, qos=QOS, retain=True
        )
        client.reconnect_delay_set(min_delay=RECONNECT_MIN, max_delay=RECONNECT_MAX)
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        return client

    # ------------------------------------------------------------- lifecycle

    def run(self) -> None:
        """Connect and pump messages forever, reconnecting on failure."""
        broker = self._config.broker
        log.info(
            "agent %s (%s backend) connecting to %s:%s",
            self._config.host,
            self._backend.name,
            broker.host,
            broker.port,
        )
        self._client.connect_async(
            broker.host, broker.port, keepalive=broker.keepalive or KEEPALIVE
        )
        # retry_first_connection so an agent that starts before the network is
        # up -- the normal case at boot -- waits rather than exiting.
        self._client.loop_forever(retry_first_connection=True)

    def stop(self) -> None:
        """Disconnect cleanly, leaving an accurate retained status behind.

        Without this an operator-stopped agent would leave `online` retained
        until the keepalive expired, and the hub would offer power buttons
        that could not work.
        """
        try:
            self._client.publish(
                status_topic(self._config.host),
                Presence.OFFLINE.value,
                qos=QOS,
                retain=True,
            ).wait_for_publish(timeout=2.0)
        except Exception as exc:  # noqa: BLE001 - best effort on the way out
            log.debug("could not publish a parting offline status: %s", exc)
        self._client.disconnect()

    # ------------------------------------------------------------- callbacks

    def _on_connect(self, client, userdata, flags, reason_code, properties=None) -> None:
        if reason_code != 0:
            # Most often bad credentials. paho will keep retrying, so say
            # plainly what is wrong rather than letting it spin silently.
            log.error("broker refused the connection: %s", reason_code)
            return

        host = self._config.host
        log.info("connected to the broker as %s", host)
        self._note_connection()
        client.subscribe(cmd_topic(host), qos=QOS)
        client.publish(status_topic(host), Presence.ONLINE.value, qos=QOS, retain=True)
        client.publish(info_topic(host), self._agent_info().encode(), qos=QOS, retain=True)

    def _note_connection(self) -> None:
        """Warn if we are reconnecting far too often to be a network problem."""
        now = time.monotonic()
        self._connects.append(now)
        if self._warned_about_flapping or len(self._connects) < FLAP_COUNT:
            return
        if now - self._connects[0] > FLAP_WINDOW:
            return
        self._warned_about_flapping = True
        log.warning(
            "connected %d times in under %.0fs. This is usually two agents "
            "configured with the same [agent].host (%r): they share an MQTT "
            "client id and the broker disconnects one whenever the other "
            "connects. Give each PC its own name, in its config and in the "
            "hub's [[hosts]] and the mosquitto ACL.",
            FLAP_COUNT,
            FLAP_WINDOW,
            self._config.host,
        )

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None) -> None:
        if reason_code == 0:
            log.info("disconnected from the broker")
        else:
            log.warning("lost the broker connection (%s); reconnecting", reason_code)

    def _on_message(self, client, userdata, message) -> None:
        try:
            command = Command.decode(message.payload)
        except ProtocolError as exc:
            log.warning("ignoring malformed command: %s", exc)
            return
        log.info("received %s (id=%s)", command.action.value, command.id)
        # Off the network thread: a power action can block for a long time
        # (SetSuspendState does not return until the machine resumes) and
        # stalling this thread would stall the keepalive with it.
        threading.Thread(
            target=self._execute,
            args=(command,),
            name=f"pcwake-{command.action.value}",
            daemon=True,
        ).start()

    def _agent_info(self) -> AgentInfo:
        return AgentInfo(
            os=f"{platform.system()} {platform.release()}",
            agent_version=__version__,
            booted_at=_boot_time(),
            dry_run=self._backend.name == "dry-run",
        )

    # --------------------------------------------------------------- actions

    def _execute(self, command: Command) -> None:
        """Perform one command, acking in the order the action requires.

        Two orderings, because the actions differ in kind:

        * LOCK leaves the machine up, so we perform it and then ack the real
          outcome. The ack is accurate.
        * SLEEP / SHUTDOWN / RESTART end this connection. If we performed
          first, the machine would be gone before the ack left the wire and
          every successful shutdown would look like a timeout to the user. So
          we preflight, ack, wait for the broker to confirm the ack, and only
          then act.

        The consequence is that an ack for a host-downing action means "the
        agent accepted this and is about to do it", not "it is done". The
        status transition is what confirms the rest -- which is exactly what
        the user watches anyway.
        """
        with self._lock:
            if not command.action.takes_host_down:
                self._perform_then_ack(command)
            else:
                self._ack_then_perform(command)

    def _perform_then_ack(self, command: Command) -> None:
        try:
            self._backend.perform(command.action)
        except PowerActionError as exc:
            log.error("%s failed: %s", command.action.value, exc)
            self._publish_ack(Ack(command.id, command.action, ok=False, error=str(exc)))
            return
        self._publish_ack(Ack(command.id, command.action, ok=True))

    def _ack_then_perform(self, command: Command) -> None:
        # Preflight first: this is the only chance to refuse an action we can
        # already tell will fail, because after the ack goes out we can no
        # longer report an outcome.
        try:
            self._backend.preflight(command.action)
        except PowerActionError as exc:
            log.error("refusing %s: %s", command.action.value, exc)
            self._publish_ack(Ack(command.id, command.action, ok=False, error=str(exc)))
            return

        flushed = self._publish_ack(Ack(command.id, command.action, ok=True))
        if not flushed:
            # The user will probably see a timeout. Going ahead anyway is the
            # lesser evil: they asked for this, and refusing to act because we
            # could not confirm the receipt would be a stranger failure.
            log.warning(
                "could not confirm the ack for %s reached the broker; "
                "performing anyway",
                command.action.value,
            )
        time.sleep(self._action_delay)

        try:
            self._backend.perform(command.action)
        except PowerActionError as exc:
            # We already said yes. Publish the correction: the hub's waiter is
            # long resolved, but it lands in the log and in anything watching.
            log.error("%s failed after acking: %s", command.action.value, exc)
            self._publish_ack(Ack(command.id, command.action, ok=False, error=str(exc)))

    def _publish_ack(self, ack: Ack) -> bool:
        """Publish an ack. Returns True if the broker confirmed it in time."""
        info = self._client.publish(
            ack_topic(self._config.host), ack.encode(), qos=QOS
        )
        try:
            info.wait_for_publish(timeout=ACK_FLUSH_TIMEOUT)
        except (ValueError, RuntimeError) as exc:
            log.debug("wait_for_publish for ack %s: %s", ack.id, exc)
        return info.is_published()


def _boot_time() -> float | None:
    """Best-effort boot timestamp. Purely informational, so an unsupported
    platform simply reports nothing rather than failing."""
    try:
        with open("/proc/uptime", encoding="ascii") as handle:
            return time.time() - float(handle.readline().split()[0])
    except (OSError, ValueError, IndexError):
        return None
