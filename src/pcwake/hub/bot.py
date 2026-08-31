"""The Telegram bot: the entire phone-side user interface.

Telegram carries the app, the push notifications, the account security and
the transport, so what is left here is a thin command surface over MQTT plus
a chat-id allowlist. There is no inbound port and no password to manage.
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import logging
import time

import aiomqtt
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from ..common.config import HostConfig, HubConfig
from ..common.protocol import ACK_TIMEOUT, WAKE_TIMEOUT, Action, Command
from .mqtt import HubMqtt
from .probe import is_reachable
from .state import HostState, HubState
from .wol import send_magic_packet

log = logging.getLogger("pcwake.bot")

PROBE_INTERVAL = 60.0
"""Seconds between background reachability sweeps. Status requests also probe
on demand, so this only keeps the picture warm between them."""

HELP_TEXT = """<b>pcwake</b>

/status - show the PC's state
/wake - send a Wake-on-LAN magic packet
/sleep - suspend to RAM
/lock - lock the screen
/shutdown - power off (asks to confirm)
/restart - reboot (asks to confirm)
/remote - links for remote desktop

Remote desktop streaming is not built in on purpose; /remote hands off to \
RustDesk or Moonlight."""


def _esc(text: object) -> str:
    return html.escape(str(text))


class PcWakeBot:
    def __init__(self, config: HubConfig) -> None:
        self._config = config
        self._state = HubState.for_hosts([h.name for h in config.hosts])
        self._mqtt = HubMqtt(config.broker, self._state, self._on_state_change)
        # Events fired when a host reaches ONLINE, so a wake can wait for the
        # transition instead of polling for it.
        self._online_waiters: dict[str, list[asyncio.Event]] = {
            h.name: [] for h in config.hosts
        }
        self._background: list[asyncio.Task] = []
        # The event loop keeps only weak references to tasks, so a task
        # nobody else holds can be garbage collected before it finishes.
        # Anything spawned to outlive its handler goes in here.
        self._spawned: set[asyncio.Task] = set()

    def _spawn(self, coro, name: str | None = None) -> asyncio.Task:
        """Start a background task and keep it alive until it finishes."""
        task = asyncio.create_task(coro, name=name)
        self._spawned.add(task)
        task.add_done_callback(self._spawned.discard)
        return task

    # ---------------------------------------------------------------- wiring

    def build(self) -> Application:
        app = (
            ApplicationBuilder()
            .token(self._config.telegram_token)
            .post_init(self._start_background)
            .post_shutdown(self._stop_background)
            .build()
        )

        # The allowlist is the whole authorization story: PTB drops anything
        # from another chat before it reaches a handler.
        allowed = filters.Chat(chat_id=list(self._config.allowed_chat_ids))

        commands = {
            "start": self.cmd_help,
            "help": self.cmd_help,
            "status": self.cmd_status,
            "wake": self.cmd_wake,
            "sleep": self.cmd_sleep,
            "lock": self.cmd_lock,
            "shutdown": self.cmd_shutdown,
            "restart": self.cmd_restart,
            "remote": self.cmd_remote,
        }
        for name, handler in commands.items():
            app.add_handler(CommandHandler(name, handler, filters=allowed))
        app.add_handler(CallbackQueryHandler(self.on_button))

        # Anything from a chat that is not allowlisted lands here. Logging the
        # id is how a new user discovers the number to put in the config; we
        # stay silent to the sender so the bot does not confirm it exists.
        app.add_handler(MessageHandler(~allowed, self.on_unauthorized))
        app.add_error_handler(self.on_error)
        return app

    async def _start_background(self, app: Application) -> None:
        await self._check_probe_works()
        self._background = [
            asyncio.create_task(self._mqtt.run(), name="pcwake-mqtt"),
            asyncio.create_task(self._probe_loop(), name="pcwake-probe"),
        ]
        log.info(
            "bot started for %d host(s): %s",
            len(self._config.hosts),
            ", ".join(h.name for h in self._config.hosts),
        )

    async def _stop_background(self, app: Application) -> None:
        for task in [*self._background, *self._spawned]:
            task.cancel()
        for task in [*self._background, *self._spawned]:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._background = []

    async def on_error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        log.error("error while handling %s", update, exc_info=context.error)

    async def on_unauthorized(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        chat = update.effective_chat
        user = update.effective_user
        log.warning(
            "ignored message from unauthorized chat_id=%s (user=%s). Add it to "
            "telegram.allowed_chat_ids if this is you.",
            chat.id if chat else "?",
            user.username if user else "?",
        )

    # ------------------------------------------------------------ background

    async def _check_probe_works(self) -> None:
        """Ping ourselves once at startup, and say so loudly if it fails.

        Without a working probe the hub silently loses the "powered on, agent
        not running" state -- it just never appears, which looks like nothing
        is wrong. Sandboxing is the usual cause: ping carries a file
        capability, and those are disabled under NoNewPrivileges, so the
        service needs AmbientCapabilities=CAP_NET_RAW (the shipped unit sets
        it). Better to name that here than leave someone wondering why a
        crashed agent always reads as a powered-off PC.
        """
        if not any(host.ip for host in self._config.hosts):
            return
        try:
            result = await is_reachable("127.0.0.1")
        except Exception:  # noqa: BLE001 - a broken self-check must not stop startup
            log.exception("reachability self-check raised")
            result = None
        if result is not True:
            log.warning(
                "reachability probe is not working (ping to 127.0.0.1 returned "
                "%r). Status will still report online and offline, but never "
                "'powered on, agent not running'. If the hub runs under "
                "systemd, check AmbientCapabilities=CAP_NET_RAW is set on the "
                "unit.",
                result,
            )
        else:
            log.info("reachability probe working")

    async def _on_state_change(
        self, host_name: str, before: HostState, after: HostState
    ) -> None:
        if after is HostState.ONLINE:
            for event in self._online_waiters.get(host_name, []):
                event.set()

    async def _probe_loop(self) -> None:
        """Keep every host's reachability fresh, and never stop doing it.

        An unguarded loop that dies takes the whole agent-down state with it,
        silently and for the lifetime of the process -- and worse than losing
        the feature, the last reachability value sticks, so a stale True would
        report a powered-off PC as "agent not running" indefinitely.
        """
        while True:
            for host in self._config.hosts:
                try:
                    await self._probe(host)
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - the loop must outlive any single probe
                    log.exception("probe for %s failed", host.name)
            await asyncio.sleep(PROBE_INTERVAL)

    async def _probe(self, host: HostConfig) -> None:
        """Refresh one host's reachability. Without an IP we simply never
        learn it, and the agent-down state is never reported -- which is
        better than guessing."""
        if host.ip is None:
            return
        status = self._state.get(host.name)
        if status is None:
            return
        status.reachable = await is_reachable(host.ip)
        status.reachable_at = time.time()

    # ------------------------------------------------------------- rendering

    def _resolve_host(self, args: list[str] | None) -> HostConfig | None:
        """With one configured PC no argument is needed; with several the
        first argument selects one."""
        if not args:
            return self._config.default_host
        return self._config.host_by_name(args[0])

    def _status_text(self, host: HostConfig) -> str:
        status = self._state.get(host.name)
        assert status is not None
        state = status.resolve()

        lines = [f"<b>{_esc(host.name)}</b>  {state.describe()}"]

        if state is HostState.AGENT_DOWN:
            lines.append(
                "<i>The PC answers on the network but the agent is not "
                "connected. Power commands will not work until it is.</i>"
            )
        elif state is HostState.UNKNOWN:
            lines.append(
                "<i>No status retained on the broker and no IP configured to "
                "probe. The agent has probably never connected.</i>"
            )

        if status.info is not None and state in (HostState.ONLINE, HostState.WAKING):
            lines.append(f"OS: {_esc(status.info.os)}")
            lines.append(f"Agent: {_esc(status.info.agent_version)}")
            if status.info.dry_run:
                lines.append(
                    "\N{WARNING SIGN} agent is in <b>dry-run</b>: it will log "
                    "power commands, not perform them"
                )

        if status.presence_since is not None and state is not HostState.WAKING:
            lines.append(f"Since: {_esc(_ago(status.presence_since))}")

        if not self._mqtt.connected:
            lines.append("\N{WARNING SIGN} <b>not connected to the broker</b>")

        return "\n".join(lines)

    def _keyboard(self, host: HostConfig) -> InlineKeyboardMarkup:
        """Tapping a button beats typing a slash command on a phone, and the
        callbacks route into exactly the same code as the text commands."""
        status = self._state.get(host.name)
        assert status is not None
        state = status.resolve()
        name = host.name

        rows: list[list[InlineKeyboardButton]] = []
        if state.accepts_commands:
            rows.append(
                [
                    InlineKeyboardButton("\N{CRESCENT MOON} Sleep", callback_data=f"a|{name}|sleep"),
                    InlineKeyboardButton("\N{LOCK} Lock", callback_data=f"a|{name}|lock"),
                ]
            )
            rows.append(
                [
                    InlineKeyboardButton(
                        "\N{ANTICLOCKWISE OPEN CIRCLE ARROW} Restart",
                        callback_data=f"a|{name}|restart",
                    ),
                    InlineKeyboardButton(
                        "\N{ELECTRIC PLUG} Shut down", callback_data=f"a|{name}|shutdown"
                    ),
                ]
            )
        else:
            rows.append(
                [InlineKeyboardButton("\N{HIGH VOLTAGE SIGN} Wake", callback_data=f"w|{name}")]
            )
        rows.append(
            [InlineKeyboardButton("\N{CLOCKWISE OPEN CIRCLE ARROW} Refresh", callback_data=f"s|{name}")]
        )
        return InlineKeyboardMarkup(rows)

    # -------------------------------------------------------------- commands

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.effective_message.reply_text(HELP_TEXT, parse_mode=ParseMode.HTML)

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        host = self._resolve_host(context.args)
        if host is None:
            await self._reply_unknown_host(update, context)
            return
        await self._refresh_if_not_online(host)
        await update.effective_message.reply_text(
            self._status_text(host),
            parse_mode=ParseMode.HTML,
            reply_markup=self._keyboard(host),
        )

    async def cmd_wake(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        host = self._resolve_host(context.args)
        if host is None:
            await self._reply_unknown_host(update, context)
            return
        await self._wake(host, update.effective_message.reply_text)

    async def cmd_sleep(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._command_from_message(update, context, Action.SLEEP)

    async def cmd_lock(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._command_from_message(update, context, Action.LOCK)

    async def cmd_shutdown(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._command_from_message(update, context, Action.SHUTDOWN)

    async def cmd_restart(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._command_from_message(update, context, Action.RESTART)

    async def cmd_remote(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        host = self._resolve_host(context.args)
        if host is None:
            await self._reply_unknown_host(update, context)
            return
        lines = [
            f"<b>Remote desktop for {_esc(host.name)}</b>",
            "",
            "Streaming is deliberately not part of this bot. These open a "
            "tool that does it properly:",
            "",
        ]
        if host.rustdesk_id:
            lines.append(f"RustDesk: <code>rustdesk://connect?id={_esc(host.rustdesk_id)}</code>")
        if host.moonlight_host:
            lines.append(f"Moonlight: <code>moonlight://{_esc(host.moonlight_host)}</code>")
        if not host.rustdesk_id and not host.moonlight_host:
            lines.append(
                "<i>Nothing configured. Set rustdesk_id or moonlight_host for "
                "this host in the hub config.</i>"
            )
        await update.effective_message.reply_text(
            "\n".join(lines), parse_mode=ParseMode.HTML
        )

    async def _reply_unknown_host(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        known = ", ".join(h.name for h in self._config.hosts)
        await update.effective_message.reply_text(
            f"Unknown host. Configured hosts: {known}"
        )

    # ---------------------------------------------------------------- buttons

    async def on_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        chat = update.effective_chat
        if chat is None or chat.id not in self._config.allowed_chat_ids:
            log.warning("ignored callback from unauthorized chat_id=%s", chat.id if chat else "?")
            await query.answer()
            return

        await query.answer()
        parts = (query.data or "").split("|")
        kind = parts[0] if parts else ""

        if kind == "n":
            await query.edit_message_text("Cancelled.")
            return

        if len(parts) < 2:
            return
        host = self._config.host_by_name(parts[1])
        if host is None:
            await query.edit_message_text("That host is no longer configured.")
            return

        if kind == "s":
            await self._refresh_if_not_online(host)
            await self._edit_status(query, host)
        elif kind == "w":
            await self._wake(host, query.edit_message_text)
        elif kind == "a" and len(parts) == 3:
            action = Action(parts[2])
            if action.is_destructive:
                await self._ask_confirm(query, host, action)
            else:
                await self._run_command(host, action, query.edit_message_text)
        elif kind == "y" and len(parts) == 3:
            await self._run_command(host, Action(parts[2]), query.edit_message_text)

    async def _edit_status(self, query, host: HostConfig) -> None:
        # Telegram rejects an edit that changes nothing; a refresh that finds
        # no change is not an error worth showing anyone.
        with contextlib.suppress(TelegramError):
            await query.edit_message_text(
                self._status_text(host),
                parse_mode=ParseMode.HTML,
                reply_markup=self._keyboard(host),
            )

    async def _ask_confirm(self, query, host: HostConfig, action: Action) -> None:
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        f"Yes, {action.value}", callback_data=f"y|{host.name}|{action.value}"
                    ),
                    InlineKeyboardButton("Cancel", callback_data="n"),
                ]
            ]
        )
        await query.edit_message_text(
            f"<b>{_esc(action.value.capitalize())} {_esc(host.name)}?</b>\n"
            "This will interrupt anything running on it.",
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )

    # --------------------------------------------------------------- actions

    async def _command_from_message(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, action: Action
    ) -> None:
        host = self._resolve_host(context.args)
        if host is None:
            await self._reply_unknown_host(update, context)
            return
        if action.is_destructive:
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            f"Yes, {action.value}",
                            callback_data=f"y|{host.name}|{action.value}",
                        ),
                        InlineKeyboardButton("Cancel", callback_data="n"),
                    ]
                ]
            )
            await update.effective_message.reply_text(
                f"<b>{_esc(action.value.capitalize())} {_esc(host.name)}?</b>\n"
                "This will interrupt anything running on it.",
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
            )
            return
        await self._run_command(host, action, update.effective_message.reply_text)

    async def _run_command(self, host: HostConfig, action: Action, respond) -> None:
        """Publish a command and report what actually happened.

        Every path here ends in a visible outcome -- success, refusal, or
        timeout. A command that quietly does nothing is the one failure mode
        that would make the whole bot untrustworthy.
        """
        status = self._state.get(host.name)
        assert status is not None
        state = status.resolve()
        if not state.accepts_commands:
            await respond(
                f"Can't {action.value} <b>{_esc(host.name)}</b>: it is "
                f"{state.describe()}.",
                parse_mode=ParseMode.HTML,
            )
            return

        # Register the waiter before the command can possibly be answered.
        # Publishing awaits the broker's PUBACK, which frees the event loop
        # long enough for a fast agent to ack; an ack with no waiter is
        # dropped, and the user would be told a command timed out that the PC
        # actually performed.
        command = Command(action=action)
        try:
            future = self._state.pending.register(command.id)
        except RuntimeError:
            # A colliding in-flight id. Vanishingly unlikely, but the user
            # must never be left with no reply at all.
            await respond(
                "That command is already in flight; try again in a moment.",
                parse_mode=ParseMode.HTML,
            )
            return

        try:
            await self._mqtt.publish_command(host.name, command)
        except aiomqtt.MqttError as exc:
            self._state.pending.discard(command.id)
            await respond(f"Broker unreachable, command not sent: {_esc(exc)}",
                          parse_mode=ParseMode.HTML)
            return

        try:
            ack = await asyncio.wait_for(future, timeout=ACK_TIMEOUT)
        except asyncio.TimeoutError:
            self._state.pending.discard(command.id)
            await respond(
                f"\N{WARNING SIGN} Sent <b>{action.value}</b> to "
                f"{_esc(host.name)} but heard nothing back in "
                f"{ACK_TIMEOUT:.0f}s. It may still have worked \N{EM DASH} "
                "check /status.",
                parse_mode=ParseMode.HTML,
            )
            return

        if ack.ok:
            await respond(
                f"\N{WHITE HEAVY CHECK MARK} <b>{_esc(host.name)}</b>: "
                f"{_esc(action.value)} acknowledged.",
                parse_mode=ParseMode.HTML,
            )
        else:
            await respond(
                f"\N{CROSS MARK} <b>{_esc(host.name)}</b> refused "
                f"{_esc(action.value)}: {_esc(ack.error or 'no reason given')}",
                parse_mode=ParseMode.HTML,
            )

    async def _wake(self, host: HostConfig, respond) -> None:
        status = self._state.get(host.name)
        assert status is not None
        if status.resolve() is HostState.ONLINE:
            await respond(
                f"<b>{_esc(host.name)}</b> is already online.",
                parse_mode=ParseMode.HTML,
            )
            return

        try:
            send_magic_packet(host.mac, host.broadcast)
        except (OSError, ValueError) as exc:
            await respond(f"Could not send the magic packet: {_esc(exc)}",
                          parse_mode=ParseMode.HTML)
            return

        status.begin_wake(WAKE_TIMEOUT)
        message = await respond(
            f"{HostState.WAKING.describe()} <b>{_esc(host.name)}</b>\n"
            f"<i>Magic packet sent. Waiting up to {WAKE_TIMEOUT:.0f}s for the "
            "agent to connect.</i>",
            parse_mode=ParseMode.HTML,
        )
        # Watch in the background so the handler returns immediately and the
        # bot stays responsive during the boot. Spawned through _spawn so the
        # watcher cannot be collected mid-wait, which would leave the user
        # looking at "Waking..." forever even though the PC came up.
        self._spawn(self._watch_wake(host, message), name=f"pcwake-wake-{host.name}")

    async def _watch_wake(self, host: HostConfig, message) -> None:
        """Wait for the woken host to appear, then say so in the same message."""
        status = self._state.get(host.name)
        assert status is not None
        event = asyncio.Event()
        self._online_waiters[host.name].append(event)
        try:
            # The host may already have come online while the "Waking..."
            # message was being sent -- that reply is a network round trip to
            # Telegram, and the state change fires with nobody registered.
            # Checking here closes that window; without it a wake that
            # succeeded would sit until the timeout and report as failed.
            if status.resolve() is HostState.ONLINE:
                came_up = True
            else:
                await asyncio.wait_for(event.wait(), timeout=WAKE_TIMEOUT)
                came_up = True
        except asyncio.TimeoutError:
            came_up = False
        finally:
            with contextlib.suppress(ValueError):
                self._online_waiters[host.name].remove(event)
            status.clear_wake()

        if came_up:
            text = self._status_text(host)
        else:
            await self._probe(host)
            text = (
                f"\N{WARNING SIGN} <b>{_esc(host.name)}</b> did not come online "
                f"within {WAKE_TIMEOUT:.0f}s.\n\n{self._status_text(host)}\n\n"
                "<i>If it did power on, the agent may not be running. If it "
                "did not, check BIOS Wake-on-LAN and (on Windows) that Fast "
                "Startup is off.</i>"
            )
        # edit_message_text returns True rather than a Message when the
        # original was an inline message. We never send those, but a watcher
        # that raised here would swallow the wake result silently.
        if not hasattr(message, "edit_text"):
            log.info("%s finished waking; no editable message to update", host.name)
            return
        with contextlib.suppress(TelegramError):
            await message.edit_text(
                text, parse_mode=ParseMode.HTML, reply_markup=self._keyboard(host)
            )

    async def _refresh_if_not_online(self, host: HostConfig) -> None:
        """Probe on demand only when it can change the answer. A live agent
        already tells us everything, so we skip the latency in that case."""
        status = self._state.get(host.name)
        if status is not None and status.resolve() is not HostState.ONLINE:
            await self._probe(host)


def _ago(timestamp: float) -> str:
    seconds = max(0, int(time.time() - timestamp))
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h {(seconds % 3600) // 60}m ago"
    return f"{seconds // 86400}d ago"
