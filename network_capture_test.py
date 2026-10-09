#!/usr/bin/env python3
"""Maximum capture preserves full evidence while forwarding opaque bytes."""

from __future__ import annotations

import hashlib
import asyncio
import json
import logging
from pathlib import Path
import tempfile
import unittest

from server import (BZRRelayProtocol, MAX_PROTOCOL_HISTORY, ProtocolTraceWriter,
                    RelayTraceWriter, ServerState, UserSession)


class Transport:
    def __init__(self):
        self.sent = []

    def get_extra_info(self, name):
        return ("198.51.100.10", 1339) if name == "sockname" else None

    def sendto(self, data, target):
        self.sent.append((data, target))


def records(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


class CaptureTests(unittest.TestCase):
    def test_full_protocol_archive_outlives_bounded_ui_history(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "protocol.jsonl"
            writer = ProtocolTraceWriter(str(path), "sanitized-test")
            state = ServerState(logging.getLogger("capture-test"), protocol_trace=writer)
            payload = {"opaqueMetadata": "x" * 5000}
            for index in range(MAX_PROTOCOL_HISTORY + 100):
                state.record_protocol_event(channel="ws", direction="rx", peer="127.0.0.1:40001",
                                            msg_type="SetPlayerData", payload={**payload, "index": index})
            writer.close()
            rows = records(path)
            self.assertEqual(len(state.protocol_history), MAX_PROTOCOL_HISTORY)
            self.assertEqual(len(rows), MAX_PROTOCOL_HISTORY + 101)
            self.assertEqual(rows[1]["payload"]["opaqueMetadata"], payload["opaqueMetadata"])
            self.assertEqual(rows[-1]["payload"]["index"], MAX_PROTOCOL_HISTORY + 99)

    def test_credentials_are_redacted_in_payload_and_preview(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "protocol.jsonl"
            writer = ProtocolTraceWriter(str(path), "sanitized-test")
            state = ServerState(logging.getLogger("capture-test"), protocol_trace=writer)
            state.record_protocol_event(channel="ws", direction="rx", peer="127.0.0.1:40001", msg_type="Authorization",
                                        payload={"id": "TEST_USER", "key": "test-key", "nested": [{"ticket": "test-ticket", "token": "test-token"}]})
            state.record_protocol_event(channel="ws", direction="rx", peer="127.0.0.1:40001", msg_type="SetLobbyData",
                                        payload={"key": "password", "value": "test-password"})
            state.record_protocol_event(channel="ws", direction="rx", peer="127.0.0.1:40001", msg_type="SetLobbyData",
                                        payload={"key": "gameSettings", "value": "unchanged opaque settings"})
            writer.close()
            raw = path.read_text()
            for secret in ("test-key", "test-ticket", "test-token", "test-password"):
                self.assertNotIn(secret, raw)
            rows = records(path)
            self.assertEqual(rows[1]["payload"]["id"], "TEST_USER")
            self.assertEqual(rows[2]["payload"]["key"], "password")
            self.assertEqual(rows[3]["payload"]["key"], "gameSettings")
            self.assertEqual(rows[3]["payload"]["value"], "unchanged opaque settings")

    def test_maximum_opaque_capture_matches_byte_exact_forward(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "relay.jsonl"
            writer = RelayTraceWriter(str(path), "sanitized-test", include_payloads=True)
            relay = BZRRelayProtocol(logging.getLogger("capture-test"), ServerState(logging.getLogger("capture-state")), trace_writer=writer)
            transport = Transport()
            relay.connection_made(transport)
            a = UserSession("A", wan_address="203.0.113.1:40001", current_lobby_id=77)
            b = UserSession("B", wan_address="203.0.113.2:40002", current_lobby_id=77)
            self.assertTrue(relay.register_route(a, b)[0])
            data = bytes.fromhex("c000000001a118ac12340000000100000002") + bytes(range(256)) * 4
            relay.datagram_received(data, ("203.0.113.1", 40001))
            writer.close()
            self.assertEqual(transport.sent, [(data, ("203.0.113.2", 40002))])
            row = records(path)[1]
            self.assertEqual(bytes.fromhex(row["datagramHex"]), data)
            self.assertEqual(row["datagramSha256"], hashlib.sha256(data).hexdigest())
            self.assertEqual(row["transport"]["headerHex"], data[:18].hex())
            self.assertEqual(row["route"], {"lobbyId": 77, "users": ["A", "B"], "sourceSide": "a"})
            self.assertEqual(row["relayEndpoint"], "198.51.100.10:1339")

    def test_default_trace_omits_gameplay_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "relay.jsonl"
            writer = RelayTraceWriter(str(path), "sanitized-test")
            data = bytes(range(18)) + b"private opaque gameplay bytes"
            writer.log(direction="a->b", source=("203.0.113.1", 40001), target=("203.0.113.2", 40002),
                       data=data, disposition="forwarded")
            writer.close()
            rows = records(path)
            self.assertFalse(rows[0]["payloadCapture"])
            self.assertNotIn("datagramHex", rows[1])
            self.assertNotIn(data[18:].hex(), path.read_text())

    def test_unmapped_and_socket_errors_are_archived(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "relay.jsonl"
            writer = RelayTraceWriter(str(path), "sanitized-test", include_payloads=True)
            relay = BZRRelayProtocol(logging.getLogger("capture-test"), ServerState(logging.getLogger("capture-state")), trace_writer=writer)
            transport = Transport()
            relay.connection_made(transport)
            data = b"unknown sender opaque datagram"
            relay.datagram_received(data, ("203.0.113.99", 40001))
            relay.error_received(OSError("test socket error"), route_key=("A", "B"), transport=transport)
            writer.close()
            rows = records(path)
            self.assertEqual(transport.sent, [])
            self.assertEqual(rows[1]["event"], "unroutable_datagram")
            self.assertEqual(rows[1]["datagramHex"], data.hex())
            self.assertEqual(rows[2]["event"], "socket_error")
            self.assertEqual(rows[2]["users"], ["A", "B"])
            self.assertEqual(rows[2]["relayEndpoint"], ["198.51.100.10", 1339])


class PairCaptureTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_pair_socket_maximum_capture_and_lifecycle(self):
        class Peer(asyncio.DatagramProtocol):
            def __init__(self):
                self.received = asyncio.Queue()

            def datagram_received(self, data, source):
                self.received.put_nowait((data, source))

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "relay.jsonl"
            writer = RelayTraceWriter(str(path), "sanitized-test", include_payloads=True)
            relay = BZRRelayProtocol(logging.getLogger("capture-test"), ServerState(logging.getLogger("capture-state")),
                                     trace_writer=writer, pair_ports=True)
            loop = asyncio.get_running_loop()
            await loop.create_datagram_endpoint(lambda: relay, local_addr=("127.0.0.1", 0))
            transports = []
            peers = []
            sessions = []
            try:
                for name in ("A", "B"):
                    transport, peer = await loop.create_datagram_endpoint(Peer, local_addr=("127.0.0.1", 0))
                    transports.append(transport)
                    peers.append(peer)
                    port = transport.get_extra_info("sockname")[1]
                    sessions.append(UserSession(name, wan_address=f"127.0.0.1:{port}", current_lobby_id=77))
                ok, reason, port = await relay.allocate_route(*sessions)
                self.assertTrue(ok, reason)
                data = bytes(range(256)) * 5
                transports[0].sendto(data, ("127.0.0.1", port))
                received, source = await asyncio.wait_for(peers[1].received.get(), 2)
                self.assertEqual(received, data)
                self.assertEqual(source, ("127.0.0.1", port))
                relay.remove_user("A")
                rows = records(path)
                events = [r["event"] for r in rows if r.get("event", "").startswith("route_")]
                self.assertEqual(events, ["route_allocated", "route_removed"])
                packet = next(r for r in rows if "disposition" in r)
                self.assertEqual(packet["relayEndpoint"], f"127.0.0.1:{port}")
                self.assertEqual(packet["route"]["users"], ["A", "B"])
                self.assertEqual(bytes.fromhex(packet["datagramHex"]), received)
            finally:
                relay.close()
                for transport in transports:
                    transport.close()
                await asyncio.sleep(.05)
                writer.close()


if __name__ == "__main__":
    unittest.main()
