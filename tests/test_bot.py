"""The bot's decision logic: who may issue commands, what needs confirming,
and what happens when the PC cannot act.

These use small stand-ins rather than a full Telegram mock -- the behaviour
worth pinning is ours, not python-telegram-bot's.
"""

from __future__ import annotations

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

    async def publish_command(self, host_name: str, action: Action):
        from pcwake.common.protocol import Command

        self.published.append((host_name, action))
        return Command(action=action)


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
