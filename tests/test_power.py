"""Backend dispatch and error handling. The real power calls are not
exercised here for obvious reasons -- FakeBackend exists so the rest of the
system can be tested without them."""

import subprocess
from unittest import mock

import pytest

from pcwake.common.protocol import Action
from pcwake.agent.linux import LinuxBackend
from pcwake.agent.power import (
    FakeBackend,
    PowerActionError,
    run_command,
    select_backend,
)


class TestFakeBackend:
    @pytest.mark.parametrize("action", list(Action))
    def test_records_instead_of_performing(self, action):
        backend = FakeBackend()
        backend.perform(action)
        assert backend.calls == [action]

    def test_dispatch_reaches_the_right_method(self):
        backend = FakeBackend()
        for action in Action:
            backend.perform(action)
        assert backend.calls == list(Action)

    def test_direct_method_calls_are_recorded_too(self):
        backend = FakeBackend()
        backend.sleep()
        backend.lock()
        assert backend.calls == [Action.SLEEP, Action.LOCK]


class TestSelectBackend:
    def test_dry_run_wins_over_the_platform(self):
        assert select_backend(dry_run=True).name == "dry-run"

    def test_linux_selects_the_systemd_backend(self):
        with mock.patch("platform.system", return_value="Linux"):
            assert select_backend().name == "linux"

    def test_windows_selects_the_ctypes_backend(self):
        with mock.patch("platform.system", return_value="Windows"):
            # Importing the module is enough; nothing calls into Win32 here.
            assert select_backend().name == "windows"

    def test_unsupported_platform_refuses_to_start(self):
        # Better than starting an agent that fails every command it receives.
        with mock.patch("platform.system", return_value="Plan9"):
            with pytest.raises(PowerActionError, match="unsupported platform"):
                select_backend()


class TestRunCommand:
    def test_success_is_silent(self):
        run_command(["true"], "no-op")

    def test_failure_surfaces_stderr(self):
        completed = subprocess.CompletedProcess(
            args=["systemctl"], returncode=1, stdout="", stderr="Access denied\n"
        )
        with mock.patch("subprocess.run", return_value=completed):
            with pytest.raises(PowerActionError, match="Access denied"):
                run_command(["systemctl", "suspend"], "suspend")

    def test_missing_binary_names_it(self):
        with pytest.raises(PowerActionError, match="not-a-real-binary"):
            run_command(["not-a-real-binary"], "nonsense")

    def test_timeout_is_reported(self):
        with mock.patch(
            "subprocess.run", side_effect=subprocess.TimeoutExpired("systemctl", 15)
        ):
            with pytest.raises(PowerActionError, match="did not return"):
                run_command(["systemctl", "poweroff"], "poweroff")

    def test_falls_back_to_stdout_when_stderr_is_empty(self):
        completed = subprocess.CompletedProcess(
            args=["x"], returncode=2, stdout="something went wrong\n", stderr=""
        )
        with mock.patch("subprocess.run", return_value=completed):
            with pytest.raises(PowerActionError, match="something went wrong"):
                run_command(["x"], "thing")


class TestLinuxBackend:
    @pytest.mark.parametrize(
        "action, expected",
        [
            (Action.SLEEP, ["systemctl", "suspend"]),
            (Action.SHUTDOWN, ["systemctl", "poweroff"]),
            (Action.RESTART, ["systemctl", "reboot"]),
        ],
    )
    def test_maps_actions_to_systemctl(self, action, expected):
        with mock.patch("pcwake.agent.linux.run_command") as run:
            LinuxBackend().perform(action)
        assert run.call_args.args[0] == expected

    def test_lock_prefers_loginctl(self):
        with mock.patch("pcwake.agent.linux.run_command") as run, mock.patch(
            "shutil.which", return_value="/usr/bin/loginctl"
        ):
            LinuxBackend().lock()
        assert run.call_args.args[0] == ["loginctl", "lock-session"]

    def test_lock_falls_back_when_loginctl_fails(self):
        # Desktop environments disagree about who owns the lock screen, so a
        # single failure must not be the end of it.
        def fail_loginctl(argv, what):
            if argv[0] == "loginctl":
                raise PowerActionError("no session")

        with mock.patch(
            "pcwake.agent.linux.run_command", side_effect=fail_loginctl
        ) as run, mock.patch("shutil.which", return_value="/usr/bin/x"):
            LinuxBackend().lock()
        assert run.call_args.args[0] == ["xdg-screensaver", "lock"]

    def test_lock_reports_every_failure_when_nothing_works(self):
        with mock.patch("shutil.which", return_value=None):
            with pytest.raises(PowerActionError, match="loginctl"):
                LinuxBackend().lock()

    def test_preflight_refuses_without_systemd(self):
        with mock.patch("shutil.which", return_value=None):
            with pytest.raises(PowerActionError, match="systemd"):
                LinuxBackend().preflight(Action.SHUTDOWN)

    def test_preflight_passes_with_systemd(self):
        with mock.patch("shutil.which", return_value="/bin/systemctl"):
            assert LinuxBackend().preflight(Action.SHUTDOWN) is None
