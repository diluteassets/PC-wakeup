"""The command-line entry points.

Nothing here exercises much logic, but a broken entry point is as
user-facing as a bug gets -- it is the first thing anyone runs, and the
failure is total. These are smoke tests: does it start, does it refuse
sensibly, does it return a shell exit code worth branching on.
"""

from unittest import mock

import pytest

from pcwake.agent import __main__ as agent_main
from pcwake.hub import __main__ as hub_main

GOOD_AGENT = '[agent]\nhost = "desk"\ndry_run = true\n[broker]\nhost = "127.0.0.1"\n'
GOOD_HUB = """
[telegram]
token = "123:abc"
allowed_chat_ids = [1]
[[hosts]]
name = "desk"
mac = "aa:bb:cc:dd:ee:ff"
"""


@pytest.fixture
def config_file(tmp_path):
    def _write(text: str) -> str:
        path = tmp_path / "config.toml"
        path.write_text(text)
        return str(path)

    return _write


class TestAgentEntryPoint:
    def test_a_missing_config_exits_two_rather_than_traceback(self, tmp_path):
        # Exit 2 is "you configured it wrong", distinct from a crash.
        assert agent_main.main(["-c", str(tmp_path / "nope.toml")]) == 2

    def test_a_malformed_config_exits_two(self, config_file):
        assert agent_main.main(["-c", config_file("[broker\n")]) == 2

    def test_doctor_runs_and_returns_its_own_verdict(self, config_file):
        # The broker is unreachable here, so doctor must report a failure.
        assert agent_main.main(["-c", config_file(GOOD_AGENT), "doctor"]) == 1

    def test_run_starts_the_agent(self, config_file):
        with mock.patch("pcwake.agent.client.Agent.run") as run:
            assert agent_main.main(["-c", config_file(GOOD_AGENT)]) == 0
        run.assert_called_once()

    def test_dry_run_overrides_the_config(self, config_file):
        # The flag has to win, or someone testing carefully would still
        # suspend a machine they did not mean to.
        captured = {}

        def capture(config, backend, **kwargs):
            captured["backend"] = backend.name
            return mock.Mock(run=mock.Mock(), stop=mock.Mock())

        text = GOOD_AGENT.replace("dry_run = true", "dry_run = false")
        with mock.patch("pcwake.agent.client.Agent", side_effect=capture):
            assert agent_main.main(["-c", config_file(text), "--dry-run"]) == 0
        assert captured["backend"] == "dry-run"

    def test_an_unsupported_platform_refuses_to_start(self, config_file):
        text = GOOD_AGENT.replace("dry_run = true", "dry_run = false")
        with mock.patch("platform.system", return_value="Plan9"):
            assert agent_main.main(["-c", config_file(text)]) == 2

    def test_force_shutdown_reaches_the_windows_backend(self, config_file):
        captured = {}

        def capture(config, backend, **kwargs):
            captured["force"] = getattr(backend, "_force", None)
            return mock.Mock(run=mock.Mock(), stop=mock.Mock())

        # force_shutdown belongs to [agent]; appending it after [broker]
        # would silently land in the wrong table.
        text = GOOD_AGENT.replace(
            "dry_run = true", "dry_run = false\nforce_shutdown = true"
        )
        with mock.patch("platform.system", return_value="Windows"), mock.patch(
            "pcwake.agent.client.Agent", side_effect=capture
        ):
            assert agent_main.main(["-c", config_file(text)]) == 0
        assert captured["force"] is True

    def test_help_exits_cleanly(self):
        with pytest.raises(SystemExit) as exc:
            agent_main.main(["--help"])
        assert exc.value.code == 0


class TestHubEntryPoint:
    def test_a_missing_config_exits_two(self, tmp_path):
        assert hub_main.main(["-c", str(tmp_path / "nope.toml")]) == 2

    def test_an_empty_allowlist_is_refused_at_startup(self, config_file):
        # Rather than starting a bot anyone could command.
        text = GOOD_HUB.replace("allowed_chat_ids = [1]", "allowed_chat_ids = []")
        assert hub_main.main(["-c", config_file(text)]) == 2

    def test_a_missing_token_is_refused_at_startup(self, config_file, monkeypatch):
        monkeypatch.delenv("PCWAKE_TELEGRAM_TOKEN", raising=False)
        text = GOOD_HUB.replace('token = "123:abc"', "")
        assert hub_main.main(["-c", config_file(text)]) == 2

    def test_a_valid_config_starts_polling(self, config_file):
        with mock.patch("pcwake.hub.bot.PcWakeBot.build") as build:
            assert hub_main.main(["-c", config_file(GOOD_HUB)]) == 0
        build.return_value.run_polling.assert_called_once()

    def test_help_exits_cleanly(self):
        with pytest.raises(SystemExit) as exc:
            hub_main.main(["--help"])
        assert exc.value.code == 0
