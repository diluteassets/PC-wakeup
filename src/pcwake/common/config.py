"""Configuration loading for both roles.

TOML file plus environment overrides. Secrets (the bot token, the broker
password) can come from the environment so they need never be written to disk
in a systemd unit or a container.
"""

from __future__ import annotations

import os
import socket
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .protocol import ProtocolError, validate_host

ENV_PREFIX = "PCWAKE_"

DEFAULT_CONFIG_PATHS = (
    Path("config.toml"),
    Path("/etc/pcwake/config.toml"),
    Path.home() / ".config" / "pcwake" / "config.toml",
)


class ConfigError(ValueError):
    """The configuration is missing or malformed."""


def default_host_name() -> str:
    """This machine's short hostname, normalised into something topic-safe."""
    raw = socket.gethostname().split(".")[0].lower()
    cleaned = "".join(ch if (ch.isalnum() or ch in "._-") else "-" for ch in raw)
    return cleaned or "pc"


@dataclass(frozen=True)
class BrokerConfig:
    host: str = "127.0.0.1"
    port: int = 1883
    username: str | None = None
    password: str | None = None
    keepalive: int | None = None


@dataclass(frozen=True)
class HostConfig:
    """A PC the hub can wake and control."""

    name: str
    mac: str
    # Used only by the reachability probe, to tell "powered on but the agent
    # is dead" apart from "powered off". Optional: without it the hub simply
    # never reports that intermediate state.
    ip: str | None = None
    # Where to send the magic packet. The global broadcast address does not
    # cross a router, so a PC on a different subnet needs that subnet's
    # directed broadcast address here (e.g. "192.168.1.255").
    broadcast: str = "255.255.255.255"
    # Optional deep links surfaced by /remote. Remote desktop is out of scope;
    # we only hand off to a tool that already does it.
    rustdesk_id: str | None = None
    moonlight_host: str | None = None


@dataclass(frozen=True)
class AgentConfig:
    host: str = field(default_factory=default_host_name)
    broker: BrokerConfig = field(default_factory=BrokerConfig)
    dry_run: bool = False
    # Windows only: append /f to shutdown.exe, forcing apps closed rather than
    # letting one unsaved document veto the shutdown. Off by default -- losing
    # work is worse than a shutdown that did not happen.
    force_shutdown: bool = False

    def __post_init__(self) -> None:
        validate_host(self.host)


@dataclass(frozen=True)
class HubConfig:
    telegram_token: str
    allowed_chat_ids: tuple[int, ...]
    hosts: tuple[HostConfig, ...]
    broker: BrokerConfig = field(default_factory=BrokerConfig)

    def host_by_name(self, name: str) -> HostConfig | None:
        return next((h for h in self.hosts if h.name == name), None)

    @property
    def default_host(self) -> HostConfig:
        """With a single configured PC -- the overwhelmingly common case --
        commands need no host argument."""
        return self.hosts[0]


def _find_config(explicit: str | os.PathLike[str] | None) -> Path:
    if explicit is not None:
        path = Path(explicit)
        if not path.is_file():
            raise ConfigError(f"config file not found: {path}")
        return path
    env_path = os.environ.get(f"{ENV_PREFIX}CONFIG")
    if env_path:
        path = Path(env_path)
        if not path.is_file():
            raise ConfigError(f"config file not found (from {ENV_PREFIX}CONFIG): {path}")
        return path
    for candidate in DEFAULT_CONFIG_PATHS:
        if candidate.is_file():
            return candidate
    searched = ", ".join(str(p) for p in DEFAULT_CONFIG_PATHS)
    raise ConfigError(
        f"no config file found. Copy config.example.toml to one of: {searched} "
        f"or set {ENV_PREFIX}CONFIG."
    )


def _load_toml(path: Path) -> dict:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc


def _broker_from(raw: dict, *, default_keepalive: int | None = None) -> BrokerConfig:
    section = raw.get("broker", {})
    if not isinstance(section, dict):
        raise ConfigError("[broker] must be a table")
    port = section.get("port", 1883)
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise ConfigError(f"broker.port must be a port number, got {port!r}")
    return BrokerConfig(
        host=str(section.get("host", "127.0.0.1")),
        port=port,
        username=_str_or_none(section.get("username"))
        or os.environ.get(f"{ENV_PREFIX}BROKER_USERNAME"),
        password=_str_or_none(section.get("password"))
        or os.environ.get(f"{ENV_PREFIX}BROKER_PASSWORD"),
        keepalive=section.get("keepalive", default_keepalive),
    )


def _str_or_none(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigError(f"expected a string, got {value!r}")
    return value or None


def load_agent_config(path: str | os.PathLike[str] | None = None) -> AgentConfig:
    raw = _load_toml(_find_config(path))
    agent = raw.get("agent", {})
    if not isinstance(agent, dict):
        raise ConfigError("[agent] must be a table")
    host = agent.get("host") or default_host_name()
    try:
        validate_host(host)
    except ProtocolError as exc:
        raise ConfigError(f"agent.host: {exc}") from exc
    return AgentConfig(
        host=host,
        broker=_broker_from(raw),
        dry_run=bool(agent.get("dry_run", False)),
        force_shutdown=bool(agent.get("force_shutdown", False)),
    )


def load_hub_config(path: str | os.PathLike[str] | None = None) -> HubConfig:
    config_path = _find_config(path)
    raw = _load_toml(config_path)

    telegram = raw.get("telegram", {})
    if not isinstance(telegram, dict):
        raise ConfigError("[telegram] must be a table")
    token = os.environ.get(f"{ENV_PREFIX}TELEGRAM_TOKEN") or _str_or_none(
        telegram.get("token")
    )
    if not token:
        raise ConfigError(
            "no Telegram bot token: set telegram.token in the config file or "
            f"{ENV_PREFIX}TELEGRAM_TOKEN in the environment"
        )

    chat_ids = telegram.get("allowed_chat_ids", [])
    if not isinstance(chat_ids, list) or not all(
        isinstance(cid, int) and not isinstance(cid, bool) for cid in chat_ids
    ):
        raise ConfigError("telegram.allowed_chat_ids must be a list of integers")
    if not chat_ids:
        raise ConfigError(
            "telegram.allowed_chat_ids is empty, which would let anyone who "
            "finds the bot control your PC. Send /start to the bot and read "
            "the chat id from the log, then add it here."
        )

    hosts_raw = raw.get("hosts", [])
    if not isinstance(hosts_raw, list) or not hosts_raw:
        raise ConfigError("at least one [[hosts]] entry is required")
    hosts = tuple(_host_from(entry, index) for index, entry in enumerate(hosts_raw))
    names = [h.name for h in hosts]
    duplicates = {n for n in names if names.count(n) > 1}
    if duplicates:
        raise ConfigError(f"duplicate host names in [[hosts]]: {', '.join(sorted(duplicates))}")

    return HubConfig(
        telegram_token=token,
        allowed_chat_ids=tuple(chat_ids),
        hosts=hosts,
        broker=_broker_from(raw),
    )


def _host_from(entry: object, index: int) -> HostConfig:
    if not isinstance(entry, dict):
        raise ConfigError(f"[[hosts]] entry {index} must be a table")
    name = entry.get("name")
    if not isinstance(name, str) or not name:
        raise ConfigError(f"[[hosts]] entry {index} is missing 'name'")
    try:
        validate_host(name)
    except ProtocolError as exc:
        raise ConfigError(f"[[hosts]] {name!r}: {exc}") from exc
    mac = entry.get("mac")
    if not isinstance(mac, str) or not mac:
        raise ConfigError(f"[[hosts]] {name!r} is missing 'mac' (needed to wake it)")
    return HostConfig(
        name=name,
        mac=mac,
        ip=_str_or_none(entry.get("ip")),
        broadcast=str(entry.get("broadcast", "255.255.255.255")),
        rustdesk_id=_str_or_none(entry.get("rustdesk_id")),
        moonlight_host=_str_or_none(entry.get("moonlight_host")),
    )
