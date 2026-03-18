#!/usr/bin/env python3
"""
Smoke tests for the BZR dedicated-server compatibility shim.

Usage:
  python smoke_test.py
  python smoke_test.py --host 192.168.1.50 --ws-port 1337 --udp-port 1338
  python smoke_test.py --skip-health
"""

from __future__ import annotations

import argparse
import asyncio
import json
import socket
import sys
import urllib.error
import urllib.request
from typing import Any

try:
    import websockets
except Exception as exc:  # pragma: no cover
    raise SystemExit("Missing dependency 'websockets'. Install with: pip install -r requirements.txt") from exc


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Smoke test BZR dedicated server")
    parser.add_argument("--host", default="127.0.0.1", help="Server host")
    parser.add_argument("--ws-port", type=int, default=1337, help="WebSocket port")
    parser.add_argument("--udp-port", type=int, default=1338, help="UDP port")
    parser.add_argument("--health-port", type=int, default=8080, help="Health endpoint port")
    parser.add_argument("--skip-health", action="store_true", help="Skip health endpoint check")
    parser.add_argument("--timeout", type=float, default=3.0, help="Receive timeout in seconds")
    return parser.parse_args()


async def recv_types(ws: Any, expected_count: int, timeout: float) -> list[str]:
    types: list[str] = []
    for _ in range(expected_count):
        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
        types.append(str(msg.get("type")))
    return types


async def recv_until_types(ws: Any, wanted: set[str], timeout: float, max_messages: int = 12) -> list[dict[str, Any]]:
    seen: list[dict[str, Any]] = []
    matched: set[str] = set()
    for _ in range(max_messages):
        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
        seen.append(msg)
        msg_type = str(msg.get("type"))
        if msg_type in wanted:
            matched.add(msg_type)
        if matched == wanted:
            return seen
    raise AssertionError(f"Timed out waiting for {sorted(wanted)}; saw {[m.get('type') for m in seen]}")


async def ws_smoke(host: str, port: int, timeout: float) -> tuple[bool, str]:
    uri = f"ws://{host}:{port}"
    async with websockets.connect(uri) as ws:
        await ws.send(
            json.dumps(
                {
                    "type": "Authorization",
                    "content": {
                        "authtype": "web",
                        "id": "U_SMOKE",
                        "name": "SmokeTester",
                        "playerName": "SmokeTester",
                        "clientVersion": "2.2.301",
                        "wanAddress": "198.51.100.44:40000",
                        "lanAddresses": ["127.0.0.1:40000", "192.168.1.44:40000"],
                    },
                }
            )
        )
        first = await recv_until_types(
            ws,
            {"OnAuthorization", "OnWhitelistUpdated", "OnRelayUpdated", "OnWANUpdated", "OnLANUpdated"},
            timeout,
            max_messages=10,
        )
        first_types = [str(msg.get("type")) for msg in first]
        if "OnAuthorization" not in first_types:
            return False, f"Expected OnAuthorization, got {first_types}"

        await ws.send(json.dumps({"type": "GetStatLog", "content": True}))
        stat_seen = False
        for _ in range(4):
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
            if msg.get("type") == "OnGetStatLog":
                stat_seen = isinstance(msg.get("data", {}).get("stats"), str)
                break
        if not stat_seen:
            return False, "Did not receive compatible OnGetStatLog payload"

        await ws.send(json.dumps({"type": "GetFullLog", "content": True}))
        log_seen = False
        for _ in range(4):
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
            if msg.get("type") == "OnGetFullLog":
                stdout = msg.get("data", {}).get("stdout", {})
                log_seen = isinstance(stdout.get("history"), list)
                break
        if not log_seen:
            return False, "Did not receive compatible OnGetFullLog payload"

        await ws.send(json.dumps({"type": "DoEnterLounge", "content": True}))
        lounge = await recv_until_types(ws, {"OnDoEnterLounge", "OnLobbyListChanged"}, timeout, max_messages=4)
        lounge_types = [str(msg.get("type")) for msg in lounge]
        if "OnDoEnterLounge" not in lounge_types:
            return False, f"Expected OnDoEnterLounge, got {lounge_types}"

        await ws.send(json.dumps({"type": "SetPlayerData", "content": {"key": "name", "value": "SmokeTester2"}}))
        player_seen = False
        for _ in range(6):
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
            if msg.get("type") == "OnUserDataChanged":
                users = msg.get("data", {}).get("users", {})
                player_seen = "U_SMOKE" in users
                break
        if not player_seen:
            return False, "Did not receive compatible OnUserDataChanged payload"

        await ws.send(json.dumps({"type": "DoUpdateWAN", "content": {"wanAddress": "198.51.100.44:40001"}}))
        wan_seen = False
        for _ in range(4):
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
            if msg.get("type") == "OnWANUpdated":
                wan_seen = msg.get("data", {}).get("wanAddress") == "198.51.100.44:40001"
                break
        if not wan_seen:
            return False, "Did not receive compatible OnWANUpdated payload"

        await ws.send(
            json.dumps(
                {
                    "type": "CreateGame",
                    "content": {"name": "SmokeLobby", "isPrivate": False, "memberLimit": 8, "password": ""},
                }
            )
        )
        created_ok = False
        for _ in range(6):
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
            if msg.get("type") == "OnLobbyCreated" and msg.get("data", {}).get("success") is True:
                created_ok = True
                break
        if not created_ok:
            return False, "Did not receive successful OnLobbyCreated"

        await ws.send(json.dumps({"type": "DoP2PConnect", "content": {"targetId": "U_SMOKE"}}))
        p2p_seen = False
        for _ in range(6):
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
            if msg.get("type") == "OnLobbyMemberP2PConnect":
                p2p_seen = msg.get("data", {}).get("sourceUserId") == "U_SMOKE"
                break
        if not p2p_seen:
            return False, "Did not receive OnLobbyMemberP2PConnect"

        await ws.send(json.dumps({"type": "DoSendChat", "content": "smoke test chat"}))
        for _ in range(6):
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
            if msg.get("type") == "OnChatMessage":
                return True, "WS auth/update/create/p2p/chat flow succeeded"
        return False, "Did not receive OnChatMessage"


def udp_smoke(host: str, port: int, timeout: float) -> tuple[bool, str]:
    payload = bytes.fromhex("40020000019c744772360000000000000000")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.sendto(payload, (host, port))
        data, _ = sock.recvfrom(1024)
    finally:
        sock.close()

    if not data.startswith(payload):
        return False, "UDP reply did not begin with original payload"
    if len(data) != len(payload) + 7:
        return False, f"UDP reply length unexpected: {len(data)} bytes"
    return True, "UDP handshake shim reply succeeded"


def health_smoke(host: str, port: int, timeout: float) -> tuple[bool, str]:
    url = f"http://{host}:{port}/health"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        return False, f"Health endpoint unreachable: {exc}"
    except Exception as exc:
        return False, f"Health endpoint invalid response: {exc}"

    if data.get("ok") is not True:
        return False, f"Health endpoint reported not ok: {data}"
    return True, "Health endpoint check succeeded"


def protocol_smoke(host: str, port: int, timeout: float) -> tuple[bool, str]:
    url = f"http://{host}:{port}/protocol"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        return False, f"Protocol endpoint invalid response: {exc}"
    supported = data.get("supportedMessages", {})
    if "client" not in supported or "server" not in supported:
        return False, f"Protocol endpoint missing supported messages: {data}"
    return True, "Protocol endpoint check succeeded"


def captures_smoke(host: str, port: int, timeout: float) -> tuple[bool, str]:
    url = f"http://{host}:{port}/captures"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        return False, f"Capture endpoint invalid response: {exc}"
    protocol = data.get("protocol", {})
    if not isinstance(protocol.get("history"), list):
        return False, f"Capture endpoint missing protocol history: {data}"
    return True, "Capture endpoint check succeeded"


async def main() -> int:
    args = parse_args()
    ws_ok, ws_msg = await ws_smoke(args.host, args.ws_port, args.timeout)
    udp_ok, udp_msg = udp_smoke(args.host, args.udp_port, args.timeout)
    health_ok, health_msg = (True, "Skipped")
    protocol_ok, protocol_msg = (True, "Skipped")
    captures_ok, captures_msg = (True, "Skipped")
    if not args.skip_health:
        health_ok, health_msg = health_smoke(args.host, args.health_port, args.timeout)
        protocol_ok, protocol_msg = protocol_smoke(args.host, args.health_port, args.timeout)
        captures_ok, captures_msg = captures_smoke(args.host, args.health_port, args.timeout)

    print(f"WS : {'PASS' if ws_ok else 'FAIL'} - {ws_msg}")
    print(f"UDP: {'PASS' if udp_ok else 'FAIL'} - {udp_msg}")
    print(f"HTTP: {'PASS' if health_ok else 'FAIL'} - {health_msg}")
    print(f"PROTO: {'PASS' if protocol_ok else 'FAIL'} - {protocol_msg}")
    print(f"CAP  : {'PASS' if captures_ok else 'FAIL'} - {captures_msg}")
    return 0 if (ws_ok and udp_ok and health_ok and protocol_ok and captures_ok) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
