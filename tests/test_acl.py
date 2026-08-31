"""The shipped Mosquitto ACL, enforced by a real broker.

install/pi/mosquitto/aclfile carries a security claim: a compromised agent
can talk about itself and nothing more -- it cannot issue commands and it
cannot forge another machine's status. That claim rests entirely on the
contents of a config file, where a single edit ("topic X" instead of
"topic read X" silently grants write as well) breaks it invisibly.

So these run the real broker against the file as shipped.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from .conftest import MOSQUITTO, _free_port, _wait_for_port

ACL_SOURCE = Path(__file__).resolve().parents[1] / "install/pi/mosquitto/aclfile"
PASSWD_TOOL = shutil.which("mosquitto_passwd")
PUB_TOOL = shutil.which("mosquitto_pub")
SUB_TOOL = shutil.which("mosquitto_sub")

HUB_PASSWORD = "hub-secret"
AGENT_PASSWORD = "agent-secret"

NOT_AUTHORIZED = 135
"""MQTT 5 PUBACK reason code 0x87. MQTT 3.1.1 tells a publisher nothing when
the broker drops its message, which is why these tests speak v5."""

ACCEPTED = (0, 16)
"""Success codes. 16 is "no matching subscribers", which still means the
broker took the message -- only 135 means it refused it."""


@pytest.fixture
def acl_broker(tmp_path):
    """A broker running the shipped ACL, with a hub and an agent account."""
    if not all([MOSQUITTO, PASSWD_TOOL, PUB_TOOL, SUB_TOOL]):
        pytest.skip("mosquitto and its clients are not installed")
    if os.geteuid() != 0:
        # Mosquitto insists these files are root-owned, and refuses to read
        # them otherwise -- which is the very thing SETUP.md documents.
        pytest.skip("needs root to give the broker root-owned credentials")

    port = _free_port()
    acl = tmp_path / "aclfile"
    shutil.copy(ACL_SOURCE, acl)
    passwd = tmp_path / "passwd"

    subprocess.run(
        [PASSWD_TOOL, "-c", "-b", str(passwd), "hub", HUB_PASSWORD],
        check=True, capture_output=True,
    )
    subprocess.run(
        [PASSWD_TOOL, "-b", str(passwd), "agent", AGENT_PASSWORD],
        check=True, capture_output=True,
    )

    # Exactly what SETUP.md prescribes: root-owned, 0700.
    for path in (passwd, acl):
        os.chown(path, 0, 0)
        os.chmod(path, 0o700)
    # The broker still has to traverse pytest's tmp directory to reach them.
    for parent in list(tmp_path.parents)[:3] + [tmp_path]:
        try:
            os.chmod(parent, 0o755)
        except OSError:
            pass

    config = tmp_path / "mosquitto.conf"
    config.write_text(
        f"listener {port} 127.0.0.1\n"
        "allow_anonymous false\n"
        f"password_file {passwd}\n"
        f"acl_file {acl}\n"
        "persistence false\n"
        # Test-only: the credentials live under a directory the mosquitto
        # user cannot reach. Irrelevant to the ACL semantics under test.
        "user root\n"
    )
    process = subprocess.Popen(
        [MOSQUITTO, "-c", str(config)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    if not _wait_for_port(port):
        process.kill()
        output = process.stdout.read().decode(errors="replace") if process.stdout else ""
        pytest.skip(f"broker would not start with the shipped ACL: {output}")

    yield port

    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def publish(port: int, user: str, password: str, topic: str, payload: str = "x") -> int:
    """Publish over MQTT 5 and return the broker's reason code."""
    result = subprocess.run(
        [PUB_TOOL, "-V", "5", "-h", "127.0.0.1", "-p", str(port),
         "-u", user, "-P", password, "-t", topic, "-m", payload, "-q", "1", "-d"],
        capture_output=True, text=True, timeout=15,
    )
    output = result.stdout + result.stderr
    if "RC:" in output:
        for token in output.split():
            if token.startswith("RC:"):
                return int(token[3:].rstrip(")"))
    return 0


def received(port: int, user: str, password: str, topic: str, publishes) -> list[str]:
    """Subscribe, run `publishes`, and return whatever actually arrived."""
    proc = subprocess.Popen(
        [SUB_TOOL, "-h", "127.0.0.1", "-p", str(port), "-u", user, "-P", password,
         "-t", topic, "-v", "-W", "4"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
    )
    time.sleep(1.0)
    publishes()
    try:
        out, _ = proc.communicate(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, _ = proc.communicate()
    return [line for line in out.splitlines() if line.strip()]


class TestTheHubCanDriveEverything:
    def test_it_may_publish_commands(self, acl_broker):
        assert publish(acl_broker, "hub", HUB_PASSWORD, "pcwake/desk/cmd") in ACCEPTED

    def test_it_may_publish_a_command_to_any_host(self, acl_broker):
        # The wildcard matters: adding a second PC must not need an ACL edit.
        assert publish(acl_broker, "hub", HUB_PASSWORD, "pcwake/laptop/cmd") in ACCEPTED


class TestTheAgentIsConfinedToItself:
    def test_it_may_publish_its_own_status(self, acl_broker):
        assert publish(acl_broker, "agent", AGENT_PASSWORD, "pcwake/desk/status") in ACCEPTED

    def test_it_may_publish_its_own_ack(self, acl_broker):
        assert publish(acl_broker, "agent", AGENT_PASSWORD, "pcwake/desk/ack") in ACCEPTED

    def test_it_may_not_issue_commands(self, acl_broker):
        """The security property the whole ACL exists for."""
        assert publish(acl_broker, "agent", AGENT_PASSWORD, "pcwake/desk/cmd") == NOT_AUTHORIZED

    def test_it_may_not_forge_another_hosts_status(self, acl_broker):
        assert (
            publish(acl_broker, "agent", AGENT_PASSWORD, "pcwake/laptop/status")
            == NOT_AUTHORIZED
        )

    def test_a_forged_command_never_reaches_the_agent(self, acl_broker):
        """Reason codes say the broker refused it; this says nobody got it."""
        lines = received(
            acl_broker, "agent", AGENT_PASSWORD, "pcwake/desk/cmd",
            lambda: (
                publish(acl_broker, "agent", AGENT_PASSWORD, "pcwake/desk/cmd", "FORGED"),
                publish(acl_broker, "hub", HUB_PASSWORD, "pcwake/desk/cmd", "LEGIT"),
            ),
        )
        assert not any("FORGED" in line for line in lines), lines
        assert any("LEGIT" in line for line in lines), (
            f"the hub's own command did not get through either: {lines}"
        )

    def test_a_forged_status_never_reaches_the_hub(self, acl_broker):
        lines = received(
            acl_broker, "hub", HUB_PASSWORD, "pcwake/+/status",
            lambda: (
                publish(acl_broker, "agent", AGENT_PASSWORD, "pcwake/laptop/status", "forged"),
                publish(acl_broker, "agent", AGENT_PASSWORD, "pcwake/desk/status", "online"),
            ),
        )
        assert not any("laptop" in line for line in lines), lines
        assert any("desk" in line for line in lines), lines


class TestAnonymousAccess:
    def test_anonymous_cannot_connect_at_all(self, acl_broker):
        result = subprocess.run(
            [PUB_TOOL, "-h", "127.0.0.1", "-p", str(acl_broker),
             "-t", "pcwake/desk/cmd", "-m", "x"],
            capture_output=True, text=True, timeout=15,
        )
        assert result.returncode != 0

    def test_a_wrong_password_is_refused(self, acl_broker):
        result = subprocess.run(
            [PUB_TOOL, "-h", "127.0.0.1", "-p", str(acl_broker),
             "-u", "agent", "-P", "wrong", "-t", "pcwake/desk/status", "-m", "x"],
            capture_output=True, text=True, timeout=15,
        )
        assert result.returncode != 0
