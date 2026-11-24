"""Wake-on-LAN: the magic packet for another server's network card, sent as a UDP broadcast."""

from __future__ import annotations

import re
import socket
from typing import Any, Dict, Optional

# six pairs of hex digits, separated all by colons or all by hyphens
MAC_RE = re.compile(r"[0-9A-Fa-f]{2}([:-])[0-9A-Fa-f]{2}(?:\1[0-9A-Fa-f]{2}){4}")


def parse_mac(text: str) -> Optional[bytes]:
    """The six bytes of a MAC address such as AA:BB:CC:DD:EE:FF or aa-bb-cc-dd-ee-ff, or None."""
    if not MAC_RE.fullmatch(text):
        return None
    return bytes.fromhex(text.replace(":", "").replace("-", ""))


def spelled(mac: bytes) -> str:
    return ":".join(f"{byte:02X}" for byte in mac)


def magic_packet(mac: bytes) -> bytes:
    """Six bytes of FF, then the address 16 times: what a sleeping network card listens for."""
    return b"\xff" * 6 + mac * 16


class Wake:
    """Sends magic packets to the broadcast address and port from [wake]."""

    def __init__(self, broadcast: str, port: int) -> None:
        self.broadcast = broadcast
        self.port = port

    def send(self, mac: bytes) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.sendto(magic_packet(mac), (self.broadcast, self.port))

    def status(self) -> Dict[str, Any]:
        return {"broadcast": self.broadcast, "port": self.port}
