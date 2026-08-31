"""Config validation exists to fail loudly at startup rather than quietly at
3am, so the failure cases are the interesting tests."""

import pytest

from pcwake.common.config import (
    ConfigError,
    default_host_name,
    load_agent_config,
    load_hub_config,
)

MINIMAL_HUB = """
[broker]
host = "127.0.0.1"

[telegram]
token = "123:abc"
allowed_chat_ids = [4242]

[[hosts]]
name = "desk"
mac = "aa:bb:cc:dd:ee:ff"
"""


@pytest.fixture
def write_config(tmp_path):
    def _write(text: str):
        path = tmp_path / "config.toml"
        path.write_text(text)
        return path

    return _write


class TestHubConfig:
    def test_minimal_config_loads(self, write_config):
        config = load_hub_config(write_config(MINIMAL_HUB))
        assert config.telegram_token == "123:abc"
        assert config.allowed_chat_ids == (4242,)
        assert config.default_host.name == "desk"
        assert config.default_host.broadcast == "255.255.255.255"

    def test_empty_allowlist_is_refused(self, write_config):
        # An empty allowlist would let anyone who finds the bot power off the
        # PC. Defaulting to "allow all" here would be the single worst bug in
        # the project, so it is a hard error.
        text = MINIMAL_HUB.replace("allowed_chat_ids = [4242]", "allowed_chat_ids = []")
        with pytest.raises(ConfigError, match="anyone"):
            load_hub_config(write_config(text))

    def test_missing_token_is_refused(self, write_config, monkeypatch):
        monkeypatch.delenv("PCWAKE_TELEGRAM_TOKEN", raising=False)
        text = MINIMAL_HUB.replace('token = "123:abc"', "")
        with pytest.raises(ConfigError, match="token"):
            load_hub_config(write_config(text))

    def test_token_can_come_from_the_environment(self, write_config, monkeypatch):
        # So a systemd unit need never write the secret to disk.
        monkeypatch.setenv("PCWAKE_TELEGRAM_TOKEN", "from-env")
        text = MINIMAL_HUB.replace('token = "123:abc"', "")
        assert load_hub_config(write_config(text)).telegram_token == "from-env"

    def test_environment_token_wins_over_the_file(self, write_config, monkeypatch):
        monkeypatch.setenv("PCWAKE_TELEGRAM_TOKEN", "from-env")
        assert load_hub_config(write_config(MINIMAL_HUB)).telegram_token == "from-env"

    def test_broker_password_can_come_from_the_environment(
        self, write_config, monkeypatch
    ):
        monkeypatch.setenv("PCWAKE_BROKER_PASSWORD", "s3cret")
        assert load_hub_config(write_config(MINIMAL_HUB)).broker.password == "s3cret"

    def test_host_without_a_mac_is_refused(self, write_config):
        text = MINIMAL_HUB.replace('mac = "aa:bb:cc:dd:ee:ff"', "")
        with pytest.raises(ConfigError, match="mac"):
            load_hub_config(write_config(text))

    def test_no_hosts_is_refused(self, write_config):
        text = MINIMAL_HUB.split("[[hosts]]")[0]
        with pytest.raises(ConfigError, match="hosts"):
            load_hub_config(write_config(text))

    def test_duplicate_host_names_are_refused(self, write_config):
        text = MINIMAL_HUB + '\n[[hosts]]\nname = "desk"\nmac = "11:22:33:44:55:66"\n'
        with pytest.raises(ConfigError, match="duplicate"):
            load_hub_config(write_config(text))

    def test_topic_unsafe_host_name_is_refused(self, write_config):
        text = MINIMAL_HUB.replace('name = "desk"', 'name = "desk/evil"')
        with pytest.raises(ConfigError):
            load_hub_config(write_config(text))

    def test_non_integer_chat_ids_are_refused(self, write_config):
        text = MINIMAL_HUB.replace("[4242]", '["4242"]')
        with pytest.raises(ConfigError, match="integers"):
            load_hub_config(write_config(text))

    def test_bad_port_is_refused(self, write_config):
        text = MINIMAL_HUB.replace('host = "127.0.0.1"', "port = 99999")
        with pytest.raises(ConfigError, match="port"):
            load_hub_config(write_config(text))

    def test_host_lookup_by_name(self, write_config):
        config = load_hub_config(write_config(MINIMAL_HUB))
        assert config.host_by_name("desk") is not None
        assert config.host_by_name("nope") is None

    def test_malformed_toml_names_the_file(self, write_config):
        with pytest.raises(ConfigError, match="not valid TOML"):
            load_hub_config(write_config("[broker\n"))

    def test_missing_file_is_reported(self, tmp_path):
        with pytest.raises(ConfigError, match="not found"):
            load_hub_config(tmp_path / "absent.toml")


class TestAgentConfig:
    def test_defaults_to_this_machines_hostname(self, write_config):
        config = load_agent_config(write_config("[agent]\n"))
        assert config.host == default_host_name()
        assert config.dry_run is False
        assert config.force_shutdown is False

    def test_explicit_settings_load(self, write_config):
        config = load_agent_config(
            write_config(
                '[agent]\nhost = "desk"\ndry_run = true\nforce_shutdown = true\n'
                '[broker]\nhost = "pi.local"\nport = 8883\nusername = "agent"\n'
            )
        )
        assert (config.host, config.dry_run, config.force_shutdown) == ("desk", True, True)
        assert (config.broker.host, config.broker.port) == ("pi.local", 8883)
        assert config.broker.username == "agent"

    def test_topic_unsafe_host_is_refused(self, write_config):
        with pytest.raises(ConfigError):
            load_agent_config(write_config('[agent]\nhost = "a/b"\n'))


class TestDefaultHostName:
    def test_is_topic_safe(self, monkeypatch):
        monkeypatch.setattr("socket.gethostname", lambda: "My PC.local")
        name = default_host_name()
        assert "/" not in name and " " not in name
        assert name == "my-pc"
