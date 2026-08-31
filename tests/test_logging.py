"""Logging setup.

The case that matters is Windows: the agent installs as a scheduled task
launched with pythonw.exe, which has no console, so sys.stderr is None and
every log line is discarded. That leaves the agent undebuggable on the
platform where it is hardest to diagnose any other way -- and the
troubleshooting guide tells people to read a log that would not exist.
"""

import logging
import sys

import pytest

from pcwake.common import logging as pcwake_logging


@pytest.fixture(autouse=True)
def restore_logging():
    root = logging.getLogger()
    saved = root.handlers[:], root.level
    yield
    for handler in root.handlers[:]:
        root.removeHandler(handler)
    for handler in saved[0]:
        root.addHandler(handler)
    root.setLevel(saved[1])


def log_lines(path):
    """Flush, then read. Not logging.shutdown() -- that is an interpreter-exit
    function which closes every handler in the process, and calling it from a
    test helper quietly breaks the tests that run after it."""
    for handler in logging.getLogger().handlers:
        handler.flush()
    return path.read_text() if path.exists() else ""


class TestConsoleLogging:
    def test_logs_to_stderr_when_there_is_one(self, capsys):
        pcwake_logging.setup()
        logging.getLogger("pcwake.test").info("hello")
        assert "hello" in capsys.readouterr().err

    def test_verbose_enables_debug(self):
        pcwake_logging.setup(verbose=True)
        assert logging.getLogger().level == logging.DEBUG

    def test_default_level_is_info(self, monkeypatch):
        monkeypatch.delenv("PCWAKE_DEBUG", raising=False)
        pcwake_logging.setup()
        assert logging.getLogger().level == logging.INFO

    def test_the_env_var_also_enables_debug(self, monkeypatch):
        monkeypatch.setenv("PCWAKE_DEBUG", "1")
        pcwake_logging.setup()
        assert logging.getLogger().level == logging.DEBUG


class TestFileLogging:
    def test_an_explicit_log_file_receives_the_lines(self, tmp_path):
        path = tmp_path / "agent.log"
        pcwake_logging.setup(log_file=path)
        logging.getLogger("pcwake.test").info("written to a file")
        assert "written to a file" in log_lines(path)

    def test_it_creates_the_directory(self, tmp_path):
        path = tmp_path / "nested" / "deeper" / "agent.log"
        pcwake_logging.setup(log_file=path)
        logging.getLogger("pcwake.test").info("hello")
        assert path.exists()

    def test_stderr_still_gets_the_lines_too(self, tmp_path, capsys):
        # A foreground run should not go quiet just because a file was asked
        # for -- you want to see it while you are watching.
        path = tmp_path / "agent.log"
        pcwake_logging.setup(log_file=path)
        logging.getLogger("pcwake.test").info("both places")
        assert "both places" in capsys.readouterr().err
        assert "both places" in log_lines(path)

    def test_an_unwritable_location_does_not_stop_the_agent(self, tmp_path):
        # Losing the log is bad; refusing to wake the PC is worse.
        pcwake_logging.setup(log_file=tmp_path / "agent.log" / "impossible.log")
        logging.getLogger("pcwake.test").info("still running")


class TestNoConsole:
    """The pythonw case.

    sys.stderr has to be blanked inside the test body, not in a fixture:
    pytest reinstalls its capture stream after fixture setup, so a fixture's
    patch is overwritten before the test even runs.
    """

    def test_falls_back_to_a_file_rather_than_to_nothing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pcwake_logging, "default_log_file",
                            lambda: tmp_path / "fallback.log")
        monkeypatch.setattr(sys, "stderr", None)
        pcwake_logging.setup()
        logging.getLogger("pcwake.agent").error("shutdown failed")
        assert "shutdown failed" in log_lines(tmp_path / "fallback.log")

    def test_an_explicit_file_still_wins(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pcwake_logging, "default_log_file",
                            lambda: tmp_path / "should-not-be-used.log")
        monkeypatch.setattr(sys, "stderr", None)
        chosen = tmp_path / "chosen.log"
        pcwake_logging.setup(log_file=chosen)
        logging.getLogger("pcwake.agent").info("hello")
        assert "hello" in log_lines(chosen)
        assert not (tmp_path / "should-not-be-used.log").exists()

    def test_logging_never_raises_even_with_nowhere_to_write(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pcwake_logging, "default_log_file",
                            lambda: tmp_path / "x" / "y")
        monkeypatch.setattr(pcwake_logging, "_file_handler", lambda path: None)
        monkeypatch.setattr(sys, "stderr", None)
        pcwake_logging.setup()
        logging.getLogger("pcwake.agent").info("swallowed, but no crash")


class TestDefaultLogFile:
    def test_windows_uses_programdata(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setenv("PROGRAMDATA", r"C:\ProgramData")
        assert "pcwake" in str(pcwake_logging.default_log_file())

    def test_posix_uses_the_state_directory(self, monkeypatch, tmp_path):
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
        assert pcwake_logging.default_log_file() == tmp_path / "pcwake" / "pcwake.log"


class TestRepeatedSetup:
    def test_calling_setup_twice_does_not_duplicate_every_line(self, capsys):
        # doctor then run, or a test suite, must not double the output.
        pcwake_logging.setup()
        pcwake_logging.setup()
        logging.getLogger("pcwake.test").info("once")
        assert capsys.readouterr().err.count("once") == 1
