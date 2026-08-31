"""The magic packet is verifiable byte for byte without any hardware, so it
is worth pinning exactly."""

import socket
from unittest import mock

import pytest

from pcwake.hub.wol import WOL_PORTS, build_magic_packet, parse_mac, send_magic_packet


class TestParseMac:
    @pytest.mark.parametrize(
        "spelling",
        [
            "aa:bb:cc:dd:ee:ff",
            "AA:BB:CC:DD:EE:FF",
            "aa-bb-cc-dd-ee-ff",
            "AA-BB-CC-DD-EE-FF",
            "aabbccddeeff",
            "AABBCCDDEEFF",
            "aa bb cc dd ee ff",
            "aabb.ccdd.eeff",
            "  aa:bb:cc:dd:ee:ff  ",
        ],
    )
    def test_accepts_common_spellings(self, spelling):
        assert parse_mac(spelling) == b"\xaa\xbb\xcc\xdd\xee\xff"

    @pytest.mark.parametrize(
        "bad, reason",
        [
            ("aa:bb:cc:dd:ee", "too short"),
            ("aa:bb:cc:dd:ee:ff:00", "too long"),
            ("", "empty"),
            ("gg:bb:cc:dd:ee:ff", "non-hex"),
            ("zz:zz:zz:zz:zz:zz", "non-hex"),
        ],
    )
    def test_rejects_malformed(self, bad, reason):
        with pytest.raises(ValueError):
            parse_mac(bad)

    def test_rejects_non_string(self):
        with pytest.raises(ValueError):
            parse_mac(None)


class TestMagicPacket:
    def test_exact_bytes(self):
        packet = build_magic_packet("01:02:03:04:05:06")
        assert packet == b"\xff" * 6 + b"\x01\x02\x03\x04\x05\x06" * 16

    def test_length_is_102(self):
        assert len(build_magic_packet("aa:bb:cc:dd:ee:ff")) == 102

    def test_starts_with_six_ff_bytes(self):
        packet = build_magic_packet("aa:bb:cc:dd:ee:ff")
        assert packet[:6] == b"\xff" * 6
        # The seventh byte is the start of the MAC, not more padding.
        assert packet[6] == 0xAA

    def test_mac_repeats_exactly_sixteen_times(self):
        mac = b"\xde\xad\xbe\xef\x00\x01"
        packet = build_magic_packet("de:ad:be:ef:00:01")
        assert packet.count(mac) == 16

    def test_spelling_does_not_change_the_packet(self):
        assert build_magic_packet("aa:bb:cc:dd:ee:ff") == build_magic_packet("AABBCCDDEEFF")


class TestSendMagicPacket:
    """Packet construction was already pinned above; this covers the send
    itself -- the socket options and the bytes that actually leave the box.
    It is the one function the entire wake feature rests on, and a magic
    packet is unacknowledged by design, so nothing downstream would notice
    if it went out malformed."""

    def _receiver(self):
        """A UDP socket on a free port, standing in for the sleeping NIC."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1", 0))
        sock.settimeout(5)
        return sock, sock.getsockname()[1]

    def test_the_bytes_on_the_wire_are_the_magic_packet(self):
        sock, port = self._receiver()
        try:
            send_magic_packet("de:ad:be:ef:00:01", broadcast="127.0.0.1", ports=(port,))
            datagram, _ = sock.recvfrom(2048)
        finally:
            sock.close()
        assert datagram == build_magic_packet("de:ad:be:ef:00:01")
        assert len(datagram) == 102

    def test_it_is_sent_to_every_configured_port(self):
        # Both 7 and 9 are in use by NIC firmware in the wild; sending to
        # both costs one datagram and removes a class of "it won't wake".
        first, port_a = self._receiver()
        second, port_b = self._receiver()
        try:
            send_magic_packet("aa:bb:cc:dd:ee:ff", broadcast="127.0.0.1",
                              ports=(port_a, port_b))
            got_a, _ = first.recvfrom(2048)
            got_b, _ = second.recvfrom(2048)
        finally:
            first.close()
            second.close()
        expected = build_magic_packet("aa:bb:cc:dd:ee:ff")
        assert got_a == expected and got_b == expected

    def test_the_default_ports_are_seven_and_nine(self):
        assert WOL_PORTS == (9, 7)

    def test_broadcast_is_enabled_on_the_socket(self):
        # Without SO_BROADCAST a send to 255.255.255.255 fails outright, and
        # the only symptom would be a PC that never wakes.
        recorded: list[tuple[int, int, int]] = []

        class RecordingSocket(socket.socket):
            def setsockopt(self, level, option, value):
                recorded.append((level, option, value))
                return super().setsockopt(level, option, value)

        sock, port = self._receiver()
        try:
            with mock.patch("socket.socket", RecordingSocket):
                send_magic_packet("aa:bb:cc:dd:ee:ff", broadcast="127.0.0.1",
                                  ports=(port,))
            # It still has to actually send, not just set the option.
            assert sock.recvfrom(2048)[0] == build_magic_packet("aa:bb:cc:dd:ee:ff")
        finally:
            sock.close()
        assert (socket.SOL_SOCKET, socket.SO_BROADCAST, 1) in recorded

    def test_a_malformed_mac_raises_before_anything_is_sent(self):
        with pytest.raises(ValueError):
            send_magic_packet("not-a-mac", broadcast="127.0.0.1", ports=(9,))
