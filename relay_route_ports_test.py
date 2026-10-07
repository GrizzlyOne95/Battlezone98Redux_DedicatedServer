#!/usr/bin/env python3
"""Per-route relay port tests (BZRNET_CLIENT_DECOMP_VALIDATION_20261006.md section 2).

Covers: distinct ports for two routes sharing one client, byte-for-byte
forwarding both ways on each route socket (fake and real loopback UDP), the
OnP2PRoute port advertised to both peers, release on route end, and the
exhausted-range fallback. Loopback only; uses RFC 5737 addresses for fakes.

Usage: python relay_route_ports_test.py
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import socket
import sys
from typing import Any

from server import BZRRelayProtocol, BZRWebSocketServer, ServerState, UserSession
from stock_client_compat_test import FakeDatagramTransport, FakeWS, _attach_session, _last_data

PAYLOAD = bytes.fromhex("c0030000019d1199210b00000000000012345353")
PAYLOAD2 = bytes(range(64)) + b"\x00\xff" * 5

A_EP, B_EP, C_EP = ("203.0.113.1", 41001), ("198.51.100.2", 42002), ("192.0.2.3", 43003)


class FakeRouteSocket:
    """Stand-in for _RouteSocket: records sends, tracks close."""

    def __init__(self, port: int) -> None:
        self.port = port
        self.route = None
        self.sent: list[tuple[bytes, tuple[str, int]]] = []
        self.closed = False

    def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
        self.sent.append((bytes(data), addr))

    def close(self) -> None:
        self.closed = True


def make_relay(start: int = 1340, end: int = 1342, ttl: int = 180):
    state = ServerState(logging.getLogger("ports-test.state"))
    handles: dict[int, FakeRouteSocket] = {}

    def factory(port: int) -> FakeRouteSocket:
        handles[port] = FakeRouteSocket(port)
        return handles[port]

    relay = BZRRelayProtocol(
        logging.getLogger("ports-test.relay"), state,
        route_ttl_seconds=ttl, route_port_start=start, route_port_end=end,
        route_socket_factory=factory,
    )
    shared = FakeDatagramTransport(1339)
    relay.connection_made(shared)  # type: ignore[arg-type]
    return relay, shared, handles


def sessions():
    a = UserSession("A", wan_address="203.0.113.1:41001", current_lobby_id=77)
    b = UserSession("B", wan_address="198.51.100.2:42002", current_lobby_id=77)
    c = UserSession("C", wan_address="192.0.2.3:43003", current_lobby_id=77)
    return a, b, c


def deliver(relay: BZRRelayProtocol, handle: FakeRouteSocket, data: bytes, addr: tuple[str, int]) -> None:
    relay._handle_datagram(data, addr, handle.route, handle)


def test_distinct_ports_and_forwarding() -> None:
    relay, shared, handles = make_relay()
    a, b, c = sessions()
    ok, _, p_ab = relay.register_route_port(a, b)
    ok2, _, p_ac = relay.register_route_port(a, c)
    assert ok and ok2 and p_ab != p_ac, (p_ab, p_ac)
    assert {p_ab, p_ac} <= {1340, 1341, 1342}
    # The pair registered from the other side reuses the same port.
    assert relay.register_route_port(b, a)[2] == p_ab
    assert relay.metrics()["activeRoutePorts"] == sorted([p_ab, p_ac])

    # Same client A sends the same bytes to both routes: each reaches only its peer.
    deliver(relay, handles[p_ab], PAYLOAD, A_EP)
    deliver(relay, handles[p_ac], PAYLOAD2, A_EP)
    assert handles[p_ab].sent == [(PAYLOAD, B_EP)]
    assert handles[p_ac].sent == [(PAYLOAD2, C_EP)]
    # Reverse direction leaves from the route's own port socket, byte-for-byte.
    deliver(relay, handles[p_ab], PAYLOAD2, B_EP)
    deliver(relay, handles[p_ac], PAYLOAD, C_EP)
    assert handles[p_ab].sent[-1] == (PAYLOAD2, A_EP)
    assert handles[p_ac].sent[-1] == (PAYLOAD, A_EP)
    assert shared.sent == []

    # Stranger on a route port is dropped.
    before = relay.drop_count
    deliver(relay, handles[p_ab], PAYLOAD, ("203.0.113.99", 5))
    assert relay.drop_count == before + 1 and len(handles[p_ab].sent) == 2

    # Endpoint learning: A's NAT source port differs from its WS-reported one.
    deliver(relay, handles[p_ab], PAYLOAD, ("203.0.113.1", 50000))
    assert handles[p_ab].sent[-1] == (PAYLOAD, B_EP)
    deliver(relay, handles[p_ab], PAYLOAD2, B_EP)
    assert handles[p_ab].sent[-1] == (PAYLOAD2, ("203.0.113.1", 50000))


def test_release() -> None:
    relay, _, handles = make_relay()
    a, b, c = sessions()
    p_ab = relay.register_route_port(a, b)[2]
    p_ac = relay.register_route_port(a, c)[2]
    relay.remove_user("B", lobby_id=77)
    assert handles[p_ab].closed and not handles[p_ac].closed
    assert relay.metrics()["activeRoutePorts"] == [p_ac]
    # Packets arriving on the released socket's stale route are dropped, not relayed.
    stale = FakeRouteSocket(p_ab)
    sent_before = relay.tx_count
    relay._handle_datagram(PAYLOAD, A_EP, None, None)  # shared path: unmapped
    assert relay.tx_count == sent_before and stale.sent == []

    # Idle timeout releases too.
    relay.routes[("A", "C")].last_activity_monotonic -= 10_000
    relay.metrics()
    assert handles[p_ac].closed and relay.metrics()["activeRoutes"] == 0

    # Disabling the relay releases everything.
    p = relay.register_route_port(a, b)[2]
    relay.set_enabled(False)
    assert handles[p].closed and not relay.routes
    relay.set_enabled(True)

    # Freed ports can be allocated again.
    relay.close()
    assert relay.register_route_port(a, b)[0]


def test_exhaustion() -> None:
    relay, shared, handles = make_relay(1340, 1341)
    a, b, c = sessions()
    d = UserSession("D", wan_address="192.0.2.4:44004", current_lobby_id=77)
    e = UserSession("E", wan_address="192.0.2.5:45005", current_lobby_id=77)
    assert relay.register_route_port(a, b)[2] in (1340, 1341)
    assert relay.register_route_port(a, c)[2] in (1340, 1341)
    ok, _, port = relay.register_route_port(d, e)
    assert ok and port == 1339, port  # falls back to the shared socket
    assert relay.port_exhausted_fallback_count == 1
    # The fallback route still works through the legacy shared matcher.
    relay.datagram_received(PAYLOAD, ("192.0.2.4", 44004))
    assert shared.sent == [(PAYLOAD, ("192.0.2.5", 45005))]
    # A per-route route is not reachable via the shared socket.
    relay.datagram_received(PAYLOAD, A_EP)
    assert len(shared.sent) == 1
    # Freeing a port lets the next route get a dedicated one.
    relay.remove_user("B", lobby_id=77)
    f = UserSession("F", wan_address="192.0.2.6:46006", current_lobby_id=77)
    assert relay.register_route_port(a, f)[2] in (1340, 1341)


def test_disabled_range_is_legacy() -> None:
    relay, shared, _ = make_relay(0, 0)
    a, b, c = sessions()
    assert relay.register_route_port(a, b)[2] == 1339
    relay.datagram_received(PAYLOAD, A_EP)
    assert shared.sent == [(PAYLOAD, B_EP)]


async def test_onp2proute_advertises_distinct_ports() -> None:
    relay, _, _ = make_relay()
    state = relay.state
    server = BZRWebSocketServer(state, logging.getLogger("ports-test.ws"), relay_protocol=relay)
    wss = {u: FakeWS(50000 + i) for i, u in enumerate("ABC")}
    sess = {}
    for u, ep in zip("ABC", ("203.0.113.1:41001", "198.51.100.2:42002", "192.0.2.3:43003")):
        s = UserSession(u, name=u, player_name=u, wan_address=ep, current_lobby_id=77, ws=wss[u])
        _attach_session(state, s)
        sess[u] = s
    await server.handle_p2p_signal(sess["A"], {"player": "B"}, route=True)
    await server.handle_p2p_signal(sess["A"], {"player": "C"}, route=True)
    ab_a = [m["data"] for m in wss["A"].by_type("OnP2PRoute")]
    ab_b = _last_data(wss["B"], "OnP2PRoute")
    ac_c = _last_data(wss["C"], "OnP2PRoute")
    assert len(ab_a) == 2 and ab_a[0]["player"] == "B" and ab_a[1]["player"] == "C"
    assert ab_a[0]["port"] == ab_b["port"] and ab_a[1]["port"] == ac_c["port"]
    assert ab_a[0]["port"] != ab_a[1]["port"]


async def test_real_loopback_sockets() -> None:
    """Real UDP: two routes for client A, bytes pass unchanged through per-route ports."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    probe.bind(("127.0.0.1", 0))
    start = probe.getsockname()[1] + 1
    probe.close()

    def client() -> socket.socket:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(("127.0.0.1", 0))
        s.setblocking(False)
        return s

    ca, cb, cc = client(), client(), client()
    state = ServerState(logging.getLogger("ports-test.real"))
    relay = BZRRelayProtocol(
        logging.getLogger("ports-test.real.relay"), state,
        route_port_start=start, route_port_end=start + 5, bind_host="127.0.0.1",
    )
    relay.connection_made(FakeDatagramTransport(1339))  # type: ignore[arg-type]
    mk = lambda uid, s: UserSession(uid, wan_address=f"127.0.0.1:{s.getsockname()[1]}", current_lobby_id=1)
    a, b, c = mk("A", ca), mk("B", cb), mk("C", cc)
    try:
        p_ab = relay.register_route_port(a, b)[2]
        p_ac = relay.register_route_port(a, c)[2]
        assert p_ab != p_ac and start <= p_ab <= start + 5
        loop = asyncio.get_running_loop()

        async def recv(sock: socket.socket) -> tuple[bytes, tuple[str, int]]:
            return await asyncio.wait_for(loop.sock_recvfrom(sock, 4096), 3)

        await asyncio.sleep(0.05)  # let route transports attach
        await loop.sock_sendto(ca, PAYLOAD, ("127.0.0.1", p_ab))
        data, src = await recv(cb)
        assert data == PAYLOAD and src == ("127.0.0.1", p_ab)
        await loop.sock_sendto(cb, PAYLOAD2, ("127.0.0.1", p_ab))
        data, src = await recv(ca)
        assert data == PAYLOAD2 and src == ("127.0.0.1", p_ab)  # A sees B at relay:p_ab
        await loop.sock_sendto(ca, PAYLOAD2, ("127.0.0.1", p_ac))
        data, src = await recv(cc)
        assert data == PAYLOAD2 and src == ("127.0.0.1", p_ac)
        await loop.sock_sendto(cc, PAYLOAD, ("127.0.0.1", p_ac))
        data, src = await recv(ca)
        assert data == PAYLOAD and src == ("127.0.0.1", p_ac)  # ...and C at relay:p_ac

        # Release closes the real port: it can be re-bound immediately.
        relay.remove_user("B", lobby_id=1)
        await asyncio.sleep(0.05)
        rebind = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        rebind.bind(("127.0.0.1", p_ab))
        rebind.close()

        # A port already taken by someone else is skipped, not fatal.
        blocker = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        blocker.bind(("127.0.0.1", relay._port_cursor))
        taken = relay._port_cursor
        d = mk("D", client())
        port = relay.register_route_port(a, d)[2]
        assert port != taken and start <= port <= start + 5
        blocker.close()
    finally:
        relay.close()
        for s in (ca, cb, cc):
            s.close()
    await asyncio.sleep(0.05)


def main() -> int:
    tests = [
        test_distinct_ports_and_forwarding, test_release, test_exhaustion, test_disabled_range_is_legacy,
        test_onp2proute_advertises_distinct_ports, test_real_loopback_sockets,
    ]
    failed = 0
    for t in tests:
        try:
            if inspect.iscoroutinefunction(t):
                asyncio.run(t())
            else:
                async def runner(fn=t):
                    fn()
                asyncio.run(runner())
            print(f"PASS - {t.__name__}")
        except Exception as exc:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            print(f"FAIL - {t.__name__}: {exc!r}")
            failed += 1
    print(f"{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
