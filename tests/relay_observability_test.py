#!/usr/bin/env python3
"""Additive socket submission evidence; fake failures and ephemeral UDP only."""
import asyncio
import json
import logging
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from relay_impairment_test import A, B, FakeState, make_relay
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT)]

from server import BZRRelayProtocol, RelayTraceWriter, UserSession


OPAQUE = b"\x7a\x75\x00\xff\x5f" + bytes(range(256))


class SubmissionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "relay.jsonl")
        self.relay, self.transport, self.scheduled = make_relay(self.path)

    def tearDown(self):
        self.relay.trace_writer.close()
        self.tmp.cleanup()

    def rows(self):
        return [json.loads(line) for line in Path(self.path).read_text().splitlines()]

    def test_immediate_identity_and_legacy_replay(self):
        self.relay.datagram_received(OPAQUE, A)
        rows = self.rows()
        decisions = [r for r in rows if "transport" in r and "disposition" in r]
        sends = [r for r in rows if r.get("event") == "socket_send"]
        self.assertEqual(len(decisions), 1)
        self.assertEqual(len(sends), 1)
        decision, send = decisions[0], sends[0]
        self.assertEqual(send["packetId"], decision["packetId"])
        self.assertEqual(send["socketId"], decision["socketId"])
        self.assertEqual((send["sourceUser"], send["targetUser"]), ("userA", "userB"))
        self.assertEqual(send["outcome"], "submitted")
        self.assertEqual(send["copyIndex"], 0)
        self.assertNotIn("transport", send)
        self.assertNotIn("disposition", send)
        self.assertTrue(rows[0]["sendOutcomeCapture"])
        self.assertEqual(self.transport.sent, [(OPAQUE, B)])

    def test_delayed_duplicates_keep_arrival_context(self):
        # Fake times must follow the real clock: a fixed value earlier than the
        # route's creation time made the route look expired on a freshly booted host.
        base = time.monotonic()
        with patch("server.time.monotonic", return_value=base):
            self.relay.set_impairment("delay=50,dup=100")
            self.relay.datagram_received(OPAQUE, A)
        decision = [r for r in self.rows() if "disposition" in r][0]
        self.assertFalse(any(r.get("event") == "socket_send" for r in self.rows()))
        self.assertEqual(len(self.scheduled), 2)
        self.relay.set_impairment("")
        self.relay.routes.clear()
        with patch("server.time.monotonic", return_value=base + 0.075):
            for delay, callback, args in self.scheduled:
                self.assertAlmostEqual(delay, .05)
                callback(*args)
        sends = [r for r in self.rows() if r.get("event") == "socket_send"]
        self.assertEqual([r["copyIndex"] for r in sends], [0, 1])
        for send in sends:
            self.assertEqual(send["packetId"], decision["packetId"])
            self.assertEqual(send["copyCount"], 2)
            self.assertEqual(send["scheduledDelayMs"], 50)
            self.assertEqual(send["queueWaitMs"], 75)
            self.assertEqual(send["schedulerLagMs"], 25)
            self.assertEqual(send["sourceUser"], "userA")
        self.assertEqual(self.transport.sent, [(OPAQUE, B), (OPAQUE, B)])

    def test_identical_arrivals_have_distinct_ids(self):
        self.relay.datagram_received(OPAQUE, A)
        self.relay.datagram_received(OPAQUE, A)
        packets = [r for r in self.rows() if "disposition" in r]
        sends = [r for r in self.rows() if r.get("event") == "socket_send"]
        self.assertEqual(packets[0]["datagramSha256"], packets[1]["datagramSha256"])
        self.assertNotEqual(packets[0]["packetId"], packets[1]["packetId"])
        self.assertEqual([r["packetId"] for r in packets], [r["packetId"] for r in sends])
        self.assertEqual(sends[0]["socketId"], sends[1]["socketId"])

    def test_delayed_closing_and_exception(self):
        self.relay.set_impairment("delay=50")
        self.relay.datagram_received(OPAQUE, A)
        self.transport.closing = True
        self.scheduled[0][1](*self.scheduled[0][2])
        self.assertEqual(self.relay.impair_late_drop_count, 1)
        self.assertEqual(self.relay.tx_count, 0)
        self.assertEqual(self.rows()[-1]["outcome"], "skipped_closing")
        self.transport.closing = False
        self.relay.datagram_received(OPAQUE, A)
        with patch.object(self.transport, "sendto", side_effect=OSError("test send failure")):
            with self.assertRaisesRegex(OSError, "test send failure"):
                self.scheduled[1][1](*self.scheduled[1][2])
        self.assertEqual(self.rows()[-1]["outcome"], "exception")
        self.assertEqual(self.rows()[-1]["errorType"], "OSError")
        self.assertEqual(self.relay.tx_count, 0)

    def test_immediate_exception_and_unattributed_error(self):
        with patch.object(self.transport, "sendto", side_effect=OSError("immediate failure")):
            with self.assertRaises(OSError):
                self.relay.datagram_received(OPAQUE, A)
        self.assertEqual(self.rows()[-1]["outcome"], "exception")
        self.assertEqual(self.relay.tx_count, 0)
        self.relay.error_received(OSError("async failure"))
        event = self.rows()[-1]
        self.assertEqual(event["event"], "socket_error")
        self.assertEqual(event["packetCorrelation"], "unknown")
        self.assertIsNotNone(event["socketId"])
        self.assertNotIn("packetId", event)

    def test_pending_callbacks_after_capture_close(self):
        self.relay.set_impairment("delay=50,dup=100")
        self.relay.datagram_received(OPAQUE, A)
        before = Path(self.path).read_text()
        self.relay.trace_writer.close()
        self.transport.closing = True
        self.scheduled[0][1](*self.scheduled[0][2])
        self.transport.closing = False
        self.scheduled[1][1](*self.scheduled[1][2])
        self.relay.error_received(OSError("late asynchronous error"))
        self.assertEqual(Path(self.path).read_text(), before)
        self.assertEqual(self.relay.impair_late_drop_count, 1)
        self.assertEqual(self.relay.tx_count, 1)
        self.assertEqual(self.transport.sent, [(OPAQUE, B)])

    def test_drops_and_no_trace_preserve_behavior(self):
        self.relay.set_impairment("loss=100")
        self.relay.datagram_received(OPAQUE, A)
        self.assertEqual(self.rows()[-1]["disposition"], "dropped_impair_loss")
        self.assertFalse(any(r.get("event") == "socket_send" for r in self.rows()))
        self.relay.set_impairment("")
        self.transport.closing = True
        self.relay.datagram_received(OPAQUE, A)
        self.assertEqual(self.transport.sent, [(OPAQUE, B)])
        self.assertEqual(self.rows()[-1]["outcome"], "submitted")
        self.assertTrue(self.rows()[-1]["transportClosing"])
        relay, transport, scheduled = make_relay()
        transport.closing = True  # Immediate path keeps its existing send call.
        relay.datagram_received(OPAQUE, A)
        self.assertEqual(transport.sent, [(OPAQUE, B)])
        self.assertFalse(scheduled)


class Receiver(asyncio.DatagramProtocol):
    def __init__(self):
        self.packets = asyncio.Queue()

    def datagram_received(self, data, addr):
        self.packets.put_nowait((data, addr))


class WireTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_udp_delayed_duplicate_bytes(self):
        loop = asyncio.get_running_loop()
        transports = []
        with tempfile.TemporaryDirectory() as tmp:
            writer = RelayTraceWriter(str(Path(tmp) / "relay.jsonl"), "opaque-wire")
            relay = BZRRelayProtocol(logging.getLogger("wire"), FakeState(), trace_writer=writer)
            try:
                sender, _ = await loop.create_datagram_endpoint(Receiver, local_addr=("127.0.0.1", 0))
                transports.append(sender)
                receiver_transport, receiver = await loop.create_datagram_endpoint(Receiver, local_addr=("127.0.0.1", 0))
                transports.append(receiver_transport)
                relay_transport, _ = await loop.create_datagram_endpoint(lambda: relay, local_addr=("127.0.0.1", 0))
                transports.append(relay_transport)
                for user, transport in (("A", sender), ("B", receiver_transport)):
                    addr = transport.get_extra_info("sockname")
                    session = UserSession(user_id=user, current_lobby_id=1, wan_address=f"{addr[0]}:{addr[1]}")
                    if user == "A":
                        source = session
                    else:
                        self.assertTrue(relay.register_route(source, session)[0])
                relay.set_impairment("delay=10,dup=100")
                sender.sendto(OPAQUE, relay_transport.get_extra_info("sockname"))
                for _ in range(2):
                    data, addr = await asyncio.wait_for(receiver.packets.get(), 2)
                    self.assertEqual(data, OPAQUE)
                    self.assertEqual(addr, relay_transport.get_extra_info("sockname"))
                events = [json.loads(line) for line in Path(writer.path).read_text().splitlines()]
                sends = [r for r in events if r.get("event") == "socket_send"]
                self.assertEqual([r["outcome"] for r in sends], ["submitted", "submitted"])
                self.assertEqual(len({r["packetId"] for r in sends}), 1)
            finally:
                for transport in transports:
                    transport.close()
                await asyncio.sleep(0)
                writer.close()


if __name__ == "__main__":
    unittest.main()
