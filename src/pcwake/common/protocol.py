"""Wire protocol shared by the agent (on the PC) and the hub (on the Pi).

Both halves import this module, so the topic names and payload shapes cannot
drift apart. Nothing here talks to a network or a broker -- it is pure
encoding, which is what makes it cheap to test.

Topic layout, one namespace per host:

    pcwake/<host>/status    retained, QoS 1, also the LWT  ->  "online"|"offline"
    pcwake/<host>/info      retained, QoS 1                ->  agent metadata
    pcwake/<host>/cmd       QoS 1                          ->  Command
    pcwake/<host>/ack       QoS 1                          ->  Ack
"""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum

TOPIC_ROOT = "pcwake"

QOS = 1
"""At-least-once everywhere. Commands are idempotent enough to tolerate a
duplicate, and losing one silently is far worse than acting on it twice."""

KEEPALIVE = 30
"""Seconds. The broker declares a client dead at 1.5x this, so a yanked power
cable surfaces as `offline` within about 45 seconds."""

ACK_TIMEOUT = 10.0
"""Seconds the hub waits for an agent's ack before telling the user it heard
nothing back."""

WAKE_TIMEOUT = 90.0
"""Seconds a host may stay in the `waking` state before we call the wake a
failure. Generous: cold boot plus service start plus broker connect."""


class ProtocolError(ValueError):
    """A payload did not conform to the protocol."""


class Action(str, Enum):
    """The four power actions. v1 is deliberately a closed set -- there is no
    arbitrary command execution, and adding one is a protocol change."""

    SLEEP = "sleep"
    SHUTDOWN = "shutdown"
    RESTART = "restart"
    LOCK = "lock"

    @property
    def is_destructive(self) -> bool:
        """Whether the action should require a confirmation tap in the UI."""
        return self in (Action.SHUTDOWN, Action.RESTART)

    @property
    def takes_host_down(self) -> bool:
        """Whether the action ends the agent's connection to the broker.

        These are the actions whose ack must be flushed *before* the action
        runs -- otherwise the machine is gone before the ack leaves the wire
        and every success looks like a timeout.
        """
        return self in (Action.SLEEP, Action.SHUTDOWN, Action.RESTART)


class Presence(str, Enum):
    ONLINE = "online"
    OFFLINE = "offline"


# A host name is used verbatim inside an MQTT topic, so it must not contain
# separators or wildcards.
_HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def validate_host(host: str) -> str:
    """Return `host` unchanged, or raise if it is unsafe to put in a topic."""
    if not isinstance(host, str) or not _HOST_RE.match(host):
        raise ProtocolError(
            f"invalid host name {host!r}: use letters, digits, dot, dash, "
            "underscore (max 64 chars, no '/', '+' or '#')"
        )
    return host


def status_topic(host: str) -> str:
    return f"{TOPIC_ROOT}/{validate_host(host)}/status"


def info_topic(host: str) -> str:
    return f"{TOPIC_ROOT}/{validate_host(host)}/info"


def cmd_topic(host: str) -> str:
    return f"{TOPIC_ROOT}/{validate_host(host)}/cmd"


def ack_topic(host: str) -> str:
    return f"{TOPIC_ROOT}/{validate_host(host)}/ack"


# Wildcards the hub subscribes to so it picks up every configured host at once.
STATUS_WILDCARD = f"{TOPIC_ROOT}/+/status"
ACK_WILDCARD = f"{TOPIC_ROOT}/+/ack"
INFO_WILDCARD = f"{TOPIC_ROOT}/+/info"


def host_from_topic(topic: str) -> str:
    """Pull the host out of a `pcwake/<host>/<leaf>` topic.

    Raises ProtocolError on anything that does not match that shape, so a
    stray publish on a neighbouring topic cannot inject a bogus host into the
    hub's state table.
    """
    parts = topic.split("/")
    if len(parts) != 3 or parts[0] != TOPIC_ROOT:
        raise ProtocolError(f"not a pcwake topic: {topic!r}")
    return validate_host(parts[1])


def new_command_id() -> str:
    """Short correlation id. 8 hex chars is ample to disambiguate the handful
    of commands that can be in flight at once, and stays readable in logs."""
    return uuid.uuid4().hex[:8]


def _require_str(payload: dict, key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        raise ProtocolError(f"field {key!r} must be a string, got {value!r}")
    return value


def _decode_json_object(raw: bytes | str) -> dict:
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProtocolError(f"payload is not utf-8: {exc}") from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"payload is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ProtocolError(f"payload must be a JSON object, got {type(payload).__name__}")
    return payload


@dataclass(frozen=True)
class Command:
    """A power action request travelling hub -> agent."""

    action: Action
    id: str = field(default_factory=new_command_id)
    ts: float = field(default_factory=time.time)

    def encode(self) -> bytes:
        return json.dumps(
            {"id": self.id, "action": self.action.value, "ts": round(self.ts, 3)}
        ).encode("utf-8")

    @classmethod
    def decode(cls, raw: bytes | str) -> Command:
        payload = _decode_json_object(raw)
        action_raw = _require_str(payload, "action")
        try:
            action = Action(action_raw)
        except ValueError as exc:
            known = ", ".join(a.value for a in Action)
            raise ProtocolError(
                f"unknown action {action_raw!r} (known actions: {known})"
            ) from exc
        ts = payload.get("ts", 0.0)
        if not isinstance(ts, (int, float)) or isinstance(ts, bool):
            raise ProtocolError(f"field 'ts' must be a number, got {ts!r}")
        return cls(action=action, id=_require_str(payload, "id"), ts=float(ts))


@dataclass(frozen=True)
class Ack:
    """The agent's reply to a Command, travelling agent -> hub."""

    id: str
    action: Action
    ok: bool
    error: str | None = None

    def encode(self) -> bytes:
        payload: dict[str, object] = {
            "id": self.id,
            "action": self.action.value,
            "ok": self.ok,
        }
        if self.error is not None:
            payload["error"] = self.error
        return json.dumps(payload).encode("utf-8")

    @classmethod
    def decode(cls, raw: bytes | str) -> Ack:
        payload = _decode_json_object(raw)
        action_raw = _require_str(payload, "action")
        try:
            action = Action(action_raw)
        except ValueError as exc:
            raise ProtocolError(f"unknown action {action_raw!r}") from exc
        ok = payload.get("ok")
        if not isinstance(ok, bool):
            raise ProtocolError(f"field 'ok' must be a boolean, got {ok!r}")
        error = payload.get("error")
        if error is not None and not isinstance(error, str):
            raise ProtocolError(f"field 'error' must be a string or absent, got {error!r}")
        return cls(id=_require_str(payload, "id"), action=action, ok=ok, error=error)


@dataclass(frozen=True)
class AgentInfo:
    """Retained metadata the agent publishes on connect. Purely informational
    -- the hub renders it in /status but never makes decisions on it."""

    os: str
    agent_version: str
    booted_at: float | None = None
    dry_run: bool = False

    def encode(self) -> bytes:
        return json.dumps(
            {
                "os": self.os,
                "agent_version": self.agent_version,
                "booted_at": self.booted_at,
                "dry_run": self.dry_run,
            }
        ).encode("utf-8")

    @classmethod
    def decode(cls, raw: bytes | str) -> AgentInfo:
        payload = _decode_json_object(raw)
        booted_at = payload.get("booted_at")
        if booted_at is not None and (
            not isinstance(booted_at, (int, float)) or isinstance(booted_at, bool)
        ):
            raise ProtocolError(f"field 'booted_at' must be a number or null, got {booted_at!r}")
        return cls(
            os=_require_str(payload, "os"),
            agent_version=_require_str(payload, "agent_version"),
            booted_at=float(booted_at) if booted_at is not None else None,
            dry_run=bool(payload.get("dry_run", False)),
        )


def decode_presence(raw: bytes | str) -> Presence:
    """Decode a status payload. Unknown values are treated as offline: the
    safe reading, since we would rather report a live machine as down than
    tell the user a dead one is up."""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    try:
        return Presence(raw.strip().lower())
    except ValueError:
        return Presence.OFFLINE
