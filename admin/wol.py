"""
Phase 11 - Wake-on-LAN.

A magic packet is just 6 bytes of 0xFF followed by the target's 6-byte
MAC address repeated 16 times, sent as a UDP broadcast - there's no
response, no confirmation the target actually woke up (that's what
polling the Devices page's "last seen" is for afterwards).

The one real, unavoidable constraint: whatever sends this packet must
be on the same broadcast domain as the target (or have a router
configured to forward directed broadcasts to that subnet) - a magic
packet doesn't route across the open internet like a normal request
does. Concretely, this means admin/server.py can wake a device when
it's running on the same LAN as that device (a common, legitimate shape
for this kind of on-prem tool); an admin console on a different
network won't be able to reach it via this endpoint. That's a property
of Wake-on-LAN itself, not a limitation of this implementation.
"""

import re
import socket

_MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}[:\-]){5}[0-9A-Fa-f]{2}$")


def is_valid_mac(mac_address: str) -> bool:
    return bool(_MAC_RE.match(mac_address.strip()))


def build_magic_packet(mac_address: str) -> bytes:
    if not is_valid_mac(mac_address):
        raise ValueError(f"'{mac_address}' doesn't look like a MAC address "
                          f"(expected e.g. AA:BB:CC:DD:EE:FF)")
    mac_bytes = bytes.fromhex(mac_address.replace(":", "").replace("-", ""))
    return b"\xff" * 6 + mac_bytes * 16


def send_magic_packet(mac_address: str, broadcast_ip: str = "255.255.255.255", port: int = 9) -> None:
    packet = build_magic_packet(mac_address)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.sendto(packet, (broadcast_ip, port))
    finally:
        sock.close()
