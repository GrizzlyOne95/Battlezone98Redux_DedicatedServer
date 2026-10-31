"""Opt-in IPv4 LAN discovery for OpenShim, independent of game packet framing.

This protocol advertises a lobby service, not an individual game or identity.
The requester uses the UDP source IP and advertised WebSocket port. Broadcast
discovery is unauthenticated; selecting a host remains an explicit user action.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
import ipaddress
import struct
import time
from typing import Callable

DISCOVERY_PORT = 21337
QUERY_MAGIC = b"BZRLAN1?"
REPLY_MAGIC = b"BZRLAN1!"
QUERY_SIZE = 96
MAX_NAME_BYTES = 64
LOCAL_NETWORKS = tuple(ipaddress.IPv4Network(cidr) for cidr in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8", "169.254.0.0/16",
))


def local_source(host: str) -> bool:
    try:
        address = ipaddress.IPv4Address(host)
    except ipaddress.AddressValueError:
        return False
    return any(address in network for network in LOCAL_NETWORKS)


def encoded_name(name: str) -> bytes:
    try:
        value = name.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError("LAN name must contain printable ASCII only") from exc
    if not 1 <= len(value) <= MAX_NAME_BYTES or any(ch < 32 or ch > 126 for ch in value):
        raise ValueError("LAN name must contain 1 to 64 printable ASCII bytes")
    if not value.strip():
        raise ValueError("LAN name must not be blank")
    return value


def discovery_reply(query: bytes, port: int, name: bytes) -> bytes | None:
    if (len(query) != QUERY_SIZE or query[:8] != QUERY_MAGIC or
            query[16:] != bytes(80)):
        return None
    return REPLY_MAGIC + query[8:16] + struct.pack("!H", port) + name


class LanDiscoveryProtocol(asyncio.DatagramProtocol):
    """Unicast replies, with bounded per-source and global token buckets.

    A response is at most 82 bytes to a 96-byte query, avoiding UDP payload
    amplification. The 128-source cache cannot grow with arbitrary source IPs.
    """

    def __init__(self, port: int, name: str, *, clock: Callable[[], float] = time.monotonic):
        if not 1 <= port <= 65534:
            raise ValueError("LAN lobby port must be 1 to 65534")
        self.port = port
        self.name = encoded_name(name)
        self.clock = clock
        self.transport: asyncio.DatagramTransport | None = None
        self.sources: OrderedDict[str, tuple[float, float]] = OrderedDict()
        self.global_bucket = (64.0, clock())

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    def connection_lost(self, exc: Exception | None) -> None:
        self.transport = None
        self.sources.clear()

    def _permit(self, source: str) -> bool:
        now = self.clock()
        global_tokens, global_time = self.global_bucket
        global_tokens = min(64.0, global_tokens + max(0.0, now - global_time) * 32.0)
        tokens, last = self.sources.pop(source, (4.0, now))
        tokens = min(4.0, tokens + max(0.0, now - last) * 2.0)
        allowed = global_tokens >= 1.0 and tokens >= 1.0
        self.sources[source] = (tokens - 1.0 if allowed else tokens, now)
        if len(self.sources) > 128:
            self.sources.popitem(last=False)
        self.global_bucket = (global_tokens - 1.0 if allowed else global_tokens, now)
        return allowed

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        if self.transport is None or not local_source(addr[0]):
            return
        reply = discovery_reply(data, self.port, self.name)
        if reply is not None and self._permit(addr[0]):
            self.transport.sendto(reply, addr)
