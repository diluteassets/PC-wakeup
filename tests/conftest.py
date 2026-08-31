"""Fixtures for the integration tests.

These run against a real Mosquitto rather than a mocked client, because the
behaviour that matters most here -- retained messages, last-will delivery,
QoS 1 publish confirmation -- is behaviour of the broker, not of our code. A
mock would happily confirm a design that does not actually work.
"""

from __future__ import annotations

import asyncio
import shutil
import socket
import subprocess
import time
from collections.abc import Callable

import pytest

MOSQUITTO = shutil.which("mosquitto") or shutil.which("/usr/sbin/mosquitto")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_for_port(port: int, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.25):
                return True
        except OSError:
            time.sleep(0.05)
    return False


class BrokerProcess:
    """A Mosquitto the test controls, so a restart can be exercised the way a
    Pi reboot or a package upgrade would cause one."""

    def __init__(self, port: int, config_path) -> None:
        self.port = port
        self._config = config_path
        self._process: subprocess.Popen | None = None

    def start(self) -> None:
        if self._process is not None:
            return
        self._process = subprocess.Popen(
            [MOSQUITTO, "-c", str(self._config)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        if not _wait_for_port(self.port):
            stderr = (
                self._process.stderr.read().decode(errors="replace")
                if self._process.stderr
                else ""
            )
            self._process.kill()
            self._process = None
            pytest.fail(f"mosquitto did not start on port {self.port}: {stderr}")

    def stop(self) -> None:
        """Kill it outright -- a graceful stop is not what a reboot looks
        like, and the clients must cope with the abrupt version."""
        if self._process is None:
            return
        self._process.kill()
        self._process.wait(timeout=5)
        self._process = None


@pytest.fixture
def restartable_broker(tmp_path) -> BrokerProcess:
    """A broker under the test's control, for outage and recovery scenarios."""
    if MOSQUITTO is None:
        pytest.skip("mosquitto is not installed")
    port = _free_port()
    config = tmp_path / "mosquitto-restartable.conf"
    config.write_text(
        f"listener {port} 127.0.0.1\nallow_anonymous true\npersistence false\n"
    )
    process = BrokerProcess(port, config)
    process.start()
    yield process
    process.stop()


@pytest.fixture
def broker(tmp_path) -> int:
    """A private Mosquitto on a free port, one per test.

    Function-scoped on purpose: retained messages and last wills are exactly
    what these tests exercise, so leaking them between tests would make the
    results meaningless.
    """
    if MOSQUITTO is None:
        pytest.skip("mosquitto is not installed")

    port = _free_port()
    config = tmp_path / "mosquitto.conf"
    config.write_text(
        f"listener {port} 127.0.0.1\n"
        "allow_anonymous true\n"
        "persistence false\n"
    )
    process = subprocess.Popen(
        [MOSQUITTO, "-c", str(config)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    if not _wait_for_port(port):
        process.kill()
        stderr = process.stderr.read().decode(errors="replace") if process.stderr else ""
        pytest.fail(f"mosquitto did not start on port {port}: {stderr}")

    yield port

    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


async def wait_until(
    predicate: Callable[[], bool], timeout: float = 10.0, interval: float = 0.05
) -> bool:
    """Poll `predicate` until it is true or the timeout elapses."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return predicate()
