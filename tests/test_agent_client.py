"""Agent client behaviour that does not need a broker."""

import logging

from pcwake.agent.client import FLAP_COUNT, FLAP_WINDOW, Agent
from pcwake.agent.power import FakeBackend
from pcwake.common.config import AgentConfig, BrokerConfig


def make_agent(host: str = "desk") -> Agent:
    return Agent(
        AgentConfig(host=host, broker=BrokerConfig(host="127.0.0.1", port=1883)),
        FakeBackend(),
    )


class TestClientIdentity:
    def test_the_client_id_is_derived_from_the_host_name(self):
        assert make_agent("desk").client._client_id == b"pcwake-agent-desk"

    def test_two_hosts_get_different_ids(self):
        # Sharing an id makes two agents kick each other off the broker
        # forever, so this is the property that keeps them apart.
        assert make_agent("desk").client._client_id != make_agent("laptop").client._client_id


class TestFlapDetection:
    """Two agents sharing an [agent].host share an MQTT client id, and the
    broker must disconnect one whenever the other connects. They then kick
    each other round in a loop. The symptom -- a status that flaps and
    commands landing unpredictably -- gives no hint of the cause, so the
    agent names it."""

    def test_a_normal_connection_says_nothing(self, caplog):
        agent = make_agent()
        with caplog.at_level(logging.WARNING):
            agent._note_connection()
        assert "same [agent].host" not in caplog.text

    def test_rapid_reconnects_name_the_likely_cause(self, caplog):
        agent = make_agent()
        with caplog.at_level(logging.WARNING):
            for _ in range(FLAP_COUNT):
                agent._note_connection()
        assert "same [agent].host" in caplog.text
        assert "'desk'" in caplog.text

    def test_it_warns_only_once(self, caplog):
        # The loop is perpetual; repeating the warning every second would
        # bury everything else in the log.
        agent = make_agent()
        with caplog.at_level(logging.WARNING):
            for _ in range(FLAP_COUNT * 3):
                agent._note_connection()
        assert caplog.text.count("same [agent].host") == 1

    def test_reconnects_spread_over_time_are_not_flapping(self, caplog, monkeypatch):
        # A flaky link or a rebooting Pi reconnects too, just not this fast.
        agent = make_agent()
        clock = [1000.0]
        monkeypatch.setattr("pcwake.agent.client.time.monotonic", lambda: clock[0])
        with caplog.at_level(logging.WARNING):
            for _ in range(FLAP_COUNT * 2):
                agent._note_connection()
                clock[0] += FLAP_WINDOW
        assert "same [agent].host" not in caplog.text
