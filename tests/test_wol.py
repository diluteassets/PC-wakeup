"""The magic packet is verifiable byte for byte without any hardware, so it
is worth pinning exactly."""

import pytest

from pcwake.hub.wol import build_magic_packet, parse_mac


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
