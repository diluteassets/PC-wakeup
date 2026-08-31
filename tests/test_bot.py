"""The bot's decision logic: who may issue commands, what needs confirming,
and what happens when the PC cannot act.

These use small stand-ins rather than a full Telegram mock -- the behaviour
worth pinning is ours, not python-telegram-bot's.
"""

from __future__ import annotations

import asyncio

import pytest

from pcwake.common.config import BrokerConfig, HostConfig, HubConfig
from pcwake.common.protocol import Action, Presence
from pcwake.hub.bot import PcWakeBot
from pcwake.hub.state import HostState

ALLOWED_CHAT = 4242
OTHER_CHAT = 9999


class FakeMqtt:
    """Records commands instead of publishing them."""

    def __init__(self) -> None:
        self.published: list[tuple[str, Action]] = []
        self.connected = True

    async def publish_command(self, host_name: str, command):
        self.published.append((host_name, command.action))


class Recorder:
    """Stands in for reply_text / edit_message_text."""

    def __init__(self) -> None:
        self.messages: list[str] = []

    async def __call__(self, text, **kwargs):
        self.messages.append(text)
        return self

    async def edit_text(self, text, **kwargs):
        self.messages.append(text)
        return self


class FakeQuery:
    def __init__(self, data: str) -> None:
        self.data = data
        self.answered = False
        self.edits: list[str] = []

    async def answer(self, *args, **kwargs):
        self.answered = True

    async def edit_message_text(self, text, **kwargs):
        self.edits.append(text)


class FakeChat:
    def __init__(self, chat_id: int) -> None:
        self.id = chat_id


class FakeMessage:
    def __init__(self) -> None:
        self.replies: list[str] = []

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)
        return self


class FakeUpdate:
    def __init__(self, chat_id: int = ALLOWED_CHAT, query: FakeQuery | None = None):
        self.effective_chat = FakeChat(chat_id)
        self.effective_message = FakeMessage()
        self.effective_user = None
        self.callback_query = query


class FakeContext:
    def __init__(self, args: list[str] | None = None) -> None:
        self.args = args or []


def make_bot(hosts=None) -> PcWakeBot:
    config = HubConfig(
        telegram_token="123:abc",
        allowed_chat_ids=(ALLOWED_CHAT,),
        hosts=hosts or (HostConfig(name="desk", mac="aa:bb:cc:dd:ee:ff", ip="10.0.0.5"),),
        broker=BrokerConfig(),
    )
    bot = PcWakeBot(config)
    bot._mqtt = FakeMqtt()
    return bot


def set_online(bot: PcWakeBot, name: str = "desk") -> None:
    bot._state.get(name).apply_presence(Presence.ONLINE)


class TestHostResolution:
    def test_a_single_host_needs_no_argument(self):
        bot = make_bot()
        assert bot._resolve_host([]).name == "desk"

    def test_the_argument_selects_among_several(self):
        bot = make_bot(
            hosts=(
                HostConfig(name="desk", mac="aa:bb:cc:dd:ee:ff"),
                HostConfig(name="laptop", mac="11:22:33:44:55:66"),
            )
        )
        assert bot._resolve_host(["laptop"]).name == "laptop"
        assert bot._resolve_host([]).name == "desk"

    def test_an_unknown_name_resolves_to_nothing(self):
        assert make_bot()._resolve_host(["nope"]) is None


@pytest.fixture
def fast_ack(monkeypatch):
    """Shrink the ack timeout so the timeout paths do not cost ten seconds
    each. The tests that use it have no agent answering by design."""
    monkeypatch.setattr("pcwake.hub.bot.ACK_TIMEOUT", 0.05)


class TestCommandGating:
    async def test_an_offline_host_refuses_the_command_without_publishing(self):
        # Publishing into the void would leave the user waiting the full ack
        # timeout to be told what we already know.
        bot = make_bot()
        bot._state.get("desk").apply_presence(Presence.OFFLINE)
        respond = Recorder()
        await bot._run_command(bot._config.default_host, Action.SLEEP, respond)
        assert bot._mqtt.published == []
        assert "Offline" in respond.messages[0]

    async def test_an_agent_down_host_refuses_the_command(self):
        bot = make_bot()
        status = bot._state.get("desk")
        status.apply_presence(Presence.OFFLINE)
        status.reachable = True
        respond = Recorder()
        await bot._run_command(bot._config.default_host, Action.LOCK, respond)
        assert bot._mqtt.published == []
        assert "agent not running" in respond.messages[0]

    async def test_an_online_host_publishes_the_command(self, fast_ack):
        bot = make_bot()
        set_online(bot)
        respond = Recorder()

        # The ack never arrives here, so this exercises the timeout path too.
        await bot._run_command(bot._config.default_host, Action.SLEEP, respond)

        assert bot._mqtt.published == [("desk", Action.SLEEP)]
        assert "heard nothing back" in respond.messages[0]

    async def test_a_timeout_leaves_no_stale_waiter(self, fast_ack):
        bot = make_bot()
        set_online(bot)
        await bot._run_command(bot._config.default_host, Action.SLEEP, Recorder())
        assert len(bot._state.pending) == 0


class TestConfirmation:
    @pytest.mark.parametrize("action", [Action.SHUTDOWN, Action.RESTART])
    async def test_destructive_commands_ask_before_publishing(self, action):
        # A fat-fingered shutdown mid-download is exactly what this prevents.
        bot = make_bot()
        set_online(bot)
        update, context = FakeUpdate(), FakeContext()
        await bot._command_from_message(update, context, action)
        assert bot._mqtt.published == []
        assert action.value in update.effective_message.replies[0].lower()

    @pytest.mark.parametrize("action", [Action.SLEEP, Action.LOCK])
    async def test_safe_commands_go_straight_through(self, action, fast_ack):
        bot = make_bot()
        set_online(bot)
        await bot._command_from_message(FakeUpdate(), FakeContext(), action)
        assert bot._mqtt.published == [("desk", action)]

    async def test_the_confirm_button_publishes(self, fast_ack):
        bot = make_bot()
        set_online(bot)
        query = FakeQuery("y|desk|shutdown")
        await bot.on_button(FakeUpdate(query=query), FakeContext())
        assert bot._mqtt.published == [("desk", Action.SHUTDOWN)]

    async def test_the_cancel_button_publishes_nothing(self):
        bot = make_bot()
        set_online(bot)
        query = FakeQuery("n")
        await bot.on_button(FakeUpdate(query=query), FakeContext())
        assert bot._mqtt.published == []
        assert query.edits == ["Cancelled."]

    async def test_the_destructive_button_asks_first(self):
        bot = make_bot()
        set_online(bot)
        query = FakeQuery("a|desk|shutdown")
        await bot.on_button(FakeUpdate(query=query), FakeContext())
        assert bot._mqtt.published == []
        assert "Shutdown desk?" in query.edits[0]


class TestAuthorization:
    async def test_a_callback_from_another_chat_is_ignored(self):
        # The command handlers are filtered by PTB, but callback queries reach
        # the handler directly, so the check is repeated here.
        bot = make_bot()
        set_online(bot)
        query = FakeQuery("y|desk|shutdown")
        await bot.on_button(FakeUpdate(chat_id=OTHER_CHAT, query=query), FakeContext())
        assert bot._mqtt.published == []
        assert query.edits == []

    async def test_the_allowed_chat_gets_through(self, fast_ack):
        bot = make_bot()
        set_online(bot)
        query = FakeQuery("a|desk|sleep")
        await bot.on_button(FakeUpdate(query=query), FakeContext())
        assert bot._mqtt.published == [("desk", Action.SLEEP)]

    async def test_a_callback_for_an_unconfigured_host_is_refused(self):
        bot = make_bot()
        query = FakeQuery("y|intruder|shutdown")
        await bot.on_button(FakeUpdate(query=query), FakeContext())
        assert bot._mqtt.published == []
        assert "no longer configured" in query.edits[0]


class TestStatusRendering:
    @pytest.mark.parametrize("state", list(HostState))
    def test_every_state_renders_without_error(self, state):
        bot = make_bot()
        status = bot._state.get("desk")
        if state is HostState.ONLINE:
            status.apply_presence(Presence.ONLINE)
        elif state is HostState.AGENT_DOWN:
            status.apply_presence(Presence.OFFLINE)
            status.reachable = True
        elif state is HostState.OFFLINE:
            status.apply_presence(Presence.OFFLINE)
            status.reachable = False
        elif state is HostState.WAKING:
            status.begin_wake(90)
        text = bot._status_text(bot._config.default_host)
        assert "desk" in text

    def test_agent_down_explains_itself(self):
        bot = make_bot()
        status = bot._state.get("desk")
        status.apply_presence(Presence.OFFLINE)
        status.reachable = True
        assert "agent is not" in bot._status_text(bot._config.default_host)

    def test_a_dry_run_agent_is_flagged(self):
        # Otherwise a first-time user watches nothing happen and has no idea
        # why the acks all look successful.
        from pcwake.common.protocol import AgentInfo

        bot = make_bot()
        status = bot._state.get("desk")
        status.apply_presence(Presence.ONLINE)
        status.info = AgentInfo(os="Linux", agent_version="1.0.0", dry_run=True)
        assert "dry-run" in bot._status_text(bot._config.default_host)

    def test_offline_hosts_offer_wake_and_online_hosts_offer_power(self):
        bot = make_bot()
        host = bot._config.default_host
        bot._state.get("desk").apply_presence(Presence.OFFLINE)
        offline_buttons = str(bot._keyboard(host))
        assert "w|desk" in offline_buttons and "a|desk|sleep" not in offline_buttons

        bot._state.get("desk").apply_presence(Presence.ONLINE)
        online_buttons = str(bot._keyboard(host))
        assert "a|desk|shutdown" in online_buttons and "w|desk" not in online_buttons

    def test_host_names_are_html_escaped(self):
        # Host names come from config rather than the network, but the status
        # text is HTML and unescaped markup would break the message.
        bot = make_bot(hosts=(HostConfig(name="a-b_c.d", mac="aa:bb:cc:dd:ee:ff"),))
        assert "a-b_c.d" in bot._status_text(bot._config.default_host)


class TestAckWaiterOrdering:
    """A command must have its ack waiter registered *before* it is
    published. Publishing first leaves a window -- the publish awaits the
    broker's PUBACK, freeing the event loop -- in which a fast ack arrives,
    finds no waiter, and is dropped. The user is then told the command timed
    out even though the PC performed it.
    """

    async def test_the_waiter_exists_before_the_command_is_published(self, fast_ack):
        bot = make_bot()
        set_online(bot)
        pending_at_publish: list[int] = []

        real_publish = bot._mqtt.publish_command

        async def spy(*args, **kwargs):
            # However the command is addressed, by the time it goes out
            # somebody must already be waiting for its answer.
            pending_at_publish.append(len(bot._state.pending))
            return await real_publish(*args, **kwargs)

        bot._mqtt.publish_command = spy
        await bot._run_command(bot._config.default_host, Action.SLEEP, Recorder())

        assert pending_at_publish == [1], (
            "the command was published with no ack waiter registered; an ack "
            "arriving before the waiter is created would be dropped and "
            "reported to the user as a timeout"
        )

    async def test_a_failed_publish_leaves_no_stale_waiter(self):
        # Registering first means the failure path has to clean up after
        # itself, or every failed send leaks a waiter.
        bot = make_bot()
        set_online(bot)

        async def boom(*args, **kwargs):
            import aiomqtt

            raise aiomqtt.MqttError("broker went away")

        bot._mqtt.publish_command = boom
        respond = Recorder()
        await bot._run_command(bot._config.default_host, Action.SLEEP, respond)

        assert len(bot._state.pending) == 0
        assert "Broker unreachable" in respond.messages[0]


class TestBackgroundTaskRetention:
    """The event loop keeps only weak references to tasks, so a task nobody
    holds can be garbage collected before it finishes. For the wake watcher
    that means the user sees 'Waking...' forever even though the PC came up.
    """

    async def test_the_wake_watcher_is_held_for_its_lifetime(self, monkeypatch):
        bot = make_bot()
        bot._state.get("desk").apply_presence(Presence.OFFLINE)
        monkeypatch.setattr("pcwake.hub.bot.send_magic_packet", lambda *a, **k: None)

        await bot._wake(bot._config.default_host, Recorder())

        assert bot._spawned, (
            "the wake watcher was spawned with no reference kept; asyncio may "
            "collect it mid-wait and the wake would never be reported"
        )
        for task in list(bot._spawned):
            task.cancel()

    async def test_a_finished_task_is_released(self):
        # The set must not grow without bound over a long uptime.
        bot = make_bot()

        async def noop():
            return None

        task = bot._spawn(noop())
        assert task in bot._spawned
        await task
        await asyncio.sleep(0)
        assert task not in bot._spawned


class TestProbeLoopSurvival:
    """Reachability is half of the status model. A probe loop that dies takes
    the agent-down state with it, silently, and leaves the last value stuck --
    so a powered-off PC would read as 'agent not running' indefinitely."""

    async def test_the_loop_survives_a_failing_probe(self, monkeypatch):
        calls = []

        async def exploding_probe(host):
            calls.append(host.name)
            raise ProcessLookupError("ping vanished between timeout and kill")

        monkeypatch.setattr("pcwake.hub.bot.PROBE_INTERVAL", 0.01)
        bot = make_bot()
        monkeypatch.setattr(bot, "_probe", exploding_probe)

        task = asyncio.create_task(bot._probe_loop())
        await asyncio.sleep(0.1)
        task.cancel()

        assert not task.done() or task.cancelled()
        assert len(calls) > 1, (
            f"the probe loop stopped after {len(calls)} failure(s); it must "
            "keep going or reachability freezes for the process lifetime"
        )

    async def test_a_cancel_still_stops_the_loop(self, monkeypatch):
        # The broad except must not swallow cancellation, or shutdown hangs.
        monkeypatch.setattr("pcwake.hub.bot.PROBE_INTERVAL", 0.01)
        bot = make_bot()
        task = asyncio.create_task(bot._probe_loop())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


class TestProbeSelfCheck:
    """A broken probe loses the agent-down state silently. The startup check
    turns that into a log line naming the usual cause."""

    async def test_warns_when_the_probe_cannot_run(self, monkeypatch, caplog):
        async def no_ping(ip, timeout=None):
            return None

        monkeypatch.setattr("pcwake.hub.bot.is_reachable", no_ping)
        bot = make_bot()
        with caplog.at_level("WARNING"):
            await bot._check_probe_works()
        assert "CAP_NET_RAW" in caplog.text

    async def test_silent_when_the_probe_works(self, monkeypatch, caplog):
        async def works(ip, timeout=None):
            return True

        monkeypatch.setattr("pcwake.hub.bot.is_reachable", works)
        bot = make_bot()
        with caplog.at_level("WARNING"):
            await bot._check_probe_works()
        assert "CAP_NET_RAW" not in caplog.text

    async def test_skipped_when_no_host_has_an_ip(self, monkeypatch, caplog):
        # Nothing to probe by configuration, so the warning would be noise.
        called = []

        async def spy(ip, timeout=None):
            called.append(ip)
            return None

        monkeypatch.setattr("pcwake.hub.bot.is_reachable", spy)
        bot = make_bot(hosts=(HostConfig(name="desk", mac="aa:bb:cc:dd:ee:ff"),))
        await bot._check_probe_works()
        assert called == []

    async def test_a_raising_probe_does_not_stop_startup(self, monkeypatch):
        async def boom(ip, timeout=None):
            raise OSError("no network namespace")

        monkeypatch.setattr("pcwake.hub.bot.is_reachable", boom)
        bot = make_bot()
        await bot._check_probe_works()   # must not raise
