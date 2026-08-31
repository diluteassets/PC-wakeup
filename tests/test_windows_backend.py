"""The Windows backend, exercised on any platform.

Half the product runs on Windows and none of it can be run here, but most of
what could be wrong is ordinary logic -- which argv is built, whether a
failure is noticed, whether the force flag is honoured. Those are testable
with the Win32 calls mocked, and worth testing precisely because nobody is
going to catch a mistake in them until it fails on a real desktop.
"""

import subprocess
from unittest import mock

import pytest

from pcwake.agent.power import PowerActionError
from pcwake.agent.win import WindowsBackend
from pcwake.common.protocol import Action


class FakeWinDLL:
    """Stands in for ctypes.windll, recording the calls made through it."""

    def __init__(self, suspend_result: int = 1, lock_result: int = 1) -> None:
        self.powrprof = mock.Mock()
        self.powrprof.SetSuspendState.return_value = suspend_result
        self.user32 = mock.Mock()
        self.user32.LockWorkStation.return_value = lock_result


def with_windll(fake: FakeWinDLL):
    return mock.patch("ctypes.windll", fake, create=True)


class TestShutdownArgv:
    def test_shutdown_is_immediate(self):
        with mock.patch("pcwake.agent.win.run_command") as run:
            WindowsBackend().shutdown()
        assert run.call_args.args[0] == ["shutdown.exe", "/s", "/t", "0"]

    def test_restart_is_immediate(self):
        with mock.patch("pcwake.agent.win.run_command") as run:
            WindowsBackend().restart()
        assert run.call_args.args[0] == ["shutdown.exe", "/r", "/t", "0"]

    def test_force_is_off_by_default(self):
        # An unsaved document surviving matters more than a shutdown always
        # going through, so /f must never appear unless it was asked for.
        with mock.patch("pcwake.agent.win.run_command") as run:
            WindowsBackend().shutdown()
        assert "/f" not in run.call_args.args[0]

    @pytest.mark.parametrize("action", [Action.SHUTDOWN, Action.RESTART])
    def test_force_is_honoured_when_configured(self, action):
        with mock.patch("pcwake.agent.win.run_command") as run:
            WindowsBackend(force_shutdown=True).perform(action)
        assert run.call_args.args[0][-1] == "/f"

    def test_a_failing_shutdown_surfaces_the_reason(self):
        completed = subprocess.CompletedProcess(
            args=["shutdown.exe"], returncode=1, stdout="", stderr="Access is denied.\n"
        )
        with mock.patch("subprocess.run", return_value=completed):
            with pytest.raises(PowerActionError, match="Access is denied"):
                WindowsBackend().shutdown()


class TestSleep:
    def test_asks_for_sleep_not_hibernate(self):
        # SetSuspendState(bHibernate, bForce, bWakeupEventsDisabled). The
        # first argument must be 0, or we would hibernate on every /sleep.
        # (Windows still hibernates anyway if hibernation is enabled, which
        # is what doctor warns about -- but that must not be our doing.)
        fake = FakeWinDLL()
        with with_windll(fake):
            WindowsBackend().sleep()
        fake.powrprof.SetSuspendState.assert_called_once_with(0, 1, 0)

    def test_a_zero_return_is_treated_as_failure(self):
        fake = FakeWinDLL(suspend_result=0)
        with with_windll(fake):
            with pytest.raises(PowerActionError, match="SetSuspendState"):
                WindowsBackend().sleep()

    def test_a_missing_powrprof_is_reported_clearly(self):
        fake = mock.Mock(spec=[])          # no .powrprof attribute at all
        with mock.patch("ctypes.windll", fake, create=True):
            with pytest.raises(PowerActionError, match="powrprof"):
                WindowsBackend().sleep()


class TestLock:
    def test_calls_lock_workstation(self):
        fake = FakeWinDLL()
        with with_windll(fake):
            WindowsBackend().lock()
        fake.user32.LockWorkStation.assert_called_once_with()

    def test_a_missing_user32_is_reported_clearly(self):
        fake = mock.Mock(spec=[])          # no .user32 attribute at all
        with mock.patch("ctypes.windll", fake, create=True):
            with pytest.raises(PowerActionError, match="user32"):
                WindowsBackend().lock()

    def test_a_zero_return_mentions_the_session_requirement(self):
        # The overwhelmingly likely cause of a failed lock is that the agent
        # is running as a service with no interactive session, so the error
        # should point straight at it.
        fake = FakeWinDLL(lock_result=0)
        with with_windll(fake):
            with pytest.raises(PowerActionError, match="interactive session"):
                WindowsBackend().lock()


class TestDispatch:
    @pytest.mark.parametrize("action", list(Action))
    def test_every_action_reaches_an_implementation(self, action):
        fake = FakeWinDLL()
        with with_windll(fake), mock.patch("pcwake.agent.win.run_command"):
            WindowsBackend().perform(action)


class TestPreflight:
    def test_refuses_a_shutdown_with_no_shutdown_exe(self):
        # The last moment a refusal can still be reported: after this the ack
        # has gone out and the outcome can no longer be communicated.
        with mock.patch("shutil.which", return_value=None):
            with pytest.raises(PowerActionError, match="shutdown.exe"):
                WindowsBackend().preflight(Action.SHUTDOWN)

    def test_passes_when_shutdown_exe_is_present(self):
        with mock.patch("shutil.which", return_value=r"C:\Windows\System32\shutdown.exe"):
            assert WindowsBackend().preflight(Action.SHUTDOWN) is None

    def test_sleep_and_lock_need_no_external_binary(self):
        backend = WindowsBackend()
        assert backend.preflight(Action.SLEEP) is None
        assert backend.preflight(Action.LOCK) is None
