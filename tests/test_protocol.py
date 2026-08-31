"""The protocol is the contract between the two halves, so both the happy
round-trip and the rejection of junk are pinned here."""

import json

import pytest

from pcwake.common.protocol import (
    Ack,
    Action,
    AgentInfo,
    Command,
    Presence,
    ProtocolError,
    ack_topic,
    cmd_topic,
    decode_presence,
    host_from_topic,
    info_topic,
    new_command_id,
    status_topic,
    validate_host,
)


class TestTopics:
    def test_layout(self):
        assert status_topic("desk") == "pcwake/desk/status"
        assert cmd_topic("desk") == "pcwake/desk/cmd"
        assert ack_topic("desk") == "pcwake/desk/ack"
        assert info_topic("desk") == "pcwake/desk/info"

    def test_round_trips_through_host_from_topic(self):
        for build in (status_topic, cmd_topic, ack_topic, info_topic):
            assert host_from_topic(build("my-pc_1.home")) == "my-pc_1.home"

    @pytest.mark.parametrize(
        "bad", ["has/slash", "wild+card", "hash#", "", "-leading-dash", "x" * 65]
    )
    def test_rejects_topic_unsafe_host_names(self, bad):
        # A host name goes verbatim into a topic; a slash or a wildcard here
        # would let one host's name address another host's topics.
        with pytest.raises(ProtocolError):
            validate_host(bad)

    @pytest.mark.parametrize(
        "bad", ["pcwake/desk", "other/desk/status", "pcwake/desk/status/extra", ""]
    )
    def test_host_from_topic_rejects_foreign_topics(self, bad):
        with pytest.raises(ProtocolError):
            host_from_topic(bad)


class TestCommand:
    def test_round_trip(self):
        original = Command(action=Action.SLEEP)
        decoded = Command.decode(original.encode())
        assert decoded.action is Action.SLEEP
        assert decoded.id == original.id
        assert decoded.ts == pytest.approx(original.ts, abs=0.01)

    @pytest.mark.parametrize("action", list(Action))
    def test_every_action_round_trips(self, action):
        assert Command.decode(Command(action=action).encode()).action is action

    def test_ids_are_unique_and_short(self):
        ids = {new_command_id() for _ in range(500)}
        assert len(ids) == 500
        assert all(len(i) == 8 for i in ids)

    @pytest.mark.parametrize(
        "raw",
        [
            b"not json",
            b"[]",
            b'"a string"',
            b"123",
            json.dumps({"id": "abc"}).encode(),
            json.dumps({"action": "sleep"}).encode(),
            json.dumps({"id": "abc", "action": "detonate"}).encode(),
            json.dumps({"id": 7, "action": "sleep"}).encode(),
            json.dumps({"id": "abc", "action": "sleep", "ts": "soon"}).encode(),
            b"\xff\xfe invalid utf-8",
        ],
    )
    def test_rejects_malformed(self, raw):
        with pytest.raises(ProtocolError):
            Command.decode(raw)

    def test_unknown_action_error_names_the_valid_ones(self):
        with pytest.raises(ProtocolError, match="sleep"):
            Command.decode(json.dumps({"id": "a", "action": "rm-rf"}).encode())


class TestAck:
    def test_success_round_trip(self):
        decoded = Ack.decode(Ack(id="abc123", action=Action.LOCK, ok=True).encode())
        assert (decoded.id, decoded.action, decoded.ok, decoded.error) == (
            "abc123",
            Action.LOCK,
            True,
            None,
        )

    def test_failure_carries_the_reason(self):
        decoded = Ack.decode(
            Ack(id="a", action=Action.SLEEP, ok=False, error="no such backend").encode()
        )
        assert decoded.ok is False
        assert decoded.error == "no such backend"

    @pytest.mark.parametrize(
        "raw",
        [
            json.dumps({"id": "a", "action": "sleep"}).encode(),
            json.dumps({"id": "a", "action": "sleep", "ok": "yes"}).encode(),
            json.dumps({"id": "a", "action": "nope", "ok": True}).encode(),
            json.dumps({"id": "a", "action": "sleep", "ok": True, "error": 5}).encode(),
        ],
    )
    def test_rejects_malformed(self, raw):
        with pytest.raises(ProtocolError):
            Ack.decode(raw)


class TestAgentInfo:
    def test_round_trip(self):
        original = AgentInfo(
            os="Windows-11", agent_version="1.0.0", booted_at=1700.0, dry_run=True
        )
        assert AgentInfo.decode(original.encode()) == original

    def test_booted_at_may_be_absent(self):
        decoded = AgentInfo.decode(
            AgentInfo(os="Linux", agent_version="1.0.0").encode()
        )
        assert decoded.booted_at is None
        assert decoded.dry_run is False


class TestPresence:
    def test_decodes_known_values(self):
        assert decode_presence(b"online") is Presence.ONLINE
        assert decode_presence(b"offline") is Presence.OFFLINE
        assert decode_presence("  ONLINE  ") is Presence.ONLINE

    @pytest.mark.parametrize("junk", [b"", b"maybe", b"\xff\xfe", b"1"])
    def test_unknown_values_read_as_offline(self, junk):
        # Reporting a live machine as down is recoverable; telling the user a
        # dead machine is up is not.
        assert decode_presence(junk) is Presence.OFFLINE


class TestActionClassification:
    def test_destructive_actions_need_confirmation(self):
        assert Action.SHUTDOWN.is_destructive
        assert Action.RESTART.is_destructive
        assert not Action.SLEEP.is_destructive
        assert not Action.LOCK.is_destructive

    def test_lock_is_the_only_action_that_keeps_the_host_up(self):
        # This drives the ack-before-action ordering in the agent.
        assert not Action.LOCK.takes_host_down
        assert all(a.takes_host_down for a in Action if a is not Action.LOCK)
