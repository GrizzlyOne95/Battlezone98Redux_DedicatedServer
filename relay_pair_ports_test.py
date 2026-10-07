#!/usr/bin/env python3
"""Real UDP regression for private multi-client relay association.

Four loopback clients negotiate all six peer pairs through the WebSocket
handler, then send opaque datagrams in all twelve directions. This checks both
the advertised destination port and the native client's source-port identity.
No game processes or externally reachable listeners are used.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from unittest.mock import patch

from server import BZRRelayProtocol, BZRWebSocketServer, ServerState, UserSession


class Peer(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.transport = None
        self.received: asyncio.Queue = asyncio.Queue()

    def connection_made(self, transport) -> None:
        self.transport = transport

    def datagram_received(self, data, addr) -> None:
        self.received.put_nowait((data, addr))


class FakeWS:
    def __init__(self) -> None:
        self.messages = []

    async def send(self, payload) -> None:
        self.messages.append(json.loads(payload))

    def route_for(self, user_id):
        return [m["data"] for m in self.messages
                if m["type"] == "OnP2PRoute" and m["data"]["player"] == user_id][-1]


async def main() -> None:
    logging.basicConfig(level=logging.CRITICAL)
    logger = logging.getLogger("pair-relay-test")
    state = ServerState(logger)
    relay = BZRRelayProtocol(logger, state, pair_ports=True)
    loop = asyncio.get_running_loop()
    await loop.create_datagram_endpoint(lambda: relay, local_addr=("127.0.0.1", 0))
    server = BZRWebSocketServer(state, logger, relay_protocol=relay)
    peers = []
    sessions = []
    try:
        for index in range(4):
            transport, peer = await loop.create_datagram_endpoint(Peer, local_addr=("127.0.0.1", 0))
            peers.append(peer)
            port = transport.get_extra_info("sockname")[1]
            session = UserSession(f"PLAYER_{index}", name=f"Player {index}",
                                  current_lobby_id=77, wan_address=f"127.0.0.1:{port}", ws=FakeWS())
            sessions.append(session)
            state.sessions_by_user_id[session.user_id] = session
            state.sessions_by_ws[session.ws] = session

        ports = {}
        # Negotiate a three-player mesh first, then extend it to four.
        for total in (3, 4):
            for a in range(total):
                for b in range(a + 1, total):
                    await asyncio.gather(
                        server.handle_p2p_signal(sessions[a], {"player": sessions[b].user_id}, route=True),
                        server.handle_p2p_signal(sessions[b], {"player": sessions[a].user_id}, route=True),
                    )
                    to_a = sessions[a].ws.route_for(sessions[b].user_id)
                    to_b = sessions[b].ws.route_for(sessions[a].user_id)
                    assert to_a["port"] == to_b["port"]
                    port = to_a["port"]
                    if (a, b) in ports:
                        assert ports[a, b] == port, "route retry changed the native peer endpoint"
                    ports[a, b] = port
            assert len(set(ports.values())) == len(ports), "peer pairs share a relay port"
            assert relay.metrics()["activePairPorts"] == total * (total - 1) // 2
            for (a, b), port in ports.items():
                for source, target in ((a, b), (b, a)):
                    payload = bytes([source, target, 0, 255]) + bytes(range(256)) * 3
                    peers[source].transport.sendto(payload, ("127.0.0.1", port))
                    data, sender = await asyncio.wait_for(peers[target].received.get(), 2)
                    assert data == payload, "opaque payload changed"
                    assert sender == ("127.0.0.1", port), "native source endpoint is not the advertised pair"
                    assert all(peer.received.empty() for peer in peers), "packet leaked to another peer"
            print(f"PASS - {total} peers: every pair forwards byte-exactly in both directions, stable distinct ports")

        # A real fourth endpoint cannot inject into an unrelated pair, even
        # though it has the same IP as both intended peers.
        dropped_before = relay.drop_count
        peers[3].transport.sendto(b"unrelated peer", ("127.0.0.1", ports[0, 1]))
        peers[0].transport.sendto(b"unassociated shared listener", ("127.0.0.1", relay.listen_port))
        await asyncio.sleep(0.05)
        assert all(peer.received.empty() for peer in peers)
        assert relay.drop_count == dropped_before + 2
        print("PASS - wrong pair and shared-listener datagrams fail closed")

        old = dict(relay.pair_transports)
        relay.remove_user(sessions[3].user_id, lobby_id=77)
        assert relay.metrics()["activeRoutes"] == 3
        assert all(t.is_closing() for k, t in old.items() if sessions[3].user_id in k)
        key = tuple(sorted((sessions[0].user_id, sessions[1].user_id)))
        expired = relay.pair_transports[key]
        relay.routes[key].last_activity_monotonic = time.monotonic() - relay.route_ttl_seconds - 1
        assert relay.metrics()["activeRoutes"] == 2
        assert expired.is_closing()
        remaining = list(relay.pair_transports.values())
        relay.set_enabled(False)
        assert relay.metrics()["activeRoutes"] == 0 and not relay.pair_transports
        assert all(t.is_closing() for t in remaining)
        print("PASS - leave, expiration and disable close the associated pair sockets")

        # Allocation failure must never advertise a nonexistent relay endpoint.
        relay.set_enabled(True)
        sessions[0].ws.messages.clear()
        sessions[1].ws.messages.clear()
        with patch.object(loop, "create_datagram_endpoint", side_effect=OSError("test allocation failure")):
            await server.handle_p2p_signal(sessions[0], {"player": sessions[1].user_id}, route=True)
        assert not any(m["type"] == "OnP2PRoute" for s in sessions[:2] for m in s.ws.messages)
        assert relay.metrics()["activeRoutes"] == 0
        print("PASS - failed socket allocation sends no OnP2PRoute and leaves no route")

        # A leave while asynchronous allocation is pending must not resurrect
        # the route or leak its newly bound socket.
        created = []
        original_create = loop.create_datagram_endpoint

        async def create_and_leave(*args, **kwargs):
            result = await original_create(*args, **kwargs)
            created.append(result[0])
            relay.remove_user(sessions[1].user_id)
            sessions[1].current_lobby_id = None
            return result

        with patch.object(loop, "create_datagram_endpoint", side_effect=create_and_leave):
            ok, _, port = await relay.allocate_route(sessions[0], sessions[1])
        assert not ok and port is None and created[0].is_closing()
        assert not relay.routes and not relay.pair_transports
        print("PASS - leave during allocation cannot resurrect a route")

        sessions[1].current_lobby_id = 77
        ok, reason, port = await relay.allocate_route(sessions[0], sessions[1])
        assert ok, reason
        remaining = list(relay.pair_transports.values())
        base = relay.transport
        relay.close()
        assert all(t.is_closing() for t in remaining) and base.is_closing()
        assert not relay.enabled and not relay.routes and not relay.pair_transports
        print("PASS - shutdown closes base and pair sockets")
    finally:
        relay.close()
        for peer in peers:
            peer.transport.close()
        await asyncio.sleep(0.05)
    print("All private pair-port relay checks passed")


if __name__ == "__main__":
    asyncio.run(main())
