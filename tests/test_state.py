"""The status model is the piece most likely to mislead the user if it is
wrong, so every row of the table gets a test -- including the one that
distinguishes a dead agent from a dead PC."""

import asyncio

import pytest

from pcwake.common.protocol import Ack, Action, Presence
from pcwake.hub.state import HostState, HostStatus, HubState, PendingAcks


def status(**kwargs) -> HostStatus:
    return HostStatus(name="desk", **kwargs)


class TestResolve:
    def test_online_presence_is_online(self):
        assert status(presence=Presence.ONLINE).resolve() is HostState.ONLINE

    def test_online_wins_even_if_the_probe_says_unreachable(self):
        # A live MQTT connection is direct evidence; a dropped ping is not.
        # Firewalls block ICMP far more often than agents fake a connection.
        host = status(presence=Presence.ONLINE, reachable=False)
        assert host.resolve() is HostState.ONLINE

    def test_offline_presence_with_failed_probe_is_offline(self):
        host = status(presence=Presence.OFFLINE, reachable=False)
        assert host.resolve() is HostState.OFFLINE

    def test_offline_presence_but_reachable_is_agent_down(self):
        # The row that earns the whole table: the machine is up, the agent is
        # not. Reporting this as "offline" would send the user to the BIOS to
        # debug a problem that is actually a stopped service.
        host = status(presence=Presence.OFFLINE, reachable=True)
        assert host.resolve() is HostState.AGENT_DOWN

    def test_offline_presence_without_a_probe_is_offline(self):
        # No IP configured, so no probe. The last will is all we have and it
        # is good evidence on its own.
        host = status(presence=Presence.OFFLINE, reachable=None)
        assert host.resolve() is HostState.OFFLINE

    def test_never_heard_anything_is_unknown(self):
        # Distinct from OFFLINE: we have no retained status and no probe, so
        # claiming the PC is off would be a guess.
        assert status().resolve() is HostState.UNKNOWN

    def test_never_heard_but_reachable_is_agent_down(self):
        assert status(reachable=True).resolve() is HostState.AGENT_DOWN

    def test_never_heard_and_unreachable_is_offline(self):
        assert status(reachable=False).resolve() is HostState.OFFLINE


class TestWakeWindow:
    def test_waking_while_the_window_is_open(self):
        host = status(presence=Presence.OFFLINE, reachable=False)
        host.begin_wake(timeout=90, now=1000.0)
        assert host.resolve(now=1030.0) is HostState.WAKING

    def test_waking_outranks_agent_down_mid_boot(self):
        # The machine answers pings before the agent has started. During the
        # wake window that is expected progress, not a fault to report.
        host = status(presence=Presence.OFFLINE, reachable=True)
        host.begin_wake(timeout=90, now=1000.0)
        assert host.resolve(now=1010.0) is HostState.WAKING

    def test_falls_back_to_the_real_state_once_the_window_closes(self):
        host = status(presence=Presence.OFFLINE, reachable=False)
        host.begin_wake(timeout=90, now=1000.0)
        assert host.resolve(now=1091.0) is HostState.OFFLINE

    def test_expired_window_becomes_agent_down_if_the_pc_answers(self):
        host = status(presence=Presence.OFFLINE, reachable=True)
        host.begin_wake(timeout=90, now=1000.0)
        assert host.resolve(now=1091.0) is HostState.AGENT_DOWN

    def test_coming_online_clears_the_window(self):
        host = status(presence=Presence.OFFLINE)
        host.begin_wake(timeout=90, now=1000.0)
        host.apply_presence(Presence.ONLINE, now=1005.0)
        assert host.wake_deadline is None
        assert host.resolve(now=1005.0) is HostState.ONLINE

    def test_wake_expired_reports_the_window_elapsing(self):
        host = status()
        assert host.wake_expired(now=1000.0) is False
        host.begin_wake(timeout=90, now=1000.0)
        assert host.wake_expired(now=1050.0) is False
        assert host.wake_expired(now=1090.0) is True


class TestPresenceTracking:
    def test_reports_a_change_and_stamps_it(self):
        host = status()
        assert host.apply_presence(Presence.ONLINE, now=1000.0) is True
        assert host.presence_since == 1000.0

    def test_a_repeat_is_not_a_change(self):
        # Retained messages redeliver on every reconnect; treating each as a
        # transition would spam the user with notifications.
        host = status()
        host.apply_presence(Presence.ONLINE, now=1000.0)
        assert host.apply_presence(Presence.ONLINE, now=1050.0) is False
        assert host.presence_since == 1000.0


class TestHostStatePresentation:
    def test_only_online_accepts_commands(self):
        assert HostState.ONLINE.accepts_commands
        assert not any(
            s.accepts_commands for s in HostState if s is not HostState.ONLINE
        )

    @pytest.mark.parametrize("state", list(HostState))
    def test_every_state_renders(self, state):
        text = state.describe()
        assert text.strip() and state.label in text


class TestPendingAcks:
    async def test_resolves_the_matching_waiter(self):
        pending = PendingAcks()
        future = pending.register("abc123")
        ack = Ack(id="abc123", action=Action.SLEEP, ok=True)
        assert pending.resolve(ack) is True
        assert await future is ack
        assert len(pending) == 0

    async def test_ignores_an_ack_nobody_is_waiting_for(self):
        # QoS 1 redelivers, and an ack can arrive after the hub gave up.
        # Neither should raise.
        pending = PendingAcks()
        assert pending.resolve(Ack(id="nope", action=Action.LOCK, ok=True)) is False

    async def test_a_duplicate_ack_resolves_only_once(self):
        pending = PendingAcks()
        pending.register("abc123")
        ack = Ack(id="abc123", action=Action.SLEEP, ok=True)
        assert pending.resolve(ack) is True
        assert pending.resolve(ack) is False

    async def test_discard_stops_the_wait(self):
        pending = PendingAcks()
        pending.register("abc123")
        pending.discard("abc123")
        assert len(pending) == 0
        assert pending.resolve(Ack(id="abc123", action=Action.SLEEP, ok=True)) is False

    async def test_rejects_a_reused_command_id(self):
        pending = PendingAcks()
        pending.register("abc123")
        with pytest.raises(RuntimeError):
            pending.register("abc123")

    async def test_timeout_leaves_no_stale_waiter(self):
        pending = PendingAcks()
        future = pending.register("abc123")
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(future, timeout=0.01)
        pending.discard("abc123")
        assert len(pending) == 0


class TestHubState:
    def test_builds_an_entry_per_configured_host(self):
        state = HubState.for_hosts(["desk", "laptop"])
        assert state.get("desk").name == "desk"
        assert state.get("laptop").name == "laptop"

    def test_unconfigured_hosts_are_not_invented(self):
        # A stray retained message on someone else's topic must not create a
        # phantom entry that then shows up in /status.
        assert HubState.for_hosts(["desk"]).get("intruder") is None
