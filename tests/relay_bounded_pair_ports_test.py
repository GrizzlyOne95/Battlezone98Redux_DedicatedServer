#!/usr/bin/env python3
"""Bounded relay allocation, native endpoint identity and failure cleanup.

Real sockets bind loopback only, in a temporary dynamically chosen UDP range.
No games, public listeners, production traffic or saved server state are used.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import logging
import os
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch

from relay_pair_ports_test import FakeWS, Peer
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT)]

from server import BZRRelayProtocol, BZRWebSocketServer, RelayTraceWriter, ServerState, UserSession, parse_args


def reserve_range(count: int) -> list[socket.socket]:
    """Find and reserve contiguous local ports without assuming a fixed free range."""
    for _ in range(30):
        sockets = []
        reserved = False
        try:
            first = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sockets.append(first)
            first.bind(("127.0.0.1", 0))
            start = first.getsockname()[1]
            if start + count - 1 > 65535:
                continue
            for offset in range(1, count):
                other = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sockets.append(other)
                other.bind(("127.0.0.1", start + offset))
            reserved = True
            return sockets
        except OSError:
            pass
        finally:
            if not reserved:
                for sock in sockets:
                    sock.close()
    raise RuntimeError("could not reserve a temporary loopback UDP range")


class BoundedRelayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        held = reserve_range(8)
        self.start = held[0].getsockname()[1]
        self.end = self.start + 7
        self.state = ServerState(logging.getLogger("bounded-state"))
        self.relay = BZRRelayProtocol(logging.getLogger("bounded-relay"), self.state, pair_ports=True,
                                      pair_port_start=self.start, pair_port_end=self.end)
        self.loop = asyncio.get_running_loop()
        await self.loop.create_datagram_endpoint(lambda: self.relay, local_addr=("127.0.0.1", 0))
        for sock in held:
            sock.close()
        self.server = BZRWebSocketServer(self.state, logging.getLogger("bounded-ws"), relay_protocol=self.relay)
        self.sessions = []
        for index in range(4):
            session = UserSession(f"PLAYER_{index}", current_lobby_id=77,
                                  wan_address=f"203.0.113.{index + 1}:{41000 + index}", ws=FakeWS())
            self.sessions.append(session)
            self.state.sessions_by_user_id[session.user_id] = session
            self.state.sessions_by_ws[session.ws] = session

    async def asyncTearDown(self):
        self.relay.close()
        await asyncio.sleep(.02)

    def single_port(self):
        self.relay.pair_port_end = self.start

    async def test_four_peer_mesh_advertises_bounded_ports_and_forwards_exactly(self):
        peers = []
        try:
            for session in self.sessions:
                transport, peer = await self.loop.create_datagram_endpoint(Peer, local_addr=("127.0.0.1", 0))
                peers.append(peer)
                session.wan_address = f"127.0.0.1:{transport.get_extra_info('sockname')[1]}"
            ports = {}
            for a in range(4):
                for b in range(a + 1, 4):
                    await asyncio.gather(
                        self.server.handle_p2p_signal(self.sessions[a], {"player": self.sessions[b].user_id}, route=True),
                        self.server.handle_p2p_signal(self.sessions[b], {"player": self.sessions[a].user_id}, route=True))
                    first = self.sessions[a].ws.route_for(self.sessions[b].user_id)["port"]
                    second = self.sessions[b].ws.route_for(self.sessions[a].user_id)["port"]
                    self.assertEqual(first, second)
                    self.assertTrue(self.start <= first <= self.end)
                    ports[a, b] = first
            self.assertEqual(len(set(ports.values())), 6)
            for (a, b), port in ports.items():
                for source, target in ((a, b), (b, a)):
                    payload = bytes([source, target, 0, 255]) + bytes(range(256)) * 3
                    peers[source].transport.sendto(payload, ("127.0.0.1", port))
                    data, endpoint = await asyncio.wait_for(peers[target].received.get(), 2)
                    self.assertEqual(data, payload)
                    self.assertEqual(endpoint, ("127.0.0.1", port))
                    self.assertTrue(all(peer.received.empty() for peer in peers))
            self.assertEqual(self.relay.metrics()["pairPortRange"], [self.start, self.end])
            self.assertEqual(self.relay.tx_count, 12)
            self.assertEqual(self.relay.drop_count, 0)
        finally:
            for peer in peers:
                peer.transport.close()

    async def test_exhaustion_sends_no_route_and_preserves_existing_pair_and_sessions(self):
        self.single_port()
        a, b, c, _ = self.sessions
        await self.server.handle_p2p_signal(a, {"player": b.user_id}, route=True)
        original = a.ws.route_for(b.user_id)["port"]
        for session in self.sessions:
            session.ws.messages.clear()
        await self.server.handle_p2p_signal(a, {"player": c.user_id}, route=True)
        self.assertFalse(any(m["type"] == "OnP2PRoute" for s in self.sessions for m in s.ws.messages))
        failures = [m["data"] for s in (a, c) for m in s.ws.messages if m["type"] == "OnLobbyMemberP2PConnect"]
        self.assertEqual(len(failures), 2)
        self.assertTrue(all(not f["success"] and "exhausted" in f["relayReason"] for f in failures))
        self.assertEqual(self.relay.metrics()["activeRoutes"], 1)
        self.assertEqual(self.relay.metrics()["activePairPorts"], 1)
        self.assertEqual(self.relay.metrics()["pairPortAllocationFailuresTotal"], 1)
        self.assertEqual(len(self.state.sessions_by_user_id), 4)
        self.assertEqual(await self.relay.allocate_route(b, a), (True, "ok", original))

    async def test_occupied_port_is_skipped(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as blocker:
            blocker.bind(("127.0.0.1", self.start))
            ok, reason, port = await self.relay.allocate_route(*self.sessions[:2])
        self.assertTrue(ok, reason)
        self.assertNotEqual(port, self.start)
        self.assertTrue(self.start <= port <= self.end)

    async def test_all_bind_failures_leave_no_route_or_advertised_endpoint(self):
        self.single_port()
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as blocker:
            blocker.bind(("127.0.0.1", self.start))
            ok, reason, port = await self.relay.allocate_route(*self.sessions[:2])
        self.assertFalse(ok)
        self.assertIsNone(port)
        self.assertIn("bind failure", reason)
        self.assertIn(f"{self.start}-{self.start}", reason)
        self.assertFalse(self.relay.routes or self.relay.pair_transports)
        self.assertEqual(len(self.state.sessions_by_user_id), 4)

    async def test_reserved_probe_and_shared_ports_are_never_allocated(self):
        self.relay.reserved_ports = {self.start}
        ok, reason, port = await self.relay.allocate_route(*self.sessions[:2])
        self.assertTrue(ok, reason)
        self.assertNotEqual(port, self.start)
        self.relay.remove_user(self.sessions[0].user_id)
        self.relay.pair_port_start = self.relay.pair_port_end = self.relay.listen_port
        ok, reason, port = await self.relay.allocate_route(*self.sessions[:2])
        self.assertFalse(ok)
        self.assertIsNone(port)
        self.assertIn("exhausted", reason)

    async def test_release_reuses_port_and_closes_socket(self):
        self.single_port()
        ok, reason, port = await self.relay.allocate_route(*self.sessions[:2])
        self.assertTrue(ok, reason)
        old = next(iter(self.relay.pair_transports.values()))
        self.relay.remove_user(self.sessions[0].user_id)
        self.assertTrue(old.is_closing())
        await asyncio.sleep(.02)
        ok, reason, reused = await self.relay.allocate_route(self.sessions[2], self.sessions[3])
        self.assertTrue(ok, reason)
        self.assertEqual(reused, port)

    async def test_leave_during_bounded_allocation_closes_new_socket(self):
        created = []
        original = self.loop.create_datagram_endpoint
        async def create_and_leave(*args, **kwargs):
            result = await original(*args, **kwargs)
            created.append(result[0])
            self.relay.remove_user(self.sessions[1].user_id)
            self.sessions[1].current_lobby_id = None
            return result
        with patch.object(self.loop, "create_datagram_endpoint", side_effect=create_and_leave):
            ok, reason, port = await self.relay.allocate_route(*self.sessions[:2])
        self.assertFalse(ok)
        self.assertIsNone(port)
        self.assertTrue(created[0].is_closing())
        self.assertFalse(self.relay.routes or self.relay.pair_transports)

    async def test_bounded_failure_is_preserved_in_forensic_trace(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "relay.jsonl"
            writer = RelayTraceWriter(str(path), "bounded-test")
            self.relay.trace_writer = writer
            try:
                self.single_port()
                self.relay.reserved_ports = {self.start}
                await self.relay.allocate_route(*self.sessions[:2])
                rows = [json.loads(line) for line in path.read_text().splitlines()]
                event = next(row for row in rows if row.get("event") == "route_allocation_failed")
                self.assertEqual(event["users"], [s.user_id for s in self.sessions[:2]])
                self.assertIn("exhausted", event["reason"])
            finally:
                self.relay.trace_writer = None
                writer.close()

    async def test_shared_overlap_warns_and_ambiguous_traffic_warning_is_throttled(self):
        self.relay.pair_ports = False
        a, b, c, _ = self.sessions
        await self.relay.allocate_route(a, b)
        with self.assertLogs("bounded-relay", "WARNING") as entries:
            await self.relay.allocate_route(a, c)
            for _ in range(3):
                self.relay.datagram_received(b"opaque", ("203.0.113.1", 41000))
        self.assertEqual(len(entries.records), 2)
        self.assertIn("overlap", entries.output[0])
        self.assertIn("ambiguous", entries.output[1])
        self.assertEqual(self.relay.ambiguous_drop_count, 3)
        self.assertEqual(self.relay.metrics()["sharedRouteWarningsTotal"], 1)


class PairPortArgumentsTests(unittest.TestCase):
    def parse(self, *arguments, env=None):
        with patch.dict(os.environ, env or {}, clear=True), patch.object(sys, "argv", ["server.py", *arguments]):
            return parse_args()

    def test_existing_cli_default_and_ephemeral_opt_in_are_preserved(self):
        self.assertFalse(self.parse().relay_pair_ports)
        args = self.parse("--relay-pair-ports")
        self.assertTrue(args.relay_pair_ports)
        self.assertEqual((args.relay_pair_port_start, args.relay_pair_port_end), (0, 0))

    def test_environment_bounds_do_not_implicitly_enable_pair_mode(self):
        env = {"BZR_RELAY_PAIR_PORT_START": "1340", "BZR_RELAY_PAIR_PORT_END": "1369"}
        self.assertFalse(self.parse(env=env).relay_pair_ports)
        env["BZR_RELAY_PAIR_PORTS"] = "1"
        self.assertTrue(self.parse(env=env).relay_pair_ports)
        self.assertFalse(self.parse("--no-relay-pair-ports", env=env).relay_pair_ports)

    def test_invalid_ranges_and_environment_mode_fail_before_bind(self):
        for start, end in ((0, 1340), (1340, 0), (1341, 1340), (-1, 0), (1, 65536)):
            with self.subTest(start=start, end=end), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    self.parse("--relay-pair-ports", "--relay-pair-port-start", str(start), "--relay-pair-port-end", str(end))
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.parse(env={"BZR_RELAY_PAIR_PORTS": "mistyped"})


if __name__ == "__main__":
    unittest.main()
