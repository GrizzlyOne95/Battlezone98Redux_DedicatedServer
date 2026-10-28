#!/usr/bin/env python3
"""Offline checks for the admin/moderation hardening in server.py.

Drives BZRWebSocketServer in-process with fake sockets (no ports, no sleeps):
name sanitizing, client-version policy, chat flood limit + auto-mute, ban
expiry, mute, admin-key transport policy, ticket-withheld marker, and that the
audit log never contains credentials.

Usage:
  python admin_hardening_test.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import server
from server import (
    AuditLog,
    BZRWebSocketServer,
    HardeningConfig,
    LobbyState,
    ServerState,
    parse_duration,
    sanitize_display_name,
)

ADMIN_KEY = "k3y-SUPERSECRET-admin"
TICKET = "TICKET-SECRET-0123456789"
_failures: list[str] = []


def check(cond: bool, label: str) -> None:
    print(f"{'PASS' if cond else 'FAIL'}: {label}")
    if not cond:
        _failures.append(label)


class FakeWS:
    def __init__(self, ip: str = "203.0.113.9", headers: dict[str, str] | None = None) -> None:
        self.remote_address = (ip, 50000)
        self.request = SimpleNamespace(headers=headers or {})
        self.sent: list[dict[str, Any]] = []
        self.close_code: int | None = None
        self.alive = True

    async def ping(self) -> "asyncio.Future[float]":
        # Live sockets answer at once; dead ones never do.
        fut = asyncio.get_running_loop().create_future()
        if self.alive:
            fut.set_result(0.0)
        return fut

    async def send(self, text: str) -> None:
        self.sent.append(json.loads(text))

    async def close(self, code: int = 1000) -> None:
        self.close_code = code

    def types(self) -> list[str]:
        return [m["type"] for m in self.sent]

    def chats(self) -> list[str]:
        return [m["data"]["text"] for m in self.sent if m["type"] == "OnChatMessage"]


class Rig:
    def __init__(self, tmp: Path, **cfg: Any) -> None:
        self.audit_path = tmp / "audit.log"
        self.audit = AuditLog(str(self.audit_path), max_bytes=1_000_000)
        self.state = ServerState(logging.getLogger("t"), state_file=str(tmp / "state.json"),
                                 admin_key=ADMIN_KEY, admin_ids={"ADMINID"}, audit=self.audit)
        self.clock = [1_000_000.0]
        self.state.access.clock = lambda: self.clock[0]
        self.ws_server = BZRWebSocketServer(self.state, logging.getLogger("t.ws"), hardening=HardeningConfig(**cfg))

    async def login(self, uid: str = "U1", name: str = "Alice", *, ip: str = "203.0.113.9",
                    headers: dict[str, str] | None = None, **content: Any) -> tuple[FakeWS, Any]:
        ws = FakeWS(ip, headers)
        body = {"authtype": "gog", "id": uid, "name": name, "clientVersion": "2.2.301", **content}
        await self.ws_server.route_message(ws, json.dumps({"type": "Authorization", "content": body}))
        return ws, self.state.sessions_by_ws.get(ws)

    async def chat(self, ws: FakeWS, text: str) -> None:
        await self.ws_server.route_message(ws, json.dumps({"type": "DoSendChat", "content": text}))

    def audit_records(self) -> list[dict[str, Any]]:
        if not self.audit_path.exists():
            return []
        return [json.loads(line) for line in self.audit_path.read_text(encoding="utf-8").splitlines() if line]

    def events(self, name: str) -> list[dict[str, Any]]:
        return [r for r in self.audit_records() if r["event"] == name]


async def test_names(tmp: Path) -> None:
    clean, notes = sanitize_display_name("‮evil​\x07  na\tme ﻿", max_length=32)
    check(clean == "evil na me" and "stripped" in notes, f"control/bidi/zero-width stripped, whitespace collapsed ({clean!r})")
    clean, notes = sanitize_display_name("x" * 80, max_length=32)
    check(len(clean) == 32 and "truncated" in notes, "long names are capped at 32")
    check(sanitize_display_name("​‮  ")[0] == "", "invisible-only name cleans to empty")
    check(sanitize_display_name("  Ad​min ", reserved=("admin",))[0] == "", "reserved word defeated by zero-width padding")
    check(sanitize_display_name("Admiral", reserved=("admin",))[0] == "Admiral", "non-reserved lookalike prefix is kept")
    check(sanitize_display_name("Аdmin", reserved=("Admin",))[0] != "", "documented limit: Cyrillic homoglyphs are not folded")

    rig = Rig(tmp)
    ws, sess = await rig.login("U100", "‮Bob​")
    check(sess is not None and sess.name == "Bob", "authorization sanitizes the stored name")
    auth = next(m for m in ws.sent if m["type"] == "OnAuthorization")["data"]
    check(auth["name"] == "Bob" and auth["success"] is True, "OnAuthorization carries the cleaned name")
    _, sess = await rig.login("U101", "​​")
    check(sess is not None and sess.name == "U101", "empty-after-clean falls back to the id (admitted, not denied)")
    _, sess = await rig.login("U102", "SERVER")
    check(sess is not None and sess.name == "Player-U102", "reserved name is replaced, not denied")
    _, sess = await rig.login("ADMINID", "Admin")
    check(sess is not None and sess.name == "Admin" and sess.is_admin, "admins may keep reserved names")
    _, sess = await rig.login("U103", "Carl")
    await rig.ws_server.route_message(sess.ws, json.dumps({"type": "SetPlayerData", "content": {"key": "name", "value": "‮SERVER"}}))
    check(sess.name == "Carl", "SetPlayerData cannot bypass name rules")
    adj = [r for r in rig.events("auth_accepted") if r["id"] == "U100"]
    check(bool(adj) and "stripped" in adj[0].get("nameAdjusted", []), "audit records that the name was adjusted")


async def test_version_policy(tmp: Path) -> None:
    h = HardeningConfig(min_client_version="2.2.301", allowed_client_versions=("2.2.*",))
    check(h.version_verdict("2.2.301") == (True, ""), "min version equal is accepted")
    check(not h.version_verdict("2.2.300")[0], "older than minimum is rejected")
    check(not h.version_verdict("2.1.999")[0] and not h.version_verdict("garbage")[0], "allowlist and unparseable are rejected")
    check(HardeningConfig().version_verdict("anything") == (True, ""), "no restriction configured admits everything")

    rig = Rig(tmp / "log", min_client_version="2.2.301", version_policy="log")
    _, sess = await rig.login("V1", clientVersion="1.0")
    check(sess is not None and bool(rig.events("client_version_warning")), "log policy admits and records the mismatch")

    rig = Rig(tmp / "enf", min_client_version="2.2.301", version_policy="enforce")
    ws, sess = await rig.login("V2", clientVersion="1.0")
    denied = rig.events("auth_denied")
    check(sess is None and ws.close_code == 4002 and ws.sent[0]["data"]["success"] is False
          and ws.types() == ["OnAuthorization"], "enforce denies with the standard failed-login + close 4002")
    check(bool(denied) and "older than" in denied[0]["reason"] and denied[0]["clientVersion"] == "1.0", "denial is audited with version and reason")
    _, sess = await rig.login("V3", clientVersion="2.3.0")
    check(sess is not None, "enforce admits a newer client")


async def test_chat_flood_and_mute(tmp: Path) -> None:
    rig = Rig(tmp, chat_rate=0.001, chat_burst=3, chat_flood_strikes=3, chat_automute_minutes=5)
    a, sa = await rig.login("A1", "Spammer")
    b, _ = await rig.login("B1", "Listener")
    for i in range(3):
        await rig.chat(a, f"hello {i}")
    check(b.chats() == ["hello 0", "hello 1", "hello 2"], "burst is delivered")
    await rig.chat(a, "over 1")
    check("over 1" not in b.chats(), "over-limit message is dropped")
    check(any("too quickly" in t for t in a.chats()), "sender gets a throttled system notice")
    notices = len([t for t in a.chats() if "too quickly" in t])
    await rig.chat(a, "over 2")
    check(len([t for t in a.chats() if "too quickly" in t]) == notices, "notice is throttled")
    await rig.chat(a, "over 3")
    check(rig.state.access.is_muted("A1"), "repeated flooding auto-mutes")
    check(any("muted for 5" in t for t in a.chats()), "auto-mute notifies the sender once")
    mute_events = rig.events("mute")
    check(bool(mute_events) and mute_events[0]["reason"] == "chat flood" and mute_events[0]["actor"]["auto"], "auto-mute is audited")
    check("A1" in rig.state.access.to_payload()["mutedIds"], "auto-mute is persisted in the access payload")
    rig.clock[0] += 301
    check(not rig.state.access.is_muted("A1"), "auto-mute expires")
    check(bool(rig.events("mute_expired")), "mute expiry is audited")

    # explicit admin mute/unmute
    rig = Rig(tmp / "m", chat_rate=0)
    a, _ = await rig.login("A2", "Quiet")
    b, _ = await rig.login("B2", "Listener")
    res = await rig.ws_server.handle_access_action({"action": "mute", "id": "A2", "duration": "10m", "reason": "test"},
                                                   {"via": "local-http", "ip": "127.0.0.1"})
    check(res["ok"] and res["access"]["mutedIds"][0]["id"] == "A2" and "expiresAt" in res["access"]["mutedIds"][0], "mute verb with duration shows in snapshot")
    await rig.chat(a, "can you hear me")
    check(b.chats() == [] and a.chats() == [], "muted chat is dropped server-side with no echo")
    check(rig.state.build_user_snapshot(rig.state.sessions_by_user_id["A2"])["authType"] == "gog", "wire user snapshot shape unchanged")
    from server import build_lobbies_payload
    check([u for u in build_lobbies_payload(rig.state)["users"] if u["id"] == "A2"][0]["muted"] is True, "operator user grid shows muted")
    await rig.ws_server.handle_access_action({"action": "unmute", "id": "A2"})
    await rig.chat(a, "now?")
    check("now?" in b.chats(), "unmute restores chat")
    check([r["event"] for r in rig.audit_records() if r["event"] in ("mute", "unmute")] == ["mute", "unmute"], "mute and unmute audited")
    check(rig.events("mute")[0]["actor"]["ip"] == "127.0.0.1", "audit names the actor")


async def test_ban_expiry(tmp: Path) -> None:
    rig = Rig(tmp)
    acc = rig.state.access
    acc.ban("T1", "198.51.100.7", "Temp", "spam", duration=parse_duration("1h"))
    acc.ban("P1", "", "Perm", "forever")
    check(acc.decide("T1", "")[0] == "deny" and acc.decide("", "198.51.100.7")[0] == "deny", "timed ban blocks by id and ip")
    payload = json.loads(json.dumps(acc.to_payload()))
    check("expiresAt" in payload["bannedIds"]["T1"] and "expiresAt" not in payload["bannedIds"]["P1"], "expiry persisted; permanent ban has none")
    rig.clock[0] += 3601
    restored = server.AccessControl(logging.getLogger("t"), lambda: None)
    restored.audit = rig.audit
    restored.clock = lambda: rig.clock[0]
    restored.load_payload(payload)
    check("T1" not in restored.banned_ids and "198.51.100.7" not in restored.banned_ips, "expired bans are pruned on load")
    check("P1" in restored.banned_ids, "ban without expiry stays permanent")
    check(acc.decide("T1", "198.51.100.7")[0] == "allow", "expired ban is pruned on check")
    check(len(rig.events("ban_id_expired")) >= 1, "expiry is audited")
    res = await rig.ws_server.handle_access_action({"action": "ban", "ip": "198.51.100.8", "duration": 90})
    check(res["ok"] and res["access"]["bannedIps"][0]["expiresAt"], "POST /access ban accepts a duration")
    try:
        await rig.ws_server.handle_access_action({"action": "ban", "id": "x", "duration": "soon"})
        check(False, "bad duration rejected")
    except ValueError:
        check(True, "bad duration rejected")
    res = await rig.ws_server.handle_access_action({"action": "ban", "id": "E1", "expiresAt": "2099-01-01 00:00:00"})
    check(res["ok"] and any(b["id"] == "E1" and b["expiresAt"].startswith("2099-01-01") for b in res["access"]["bannedIds"]), "expiresAt accepted")
    check("E1" in rig.state.access.banned_ids, "ban with absolute expiresAt stored")


async def test_admin_key_transport(tmp: Path) -> None:
    rig = Rig(tmp / "a")
    ws, sess = await rig.login("R1", "Remote", ip="203.0.113.50", key=ADMIN_KEY)
    check(sess is not None and not sess.is_admin, "admin key over plain ws from a remote peer is refused (admitted as a player)")
    refused = rig.events("admin_key_refused")
    check(bool(refused) and refused[0]["ip"] == "203.0.113.50", "refusal is audited")
    check(next(m for m in ws.sent if m["type"] == "OnAuthorization")["data"]["isAdmin"] is False, "client is told it is not admin")

    ws, sess = await rig.login("L1", "Local", ip="127.0.0.1", key=ADMIN_KEY)
    check(sess is not None and sess.is_admin, "loopback peer may elevate")
    ws, sess = await rig.login("L2", "LocalV6", ip="::1", key=ADMIN_KEY)
    check(sess is not None and sess.is_admin, "IPv6 loopback may elevate")
    _, sess = await rig.login("L3", "Wrong", ip="127.0.0.1", key=ADMIN_KEY + "x")
    check(sess is not None and not sess.is_admin and bool(rig.events("admin_key_failure")), "wrong key audited as a failure")

    # A local reverse proxy makes every client look like loopback: forwarded headers must disqualify it.
    _, sess = await rig.login("P0", "Proxied", ip="127.0.0.1", headers={"X-Forwarded-For": "203.0.113.77"}, key=ADMIN_KEY)
    check(sess is not None and not sess.is_admin, "forwarded request via an untrusted loopback proxy is refused")

    rig = Rig(tmp / "b", trusted_proxies=("192.0.2.0/24",))
    _, sess = await rig.login("P1", "TlsProxy", ip="192.0.2.10", headers={"X-Forwarded-Proto": "https", "X-Forwarded-For": "203.0.113.5"}, key=ADMIN_KEY)
    check(sess is not None and sess.is_admin, "trusted proxy marking TLS may elevate")
    _, sess = await rig.login("P2", "PlainProxy", ip="192.0.2.10", headers={"X-Forwarded-Proto": "http"}, key=ADMIN_KEY)
    check(sess is not None and not sess.is_admin, "trusted proxy on plain http is refused")
    _, sess = await rig.login("P3", "FakeProxy", ip="203.0.113.99", headers={"X-Forwarded-Proto": "https"}, key=ADMIN_KEY)
    check(sess is not None and not sess.is_admin, "spoofed X-Forwarded-Proto from an untrusted peer is refused")

    rig = Rig(tmp / "c", allow_plain_remote_admin=True)
    _, sess = await rig.login("O1", "OptIn", ip="203.0.113.60", key=ADMIN_KEY)
    check(sess is not None and sess.is_admin, "explicit opt-in allows plain remote admin")

    # elevated session can use admin verbs, others are audited as denied
    rig = Rig(tmp / "d")
    ws, sess = await rig.login("L9", "Boss", ip="127.0.0.1", key=ADMIN_KEY)
    ws2, _ = await rig.login("N9", "Pleb")
    await rig.ws_server.route_message(ws2, json.dumps({"type": "AddRegex", "content": {"regex": "foo", "password": "hunter2"}}))
    await rig.ws_server.route_message(ws, json.dumps({"type": "SetLogLevel", "content": {"level": 1}}))
    verbs = rig.events("admin_verb")
    check([v["allowed"] for v in verbs] == [False, True] and verbs[0]["actor"]["id"] == "N9", "admin verbs are audited with allowed flag")


async def test_ticket_withheld(tmp: Path) -> None:
    rig = Rig(tmp)
    ws, sess = await rig.login("S1", "Steamy", authtype="steam", steamAppTicket="withheld")
    check(sess is not None and sess.ticket_withheld, "withheld marker admitted and recorded")
    auth = next(m for m in ws.sent if m["type"] == "OnAuthorization")["data"]
    check(auth["authType"] == "steam" and "ticketWithheld" not in auth and "ticketWithheld" not in auth["user"],
          "wire messages for stock clients are unchanged")
    from server import build_lobbies_payload
    row = [u for u in build_lobbies_payload(rig.state)["users"] if u["id"] == "S1"][0]
    check(row["authtype"] == "steam (ticket withheld)" and row["ticketWithheld"] is True, "operator grid labels withheld tickets")
    check(rig.events("auth_accepted")[0]["ticketWithheld"] is True, "audit records ticketWithheld")
    ws, sess = await rig.login("S2", "Real", authtype="steam", authTicket=TICKET)
    check(sess is not None and not sess.ticket_withheld, "a real ticket is not marked withheld")
    ws, sess = await rig.login("S3", "NoTicket", authtype="steam")
    check(sess is None and ws.close_code == 4002 and ws.sent[0]["data"]["success"] is False, "empty steam ticket still refused (stock parity)")
    ws, sess = await rig.login("S4", "Gog", authtype="gog", gogAppTicket="withheld")
    check(sess is not None and sess.ticket_withheld, "gog withheld marker recorded")


async def test_audit_log(tmp: Path) -> None:
    rig = Rig(tmp)
    await rig.login("Q1", "Ticketer", authtype="steam", steamAppTicket=TICKET, key=ADMIN_KEY, password="pw-secret-xyz")
    await rig.login("Q2", "Remote", ip="203.0.113.5", key=ADMIN_KEY)
    await rig.ws_server.handle_access_action({"action": "ban", "id": "Q2", "reason": f"bad key {ADMIN_KEY}"})
    rig.audit.write("probe", args={"adminKey": ADMIN_KEY, "note": "ok", "nested": [{"steamAppTicket": TICKET}]})
    text = rig.audit_path.read_text(encoding="utf-8")
    check(bool(text) and ADMIN_KEY not in text and TICKET not in text and "pw-secret-xyz" not in text, "audit log contains no tickets, passwords or admin keys")
    check(all(isinstance(json.loads(line), dict) for line in text.splitlines()), "audit log is valid JSON Lines")
    check({"auth_accepted", "admin_key_refused", "ban", "kick"} <= {r["event"] for r in rig.audit_records()}, "auth, key refusal, ban and kick all recorded")

    small = AuditLog(str(tmp / "rot" / "audit.log"), max_bytes=400, backups=2)
    for i in range(30):
        small.write("tick", n=i, pad="x" * 40)
    files = sorted(p.name for p in (tmp / "rot").iterdir())
    check(files == ["audit.log", "audit.log.1", "audit.log.2"], f"audit log rotates by size and keeps 2 backups ({files})")
    check(AuditLog(None).enabled is False, "audit can be disabled")


async def test_duplicate_login(tmp: Path) -> None:
    rig = Rig(tmp)
    rig.ws_server.DUPLICATE_PROBE_SECONDS = 0.05
    lan = ["127.0.0.1:62185", "169.254.1.2:62185", "192.168.0.63:62185", "192.168.0.102:62185"]
    old_ws, old = await rig.login("S765", "Squaw", ip="192.168.0.63", lanAddresses=lan)

    # Old socket still answers pings: the second login is refused, the first is untouched.
    dup_ws, dup = await rig.login("S765", "Squaw", ip="192.168.0.102", lanAddresses=lan)
    check(dup is None and dup_ws.close_code == 4002 and old_ws.close_code is None
          and rig.state.sessions_by_user_id.get("S765") is old, "duplicate of a live session is refused")

    # Seat the player in a running game, then let the old socket die silently.
    rig.state.lobbies[7] = LobbyState(lobby_id=7, owner="S765", metadata={"name": "~game~pub~~t"})
    rig.state.lobbies[7].users["S765"] = rig.state.build_user_snapshot(old)
    rig.state.lobbies[7].users["P2"] = {"id": "P2"}
    old.current_lobby_id = 7
    old.extra_data.update({"ready": "1", "team": "2"})
    old_ws.alive = False
    peer_ws, _ = await rig.login("P2", "Peer", ip="192.168.0.44")
    peer_mark = len(peer_ws.sent)

    # Same PC reconnecting through its other adapter before keepalive noticed.
    new_ws, new = await rig.login("S765", "Squaw", ip="192.168.0.102", lanAddresses=lan)
    await asyncio.sleep(0)
    check(new is not None and new.user_id == "S765", "same-PC reconnect keeps the id (no _xxxx suffix)")
    check(rig.state.sessions_by_user_id.get("S765") is new and old_ws not in rig.state.sessions_by_ws,
          "dead session is replaced, not left as a ghost")
    check(new.current_lobby_id == 7 and "S765" in rig.state.lobbies[7].users and rig.state.lobbies[7].owner == "S765"
          and new.extra_data.get("ready") == "1", "reconnect inherits the lobby seat, ownership and player data")
    check(not any(m["type"] == "OnLobbyMemberListChanged" for m in peer_ws.sent[peer_mark:]),
          "other players are not told the player left")
    check(old_ws.close_code == 4002, "dead socket is closed")
    check(len(rig.events("session_superseded")) == 1, "audit records the replacement")
    await rig.ws_server.unregister(old_ws)
    check(rig.state.sessions_by_user_id.get("S765") is new and "S765" in rig.state.lobbies[7].users,
          "late cleanup of the old socket keeps the new session and seat")

    # A different machine claiming the id of a dead session does not get the seat.
    new_ws.alive = False
    _, other = await rig.login("S765", "Squaw", ip="198.51.100.7", lanAddresses=["10.0.0.5:62185"])
    check(other is not None and other.user_id.startswith("S765_") and other.current_lobby_id is None,
          "different machine gets a suffixed id and no seat")
    # Loopback/link-local overlap alone is not proof of the same machine.
    _, other2 = await rig.login("S765", "Squaw", ip="198.51.100.8", lanAddresses=["127.0.0.1:62185", "169.254.1.2:62185"])
    check(other2 is not None and other2.user_id.startswith("S765_"), "loopback/link-local overlap is ignored")


async def test_community_identity(tmp: Path) -> None:
    key1, key2 = "ck-" + "a" * 40, "ck-" + "b" * 40
    rig = Rig(tmp)
    rig.state.access.rate_limit_per_minute = 0

    async def login_out(uid: str, **content: Any) -> tuple[FakeWS, Any]:
        ws, s = await rig.login(uid, "Gina", **content)
        if s is not None:
            await rig.ws_server.unregister(ws)
        return ws, s

    _, s = await login_out("G1")
    check(s is not None and "G1" not in rig.state.access.identities, "stock login without a key is admitted and unprotected")
    _, s = await login_out("G1", communityKey=key1)
    check(s is not None and "G1" in rig.state.access.identities and len(rig.events("identity_registered")) == 1,
          "first login with a key registers it")
    _, s = await login_out("G1", communityKey=key1)
    check(s is not None, "same key is admitted again")
    ws, s = await login_out("G1")
    check(s is None and ws.close_code == 4002 and ws.sent[-1]["data"]["success"] is False,
          "protected id without a key is refused like a failed login")
    ws, s = await login_out("G1", communityKey=key2)
    check(s is None and rig.state.access.identities["G1"].get("failedAttempts") == 1,
          "wrong key is refused and counted")
    _, s = await login_out("G2", communityKey="short")
    check(s is None and "G2" not in rig.state.access.identities, "malformed key is refused")
    ws, s = await rig.login("0", "Web", authtype="web", communityKey=key1)
    check(s is not None and not any(k.startswith("U") for k in rig.state.access.identities),
          "server-assigned ids never register a key")
    await rig.ws_server.unregister(ws)
    _, s = await login_out("0", communityKey=key1)
    check(s is not None and "0" not in rig.state.access.identities, "gog placeholder id 0 never registers a key")

    # A squatter cannot lock out a stock player by claiming their id with a key.
    online_ws, online = await rig.login("G4", "Stock")
    ws, s = await rig.login("G4", "Squat", ip="198.51.100.7", communityKey=key2)
    check(s is None and "G4" not in rig.state.access.identities, "refused duplicate login binds no key")
    online_ws.alive = False
    _, s = await rig.login("G4", "Squat", ip="198.51.100.7", communityKey=key2)
    check(s is not None and s.user_id.startswith("G4_") and not any(k.startswith("G4") for k in rig.state.access.identities),
          "suffixed duplicate login binds no key")
    await rig.ws_server.unregister(s.ws)
    await rig.ws_server.unregister(online_ws)
    # Two first logins racing (e.g. both waiting on approval): only the first key binds.
    check(rig.state.access.bind_identity("G5", key1, "A") and not rig.state.access.bind_identity("G5", key2, "B"),
          "a concurrent first login with a different key does not bind over the first")

    entry = rig.state.record_protocol_event(channel="ws", direction="in", peer="203.0.113.9", msg_type="Authorization",
                                            payload={"id": "G1", "key": "admin-secret-xyz", "communityKey": key1})
    check("admin-secret-xyz" not in entry["preview"] and key1 not in entry["preview"],
          "protocol history preview redacts the admin key and community key")

    snap = rig.state.access.snapshot()
    check(snap["identities"][0]["id"] == "G1" and "hash" not in snap["identities"][0]
          and "salt" not in snap["identities"][0], "snapshot lists protected ids without salt or hash")
    rig.state.save_state()
    saved = (tmp / "state.json").read_text(encoding="utf-8")
    check(key1 not in saved and key2 not in saved, "state file stores no raw key")
    check(key1 not in rig.audit_path.read_text(encoding="utf-8") and key2 not in rig.audit_path.read_text(encoding="utf-8"),
          "audit log never contains a community key")

    rig2 = Rig(tmp)
    rig2.state.load_state()
    rig2.state.access.rate_limit_per_minute = 0
    _, s = await rig2.login("G1", "Gina")
    check(s is None, "protection survives a restart")
    _, s = await rig2.login("G1", "Gina", ip="203.0.113.10", communityKey=key1)
    check(s is not None, "registered key still verifies after a restart")
    await rig2.ws_server.unregister(s.ws)

    r = await rig2.ws_server.handle_access_action({"action": "resetIdentity", "id": "G1"})
    check(r["ok"] and "G1" not in rig2.state.access.identities, "operator reset forgets the key")
    _, s = await rig2.login("G1", "Gina", communityKey=key2)
    check(s is not None and "G1" in rig2.state.access.identities, "after a reset a new key registers")
    await rig2.ws_server.unregister(s.ws)
    r = await rig2.ws_server.handle_access_action({"action": "resetIdentity", "id": "NOPE"})
    check(not r["ok"], "resetting an unknown id reports an error")

    await rig2.ws_server.handle_access_action({"action": "set", "identityPolicy": "required"})
    _, s = await rig2.login("G3", "New")
    check(s is None, "required policy refuses a login without a key")
    ws, s = await rig2.login("0", "Web", authtype="web")
    check(s is not None, "required policy still admits server-assigned ids")
    await rig2.ws_server.unregister(ws)
    await rig2.ws_server.handle_access_action({"action": "set", "identityPolicy": "off"})
    _, s = await rig2.login("G1", "Gina", communityKey=key1)
    check(s is not None, "off policy ignores keys")
    try:
        await rig2.ws_server.handle_access_action({"action": "set", "identityPolicy": "bogus"})
        check(False, "bad identity policy is rejected")
    except ValueError:
        check(rig2.state.access.identity_policy == "off", "bad identity policy is rejected")


async def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        for i, test in enumerate((test_names, test_version_policy, test_chat_flood_and_mute, test_ban_expiry,
                                  test_admin_key_transport, test_ticket_withheld, test_audit_log,
                                  test_duplicate_login, test_community_identity)):
            await test(base / f"t{i}")
    print(f"\n{'ALL PASSED' if not _failures else str(len(_failures)) + ' FAILED: ' + '; '.join(_failures)}")
    return 0 if not _failures else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
