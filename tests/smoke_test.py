#!/usr/bin/env python3
"""
Smoke tests for the BZR dedicated-server compatibility shim.

Usage:
  python tests/smoke_test.py
  python tests/smoke_test.py --host 192.168.1.50 --ws-port 1337 --udp-port 1338
  python tests/smoke_test.py --skip-health
"""

from __future__ import annotations

import argparse
import asyncio
import json
import socket
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tools")]

from analyze_pcapng_udp import build_consensus_only_summary
from compare_capture_consensus import compare_consensus, render_markdown_report
from server import decode_bzr_udp_datagram, summarize_protocol_history

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
            # A server running --redact-addresses refuses logs to non-admins.
            if msg.get("type") == "OnStdoutLogEntry" and "admin only" in str(msg.get("data", {}).get("message", "")):
                log_seen = True
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
                # Official shape: {member: <user id>, lobbyId}.
                player_seen = msg.get("data", {}).get("member") == "U_SMOKE"
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
                    # Stock CreateGame builder sends no synthetic name field.
                    "content": {"isPrivate": False, "memberLimit": 8, "password": ""},
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

        await ws.send(json.dumps({"type": "DoP2PConnect", "content": {"player": "U_SMOKE", "reset": False}}))
        p2p_seen = False
        for _ in range(6):
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
            if msg.get("type") == "OnLobbyMemberP2PConnect":
                data = msg.get("data", {})
                p2p_seen = (
                    data.get("member") == "U_SMOKE"
                    and data.get("connect") is True
                    and data.get("reset") is False
                )
                break
        if not p2p_seen:
            return False, "Did not receive OnLobbyMemberP2PConnect"

        await ws.send(json.dumps({"type": "DoSendChat", "content": "smoke test chat"}))
        for _ in range(6):
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
            if msg.get("type") == "OnChatMessage":
                return True, "WS auth/update/create/p2p/chat flow succeeded"
        return False, "Did not receive OnChatMessage"


async def rehost_smoke(host: str, port: int, timeout: float) -> tuple[bool, str]:
    """Create a game, mark ready/launched, create again: the new room must start clean."""
    uid = "U_REHOST"
    async with websockets.connect(f"ws://{host}:{port}") as ws:
        await ws.send(json.dumps({"type": "Authorization", "content": {
            "authtype": "web", "id": uid, "name": "RehostTester", "playerName": "RehostTester",
            "clientVersion": "2.2.301"}}))
        auth = await recv_until_types(ws, {"OnAuthorization"}, timeout, max_messages=10)
        if "OnAuthorization" not in [str(m.get("type")) for m in auth]:
            return False, "Expected OnAuthorization"

        async def create_game() -> int | None:
            await ws.send(json.dumps({"type": "CreateGame",
                                      "content": {"isPrivate": False, "memberLimit": 8, "password": ""}}))
            for _ in range(12):
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
                data = msg.get("data", {})
                if msg.get("type") == "OnLobbyCreated" and data.get("success") is True:
                    return int(data["id"])
            return None

        async def own_metadata(lid: int) -> dict[str, Any] | None:
            await ws.send(json.dumps({"type": "GetLobbyList", "content": True}))
            for _ in range(40):
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
                lobbies = msg.get("data", {}).get("lobbies")
                if msg.get("type") == "OnGetLobbyList" and isinstance(lobbies, dict):
                    lobby = lobbies.get(str(lid)) or {}
                    return (lobby.get("users", {}).get(uid) or {}).get("metadata")
            return None

        first = await create_game()
        if first is None:
            return False, "First CreateGame failed"
        for key, value in (("friendID", "S_REHOST"), ("ready", "1"), ("launched", "1"), ("team", "1")):
            await ws.send(json.dumps({"type": "SetPlayerData", "content": {"key": key, "value": value}}))
        meta = await own_metadata(first)
        if not meta or meta.get("ready") != "1" or meta.get("launched") != "1":
            return False, f"First room did not record ready/launched: {meta}"

        second = await create_game()
        if second is None or second == first:
            return False, f"Second CreateGame failed ({second})"
        meta = await own_metadata(second)
        if meta is None:
            return False, "Second room has no host member metadata"
        stale = sorted(k for k in ("ready", "launched", "team") if k in meta)
        if stale:
            return False, f"Re-hosted room inherited stale member keys {stale}: {meta}"
        if meta.get("friendID") != "S_REHOST":
            return False, f"friendID did not survive the re-host: {meta}"
        await ws.send(json.dumps({"type": "DoExitLobby", "content": True}))
        return True, f"Re-host room {second} starts without stale ready/launched"


def udp_smoke(host: str, port: int, timeout: float) -> tuple[bool, str]:
    payload = bytes.fromhex("40020000019c744772360000000000000000")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.bind(("0.0.0.0", 40123))
        sock.sendto(payload, (host, port))
        data, _ = sock.recvfrom(1024)
    finally:
        sock.close()

    if not data.startswith(payload):
        return False, "UDP reply did not begin with original payload"
    if len(data) != len(payload) + 7:
        return False, f"UDP reply length unexpected: {len(data)} bytes"
    trailer = data[-7:]
    expected = b"\x04" + socket.inet_aton(host) + (40123).to_bytes(2, "big")
    if trailer != expected:
        return False, f"UDP reply trailer unexpected: {trailer.hex()} != {expected.hex()}"
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


def decoder_smoke() -> tuple[bool, str]:
    cases = [
        (
            "40000000019d1199a1dc00000009000000054b41",
            "KA",
            "peer_keepalive",
            "medium",
            {},
        ),
        (
            "40000000019d11998bb10000000500000000535301",
            "SS",
            "peer_session_sync",
            "medium",
            {"syncVariant": 0x01},
        ),
        (
            "40000000019d11998a050000000100000000505012a7010000000000000000000000000000000000",
            "PP",
            "peer_ping_request",
            "high",
            {
                "timingFields": {
                    "localMsTick": 108306,
                    "clockOffset": 0,
                    "hostMsTick": 0,
                    "delayUnits": 0,
                    "flags": 0,
                }
            },
        ),
        (
            "40000000019d11999f0b0000000000000001505212a701003bf80000016f6e0a",
            "PR",
            "peer_ping_response",
            "high",
            {
                "timingFields": {
                    "echoedPingMsTick": 108306,
                    "responderMsTick": 63547,
                    "responseWord": "0x0a6e6f01",
                }
            },
        ),
        (
            "40000000019d11998a0100000000000000005042a00f00000c42bb91",
            "PB",
            "peer_timing_sideband",
            "low",
            {
                "timingFields": {
                    "bandwidthTarget": 4000,
                    "opaqueWord": "0x91bb420c",
                    "opaqueLow16": 16908,
                    "opaqueHigh16": 37307,
                }
            },
        ),
        (
            "c0000000019d119a4f210000000d00000018504f19845542060346000000",
            "PO",
            "peer_control_unknown_po",
            "low",
            {
                "timingFields": {
                    "clockSeconds": 53.379,
                    "modeWord": "0x0306",
                    "sequenceValue": 70,
                    "laneValue": 0,
                }
            },
        ),
    ]
    for payload_hex, marker, control_event, confidence, expected in cases:
        raw = bytes.fromhex(payload_hex)
        decoded = decode_bzr_udp_datagram(raw)
        transport_header = decoded.get("transportHeader", {})
        expected_sequence = int.from_bytes(raw[16:18], "big")
        if transport_header.get("headerBytes") != 18 or decoded.get("payloadOffset") != 18:
            return False, f"{marker} common transport boundary mismatch: {decoded}"
        if transport_header.get("sequence") != expected_sequence or transport_header.get("sequenceEncoding") != "u16-be":
            return False, f"{marker} transport sequence mismatch: {decoded}"
        if decoded.get("family") != "peer_control_marker":
            return False, f"{marker} marker did not decode as peer_control_marker: {decoded}"
        if decoded.get("marker") != marker:
            return False, f"{marker} marker mismatch: {decoded}"
        if decoded.get("controlEvent") != control_event:
            return False, f"{marker} controlEvent mismatch: {decoded}"
        if decoded.get("meaningConfidence") != confidence:
            return False, f"{marker} meaningConfidence mismatch: {decoded}"
        for key, value in expected.items():
            if decoded.get(key) != value:
                return False, f"{marker} {key} mismatch: {decoded}"

    state_payload = (
        "40000000019d1199a1e30000000a000000055f0021b00641358401000100000000000000"
        "f7c81f402800300c6376747572720000ffff83000000000000000000000000000000000000"
        "ffff8000330402000100000000000000f7d21fc02800800c637672656d7000000000000000"
        "000000000000000000000000000000ff008000"
    )
    state_decoded = decode_bzr_udp_datagram(bytes.fromhex(state_payload))
    if state_decoded.get("family") != "peer_state_block":
        return False, f"state block family mismatch: {state_decoded}"
    expected_header = {
        "stateTimeSeconds": 8.418,
        "stateStreamWord": 99381,
        "statePlayerIndex": 1,
        "stateHeaderWord": "0x00000000",
    }
    if state_decoded.get("stateHeader") != expected_header:
        return False, f"state block header mismatch: {state_decoded}"
    if state_decoded.get("unitCodes") != ["cvturr", "cvremp"]:
        return False, f"state block unitCodes mismatch: {state_decoded}"
    if state_decoded.get("stateObjectCount") != 2:
        return False, f"state block object count mismatch: {state_decoded}"

    object_payload = (
        "c0000000019d11998a2500000002000000007a7501000221000000002f2f84010001000000000000"
        "00f7c81f402800300c6376747572720000637674757272305f74757272657474616e6b00"
    )
    object_decoded = decode_bzr_udp_datagram(bytes.fromhex(object_payload))
    if object_decoded.get("family") != "peer_object_descriptor":
        return False, f"object descriptor family mismatch: {object_decoded}"
    if object_decoded.get("transportReliability") != "reliable":
        return False, f"object transport mismatch: {object_decoded}"
    expected_object_header = {
        "objectVariant": 1,
        "objectIndex": 2,
        "descriptorFlags": 33,
        "objectHeaderWord": "0x01842f2f",
        "objectOwnerFlags": 0,
        "objectOwnerIndex": 1,
    }
    if object_decoded.get("objectHeader") != expected_object_header:
        return False, f"object header mismatch: {object_decoded}"
    if object_decoded.get("objectCodes") != ["cvturr"]:
        return False, f"object codes mismatch: {object_decoded}"
    if object_decoded.get("objectNames") != ["cvturr0_turrettank"]:
        return False, f"object names mismatch: {object_decoded}"
    if object_decoded.get("objectKind") != "turrettank":
        return False, f"object kind mismatch: {object_decoded}"
    if object_decoded.get("objectOrdinal") != 0:
        return False, f"object ordinal mismatch: {object_decoded}"
    if object_decoded.get("sideLabel") != "c_side":
        return False, f"object side label mismatch: {object_decoded}"
    if object_decoded.get("descriptorFamilyHint") != "c_side_turrettank_descriptor":
        return False, f"object family hint mismatch: {object_decoded}"
    if object_decoded.get("descriptorArchetypeHint") != "turrettank":
        return False, f"object archetype hint mismatch: {object_decoded}"
    if object_decoded.get("descriptorPayloadFamilyHint") != "c_side_structure_payload":
        return False, f"object payload family hint mismatch: {object_decoded}"
    expected_descriptor_sketch = {
        "classWord": "0x2f2f",
        "subtypeWord": "0x0184",
        "ownerWord": "0x0100",
        "controlWord": "0x00000000",
        "paramWord": "0xf7000000",
        "vectorWordA": "0x28401fc8",
        "vectorWordB": "0x630c3000",
    }
    if object_decoded.get("descriptorSketch") != expected_descriptor_sketch:
        return False, f"object descriptor sketch mismatch: {object_decoded}"
    if object_decoded.get("descriptorPattern") != "0x2f2f/0x0184/0x0100":
        return False, f"object descriptor pattern mismatch: {object_decoded}"
    expected_field_hints = {
        "identityWords": {
            "classWord": "0x2f2f",
            "subtypeWord": "0x0184",
            "ownerWord": "0x0100",
        },
        "payloadWords": {
            "controlWord": "0x00000000",
            "paramWord": "0xf7000000",
            "vectorWordA": "0x28401fc8",
            "vectorWordB": "0x630c3000",
        },
        "ownerLane": "owner_index_1",
        "stabilityAssessment": "class/subtype/owner words look family-stable in March 21 captures; later words vary more like descriptor payload",
    }
    if object_decoded.get("descriptorFieldHints") != expected_field_hints:
        return False, f"object descriptor field hints mismatch: {object_decoded}"
    expected_payload_hints = {
        "controlLaneWords": {
            "controlWord": "0x00000000",
            "paramWord": "0xf7000000",
        },
        "vectorLaneWords": {
            "vectorWordA": "0x28401fc8",
            "vectorWordB": "0x630c3000",
        },
        "payloadLaneAssessment": "controlWord/paramWord look more family-side dependent; vectorWordA/vectorWordB vary more like per-descriptor payload",
        "controlSignature": "c_side_structure_payload:0x00000000/0xf7000000",
    }
    if object_decoded.get("descriptorPayloadHints") != expected_payload_hints:
        return False, f"object descriptor payload hints mismatch: {object_decoded}"

    scrap_payload = (
        "c0000000019d1199a7b00000000c000000087a750100fe20000000002e2e00040001000000dc200000350200002838420a6e707363723100000000803f6e7073637231305f736372617000"
    )
    scrap_decoded = decode_bzr_udp_datagram(bytes.fromhex(scrap_payload))
    if scrap_decoded.get("family") != "peer_object_descriptor":
        return False, f"scrap descriptor family mismatch: {scrap_decoded}"
    if scrap_decoded.get("primaryObjectCode") != "npscr1":
        return False, f"scrap code mismatch: {scrap_decoded}"
    if scrap_decoded.get("objectKind") != "scrap":
        return False, f"scrap kind mismatch: {scrap_decoded}"
    if scrap_decoded.get("objectOrdinal") != 10:
        return False, f"scrap ordinal mismatch: {scrap_decoded}"
    if scrap_decoded.get("descriptorFamilyHint") != "neutral_scrap_descriptor":
        return False, f"scrap family hint mismatch: {scrap_decoded}"
    if scrap_decoded.get("descriptorPayloadFamilyHint") != "neutral_scrap_payload":
        return False, f"scrap payload family hint mismatch: {scrap_decoded}"
    expected_scrap_payload_hints = {
        "controlLaneWords": {
            "controlWord": "0xdc000000",
            "paramWord": "0x35000020",
        },
        "vectorLaneWords": {
            "vectorWordA": "0x28000002",
            "vectorWordB": "0x6e0a4238",
        },
        "payloadLaneAssessment": "controlWord/paramWord look more family-side dependent; vectorWordA/vectorWordB vary more like per-descriptor payload",
        "controlSignature": "neutral_scrap_payload:0xdc000000/0x35000020",
    }
    if scrap_decoded.get("descriptorPayloadHints") != expected_scrap_payload_hints:
        return False, f"scrap payload hints mismatch: {scrap_decoded}"

    object_batch_payload = (
        "40000000019d11998a2500000002000000007a7501000c0000000000140000000000000000000000000300000000010000"
    )
    batch_decoded = decode_bzr_udp_datagram(bytes.fromhex(object_batch_payload))
    if batch_decoded.get("family") != "peer_object_batch":
        return False, f"object batch family mismatch: {batch_decoded}"
    if batch_decoded.get("objectSetupEvent") != "object_roster_batch":
        return False, f"object batch event mismatch: {batch_decoded}"
    if batch_decoded.get("batchDescriptorCount") != 3:
        return False, f"object batch count mismatch: {batch_decoded}"
    if batch_decoded.get("batchOwnerIndex") != 1:
        return False, f"object batch owner mismatch: {batch_decoded}"

    object_presence_payload = (
        "40000000019d11999f81000000050000000a7a75020002210000000006060101000100"
    )
    presence_decoded = decode_bzr_udp_datagram(bytes.fromhex(object_presence_payload))
    if presence_decoded.get("family") != "peer_object_presence":
        return False, f"object presence family mismatch: {presence_decoded}"
    if presence_decoded.get("objectSetupEvent") != "object_presence":
        return False, f"object presence event mismatch: {presence_decoded}"
    if presence_decoded.get("presenceKeyWord") != "0x2102":
        return False, f"object presence keyword mismatch: {presence_decoded}"

    object_counter_payload = (
        "40000000019d119c0bb80000007f000000687a7502009c02000000000606019c000000"
    )
    counter_decoded = decode_bzr_udp_datagram(bytes.fromhex(object_counter_payload))
    if counter_decoded.get("family") != "peer_object_counter":
        return False, f"object counter family mismatch: {counter_decoded}"
    if counter_decoded.get("objectSetupEvent") != "object_counter":
        return False, f"object counter event mismatch: {counter_decoded}"
    if counter_decoded.get("counterValue") != 0x029C:
        return False, f"object counter value mismatch: {counter_decoded}"

    pilot_payload = (
        "40000000019d119c200000000073000000857a750200af21000000002b2b0807000200a1ffd5a83740a881c1192bdbbcfe627370696c6f0000627370696c6f335f706572736f6e00"
    )
    pilot_decoded = decode_bzr_udp_datagram(bytes.fromhex(pilot_payload))
    if pilot_decoded.get("personRole") != "pilot":
        return False, f"pilot role mismatch: {pilot_decoded}"
    if pilot_decoded.get("descriptorFamilyHint") != "b_side_person_pilot_descriptor":
        return False, f"pilot family hint mismatch: {pilot_decoded}"
    if pilot_decoded.get("descriptorArchetypeHint") != "person_pilot":
        return False, f"pilot archetype hint mismatch: {pilot_decoded}"
    if pilot_decoded.get("descriptorPayloadFamilyHint") != "b_side_person_payload":
        return False, f"pilot payload family hint mismatch: {pilot_decoded}"
    expected_pilot_payload_hints = {
        "controlLaneWords": {
            "controlWord": "0xd5ffa100",
            "paramWord": "0xa84037a8",
        },
        "vectorLaneWords": {
            "vectorWordA": "0x2b19c181",
            "vectorWordB": "0x62febcdb",
        },
        "payloadLaneAssessment": "controlWord/paramWord look more family-side dependent; vectorWordA/vectorWordB vary more like per-descriptor payload",
        "controlSignature": "b_side_person_payload:0xd5ffa100/0xa84037a8",
    }
    if pilot_decoded.get("descriptorPayloadHints") != expected_pilot_payload_hints:
        return False, f"pilot payload hints mismatch: {pilot_decoded}"

    object_control_payload = (
        "c0000000019d119b17720000003c0000003b7a750100af210000000006060507000200"
    )
    object_control_decoded = decode_bzr_udp_datagram(bytes.fromhex(object_control_payload))
    if object_control_decoded.get("family") != "peer_object_control":
        return False, f"object control family mismatch: {object_control_decoded}"
    if object_control_decoded.get("controlBodyBytes") != 17:
        return False, f"object control body bytes mismatch: {object_control_decoded}"
    if object_control_decoded.get("controlBodySignature") != "0100af210000000006060507000200":
        return False, f"object control signature mismatch: {object_control_decoded}"
    if object_control_decoded.get("controlModeWord") != "0x0606":
        return False, f"object control mode word mismatch: {object_control_decoded}"
    if object_control_decoded.get("controlTailWord") != "0x07050606":
        return False, f"object control tail word mismatch: {object_control_decoded}"

    object_control_short_payload = (
        "c0000000019d119bd88d00000060000000627a7501001000000000000403000000"
    )
    object_control_short_decoded = decode_bzr_udp_datagram(bytes.fromhex(object_control_short_payload))
    if object_control_short_decoded.get("family") != "peer_object_control":
        return False, f"short object control family mismatch: {object_control_short_decoded}"
    if object_control_short_decoded.get("controlBodyBytes") != 15:
        return False, f"short object control body bytes mismatch: {object_control_short_decoded}"
    if object_control_short_decoded.get("controlBodySignature") != "01001000000000000403000000":
        return False, f"short object control signature mismatch: {object_control_short_decoded}"
    if object_control_short_decoded.get("controlModeWord") != "0x0304":
        return False, f"short object control mode word mismatch: {object_control_short_decoded}"
    if object_control_short_decoded.get("controlTailWord") != "0x00000304":
        return False, f"short object control tail word mismatch: {object_control_short_decoded}"

    setup_marker_payload = (
        "c0000000019d119bd63000000062000000627a7502000f00000000000401000000"
    )
    setup_marker_decoded = decode_bzr_udp_datagram(bytes.fromhex(setup_marker_payload))
    if setup_marker_decoded.get("objectSetupEvent") != "object_setup_marker":
        return False, f"setup marker decode mismatch: {setup_marker_decoded}"

    recycler_payload = (
        "c0000000019d11998a2800000003000000007a7501000121000000002d2d0402000100000000000000f7d21fc02800800c637672656d700000637672656d70305f72656379636c657200"
    )
    recycler_decoded = decode_bzr_udp_datagram(bytes.fromhex(recycler_payload))
    if recycler_decoded.get("primaryObjectCode") != "cvremp":
        return False, f"recycler primary code mismatch: {recycler_decoded}"
    if recycler_decoded.get("objectKind") != "recycler":
        return False, f"recycler kind mismatch: {recycler_decoded}"

    person_payload = (
        "c0000000019d1199c2c20000000e000000087a750100ff21000000002b2b0403000100cc7da4210461fcf11fb82b840b0b6373757365720000637375736572305f706572736f6e00"
    )
    person_decoded = decode_bzr_udp_datagram(bytes.fromhex(person_payload))
    if person_decoded.get("primaryObjectCode") != "csuser":
        return False, f"person primary code mismatch: {person_decoded}"
    if person_decoded.get("objectKind") != "person":
        return False, f"person kind mismatch: {person_decoded}"

    descriptor_state_history = [
        {"channel": "udp", "peer": "192.168.0.63:50745", "time": "d0", "decoder": object_decoded},
        {"channel": "udp", "peer": "192.168.0.63:50745", "time": "d1", "decoder": recycler_decoded},
        {"channel": "udp", "peer": "192.168.0.63:50745", "time": "d2", "decoder": person_decoded},
        {"channel": "udp", "peer": "192.168.0.63:50745", "time": "d3", "decoder": scrap_decoded},
        {"channel": "udp", "peer": "192.168.0.63:50745", "time": "d4", "decoder": state_decoded},
    ]
    descriptor_state_summary = summarize_protocol_history(descriptor_state_history)
    expected_descriptor_state_correlation = {
        "descriptorUnitCodes": {
            "cvremp": 1,
            "csuser": 1,
            "cvturr": 1,
            "npscr": 1,
            "npscr1": 1,
        },
        "stateUnitCodes": {
            "cvremp": 1,
            "cvturr": 1,
        },
        "sharedUnitCodes": ["cvremp", "cvturr"],
        "descriptorOnlyUnitCodes": ["csuser", "npscr", "npscr1"],
        "stateOnlyUnitCodes": [],
        "descriptorKinds": {
            "person": 1,
            "recycler": 1,
            "scrap": 1,
            "turrettank": 1,
        },
        "stateKindCounts": {
            "recycler": 1,
            "turrettank": 1,
        },
        "stateUnitCodeKinds": {
            "cvremp": "recycler",
            "cvturr": "turrettank",
        },
        "stateVisibleObjectKinds": ["recycler", "turrettank"],
        "descriptorOnlyObjectKinds": ["person", "scrap"],
        "assessment": "state blocks reuse a subset of descriptor unit codes in this sample; person and scrap remain descriptor-only",
    }
    if descriptor_state_summary.get("descriptorStateCorrelations") != expected_descriptor_state_correlation:
        return False, f"descriptor/state correlation mismatch: {descriptor_state_summary}"

    descriptor_followup_history = [
        {"channel": "udp", "peer": "192.168.0.63:50745", "time": "f0", "decoder": person_decoded},
        {"channel": "udp", "peer": "192.168.0.63:50745", "time": "f1", "decoder": state_decoded},
        {"channel": "udp", "peer": "192.168.0.63:50745", "time": "f2", "decoder": presence_decoded},
        {"channel": "udp", "peer": "192.168.0.63:50745", "time": "f3", "decoder": person_decoded},
        {"channel": "udp", "peer": "192.168.0.63:50745", "time": "f4", "decoder": counter_decoded},
        {"channel": "udp", "peer": "192.168.0.63:50745", "time": "f5", "decoder": scrap_decoded},
        {"channel": "udp", "peer": "192.168.0.63:50745", "time": "f6", "decoder": state_decoded},
        {"channel": "udp", "peer": "192.168.0.63:50745", "time": "f7", "decoder": scrap_decoded},
        {"channel": "udp", "peer": "192.168.0.63:50745", "time": "f8", "decoder": state_decoded},
    ]
    descriptor_followup_summary = summarize_protocol_history(descriptor_followup_history)
    expected_descriptor_followups = {
        "lookaheadPackets": 12,
        "objectKinds": {
            "person": {
                "descriptorCount": 2,
                "firstNonDescriptorEvents": {
                    "peer_state_block": 1,
                    "object_counter": 1,
                },
                "firstShortObjectEvents": {
                    "object_counter": 1,
                    "object_presence": 1,
                },
                "descriptorsWithShortObjectFollowup": 2,
                "descriptorsWithoutShortObjectFollowup": 0,
                "assessment": "descriptors often transition into short object/setup followup packets",
            },
            "scrap": {
                "descriptorCount": 2,
                "firstNonDescriptorEvents": {
                    "peer_state_block": 2,
                },
                "firstShortObjectEvents": {},
                "descriptorsWithShortObjectFollowup": 0,
                "descriptorsWithoutShortObjectFollowup": 2,
                "assessment": "descriptors usually hand off directly into state traffic without a decoded short object/setup followup",
            },
        },
    }
    if descriptor_followup_summary.get("descriptorFollowups") != expected_descriptor_followups:
        return False, f"descriptor followup summary mismatch: {descriptor_followup_summary}"

    po_special_payload = (
        "c0000000019d119c3d940000008c00000073504f9458334306020d000200"
    )
    po_special_decoded = decode_bzr_udp_datagram(bytes.fromhex(po_special_payload))
    unresolved_control_history = [
        {"channel": "udp", "peer": "192.168.0.63:50745", "time": "u0", "decoder": decode_bzr_udp_datagram(bytes.fromhex("c0000000019d119a4f210000000d00000018504f19845542060346000000"))},
        {"channel": "udp", "peer": "192.168.0.63:50745", "time": "u1", "decoder": scrap_decoded},
        {"channel": "udp", "peer": "192.168.0.63:50745", "time": "u2", "decoder": po_special_decoded},
        {"channel": "udp", "peer": "192.168.0.63:50745", "time": "u3", "decoder": setup_marker_decoded},
        {"channel": "udp", "peer": "192.168.0.63:50745", "time": "u4", "decoder": object_control_decoded},
        {"channel": "udp", "peer": "192.168.0.63:50745", "time": "u5", "decoder": object_control_short_decoded},
    ]
    unresolved_control_summary = summarize_protocol_history(unresolved_control_history)
    expected_unresolved_control_comparisons = {
        "poControlSignatures": {
            "0x0206/lane2": {
                "count": 1,
                "modeWord": "0x0206",
                "laneValue": 2,
                "sequenceValues": [13],
                "clockSecondsRange": [179.346, 179.346],
                "nearbyDescriptorKinds": {"scrap": 1},
                "nearbyObjectEvents": {"object_setup_marker": 1},
                "nearbyObjectNames": {"npscr10_scrap": 1},
                "nearbyPayloadFamilies": {"neutral_scrap_payload": 1},
                "nearbyScrapControlHalf0": {"0xdc00": 1},
                "nearbyScrapParamHalf1": {"0x0020": 1},
                "nearbyMutableScrapNames": {},
                "nearbyMutableScrapControlHalf0": {},
                "mutableScrapAssessment": "no mutable scrap overlap observed",
                "sequenceNextDescriptorNames": {},
                "sequenceNextScrapControlHalf0": {},
                "sequenceNextScrapParamHalf1": {},
                "sequenceNextScrapOrdinals": {},
                "sequenceNextScrapSubtypeWords": {},
                "sequencePatternAssessment": "no next-scrap sequence pattern observed",
                "subtypeToScrapControlHalf0": {},
                "subtypeToScrapParamHalf1": {},
                "sequenceHalfwordAssessment": "no subtype-linked halfword pattern observed",
            },
            "0x0306/lane0": {
                "count": 1,
                "modeWord": "0x0306",
                "laneValue": 0,
                "sequenceValues": [70],
                "clockSecondsRange": [53.379, 53.379],
                "nearbyDescriptorKinds": {"scrap": 1},
                "nearbyObjectEvents": {"object_setup_marker": 1},
                "nearbyObjectNames": {"npscr10_scrap": 1},
                "nearbyPayloadFamilies": {"neutral_scrap_payload": 1},
                "nearbyScrapControlHalf0": {"0xdc00": 1},
                "nearbyScrapParamHalf1": {"0x0020": 1},
                "nearbyMutableScrapNames": {},
                "nearbyMutableScrapControlHalf0": {},
                "mutableScrapAssessment": "no mutable scrap overlap observed",
                "sequenceNextDescriptorNames": {"70": {"npscr10_scrap": 1}},
                "sequenceNextScrapControlHalf0": {"70": {"0xdc00": 1}},
                "sequenceNextScrapParamHalf1": {"70": {"0x0020": 1}},
                "sequenceNextScrapOrdinals": {"70": [10]},
                "sequenceNextScrapSubtypeWords": {"70": {"0x0400": 1}},
                "sequencePatternAssessment": "next-scrap sequence pattern is partially decoded",
                "subtypeToScrapControlHalf0": {"0x0400": {"0xdc00": 1}},
                "subtypeToScrapParamHalf1": {"0x0400": {"0x0020": 1}},
                "sequenceHalfwordAssessment": "subtypeWord currently maps one-to-one onto both controlHalf0 and paramHalf1",
            },
        },
        "poAssessment": "PO mostly runs in a 0x0306/lane0 cadence, with a smaller 0x0206/lane2 burst in the March 21 captures; the late 0x0206/lane2 burst is only seen near scrap descriptors in the current windowed context; its local scrap neighborhood is led by npscr10_scrap",
        "objectControlSignatures": {
            "01001000000000000403000000": {
                "count": 1,
                "controlBodyBytes": 15,
                "controlModeWord": "0x0304",
                "controlTailWord": "0x00000304",
                "nearbyDescriptorKinds": {"scrap": 1},
                "nearbyObjectEvents": {"object_setup_marker": 1},
                "nearbyObjectNames": {"npscr10_scrap": 1},
                "nearbyPayloadFamilies": {"neutral_scrap_payload": 1},
                "nearbyScrapControlHalf0": {"0xdc00": 1},
                "nearbyScrapParamHalf1": {"0x0020": 1},
                "nearbyMutableScrapNames": {},
                "nearbyMutableScrapControlHalf0": {},
            },
            "0100af210000000006060507000200": {
                "count": 1,
                "controlBodyBytes": 17,
                "controlModeWord": "0x0606",
                "controlTailWord": "0x07050606",
                "nearbyDescriptorKinds": {"scrap": 1},
                "nearbyObjectEvents": {"object_setup_marker": 1},
                "nearbyObjectNames": {"npscr10_scrap": 1},
                "nearbyPayloadFamilies": {"neutral_scrap_payload": 1},
                "nearbyScrapControlHalf0": {"0xdc00": 1},
                "nearbyScrapParamHalf1": {"0x0020": 1},
                "nearbyMutableScrapNames": {},
                "nearbyMutableScrapControlHalf0": {},
            },
        },
        "objectControlAssessment": "peer_object_control currently splits across a small set of repeated short-packet signatures; the 0x0606 signatures appear adjacent to object_setup_marker packets",
    }
    if unresolved_control_summary.get("unresolvedControlComparisons") != expected_unresolved_control_comparisons:
        return False, f"unresolved control comparison mismatch: {unresolved_control_summary}"

    scav_payload = (
        "40000000019d119c09e60000006c000000847a750200fa2000000000272708030002000000bd220080ec792162294541feb2627673636176325f73636176656e67657200"
    )
    scav_decoded = decode_bzr_udp_datagram(bytes.fromhex(scav_payload))
    if scav_decoded.get("objectKind") != "scavenger":
        return False, f"scavenger kind mismatch: {scav_decoded}"
    if scav_decoded.get("descriptorFamilyHint") != "b_side_scavenger_descriptor":
        return False, f"scavenger family hint mismatch: {scav_decoded}"
    if scav_decoded.get("descriptorPayloadFamilyHint") != "b_side_mobile_unit_payload":
        return False, f"scavenger payload family hint mismatch: {scav_decoded}"
    wingman_payload = (
        "40000000019d119c0bb60000007f000000687a750200f92000000000242408040002000000bd2200404379619529733bfeaf627672617a325f77696e676d616e00"
    )
    wingman_decoded = decode_bzr_udp_datagram(bytes.fromhex(wingman_payload))
    if wingman_decoded.get("objectKind") != "wingman":
        return False, f"wingman kind mismatch: {wingman_decoded}"
    if wingman_decoded.get("descriptorFamilyHint") != "b_side_wingman_descriptor":
        return False, f"wingman family hint mismatch: {wingman_decoded}"
    if wingman_decoded.get("descriptorPayloadFamilyHint") != "b_side_mobile_unit_payload":
        return False, f"wingman payload family hint mismatch: {wingman_decoded}"

    history = [
        {"channel": "udp", "peer": "192.168.0.63:50745", "time": "t0", "decoder": object_decoded},
        {"channel": "udp", "peer": "192.168.0.63:50745", "time": "t1", "decoder": scrap_decoded},
        {"channel": "udp", "peer": "192.168.0.63:50745", "time": "t2", "decoder": pilot_decoded},
        {"channel": "udp", "peer": "192.168.0.63:50745", "time": "t3", "decoder": scav_decoded},
        {"channel": "udp", "peer": "192.168.0.63:50745", "time": "t4", "decoder": wingman_decoded},
    ]
    summary = summarize_protocol_history(history)
    peer_summary = summary["peerSessions"][0]
    if peer_summary.get("objectKindCounts") != {"turrettank": 1, "scrap": 1, "person": 1, "scavenger": 1, "wingman": 1}:
        return False, f"object kind count summary mismatch: {peer_summary}"
    if peer_summary.get("descriptorPatterns", {}).get("0x2f2f/0x0184/0x0100") != 1:
        return False, f"descriptor pattern summary mismatch: {peer_summary}"
    if peer_summary.get("descriptorFamilyHints") != {
        "c_side_turrettank_descriptor": 1,
        "neutral_scrap_descriptor": 1,
        "b_side_person_pilot_descriptor": 1,
        "b_side_scavenger_descriptor": 1,
        "b_side_wingman_descriptor": 1,
    }:
        return False, f"descriptor family hint summary mismatch: {peer_summary}"
    if peer_summary.get("descriptorArchetypeHints") != {
        "turrettank": 1,
        "scrap": 1,
        "person_pilot": 1,
        "scavenger": 1,
        "wingman": 1,
    }:
        return False, f"descriptor archetype hint summary mismatch: {peer_summary}"
    if peer_summary.get("descriptorPayloadFamilyHints") != {
        "c_side_structure_payload": 1,
        "neutral_scrap_payload": 1,
        "b_side_person_payload": 1,
        "b_side_mobile_unit_payload": 2,
    }:
        return False, f"descriptor payload family hint summary mismatch: {peer_summary}"
    if peer_summary.get("descriptorControlSignatures") != {
        "c_side_structure_payload:0x00000000/0xf7000000": 1,
        "neutral_scrap_payload:0xdc000000/0x35000020": 1,
        "b_side_person_payload:0xd5ffa100/0xa84037a8": 1,
        "b_side_mobile_unit_payload:0xbd000000/0xec800022": 1,
        "b_side_mobile_unit_payload:0xbd000000/0x43400022": 1,
    }:
        return False, f"descriptor control signature summary mismatch: {peer_summary}"
    if peer_summary.get("descriptorIdentityWords") != {
        "classWord": {"0x2f2f": 1, "0x2e2e": 1, "0x2b2b": 1, "0x2727": 1, "0x2424": 1},
        "subtypeWord": {"0x0184": 1, "0x0400": 1, "0x0708": 1, "0x0308": 1, "0x0408": 1},
        "ownerWord": {"0x0100": 2, "0x0200": 3},
    }:
        return False, f"descriptor identity word summary mismatch: {peer_summary}"
    latest_descriptor = peer_summary.get("latestObjectDescriptor")
    if not isinstance(latest_descriptor, dict):
        return False, f"latest object descriptor missing: {peer_summary}"
    expected_wingman_payload_hints = {
        "controlLaneWords": {
            "controlWord": "0xbd000000",
            "paramWord": "0x43400022",
        },
        "vectorLaneWords": {
            "vectorWordA": "0x29956179",
            "vectorWordB": "0xaffe3b73",
        },
        "payloadLaneAssessment": "controlWord/paramWord look more family-side dependent; vectorWordA/vectorWordB vary more like per-descriptor payload",
        "controlSignature": "b_side_mobile_unit_payload:0xbd000000/0x43400022",
    }
    if latest_descriptor.get("descriptorPayloadHints") != expected_wingman_payload_hints:
        return False, f"latest object descriptor payload hints mismatch: {peer_summary}"
    descriptor_payload_comparisons = summary.get("descriptorPayloadComparisons")
    if not isinstance(descriptor_payload_comparisons, list):
        return False, f"descriptor payload comparison summary missing: {summary}"
    mobile_unit_summary = next(
        (item for item in descriptor_payload_comparisons if item.get("payloadFamily") == "b_side_mobile_unit_payload"),
        None,
    )
    if not isinstance(mobile_unit_summary, dict):
        return False, f"mobile unit payload comparison missing: {descriptor_payload_comparisons}"
    if mobile_unit_summary.get("descriptorFamilies") != {
        "b_side_scavenger_descriptor": 1,
        "b_side_wingman_descriptor": 1,
    }:
        return False, f"mobile unit descriptor family comparison mismatch: {mobile_unit_summary}"
    if mobile_unit_summary.get("stablePayloadFields") != ["controlWord"]:
        return False, f"mobile unit stable payload fields mismatch: {mobile_unit_summary}"
    if mobile_unit_summary.get("varyingPayloadFields") != ["paramWord", "vectorWordA", "vectorWordB"]:
        return False, f"mobile unit varying payload fields mismatch: {mobile_unit_summary}"
    if mobile_unit_summary.get("commonPayloadValues") != {"controlWord": "0xbd000000"}:
        return False, f"mobile unit common payload values mismatch: {mobile_unit_summary}"
    if mobile_unit_summary.get("likelyControlFields") != ["controlWord"]:
        return False, f"mobile unit likely control fields mismatch: {mobile_unit_summary}"
    if mobile_unit_summary.get("likelyVectorFields") != ["vectorWordA", "vectorWordB"]:
        return False, f"mobile unit likely vector fields mismatch: {mobile_unit_summary}"
    if mobile_unit_summary.get("payloadAssessment") != "controlWord looks family-stable while later payload words vary across descriptors":
        return False, f"mobile unit payload assessment mismatch: {mobile_unit_summary}"
    mobile_control_byte_profile = mobile_unit_summary.get("payloadWordByteProfiles", {}).get("controlWord")
    expected_mobile_control_byte_profile = {
        "lanes": {
            "hexByte0": {"0xbd": 2},
            "hexByte1": {"0x00": 2},
            "hexByte2": {"0x00": 2},
            "hexByte3": {"0x00": 2},
        },
        "stableLanes": ["hexByte0", "hexByte1", "hexByte2", "hexByte3"],
        "varyingLanes": [],
        "alwaysZeroLanes": ["hexByte1", "hexByte2", "hexByte3"],
        "commonLaneValues": {
            "hexByte0": "0xbd",
            "hexByte1": "0x00",
            "hexByte2": "0x00",
            "hexByte3": "0x00",
        },
    }
    if mobile_control_byte_profile != expected_mobile_control_byte_profile:
        return False, f"mobile unit control byte profile mismatch: {mobile_unit_summary}"
    mobile_control_half_profile = mobile_unit_summary.get("payloadWordHalfProfiles", {}).get("controlWord")
    expected_mobile_control_half_profile = {
        "lanes": {
            "hexHalf0": {"0xbd00": 2},
            "hexHalf1": {"0x0000": 2},
        },
        "stableLanes": ["hexHalf0", "hexHalf1"],
        "varyingLanes": [],
        "alwaysZeroLanes": ["hexHalf1"],
        "commonLaneValues": {
            "hexHalf0": "0xbd00",
            "hexHalf1": "0x0000",
        },
    }
    if mobile_control_half_profile != expected_mobile_control_half_profile:
        return False, f"mobile unit control half profile mismatch: {mobile_unit_summary}"
    expected_mobile_control_half0_clusters = {
        "0xbd00": {
            "count": 2,
            "objectNames": ["bvraz2_wingman", "bvscav2_scavenger"],
            "ordinals": [2],
            "pairedHalf0": {"0x4340": 1, "0xec80": 1},
            "pairedHalf1": {"0x0022": 2},
        }
    }
    if mobile_unit_summary.get("controlWordHalf0Clusters") != expected_mobile_control_half0_clusters:
        return False, f"mobile unit control half0 clusters mismatch: {mobile_unit_summary}"
    expected_mobile_control_half0_cluster_hints = {
        "0xbd00": {
            "scopeHint": "family_shared",
            "laneHint": "paired_half0_varies",
            "objectNameCount": 2,
            "ordinalCount": 1,
            "pairedHalf0Count": 2,
            "pairedHalf1Count": 1,
        }
    }
    if mobile_unit_summary.get("controlWordHalf0ClusterHints") != expected_mobile_control_half0_cluster_hints:
        return False, f"mobile unit control half0 cluster hints mismatch: {mobile_unit_summary}"
    expected_mobile_param_half1_clusters = {
        "0x0022": {
            "count": 2,
            "objectNames": ["bvraz2_wingman", "bvscav2_scavenger"],
            "ordinals": [2],
            "pairedHalf0": {"0xbd00": 2},
            "pairedHalf1": {"0x4340": 1, "0xec80": 1},
        }
    }
    if mobile_unit_summary.get("paramWordHalf1Clusters") != expected_mobile_param_half1_clusters:
        return False, f"mobile unit param half1 clusters mismatch: {mobile_unit_summary}"
    expected_mobile_param_half1_cluster_hints = {
        "0x0022": {
            "scopeHint": "family_shared",
            "laneHint": "paired_half1_varies",
            "objectNameCount": 2,
            "ordinalCount": 1,
            "pairedHalf0Count": 1,
            "pairedHalf1Count": 2,
        }
    }
    if mobile_unit_summary.get("paramWordHalf1ClusterHints") != expected_mobile_param_half1_cluster_hints:
        return False, f"mobile unit param half1 cluster hints mismatch: {mobile_unit_summary}"
    sample_descriptors = mobile_unit_summary.get("sampleDescriptors")
    if not isinstance(sample_descriptors, list) or len(sample_descriptors) != 2:
        return False, f"mobile unit sample descriptors mismatch: {mobile_unit_summary}"
    if sample_descriptors[0].get("name") != "bvscav2_scavenger" or sample_descriptors[1].get("name") != "bvraz2_wingman":
        return False, f"mobile unit sample descriptor names mismatch: {mobile_unit_summary}"

    repeated_mobile_history = [
        {"channel": "udp", "peer": "192.168.0.63:50745", "time": "r0", "decoder": scav_decoded},
        {"channel": "udp", "peer": "192.168.0.63:50745", "time": "r1", "decoder": wingman_decoded},
        {"channel": "udp", "peer": "192.168.0.63:50745", "time": "r2", "decoder": scav_decoded},
        {"channel": "udp", "peer": "192.168.0.63:50745", "time": "r3", "decoder": wingman_decoded},
    ]
    repeated_mobile_summary = summarize_protocol_history(repeated_mobile_history)
    repeated_mobile_comparisons = repeated_mobile_summary.get("descriptorPayloadComparisons")
    if not isinstance(repeated_mobile_comparisons, list):
        return False, f"repeated mobile comparison summary missing: {repeated_mobile_summary}"
    repeated_mobile_family = next(
        (item for item in repeated_mobile_comparisons if item.get("payloadFamily") == "b_side_mobile_unit_payload"),
        None,
    )
    if not isinstance(repeated_mobile_family, dict):
        return False, f"repeated mobile family summary missing: {repeated_mobile_comparisons}"
    if repeated_mobile_family.get("objectStablePayloadFields") != ["controlWord", "paramWord", "vectorWordA", "vectorWordB"]:
        return False, f"repeated mobile object-stable fields mismatch: {repeated_mobile_family}"
    if repeated_mobile_family.get("objectVaryingAcrossNamesFields") != ["paramWord", "vectorWordA", "vectorWordB"]:
        return False, f"repeated mobile object-varying fields mismatch: {repeated_mobile_family}"
    if repeated_mobile_family.get("objectSpecificPayloadFields") != ["paramWord", "vectorWordA", "vectorWordB"]:
        return False, f"repeated mobile object-specific fields mismatch: {repeated_mobile_family}"
    if repeated_mobile_family.get("mutableWithinObjectNameFields") != []:
        return False, f"repeated mobile mutable fields mismatch: {repeated_mobile_family}"
    if repeated_mobile_family.get("objectStabilityAssessment") != "payload words are stable within repeated object names and vary across different named objects":
        return False, f"repeated mobile object stability assessment mismatch: {repeated_mobile_family}"
    sample_object_profiles = repeated_mobile_family.get("sampleObjectProfiles")
    if not isinstance(sample_object_profiles, list) or len(sample_object_profiles) != 2:
        return False, f"repeated mobile sample object profiles mismatch: {repeated_mobile_family}"
    if sample_object_profiles[0].get("name") != "bvraz2_wingman" or sample_object_profiles[1].get("name") != "bvscav2_scavenger":
        return False, f"repeated mobile sample object profile names mismatch: {repeated_mobile_family}"

    scrap_mutation_payload_a = (
        "c0000000019d119c16d000000070000000847a750200a621000000002f2f000e0002000000aa08000033c41f0028087c106e707363723100000000803f6e707363723131315f736372617000"
    )
    scrap_mutation_payload_b = (
        "c0000000019d119c3d950000008d000000737a750100a621000000002f2f000e0002000080a908000033c41f0028087c106e707363723100000000803f6e707363723131315f736372617000"
    )
    scrap_mutation_a = decode_bzr_udp_datagram(bytes.fromhex(scrap_mutation_payload_a))
    scrap_mutation_b = decode_bzr_udp_datagram(bytes.fromhex(scrap_mutation_payload_b))
    if scrap_mutation_a.get("primaryObjectName") != "npscr111_scrap":
        return False, f"scrap mutation sample A name mismatch: {scrap_mutation_a}"
    if scrap_mutation_b.get("primaryObjectName") != "npscr111_scrap":
        return False, f"scrap mutation sample B name mismatch: {scrap_mutation_b}"
    unresolved_control_mutable_history = [
        {"channel": "udp", "peer": "192.168.0.63:50745", "time": "um0", "decoder": po_special_decoded},
        {"channel": "udp", "peer": "192.168.0.63:50745", "time": "um1", "decoder": scrap_mutation_a},
        {"channel": "udp", "peer": "192.168.0.63:50745", "time": "um2", "decoder": scrap_mutation_b},
    ]
    unresolved_control_mutable_summary = summarize_protocol_history(unresolved_control_mutable_history)
    expected_mutable_po_signature = {
        "count": 1,
        "modeWord": "0x0206",
        "laneValue": 2,
        "sequenceValues": [13],
        "clockSecondsRange": [179.346, 179.346],
        "nearbyDescriptorKinds": {"scrap": 2},
        "nearbyObjectEvents": {},
        "nearbyObjectNames": {"npscr111_scrap": 2},
        "nearbyPayloadFamilies": {"neutral_scrap_payload": 2},
        "nearbyScrapControlHalf0": {"0xa980": 1, "0xaa00": 1},
        "nearbyScrapParamHalf1": {"0x0008": 2},
        "nearbyMutableScrapNames": {"npscr111_scrap": 2},
        "nearbyMutableScrapControlHalf0": {"0xa980": 1, "0xaa00": 1},
        "mutableScrapAssessment": "overlaps mutable scrap via npscr111_scrap",
        "sequenceNextDescriptorNames": {"13": {"npscr111_scrap": 1}},
        "sequenceNextScrapControlHalf0": {"13": {"0xaa00": 1}},
        "sequenceNextScrapParamHalf1": {"13": {"0x0008": 1}},
        "sequenceNextScrapOrdinals": {"13": [111]},
        "sequenceNextScrapSubtypeWords": {"13": {"0x0e00": 1}},
        "sequencePatternAssessment": "next-scrap sequence pattern is partially decoded",
        "subtypeToScrapControlHalf0": {"0x0e00": {"0xaa00": 1}},
        "subtypeToScrapParamHalf1": {"0x0e00": {"0x0008": 1}},
        "sequenceHalfwordAssessment": "subtypeWord currently maps one-to-one onto both controlHalf0 and paramHalf1",
    }
    mutable_po_signature = unresolved_control_mutable_summary.get("unresolvedControlComparisons", {}).get("poControlSignatures", {}).get("0x0206/lane2")
    if mutable_po_signature != expected_mutable_po_signature:
        return False, f"mutable PO signature mismatch: {unresolved_control_mutable_summary}"
    expected_mutable_po_assessment = (
        "PO exposes a stable float-like clock plus small mode/lane words, but only one observed signature family"
    )
    if unresolved_control_mutable_summary.get("unresolvedControlComparisons", {}).get("poAssessment") != expected_mutable_po_assessment:
        return False, f"mutable PO assessment mismatch: {unresolved_control_mutable_summary}"

    late_burst_sequence_history = []
    for sequence_value, object_name, ordinal, subtype_word, control_word, param_word in [
        (11, "npscr39_scrap", 39, "0x0b00", "0x6d800000", "0x0c400020"),
        (12, "npscr29_scrap", 29, "0x0c00", "0x35800000", "0x6b40000b"),
        (13, "npscr110_scrap", 110, "0x0d00", "0x61000000", "0x64a00022"),
        (14, "npscr111_scrap", 111, "0x0e00", "0xa9800000", "0x33000008"),
        (15, "npscr112_scrap", 112, "0x0f00", "0x0a800000", "0x47e00020"),
    ]:
        po_decoder = {
            **po_special_decoded,
            "timingFields": {
                "clockSeconds": 179.346,
                "modeWord": "0x0206",
                "sequenceValue": sequence_value,
                "laneValue": 2,
            },
        }
        descriptor_decoder = {
            "family": "peer_object_descriptor",
            "objectKind": "scrap",
            "primaryObjectName": object_name,
            "objectOrdinal": ordinal,
            "descriptorPayloadFamilyHint": "neutral_scrap_payload",
            "descriptorSketch": {
                "subtypeWord": subtype_word,
                "controlWord": control_word,
                "paramWord": param_word,
            },
        }
        late_burst_sequence_history.append(
            {"channel": "udp", "peer": "192.168.0.63:50745", "time": f"lb{sequence_value}a", "decoder": po_decoder}
        )
        late_burst_sequence_history.append(
            {"channel": "udp", "peer": "192.168.0.63:50745", "time": f"lb{sequence_value}b", "decoder": descriptor_decoder}
        )
    late_burst_sequence_summary = summarize_protocol_history(late_burst_sequence_history)
    late_burst_po_signature = late_burst_sequence_summary.get("unresolvedControlComparisons", {}).get("poControlSignatures", {}).get("0x0206/lane2")
    if not isinstance(late_burst_po_signature, dict):
        return False, f"late burst PO signature missing: {late_burst_sequence_summary}"
    if late_burst_po_signature.get("sequenceNextScrapOrdinals") != {
        "11": [39],
        "12": [29],
        "13": [110],
        "14": [111],
        "15": [112],
    }:
        return False, f"late burst PO ordinal sequence mismatch: {late_burst_po_signature}"
    if late_burst_po_signature.get("sequenceNextScrapSubtypeWords") != {
        "11": {"0x0b00": 1},
        "12": {"0x0c00": 1},
        "13": {"0x0d00": 1},
        "14": {"0x0e00": 1},
        "15": {"0x0f00": 1},
    }:
        return False, f"late burst PO subtype sequence mismatch: {late_burst_po_signature}"
    if late_burst_po_signature.get("sequencePatternAssessment") != "next-scrap subtypeWord steps contiguously while objectOrdinal does not":
        return False, f"late burst PO sequence pattern mismatch: {late_burst_po_signature}"
    if late_burst_po_signature.get("subtypeToScrapControlHalf0") != {
        "0x0b00": {"0x6d80": 1},
        "0x0c00": {"0x3580": 1},
        "0x0d00": {"0x6100": 1},
        "0x0e00": {"0xa980": 1},
        "0x0f00": {"0x0a80": 1},
    }:
        return False, f"late burst PO subtype-to-control mismatch: {late_burst_po_signature}"
    if late_burst_po_signature.get("subtypeToScrapParamHalf1") != {
        "0x0b00": {"0x0020": 1},
        "0x0c00": {"0x000b": 1},
        "0x0d00": {"0x0022": 1},
        "0x0e00": {"0x0008": 1},
        "0x0f00": {"0x0020": 1},
    }:
        return False, f"late burst PO subtype-to-param mismatch: {late_burst_po_signature}"
    if late_burst_po_signature.get("sequenceHalfwordAssessment") != "subtypeWord maps one-to-one onto controlHalf0, while paramHalf1 repeats across subtype steps":
        return False, f"late burst PO halfword assessment mismatch: {late_burst_po_signature}"
    expected_late_burst_po_assessment = (
        "PO exposes a stable float-like clock plus small mode/lane words, but only one observed signature family"
    )
    if late_burst_sequence_summary.get("unresolvedControlComparisons", {}).get("poAssessment") != expected_late_burst_po_assessment:
        return False, f"late burst PO assessment mismatch: {late_burst_sequence_summary}"
    late_burst_sequence_family = next(
        (item for item in late_burst_sequence_summary.get("descriptorPayloadComparisons", []) if item.get("payloadFamily") == "neutral_scrap_payload"),
        None,
    )
    if not isinstance(late_burst_sequence_family, dict):
        return False, f"late burst scrap family summary missing: {late_burst_sequence_summary}"
    if late_burst_sequence_family.get("poSandwichedObjects") != [
        "npscr110_scrap",
        "npscr111_scrap",
        "npscr29_scrap",
        "npscr39_scrap",
    ]:
        return False, f"late burst PO-sandwiched objects mismatch: {late_burst_sequence_family}"
    if late_burst_sequence_family.get("poLeadingEdgeObjects") != ["npscr112_scrap"]:
        return False, f"late burst PO-leading-edge objects mismatch: {late_burst_sequence_family}"
    expected_late_burst_context_assessment = (
        "PO-sandwiched descriptor observations appear for npscr110_scrap, npscr111_scrap, npscr29_scrap, npscr39_scrap; npscr112_scrap also appears on a leading-PO trailing-edge observation"
    )
    if late_burst_sequence_family.get("objectObservationContextAssessment") != expected_late_burst_context_assessment:
        return False, f"late burst object context assessment mismatch: {late_burst_sequence_family}"
    expected_late_burst_po_context_control_comparisons = {
        "npscr110_scrap": {
            "contextSignatures": {"po_sandwiched": 1},
            "subtypeWords": {"0x0d00": 1},
            "controlHalf0": {"0x6100": 1},
            "paramWord": {"0x64a00022": 1},
            "mutableFields": [],
            "poContextRole": "po_sandwiched",
        },
        "npscr111_scrap": {
            "contextSignatures": {"po_sandwiched": 1},
            "subtypeWords": {"0x0e00": 1},
            "controlHalf0": {"0xa980": 1},
            "paramWord": {"0x33000008": 1},
            "mutableFields": [],
            "poContextRole": "po_sandwiched",
        },
        "npscr112_scrap": {
            "contextSignatures": {"po_leading_edge": 1},
            "subtypeWords": {"0x0f00": 1},
            "controlHalf0": {"0x0a80": 1},
            "paramWord": {"0x47e00020": 1},
            "mutableFields": [],
            "poContextRole": "po_leading_edge",
        },
        "npscr29_scrap": {
            "contextSignatures": {"po_sandwiched": 1},
            "subtypeWords": {"0x0c00": 1},
            "controlHalf0": {"0x3580": 1},
            "paramWord": {"0x6b40000b": 1},
            "mutableFields": [],
            "poContextRole": "po_sandwiched",
        },
        "npscr39_scrap": {
            "contextSignatures": {"po_sandwiched": 1},
            "subtypeWords": {"0x0b00": 1},
            "controlHalf0": {"0x6d80": 1},
            "paramWord": {"0x0c400020": 1},
            "mutableFields": [],
            "poContextRole": "po_sandwiched",
        },
    }
    if late_burst_sequence_family.get("poContextControlComparisons") != expected_late_burst_po_context_control_comparisons:
        return False, f"late burst PO-context control comparisons mismatch: {late_burst_sequence_family}"
    expected_late_burst_po_context_assessment = (
        "npscr110_scrap, npscr111_scrap, npscr29_scrap, npscr39_scrap remain single-state across observed controlHalf0 values; npscr112_scrap currently appears only on the PO-leading edge"
    )
    if late_burst_sequence_family.get("poContextControlAssessment") != expected_late_burst_po_context_assessment:
        return False, f"late burst PO-context control assessment mismatch: {late_burst_sequence_family}"
    scrap_mutation_history = [
        {"channel": "udp", "peer": "192.168.0.63:50745", "time": "s0", "decoder": scrap_mutation_a},
        {"channel": "udp", "peer": "192.168.0.63:50745", "time": "s1", "decoder": scrap_mutation_b},
        {"channel": "udp", "peer": "192.168.0.63:50745", "time": "s2", "decoder": scrap_mutation_a},
    ]
    scrap_mutation_summary = summarize_protocol_history(scrap_mutation_history)
    scrap_mutation_comparisons = scrap_mutation_summary.get("descriptorPayloadComparisons")
    if not isinstance(scrap_mutation_comparisons, list):
        return False, f"scrap mutation comparisons missing: {scrap_mutation_summary}"
    scrap_mutation_family = next(
        (item for item in scrap_mutation_comparisons if item.get("payloadFamily") == "neutral_scrap_payload"),
        None,
    )
    if not isinstance(scrap_mutation_family, dict):
        return False, f"scrap mutation family summary missing: {scrap_mutation_comparisons}"
    if scrap_mutation_family.get("mutableWithinObjectNameFields") != ["controlWord"]:
        return False, f"scrap mutation mutable fields mismatch: {scrap_mutation_family}"
    if scrap_mutation_family.get("objectSpecificPayloadFields") != []:
        return False, f"scrap mutation object-specific fields mismatch: {scrap_mutation_family}"
    if scrap_mutation_family.get("objectStabilityAssessment") != "some payload words mutate within repeated observations of the same named object":
        return False, f"scrap mutation object stability assessment mismatch: {scrap_mutation_family}"
    mutable_object_mutations = scrap_mutation_family.get("mutableObjectMutations")
    expected_mutable_object_mutations = [
        {
            "name": "npscr111_scrap",
            "observationCount": 3,
            "mutableFields": ["controlWord"],
            "fieldMutations": {
                "controlWord": {
                    "values": ["0xa9800000", "0xaa000000"],
                    "valueCounts": {"0xaa000000": 2, "0xa9800000": 1},
                    "xorFromFirstValue": ["0x03800000"],
                    "bitPositions": [23, 24, 25],
                    "changedByteLanes": ["hexByte0", "hexByte1"],
                    "changedHalfLanes": ["hexHalf0"],
                }
            },
        }
    ]
    if mutable_object_mutations != expected_mutable_object_mutations:
        return False, f"scrap mutation details mismatch: {scrap_mutation_family}"
    scrap_control_byte_profile = scrap_mutation_family.get("payloadWordByteProfiles", {}).get("controlWord")
    expected_scrap_control_byte_profile = {
        "lanes": {
            "hexByte0": {"0xaa": 2, "0xa9": 1},
            "hexByte1": {"0x00": 2, "0x80": 1},
            "hexByte2": {"0x00": 3},
            "hexByte3": {"0x00": 3},
        },
        "stableLanes": ["hexByte2", "hexByte3"],
        "varyingLanes": ["hexByte0", "hexByte1"],
        "alwaysZeroLanes": ["hexByte2", "hexByte3"],
        "commonLaneValues": {
            "hexByte2": "0x00",
            "hexByte3": "0x00",
        },
    }
    if scrap_control_byte_profile != expected_scrap_control_byte_profile:
        return False, f"scrap control byte profile mismatch: {scrap_mutation_family}"
    scrap_control_half_profile = scrap_mutation_family.get("payloadWordHalfProfiles", {}).get("controlWord")
    expected_scrap_control_half_profile = {
        "lanes": {
            "hexHalf0": {"0xaa00": 2, "0xa980": 1},
            "hexHalf1": {"0x0000": 3},
        },
        "stableLanes": ["hexHalf1"],
        "varyingLanes": ["hexHalf0"],
        "alwaysZeroLanes": ["hexHalf1"],
        "commonLaneValues": {
            "hexHalf1": "0x0000",
        },
    }
    if scrap_control_half_profile != expected_scrap_control_half_profile:
        return False, f"scrap control half profile mismatch: {scrap_mutation_family}"
    expected_scrap_control_half0_clusters = {
        "0xaa00": {
            "count": 2,
            "objectNames": ["npscr111_scrap"],
            "ordinals": [111],
            "pairedHalf0": {"0x3300": 2},
            "pairedHalf1": {"0x0008": 2},
        },
        "0xa980": {
            "count": 1,
            "objectNames": ["npscr111_scrap"],
            "ordinals": [111],
            "pairedHalf0": {"0x3300": 1},
            "pairedHalf1": {"0x0008": 1},
        },
    }
    if scrap_mutation_family.get("controlWordHalf0Clusters") != expected_scrap_control_half0_clusters:
        return False, f"scrap control half0 clusters mismatch: {scrap_mutation_family}"
    expected_scrap_control_half0_cluster_hints = {
        "0xaa00": {
            "scopeHint": "single_object_specific",
            "laneHint": "paired_halves_stable",
            "objectNameCount": 1,
            "ordinalCount": 1,
            "pairedHalf0Count": 1,
            "pairedHalf1Count": 1,
        },
        "0xa980": {
            "scopeHint": "single_object_specific",
            "laneHint": "paired_halves_stable",
            "objectNameCount": 1,
            "ordinalCount": 1,
            "pairedHalf0Count": 1,
            "pairedHalf1Count": 1,
        },
    }
    if scrap_mutation_family.get("controlWordHalf0ClusterHints") != expected_scrap_control_half0_cluster_hints:
        return False, f"scrap control half0 cluster hints mismatch: {scrap_mutation_family}"
    expected_scrap_param_half1_clusters = {
        "0x0008": {
            "count": 3,
            "objectNames": ["npscr111_scrap"],
            "ordinals": [111],
            "pairedHalf0": {"0xa980": 1, "0xaa00": 2},
            "pairedHalf1": {"0x3300": 3},
        }
    }
    if scrap_mutation_family.get("paramWordHalf1Clusters") != expected_scrap_param_half1_clusters:
        return False, f"scrap param half1 clusters mismatch: {scrap_mutation_family}"
    expected_scrap_param_half1_cluster_hints = {
        "0x0008": {
            "scopeHint": "single_object_specific",
            "laneHint": "paired_half0_varies",
            "objectNameCount": 1,
            "ordinalCount": 1,
            "pairedHalf0Count": 2,
            "pairedHalf1Count": 1,
        }
    }
    if scrap_mutation_family.get("paramWordHalf1ClusterHints") != expected_scrap_param_half1_cluster_hints:
        return False, f"scrap param half1 cluster hints mismatch: {scrap_mutation_family}"

    scrap_category_payloads = [
        "c0000000019d1199caaa00000010000000097a750100fe20000000002e2e00040001000000dc200000350200002838420a6e707363723100000000803f6e7073637231305f736372617000",
        "c0000000019d1199caaa00000011000000097a750100fd20000000002e2e000500010000007c340060500400002848470a6e707363723300000000803f6e7073637233305f736372617000",
        "c0000000019d1199caaa00000012000000097a750100fc20000000002e2e0006000100008035120060c60100002838490a6e707363723300000000803f6e7073637233315f736372617000",
        "c0000000019d119be0a100000069000000657a7501006121000000002e2e000d0001000000cd3400c05eb81f0028901c106e707363723200000000803f6e7073637232345f736372617000",
        "c0000000019d119c16ce0000006d000000847a750200a721000000002e2e000b00020000806d2000400cc51f0028e05e106e707363723300000000803f6e7073637233395f736372617000",
        "c0000000019d119c16d000000071000000847a7502006521000000002f2f000f00020000800a2000e047c51f0028c086106e707363723100000000803f6e707363723131325f736372617000",
        "c0000000019d119c16d00000006e000000847a750200a421000000002e2e000c0002000080350b00406bc11f0028b0aa106e707363723200000000803f6e7073637232395f736372617000",
    ]
    scrap_category_history = [
        {
            "channel": "udp",
            "peer": "192.168.0.63:50745",
            "time": f"c{idx}",
            "decoder": decode_bzr_udp_datagram(bytes.fromhex(payload)),
        }
        for idx, payload in enumerate(scrap_category_payloads)
    ]
    scrap_category_summary = summarize_protocol_history(scrap_category_history)
    scrap_category_comparisons = scrap_category_summary.get("descriptorPayloadComparisons")
    if not isinstance(scrap_category_comparisons, list):
        return False, f"scrap category comparisons missing: {scrap_category_summary}"
    scrap_category_family = next(
        (item for item in scrap_category_comparisons if item.get("payloadFamily") == "neutral_scrap_payload"),
        None,
    )
    if not isinstance(scrap_category_family, dict):
        return False, f"scrap category family summary missing: {scrap_category_comparisons}"
    expected_scrap_param_half1_vector_clusters = {
        "0x0020": {
            "count": 3,
            "objectNames": ["npscr10_scrap", "npscr112_scrap", "npscr39_scrap"],
            "ordinals": [10, 39, 112],
            "pairedVectorAHalf1": {"0x1fc5": 2, "0x0002": 1},
            "pairedVectorBHalf0": {"0x6e10": 2, "0x6e0a": 1},
        },
        "0x0034": {
            "count": 2,
            "objectNames": ["npscr24_scrap", "npscr30_scrap"],
            "ordinals": [24, 30],
            "pairedVectorAHalf1": {"0x0004": 1, "0x1fb8": 1},
            "pairedVectorBHalf0": {"0x6e0a": 1, "0x6e10": 1},
        },
        "0x000b": {
            "count": 1,
            "objectNames": ["npscr29_scrap"],
            "ordinals": [29],
            "pairedVectorAHalf1": {"0x1fc1": 1},
            "pairedVectorBHalf0": {"0x6e10": 1},
        },
        "0x0012": {
            "count": 1,
            "objectNames": ["npscr31_scrap"],
            "ordinals": [31],
            "pairedVectorAHalf1": {"0x0001": 1},
            "pairedVectorBHalf0": {"0x6e0a": 1},
        },
    }
    if scrap_category_family.get("paramWordHalf1VectorClusters") != expected_scrap_param_half1_vector_clusters:
        return False, f"scrap param half1 vector clusters mismatch: {scrap_category_family}"
    expected_scrap_param_half1_vector_cluster_hints = {
        "0x0020": {
            "scopeHint": "multi_object_category",
            "vectorLaneHint": "vector_pair_both_vary",
            "objectNameCount": 3,
            "ordinalCount": 3,
            "pairedVectorAHalf1Count": 2,
            "pairedVectorBHalf0Count": 2,
        },
        "0x0034": {
            "scopeHint": "multi_object_category",
            "vectorLaneHint": "vector_pair_both_vary",
            "objectNameCount": 2,
            "ordinalCount": 2,
            "pairedVectorAHalf1Count": 2,
            "pairedVectorBHalf0Count": 2,
        },
        "0x000b": {
            "scopeHint": "single_object_specific",
            "vectorLaneHint": "vector_pair_stable",
            "objectNameCount": 1,
            "ordinalCount": 1,
            "pairedVectorAHalf1Count": 1,
            "pairedVectorBHalf0Count": 1,
        },
        "0x0012": {
            "scopeHint": "single_object_specific",
            "vectorLaneHint": "vector_pair_stable",
            "objectNameCount": 1,
            "ordinalCount": 1,
            "pairedVectorAHalf1Count": 1,
            "pairedVectorBHalf0Count": 1,
        },
    }
    if scrap_category_family.get("paramWordHalf1VectorClusterHints") != expected_scrap_param_half1_vector_cluster_hints:
        return False, f"scrap param half1 vector cluster hints mismatch: {scrap_category_family}"
    expected_scrap_control_half0_vector_clusters = {
        "0xdc00": {
            "count": 1,
            "objectNames": ["npscr10_scrap"],
            "ordinals": [10],
            "pairedVectorAHalf1": {"0x0002": 1},
            "pairedVectorBHalf0": {"0x6e0a": 1},
        },
        "0xcd00": {
            "count": 1,
            "objectNames": ["npscr24_scrap"],
            "ordinals": [24],
            "pairedVectorAHalf1": {"0x1fb8": 1},
            "pairedVectorBHalf0": {"0x6e10": 1},
        },
        "0x7c00": {
            "count": 1,
            "objectNames": ["npscr30_scrap"],
            "ordinals": [30],
            "pairedVectorAHalf1": {"0x0004": 1},
            "pairedVectorBHalf0": {"0x6e0a": 1},
        },
        "0x6d80": {
            "count": 1,
            "objectNames": ["npscr39_scrap"],
            "ordinals": [39],
            "pairedVectorAHalf1": {"0x1fc5": 1},
            "pairedVectorBHalf0": {"0x6e10": 1},
        },
        "0x3580": {
            "count": 2,
            "objectNames": ["npscr29_scrap", "npscr31_scrap"],
            "ordinals": [29, 31],
            "pairedVectorAHalf1": {"0x0001": 1, "0x1fc1": 1},
            "pairedVectorBHalf0": {"0x6e0a": 1, "0x6e10": 1},
        },
        "0x0a80": {
            "count": 1,
            "objectNames": ["npscr112_scrap"],
            "ordinals": [112],
            "pairedVectorAHalf1": {"0x1fc5": 1},
            "pairedVectorBHalf0": {"0x6e10": 1},
        },
    }
    if scrap_category_family.get("controlWordHalf0VectorClusters") != expected_scrap_control_half0_vector_clusters:
        return False, f"scrap control half0 vector clusters mismatch: {scrap_category_family}"
    expected_scrap_control_half0_vector_cluster_hints = {
        "0xdc00": {
            "scopeHint": "single_object_specific",
            "vectorLaneHint": "vector_pair_stable",
            "objectNameCount": 1,
            "ordinalCount": 1,
            "pairedVectorAHalf1Count": 1,
            "pairedVectorBHalf0Count": 1,
        },
        "0xcd00": {
            "scopeHint": "single_object_specific",
            "vectorLaneHint": "vector_pair_stable",
            "objectNameCount": 1,
            "ordinalCount": 1,
            "pairedVectorAHalf1Count": 1,
            "pairedVectorBHalf0Count": 1,
        },
        "0x7c00": {
            "scopeHint": "single_object_specific",
            "vectorLaneHint": "vector_pair_stable",
            "objectNameCount": 1,
            "ordinalCount": 1,
            "pairedVectorAHalf1Count": 1,
            "pairedVectorBHalf0Count": 1,
        },
        "0x6d80": {
            "scopeHint": "single_object_specific",
            "vectorLaneHint": "vector_pair_stable",
            "objectNameCount": 1,
            "ordinalCount": 1,
            "pairedVectorAHalf1Count": 1,
            "pairedVectorBHalf0Count": 1,
        },
        "0x3580": {
            "scopeHint": "multi_object_category",
            "vectorLaneHint": "vector_pair_both_vary",
            "objectNameCount": 2,
            "ordinalCount": 2,
            "pairedVectorAHalf1Count": 2,
            "pairedVectorBHalf0Count": 2,
        },
        "0x0a80": {
            "scopeHint": "single_object_specific",
            "vectorLaneHint": "vector_pair_stable",
            "objectNameCount": 1,
            "ordinalCount": 1,
            "pairedVectorAHalf1Count": 1,
            "pairedVectorBHalf0Count": 1,
        },
    }
    if scrap_category_family.get("controlWordHalf0VectorClusterHints") != expected_scrap_control_half0_vector_cluster_hints:
        return False, f"scrap control half0 vector cluster hints mismatch: {scrap_category_family}"
    if scrap_category_family.get("paramWordHalf1VectorAssessment") != "multi-object paramWordHalf1 clusters span multiple vector signatures; not a simple vector bucket":
        return False, f"scrap param half1 vector assessment mismatch: {scrap_category_family}"
    if scrap_category_family.get("controlWordHalf0VectorAssessment") != "multi-object controlWordHalf0 clusters span multiple vector signatures; not a simple vector bucket":
        return False, f"scrap control half0 vector assessment mismatch: {scrap_category_family}"

    scrap_subtype_family_history = [
        {
            "channel": "udp",
            "peer": "192.168.0.63:50745",
            "time": "st0",
            "decoder": {
                "family": "peer_object_descriptor",
                "descriptorPayloadFamilyHint": "neutral_scrap_payload",
                "descriptorFamilyHint": "neutral_scrap_descriptor",
                "descriptorArchetypeHint": "scrap",
                "objectKind": "scrap",
                "primaryObjectName": "npscr29_scrap",
                "objectOrdinal": 29,
                "descriptorSketch": {
                    "subtypeWord": "0x0c00",
                    "controlWord": "0x35800000",
                    "paramWord": "0x6b40000b",
                    "vectorWordA": "0x00000000",
                    "vectorWordB": "0x00000000",
                },
            },
        },
        {
            "channel": "udp",
            "peer": "192.168.0.63:50745",
            "time": "st1",
            "decoder": {
                "family": "peer_object_descriptor",
                "descriptorPayloadFamilyHint": "neutral_scrap_payload",
                "descriptorFamilyHint": "neutral_scrap_descriptor",
                "descriptorArchetypeHint": "scrap",
                "objectKind": "scrap",
                "primaryObjectName": "npscr34_scrap",
                "objectOrdinal": 34,
                "descriptorSketch": {
                    "subtypeWord": "0x0c00",
                    "controlWord": "0x90000000",
                    "paramWord": "0x7440003e",
                    "vectorWordA": "0x00000000",
                    "vectorWordB": "0x00000000",
                },
            },
        },
        {
            "channel": "udp",
            "peer": "192.168.0.63:50745",
            "time": "st2",
            "decoder": {
                "family": "peer_object_descriptor",
                "descriptorPayloadFamilyHint": "neutral_scrap_payload",
                "descriptorFamilyHint": "neutral_scrap_descriptor",
                "descriptorArchetypeHint": "scrap",
                "objectKind": "scrap",
                "primaryObjectName": "npscr110_scrap",
                "objectOrdinal": 110,
                "descriptorSketch": {
                    "subtypeWord": "0x0d00",
                    "controlWord": "0x61000000",
                    "paramWord": "0x64a00022",
                    "vectorWordA": "0x00000000",
                    "vectorWordB": "0x00000000",
                },
            },
        },
        {
            "channel": "udp",
            "peer": "192.168.0.63:50745",
            "time": "st3",
            "decoder": {
                "family": "peer_object_descriptor",
                "descriptorPayloadFamilyHint": "neutral_scrap_payload",
                "descriptorFamilyHint": "neutral_scrap_descriptor",
                "descriptorArchetypeHint": "scrap",
                "objectKind": "scrap",
                "primaryObjectName": "npscr24_scrap",
                "objectOrdinal": 24,
                "descriptorSketch": {
                    "subtypeWord": "0x0d00",
                    "controlWord": "0xcd000000",
                    "paramWord": "0x5ec00034",
                    "vectorWordA": "0x00000000",
                    "vectorWordB": "0x00000000",
                },
            },
        },
        {
            "channel": "udp",
            "peer": "192.168.0.63:50745",
            "time": "st4",
            "decoder": {
                "family": "peer_object_descriptor",
                "descriptorPayloadFamilyHint": "neutral_scrap_payload",
                "descriptorFamilyHint": "neutral_scrap_descriptor",
                "descriptorArchetypeHint": "scrap",
                "objectKind": "scrap",
                "primaryObjectName": "npscr39_scrap",
                "objectOrdinal": 39,
                "descriptorSketch": {
                    "subtypeWord": "0x0b00",
                    "controlWord": "0x6d800000",
                    "paramWord": "0x0c400020",
                    "vectorWordA": "0x00000000",
                    "vectorWordB": "0x00000000",
                },
            },
        },
    ]
    scrap_subtype_family_summary = summarize_protocol_history(scrap_subtype_family_history)
    scrap_subtype_family_comparisons = scrap_subtype_family_summary.get("descriptorPayloadComparisons")
    if not isinstance(scrap_subtype_family_comparisons, list):
        return False, f"scrap subtype family comparisons missing: {scrap_subtype_family_summary}"
    scrap_subtype_family = next(
        (item for item in scrap_subtype_family_comparisons if item.get("payloadFamily") == "neutral_scrap_payload"),
        None,
    )
    if not isinstance(scrap_subtype_family, dict):
        return False, f"scrap subtype family summary missing: {scrap_subtype_family_comparisons}"
    expected_scrap_subtype_word_clusters = {
        "0x0c00": {
            "count": 2,
            "objectNames": ["npscr29_scrap", "npscr34_scrap"],
            "ordinals": [29, 34],
            "controlHalf0": {"0x3580": 1, "0x9000": 1},
            "paramHalf1": {"0x000b": 1, "0x003e": 1},
        },
        "0x0d00": {
            "count": 2,
            "objectNames": ["npscr110_scrap", "npscr24_scrap"],
            "ordinals": [24, 110],
            "controlHalf0": {"0x6100": 1, "0xcd00": 1},
            "paramHalf1": {"0x0022": 1, "0x0034": 1},
        },
        "0x0b00": {
            "count": 1,
            "objectNames": ["npscr39_scrap"],
            "ordinals": [39],
            "controlHalf0": {"0x6d80": 1},
            "paramHalf1": {"0x0020": 1},
        },
    }
    if scrap_subtype_family.get("subtypeWordClusters") != expected_scrap_subtype_word_clusters:
        return False, f"scrap subtype word clusters mismatch: {scrap_subtype_family}"
    if scrap_subtype_family.get("reusedSubtypeWords") != ["0x0c00", "0x0d00"]:
        return False, f"scrap reused subtype words mismatch: {scrap_subtype_family}"
    expected_scrap_subtype_assessment = (
        "some subtypeWord values span multiple named objects, so subtypeWord looks more like a family/category lane than a unique object id; 0x0c00, 0x0d00 also carry multiple control/param overlays inside the same subtype family"
    )
    if scrap_subtype_family.get("subtypeWordAssessment") != expected_scrap_subtype_assessment:
        return False, f"scrap subtype assessment mismatch: {scrap_subtype_family}"

    scrap_subtype_mutation_history = [
        {
            "channel": "udp",
            "peer": "192.168.0.63:50745",
            "time": "sm0",
            "decoder": {
                "family": "peer_object_descriptor",
                "descriptorPayloadFamilyHint": "neutral_scrap_payload",
                "descriptorFamilyHint": "neutral_scrap_descriptor",
                "descriptorArchetypeHint": "scrap",
                "objectKind": "scrap",
                "primaryObjectName": "npscr111_scrap",
                "objectOrdinal": 111,
                "descriptorSketch": {
                    "subtypeWord": "0x0e00",
                    "controlWord": "0xaa000000",
                    "paramWord": "0x33000008",
                    "vectorWordA": "0x00000000",
                    "vectorWordB": "0x00000000",
                },
            },
        },
        {
            "channel": "udp",
            "peer": "192.168.0.63:50745",
            "time": "sm1",
            "decoder": {
                "family": "peer_object_descriptor",
                "descriptorPayloadFamilyHint": "neutral_scrap_payload",
                "descriptorFamilyHint": "neutral_scrap_descriptor",
                "descriptorArchetypeHint": "scrap",
                "objectKind": "scrap",
                "primaryObjectName": "npscr111_scrap",
                "objectOrdinal": 111,
                "descriptorSketch": {
                    "subtypeWord": "0x0e00",
                    "controlWord": "0xa9800000",
                    "paramWord": "0x33000008",
                    "vectorWordA": "0x00000000",
                    "vectorWordB": "0x00000000",
                },
            },
        },
        {
            "channel": "udp",
            "peer": "192.168.0.63:50745",
            "time": "sm2",
            "decoder": {
                "family": "peer_object_descriptor",
                "descriptorPayloadFamilyHint": "neutral_scrap_payload",
                "descriptorFamilyHint": "neutral_scrap_descriptor",
                "descriptorArchetypeHint": "scrap",
                "objectKind": "scrap",
                "primaryObjectName": "npscr35_scrap",
                "objectOrdinal": 35,
                "descriptorSketch": {
                    "subtypeWord": "0x0e00",
                    "controlWord": "0x0f000000",
                    "paramWord": "0xf4800011",
                    "vectorWordA": "0x00000000",
                    "vectorWordB": "0x00000000",
                },
            },
        },
    ]
    scrap_subtype_mutation_summary = summarize_protocol_history(scrap_subtype_mutation_history)
    scrap_subtype_mutation_comparisons = scrap_subtype_mutation_summary.get("descriptorPayloadComparisons")
    if not isinstance(scrap_subtype_mutation_comparisons, list):
        return False, f"scrap subtype mutation comparisons missing: {scrap_subtype_mutation_summary}"
    scrap_subtype_mutation_family = next(
        (item for item in scrap_subtype_mutation_comparisons if item.get("payloadFamily") == "neutral_scrap_payload"),
        None,
    )
    if not isinstance(scrap_subtype_mutation_family, dict):
        return False, f"scrap subtype mutation family summary missing: {scrap_subtype_mutation_comparisons}"
    expected_subtype_mutation_correlations = {
        "0x0e00": {
            "objectNames": ["npscr111_scrap", "npscr35_scrap"],
            "mutableObjectNames": ["npscr111_scrap"],
            "mutableFields": ["controlWord"],
            "controlHalf0": {"0x0f00": 1, "0xa980": 1, "0xaa00": 1},
            "paramHalf1": {"0x0008": 2, "0x0011": 1},
            "mutableControlHalf0": {"0xa980": 1, "0xaa00": 1},
            "mutableParamHalf1": {"0x0008": 2},
        }
    }
    if scrap_subtype_mutation_family.get("subtypeMutationCorrelations") != expected_subtype_mutation_correlations:
        return False, f"scrap subtype mutation correlations mismatch: {scrap_subtype_mutation_family}"
    if scrap_subtype_mutation_family.get("mutableSubtypeWords") != ["0x0e00"]:
        return False, f"scrap mutable subtype words mismatch: {scrap_subtype_mutation_family}"
    expected_subtype_mutation_assessment = (
        "0x0e00 is currently the only subtype family with an observed mutable object; the mutation still looks object-specific to npscr111_scrap inside that subtype family"
    )
    if scrap_subtype_mutation_family.get("subtypeMutationAssessment") != expected_subtype_mutation_assessment:
        return False, f"scrap subtype mutation assessment mismatch: {scrap_subtype_mutation_family}"

    scrap_transition_context_history = [
        {"channel": "udp", "peer": "192.168.0.63:50745", "time": "tc0", "decoder": scrap_decoded},
        {"channel": "udp", "peer": "192.168.0.63:50745", "time": "tc1", "decoder": scrap_mutation_a},
        {"channel": "udp", "peer": "192.168.0.63:50745", "time": "tc2", "decoder": scrap_decoded},
        {"channel": "udp", "peer": "192.168.0.63:50745", "time": "tc3", "decoder": po_special_decoded},
        {"channel": "udp", "peer": "192.168.0.63:50745", "time": "tc4", "decoder": scrap_mutation_b},
        {"channel": "udp", "peer": "192.168.0.63:50745", "time": "tc5", "decoder": po_special_decoded},
    ]
    scrap_transition_context_summary = summarize_protocol_history(scrap_transition_context_history)
    scrap_transition_context_comparisons = scrap_transition_context_summary.get("descriptorPayloadComparisons")
    if not isinstance(scrap_transition_context_comparisons, list):
        return False, f"scrap transition context comparisons missing: {scrap_transition_context_summary}"
    scrap_transition_context_family = next(
        (item for item in scrap_transition_context_comparisons if item.get("payloadFamily") == "neutral_scrap_payload"),
        None,
    )
    if not isinstance(scrap_transition_context_family, dict):
        return False, f"scrap transition context family summary missing: {scrap_transition_context_comparisons}"
    expected_mutable_object_observation_contexts = {
        "npscr111_scrap": [
            {
                "controlHalf0": "0xaa00",
                "paramHalf1": "0x0008",
                "subtypeWord": "0x0e00",
                "immediatePrevEvent": "npscr10_scrap",
                "immediateNextEvent": "npscr10_scrap",
                "immediatePrevShortControl": None,
                "immediateNextShortControl": None,
                "contextSignature": "no_short_control",
            },
            {
                "controlHalf0": "0xa980",
                "paramHalf1": "0x0008",
                "subtypeWord": "0x0e00",
                "immediatePrevEvent": "peer_control_unknown_po",
                "immediateNextEvent": "peer_control_unknown_po",
                "immediatePrevShortControl": "peer_control_unknown_po",
                "immediateNextShortControl": "peer_control_unknown_po",
                "contextSignature": "po_sandwiched",
            },
        ]
    }
    if scrap_transition_context_family.get("mutableObjectObservationContexts") != expected_mutable_object_observation_contexts:
        return False, f"scrap transition object contexts mismatch: {scrap_transition_context_family}"
    expected_transition_assessment = (
        "npscr111_scrap has a controlHalf0 transition context: 0xa980 is only observed when immediately bracketed by PO, while 0xaa00 is not"
    )
    if scrap_transition_context_family.get("mutableObjectTransitionAssessment") != expected_transition_assessment:
        return False, f"scrap transition assessment mismatch: {scrap_transition_context_family}"
    expected_transition_object_signatures = {
        "npscr10_scrap": {"no_short_control": 1, "po_trailing_edge": 1},
        "npscr111_scrap": {"no_short_control": 1, "po_sandwiched": 1},
    }
    if scrap_transition_context_family.get("objectObservationContextSignatures") != expected_transition_object_signatures:
        return False, f"scrap transition object signatures mismatch: {scrap_transition_context_family}"
    if scrap_transition_context_family.get("poSandwichedObjects") != ["npscr111_scrap"]:
        return False, f"scrap transition PO-sandwiched objects mismatch: {scrap_transition_context_family}"
    if scrap_transition_context_family.get("poLeadingEdgeObjects") != []:
        return False, f"scrap transition PO-leading-edge objects mismatch: {scrap_transition_context_family}"
    expected_transition_context_assessment = "PO-sandwiched descriptor observations appear for npscr111_scrap"
    if scrap_transition_context_family.get("objectObservationContextAssessment") != expected_transition_context_assessment:
        return False, f"scrap transition object context assessment mismatch: {scrap_transition_context_family}"
    expected_transition_po_context_control_comparisons = {
        "npscr111_scrap": {
            "contextSignatures": {"no_short_control": 1, "po_sandwiched": 1},
            "subtypeWords": {"0x0e00": 2},
            "controlHalf0": {"0xa980": 1, "0xaa00": 1},
            "paramWord": {"0x33000008": 2},
            "mutableFields": ["controlWord"],
            "poContextRole": "po_sandwiched",
        },
    }
    if scrap_transition_context_family.get("poContextControlComparisons") != expected_transition_po_context_control_comparisons:
        return False, f"scrap transition PO-context control comparisons mismatch: {scrap_transition_context_family}"
    expected_transition_po_context_assessment = (
        "among PO-sandwiched objects, only npscr111_scrap show multiple observed controlHalf0 values"
    )
    if scrap_transition_context_family.get("poContextControlAssessment") != expected_transition_po_context_assessment:
        return False, f"scrap transition PO-context control assessment mismatch: {scrap_transition_context_family}"

    scrap_state_timeline_history = [
        {
            "channel": "udp",
            "peer": "192.168.0.63:50745",
            "time": "2026-03-21 13:15:35.376028",
            "sourceLog": "kateri",
            "lineNumber": 31793,
            "decoder": scrap_mutation_a,
        },
        {
            "channel": "udp",
            "peer": "192.168.0.63:50745",
            "time": "2026-03-21 13:15:35.987732",
            "sourceLog": "ian",
            "lineNumber": 40555,
            "decoder": scrap_mutation_a,
        },
        {
            "channel": "udp",
            "peer": "192.168.0.63:50745",
            "time": "2026-03-21 13:15:45.301753",
            "sourceLog": "ian",
            "lineNumber": 42439,
            "decoder": scrap_mutation_b,
        },
    ]
    scrap_state_timeline_summary = summarize_protocol_history(scrap_state_timeline_history)
    scrap_state_timeline_family = next(
        (item for item in scrap_state_timeline_summary.get("descriptorPayloadComparisons", []) if item.get("payloadFamily") == "neutral_scrap_payload"),
        None,
    )
    if not isinstance(scrap_state_timeline_family, dict):
        return False, f"scrap state timeline family summary missing: {scrap_state_timeline_summary}"
    expected_state_timeline = {
        "npscr111_scrap": {
            "observationCount": 3,
            "controlHalf0Timeline": ["0xaa00", "0xaa00", "0xa980"],
            "uniqueControlHalf0Sequence": ["0xaa00", "0xa980"],
            "finalObservedControlHalf0": "0xa980",
            "reverseTransitionObserved": False,
            "timeRange": ["2026-03-21 13:15:35.376028", "2026-03-21 13:15:45.301753"],
            "sampleObservations": [
                {
                    "time": "2026-03-21 13:15:35.376028",
                    "sourceLog": "kateri",
                    "lineNumber": 31793,
                    "controlHalf0": "0xaa00",
                    "paramHalf1": "0x0008",
                },
                {
                    "time": "2026-03-21 13:15:35.987732",
                    "sourceLog": "ian",
                    "lineNumber": 40555,
                    "controlHalf0": "0xaa00",
                    "paramHalf1": "0x0008",
                },
                {
                    "time": "2026-03-21 13:15:45.301753",
                    "sourceLog": "ian",
                    "lineNumber": 42439,
                    "controlHalf0": "0xa980",
                    "paramHalf1": "0x0008",
                },
            ],
        }
    }
    if scrap_state_timeline_family.get("mutableObjectStateTimelines") != expected_state_timeline:
        return False, f"scrap state timeline mismatch: {scrap_state_timeline_family}"
    expected_state_timeline_assessment = (
        "npscr111_scrap currently shows a one-way observed controlHalf0 sequence 0xaa00 -> 0xa980 with no later reverse transition in the March 21 logs"
    )
    if scrap_state_timeline_family.get("mutableObjectStateTimelineAssessment") != expected_state_timeline_assessment:
        return False, f"scrap state timeline assessment mismatch: {scrap_state_timeline_family}"
    expected_state_transitions = {
        "npscr111_scrap": [
            {
                "fromControlHalf0": "0xaa00",
                "toControlHalf0": "0xa980",
                "previousObservationTime": "2026-03-21 13:15:35.987732",
                "changedObservationTime": "2026-03-21 13:15:45.301753",
                "previousObservationLineNumber": 40555,
                "changedObservationLineNumber": 42439,
                "immediatePrevEvent": "npscr111_scrap",
                "immediateNextEvent": None,
                "immediatePrevShortControl": None,
                "immediateNextShortControl": None,
                "precedingEventFamilies": {
                    "peer_object_descriptor": 1,
                },
                "precedingControlEvents": {},
                "precedingObjectNames": {
                    "npscr111_scrap": 1,
                },
                "nearestPrecedingStateBlock": None,
                "postTransitionPacketCount": 0,
                "followingEventFamilies": {},
                "followingObjectNames": {},
                "laterSameObjectControlHalf0": {},
            }
        ]
    }
    if scrap_state_timeline_family.get("mutableObjectStateTransitions") != expected_state_transitions:
        return False, f"scrap state transitions mismatch: {scrap_state_timeline_family}"
    expected_transition_trigger_assessment = (
        "npscr111_scrap has a partially decoded transition trigger context"
    )
    if scrap_state_timeline_family.get("mutableObjectTransitionTriggerAssessment") != expected_transition_trigger_assessment:
        return False, f"scrap transition trigger assessment mismatch: {scrap_state_timeline_family}"
    if scrap_state_timeline_family.get("mutableObjectPostTransitionRevisits") != expected_state_transitions:
        return False, f"scrap post-transition revisits mismatch: {scrap_state_timeline_family}"
    expected_post_transition_assessment = (
        "npscr111_scrap has no later descriptor revisit after the observed state change in the current log tail"
    )
    if scrap_state_timeline_family.get("mutableObjectPostTransitionAssessment") != expected_post_transition_assessment:
        return False, f"scrap post-transition assessment mismatch: {scrap_state_timeline_family}"

    full_consensus_summary = {
        "pcaps": ["ian.pcapng", "kateri.pcapng"],
        "objectNames": ["npscr111_scrap"],
        "subtypeWords": ["0x0e00"],
        "window": 4,
        "objectReports": [{"ignored": True}],
        "subtypeReports": [{"ignored": True}],
        "objectConsensusReports": [
            {
                "objectName": "npscr111_scrap",
                "pcapCount": 2,
                "pcaps": ["ian.pcapng", "kateri.pcapng"],
                "observationCounts": [2],
                "uniqueSubtypeWords": [["0x0e00"]],
                "uniqueParamHalf1Sets": [["0x0008"]],
                "uniqueControlHalf0Sequences": [["0xaa00", "0xa980"]],
                "transitionWindowCounts": [1],
                "assessment": "all pcaps agree on controlHalf0 sequence 0xaa00 -> 0xa980; all pcaps saw 2 observations; all pcaps saw 1 transition window(s)",
            }
        ],
        "subtypeConsensusReports": [
            {
                "subtypeWord": "0x0e00",
                "pcapCount": 2,
                "pcaps": ["ian.pcapng", "kateri.pcapng"],
                "objectCounts": [2],
                "objectNameSets": [["npscr111_scrap", "npscr35_scrap"]],
                "mutableObjectSets": [["npscr111_scrap"]],
                "singleSampleObjectSets": [["npscr35_scrap"]],
                "assessment": "all pcaps agree mutable objects are npscr111_scrap; all pcaps agree single-sample objects are npscr35_scrap; all pcaps agree subtype roster is npscr111_scrap, npscr35_scrap",
            }
        ],
        "perPcap": [{"ignored": True}],
    }
    compact_consensus_summary = build_consensus_only_summary(full_consensus_summary)
    expected_compact_consensus_summary = {
        "pcaps": ["ian.pcapng", "kateri.pcapng"],
        "objectNames": ["npscr111_scrap"],
        "subtypeWords": ["0x0e00"],
        "window": 4,
        "objectConsensusReports": full_consensus_summary["objectConsensusReports"],
        "subtypeConsensusReports": full_consensus_summary["subtypeConsensusReports"],
    }
    if compact_consensus_summary != expected_compact_consensus_summary:
        return False, f"consensus-only summary mismatch: {compact_consensus_summary}"

    identical_compare_result = compare_consensus(compact_consensus_summary, compact_consensus_summary)
    if identical_compare_result.get("identical") is not True:
        return False, f"identical consensus compare mismatch: {identical_compare_result}"
    if identical_compare_result.get("summary") != "baseline and candidate consensus artifacts match exactly":
        return False, f"identical consensus summary mismatch: {identical_compare_result}"
    if identical_compare_result.get("subtypeConsensusDiff", {}).get("unchangedCount") != 1:
        return False, f"identical subtype unchangedCount mismatch: {identical_compare_result}"
    if identical_compare_result.get("objectConsensusDiff", {}).get("unchangedCount") != 1:
        return False, f"identical object unchangedCount mismatch: {identical_compare_result}"

    changed_consensus_summary = {
        **compact_consensus_summary,
        "objectConsensusReports": [
            {
                **compact_consensus_summary["objectConsensusReports"][0],
                "uniqueControlHalf0Sequences": [["0xaa00", "0xaa80"]],
                "assessment": "all pcaps agree on controlHalf0 sequence 0xaa00 -> 0xaa80; all pcaps saw 2 observations; all pcaps saw 1 transition window(s)",
            }
        ],
        "subtypeConsensusReports": [
            {
                **compact_consensus_summary["subtypeConsensusReports"][0],
                "mutableObjectSets": [[]],
                "assessment": "all pcaps agree no mutable objects were observed; all pcaps agree single-sample objects are npscr35_scrap; all pcaps agree subtype roster is npscr111_scrap, npscr35_scrap",
            }
        ],
    }
    changed_compare_result = compare_consensus(compact_consensus_summary, changed_consensus_summary)
    if changed_compare_result.get("identical") is not False:
        return False, f"changed consensus compare mismatch: {changed_compare_result}"
    changed_subtype = changed_compare_result.get("subtypeConsensusDiff", {}).get("changed", [])
    if len(changed_subtype) != 1 or changed_subtype[0].get("subtypeWord") != "0x0e00":
        return False, f"changed subtype diff mismatch: {changed_compare_result}"
    changed_object = changed_compare_result.get("objectConsensusDiff", {}).get("changed", [])
    if len(changed_object) != 1 or changed_object[0].get("objectName") != "npscr111_scrap":
        return False, f"changed object diff mismatch: {changed_compare_result}"
    changed_subtype_fields = changed_subtype[0].get("fieldChanges", {})
    if changed_subtype_fields.get("mutableObjectSets") != {"baseline": [["npscr111_scrap"]], "candidate": [[]]}:
        return False, f"changed subtype field diff mismatch: {changed_compare_result}"
    changed_object_fields = changed_object[0].get("fieldChanges", {})
    if changed_object_fields.get("uniqueControlHalf0Sequences") != {
        "baseline": [["0xa980", "0xaa00"]],
        "candidate": [["0xaa00", "0xaa80"]],
    }:
        return False, f"changed object field diff mismatch: {changed_compare_result}"

    markdown_report = render_markdown_report(
        changed_compare_result,
        Path("baseline_consensus.json"),
        Path("candidate_consensus.json"),
    )
    if "# Capture Consensus Comparison" not in markdown_report:
        return False, f"markdown report header mismatch: {markdown_report}"
    if "- Baseline: `baseline_consensus.json`" not in markdown_report:
        return False, f"markdown report baseline mismatch: {markdown_report}"
    if "## Subtype Diff" not in markdown_report or "## Object Diff" not in markdown_report:
        return False, f"markdown report sections missing: {markdown_report}"
    if "### `0x0e00`" not in markdown_report or "### `npscr111_scrap`" not in markdown_report:
        return False, f"markdown report changed-entry headings missing: {markdown_report}"
    return True, "Direct UDP marker decoder checks succeeded"


async def main() -> int:
    args = parse_args()
    ws_ok, ws_msg = await ws_smoke(args.host, args.ws_port, args.timeout)
    rehost_ok, rehost_msg = await rehost_smoke(args.host, args.ws_port, args.timeout)
    udp_ok, udp_msg = udp_smoke(args.host, args.udp_port, args.timeout)
    decoder_ok, decoder_msg = decoder_smoke()
    health_ok, health_msg = (True, "Skipped")
    protocol_ok, protocol_msg = (True, "Skipped")
    captures_ok, captures_msg = (True, "Skipped")
    if not args.skip_health:
        health_ok, health_msg = health_smoke(args.host, args.health_port, args.timeout)
        protocol_ok, protocol_msg = protocol_smoke(args.host, args.health_port, args.timeout)
        captures_ok, captures_msg = captures_smoke(args.host, args.health_port, args.timeout)

    print(f"WS : {'PASS' if ws_ok else 'FAIL'} - {ws_msg}")
    print(f"RHST: {'PASS' if rehost_ok else 'FAIL'} - {rehost_msg}")
    print(f"UDP: {'PASS' if udp_ok else 'FAIL'} - {udp_msg}")
    print(f"DEC : {'PASS' if decoder_ok else 'FAIL'} - {decoder_msg}")
    print(f"HTTP: {'PASS' if health_ok else 'FAIL'} - {health_msg}")
    print(f"PROTO: {'PASS' if protocol_ok else 'FAIL'} - {protocol_msg}")
    print(f"CAP  : {'PASS' if captures_ok else 'FAIL'} - {captures_msg}")
    return 0 if (ws_ok and rehost_ok and udp_ok and decoder_ok and health_ok and protocol_ok and captures_ok) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
