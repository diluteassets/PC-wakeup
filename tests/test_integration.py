"""End-to-end tests of the agent and hub against a real broker.

Covers the paths that only exist once both halves and a broker are in the
room: retained presence, last-will delivery, command/ack correlation, and the
ack-before-action ordering that makes a shutdown reportable.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from pcwake.agent.client import Agent
from pcwake.agent.power import FakeBackend
from pcwake.common.config import AgentConfig, BrokerConfig
from pcwake.common.protocol import Action, Presence
from pcwake.hub.mqtt import HubMqtt
from pcwake.hub.state import HostState, HubState

from .conftest import wait_until

HOST = "testpc"


async def send(hub, action: Action):
    """Send a command the way the bot does: register the ack waiter first,
    then publish. Registering afterwards leaves a window in which the ack is
    dropped -- see TestAckWaiterOrdering in test_bot.py."""
    from pcwake.common.protocol import Command

    command = Command(action=action)
    future = hub.state.pending.register(command.id)
    await hub.mqtt.publish_command(HOST, command)
    return future


class BlockingBackend(FakeBackend):
    """Holds an action open so the test can inspect what the hub knows
    *while* the action is still in progress. That is the only way to pin an
    ordering rather than a timing coincidence."""

    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()

    def perform(self, action: Action) -> None:
        self.calls.append(action)
        self.started.set()
        self.release.wait(timeout=10)


def agent_config(port: int) -> AgentConfig:
    return AgentConfig(
        host=HOST, broker=BrokerConfig(host="127.0.0.1", port=port), dry_run=True
    )


class RunningAgent:
    """Runs a real Agent on paho's own thread, as it runs in production."""

    def __init__(self, port: int, backend, action_delay: float = 0.3) -> None:
        self.backend = backend
        self.agent = Agent(agent_config(port), backend, action_delay=action_delay)
        self._thread = threading.Thread(target=self.agent.run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self.agent.stop()
        self._thread.join(timeout=5)

    def kill(self) -> None:
        """Drop the connection without a clean disconnect, so the broker
        publishes the last will -- what a crash or a power cut looks like."""
        self.agent.client.socket().close()


class RunningHub:
    def __init__(self, port: int) -> None:
        self.state = HubState.for_hosts([HOST])
        self.mqtt = HubMqtt(BrokerConfig(host="127.0.0.1", port=port), self.state)
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self.mqtt.run())
        assert await self.mqtt.wait_connected(timeout=10), "hub never reached the broker"

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    @property
    def host(self):
        return self.state.get(HOST)


@pytest.fixture
async def hub(broker):
    running = RunningHub(broker)
    await running.start()
    yield running
    await running.stop()


@pytest.fixture
def agent(broker):
    running = RunningAgent(broker, FakeBackend())
    running.start()
    yield running
    running.stop()


class TestPresence:
    async def test_agent_connecting_makes_the_host_online(self, hub, agent):
        assert await wait_until(lambda: hub.host.resolve() is HostState.ONLINE)

    async def test_agent_publishes_its_info(self, hub, agent):
        assert await wait_until(lambda: hub.host.info is not None)
        assert hub.host.info.agent_version
        assert hub.host.info.dry_run is True

    async def test_a_killed_agent_shows_offline_via_the_last_will(self, hub, agent):
        # The whole offline-detection mechanism: no polling, no heartbeat,
        # just the broker publishing the will when the socket dies.
        assert await wait_until(lambda: hub.host.resolve() is HostState.ONLINE)
        agent.kill()
        assert await wait_until(
            lambda: hub.host.presence is Presence.OFFLINE, timeout=15
        )

    async def test_a_clean_stop_also_leaves_an_offline_status(self, hub, broker):
        # An operator-stopped agent must not leave `online` retained, or the
        # hub would offer power buttons that cannot work.
        running = RunningAgent(broker, FakeBackend())
        running.start()
        assert await wait_until(lambda: hub.host.resolve() is HostState.ONLINE)
        running.stop()
        assert await wait_until(lambda: hub.host.presence is Presence.OFFLINE)

    async def test_a_late_hub_still_learns_the_state_from_the_retained_status(
        self, broker, agent
    ):
        # A hub restart must not need the agents to do anything: the retained
        # status arrives on subscribe.
        await asyncio.sleep(1.0)
        late = RunningHub(broker)
        await late.start()
        try:
            assert await wait_until(lambda: late.host.resolve() is HostState.ONLINE)
        finally:
            await late.stop()


class TestCommandRoundTrip:
    async def test_lock_is_performed_and_acked(self, hub, agent):
        assert await wait_until(lambda: hub.host.resolve() is HostState.ONLINE)
        future = await send(hub, Action.LOCK)
        ack = await asyncio.wait_for(future, timeout=10)
        assert ack.ok is True
        assert agent.backend.calls == [Action.LOCK]

    @pytest.mark.parametrize("action", list(Action))
    async def test_every_action_round_trips(self, hub, agent, action):
        assert await wait_until(lambda: hub.host.resolve() is HostState.ONLINE)
        future = await send(hub, action)
        ack = await asyncio.wait_for(future, timeout=10)
        assert ack.ok is True and ack.action is action
        assert await wait_until(lambda: agent.backend.calls == [action])

    async def test_a_failing_action_acks_with_the_reason(self, hub, broker):
        class FailingBackend(FakeBackend):
            def perform(self, action):
                from pcwake.agent.power import PowerActionError

                raise PowerActionError("no session to lock")

        running = RunningAgent(broker, FailingBackend())
        running.start()
        try:
            assert await wait_until(lambda: hub.host.resolve() is HostState.ONLINE)
            future = await send(hub, Action.LOCK)
            ack = await asyncio.wait_for(future, timeout=10)
            assert ack.ok is False
            assert "no session" in ack.error
        finally:
            running.stop()

    async def test_a_malformed_command_does_not_kill_the_agent(self, hub, agent):
        assert await wait_until(lambda: hub.host.resolve() is HostState.ONLINE)
        from pcwake.common.protocol import QOS, cmd_topic

        await hub.mqtt._client.publish(cmd_topic(HOST), b"{not json", qos=QOS)
        await asyncio.sleep(0.5)
        # Still alive and still doing its job.
        future = await send(hub, Action.LOCK)
        assert (await asyncio.wait_for(future, timeout=10)).ok is True


class TestAckOrdering:
    """The failure this project would otherwise ship with: an ack that never
    escapes because the machine went down first."""

    async def test_ack_for_a_shutdown_arrives_before_the_action_runs(
        self, hub, broker
    ):
        backend = BlockingBackend()
        running = RunningAgent(broker, backend, action_delay=0.5)
        running.start()
        try:
            assert await wait_until(lambda: hub.host.resolve() is HostState.ONLINE)
            future = await send(hub, Action.SHUTDOWN)
            ack = await asyncio.wait_for(future, timeout=10)

            # The ack is in hand and the machine has not begun going down.
            # Reverse the order in _execute and this assertion fails.
            assert ack.ok is True
            assert not backend.started.is_set(), (
                "the action began before the hub had the ack; on a real PC the "
                "ack would never have reached the broker"
            )

            backend.release.set()
            assert await wait_until(lambda: backend.calls == [Action.SHUTDOWN])
        finally:
            backend.release.set()
            running.stop()

    @pytest.mark.parametrize(
        "action", [Action.SLEEP, Action.SHUTDOWN, Action.RESTART]
    )
    async def test_every_host_downing_action_acks_first(self, hub, broker, action):
        backend = BlockingBackend()
        running = RunningAgent(broker, backend, action_delay=0.5)
        running.start()
        try:
            assert await wait_until(lambda: hub.host.resolve() is HostState.ONLINE)
            future = await send(hub, action)
            await asyncio.wait_for(future, timeout=10)
            assert not backend.started.is_set()
        finally:
            backend.release.set()
            running.stop()

    async def test_lock_acks_only_after_the_action_completes(self, hub, broker):
        # The other half of the contract: lock keeps the host up, so its ack
        # can and does report the real outcome.
        backend = BlockingBackend()
        running = RunningAgent(broker, backend)
        running.start()
        try:
            assert await wait_until(lambda: hub.host.resolve() is HostState.ONLINE)
            future = await send(hub, Action.LOCK)

            assert await wait_until(backend.started.is_set)
            # The action is in progress and no ack has been sent yet.
            await asyncio.sleep(0.3)
            assert not future.done()

            backend.release.set()
            assert (await asyncio.wait_for(future, timeout=10)).ok is True
        finally:
            backend.release.set()
            running.stop()


class TestTimeouts:
    async def test_a_command_to_a_dead_agent_times_out_rather_than_hanging(self, hub):
        # No agent is running. The hub must produce a visible timeout, which
        # is what the bot turns into "heard nothing back".
        from pcwake.common.protocol import Command

        command = Command(action=Action.LOCK)
        future = hub.state.pending.register(command.id)
        await hub.mqtt.publish_command(HOST, command)
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(future, timeout=1.0)
        hub.state.pending.discard(command.id)
        assert len(hub.state.pending) == 0
