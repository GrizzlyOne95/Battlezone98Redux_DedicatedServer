#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from server import (
    BZRRelayProtocol,
    BZRWebSocketServer,
    LobbyState,
    ServerState,
    UserSession,
    decode_bzr_udp_datagram,
)


class FakeWS:
    def __init__(self, port: int) -> None:
        self.remote_address = ("127.0.0.1", port)
        self.messages: list[dict[str, Any]] = []

    async def send(self, payload: str) -> None:
        self.messages.append(json.loads(payload))

    def clear(self) -> None:
        self.messages.clear()

    def by_type(self, message_type: str) -> list[dict[str, Any]]:
        return [m for m in self.messages if m.get("type") == message_type]


class FakeDatagramTransport:
    def __init__(self, port: int = 1339) -> None:
        self.port = port
        self.sent: list[tuple[bytes, tuple[str, int]]] = []

    def get_extra_info(self, name: str) -> Any:
        if name == "sockname":
            return ("0.0.0.0", self.port)
        return None

    def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
        self.sent.append((bytes(data), addr))


def attach_session(state: ServerState, session: UserSession) -> None:
    state.sessions_by_ws[session.ws] = session
    state.sessions_by_user_id[session.user_id] = session


def last_data(ws: FakeWS, message_type: str) -> dict[str, Any]:
    matches = ws.by_type(message_type)
    assert matches, f"missing {message_type}; saw {[m.get('type') for m in ws.messages]}"
    return matches[-1].get("data", {})


async def test_stock_ws_contracts() -> None:
    state = ServerState(logging.getLogger("stock-test.state"))
    fake_udp = FakeDatagramTransport(1339)
    relay = BZRRelayProtocol(logging.getLogger("stock-test.relay"), state)
    relay.connection_made(fake_udp)  # type: ignore[arg-type]
    server = BZRWebSocketServer(state, logging.getLogger("stock-test.ws"), relay_protocol=relay)

    ws_a = FakeWS(50001)
    ws_b = FakeWS(50002)
    a = UserSession(
        "PLAYER_A",
        name="Player A",
        player_name="Player A",
        wan_address="203.0.113.10:41001",
        lan_addresses=["192.168.1.10:41001"],
        ws=ws_a,
    )
    b = UserSession(
        "PLAYER_B",
        name="Player B",
        player_name="Player B",
        wan_address="198.51.100.20:42002",
        lan_addresses=["192.168.1.20:42002"],
        ws=ws_b,
    )
    attach_session(state, a)
    attach_session(state, b)

    # The actual DoEnterLounge builder serializes JSON number 1, not an empty object.
    await server.route_message(ws_a, json.dumps({"type": "DoEnterLounge", "content": 1}))
    assert a.in_lounge is True
    assert ws_a.by_type("OnDoEnterLounge")
    ws_a.clear()
    ws_b.clear()

    # Match the stock CreateGame builder: only memberLimit/isPrivate/password.
    await server.handle_create_game(a, {"memberLimit": 8, "isPrivate": False, "password": ""})
    assert a.current_lobby_id is not None
    lobby = state.lobbies[a.current_lobby_id]
    assert lobby.metadata["name"].startswith("~game~")

    await server.handle_join_lobby(b, {"id": lobby.lobby_id, "password": ""})
    joined = last_data(ws_b, "OnLobbyJoined")
    assert joined["reasonCode"] == 0
    assert joined["lobbyId"] == lobby.lobby_id
    assert joined["lobby"]["lobbyId"] == lobby.lobby_id
    assert joined["lobby"]["users"]["PLAYER_A"]["userId"] == "PLAYER_A"
    assert joined["lobby"]["users"]["PLAYER_B"]["userId"] == "PLAYER_B"

    ws_a.clear()
    ws_b.clear()
    await server.handle_chat(a, "opaque matchmaking test")
    chat = last_data(ws_b, "OnChatMessage")
    assert chat["speakerId"] == "PLAYER_A"
    assert chat["lobbyId"] == lobby.lobby_id
    assert chat["isChat"] is False  # game lobby, not a ~chat~ lobby

    # Relay enable/disable is an admin control now that it changes a real
    # network service. A normal player must not be able to disable it globally.
    ws_a.clear()
    await server.route_message(ws_a, json.dumps({"type": "SetRelay", "content": {"toggle": False}}))
    assert relay.enabled is True
    assert ws_a.by_type("OnStdoutLogEntry")

    a.is_admin = True
    await server.route_message(ws_a, json.dumps({"type": "SetRelay", "content": {"toggle": False}}))
    assert relay.enabled is False
    await server.route_message(ws_a, json.dumps({"type": "SetRelay", "content": {"toggle": True}}))
    assert relay.enabled is True
    a.is_admin = False

    ws_a.clear()
    ws_b.clear()
    await server.handle_p2p_signal(a, {"player": "PLAYER_B"}, route=True)
    route_to_a = last_data(ws_a, "OnP2PRoute")
    route_to_b = last_data(ws_b, "OnP2PRoute")
    assert route_to_a["player"] == "PLAYER_B" and route_to_a["port"] == 1339
    assert route_to_b["player"] == "PLAYER_A" and route_to_b["port"] == 1339
    assert relay.metrics()["activeRoutes"] == 1

    # A server without a live relay must fail the route request, not advertise 1339.
    ws_a.clear()
    ws_b.clear()
    no_relay_server = BZRWebSocketServer(state, logging.getLogger("stock-test.no-relay"), relay_protocol=None)
    await no_relay_server.handle_p2p_signal(a, {"player": "PLAYER_B"}, route=True)
    assert not ws_a.by_type("OnP2PRoute")
    failure = last_data(ws_a, "OnLobbyMemberP2PConnect")
    assert failure["success"] is False
    assert "relayReason" in failure


async def test_opaque_relay() -> None:
    state = ServerState(logging.getLogger("relay-test.state"))
    fake_udp = FakeDatagramTransport(1339)
    relay = BZRRelayProtocol(logging.getLogger("relay-test.relay"), state)
    relay.connection_made(fake_udp)  # type: ignore[arg-type]

    a = UserSession("A", wan_address="203.0.113.1:41001", current_lobby_id=77)
    b = UserSession("B", wan_address="198.51.100.2:42002", current_lobby_id=77)
    ok, reason = relay.register_route(a, b)
    assert ok, reason

    payload = bytes.fromhex("c0030000019d1199210b00000000000012345353")
    relay.datagram_received(payload, ("203.0.113.1", 41001))
    assert fake_udp.sent == [(payload, ("198.51.100.2", 42002))]
    assert fake_udp.sent[0][0] is not payload or fake_udp.sent[0][0] == payload

    # Registering a second target for the same endpoint makes a raw datagram
    # ambiguous without the stock association envelope. It must be dropped.
    c = UserSession("C", wan_address="192.0.2.3:43003", current_lobby_id=77)
    ok, reason = relay.register_route(a, c)
    assert ok, reason
    fake_udp.sent.clear()
    ambiguous_before = relay.ambiguous_drop_count
    relay.datagram_received(payload, ("203.0.113.1", 41001))
    assert fake_udp.sent == []
    assert relay.ambiguous_drop_count == ambiguous_before + 1


def test_transport_header() -> None:
    raw = bytes.fromhex("40000000019d1199a1dc000000090000beef4b41")
    decoded = decode_bzr_udp_datagram(raw)
    header = decoded["transportHeader"]
    assert header["headerBytes"] == 18
    assert header["payloadOffset"] == 18
    assert header["sequence"] == 0xBEEF
    assert header["sequenceEncoding"] == "u16-be"
    assert raw[decoded["payloadOffset"]:] == b"KA"

    wrap_zero = bytearray(raw)
    wrap_zero[16:18] = b"\x00\x00"
    assert decode_bzr_udp_datagram(bytes(wrap_zero))["transportHeader"]["sequence"] == 0
    wrap_max = bytearray(raw)
    wrap_max[16:18] = b"\xff\xff"
    assert decode_bzr_udp_datagram(bytes(wrap_max))["transportHeader"]["sequence"] == 65535


async def main() -> None:
    test_transport_header()
    await test_stock_ws_contracts()
    await test_opaque_relay()
    print("PASS: stock-client WS contracts, BZRNet header, and opaque relay")


if __name__ == "__main__":
    asyncio.run(main())
