#!/usr/bin/env python3
"""Offline regression checks for operator-facing robustness fixes in server.py.

No ports are opened except one throwaway UDP socket for the bind-error message.

    python robustness_test.py
"""

from __future__ import annotations

import asyncio
import errno
import json
import logging
from pathlib import Path
import socket
import tempfile
from types import SimpleNamespace
from typing import Any
import unittest

import server
from server import BZRWebSocketServer, ServerState, describe_bind_error, ip_in_networks, serve_health_http


class FakeWS:
    def __init__(self, ip: str = "203.0.113.9") -> None:
        self.remote_address = (ip, 50000)
        self.request = SimpleNamespace(headers={})
        self.sent: list[dict[str, Any]] = []

    async def ping(self) -> "asyncio.Future[float]":
        fut = asyncio.get_running_loop().create_future()
        fut.set_result(0.0)
        return fut

    async def send(self, text: str) -> None:
        self.sent.append(json.loads(text))

    async def close(self, code: int = 1000) -> None:
        pass


def make_server(tmp: Path) -> BZRWebSocketServer:
    state = ServerState(logging.getLogger("robustness"), state_file=str(tmp / "state.json"))
    state.ensure_default_lobby()
    return BZRWebSocketServer(state, logging.getLogger("robustness.ws"))


async def login(ws_server: BZRWebSocketServer, ws: FakeWS, uid: str, authtype: str = "gog") -> None:
    await ws_server.route_message(ws, json.dumps({"type": "Authorization",
                                                  "content": {"authtype": authtype, "id": uid, "name": uid}}))


class StateFileTests(unittest.TestCase):
    def test_corrupt_state_is_kept_aside_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "server_state.json"
            broken = '{"version": 1, "access": {"bannedIds": {"evil": {}}}, "lobbies": ['
            path.write_text(broken, encoding="utf-8")
            state = ServerState(logging.getLogger("t"), state_file=str(path))
            logging.disable(logging.CRITICAL)
            try:
                state.load_state()
            finally:
                logging.disable(logging.NOTSET)
            backups = list(Path(tmp).glob("server_state.json.corrupt-*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_text(encoding="utf-8"), broken)
            state.save_state(force=True)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["version"], 1)
            self.assertEqual(backups[0].read_text(encoding="utf-8"), broken)

    def test_unsupported_version_is_kept_aside(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "server_state.json"
            path.write_text('{"version": 99}', encoding="utf-8")
            logging.disable(logging.CRITICAL)
            try:
                ServerState(logging.getLogger("t"), state_file=str(path)).load_state()
            finally:
                logging.disable(logging.NOTSET)
            self.assertFalse(path.exists())
            self.assertEqual(len(list(Path(tmp).glob("server_state.json.unsupported-version-*"))), 1)


class LobbyTests(unittest.TestCase):
    def test_password_is_never_broadcast_but_still_checked(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                srv = make_server(Path(tmp))
                host, spy = FakeWS("198.51.100.1"), FakeWS("198.51.100.2")
                await login(srv, host, "111")
                await login(srv, spy, "222")
                await srv.route_message(host, json.dumps({"type": "CreateLobby", "content": {
                    "name": "~game~pub~~Secret", "password": "hunter2", "memberLimit": 4}}))
                self.assertNotIn("hunter2", json.dumps(spy.sent))
                lobby = next(l for l in srv.state.lobbies.values() if l.password)
                self.assertEqual(srv.state.serialize_lobby(lobby)["password"], server.LOBBY_PASSWORD_PLACEHOLDER)
                for attempt, ok in ((server.LOBBY_PASSWORD_PLACEHOLDER, False), ("hunter2", True)):
                    spy.sent.clear()
                    await srv.route_message(spy, json.dumps({"type": "DoJoinLobby",
                                                             "content": {"id": lobby.lobby_id, "password": attempt}}))
                    joined = [m["data"] for m in spy.sent if m["type"] == "OnLobbyJoined"][-1]
                    self.assertEqual(joined["success"], ok)
        asyncio.run(run())

    def test_malformed_message_does_not_drop_the_connection(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                srv = make_server(Path(tmp))
                ws = FakeWS()
                await login(srv, ws, "333")
                messages = [json.dumps({"type": "CreateLobby", "content": {"name": "x", "memberLimit": "four"}}),
                            json.dumps({"type": "GetLobbyList"})]

                class Stream(FakeWS):
                    def __aiter__(self):
                        return self._gen()

                    async def _gen(self):
                        for m in messages:
                            yield m
                stream = Stream()
                srv.state.sessions_by_ws[stream] = srv.state.sessions_by_ws.pop(ws)
                srv.state.sessions_by_ws[stream].ws = stream
                logging.disable(logging.CRITICAL)
                try:
                    await srv.handler(stream)
                finally:
                    logging.disable(logging.NOTSET)
                self.assertIn("OnGetLobbyList", [m["type"] for m in stream.sent])
        asyncio.run(run())


class AuthTests(unittest.TestCase):
    def test_gog_placeholder_ids_get_unique_server_ids(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                srv = make_server(Path(tmp))
                a, b = FakeWS("198.51.100.3"), FakeWS("198.51.100.4")
                await login(srv, a, "")
                await login(srv, b, "0")
                ids = [srv.state.sessions_by_ws[w].user_id for w in (a, b)]
                self.assertTrue(all(ids))
                self.assertNotEqual(ids[0], ids[1])
                self.assertNotIn("", srv.state.sessions_by_user_id)
        asyncio.run(run())

    def test_switching_to_open_admits_waiting_players_without_approving_them(self):
        async def run():
            access = server.AccessControl(logging.getLogger("t"), lambda: None)
            access.mode = "approve"
            fut = access.add_pending("444", "Pat", "198.51.100.5", "not approved")
            access.set_mode("open")
            self.assertTrue(fut.result())
            self.assertNotIn("444", access.approved)
            access.set_mode("approve")
            self.assertEqual(access.decide("444", "198.51.100.5")[0], "pending")
        asyncio.run(run())


class StatusPortTests(unittest.TestCase):
    def request(self, path: str, peer: str, detail_hosts: tuple[str, ...] = ()) -> tuple[int, dict]:
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                srv = make_server(Path(tmp))
                reader = asyncio.StreamReader()
                reader.feed_data(f"GET {path} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
                reader.feed_eof()
                out = bytearray()

                class Writer:
                    def get_extra_info(self, name):
                        return (peer, 40000) if name == "peername" else None

                    def write(self, data):
                        out.extend(data)

                    async def drain(self):
                        pass

                    def close(self):
                        pass

                    async def wait_closed(self):
                        pass

                udp = SimpleNamespace(rx_count=0, tx_count=0)
                await serve_health_http(reader, Writer(), state=srv.state, ws_server=srv, udp_protocol=udp,
                                        relay_protocol=None, started_monotonic=0.0, detail_hosts=detail_hosts)
                head, _, body = bytes(out).partition(b"\r\n\r\n")
                return int(head.split()[1]), json.loads(body)
        return asyncio.run(run())

    def test_player_details_are_local_only(self):
        for path in ("/access", "/lobbies", "/captures"):
            self.assertEqual(self.request(path, "203.0.113.50")[0], 403, path)
            self.assertEqual(self.request(path, "127.0.0.1")[0], 200, path)
        self.assertEqual(self.request("/lobbies", "172.17.0.1", ("172.16.0.0/12",))[0], 200)
        self.assertEqual(self.request("/health", "203.0.113.50")[0], 200)

    def test_ip_in_networks_handles_mapped_and_bad_entries(self):
        self.assertTrue(ip_in_networks("::ffff:10.1.2.3", ["10.0.0.0/8"]))
        self.assertFalse(ip_in_networks("10.1.2.3", ["not-a-network", "192.168.0.0/16"]))
        self.assertFalse(ip_in_networks("", ["0.0.0.0/0"]))


class StartupTests(unittest.TestCase):
    def test_port_in_use_names_the_port(self):
        holder = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        holder.bind(("127.0.0.1", 0))
        port = holder.getsockname()[1]
        other = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            with self.assertRaises(OSError) as ctx:
                other.bind(("127.0.0.1", port))
            message = describe_bind_error("handshake UDP", "127.0.0.1", port, ctx.exception)
        finally:
            other.close()
            holder.close()
        self.assertIn(f"port {port}", message)
        self.assertIn("already in use", message)
        denied = describe_bind_error("relay UDP", "0.0.0.0", 80, OSError(errno.EACCES, "denied"))
        self.assertIn("permission denied", denied)


if __name__ == "__main__":
    unittest.main()
