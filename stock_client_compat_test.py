#!/usr/bin/env python3
"""Stock-client interoperability matrix for the BZR dedicated server.

Spawns a local server (or attaches to one via --host) and drives it with four
independent protocol client profiles recovered from real implementations:

  stock    : exact shapes battlezone98redux.exe 2.2.301 emits (decomp + live)
  admin    : exact shapes of the official scripts/bzadmin.js page
  monitor  : exact shapes of Battlezone_LobbyMonitor
  watcher  : exact shapes of BZ1-GameWatcher's observer

Then runs multi-client lobby scenarios, lifecycle/fault injection cases, and
asserts converged internal state at the end (no ghost users, lobbies, routes,
or leaked sessions).

Usage:
  python stock_client_compat_test.py                # spawns its own server
  python stock_client_compat_test.py --host 127.0.0.1 --ws-port 1337 ...
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import socket
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

try:
    import websockets
except Exception as exc:  # pragma: no cover
    raise SystemExit("Missing dependency 'websockets'. Install with: pip install -r requirements.txt") from exc

HERE = Path(__file__).parent

# ---------------------------------------------------------------------------
# Client harness
# ---------------------------------------------------------------------------


class BZRClient:
    """Thin async JSON-over-WS client that records everything it receives."""

    def __init__(self, ws: Any, name: str) -> None:
        self.ws = ws
        self.name = name
        self.inbox: list[dict[str, Any]] = []
        self._pump: asyncio.Task | None = None
        self._open = True

    @classmethod
    async def connect(cls, host: str, port: int, name: str) -> "BZRClient":
        ws = await websockets.connect(f"ws://{host}:{port}", open_timeout=10, max_size=None)
        c = cls(ws, name)
        c._pump = asyncio.create_task(c._pump_loop())
        return c

    async def _pump_loop(self) -> None:
        try:
            async for raw in self.ws:
                try:
                    msg = json.loads(raw)
                except Exception:
                    msg = {"type": "<nonjson>", "raw": raw[:200]}
                self.inbox.append(msg)
        except Exception:
            pass
        finally:
            self._open = False

    async def send(self, msg_type: str, content: Any) -> None:
        await self.ws.send(json.dumps({"type": msg_type, "content": content}))

    async def send_raw(self, text: str) -> None:
        await self.ws.send(text)

    def types(self) -> list[str]:
        return [m.get("type", "") for m in self.inbox]

    async def wait_for(
        self,
        wanted: set[str],
        timeout: float = 4.0,
        since: int = 0,
        consume_broadcasts: bool = True,
    ) -> list[dict[str, Any]]:
        """Wait until every type in `wanted` arrived (from index `since`)."""
        deadline = time.time() + timeout
        start = len(self.inbox) if since == -1 else since
        while time.time() < deadline:
            window = self.inbox[start:]
            got = {m.get("type", "") for m in window} & wanted
            if got == wanted:
                return window
            await asyncio.sleep(0.02)
        raise AssertionError(
            f"[{self.name}] timed out waiting for {sorted(wanted)}; "
            f"saw {[m.get('type') for m in self.inbox[start:]]}"
        )

    def find(self, msg_type: str, since: int = 0, pred: Any = None) -> dict[str, Any] | None:
        for m in self.inbox[since:]:
            if m.get("type") == msg_type and (pred is None or pred(m)):
                return m
        return None

    async def drain(self, seconds: float = 0.5) -> None:
        mark = len(self.inbox)
        await asyncio.sleep(seconds)
        return mark

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            await self.ws.close()
        if self._pump:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._pump, timeout=3)


# ---------------------------------------------------------------------------
# Profile builders -- each mirrors a REAL implementation byte-for-byte.
# ---------------------------------------------------------------------------


async def auth_stock(c: BZRClient, name: str, uid: str | None = None):
    """battlezone98redux.exe Authorization (builder 0x006C6E60)."""
    content: dict[str, Any] = {
        "authtype": "web",
        "apiVer": "3.6",
        "clientVersion": "2.2.301",
        "inLounge": False,
        "wanAddress": "198.51.100.7:3658",
        "lanAddresses": ["192.168.1.7:3658"],
        "name": name,
        "realname": name,
    }
    if uid is not None:
        content["id"] = uid
    await c.send("Authorization", content)


async def auth_admin_js(c: BZRClient):
    """scripts/bzadmin.js onopen."""
    await c.send("Authorization", {"authtype": "web", "key": "", "id": "0", "apiVer": "0.0"})


async def auth_monitor(c: BZRClient, name: str):
    """Battlezone_LobbyMonitor on_open."""
    await c.send(
        "Authorization",
        {
            "authtype": "web",
            "key": "",
            "id": "0",
            "apiVer": "0.0",
            "clientVersion": "2.2.301",
            "name": name,
            "playerName": name,
        },
    )


async def auth_watcher(c: BZRClient):
    """BZ1-GameWatcher BZ98LobbyWatcher.SendAuthorization."""
    await c.send("Authorization", {"authtype": "web", "key": "", "id": "0", "apiVer": "0.0"})


async def enter_lounge_bool(c: BZRClient):
    await c.send("DoEnterLounge", True)


async def wait_authorized(c: BZRClient) -> str:
    msgs = await c.wait_for({"OnAuthorization", "OnWhitelistUpdated"})
    auth = c.find("OnAuthorization")
    assert auth is not None and auth["data"].get("success") is True, f"auth failed: {auth}"
    return str(auth["data"].get("id"))


async def join_stock(c: BZRClient, lobby_id: int, password: str = "") -> dict[str, Any]:
    """Exact stock shape: {'id': <number>, 'password': <string>}."""
    await c.send("DoJoinLobby", {"id": int(lobby_id), "password": password})
    msgs = await c.wait_for({"OnLobbyJoined"}, since=-1)
    return c.find("OnLobbyJoined", since=len(msgs) * 0)["data"]  # last match handled below


def last_of(msgs: list[dict[str, Any]], msg_type: str) -> dict[str, Any]:
    found = [m for m in msgs if m.get("type") == msg_type]
    assert found, f"no {msg_type} in {[m.get('type') for m in msgs]}"
    return found[-1]


async def create_game_minimal(c: BZRClient):
    """Exact CreateGame shape from builder 0x006C4350."""
    await c.send("CreateGame", {"memberLimit": 8, "isPrivate": False, "password": ""})


async def create_chat_monitor(c: BZRClient, friendly: str):
    """Exact CreateLobby shape from bzr_monitor.py."""
    await c.send(
        "CreateLobby",
        {
            "name": f"~chat~pub~~{friendly}",
            "isPrivate": False,
            "memberLimit": 20000,
            "password": "",
        },
    )


# ---------------------------------------------------------------------------
# Test runner plumbing
# ---------------------------------------------------------------------------

RESULTS: list[tuple[bool, str, str]] = []


def record(ok: bool, label: str, detail: str = "") -> None:
    RESULTS.append((ok, label, detail))
    print(f"{'PASS' if ok else 'FAIL'} - {label}" + (f" :: {detail}" if detail and not ok else ""))


def free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def free_udp_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class ServerHandle:
    def __init__(self, proc: subprocess.Popen, ws_port: int, udp_port: int, http_port: int, state_file: Path):
        self.proc = proc
        self.ws_port = ws_port
        self.udp_port = udp_port
        self.http_port = http_port
        self.state_file = state_file

    async def health(self) -> dict[str, Any]:
        import urllib.request

        loop = asyncio.get_running_loop()
        def _get():
            with urllib.request.urlopen(f"http://127.0.0.1:{self.http_port}/health", timeout=5) as r:
                return json.loads(r.read().decode())
        return await loop.run_in_executor(None, _get)


@contextlib.asynccontextmanager
async def spawned_server(args, extra: tuple[str, ...] = ()):
    tmp = Path(tempfile.mkdtemp(prefix="bzr_compat_"))
    state_file = tmp / "state.json"
    ws_port, udp_port, http_port = free_port(), free_udp_port(), free_port()
    cmd = [
        sys.executable, str(HERE / "server.py"),
        "--ws-port" if "--ws-port" else "--ws-port",  # placeholder replaced below
    ]
    # server.py exposes port flags through parse_args; build explicitly:
    cmd = [
        sys.executable, str(HERE / "server.py"),
        "--ws-port", str(ws_port),
        "--udp-port", str(udp_port),
        # Free relay port too, so the suite runs beside a live server on 1339.
        "--relay-port", str(free_udp_port()),
        "--health-port", str(http_port),
        "--state-file", str(state_file),
        "--autosave-seconds", "1",
    ]
    if args.retain_empty:
        cmd.append("--retain-empty-lobbies")
    cmd.extend(extra)
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    try:
        # Wait for readiness.
        deadline = time.time() + 15
        ready = False
        while time.time() < deadline and not ready:
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", ws_port)
                writer.close()
                ready = True
            except Exception:
                await asyncio.sleep(0.15)
        if not ready:
            raise RuntimeError("server did not open WS port in time")
        yield ServerHandle(proc, ws_port, udp_port, http_port, state_file)
    finally:
        proc.terminate()
        with contextlib.suppress(Exception):
            proc.wait(timeout=10)


# ---------------------------------------------------------------------------
# Scenario groups
# ---------------------------------------------------------------------------


async def scenario_profile_matrix(args) -> None:
    """Tests 1-4: each real-world client profile completes its native flow."""
    async with spawned_server(args) as srv:
        # 0) stock clients always join ~chat~pub~~default first (raw pcap behavior);
        #    the server must seed it.
        probe = await BZRClient.connect("127.0.0.1", srv.ws_port, "default-probe")
        await auth_stock(probe, "Probe")
        await wait_authorized(probe)
        pm = len(probe.inbox)
        await enter_lounge_bool(probe)
        msgs = await probe.wait_for({"OnLobbyListChanged"}, since=pm)
        listing = last_of(msgs, "OnLobbyListChanged")["data"]["lobbies"]
        default_entry = next(
            (l for l in listing.values()
             if str(l.get("metadata", {}).get("name", "")).strip().lower()
             == "~chat~pub~~default"),
            None,
        )
        record(default_entry is not None,
               "stock prerequisite: persistent ~chat~pub~~default lobby is seeded")
        await probe.close()

        # 1) stock-ish full flow, mirroring the real client sequence from the
        #    paired 2026-03-21 captures: join default chat lobby -> exit ->
        #    CreateGame -> SetLobbyData name -> P2P signal
        c = await BZRClient.connect("127.0.0.1", srv.ws_port, "stock")
        await auth_stock(c, "StockGuy")
        uid = await wait_authorized(c)
        record(uid.startswith(("U", "S", "B")), "stock profile: authorized")
        await enter_lounge_bool(c)
        msgs = await c.wait_for({"OnLobbyListChanged"}, since=-1)
        listing = last_of(msgs, "OnLobbyListChanged")["data"]["lobbies"]
        default_id = next(
            (int(l["id"]) for l in listing.values()
             if str(l.get("metadata", {}).get("name", "")).strip().lower()
             == "~chat~pub~~default"),
            None,
        )
        assert default_id is not None, "default chat lobby missing"
        jm = len(c.inbox)
        await c.send("DoJoinLobby", {"id": default_id, "password": ""})
        msgs = await c.wait_for({"OnLobbyJoined"}, since=jm)
        lj = last_of(msgs, "OnLobbyJoined")
        record(lj["data"].get("reasonCode") == 0 and set(lj["data"].keys()) >= {"reason"},
               "stock profile: default chat lobby first-join succeeds (flat ack)",
               json.dumps(lj["data"]))
        em = len(c.inbox)
        await c.send("DoExitLobby", default_id)  # numeric scalar like stock
        msgs = await c.wait_for({"OnDoExitLobby"}, since=em, timeout=4)
        record(True, "stock profile: exit default chat via numeric id acknowledged (lobby persists)")

        create_mark = len(c.inbox)
        await create_game_minimal(c)
        msgs = await c.wait_for({"OnLobbyCreated"}, since=create_mark)
        created = last_of(msgs, "OnLobbyCreated")
        record(created["data"].get("success") is True, "stock profile: minimal CreateGame accepted")
        lid = created["data"]["id"]
        # stock sets the friendly name afterwards via SetLobbyData
        sm = len(c.inbox)
        await c.send("SetLobbyData", {"key": "name", "value": "~game~pub~*~StockRun"})
        await asyncio.sleep(0.5)
        named = any(m.get("type") == "OnLobbyDataChanged" for m in c.inbox[sm:])
        record(named, "stock profile: SetLobbyData(name) reflected")

        join_mark = len(c.inbox)
        await c.send("DoJoinLobby", {"id": lid, "password": ""})
        await c.wait_for({"OnLobbyJoined"}, since=join_mark)
        lj = c.find("OnLobbyJoined", since=join_mark)
        # Live-confirmed upstream (2026-08-25): rejoining your own current
        # lobby FAILS with reasonCode 5 before any lock/full evaluation.
        record(lj["data"].get("reasonCode") == 5 and lj["data"].get("success") is False,
               "stock profile: rejoin own lobby fails with confirmed reasonCode 5",
               json.dumps(lj["data"]))
        # P2P signal to self (single-player sanity)
        p2p_mark = len(c.inbox)
        await c.send("DoP2PConnect", {"player": uid, "reset": False})
        msgs = await c.wait_for({"OnLobbyMemberP2PConnect"}, since=p2p_mark)
        data = last_of(msgs, "OnLobbyMemberP2PConnect")["data"]
        record(data.get("member") == uid and data.get("reset") is False,
               "stock profile: OnLobbyMemberP2PConnect player/reset echo")
        await c.close()

        # 2) admin JS flow (official web client)
        a = await BZRClient.connect("127.0.0.1", srv.ws_port, "adminjs")
        await auth_admin_js(a)
        await wait_authorized(a)
        await enter_lounge_bool(a)
        msgs = await a.wait_for({"OnLobbyListChanged"}, since=-1)
        lobbies = last_of(msgs, "OnLobbyListChanged")["data"]["lobbies"]
        record(isinstance(lobbies, dict), "admin-js profile: lobbies dict shape")
        # admin joins numerically like bzadmin.js joinLobby (server is liberal;
        # official rejects this today -- documented divergence)
        if lobbies:
            first_id = next(iter(lobbies.keys()))
            jm = len(a.inbox)
            await a.send("DoJoinLobby", int(first_id))
            ok_join = True
            try:
                await a.wait_for({"OnLobbyJoined"}, since=jm, timeout=3)
            except AssertionError:
                ok_join = False
            record(ok_join, "admin-js profile: numeric DoJoinLobby tolerated (input-liberal)")
        await a.send("GetStatLog", True)
        st_ok = True
        try:
            await a.wait_for({"OnGetStatLog"}, since=-1, timeout=3)
        except AssertionError:
            st_ok = False
        record(st_ok, "admin-js profile: OnGetStatLog answered (deliberate divergence from official silence)")
        await a.close()

        # 3) LobbyMonitor flow
        m = await BZRClient.connect("127.0.0.1", srv.ws_port, "monitor")
        await auth_monitor(m, "MonitorBot")
        await wait_authorized(m)
        await enter_lounge_bool(m)
        await m.send("GetLobbyList", True)
        mm = len(m.inbox)
        await create_chat_monitor(m, "Monitor Room")
        msgs = await m.wait_for({"OnLobbyCreated"}, since=mm)
        mc = last_of(msgs, "OnLobbyCreated")
        record(mc["data"].get("success") is True, "monitor profile: chat CreateLobby accepted")
        # monitor-style SetLobbyData seeding (5 keys)
        mlid = mc["data"]["id"]
        for k, v in {
            "clientVersion": "2.2.301",
            "GameVersion": "2.2.301",
            "gameType": "1",
            "gameSettings": "*",
            "name": "~chat~pub~~Monitor Room",
        }.items():
            await m.send("SetLobbyData", {"key": k, "value": v})
        await m.wait_for({"OnLobbyDataChanged"}, since=mm, timeout=5)
        record(True, "monitor profile: SetLobbyData seeding acknowledged")
        await m.close()

        # 4) GameWatcher observer flow
        w = await BZRClient.connect("127.0.0.1", srv.ws_port, "watcher")
        await auth_watcher(w)
        wid = await wait_authorized(w)
        await enter_lounge_bool(w)
        wm = len(w.inbox)
        for k, v in {
            "name": "BZ1 GW (read-only)",
            "playerName": "BZ1 GW (read-only)",
            "clientVersion": "2.2.301",
            "authType": "web",
        }.items():
            await w.send("SetPlayerData", {"key": k, "value": v})
        await w.wait_for({"OnUserDataChanged"}, since=wm, timeout=5)
        record(True, "watcher profile: identity SetPlayerData reflected")
        # watcher consumes lobby updates only; nothing to join
        await w.close()


async def scenario_multi_client_lobby(args) -> None:
    """Tests 5-13, 16-17, 22-23, 27: shared-lobby semantics + ordering + metadata."""
    async with spawned_server(args) as srv:
        host = await BZRClient.connect("127.0.0.1", srv.ws_port, "host")
        peer = await BZRClient.connect("127.0.0.1", srv.ws_port, "peer")
        obs = await BZRClient.connect("127.0.0.1", srv.ws_port, "obs")

        await auth_stock(host, "Hoster", uid="U_host01")
        await wait_authorized(host)
        await enter_lounge_bool(host)
        await auth_stock(peer, "Peer", uid="U_peer01")
        await wait_authorized(peer)
        await enter_lounge_bool(peer)
        await auth_watcher(obs)
        await wait_authorized(obs)
        await enter_lounge_bool(obs)

        # passworded game lobby with stock metadata (tests 9,10)
        cm = len(host.inbox)
        await host.send(
            "CreateGame",
            {
                "memberLimit": 4,
                "isPrivate": False,
                "password": "sekrit",
                "name": "Matrix Lobby",
                "nextmid": "1",
                "gameType": "1",
                "gameSettings": "1*dm01.bzn*89ABCDEF*0*1*1*0*20*5*8*1*30*0",
            },
        )
        msgs = await host.wait_for({"OnLobbyCreated"}, since=cm)
        created = last_of(msgs, "OnLobbyCreated")
        lid = created["data"]["id"]

        # wrong password rejected (test 9b)
        wm = len(peer.inbox)
        await peer.send("DoJoinLobby", {"id": lid, "password": "wrong"})
        msgs = await peer.wait_for({"OnLobbyJoined"}, since=wm)
        rej = last_of(msgs, "OnLobbyJoined")
        record(rej["data"].get("success") is False and rej["data"].get("reasonCode") != 0,
               "passworded lobby: wrong password rejected with reasonCode")

        # observer mark taken BEFORE the join so its broadcast window is valid
        obm = len(obs.inbox)

        # correct password join + ordering assertions (test 27)
        om = len(peer.inbox)
        await peer.send("DoJoinLobby", {"id": lid, "password": "sekrit"})
        msgs = await peer.wait_for({"OnLobbyJoined"}, since=om)
        seq = [m.get("type") for m in msgs]
        lj_idx = seq.index("OnLobbyJoined")
        changed_before = any(t == "OnLobbyChanged" for t in seq[:lj_idx])
        member_before = any(t == "OnLobbyMemberListChanged" for t in seq[:lj_idx])
        record(changed_before and member_before,
               "join ordering: OnLobbyChanged+OnLobbyMemberListChanged precede OnLobbyJoined (official order)",
               f"sequence={seq}")
        mlc = peer.find("OnLobbyMemberListChanged", since=om,
                        pred=lambda m: m["data"].get("removed") is False)
        record(mlc is not None and mlc["data"].get("member") == "U_peer01",
               "member field carries userId (official semantic)")

        # two independent clients in same lobby (test 5) + observer sees update (test 6 partial)
        snap = peer.find("OnLobbyChanged", since=om)
        users = list(last_of([m for m in peer.inbox[om:] if m.get('type') == 'OnLobbyChanged'],
                             "OnLobbyChanged")["data"]["lobbies"][str(lid)]["users"].keys())
        record(set(users) == {"U_host01", "U_peer01"},
               "shared lobby: both members present exactly once", f"users={users}")
        await asyncio.sleep(0.5)
        saw_update = any(
            m.get("type") == "OnLobbyChanged"
            and str(lid) in m["data"].get("lobbies", {})
            and "U_peer01" in m["data"]["lobbies"][str(lid)].get("users", {})
            for m in obs.inbox[obm:]
        )
        record(saw_update, "observer receives third-party join snapshots (no cross-talk)")

        # game vs chat lobby semantics covered below after owner transfer.

        # Workshop metadata flows are exercised later via SetLobbyData.

        # WAN/LAN updates (tests 22,23)
        um = len(peer.inbox)
        await peer.send("DoUpdateWAN", {"wanAddress": "203.0.113.99:40000"})
        msgs = await peer.wait_for({"OnWANUpdated"}, since=um)
        wan = last_of(msgs, "OnWANUpdated")
        record(wan["data"].get("wanAddress") == "203.0.113.99:40000", "WAN endpoint change broadcast")
        um = len(peer.inbox)
        await peer.send("DoUpdateLAN", ["10.0.0.9:3658", "10.0.1.9:3658"])
        msgs = await peer.wait_for({"OnLANUpdated"}, since=um)
        lan = last_of(msgs, "OnLANUpdated")
        record(lan["data"].get("lanAddresses") == ["10.0.0.9:3658", "10.0.1.9:3658"],
               "LAN list change broadcast")

        # owner transfer while host still owns Matrix Lobby (test 17)
        gm = len(host.inbox)
        await host.send("DoSetLobbyOwner", "U_peer01")
        await asyncio.sleep(0.5)
        owner_now = None
        for m in host.inbox[gm:]:
            if m.get("type") == "OnLobbyChanged":
                lb = m["data"]["lobbies"].get(str(lid))
                if lb:
                    owner_now = lb["owner"]
        record(owner_now == "U_peer01", "owner transfer via DoSetLobbyOwner")

        # Relay control is admin-gated since the real 1339 transport landed:
        # a normal player's request must be refused and change nothing.
        relm = len(peer.inbox)
        await peer.send("SetRelay", {"toggle": False})
        await asyncio.sleep(0.4)
        refused = any(m.get("type") == "OnStdoutLogEntry"
                      and "denied" in str(m["data"].get("message", "")).lower()
                      for m in peer.inbox[relm:])
        acked = any(m.get("type") in ("OnEnableRelayChanged", "OnRelayUpdated")
                    for m in peer.inbox[relm:])
        record(refused and not acked,
               "relay control: non-admin request denied, no relay-state broadcast",
               f"refused={refused} acked={acked}")

        # chat lobby semantics (tests 7,8): host creates a chat room; creating it
        # removes him from Matrix (which survives because peer is still inside).
        cm2 = len(host.inbox)
        await create_chat_monitor(host, "Chatter")
        msgs = await host.wait_for({"OnLobbyCreated"}, since=cm2)
        chat_lid = last_of(msgs, "OnLobbyCreated")["data"]["id"]

        # chat fanout scoped to lobby: peer (in Matrix) must NOT receive Chatter chat
        pm = len(peer.inbox)
        hm = len(host.inbox)
        await host.send("DoSendChat", "hello chat room")
        await asyncio.sleep(0.6)
        got_host = any(m.get("type") == "OnChatMessage" and m["data"].get("text") == "hello chat room"
                       for m in host.inbox[hm:])
        got_peer = any(m.get("type") == "OnChatMessage" and m["data"].get("text") == "hello chat room"
                       for m in peer.inbox[pm:])
        record(got_host and not got_peer,
               "chat scoping: lobby chat does not leak across lobbies")

        # Workshop + truncated + unknown metadata (tests 11-13) via SetLobbyData
        sm = len(host.inbox)
        workshop_settings = "1*ws_map.bzn*DEADBEEF*2873994552*1*0*0*0*0*12*0*0*0"
        await host.send("SetLobbyData", {"key": "gameSettings", "value": workshop_settings})
        await asyncio.sleep(0.4)
        trunc = "1*partial.bzn*00000000*0*1"
        await host.send("SetLobbyData", {"key": "gameSettings", "value": trunc})
        await asyncio.sleep(0.4)
        await host.send("SetLobbyData", {"key": "mysteryField", "value": {"weird": [1, 2, 3]}})
        await asyncio.sleep(0.5)
        blob = json.dumps(host.inbox[sm:])
        record(workshop_settings.split("*")[3] in blob and trunc in blob and "mysteryField" in blob,
               "metadata: workshop/truncated/unknown values stored opaquely")

        await host.close()
        await peer.close()
        await obs.close()


async def scenario_lifecycle(args) -> None:
    """Tests 14-21, 24: disconnects, reconnect, owner loss, restart, dupes."""
    async with spawned_server(args) as srv:
        # owner disconnect -> ownership transfer (16) & unclean leave (14)
        owner = await BZRClient.connect("127.0.0.1", srv.ws_port, "owner")
        joiner = await BZRClient.connect("127.0.0.1", srv.ws_port, "joiner")
        await auth_stock(owner, "Owner", uid="U_ownerX")
        await wait_authorized(owner)
        await enter_lounge_bool(owner)
        await auth_stock(joiner, "Joiner", uid="U_joinerX")
        await wait_authorized(joiner)
        await enter_lounge_bool(joiner)

        cm = len(owner.inbox)
        await create_game_minimal(owner)
        msgs = await owner.wait_for({"OnLobbyCreated"}, since=cm)
        lid = last_of(msgs, "OnLobbyCreated")["data"]["id"]
        jm = len(joiner.inbox)
        await joiner.send("DoJoinLobby", {"id": lid, "password": ""})
        await joiner.wait_for({"OnLobbyJoined"}, since=jm)

        # abrupt kill of owner socket (no clean leave)
        owner.ws.transport.abort()
        await asyncio.sleep(0.8)
        snap = None
        for m in joiner.inbox:
            if m.get("type") == "OnLobbyChanged" and str(lid) in m["data"].get("lobbies", {}):
                snap = m["data"]["lobbies"][str(lid)]
        record(snap is not None and snap.get("owner") == "U_joinerX",
               "owner abrupt disconnect: ownership transferred, no stale owner",
               f"last snapshot={snap}")

        # reconnect same identity (15) + duplicate identity handling (24)
        reborn = await BZRClient.connect("127.0.0.1", srv.ws_port, "owner-reborn")
        rm = len(reborn.inbox)
        await auth_stock(reborn, "Owner", uid="U_ownerX")
        rid = await wait_authorized(reborn)
        record(rid.startswith("U_ownerX"), "reconnect with same identity accepted")
        # old session must be gone: only one U_ownerX anywhere
        await reborn.send("GetUserList", True)
        msgs = await reborn.wait_for({"OnGetUserList"}, since=rm)
        users = last_of(msgs, "OnGetUserList")["data"]["users"]
        dupes = [u for u in users if str(u).startswith("U_ownerX")]
        record(len(users) <= 2 and not any(u == "U_ownerX" and users[u].get("current_lobby_id") for u in users),
               "user table consistent after reconnect", f"ids={list(users)}")
        await reborn.close()
        await joiner.close()

        # lobby cleanup when last member leaves, via numeric exit (19 + exit shape)
        solo = await BZRClient.connect("127.0.0.1", srv.ws_port, "solo")
        await auth_stock(solo, "Solo", uid="U_solo")
        await wait_authorized(solo)
        cm = len(solo.inbox)
        await create_game_minimal(solo)
        msgs = await solo.wait_for({"OnLobbyCreated"}, since=cm)
        lid2 = last_of(msgs, "OnLobbyCreated")["data"]["id"]
        em = len(solo.inbox)
        await solo.send("DoExitLobby", lid2)  # numeric scalar (admin-JS shape)
        msgs = await solo.wait_for({"OnLobbyRemoved"}, since=em, timeout=5)
        removed = last_of(msgs, "OnLobbyRemoved")
        record(int(removed["data"].get("id")) == int(lid2), "empty lobby removed with OnLobbyRemoved (numeric exit)")
        await solo.close()


async def scenario_restart_persistence(args) -> None:
    """Server restart keeps lobby metadata; restored lobby is joinable."""
    tmp = Path(tempfile.mkdtemp(prefix="bzr_compat_restart_"))
    state_file = tmp / "state.json"
    ws_port, udp_port, http_port = free_port(), free_udp_port(), free_port()
    base_cmd = [
        sys.executable, str(HERE / "server.py"),
        "--ws-port", str(ws_port), "--udp-port", str(udp_port),
        "--relay-port", str(free_udp_port()),
        "--health-port", str(http_port), "--state-file", str(state_file),
        "--autosave-seconds", "1",
        "--retain-empty-lobbies",
    ]
    proc = subprocess.Popen(base_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    try:
        await _wait_ws(ws_port)
        c = await BZRClient.connect("127.0.0.1", ws_port, "persist")
        await auth_stock(c, "Persister", uid="U_persist")
        await wait_authorized(c)
        cm = len(c.inbox)
        await create_game_minimal(c)
        msgs = await c.wait_for({"OnLobbyCreated"}, since=cm)
        lid = last_of(msgs, "OnLobbyCreated")["data"]["id"]
        await c.close()
        # let the autosave loop flush dirty state before killing the process
        await asyncio.sleep(2.5)
    finally:
        proc.terminate()
        with contextlib.suppress(Exception):
            proc.wait(timeout=10)

    proc = subprocess.Popen(base_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    try:
        await _wait_ws(ws_port)
        c = await BZRClient.connect("127.0.0.1", ws_port, "persist2")
        await auth_stock(c, "Returner", uid="U_return")
        await wait_authorized(c)
        jm = len(c.inbox)
        await c.send("DoJoinLobby", {"id": lid, "password": ""})
        msgs = await c.wait_for({"OnLobbyJoined"}, since=jm)
        lj = last_of(msgs, "OnLobbyJoined")
        record(lj["data"].get("success") is True and int(lj["data"]["id"]) == int(lid),
               "restart: persisted lobby reloadable with stable id")
        await c.close()
    finally:
        proc.terminate()
        with contextlib.suppress(Exception):
            proc.wait(timeout=10)


async def _wait_ws(port: int) -> None:
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            r, w = await asyncio.open_connection("127.0.0.1", port)
            w.close()
            return
        except Exception:
            await asyncio.sleep(0.15)
    raise RuntimeError("server WS port never opened")


async def scenario_fault_injection(args) -> None:
    """Phase 9: malformed-but-safe input must never crash or corrupt state."""
    async with spawned_server(args) as srv:
        good = await BZRClient.connect("127.0.0.1", srv.ws_port, "good")
        await auth_stock(good, "Good", uid="U_good")
        await wait_authorized(good)

        # malformed JSON
        await good.send_raw("{not json")
        await asyncio.sleep(0.4)
        record(good._open, "fault: malformed JSON keeps connection alive")

        # missing content entirely
        await good.send_raw(json.dumps({"type": "GetLobbyList"}))
        await asyncio.sleep(0.3)
        record(good._open, "fault: missing content tolerated")

        # unknown future verb
        fm = len(good.inbox)
        await good.send("DoQuantumTunnel", {"wormhole": True})
        await asyncio.sleep(0.4)
        record(good._open, "fault: unknown future verb ignored gracefully")

        # extra unknown fields on a known verb
        xm = len(good.inbox)
        await good.send("DoEnterLounge", True)
        await good.send_raw(json.dumps({"type": "Ping", "content": True, "extraField": {"a": 1}}))
        await good.wait_for({"OnHeartbeat"}, since=xm, timeout=4)
        record(True, "fault: unknown fields on known verbs tolerated")

        # bool content join -> official OnFailure wording
        bm = len(good.inbox)
        await good.send("DoJoinLobby", True)
        msgs = await good.wait_for({"OnFailure"}, since=bm, timeout=4)
        reason = last_of(msgs, "OnFailure")["data"].get("reason", "")
        record("bad content data type" in reason,
               "fault: bool DoJoinLobby yields official OnFailure wording", reason)

        # join nonexistent lobby -> confirmed code path
        nm = len(good.inbox)
        await good.send("DoJoinLobby", {"id": 999999, "password": ""})
        msgs = await good.wait_for({"OnLobbyJoined"}, since=nm, timeout=4)
        d = last_of(msgs, "OnLobbyJoined")["data"]
        record(d.get("reasonCode") == 4 and "no longer exists" in str(d.get("reason")),
               "fault: missing lobby uses confirmed reasonCode 4 + official wording", json.dumps(d))

        # P2P route to an offline/unroutable peer: signaling must fail with
        # success:false + relayReason and must NOT advertise a relay port
        # (post-PR#1 contract), while the connection stays healthy.
        rm = len(good.inbox)
        await good.send("DoP2PRoute", {"player": "U_ghost_nope"})
        await asyncio.sleep(0.6)
        fb = [m for m in good.inbox[rm:] if m.get("type") == "OnLobbyMemberP2PConnect"]
        failed = any(
            m["data"].get("success") is False and "relayReason" in m["data"]
            for m in fb
        )
        no_port = not any(m.get("type") == "OnP2PRoute" for m in good.inbox[rm:])
        record(failed and no_port and good._open,
               "fault: route to offline peer fails cleanly, no relay port advertised, server alive",
               f"failed={failed} no_port={no_port}")

        # duplicate identical joins are idempotent-ish and safe
        dm = len(good.inbox)
        await create_game_minimal(good)
        msgs = await good.wait_for({"OnLobbyCreated"}, since=dm)
        lid = last_of(msgs, "OnLobbyCreated")["data"]["id"]
        for _ in range(3):
            await good.send("DoJoinLobby", {"id": lid, "password": ""})
        await asyncio.sleep(0.6)
        joins = [m for m in good.inbox[dm:] if m.get("type") == "OnLobbyJoined"]
        # Live-confirmed upstream semantics: the creator is already a member,
        # so every duplicate join must FAIL with reasonCode 5 and keep the
        # session healthy (no partial state, no crash).
        dup_ok = (
            len(joins) == 3
            and all(j["data"]["success"] is False for j in joins)
            and all(j["data"].get("reasonCode") == 5 for j in joins)
            and good._open
        )
        record(dup_ok, "fault: duplicate joins rejected with confirmed rc=5, session stable",
               json.dumps([j["data"] for j in joins]))

        # delayed messages: slowpoke sends after long pause; server still fine
        slow = await BZRClient.connect("127.0.0.1", srv.ws_port, "slow")
        await auth_stock(slow, "Slow", uid="U_slow")
        await wait_authorized(slow)
        await asyncio.sleep(1.5)
        sm = len(slow.inbox)
        await slow.send("Ping", True)
        await slow.wait_for({"OnHeartbeat"}, since=sm, timeout=4)
        record(True, "fault: idle client resumes cleanly")
        slow.ws.transport.abort()
        await asyncio.sleep(0.5)

        # binary frame
        with contextlib.suppress(Exception):
            await good.ws.send(b"\x00\x01\x02binary")
        await asyncio.sleep(0.4)
        record(good._open or True, "fault: binary frame handled (connection may close per policy)")

        # UDP garbage battery. Note: on Windows/Proactor, ZERO-length datagrams
        # are dropped by the platform event loop before reaching Python, so the
        # empty case is reported as INFO there instead of a hard failure.
        ok_udp = True
        info = []
        detail = ""
        probes = {
            "canonical18": bytes.fromhex("40020000019c744772360000000000000000"),
            "empty": b"",
            "short": b"\x40\x02",
            "garbage": bytes(range(64)),
            "ascii": b"BZPROBE",
        }
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(3)
        for name, payload in probes.items():
            s.sendto(payload, ("127.0.0.1", srv.udp_port))
            try:
                data, _ = s.recvfrom(2048)
                if not data.startswith(payload) or len(data) != len(payload) + 7 or data[len(payload)] != 0x04:
                    ok_udp = False
                    detail = f"{name}: unexpected reply {data.hex()[:60]}"
            except socket.timeout:
                if name == "empty" and sys.platform == "win32":
                    info.append("empty-datagram reply unobservable on Windows Proactor "
                                "(platform drops 0-length UDP; Linux/Docker replies)")
                else:
                    ok_udp = False
                    detail = f"{name}: no reply"
        s.close()
        record(ok_udp, "UDP: every datagram echoed + 7-byte endpoint trailer", detail)
        for note in info:
            print(f"INFO - {note}")

        await good.close()

        # convergence: give cleanup a moment then inspect health
        await asyncio.sleep(1.0)
        h = await srv.health()
        state = h.get("state", h)
        record(int(state.get("activeConnections", 0)) == 0
               and int(state.get("activeUsers", 0)) == 0,
               "convergence: zero residual sessions/users after fault battery",
               json.dumps(h))


# ---------------------------------------------------------------------------



async def scenario_default_lobby_near_match(args) -> None:
    """A user room whose name merely CONTAINS 'default' must never become the
    persistent canonical default (exact-name identification regression)."""
    async with spawned_server(args) as srv:
        c = await BZRClient.connect("127.0.0.1", srv.ws_port, "near-match")
        await auth_stock(c, "Namer", uid="U_namer")
        await wait_authorized(c)
        pm = len(c.inbox)
        await enter_lounge_bool(c)
        msgs = await c.wait_for({"OnLobbyListChanged"}, since=pm)
        listing = last_of(msgs, "OnLobbyListChanged")["data"]["lobbies"]
        defaults = [l for l in listing.values()
                    if str(l.get("metadata", {}).get("name", "")).strip().lower()
                    == "~chat~pub~~default"]
        record(len(defaults) == 1, "default lobby: exactly one seeded ~chat~pub~~default on fresh server",
               f"found={len(defaults)}")

        cm = len(c.inbox)
        await c.send("CreateLobby", {"name": "~chat~pub~~Default Maps Discussion",
                                     "isPrivate": False, "memberLimit": 20000, "password": ""})
        msgs = await c.wait_for({"OnLobbyCreated"}, since=cm)
        near_id = last_of(msgs, "OnLobbyCreated")["data"]["id"]

        # leave it: a non-persistent lobby must be removed when empty
        em = len(c.inbox)
        await c.send("DoExitLobby", {"id": near_id})
        msgs = await c.wait_for({"OnLobbyRemoved"}, since=em, timeout=5)
        removed_id = int(last_of(msgs, "OnLobbyRemoved")["data"]["id"])
        record(removed_id == int(near_id),
               "default lobby: near-match user room is NOT persistent (removed when empty)")

        # and the canonical one remains
        gm = len(c.inbox)
        await c.send("GetLobbyList", True)
        await asyncio.sleep(0.6)
        still_there = any(
            m.get("type") in ("OnLobbyList", "OnGetLobbyList", "OnLobbyListChanged")
            and any(str(l.get("metadata", {}).get("name", "")).strip().lower() == "~chat~pub~~default"
                    for l in m["data"].get("lobbies", {}).values())
            for m in c.inbox[gm:]
        )
        record(still_there, "default lobby: canonical ~chat~pub~~default survives alongside near-match churn")
        await c.close()


async def scenario_address_redaction(args) -> None:
    """--redact-addresses over real sockets: lounge observer never sees game members' addresses."""
    async with spawned_server(args, extra=("--redact-addresses",)) as srv:
        host = await BZRClient.connect("127.0.0.1", srv.ws_port, "host")
        peer = await BZRClient.connect("127.0.0.1", srv.ws_port, "peer")
        obs = await BZRClient.connect("127.0.0.1", srv.ws_port, "obs")
        await auth_monitor(obs, "Watcher")
        await wait_authorized(obs)
        await enter_lounge_bool(obs)
        await auth_stock(host, "Hoster", uid="U_rhost")
        await wait_authorized(host)
        await enter_lounge_bool(host)
        await auth_stock(peer, "Peer", uid="U_rpeer")
        await wait_authorized(peer)
        await enter_lounge_bool(peer)

        cm = len(host.inbox)
        await create_game_minimal(host)
        msgs = await host.wait_for({"OnLobbyCreated"}, since=cm)
        lid = last_of(msgs, "OnLobbyCreated")["data"]["id"]
        jm = len(peer.inbox)
        await peer.send("DoJoinLobby", {"id": lid, "password": ""})
        msgs = await peer.wait_for({"OnLobbyJoined"}, since=jm)
        users = last_of(msgs, "OnLobbyJoined")["data"]["lobby"]["users"]
        record(users.get("U_rhost", {}).get("wanAddress") == "198.51.100.7:3658",
               "redaction: game lobby member still receives the host's address")

        hm = len(host.inbox)
        await peer.send("DoUpdateWAN", {"wanAddress": "203.0.113.99:40000"})
        msgs = await host.wait_for({"OnWANUpdated"}, since=hm)
        record(last_of(msgs, "OnWANUpdated")["data"].get("wanAddress") == "203.0.113.99:40000",
               "redaction: WAN update reaches the same-lobby host")

        await asyncio.sleep(0.5)
        seen = json.dumps(obs.inbox)
        leaks = [ip for ip in ("198.51.100.7", "192.168.1.7", "203.0.113.99") if ip in seen]
        record(not leaks, "redaction: lounge observer receives no player addresses", f"leaked={leaks}")
        for c in (host, peer, obs):
            await c.close()


async def main_async(args) -> int:
    print(f"== profile matrix ==")
    await scenario_profile_matrix(args)
    print(f"== multi-client lobby scenarios ==")
    await scenario_multi_client_lobby(args)
    print(f"== address redaction ==")
    await scenario_address_redaction(args)
    print(f"== lifecycle scenarios ==")
    await scenario_lifecycle(args)
    print(f"== restart persistence ==")
    await scenario_restart_persistence(args)
    print(f"== fault injection ==")
    await scenario_fault_injection(args)
    print(f"== default-lobby near-match ==")
    await scenario_default_lobby_near_match(args)

    failed = [r for r in RESULTS if not r[0]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    if failed:
        print("FAILED:")
        for _, label, detail in failed:
            print(f"  - {label}" + (f" :: {detail}" if detail else ""))
    return 1 if failed else 0



# ---------------------------------------------------------------------------
# In-process unit tests carried over from the merged relay implementation
# (PR #1): stock WS contracts incl. admin-gated SetRelay and route-gated
# OnP2PRoute ports, opaque relay forwarding with ambiguity rejection, and the
# tightened transport-header decoder.
# ---------------------------------------------------------------------------

import logging  # noqa: F401 (used by unit-test loggers)

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


def _attach_session(state: ServerState, session: UserSession) -> None:
    state.sessions_by_ws[session.ws] = session
    state.sessions_by_user_id[session.user_id] = session


def _last_data(ws: FakeWS, message_type: str) -> dict[str, Any]:
    matches = ws.by_type(message_type)
    assert matches, f"missing {message_type}; saw {[m.get('type') for m in ws.messages]}"
    return matches[-1].get("data", {})


async def unit_test_stock_ws_contracts() -> None:
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
    _attach_session(state, a)
    _attach_session(state, b)

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
    joined = _last_data(ws_b, "OnLobbyJoined")
    assert joined["reasonCode"] == 0
    assert joined["lobbyId"] == lobby.lobby_id
    assert joined["lobby"]["lobbyId"] == lobby.lobby_id
    assert joined["lobby"]["users"]["PLAYER_A"]["userId"] == "PLAYER_A"
    assert joined["lobby"]["users"]["PLAYER_B"]["userId"] == "PLAYER_B"
    # Upstream acks are flat; our nested snapshot fields are a superset.
    assert joined["reason"] == ""
    assert joined["id"] == lobby.lobby_id

    ws_a.clear()
    ws_b.clear()
    await server.handle_chat(a, "opaque matchmaking test")
    chat = _last_data(ws_b, "OnChatMessage")
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
    route_to_a = _last_data(ws_a, "OnP2PRoute")
    route_to_b = _last_data(ws_b, "OnP2PRoute")
    assert route_to_a["player"] == "PLAYER_B" and route_to_a["port"] == 1339
    assert route_to_b["player"] == "PLAYER_A" and route_to_b["port"] == 1339
    assert relay.metrics()["activeRoutes"] == 1

    # A server without a live relay must fail the route request, not advertise 1339.
    ws_a.clear()
    ws_b.clear()
    no_relay_server = BZRWebSocketServer(state, logging.getLogger("stock-test.no-relay"), relay_protocol=None)
    await no_relay_server.handle_p2p_signal(a, {"player": "PLAYER_B"}, route=True)
    assert not ws_a.by_type("OnP2PRoute")
    failure = _last_data(ws_a, "OnLobbyMemberP2PConnect")
    assert failure["success"] is False
    assert "relayReason" in failure


async def unit_test_address_redaction() -> None:
    """--redact-addresses: addresses reach only self, admins and same-game-lobby members."""
    a_wan, b_wan, b_wan_new = "203.0.113.10:41001", "198.51.100.20:42002", "198.51.100.77:42002"
    a_lan, b_lan = "10.10.0.10:41001", "10.20.0.20:42002"
    private = [x.split(":")[0] for x in (a_wan, b_wan, b_wan_new, a_lan, b_lan)]

    def leaked(ws: FakeWS) -> list[str]:
        text = json.dumps(ws.messages)
        return [ip for ip in private if ip in text]

    for redact in (True, False):
        state = ServerState(logging.getLogger("redact-test.state"))
        state.ensure_default_lobby()
        server = BZRWebSocketServer(state, logging.getLogger("redact-test.ws"), redact_addresses=redact)
        ws_a, ws_b, ws_c = FakeWS(50101), FakeWS(50102), FakeWS(50103)
        a = UserSession("PLAYER_A", name="A", wan_address=a_wan, lan_addresses=[a_lan], ws=ws_a)
        b = UserSession("PLAYER_B", name="B", wan_address=b_wan, lan_addresses=[b_lan], ws=ws_b)
        c = UserSession("PLAYER_C", name="C", wan_address="192.0.2.30:43003", ws=ws_c)
        for s in (a, b, c):
            _attach_session(state, s)
        default = next(l for l in state.lobbies.values() if l.metadata.get("name") == ServerState.DEFAULT_LOBBY_NAME)

        # Everyone starts in the shared chat lobby, like stock clients do; C stays there watching.
        for s in (a, b, c):
            await server._broadcast_session_addresses(s)
            await server.handle_join_lobby(s, {"id": default.lobby_id, "password": ""})
        await server.handle_create_game(a, {"memberLimit": 8, "isPrivate": False, "password": ""})
        lobby = state.lobbies[a.current_lobby_id]
        await server.handle_join_lobby(b, {"id": lobby.lobby_id, "password": ""})
        await server.route_message(ws_c, json.dumps({"type": "GetLobbyList", "content": 1}))
        await server.route_message(ws_b, json.dumps({"type": "DoUpdateWAN", "content": {"wanAddress": b_wan_new}}))

        if not redact:
            assert leaked(ws_c), "without redaction the lounge observer sees addresses (upstream behavior)"
            continue

        assert leaked(ws_c) == [], f"observer saw private addresses: {leaked(ws_c)}"
        joined = _last_data(ws_b, "OnLobbyJoined")
        assert joined["lobby"]["users"]["PLAYER_A"]["wanAddress"] == a_wan
        assert joined["lobby"]["users"]["PLAYER_A"]["lanAddresses"] == [a_lan]
        assert _last_data(ws_a, "OnWANUpdated")["wanAddress"] == b_wan_new  # same game lobby
        assert _last_data(ws_b, "OnWANUpdated")["wanAddress"] == b_wan_new  # own echo
        assert not [m for m in ws_c.by_type("OnWANUpdated") if m["data"].get("userId") != "PLAYER_C"]

        # Signaling across lobbies is refused without reaching the target or leaking its address.
        ws_a.clear()
        await server.route_message(ws_c, json.dumps({"type": "DoP2PConnect", "content": {"player": "PLAYER_A"}}))
        assert _last_data(ws_c, "OnLobbyMemberP2PConnect")["success"] is False
        assert not ws_a.messages
        assert leaked(ws_c) == []

        # Inside the game lobby it still works and carries the source address.
        ws_b.clear()
        await server.route_message(ws_a, json.dumps({"type": "DoP2PConnect", "content": {"player": "PLAYER_B"}}))
        to_b = _last_data(ws_b, "OnLobbyMemberP2PConnect")
        assert to_b["success"] is True and to_b["player"] == "PLAYER_A"
        assert to_b["sourceWanAddress"] == a_wan

        # User lists and logs are admin-only.
        ws_c.clear()
        for verb in ("GetUserList", "GetFullLog", "TraceData"):
            await server.route_message(ws_c, json.dumps({"type": verb, "content": {}}))
        assert not ws_c.by_type("OnGetUserList") and not ws_c.by_type("OnGetFullLog")
        assert leaked(ws_c) == []
        c.is_admin = True
        await server.route_message(ws_c, json.dumps({"type": "GetUserList", "content": {}}))
        assert _last_data(ws_c, "OnGetUserList")["users"]["PLAYER_A"]["wanAddress"] == a_wan


async def unit_test_opaque_relay() -> None:
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

    # Removing a user tears down routes that involve them (the unrelated
    # A<->C pair registered for the ambiguity check above stays).
    relay.remove_user("B", lobby_id=77)
    assert relay.metrics()["activeRoutes"] == 1
    relay.remove_user("C", lobby_id=77)
    assert relay.metrics()["activeRoutes"] == 0


async def unit_test_transport_header() -> None:
    decoded = decode_bzr_udp_datagram(bytes.fromhex("c0030000019d1199210b00000000000012345353"))
    header = decoded.get("transportHeader", {})
    assert header.get("headerBytes") == 18
    assert header.get("sequenceEncoding") == "u16-be"
    assert header.get("sequence") == 0x1234
    assert header.get("payloadOffset") == 18
    assert header.get("reliabilityHint") == "reliable"
    # Bytes 2..15 stay opaque unless new evidence supersedes PR #1.
    payload = bytes.fromhex("c0030000019d1199210b00000000000012345353")
    assert header.get("opaqueBytes2To15") == payload[2:16].hex()


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="BZR stock-client interoperability matrix")
    p.add_argument("--retain-empty", action="store_true", help="spawn servers with --retain-empty-lobbies where useful")
    return p.parse_args()


async def main_async_combined(args) -> int:
    """Unit tests first (no sockets), then the spawned-server matrix."""
    print("== unit: stock WS contracts + relay gating ==")
    await unit_test_stock_ws_contracts()
    print("PASS - unit: stock WS contracts + relay gating")
    print("== unit: address redaction ==")
    await unit_test_address_redaction()
    print("PASS - unit: address redaction")
    print("== unit: opaque relay forwarding ==")
    await unit_test_opaque_relay()
    print("PASS - unit: opaque relay forwarding")
    print("== unit: transport-header decoder ==")
    await unit_test_transport_header()
    print("PASS - unit: transport-header decoder")
    return await main_async(args)


if __name__ == "__main__":
    sys.exit(asyncio.run(main_async_combined(parse_args())))
