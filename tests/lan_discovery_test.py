#!/usr/bin/env python3
"""LAN wire-format, resource bounds, and managed-process lifecycle checks."""

from __future__ import annotations

import asyncio
import contextlib
import io
import os
from pathlib import Path
import socket
import struct
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT)]

from lan_discovery import (
    LanDiscoveryProtocol, QUERY_MAGIC, QUERY_SIZE, REPLY_MAGIC,
    discovery_reply, encoded_name, local_source,
)
from server import parse_args

NONCE = b"\x00\xffNONCE!"
QUERY = QUERY_MAGIC + NONCE + bytes(80)


class RecordingTransport:
    def __init__(self):
        self.sent = []

    def sendto(self, data, addr):
        self.sent.append((data, addr))


class WireTests(unittest.TestCase):
    def test_wire_and_nondefault_port(self):
        reply = discovery_reply(QUERY, 31415, encoded_name("Test host"))
        self.assertEqual(reply, REPLY_MAGIC + NONCE + b"\x7a\xb7Test host")
        self.assertLess(len(reply), len(QUERY))

    def test_malformed_queries_are_silent(self):
        for query in (b"", QUERY[:-1], QUERY + b"\x00", b"BZRLAN2?" + QUERY[8:],
                      QUERY[:-1] + b"\x01", b"x" * 65507):
            with self.subTest(query=query[:16]):
                self.assertIsNone(discovery_reply(query, 1337, b"Test"))

    def test_explicit_local_ipv4_only(self):
        for host in ("10.1.2.3", "172.16.0.1", "172.31.255.254", "192.168.1.1", "127.0.0.1", "169.254.2.2"):
            self.assertTrue(local_source(host), host)
        for host in ("172.15.255.255", "172.32.0.1", "192.0.2.1", "198.51.100.1", "203.0.113.1",
                     "100.64.0.1", "0.0.0.0", "255.255.255.255", "::1", "invalid"):
            self.assertFalse(local_source(host), host)

    def test_name_bounds(self):
        self.assertEqual(encoded_name("A" * 64), b"A" * 64)
        self.assertEqual(len(discovery_reply(QUERY, 1337, b"A" * 64)), 82)
        for name in ("", " ", "A" * 65, "line\nfeed", "café", "\x7f"):
            with self.assertRaises(ValueError):
                encoded_name(name)

    def test_rate_limit_and_bounded_cache(self):
        now = [100.0]
        responder = LanDiscoveryProtocol(1337, "Rate test", clock=lambda: now[0])
        transport = RecordingTransport()
        responder.connection_made(transport)
        for _ in range(20):
            responder.datagram_received(QUERY, ("192.168.1.20", 32000))
        self.assertEqual(len(transport.sent), 4)
        now[0] += 0.5
        responder.datagram_received(QUERY, ("192.168.1.20", 32000))
        self.assertEqual(len(transport.sent), 5)
        before = len(transport.sent)
        for number in range(1, 255):
            responder.datagram_received(QUERY, (f"10.0.0.{number}", 32000))
        self.assertLessEqual(len(transport.sent) - before, 64)
        self.assertLessEqual(len(responder.sources), 128)
        responder.datagram_received(QUERY, ("203.0.113.1", 32000))
        self.assertNotIn("203.0.113.1", responder.sources)
        responder.connection_lost(None)
        self.assertEqual(len(responder.sources), 0)
        before = len(transport.sent)
        responder.datagram_received(QUERY, ("127.0.0.1", 32000))
        self.assertEqual(len(transport.sent), before)

    def test_managed_option_guards_and_default_off(self):
        self.assertFalse(parse_args([]).lan_discovery)
        self.assertFalse(parse_args([]).lan_managed)
        valid = ["--lan-managed", "--lan-discovery", "--stdin-control", "--health-port", "0",
                 "--state-file", "scratch/state.json"]
        self.assertTrue(parse_args(valid).lan_managed)
        for args in (["--lan-managed"], ["--lan-name", "x\ny", "--lan-discovery"],
                     ["--lan-discovery", "--lan-discovery-port", "1338"],
                     ["--lan-discovery", "--ws-port", "65535"]):
            with self.subTest(args=args), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    parse_args(args)


def unused_port(kind):
    with socket.socket(socket.AF_INET, kind) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class ManagedTests(unittest.IsolatedAsyncioTestCase):
    async def start_helper(self, folder, discovery_port):
        port = unused_port(socket.SOCK_STREAM)
        udp = unused_port(socket.SOCK_DGRAM)
        helper = os.getenv("BZR_LAN_HELPER_EXE")
        command = [helper] if helper else [sys.executable, str(ROOT / "server.py")]
        process = await asyncio.create_subprocess_exec(
            *command,
            "--ws-host", "127.0.0.1", "--ws-port", str(port),
            "--udp-host", "127.0.0.1", "--udp-port", str(udp), "--disable-relay",
            "--health-port", "0", "--state-file", str(Path(folder) / "state.json"),
            "--audit-log", "", "--stdin-control", "--lan-managed", "--lan-discovery",
            "--lan-discovery-port", str(discovery_port), "--lan-name", "Socket test host",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        return process, port, udp

    async def stop_helper(self, process, *, eof=False):
        if process.returncode is None:
            if eof:
                process.stdin.close()
            else:
                process.stdin.write(b"stop\n")
                await process.stdin.drain()
            try:
                await asyncio.wait_for(process.wait(), 10)
            except TimeoutError:
                process.kill()  # only this test's helper, never the game
                await process.wait()
                raise

    async def test_ready_discovery_shutdown_and_eof(self):
        import websockets
        for eof in (False, True):
            with self.subTest(eof=eof), tempfile.TemporaryDirectory() as folder:
                discovery = unused_port(socket.SOCK_DGRAM)
                process, port, udp = await self.start_helper(folder, discovery)
                try:
                    line = await asyncio.wait_for(process.stdout.readline(), 10)
                    self.assertEqual(line, f"BZRLAN_READY {port}\n".encode())
                    # Readiness guarantees both discovery and lobby are live.
                    async with websockets.connect(f"ws://127.0.0.1:{port}/"):
                        pass
                    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
                        client.setblocking(False)
                        loop = asyncio.get_running_loop()
                        await loop.sock_sendto(client, QUERY, ("127.0.0.1", discovery))
                        reply, source = await asyncio.wait_for(loop.sock_recvfrom(client, QUERY_SIZE), 2)
                        self.assertEqual(source, ("127.0.0.1", discovery))
                        self.assertEqual(reply, REPLY_MAGIC + NONCE + struct.pack("!H", port) + b"Socket test host")
                    await self.stop_helper(process, eof=eof)
                    self.assertEqual(process.returncode, 0, (await process.stderr.read()).decode())
                    self.assertTrue((Path(folder) / "state.json").exists())
                    for number in (discovery, udp):
                        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as released:
                            released.bind(("127.0.0.1", number))
                finally:
                    await self.stop_helper(process)

    async def test_discovery_bind_failure_never_signals_ready(self):
        with tempfile.TemporaryDirectory() as folder, socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as occupied:
            occupied.bind(("127.0.0.1", 0))
            process, port, udp = await self.start_helper(folder, occupied.getsockname()[1])
            try:
                await asyncio.wait_for(process.wait(), 10)
                self.assertNotEqual(process.returncode, 0)
                self.assertNotIn(b"BZRLAN_READY", await process.stdout.read())
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as released:
                    released.bind(("127.0.0.1", udp))
            finally:
                await self.stop_helper(process)


if __name__ == "__main__":
    unittest.main()
