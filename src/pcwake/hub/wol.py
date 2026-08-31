"""Wake-on-LAN magic packets.

This is the one thing that cannot live on the PC: when the PC is off, nothing
on it can listen, so the Pi builds and broadcasts the packet itself. It never
goes through MQTT.

A magic packet is six 0xFF bytes followed by the target MAC repeated sixteen
times -- 102 bytes, sent as a UDP broadcast the sleeping NIC's firmware
matches on. There is no reply and no delivery guarantee: the only way to know
it worked is that the machine shows up.
"""

from __future__ import annotations

import logging
import socket

log = logging.getLogger("pcwake.wol")

# Both are in common use by NIC firmware. Neither is a listening service; the
# packet is matched by the adapter, so the port barely matters -- we send to
# both because it costs one extra datagram and removes a class of "why won't
# it wake" reports.
WOL_PORTS = (9, 7)

_SEPARATORS = str.maketrans("", "", ":-. ")


def parse_mac(mac: str) -> bytes:
    """Parse a MAC address into its six raw bytes.

    Accepts the common spellings: colon-separated, dash-separated,
    dot-separated, space-separated, or bare hex.
    """
    if not isinstance(mac, str):
        raise ValueError(f"MAC address must be a string, got {type(mac).__name__}")
    cleaned = mac.strip().translate(_SEPARATORS)
    if len(cleaned) != 12:
        raise ValueError(
            f"MAC address {mac!r} must have 12 hex digits, found {len(cleaned)}"
        )
    try:
        raw = bytes.fromhex(cleaned)
    except ValueError as exc:
        raise ValueError(f"MAC address {mac!r} contains non-hex characters") from exc
    return raw


def build_magic_packet(mac: str) -> bytes:
    """Build the 102-byte magic packet for `mac`."""
    return b"\xff" * 6 + parse_mac(mac) * 16


def send_magic_packet(
    mac: str, broadcast: str = "255.255.255.255", ports: tuple[int, ...] = WOL_PORTS
) -> None:
    """Broadcast a magic packet for `mac`.

    Raises OSError if the datagram could not be sent at all. A successful send
    means only that the packet left this machine: waking is unacknowledged by
    design, so the caller must confirm by watching for the host to appear.

    `broadcast` matters when the PC is not on the Pi's subnet -- the global
    255.255.255.255 address is not forwarded by routers, so a PC on another
    subnet needs that subnet's directed broadcast address (e.g.
    192.168.1.255) and a router willing to forward it.
    """
    packet = build_magic_packet(mac)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for port in ports:
            sock.sendto(packet, (broadcast, port))
    log.info("sent magic packet for %s to %s on ports %s", mac, broadcast, list(ports))
