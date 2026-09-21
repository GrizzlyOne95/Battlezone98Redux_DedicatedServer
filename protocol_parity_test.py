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
  E4  Pre-auth verbs are silently ignored - zero outbound frames (the old
      "Authorization required" reply was client-visible).
  E5  A second Authorization on a bound socket is silently ignored - zero
      outbound frames, identity not rebound.
  E6  Missing authtype closes the socket (4002) with no message frame and
      no session.
  E7  Steam without a ticket yields OnAuthorization success:false with the
      requested id, then close 4002, and no session.
  E8  Gog without a ticket is accepted and the client-supplied id is echoed
      back verbatim (no generated replacement).

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
        self.close_codes: list[int] = []

    async def send(self, payload: str) -> None:
        self.messages.append(json.loads(payload))

    async def close(self, code: int = 1000) -> None:
        self.close_codes.append(code)

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


async def _auth_envelope(**fields: Any) -> str:
    content = {"authtype": "web", "id": "0", "apiVer": "0.0"}
    content.update(fields)
    return json.dumps({"type": "Authorization", "content": content})


async def test_e4_preauth_verbs_emit_nothing() -> None:
    server, state = await make_server()
    ws = FakeWS(51004)

    for raw in (
        json.dumps({"type": "DoEnterLounge", "content": True}),
        json.dumps({"type": "DoJoinLobby", "content": {"id": "1"}}),
        json.dumps({"type": "NotARealVerb", "content": {}}),
    ):
        await server.route_message(ws, raw)

    assert not ws.messages, (
        "pre-auth verbs must be silent upstream; sent "
        f"{[m.get('type') for m in ws.messages]}"
    )
    assert ws not in state.sessions_by_ws, "pre-auth traffic must not create a session"
    assert not ws.close_codes, "pre-auth traffic must not close the socket"
    print("E4 pre-auth verbs silently ignored: PASS")


async def test_e5_second_authorization_ignored() -> None:
    server, state = await make_server()
    ws = FakeWS(51005)

    await server.route_message(ws, await _auth_envelope(name="First"))
    first = state.sessions_by_ws[ws]
    assert ws.by_type("OnAuthorization"), "first Authorization must succeed"

    ws.clear()
    await server.route_message(ws, await _auth_envelope(name="Second"))

    assert not ws.messages, (
        "second Authorization must be silent upstream; sent "
        f"{[m.get('type') for m in ws.messages]}"
    )
    assert state.sessions_by_ws[ws] is first, "identity must not be rebound"
    assert first.name == "First", "original identity must be retained"
    print("E5 second Authorization ignored: PASS")


async def test_e6_missing_authtype_closes_4002() -> None:
    server, state = await make_server()
    ws = FakeWS(51006)

    await server.route_message(
        ws, json.dumps({"type": "Authorization", "content": {"id": "0"}})
    )

    assert not ws.messages, "missing authtype must send no message frame"
    assert ws.close_codes == [4002], f"expected close 4002, got {ws.close_codes}"
    assert ws not in state.sessions_by_ws, "no session may be created"
    print("E6 missing authtype closes 4002: PASS")


async def test_e7_steam_without_ticket_fails_then_closes() -> None:
    server, state = await make_server()
    ws = FakeWS(51007)

    await server.route_message(
        ws,
        json.dumps(
            {
                "type": "Authorization",
                "content": {"authtype": "steam", "id": "S123", "clientVersion": "2.2.301"},
            }
        ),
    )

    failures = ws.by_type("OnAuthorization")
    assert len(failures) == 1, f"expected one OnAuthorization, got {len(failures)}"
    data = failures[0]["data"]
    assert data["success"] is False, "steam without ticket must fail"
    assert data["id"] == "S123", "requested id must be echoed"
    assert ws.close_codes == [4002], f"expected close 4002, got {ws.close_codes}"
    assert ws not in state.sessions_by_ws, "no session may be created"
    print("E7 steam without ticket fails then closes: PASS")


async def test_e8_gog_id_echoed_verbatim() -> None:
    server, state = await make_server()
    ws = FakeWS(51008)

    await server.route_message(
        ws,
        json.dumps(
            {"type": "Authorization", "content": {"authtype": "gog", "id": "G999"}}
        ),
    )

    grants = ws.by_type("OnAuthorization")
    assert len(grants) == 1, f"expected one OnAuthorization, got {len(grants)}"
    assert grants[0]["data"]["success"] is True, "gog without ticket is accepted"
    assert grants[0]["data"]["id"] == "G999", "gog id must echo verbatim"
    assert not ws.close_codes, "accepted gog session must stay open"
    assert state.sessions_by_ws[ws].user_id == "G999"
    print("E8 gog id echoed verbatim: PASS")


async def main_async() -> int:
    await test_e1_do_exit_lounge_is_silent()
    await test_e2_delete_lobby_data_missing_key_is_inert()
    await test_e3_unknown_verbs_emit_nothing()
    await test_e4_preauth_verbs_emit_nothing()
    await test_e5_second_authorization_ignored()
    await test_e6_missing_authtype_closes_4002()
    await test_e7_steam_without_ticket_fails_then_closes()
    await test_e8_gog_id_echoed_verbatim()
    print("protocol_parity_test: all passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main_async()))
