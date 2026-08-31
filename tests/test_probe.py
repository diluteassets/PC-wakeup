"""The reachability probe. Its failure modes matter more than its happy path:
an exception escaping here ends the caller's probe loop for good, and the
last reachability value then sticks -- so a powered-off PC would report as
"agent not running" indefinitely."""

import asyncio
from unittest import mock

import pytest

from pcwake.hub.probe import is_reachable, ping_available


class FakeProcess:
    """Stands in for an asyncio subprocess, without AsyncMock's sharp edges."""

    def __init__(self, returncode: int = 0) -> None:
        self._returncode = returncode
        self.kill_calls = 0

    async def wait(self) -> int:
        return self._returncode

    def kill(self) -> None:
        self.kill_calls += 1


class HangingProcess(FakeProcess):
    """Never finishes on the first wait, forcing the timeout branch."""

    def __init__(self, kill_error: Exception | None = None) -> None:
        super().__init__(returncode=-9)
        self._waits = 0
        self._kill_error = kill_error

    async def wait(self) -> int:
        self._waits += 1
        if self._waits == 1:
            await asyncio.sleep(3600)
        # After the kill the process is reaped, so this returns at once.
        return self._returncode

    def kill(self) -> None:
        self.kill_calls += 1
        if self._kill_error is not None:
            raise self._kill_error


def with_process(process):
    return mock.patch("asyncio.create_subprocess_exec", return_value=process)


class TestIsReachable:
    async def test_no_ping_binary_reports_no_information(self):
        # Not "the PC is off" -- an unrunnable probe must never reach the user
        # as a powered-off machine.
        with mock.patch("shutil.which", return_value=None):
            assert await is_reachable("10.0.0.5") is None

    async def test_a_zero_exit_means_reachable(self):
        with mock.patch("shutil.which", return_value="/bin/ping"), with_process(
            FakeProcess(returncode=0)
        ):
            assert await is_reachable("10.0.0.5") is True

    async def test_a_nonzero_exit_means_unreachable(self):
        with mock.patch("shutil.which", return_value="/bin/ping"), with_process(
            FakeProcess(returncode=1)
        ):
            assert await is_reachable("10.0.0.5") is False

    async def test_a_failure_to_launch_reports_no_information(self):
        with mock.patch("shutil.which", return_value="/bin/ping"), mock.patch(
            "asyncio.create_subprocess_exec", side_effect=OSError("cannot fork")
        ):
            assert await is_reachable("10.0.0.5") is None

    async def test_a_hung_ping_is_killed_and_reported_unreachable(self):
        process = HangingProcess()
        with mock.patch("shutil.which", return_value="/bin/ping"), with_process(process):
            assert await is_reachable("10.0.0.5", timeout=0.05) is False
        assert process.kill_calls == 1, "a hung ping must not be left behind"

    async def test_a_process_that_exits_during_the_timeout_kill_does_not_raise(self):
        """The race that would otherwise kill the caller's probe loop.

        Process.kill() raises ProcessLookupError when the process has already
        exited -- exactly what can happen in the moment between the timeout
        firing and the kill. ProcessLookupError is an OSError subclass, but
        the launch guard does not cover the kill, so before the fix this
        escaped all the way out of the probe loop and stopped it for good.
        """
        process = HangingProcess(kill_error=ProcessLookupError())
        with mock.patch("shutil.which", return_value="/bin/ping"), with_process(process):
            assert await is_reachable("10.0.0.5", timeout=0.05) is False


class TestAgainstRealPing:
    """A couple of unmocked probes, so the argv we build is known to work."""

    async def test_localhost_answers(self):
        if not ping_available():
            pytest.skip("no ping binary")
        assert await is_reachable("127.0.0.1") is True

    async def test_an_unroutable_address_does_not(self):
        if not ping_available():
            pytest.skip("no ping binary")
        # Reserved for documentation by RFC 5737, so it never answers on a
        # real network. Some sandboxed CI networks answer everything, which
        # makes the assertion meaningless rather than failing -- detect that
        # and skip instead of reporting a bug that is not there.
        result = await is_reachable("192.0.2.1", timeout=3.0)
        if result is True:
            pytest.skip("this network answers unroutable addresses; cannot test")
        assert result is False
