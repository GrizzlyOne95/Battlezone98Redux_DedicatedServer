#!/usr/bin/env python3
"""
Protocol-parity unit tests for upstream-behavior fixes (2026-08-25 evidence).

Covers three places where the server previously diverged from proven upstream
behavior:

  E1  DoExitLounge is a SILENT state transition (no OnDoExitLounge frame).
  E2  DeleteLobbyData for a nonexistent key: no mutation, no dirty flag,
      no broadcast.
  E3  Unknown protocol verbs are silently ignored - zero outbound frames
      (the old OnStdoutLogEntry reply was client-visible).

Run:
  python protocol_parity_test.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from typing import Any

from server import BZRWebSocketServer, ServerState, UserSession


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


def _attach(state: ServerState, ws: FakeWS, user_id: str) -> UserSession:
    session = UserSession(
        user_id,
        name=user_id,
        player_name=user_id,
        wan_address="203.0.113.10:41001",
        lan_addresses=["192.168.1.10:41001"],
        ws=ws,
    )
    state.sessions_by_ws[ws] = session
    state.sessions_by_user_id[user_id] = session
    return session


async def make_server() -> tuple[BZRWebSocketServer, ServerState]:
    state = ServerState(logging.getLogger("parity-test.state"))
    server = BZRWebSocketServer(state, logging.getLogger("parity-test.ws"))
    return server, state


async def test_e1_do_exit_lounge_is_silent() -> None:
    server, _state = await make_server()
    ws = FakeWS(51001)
    session = _attach(server.state, ws, "PARITY_A")

    session.in_lounge = True
    await server.route_message(ws, json.dumps({"type": "DoExitLounge", "content": 1}))

    assert session.in_lounge is False, "state transition must still happen"
    assert not ws.messages, (
        "DoExitLounge must be silent upstream; sent "
        f"{[m.get('type') for m in ws.messages]}"
    )
    print("E1 DoExitLounge silent: PASS")


async def test_e2_delete_lobby_data_missing_key_is_inert() -> None:
    server, state = await make_server()
    ws = FakeWS(51002)
    session = _attach(state, ws, "PARITY_B")

    await server.handle_create_game(session, {"memberLimit": 8})
    lobby = state.lobbies[session.current_lobby_id]
    assert "name" in lobby.metadata  # created by handle_create_game

    ws.clear()
    dirty_before = state._dirty

    await server.route_message(
        ws, json.dumps({"type": "DeleteLobbyData", "content": {"key": "does-not-exist"}})
    )

    assert not ws.messages, (
        "missing-key delete must broadcast nothing; sent "
        f"{[m.get('type') for m in ws.messages]}"
    )
    assert state._dirty == dirty_before, "missing-key delete must not mark state dirty"
    assert "does-not-exist" not in lobby.metadata

    # Sanity: deleting an EXISTING key still mutates + broadcasts.
    await server.route_message(
        ws, json.dumps({"type": "DeleteLobbyData", "content": {"key": "name"}})
    )
    assert ws.by_type("OnLobbyDataChanged"), "existing-key delete must broadcast"
    print("E2 DeleteLobbyData missing key inert: PASS")


async def test_e3_unknown_verbs_emit_nothing() -> None:
    server, _state = await make_server()
    ws = FakeWS(51003)
    _attach(server.state, ws, "PARITY_C")

    for verb in ("NotARealVerb", "DoSomethingFuture", "OnClientOnlyEvent"):
        ws.clear()
        history_before = list(server.state.protocol_history)
        await server.route_message(ws, json.dumps({"type": verb, "content": {}}))
        outbound = [
            e for e in server.state.protocol_history[len(history_before):]
            if e.get("direction") == "tx"
        ]
        assert not ws.messages, f"{verb} produced frames: {[m.get('type') for m in ws.messages]}"
        assert not outbound, f"{verb} recorded tx events: {outbound}"
    print("E3 unknown verbs silently ignored: PASS")


async def main_async() -> int:
    await test_e1_do_exit_lounge_is_silent()
    await test_e2_delete_lobby_data_missing_key_is_inert()
    await test_e3_unknown_verbs_emit_nothing()
    print("protocol_parity_test: all passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main_async()))
