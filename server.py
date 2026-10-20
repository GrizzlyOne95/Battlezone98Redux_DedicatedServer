#!/usr/bin/env python3
"""
Battlezone 98 Redux dedicated-server compatibility shim.

This service provides:
1. A WebSocket lobby server (default port 1337) for BZRNet-style JSON messages.
2. A UDP shim (default port 1338) for initial BZRNet P2P handshake probing.
3. An opaque UDP relay (default port 1339) for WS-negotiated peer fallback.

It is intentionally lightweight and in-memory to accelerate local testing and
community-hosted failover scenarios.
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import contextlib
import hashlib
import hmac
import ipaddress
import json
import logging
import os
from pathlib import Path
import re
import signal
import socket
import struct
import time
import unicodedata
import uuid
import weakref
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from relay_impairment import PASS as IMPAIR_PASS, RelayImpairment, parse_spec as parse_impairment_spec

websockets = None
ConnectionClosed = Exception


DEFAULT_WS_HOST = "0.0.0.0"
DEFAULT_WS_PORT = 1337
DEFAULT_UDP_HOST = "0.0.0.0"
DEFAULT_UDP_PORT = 1338
DEFAULT_RELAY_HOST = "0.0.0.0"
DEFAULT_RELAY_PORT = 1339
DEFAULT_RELAY_ROUTE_TTL_SECONDS = 180
BZR_TRANSPORT_HEADER_BYTES = 18
DEFAULT_HEALTH_HOST = "127.0.0.1"
DEFAULT_HEALTH_PORT = 8080
DEFAULT_STATE_FILE = "data/server_state.json"
STATE_FORMAT_VERSION = 1
MAX_STDOUT_HISTORY = 200
MAX_PROTOCOL_HISTORY = 400
BZR_CONTROL_MARKERS = {"SS", "KA", "PP", "PR", "PB", "PO", "PZ"}
BZR_CONTROL_MARKER_INFO = {
    "KA": {
        "controlEvent": "peer_keepalive",
        "likelyMeaning": "short keepalive or keepalive-ack marker",
        "meaningConfidence": "medium",
    },
    "PB": {
        "controlEvent": "peer_timing_sideband",
        "likelyMeaning": "short timing-sideband marker seen near PP and PR exchanges",
        "meaningConfidence": "low",
    },
    "PO": {
        "controlEvent": "peer_control_unknown_po",
        "likelyMeaning": "short object-side control marker with a float-like time field and small control words",
        "meaningConfidence": "low",
    },
    "PP": {
        "controlEvent": "peer_ping_request",
        "likelyMeaning": "ping or timing request frame; aligns with BZLogger PING SENT lines",
        "meaningConfidence": "high",
    },
    "PR": {
        "controlEvent": "peer_ping_response",
        "likelyMeaning": "ping or timing response paired against PP",
        "meaningConfidence": "high",
    },
    "PZ": {
        "controlEvent": "peer_control_unknown_pz",
        "likelyMeaning": "unresolved PZ control marker",
        "meaningConfidence": "low",
    },
    "SS": {
        "controlEvent": "peer_session_sync",
        "likelyMeaning": "repeated session-sync or step-sync marker during bootstrap ramp",
        "meaningConfidence": "medium",
    },
}

DEFAULT_WHITELIST = {
    "list": [
        "*.wav",
        "*.ogg",
        "*.ini",
        "*.inf",
        "*.jpg",
        "*.txt",
        "*.stb",
        "*.des",
        "*.dds",
        "*.png",
        "*.sta",
        "*mod.material",
    ],
    "major": 8,
    "minor": 0,
}

# reasonCode values returned on OnLobbyJoined / OnFailure. The client dispatcher
# (0x006BF2A0) reads a numeric "reasonCode"; 0 == success.
#
# Live-confirmed against the official service:
#   2026-08-24  4 = "Join Lobby Failed, Lobby no longer exists"
#   2026-08-25  5 = "Join Lobby Failed, Trying to join a lobby you're already in"
#                   (checked BEFORE lock/full when a member rejoins)
# Remaining values are provisional and numbered to avoid the confirmed slots;
# renumber only with fresh official evidence.
REASON_OK = 0
JOIN_FAIL_MISSING_ID = 1
JOIN_FAIL_INVALID_ID = 2
JOIN_FAIL_BAD_PASSWORD = 3
JOIN_FAIL_NOT_FOUND = 4
JOIN_FAIL_ALREADY_IN = 5
JOIN_FAIL_LOCKED = 6
JOIN_FAIL_FULL = 7

SUPPORTED_CLIENT_MESSAGES = [
    "Authorization",
    "CreateGame",
    "CreateLobby",
    "DeleteLobbyData",
    "DoEnterLounge",
    "DoExitLobby",
    "DoExitLounge",
    "DoJoinLobby",
    "DoKickUser",
    "DoPing",
    "DoP2PConnect",
    "DoP2PRoute",
    "DoSendChat",
    "DoSetLobbyOwner",
    "DoUpdateLAN",
    "DoUpdateWAN",
    "GetFullLog",
    "GetLobbyList",
    "GetStatLog",
    "GetUserList",
    "LockLobby",
    "Ping",
    "SetLobbyData",
    "SetLobbyMemberLimit",
    "SetPlayerData",
    "SetRelay",
    "SetTest",
]

SUPPORTED_SERVER_MESSAGES = [
    "OnAuthorization",
    "OnChatMessage",
    "OnDoEnterLounge",
    "OnDoExitLobby",
    "OnDoExitLounge",
    "OnEnableRelayChanged",
    "OnGetFullLog",
    "OnGetLobbyList",
    "OnGetStatLog",
    "OnGetUserList",
    "OnHeartbeat",
    "OnLANUpdated",
    "OnLobbyChanged",
    "OnLobbyCreated",
    "OnLobbyDataChanged",
    "OnLobbyJoined",
    "OnLobbyList",
    "OnLobbyListChanged",
    "OnLobbyMemberListChanged",
    "OnLobbyMemberP2PConnect",
    "OnP2PRoute",
    "OnLobbyRemoved",
    "OnRelayUpdated",
    "OnStdoutLogEntry",
    "OnUserDataChanged",
    "OnWANUpdated",
    "OnWhitelistUpdated",
]


def utc_now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def compact_preview(value: Any, *, limit: int = 240) -> str:
    if isinstance(value, (bytes, bytearray)):
        text = bytes(value).hex()
    else:
        try:
            text = json.dumps(value, separators=(",", ":"), sort_keys=True)
        except Exception:
            text = str(value)
    if len(text) > limit:
        return text[: limit - 3] + "..."
    return text


def _ascii_runs(data: bytes, *, min_len: int = 2) -> list[str]:
    runs: list[str] = []
    current = bytearray()
    for b in data:
        if 32 <= b <= 126:
            current.append(b)
            continue
        if len(current) >= min_len:
            runs.append(current.decode("ascii", errors="ignore"))
        current.clear()
    if len(current) >= min_len:
        runs.append(current.decode("ascii", errors="ignore"))
    return runs


def _dedupe_preserve(values: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return ordered


def _looks_like_steam_id(text: str) -> bool:
    return len(text) >= 8 and text.startswith("S") and text[1:].isdigit()


def _looks_like_unit_code(text: str) -> bool:
    lowered = text.lower()
    return 5 <= len(lowered) <= 8 and lowered.startswith(("bv", "cv", "sv", "iv", "av"))


def _looks_like_compact_object_code(text: str) -> bool:
    lowered = text.lower()
    if _looks_like_unit_code(lowered):
        return True
    if not (5 <= len(lowered) <= 8):
        return False
    if "_" in lowered or not lowered[0].isalpha() or not lowered.isalnum():
        return False
    if lowered in BZR_CONTROL_MARKERS or _looks_like_steam_id(text):
        return False
    return any(ch.isalpha() for ch in lowered) and any(ch.isdigit() for ch in lowered) or lowered.isalpha()


def _looks_like_object_name(text: str) -> bool:
    lowered = text.lower()
    return "_" in lowered and len(lowered) >= 8 and lowered.replace("_", "").isalnum()


OBJECT_NAME_PATTERN = re.compile(r"([a-z][a-z0-9]{4,}_[a-z][a-z0-9_]{3,})")


def _normalize_object_token(text: str) -> str:
    lowered = text.lower()
    start = 0
    end = len(lowered)
    while start < end and not (lowered[start].isalnum() or lowered[start] == "_"):
        start += 1
    while end > start and not (lowered[end - 1].isalnum() or lowered[end - 1] == "_"):
        end -= 1
    return lowered[start:end]


def _split_object_name(text: str) -> tuple[str, str, int | None]:
    lowered = text.lower()
    stem, _, object_kind = lowered.partition("_")
    base = stem
    ordinal: int | None = None
    while base and base[-1].isdigit():
        base = base[:-1]
    if len(base) < len(stem):
        ordinal = int(stem[len(base) :])
    return base, object_kind, ordinal


def _extract_object_names_from_runs(ascii_runs: list[str]) -> list[str]:
    found: list[str] = []
    for run in ascii_runs:
        lowered = run.lower()
        for match in OBJECT_NAME_PATTERN.finditer(lowered):
            token = _normalize_object_token(match.group(1))
            if _looks_like_object_name(token):
                found.append(token)
    return _dedupe_preserve(found)


def _side_label_from_code_prefix(prefix: str) -> str:
    return {
        "b": "b_side",
        "c": "c_side",
        "n": "neutral",
    }.get(prefix, f"{prefix}_side")


def _build_descriptor_field_hints(descriptor_sketch: dict[str, Any], owner_word: int) -> dict[str, Any]:
    owner_lane = {
        0x0100: "owner_index_1",
        0x0200: "owner_index_2",
    }.get(owner_word, f"owner_word_{owner_word:04x}")
    return {
        "identityWords": {
            "classWord": descriptor_sketch["classWord"],
            "subtypeWord": descriptor_sketch["subtypeWord"],
            "ownerWord": descriptor_sketch["ownerWord"],
        },
        "payloadWords": {
            "controlWord": descriptor_sketch["controlWord"],
            "paramWord": descriptor_sketch["paramWord"],
            "vectorWordA": descriptor_sketch["vectorWordA"],
            "vectorWordB": descriptor_sketch["vectorWordB"],
        },
        "ownerLane": owner_lane,
        "stabilityAssessment": "class/subtype/owner words look family-stable in March 21 captures; later words vary more like descriptor payload",
    }


def _build_descriptor_archetype_hint(object_kind: str, person_role: str | None) -> str:
    if object_kind == "person" and person_role:
        return f"person_{person_role}"
    return object_kind


def _build_descriptor_payload_family_hint(
    side_label: str | None,
    object_kind: str | None,
    descriptor_sketch: dict[str, Any] | None,
) -> str | None:
    if not side_label or not object_kind or not descriptor_sketch:
        return None

    control_word = descriptor_sketch.get("controlWord")
    if object_kind in {"turrettank", "recycler"} and control_word == "0x00000000":
        return f"{side_label}_structure_payload"
    if object_kind in {"scavenger", "wingman"} and control_word in {"0xda000000", "0xbd000000"}:
        return f"{side_label}_mobile_unit_payload"
    if object_kind == "person":
        return f"{side_label}_person_payload"
    if object_kind == "scrap":
        return f"{side_label}_scrap_payload"
    return f"{side_label}_{object_kind}_payload"


def _build_descriptor_payload_hints(
    descriptor_sketch: dict[str, Any],
    payload_family_hint: str | None,
) -> dict[str, Any]:
    control_word = descriptor_sketch["controlWord"]
    param_word = descriptor_sketch["paramWord"]
    vector_word_a = descriptor_sketch["vectorWordA"]
    vector_word_b = descriptor_sketch["vectorWordB"]
    payload_hints = {
        "controlLaneWords": {
            "controlWord": control_word,
            "paramWord": param_word,
        },
        "vectorLaneWords": {
            "vectorWordA": vector_word_a,
            "vectorWordB": vector_word_b,
        },
        "payloadLaneAssessment": "controlWord/paramWord look more family-side dependent; vectorWordA/vectorWordB vary more like per-descriptor payload",
    }
    if payload_family_hint:
        payload_hints["controlSignature"] = f"{payload_family_hint}:{control_word}/{param_word}"
    return payload_hints


def _read_u16_le(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset : offset + 2], "little")


def _read_u32_le(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset : offset + 4], "little")


def _read_i8(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset : offset + 1], "little", signed=True)


def _hex_u16(data: bytes, offset: int) -> str:
    return f"0x{_read_u16_le(data, offset):04x}"


def _hex_u32(data: bytes, offset: int) -> str:
    return f"0x{_read_u32_le(data, offset):08x}"


def _decode_bzr_transport_header(data: bytes) -> dict[str, Any]:
    """Decode only the independently corroborated common BZRNet wrapper.

    Bytes 0..17 form the common transport header, bytes 16..17 are a big-endian
    16-bit sequence number, and application/control payload starts at byte 18.
    Fields that are not yet named with confidence remain deliberately opaque.
    """
    if len(data) < BZR_TRANSPORT_HEADER_BYTES:
        return {}

    sequence = int.from_bytes(data[16:18], "big")
    flags_byte = data[0]
    return {
        "payloadOffset": BZR_TRANSPORT_HEADER_BYTES,
        "transportHeader": {
            "headerBytes": BZR_TRANSPORT_HEADER_BYTES,
            "headerHex": data[:BZR_TRANSPORT_HEADER_BYTES].hex(),
            "flagsByte": f"0x{flags_byte:02x}",
            "packetKind": data[1],
            "packetKindHex": f"0x{data[1]:02x}",
            "opaqueBytes2To15": data[2:16].hex(),
            "sequence": sequence,
            "sequenceEncoding": "u16-be",
            "payloadOffset": BZR_TRANSPORT_HEADER_BYTES,
            "payloadBytes": max(0, len(data) - BZR_TRANSPORT_HEADER_BYTES),
            "reliabilityHint": "reliable" if flags_byte & 0x80 else "unreliable",
            "boundaryConfidence": "high",
            "sequenceConfidence": "high",
        },
    }


def _decode_pp_marker_payload(payload: bytes) -> dict[str, Any]:
    body = payload[2:]
    if len(body) < 20:
        return {}
    return {
        "timingFields": {
            "localMsTick": _read_u32_le(body, 0),
            "clockOffset": _read_u32_le(body, 4),
            "hostMsTick": _read_u32_le(body, 8),
            "delayUnits": _read_u32_le(body, 12),
            "flags": _read_u32_le(body, 16),
        },
        "fieldConfidence": "high",
        "fieldCorrelation": "matches BZLogger PING SENT fields mst/off/hst/dl",
    }


def _decode_pr_marker_payload(payload: bytes) -> dict[str, Any]:
    body = payload[2:]
    if len(body) < 12:
        return {}
    return {
        "timingFields": {
            "echoedPingMsTick": _read_u32_le(body, 0),
            "responderMsTick": _read_u32_le(body, 4),
            "responseWord": f"0x{_read_u32_le(body, 8):08x}",
        },
        "fieldConfidence": "medium",
        "fieldCorrelation": "first field matches the paired PP localMsTick in March 21 captures",
    }


def _decode_pb_marker_payload(payload: bytes) -> dict[str, Any]:
    body = payload[2:]
    if len(body) < 8:
        return {}
    return {
        "timingFields": {
            "bandwidthTarget": _read_u32_le(body, 0),
            "opaqueWord": f"0x{_read_u32_le(body, 4):08x}",
            "opaqueLow16": _read_u16_le(body, 4),
            "opaqueHigh16": _read_u16_le(body, 6),
        },
        "fieldConfidence": "medium",
        "fieldCorrelation": "first field aligns with TempStateSendAll target BW values in March 21 logs",
    }


def _decode_po_marker_payload(payload: bytes) -> dict[str, Any]:
    body = payload[2:]
    if len(body) < 10:
        return {}
    clock_seconds = struct.unpack_from("<f", body, 0)[0]
    timing_fields: dict[str, Any] = {
        "modeWord": f"0x{_read_u16_le(body, 4):04x}",
        "sequenceValue": _read_u16_le(body, 6),
        "laneValue": _read_u16_le(body, 8),
    }
    if 0.0 <= clock_seconds < 86400.0:
        timing_fields["clockSeconds"] = round(clock_seconds, 3)
    return {
        "timingFields": timing_fields,
        "fieldConfidence": "medium",
        "fieldCorrelation": "first float-like word tracks elapsed match time in March 21 captures; 0x0306/lane0 is common and a smaller 0x0206/lane2 burst appears later",
    }


def _decode_state_block(data: bytes, ascii_runs: list[str]) -> dict[str, Any]:
    body = data[18:]
    decoded: dict[str, Any] = {
        "family": "peer_state_block",
        "stateOpcode": f"0x{data[19]:02x}",
    }
    if len(body) >= 16:
        state_seconds = struct.unpack_from("<f", body, 2)[0]
        if 0.0 <= state_seconds < 86400.0:
            decoded["stateHeader"] = {
                "stateTimeSeconds": round(state_seconds, 3),
                "stateStreamWord": _read_u32_le(body, 6),
                "statePlayerIndex": _read_u16_le(body, 10),
                "stateHeaderWord": f"0x{_read_u32_le(body, 12):08x}",
            }
            decoded["fieldConfidence"] = "medium"
            decoded["fieldCorrelation"] = "float clock plus stable stream/player header words across March 21 state samples"

    unit_codes = _dedupe_preserve([text.lower() for text in ascii_runs if _looks_like_unit_code(text)])
    if unit_codes:
        decoded["unitCodes"] = unit_codes[:8]
        decoded["stateObjectCount"] = len(unit_codes)
    return decoded


def _decode_object_packet(data: bytes, ascii_runs: list[str]) -> dict[str, Any]:
    body = data[18:]
    decoded: dict[str, Any] = {
        "transportReliability": "reliable" if data[0] == 0xC0 else "unreliable",
        "objectPacketSignature": body[:2].hex(),
    }
    if len(body) >= 16:
        decoded["objectHeader"] = {
            "objectVariant": body[2],
            "objectIndex": _read_i8(body, 4),
            "descriptorFlags": body[5],
            "objectHeaderWord": f"0x{_read_u32_le(body, 10):08x}",
            "objectOwnerFlags": body[14],
            "objectOwnerIndex": body[15],
        }
    if len(body) >= 30:
        class_word = _read_u16_le(body, 10)
        subtype_word = _read_u16_le(body, 12)
        owner_word = _read_u16_le(body, 14)
        decoded["descriptorSketch"] = {
            "classWord": f"0x{class_word:04x}",
            "subtypeWord": f"0x{subtype_word:04x}",
            "ownerWord": f"0x{owner_word:04x}",
            "controlWord": _hex_u32(body, 16),
            "paramWord": _hex_u32(body, 20),
            "vectorWordA": _hex_u32(body, 24),
            "vectorWordB": _hex_u32(body, 28),
        }
        decoded["descriptorPattern"] = f"0x{class_word:04x}/0x{subtype_word:04x}/0x{owner_word:04x}"
        decoded["descriptorWordProfile"] = {
            "classLowByte": body[10],
            "classHighByte": body[11],
            "subtypeLowByte": body[12],
            "subtypeHighByte": body[13],
            "ownerLowByte": body[14],
            "ownerHighByte": body[15],
        }
        decoded["descriptorFieldHints"] = _build_descriptor_field_hints(decoded["descriptorSketch"], owner_word)

    normalized_runs = _dedupe_preserve(
        [token for token in (_normalize_object_token(text) for text in ascii_runs) if token]
    )
    object_names = _dedupe_preserve(
        [text for text in normalized_runs if _looks_like_object_name(text)] + _extract_object_names_from_runs(ascii_runs)
    )
    derived_codes = []
    for object_name in object_names:
        stem, _, _ = _split_object_name(object_name)
        if stem:
            derived_codes.append(stem)
    object_codes = _dedupe_preserve(
        [text for text in normalized_runs if _looks_like_compact_object_code(text)] + derived_codes
    )
    if object_codes:
        decoded["objectCodes"] = object_codes[:8]
    if object_names:
        decoded["objectNames"] = object_names[:8]

    if object_codes or object_names:
        if object_names:
            primary_name = object_names[0]
            name_base, object_kind, object_ordinal = _split_object_name(primary_name)
            decoded["primaryObjectName"] = primary_name
            decoded["objectNameStem"] = name_base
            decoded["objectKind"] = object_kind
            if object_ordinal is not None:
                decoded["objectOrdinal"] = object_ordinal
        if object_codes:
            primary_code = object_codes[0]
            decoded["primaryObjectCode"] = primary_code
            decoded["sideCodePrefix"] = primary_code[0]
            decoded["sideLabel"] = _side_label_from_code_prefix(primary_code[0])
            if decoded.get("objectKind") == "person":
                if primary_code.endswith("pilo"):
                    decoded["personRole"] = "pilot"
                elif primary_code.endswith("user"):
                    decoded["personRole"] = "user"
        object_kind = decoded.get("objectKind")
        side_label = decoded.get("sideLabel")
        if isinstance(object_kind, str) and isinstance(side_label, str):
            person_role = decoded.get("personRole")
            family_parts = [side_label, object_kind]
            if isinstance(person_role, str) and person_role:
                family_parts.append(person_role)
            decoded["descriptorFamilyHint"] = "_".join(family_parts) + "_descriptor"
            decoded["descriptorArchetypeHint"] = _build_descriptor_archetype_hint(object_kind, person_role if isinstance(person_role, str) else None)
            payload_family_hint = _build_descriptor_payload_family_hint(
                side_label,
                object_kind,
                decoded.get("descriptorSketch") if isinstance(decoded.get("descriptorSketch"), dict) else None,
            )
            if payload_family_hint:
                decoded["descriptorPayloadFamilyHint"] = payload_family_hint
                descriptor_sketch = decoded.get("descriptorSketch")
                if isinstance(descriptor_sketch, dict):
                    decoded["descriptorPayloadHints"] = _build_descriptor_payload_hints(
                        descriptor_sketch,
                        payload_family_hint,
                    )

        decoded["family"] = "peer_object_descriptor"
        decoded["fieldConfidence"] = "medium"
        decoded["fieldCorrelation"] = "7a75 packets carry unit codes and stable object names ahead of the first 0x5f state stream"
        return decoded

    if len(body) >= 17 and body[10:13] == bytes.fromhex("060601"):
        control_value = _read_u16_le(body, 4)
        if 0x2100 <= control_value <= 0x21FF:
            decoded["family"] = "peer_object_presence"
            decoded["objectSetupEvent"] = "object_presence"
            decoded["presenceKeyWord"] = f"0x{control_value:04x}"
            decoded["presenceFlags"] = body[13]
            decoded["presenceOwnerIndex"] = body[15] if len(body) >= 16 else None
            decoded["fieldConfidence"] = "medium"
            decoded["fieldCorrelation"] = "short 7a75 060601 packets appear during object presence/setup exchange"
            return decoded

        decoded["family"] = "peer_object_counter"
        decoded["objectSetupEvent"] = "object_counter"
        decoded["counterValue"] = control_value
        decoded["counterFlags"] = body[13]
        decoded["fieldConfidence"] = "medium"
        decoded["fieldCorrelation"] = "short 7a75 060601 packets repeat as synchronized object/setup counters"
        return decoded

    if len(body) >= 31 and body[10:14] == (20).to_bytes(4, "little"):
        decoded["family"] = "peer_object_batch"
        decoded["objectSetupEvent"] = "object_roster_batch"
        decoded["batchDescriptorCount"] = _read_u32_le(body, 23) if len(body) >= 27 else None
        decoded["batchOwnerIndex"] = _read_u16_le(body, 28) if len(body) >= 30 else None
        decoded["fieldConfidence"] = "medium"
        decoded["fieldCorrelation"] = "31-byte 7a75 header packets precede object descriptor bursts in March 21 captures"
        return decoded

    if len(body) == 15 and body[10:12] in {bytes.fromhex("0401"), bytes.fromhex("0402")}:
        decoded["family"] = "peer_object_setup_marker"
        decoded["objectSetupEvent"] = "object_setup_marker"
        decoded["setupMarkerWord"] = f"0x{_read_u16_le(body, 10):04x}"
        decoded["fieldConfidence"] = "low"
        decoded["fieldCorrelation"] = "short 7a75 setup markers appear around roster refresh points"
        return decoded

    decoded["family"] = "peer_object_control"
    decoded["controlBodyBytes"] = len(body)
    decoded["controlBodySignature"] = body[2:].hex() if len(body) >= 2 else body.hex()
    if len(body) >= 12:
        decoded["controlModeWord"] = f"0x{_read_u16_le(body, 10):04x}"
    if len(body) >= 14:
        decoded["controlTailWord"] = f"0x{_read_u32_le(body, 10):08x}"
    decoded["fieldConfidence"] = "low"
    decoded["fieldCorrelation"] = "7a75 short packets appear during roster/setup exchange but remain only partially decoded"
    return decoded


def _describe_bzr_control_marker(marker: str, payload: bytes) -> dict[str, Any]:
    info = BZR_CONTROL_MARKER_INFO.get(marker)
    if not info:
        return {}

    description = dict(info)
    description["controlPayloadBytes"] = len(payload)
    if marker == "SS" and len(payload) >= 3:
        description["syncVariant"] = payload[2]
    elif marker == "PP":
        description.update(_decode_pp_marker_payload(payload))
    elif marker == "PR":
        description.update(_decode_pr_marker_payload(payload))
    elif marker == "PB":
        description.update(_decode_pb_marker_payload(payload))
    elif marker == "PO":
        description.update(_decode_po_marker_payload(payload))
    return description


def decode_bzr_udp_datagram(data: bytes) -> dict[str, Any]:
    decoded: dict[str, Any] = {
        "payloadBytes": len(data),
        "lead": data[:2].hex() if len(data) >= 2 else data.hex(),
    }
    if not data:
        decoded["family"] = "empty"
        return decoded

    if len(data) >= BZR_TRANSPORT_HEADER_BYTES:
        decoded.update(_decode_bzr_transport_header(data))

    if len(data) >= 18 and data[0] == 0x40 and data[1] == 0x02:
        if len(data) == 18:
            decoded["family"] = "bootstrap_probe"
            return decoded
        if len(data) == 25 and data[18] == 0x04:
            decoded["family"] = "bootstrap_reply"
            decoded["publicEndpoint"] = f"{socket.inet_ntoa(data[19:23])}:{int.from_bytes(data[23:25], 'big')}"
            return decoded
        decoded["family"] = "bootstrap_variant"
        return decoded

    ascii_runs = _ascii_runs(data, min_len=2)
    steam_ids = [text for text in ascii_runs if _looks_like_steam_id(text)]
    if steam_ids and len(data) >= 2 and data[1] in {0x03, 0x04, 0x05}:
        decoded["family"] = "peer_identity"
        decoded["variant"] = f"0x{data[1]:02x}"
        decoded["steamIds"] = steam_ids[:2]
        return decoded

    if len(data) >= 20 and data[18:20] == bytes.fromhex("7a75"):
        decoded.update(_decode_object_packet(data, ascii_runs))
        return decoded

    marker = ""
    marker_offset = -1
    if len(data) >= 20 and all(65 <= b <= 90 for b in data[18:20]):
        marker = data[18:20].decode("ascii", errors="ignore")
        marker_offset = 18
    elif len(data) >= 2 and all(65 <= b <= 90 for b in data[-2:]):
        marker = data[-2:].decode("ascii", errors="ignore")
        marker_offset = len(data) - 2
    if marker in BZR_CONTROL_MARKERS:
        decoded["marker"] = marker
        marker_payload = data[marker_offset:] if marker_offset >= 0 else b""
        decoded["markerOffset"] = marker_offset
        decoded.update(_describe_bzr_control_marker(marker, marker_payload))

    interesting_strings = _dedupe_preserve(
        [
            text
            for text in ascii_runs
            if _looks_like_steam_id(text)
            or _looks_like_unit_code(text)
            or text in BZR_CONTROL_MARKERS
        ]
    )
    if interesting_strings:
        decoded["asciiStrings"] = interesting_strings[:6]

    if len(data) >= 20 and data[18] == 0x5F:
        decoded.update(_decode_state_block(data, ascii_runs))
        return decoded

    if marker:
        decoded["family"] = "peer_control_marker"
        return decoded

    if steam_ids:
        decoded["family"] = "peer_identity_variant"
        decoded["steamIds"] = steam_ids[:2]
        return decoded

    if len(data) >= 20 and data[0] in {0x40, 0xC0}:
        decoded["family"] = "peer_unknown"
        return decoded

    decoded["family"] = "unknown"
    return decoded


def summarize_protocol_history(history: list[dict[str, Any]]) -> dict[str, Any]:
    channels: dict[str, int] = {}
    udp_families: dict[str, int] = {}
    udp_control_events: dict[str, int] = {}
    udp_object_events: dict[str, int] = {}
    ws_types: dict[str, int] = {}
    for entry in history:
        channel = str(entry.get("channel", "unknown"))
        channels[channel] = channels.get(channel, 0) + 1
        if channel == "udp":
            decoder = entry.get("decoder", {})
            family = str(decoder.get("family", "unclassified"))
            udp_families[family] = udp_families.get(family, 0) + 1
            control_event = decoder.get("controlEvent")
            if isinstance(control_event, str) and control_event:
                udp_control_events[control_event] = udp_control_events.get(control_event, 0) + 1
            object_event = decoder.get("objectSetupEvent")
            if isinstance(object_event, str) and object_event:
                udp_object_events[object_event] = udp_object_events.get(object_event, 0) + 1
        elif channel == "ws":
            msg_type = str(entry.get("type", "unknown"))
            ws_types[msg_type] = ws_types.get(msg_type, 0) + 1
    return {
        "channels": channels,
        "udpFamilies": udp_families,
        "udpControlEvents": udp_control_events,
        "udpObjectEvents": udp_object_events,
        "wsTypes": ws_types,
        "descriptorPayloadComparisons": summarize_descriptor_payload_families(history),
        "descriptorStateCorrelations": summarize_descriptor_state_correlations(history),
        "descriptorFollowups": summarize_descriptor_followups(history),
        "unresolvedControlComparisons": summarize_unresolved_control_traffic(history),
        "peerSessions": summarize_peer_udp_sessions(history),
    }


def _phase_for_udp_family(family: str) -> str:
    if family in {"peer_identity", "peer_identity_variant"}:
        return "identity"
    if family in {
        "peer_object_descriptor",
        "peer_object_control",
        "peer_object_presence",
        "peer_object_counter",
        "peer_object_batch",
        "peer_object_setup_marker",
    }:
        return "objects"
    if family == "peer_control_marker":
        return "control"
    if family == "peer_state_block":
        return "state"
    if family.startswith("peer_"):
        return "peer_other"
    return "other"


def _sorted_count_map(values: dict[str, int]) -> dict[str, int]:
    return dict(sorted(values.items(), key=lambda item: (-item[1], item[0])))


def _top_count_key(values: dict[str, int]) -> str | None:
    if not values:
        return None
    return sorted(values.items(), key=lambda item: (-item[1], item[0]))[0][0]


def _parse_hex_word(value: str) -> int | None:
    if not isinstance(value, str) or not value.startswith("0x"):
        return None
    try:
        return int(value, 16)
    except ValueError:
        return None


HEX_BYTE_LANES = ("hexByte0", "hexByte1", "hexByte2", "hexByte3")
HEX_HALF_LANES = ("hexHalf0", "hexHalf1")


def _hex_word_bytes(value: int) -> tuple[int, int, int, int]:
    return (
        (value >> 24) & 0xFF,
        (value >> 16) & 0xFF,
        (value >> 8) & 0xFF,
        value & 0xFF,
    )


def _summarize_word_byte_profiles(counts: dict[str, int]) -> dict[str, Any] | None:
    if not counts:
        return None
    lane_counts: dict[str, dict[str, int]] = {lane: {} for lane in HEX_BYTE_LANES}
    for value, value_count in counts.items():
        parsed_value = _parse_hex_word(value)
        if parsed_value is None:
            continue
        for lane_name, byte_value in zip(HEX_BYTE_LANES, _hex_word_bytes(parsed_value)):
            bucket = lane_counts[lane_name]
            byte_hex = f"0x{byte_value:02x}"
            bucket[byte_hex] = bucket.get(byte_hex, 0) + value_count

    stable_lanes: list[str] = []
    varying_lanes: list[str] = []
    always_zero_lanes: list[str] = []
    common_lane_values: dict[str, str] = {}
    for lane_name, bucket in lane_counts.items():
        if not bucket:
            continue
        if len(bucket) == 1:
            stable_lanes.append(lane_name)
            only_value = next(iter(bucket))
            common_lane_values[lane_name] = only_value
            if only_value == "0x00":
                always_zero_lanes.append(lane_name)
        else:
            varying_lanes.append(lane_name)

    return {
        "lanes": {lane_name: _sorted_count_map(bucket) for lane_name, bucket in lane_counts.items()},
        "stableLanes": stable_lanes,
        "varyingLanes": varying_lanes,
        "alwaysZeroLanes": always_zero_lanes,
        "commonLaneValues": common_lane_values,
    }


def _summarize_word_half_profiles(counts: dict[str, int]) -> dict[str, Any] | None:
    if not counts:
        return None
    lane_counts: dict[str, dict[str, int]] = {lane: {} for lane in HEX_HALF_LANES}
    for value, value_count in counts.items():
        parsed_value = _parse_hex_word(value)
        if parsed_value is None:
            continue
        upper_half = f"0x{(parsed_value >> 16) & 0xFFFF:04x}"
        lower_half = f"0x{parsed_value & 0xFFFF:04x}"
        for lane_name, half_value in zip(HEX_HALF_LANES, (upper_half, lower_half)):
            bucket = lane_counts[lane_name]
            bucket[half_value] = bucket.get(half_value, 0) + value_count

    stable_lanes: list[str] = []
    varying_lanes: list[str] = []
    always_zero_lanes: list[str] = []
    common_lane_values: dict[str, str] = {}
    for lane_name, bucket in lane_counts.items():
        if not bucket:
            continue
        if len(bucket) == 1:
            stable_lanes.append(lane_name)
            only_value = next(iter(bucket))
            common_lane_values[lane_name] = only_value
            if only_value == "0x0000":
                always_zero_lanes.append(lane_name)
        else:
            varying_lanes.append(lane_name)

    return {
        "lanes": {lane_name: _sorted_count_map(bucket) for lane_name, bucket in lane_counts.items()},
        "stableLanes": stable_lanes,
        "varyingLanes": varying_lanes,
        "alwaysZeroLanes": always_zero_lanes,
        "commonLaneValues": common_lane_values,
    }


def _summarize_mutating_word(counts: dict[str, int]) -> dict[str, Any] | None:
    if len(counts) <= 1:
        return None
    ordered_values = list(sorted(counts))
    parsed_values = [_parse_hex_word(value) for value in ordered_values]
    if any(value is None for value in parsed_values):
        return {
            "values": ordered_values,
            "valueCounts": _sorted_count_map(counts),
        }
    base_value = parsed_values[0]
    assert base_value is not None
    xor_values = sorted({base_value ^ value for value in parsed_values[1:] if value is not None})
    mutation_bits = sorted(
        {
            bit_index
            for xor_value in xor_values
            for bit_index in range(32)
            if xor_value & (1 << bit_index)
        }
    )
    changed_byte_lanes = sorted(
        {
            HEX_BYTE_LANES[lane_index]
            for xor_value in xor_values
            for lane_index, lane_byte in enumerate(_hex_word_bytes(xor_value))
            if lane_byte
        }
    )
    changed_half_lanes = sorted(
        {
            HEX_HALF_LANES[0] if xor_value & 0xFFFF0000 else HEX_HALF_LANES[1]
            for xor_value in xor_values
            if xor_value
        }
    )
    return {
        "values": ordered_values,
        "valueCounts": _sorted_count_map(counts),
        "xorFromFirstValue": [f"0x{xor_value:08x}" for xor_value in xor_values],
        "bitPositions": mutation_bits,
        "changedByteLanes": changed_byte_lanes,
        "changedHalfLanes": changed_half_lanes,
    }


def _update_halfword_cluster(
    clusters: dict[str, dict[str, Any]],
    cluster_key: str,
    *,
    object_name: str | None,
    object_ordinal: int | None,
    paired_half0: str | None = None,
    paired_half1: str | None = None,
) -> None:
    cluster = clusters.setdefault(
        cluster_key,
        {
            "count": 0,
            "objectNames": set(),
            "ordinals": set(),
            "pairedHalf0": {},
            "pairedHalf1": {},
        },
    )
    cluster["count"] += 1
    if isinstance(object_name, str) and object_name:
        cluster["objectNames"].add(object_name)
    if isinstance(object_ordinal, int):
        cluster["ordinals"].add(object_ordinal)
    if isinstance(paired_half0, str) and paired_half0:
        cluster["pairedHalf0"][paired_half0] = cluster["pairedHalf0"].get(paired_half0, 0) + 1
    if isinstance(paired_half1, str) and paired_half1:
        cluster["pairedHalf1"][paired_half1] = cluster["pairedHalf1"].get(paired_half1, 0) + 1


def _build_halfword_cluster_hints(clusters: dict[str, dict[str, Any]], total_object_names: int) -> dict[str, dict[str, Any]]:
    hints: dict[str, dict[str, Any]] = {}
    for key, value in clusters.items():
        object_name_count = len(value["objectNames"])
        paired_half0_count = len(value["pairedHalf0"])
        paired_half1_count = len(value["pairedHalf1"])
        scope_hint = "single_object_specific"
        if object_name_count > 1 and object_name_count == total_object_names:
            scope_hint = "family_shared"
        elif object_name_count > 1:
            scope_hint = "multi_object_category"

        lane_hint = "mixed_cluster"
        if paired_half0_count == 1 and paired_half1_count == 1:
            lane_hint = "paired_halves_stable"
        elif paired_half0_count > 1 and paired_half1_count == 1:
            lane_hint = "paired_half0_varies"
        elif paired_half0_count == 1 and paired_half1_count > 1:
            lane_hint = "paired_half1_varies"
        elif paired_half0_count > 1 and paired_half1_count > 1:
            lane_hint = "paired_halves_both_vary"

        hints[key] = {
            "scopeHint": scope_hint,
            "laneHint": lane_hint,
            "objectNameCount": object_name_count,
            "ordinalCount": len(value["ordinals"]),
            "pairedHalf0Count": paired_half0_count,
            "pairedHalf1Count": paired_half1_count,
        }
    return hints


def _update_vector_alignment_cluster(
    clusters: dict[str, dict[str, Any]],
    cluster_key: str,
    *,
    object_name: str | None,
    object_ordinal: int | None,
    paired_vector_a_half1: str | None = None,
    paired_vector_b_half0: str | None = None,
) -> None:
    cluster = clusters.setdefault(
        cluster_key,
        {
            "count": 0,
            "objectNames": set(),
            "ordinals": set(),
            "pairedVectorAHalf1": {},
            "pairedVectorBHalf0": {},
        },
    )
    cluster["count"] += 1
    if isinstance(object_name, str) and object_name:
        cluster["objectNames"].add(object_name)
    if isinstance(object_ordinal, int):
        cluster["ordinals"].add(object_ordinal)
    if isinstance(paired_vector_a_half1, str) and paired_vector_a_half1:
        bucket = cluster["pairedVectorAHalf1"]
        bucket[paired_vector_a_half1] = bucket.get(paired_vector_a_half1, 0) + 1
    if isinstance(paired_vector_b_half0, str) and paired_vector_b_half0:
        bucket = cluster["pairedVectorBHalf0"]
        bucket[paired_vector_b_half0] = bucket.get(paired_vector_b_half0, 0) + 1


def _build_vector_alignment_hints(clusters: dict[str, dict[str, Any]], total_object_names: int) -> dict[str, dict[str, Any]]:
    hints: dict[str, dict[str, Any]] = {}
    for key, value in clusters.items():
        object_name_count = len(value["objectNames"])
        paired_vector_a_count = len(value["pairedVectorAHalf1"])
        paired_vector_b_count = len(value["pairedVectorBHalf0"])
        scope_hint = "single_object_specific"
        if object_name_count > 1 and object_name_count == total_object_names:
            scope_hint = "family_shared"
        elif object_name_count > 1:
            scope_hint = "multi_object_category"

        vector_lane_hint = "mixed_vector_cluster"
        if paired_vector_a_count == 1 and paired_vector_b_count == 1:
            vector_lane_hint = "vector_pair_stable"
        elif paired_vector_a_count > 1 and paired_vector_b_count == 1:
            vector_lane_hint = "vector_a_half1_varies"
        elif paired_vector_a_count == 1 and paired_vector_b_count > 1:
            vector_lane_hint = "vector_b_half0_varies"
        elif paired_vector_a_count > 1 and paired_vector_b_count > 1:
            vector_lane_hint = "vector_pair_both_vary"

        hints[key] = {
            "scopeHint": scope_hint,
            "vectorLaneHint": vector_lane_hint,
            "objectNameCount": object_name_count,
            "ordinalCount": len(value["ordinals"]),
            "pairedVectorAHalf1Count": paired_vector_a_count,
            "pairedVectorBHalf0Count": paired_vector_b_count,
        }
    return hints


def _assess_vector_cluster_alignment(cluster_hints: dict[str, dict[str, Any]], cluster_label: str) -> str:
    multi_object_keys = [
        key
        for key, value in cluster_hints.items()
        if value.get("scopeHint") == "multi_object_category"
    ]
    if not multi_object_keys:
        return f"no multi-object {cluster_label} vector clusters observed"
    if any(
        cluster_hints[key].get("vectorLaneHint") != "vector_pair_stable"
        for key in multi_object_keys
    ):
        return f"multi-object {cluster_label} clusters span multiple vector signatures; not a simple vector bucket"
    return f"multi-object {cluster_label} clusters currently map to stable vector signatures"


def summarize_descriptor_state_correlations(history: list[dict[str, Any]]) -> dict[str, Any]:
    descriptor_unit_codes: dict[str, int] = {}
    descriptor_kinds: dict[str, int] = {}
    descriptor_code_kinds: dict[str, dict[str, int]] = {}
    state_unit_codes: dict[str, int] = {}

    for entry in history:
        if entry.get("channel") != "udp":
            continue
        decoder = entry.get("decoder") or {}
        family = decoder.get("family")
        if family == "peer_object_descriptor":
            object_kind = decoder.get("objectKind")
            if isinstance(object_kind, str) and object_kind:
                descriptor_kinds[object_kind] = descriptor_kinds.get(object_kind, 0) + 1
            object_codes = decoder.get("objectCodes")
            if isinstance(object_codes, list):
                for code in object_codes:
                    if not isinstance(code, str) or not code:
                        continue
                    descriptor_unit_codes[code] = descriptor_unit_codes.get(code, 0) + 1
                    if isinstance(object_kind, str) and object_kind:
                        bucket = descriptor_code_kinds.setdefault(code, {})
                        bucket[object_kind] = bucket.get(object_kind, 0) + 1
        elif family == "peer_state_block":
            unit_codes = decoder.get("unitCodes")
            if isinstance(unit_codes, list):
                for code in unit_codes:
                    if not isinstance(code, str) or not code:
                        continue
                    state_unit_codes[code] = state_unit_codes.get(code, 0) + 1

    descriptor_code_set = set(descriptor_unit_codes)
    state_code_set = set(state_unit_codes)
    shared_unit_codes = sorted(descriptor_code_set & state_code_set)
    descriptor_only_unit_codes = sorted(descriptor_code_set - state_code_set)
    state_only_unit_codes = sorted(state_code_set - descriptor_code_set)

    state_unit_code_kinds: dict[str, str] = {}
    state_kind_counts: dict[str, int] = {}
    for code in shared_unit_codes:
        kind = _top_count_key(descriptor_code_kinds.get(code, {}))
        if kind:
            state_unit_code_kinds[code] = kind
            state_kind_counts[kind] = state_kind_counts.get(kind, 0) + state_unit_codes.get(code, 0)

    state_visible_object_kinds = sorted(state_kind_counts)
    descriptor_only_object_kinds = sorted(set(descriptor_kinds) - set(state_kind_counts))

    assessment = "insufficient descriptor/state overlap for correlation"
    if descriptor_unit_codes and state_unit_codes:
        assessment_parts = ["state blocks reuse a subset of descriptor unit codes in this sample"]
        highlighted_kinds = [kind for kind in ("person", "scrap") if kind in descriptor_only_object_kinds]
        if highlighted_kinds:
            if len(descriptor_only_object_kinds) > len(highlighted_kinds):
                assessment_parts.append(
                    f"{', '.join(highlighted_kinds)} and some other roster entries remain descriptor-only"
                )
            else:
                assessment_parts.append(f"{' and '.join(highlighted_kinds)} remain descriptor-only")
        elif descriptor_only_unit_codes:
            assessment_parts.append("some descriptor roster codes remain outside the observed state stream")
        elif state_only_unit_codes:
            assessment_parts.append("some state codes have not yet been seen in descriptor packets")
        assessment = "; ".join(assessment_parts)

    return {
        "descriptorUnitCodes": _sorted_count_map(descriptor_unit_codes),
        "stateUnitCodes": _sorted_count_map(state_unit_codes),
        "sharedUnitCodes": shared_unit_codes,
        "descriptorOnlyUnitCodes": descriptor_only_unit_codes,
        "stateOnlyUnitCodes": state_only_unit_codes,
        "descriptorKinds": _sorted_count_map(descriptor_kinds),
        "stateKindCounts": _sorted_count_map(state_kind_counts),
        "stateUnitCodeKinds": state_unit_code_kinds,
        "stateVisibleObjectKinds": state_visible_object_kinds,
        "descriptorOnlyObjectKinds": descriptor_only_object_kinds,
        "assessment": assessment,
    }


def summarize_descriptor_followups(history: list[dict[str, Any]], lookahead_packets: int = 12) -> dict[str, Any]:
    descriptor_entries: list[tuple[int, dict[str, Any]]] = []
    for idx, entry in enumerate(history):
        if entry.get("channel") != "udp":
            continue
        decoder = entry.get("decoder") or {}
        if decoder.get("family") != "peer_object_descriptor":
            continue
        object_kind = decoder.get("objectKind")
        if not isinstance(object_kind, str) or not object_kind:
            continue
        descriptor_entries.append((idx, decoder))

    per_kind: dict[str, dict[str, Any]] = {}
    for idx, decoder in descriptor_entries:
        object_kind = str(decoder.get("objectKind"))
        summary = per_kind.setdefault(
            object_kind,
            {
                "descriptorCount": 0,
                "firstNonDescriptorEvents": {},
                "firstShortObjectEvents": {},
                "descriptorsWithoutShortObjectFollowup": 0,
            },
        )
        summary["descriptorCount"] += 1

        first_non_descriptor: str | None = None
        first_short_object: str | None = None
        for next_idx in range(idx + 1, min(len(history), idx + 1 + lookahead_packets)):
            next_entry = history[next_idx]
            if next_entry.get("channel") != "udp":
                continue
            next_decoder = next_entry.get("decoder") or {}
            next_family = next_decoder.get("family")
            if next_family == "peer_object_descriptor":
                continue
            if not isinstance(next_family, str) or not next_family.startswith("peer_"):
                continue

            if first_non_descriptor is None:
                first_non_descriptor = (
                    next_decoder.get("objectSetupEvent")
                    or next_decoder.get("controlEvent")
                    or next_family
                )
                if isinstance(first_non_descriptor, str) and first_non_descriptor:
                    bucket = summary["firstNonDescriptorEvents"]
                    bucket[first_non_descriptor] = bucket.get(first_non_descriptor, 0) + 1

            if next_family in {
                "peer_object_presence",
                "peer_object_counter",
                "peer_object_control",
                "peer_object_setup_marker",
                "peer_object_batch",
            }:
                first_short_object = next_decoder.get("objectSetupEvent") or next_family
                if isinstance(first_short_object, str) and first_short_object:
                    bucket = summary["firstShortObjectEvents"]
                    bucket[first_short_object] = bucket.get(first_short_object, 0) + 1
                break

        if first_short_object is None:
            summary["descriptorsWithoutShortObjectFollowup"] += 1

    ordered: dict[str, Any] = {}
    for object_kind, summary in sorted(per_kind.items()):
        descriptor_count = summary["descriptorCount"]
        first_non_descriptor_events = _sorted_count_map(summary["firstNonDescriptorEvents"])
        first_short_object_events = _sorted_count_map(summary["firstShortObjectEvents"])
        descriptors_without_short = summary["descriptorsWithoutShortObjectFollowup"]
        descriptors_with_short = descriptor_count - descriptors_without_short

        assessment = "insufficient descriptor follow-up data"
        top_non_descriptor = _top_count_key(first_non_descriptor_events)
        if descriptors_with_short and descriptors_without_short:
            assessment = "some descriptors reach short object/setup followups while others hand off directly into broader peer traffic"
        elif descriptors_with_short:
            assessment = "descriptors often transition into short object/setup followup packets"
        elif top_non_descriptor == "peer_state_block":
            assessment = "descriptors usually hand off directly into state traffic without a decoded short object/setup followup"
        elif top_non_descriptor:
            assessment = f"descriptors usually hand off into {top_non_descriptor} without a decoded short object/setup followup"

        ordered[object_kind] = {
            "descriptorCount": descriptor_count,
            "firstNonDescriptorEvents": first_non_descriptor_events,
            "firstShortObjectEvents": first_short_object_events,
            "descriptorsWithShortObjectFollowup": descriptors_with_short,
            "descriptorsWithoutShortObjectFollowup": descriptors_without_short,
            "assessment": assessment,
        }

    return {
        "lookaheadPackets": lookahead_packets,
        "objectKinds": ordered,
    }


def summarize_unresolved_control_traffic(history: list[dict[str, Any]]) -> dict[str, Any]:
    po_signatures: dict[str, dict[str, Any]] = {}
    object_control_signatures: dict[str, dict[str, Any]] = {}
    context_window = 4
    scrap_control_words_by_name: dict[str, set[str]] = {}

    for entry in history:
        if entry.get("channel") != "udp":
            continue
        decoder = entry.get("decoder") or {}
        if decoder.get("family") != "peer_object_descriptor" or decoder.get("objectKind") != "scrap":
            continue
        object_name = decoder.get("primaryObjectName")
        descriptor_sketch = decoder.get("descriptorSketch")
        if not isinstance(object_name, str) or not object_name or not isinstance(descriptor_sketch, dict):
            continue
        control_word = descriptor_sketch.get("controlWord")
        if isinstance(control_word, str) and control_word:
            scrap_control_words_by_name.setdefault(object_name, set()).add(control_word)

    mutable_scrap_names = {
        name for name, values in scrap_control_words_by_name.items() if len(values) > 1
    }

    def _next_scrap_descriptor(index: int) -> tuple[str | None, str | None, str | None, int | None, str | None]:
        source_log = history[index].get("sourceLog")
        for neighbor_index in range(index + 1, min(len(history), index + context_window + 3)):
            if source_log and history[neighbor_index].get("sourceLog") != source_log:
                continue
            neighbor_decoder = history[neighbor_index].get("decoder") or {}
            if neighbor_decoder.get("family") != "peer_object_descriptor" or neighbor_decoder.get("objectKind") != "scrap":
                continue
            object_name = neighbor_decoder.get("primaryObjectName") if isinstance(neighbor_decoder.get("primaryObjectName"), str) else None
            object_ordinal = neighbor_decoder.get("objectOrdinal") if isinstance(neighbor_decoder.get("objectOrdinal"), int) else None
            descriptor_sketch = neighbor_decoder.get("descriptorSketch")
            control_half0 = None
            param_half1 = None
            subtype_word = None
            if isinstance(descriptor_sketch, dict):
                control_word = descriptor_sketch.get("controlWord")
                param_word = descriptor_sketch.get("paramWord")
                subtype_value = descriptor_sketch.get("subtypeWord")
                control_value = _parse_hex_word(control_word) if isinstance(control_word, str) else None
                param_value = _parse_hex_word(param_word) if isinstance(param_word, str) else None
                if control_value is not None:
                    control_half0 = f"0x{(control_value >> 16) & 0xFFFF:04x}"
                if param_value is not None:
                    param_half1 = f"0x{param_value & 0xFFFF:04x}"
                if isinstance(subtype_value, str) and subtype_value:
                    subtype_word = subtype_value
            return object_name, control_half0, param_half1, object_ordinal, subtype_word
        return None, None, None, None, None

    def _collect_local_context(index: int) -> tuple[dict[str, int], dict[str, int], dict[str, int], dict[str, int], dict[str, int], dict[str, int], dict[str, int], dict[str, int]]:
        descriptor_kinds: dict[str, int] = {}
        object_events: dict[str, int] = {}
        object_names: dict[str, int] = {}
        payload_families: dict[str, int] = {}
        scrap_control_half0: dict[str, int] = {}
        scrap_param_half1: dict[str, int] = {}
        mutable_scrap_names_nearby: dict[str, int] = {}
        mutable_scrap_control_half0: dict[str, int] = {}
        source_log = history[index].get("sourceLog")
        for neighbor_index in range(max(0, index - context_window), min(len(history), index + context_window + 1)):
            if neighbor_index == index:
                continue
            if source_log and history[neighbor_index].get("sourceLog") != source_log:
                continue
            neighbor_decoder = history[neighbor_index].get("decoder") or {}
            if neighbor_decoder.get("family") == "peer_object_descriptor":
                object_kind = neighbor_decoder.get("objectKind")
                if isinstance(object_kind, str) and object_kind:
                    descriptor_kinds[object_kind] = descriptor_kinds.get(object_kind, 0) + 1
                object_name = neighbor_decoder.get("primaryObjectName")
                if isinstance(object_name, str) and object_name:
                    object_names[object_name] = object_names.get(object_name, 0) + 1
                payload_family = neighbor_decoder.get("descriptorPayloadFamilyHint")
                if isinstance(payload_family, str) and payload_family:
                    payload_families[payload_family] = payload_families.get(payload_family, 0) + 1
                if object_kind == "scrap":
                    descriptor_sketch = neighbor_decoder.get("descriptorSketch")
                    if isinstance(descriptor_sketch, dict):
                        control_word = descriptor_sketch.get("controlWord")
                        param_word = descriptor_sketch.get("paramWord")
                        control_value = _parse_hex_word(control_word) if isinstance(control_word, str) else None
                        param_value = _parse_hex_word(param_word) if isinstance(param_word, str) else None
                        if control_value is not None:
                            key = f"0x{(control_value >> 16) & 0xFFFF:04x}"
                            scrap_control_half0[key] = scrap_control_half0.get(key, 0) + 1
                            if object_name in mutable_scrap_names:
                                mutable_scrap_control_half0[key] = mutable_scrap_control_half0.get(key, 0) + 1
                        if param_value is not None:
                            key = f"0x{param_value & 0xFFFF:04x}"
                            scrap_param_half1[key] = scrap_param_half1.get(key, 0) + 1
                    if isinstance(object_name, str) and object_name in mutable_scrap_names:
                        mutable_scrap_names_nearby[object_name] = mutable_scrap_names_nearby.get(object_name, 0) + 1
            object_event = neighbor_decoder.get("objectSetupEvent")
            if isinstance(object_event, str) and object_event:
                object_events[object_event] = object_events.get(object_event, 0) + 1
        return (
            descriptor_kinds,
            object_events,
            object_names,
            payload_families,
            scrap_control_half0,
            scrap_param_half1,
            mutable_scrap_names_nearby,
            mutable_scrap_control_half0,
        )

    for idx, entry in enumerate(history):
        if entry.get("channel") != "udp":
            continue
        decoder = entry.get("decoder") or {}
        family = decoder.get("family")
        if family == "peer_control_marker" and decoder.get("controlEvent") == "peer_control_unknown_po":
            timing_fields = decoder.get("timingFields")
            if not isinstance(timing_fields, dict):
                continue
            mode_word = timing_fields.get("modeWord")
            lane_value = timing_fields.get("laneValue")
            signature_key = f"{mode_word}/lane{lane_value}" if mode_word is not None and lane_value is not None else "unknown"
            summary = po_signatures.setdefault(
                signature_key,
                {
                    "count": 0,
                    "modeWord": mode_word,
                    "laneValue": lane_value,
                    "sequenceValues": set(),
                    "_clockValues": [],
                    "_nearbyDescriptorKinds": {},
                    "_nearbyObjectEvents": {},
                    "_nearbyObjectNames": {},
                    "_nearbyPayloadFamilies": {},
                    "_nearbyScrapControlHalf0": {},
                    "_nearbyScrapParamHalf1": {},
                    "_nearbyMutableScrapNames": {},
                    "_nearbyMutableScrapControlHalf0": {},
                    "_sequenceNextDescriptorNames": {},
                    "_sequenceNextScrapControlHalf0": {},
                    "_sequenceNextScrapParamHalf1": {},
                    "_sequenceNextScrapOrdinals": {},
                    "_sequenceNextScrapSubtypeWords": {},
                },
            )
            summary["count"] += 1
            sequence_value = timing_fields.get("sequenceValue")
            if isinstance(sequence_value, int):
                summary["sequenceValues"].add(sequence_value)
            clock_seconds = timing_fields.get("clockSeconds")
            if isinstance(clock_seconds, (int, float)):
                summary["_clockValues"].append(float(clock_seconds))
            (
                nearby_descriptor_kinds,
                nearby_object_events,
                nearby_object_names,
                nearby_payload_families,
                nearby_scrap_control_half0,
                nearby_scrap_param_half1,
                nearby_mutable_scrap_names,
                nearby_mutable_scrap_control_half0,
            ) = _collect_local_context(idx)
            for key, value in nearby_descriptor_kinds.items():
                bucket = summary["_nearbyDescriptorKinds"]
                bucket[key] = bucket.get(key, 0) + value
            for key, value in nearby_object_events.items():
                bucket = summary["_nearbyObjectEvents"]
                bucket[key] = bucket.get(key, 0) + value
            for key, value in nearby_object_names.items():
                bucket = summary["_nearbyObjectNames"]
                bucket[key] = bucket.get(key, 0) + value
            for key, value in nearby_payload_families.items():
                bucket = summary["_nearbyPayloadFamilies"]
                bucket[key] = bucket.get(key, 0) + value
            for key, value in nearby_scrap_control_half0.items():
                bucket = summary["_nearbyScrapControlHalf0"]
                bucket[key] = bucket.get(key, 0) + value
            for key, value in nearby_scrap_param_half1.items():
                bucket = summary["_nearbyScrapParamHalf1"]
                bucket[key] = bucket.get(key, 0) + value
            for key, value in nearby_mutable_scrap_names.items():
                bucket = summary["_nearbyMutableScrapNames"]
                bucket[key] = bucket.get(key, 0) + value
            for key, value in nearby_mutable_scrap_control_half0.items():
                bucket = summary["_nearbyMutableScrapControlHalf0"]
                bucket[key] = bucket.get(key, 0) + value
            if isinstance(sequence_value, int):
                next_object_name, next_control_half0, next_param_half1, next_ordinal, next_subtype_word = _next_scrap_descriptor(idx)
                if isinstance(next_object_name, str) and next_object_name:
                    bucket = summary["_sequenceNextDescriptorNames"].setdefault(sequence_value, {})
                    bucket[next_object_name] = bucket.get(next_object_name, 0) + 1
                if isinstance(next_control_half0, str) and next_control_half0:
                    bucket = summary["_sequenceNextScrapControlHalf0"].setdefault(sequence_value, {})
                    bucket[next_control_half0] = bucket.get(next_control_half0, 0) + 1
                if isinstance(next_param_half1, str) and next_param_half1:
                    bucket = summary["_sequenceNextScrapParamHalf1"].setdefault(sequence_value, {})
                    bucket[next_param_half1] = bucket.get(next_param_half1, 0) + 1
                if isinstance(next_ordinal, int):
                    bucket = summary["_sequenceNextScrapOrdinals"].setdefault(sequence_value, set())
                    bucket.add(next_ordinal)
                if isinstance(next_subtype_word, str) and next_subtype_word:
                    bucket = summary["_sequenceNextScrapSubtypeWords"].setdefault(sequence_value, {})
                    bucket[next_subtype_word] = bucket.get(next_subtype_word, 0) + 1
        elif family == "peer_object_control":
            signature = decoder.get("controlBodySignature")
            if not isinstance(signature, str) or not signature:
                signature = "unknown"
            summary = object_control_signatures.setdefault(
                signature,
                {
                    "count": 0,
                    "controlBodyBytes": decoder.get("controlBodyBytes"),
                    "controlModeWord": decoder.get("controlModeWord"),
                    "controlTailWord": decoder.get("controlTailWord"),
                    "_nearbyDescriptorKinds": {},
                    "_nearbyObjectEvents": {},
                    "_nearbyObjectNames": {},
                    "_nearbyPayloadFamilies": {},
                    "_nearbyScrapControlHalf0": {},
                    "_nearbyScrapParamHalf1": {},
                    "_nearbyMutableScrapNames": {},
                    "_nearbyMutableScrapControlHalf0": {},
                },
            )
            summary["count"] += 1
            (
                nearby_descriptor_kinds,
                nearby_object_events,
                nearby_object_names,
                nearby_payload_families,
                nearby_scrap_control_half0,
                nearby_scrap_param_half1,
                nearby_mutable_scrap_names,
                nearby_mutable_scrap_control_half0,
            ) = _collect_local_context(idx)
            for key, value in nearby_descriptor_kinds.items():
                bucket = summary["_nearbyDescriptorKinds"]
                bucket[key] = bucket.get(key, 0) + value
            for key, value in nearby_object_events.items():
                bucket = summary["_nearbyObjectEvents"]
                bucket[key] = bucket.get(key, 0) + value
            for key, value in nearby_object_names.items():
                bucket = summary["_nearbyObjectNames"]
                bucket[key] = bucket.get(key, 0) + value
            for key, value in nearby_payload_families.items():
                bucket = summary["_nearbyPayloadFamilies"]
                bucket[key] = bucket.get(key, 0) + value
            for key, value in nearby_scrap_control_half0.items():
                bucket = summary["_nearbyScrapControlHalf0"]
                bucket[key] = bucket.get(key, 0) + value
            for key, value in nearby_scrap_param_half1.items():
                bucket = summary["_nearbyScrapParamHalf1"]
                bucket[key] = bucket.get(key, 0) + value
            for key, value in nearby_mutable_scrap_names.items():
                bucket = summary["_nearbyMutableScrapNames"]
                bucket[key] = bucket.get(key, 0) + value
            for key, value in nearby_mutable_scrap_control_half0.items():
                bucket = summary["_nearbyMutableScrapControlHalf0"]
                bucket[key] = bucket.get(key, 0) + value

    ordered_po_signatures: dict[str, Any] = {}
    for key, value in sorted(po_signatures.items(), key=lambda item: (-item[1]["count"], item[0])):
        clock_values = value.pop("_clockValues")
        nearby_descriptor_kinds = value.pop("_nearbyDescriptorKinds")
        nearby_object_events = value.pop("_nearbyObjectEvents")
        nearby_object_names = value.pop("_nearbyObjectNames")
        nearby_payload_families = value.pop("_nearbyPayloadFamilies")
        nearby_scrap_control_half0 = value.pop("_nearbyScrapControlHalf0")
        nearby_scrap_param_half1 = value.pop("_nearbyScrapParamHalf1")
        nearby_mutable_scrap_names = value.pop("_nearbyMutableScrapNames")
        nearby_mutable_scrap_control_half0 = value.pop("_nearbyMutableScrapControlHalf0")
        sequence_next_descriptor_names = value.pop("_sequenceNextDescriptorNames")
        sequence_next_scrap_control_half0 = value.pop("_sequenceNextScrapControlHalf0")
        sequence_next_scrap_param_half1 = value.pop("_sequenceNextScrapParamHalf1")
        sequence_next_scrap_ordinals = value.pop("_sequenceNextScrapOrdinals")
        sequence_next_scrap_subtype_words = value.pop("_sequenceNextScrapSubtypeWords")
        subtype_to_control_half0: dict[str, dict[str, int]] = {}
        subtype_to_param_half1: dict[str, dict[str, int]] = {}
        for sequence_value in sorted(
            set(sequence_next_scrap_subtype_words) | set(sequence_next_scrap_control_half0) | set(sequence_next_scrap_param_half1)
        ):
            subtype_counts = sequence_next_scrap_subtype_words.get(sequence_value, {})
            control_counts = sequence_next_scrap_control_half0.get(sequence_value, {})
            param_counts = sequence_next_scrap_param_half1.get(sequence_value, {})
            if len(subtype_counts) != 1:
                continue
            subtype_word = next(iter(subtype_counts))
            if len(control_counts) == 1:
                control_half0 = next(iter(control_counts))
                bucket = subtype_to_control_half0.setdefault(subtype_word, {})
                bucket[control_half0] = bucket.get(control_half0, 0) + 1
            if len(param_counts) == 1:
                param_half1 = next(iter(param_counts))
                bucket = subtype_to_param_half1.setdefault(subtype_word, {})
                bucket[param_half1] = bucket.get(param_half1, 0) + 1
        mutable_scrap_assessment = "no mutable scrap overlap observed"
        if nearby_mutable_scrap_names:
            highlighted_mutable = ", ".join(list(_sorted_count_map(nearby_mutable_scrap_names).keys())[:3])
            if len(nearby_mutable_scrap_control_half0) == 1:
                highlighted_half0 = next(iter(_sorted_count_map(nearby_mutable_scrap_control_half0)))
                mutable_scrap_assessment = f"overlaps mutable scrap via {highlighted_mutable}; observed mutable scrap controlHalf0 is {highlighted_half0}"
            else:
                mutable_scrap_assessment = f"overlaps mutable scrap via {highlighted_mutable}"
        sequence_pattern_assessment = "no next-scrap sequence pattern observed"
        sequence_halfword_assessment = "no subtype-linked halfword pattern observed"
        ordered_sequence_ordinals: list[int] = []
        ordered_sequence_subtype_values: list[int] = []
        for sequence_value in sorted(sequence_next_scrap_ordinals):
            ordinals_for_sequence = sequence_next_scrap_ordinals.get(sequence_value, set())
            if len(ordinals_for_sequence) == 1:
                ordered_sequence_ordinals.append(next(iter(ordinals_for_sequence)))
        for sequence_value in sorted(sequence_next_scrap_subtype_words):
            subtype_counts = sequence_next_scrap_subtype_words.get(sequence_value, {})
            if len(subtype_counts) == 1:
                subtype_word = next(iter(subtype_counts))
                subtype_value = _parse_hex_word(subtype_word)
                if subtype_value is not None:
                    ordered_sequence_subtype_values.append(subtype_value)
        subtype_stride = None
        if len(ordered_sequence_subtype_values) >= 2:
            subtype_deltas = [
                right - left for left, right in zip(ordered_sequence_subtype_values, ordered_sequence_subtype_values[1:])
            ]
            if len(set(subtype_deltas)) == 1:
                subtype_stride = subtype_deltas[0]
        if ordered_sequence_ordinals or ordered_sequence_subtype_values:
            sequence_pattern_assessment = "next-scrap sequence pattern is partially decoded"
            if (
                len(ordered_sequence_subtype_values) >= 2
                and subtype_stride == 0x0100
            ):
                sequence_pattern_assessment = "next-scrap subtypeWord steps contiguously while objectOrdinal does not"
            elif len(ordered_sequence_ordinals) >= 2 and ordered_sequence_ordinals == sorted(ordered_sequence_ordinals):
                sequence_pattern_assessment = "next-scrap objectOrdinal steps monotonically"
        if subtype_to_control_half0 or subtype_to_param_half1:
            control_one_to_one = bool(subtype_to_control_half0) and all(len(counts) == 1 for counts in subtype_to_control_half0.values()) and len({
                next(iter(counts)) for counts in subtype_to_control_half0.values()
            }) == len(subtype_to_control_half0)
            param_one_to_one = bool(subtype_to_param_half1) and all(len(counts) == 1 for counts in subtype_to_param_half1.values()) and len({
                next(iter(counts)) for counts in subtype_to_param_half1.values()
            }) == len(subtype_to_param_half1)
            if control_one_to_one and not param_one_to_one:
                sequence_halfword_assessment = "subtypeWord maps one-to-one onto controlHalf0, while paramHalf1 repeats across subtype steps"
            elif control_one_to_one and param_one_to_one:
                sequence_halfword_assessment = "subtypeWord currently maps one-to-one onto both controlHalf0 and paramHalf1"
            else:
                sequence_halfword_assessment = "subtypeWord-to-halfword mapping is partially decoded"
        ordered_po_signatures[key] = {
            "count": value["count"],
            "modeWord": value["modeWord"],
            "laneValue": value["laneValue"],
            "sequenceValues": sorted(value["sequenceValues"]),
            "clockSecondsRange": [
                round(min(clock_values), 3),
                round(max(clock_values), 3),
            ] if clock_values else [],
            "nearbyDescriptorKinds": _sorted_count_map(nearby_descriptor_kinds),
            "nearbyObjectEvents": _sorted_count_map(nearby_object_events),
            "nearbyObjectNames": _sorted_count_map(nearby_object_names),
            "nearbyPayloadFamilies": _sorted_count_map(nearby_payload_families),
            "nearbyScrapControlHalf0": _sorted_count_map(nearby_scrap_control_half0),
            "nearbyScrapParamHalf1": _sorted_count_map(nearby_scrap_param_half1),
            "nearbyMutableScrapNames": _sorted_count_map(nearby_mutable_scrap_names),
            "nearbyMutableScrapControlHalf0": _sorted_count_map(nearby_mutable_scrap_control_half0),
            "mutableScrapAssessment": mutable_scrap_assessment,
            "sequenceNextDescriptorNames": {
                str(sequence_value): _sorted_count_map(counts)
                for sequence_value, counts in sorted(sequence_next_descriptor_names.items())
            },
            "sequenceNextScrapControlHalf0": {
                str(sequence_value): _sorted_count_map(counts)
                for sequence_value, counts in sorted(sequence_next_scrap_control_half0.items())
            },
            "sequenceNextScrapParamHalf1": {
                str(sequence_value): _sorted_count_map(counts)
                for sequence_value, counts in sorted(sequence_next_scrap_param_half1.items())
            },
            "sequenceNextScrapOrdinals": {
                str(sequence_value): sorted(ordinals)
                for sequence_value, ordinals in sorted(sequence_next_scrap_ordinals.items())
            },
            "sequenceNextScrapSubtypeWords": {
                str(sequence_value): _sorted_count_map(counts)
                for sequence_value, counts in sorted(sequence_next_scrap_subtype_words.items())
            },
            "sequencePatternAssessment": sequence_pattern_assessment,
            "subtypeToScrapControlHalf0": {
                subtype_word: _sorted_count_map(counts)
                for subtype_word, counts in sorted(subtype_to_control_half0.items())
            },
            "subtypeToScrapParamHalf1": {
                subtype_word: _sorted_count_map(counts)
                for subtype_word, counts in sorted(subtype_to_param_half1.items())
            },
            "sequenceHalfwordAssessment": sequence_halfword_assessment,
        }

    ordered_object_control_signatures: dict[str, Any] = {}
    for key, value in sorted(object_control_signatures.items(), key=lambda item: (-item[1]["count"], item[0])):
        nearby_descriptor_kinds = value.pop("_nearbyDescriptorKinds")
        nearby_object_events = value.pop("_nearbyObjectEvents")
        nearby_object_names = value.pop("_nearbyObjectNames")
        nearby_payload_families = value.pop("_nearbyPayloadFamilies")
        nearby_scrap_control_half0 = value.pop("_nearbyScrapControlHalf0")
        nearby_scrap_param_half1 = value.pop("_nearbyScrapParamHalf1")
        nearby_mutable_scrap_names = value.pop("_nearbyMutableScrapNames")
        nearby_mutable_scrap_control_half0 = value.pop("_nearbyMutableScrapControlHalf0")
        ordered_object_control_signatures[key] = {
            **value,
            "nearbyDescriptorKinds": _sorted_count_map(nearby_descriptor_kinds),
            "nearbyObjectEvents": _sorted_count_map(nearby_object_events),
            "nearbyObjectNames": _sorted_count_map(nearby_object_names),
            "nearbyPayloadFamilies": _sorted_count_map(nearby_payload_families),
            "nearbyScrapControlHalf0": _sorted_count_map(nearby_scrap_control_half0),
            "nearbyScrapParamHalf1": _sorted_count_map(nearby_scrap_param_half1),
            "nearbyMutableScrapNames": _sorted_count_map(nearby_mutable_scrap_names),
            "nearbyMutableScrapControlHalf0": _sorted_count_map(nearby_mutable_scrap_control_half0),
        }

    po_assessment = "no PO control markers observed"
    if "0x0306/lane0" in ordered_po_signatures and "0x0206/lane2" in ordered_po_signatures:
        po_assessment = "PO mostly runs in a 0x0306/lane0 cadence, with a smaller 0x0206/lane2 burst in the March 21 captures"
        lane2_descriptor_kinds = ordered_po_signatures["0x0206/lane2"].get("nearbyDescriptorKinds", {})
        if set(lane2_descriptor_kinds) == {"scrap"}:
            po_assessment += "; the late 0x0206/lane2 burst is only seen near scrap descriptors in the current windowed context"
        lane2_scrap_names = ordered_po_signatures["0x0206/lane2"].get("nearbyObjectNames", {})
        if lane2_scrap_names:
            highlighted_names = ", ".join(list(lane2_scrap_names.keys())[:5])
            po_assessment += f"; its local scrap neighborhood is led by {highlighted_names}"
        lane2_mutable_scrap_names = ordered_po_signatures["0x0206/lane2"].get("nearbyMutableScrapNames", {})
        if lane2_mutable_scrap_names:
            highlighted_mutable = ", ".join(list(lane2_mutable_scrap_names.keys())[:3])
            po_assessment += f"; it overlaps the currently mutable scrap object set via {highlighted_mutable}"
            lane2_mutable_scrap_control_half0 = ordered_po_signatures["0x0206/lane2"].get("nearbyMutableScrapControlHalf0", {})
            if set(lane2_mutable_scrap_control_half0) == {"0xa980"}:
                po_assessment += "; that mutable overlap is only seen with the mutated 0xa980 halfword, not the earlier 0xaa00 variant"
        lane2_sequence_next_descriptor_names = ordered_po_signatures["0x0206/lane2"].get("sequenceNextDescriptorNames", {})
        if lane2_sequence_next_descriptor_names:
            ordered_pairs = [
                f"{sequence}->{next(iter(names))}"
                for sequence, names in lane2_sequence_next_descriptor_names.items()
                if len(names) == 1
            ]
            if ordered_pairs:
                po_assessment += f"; observed late-burst ordering is {'; '.join(ordered_pairs)}"
        lane2_sequence_next_scrap_subtype_words = ordered_po_signatures["0x0206/lane2"].get("sequenceNextScrapSubtypeWords", {})
        lane2_sequence_next_scrap_ordinals = ordered_po_signatures["0x0206/lane2"].get("sequenceNextScrapOrdinals", {})
        ordered_lane2_subtypes: list[int] = []
        ordered_lane2_ordinals: list[int] = []
        for sequence_value, subtype_counts in lane2_sequence_next_scrap_subtype_words.items():
            if len(subtype_counts) == 1:
                subtype_value = _parse_hex_word(next(iter(subtype_counts)))
                if subtype_value is not None:
                    ordered_lane2_subtypes.append(subtype_value)
        for sequence_value, ordinals in lane2_sequence_next_scrap_ordinals.items():
            if len(ordinals) == 1:
                ordered_lane2_ordinals.append(ordinals[0])
        lane2_subtype_stride = None
        if len(ordered_lane2_subtypes) >= 2:
            subtype_deltas = [right - left for left, right in zip(ordered_lane2_subtypes, ordered_lane2_subtypes[1:])]
            if len(set(subtype_deltas)) == 1:
                lane2_subtype_stride = subtype_deltas[0]
        if len(ordered_lane2_subtypes) >= 2 and lane2_subtype_stride == 0x0100:
            subtype_start = f"0x{ordered_lane2_subtypes[0]:04x}"
            subtype_end = f"0x{ordered_lane2_subtypes[-1]:04x}"
            po_assessment += f"; the paired scrap subtypeWord values step contiguously from {subtype_start} through {subtype_end}"
            if len(ordered_lane2_ordinals) >= 2 and ordered_lane2_ordinals != sorted(ordered_lane2_ordinals):
                ordinal_text = ", ".join(str(ordinal) for ordinal in ordered_lane2_ordinals)
                po_assessment += f", while the linked scrap ordinals jump {ordinal_text}"
        lane2_halfword_assessment = ordered_po_signatures["0x0206/lane2"].get("sequenceHalfwordAssessment")
        if lane2_halfword_assessment == "subtypeWord maps one-to-one onto controlHalf0, while paramHalf1 repeats across subtype steps":
            po_assessment += "; across that subtype ladder, controlHalf0 stays subtype-specific while paramHalf1 repeats across more than one subtype step"
    elif ordered_po_signatures:
        po_assessment = "PO exposes a stable float-like clock plus small mode/lane words, but only one observed signature family"

    object_control_assessment = "no generic peer_object_control packets observed"
    if len(ordered_object_control_signatures) > 1:
        object_control_assessment = "peer_object_control currently splits across a small set of repeated short-packet signatures"
        if any(
            signature_info.get("nearbyObjectEvents", {}).get("object_setup_marker")
            for signature_info in ordered_object_control_signatures.values()
        ):
            object_control_assessment += "; the 0x0606 signatures appear adjacent to object_setup_marker packets"
    elif ordered_object_control_signatures:
        object_control_assessment = "peer_object_control currently resolves to a single repeated short-packet signature"

    return {
        "poControlSignatures": ordered_po_signatures,
        "poAssessment": po_assessment,
        "objectControlSignatures": ordered_object_control_signatures,
        "objectControlAssessment": object_control_assessment,
    }


def summarize_descriptor_payload_families(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    families: dict[str, dict[str, Any]] = {}
    for idx, entry in enumerate(history):
        if entry.get("channel") != "udp":
            continue
        decoder = entry.get("decoder") or {}
        if decoder.get("family") != "peer_object_descriptor":
            continue

        payload_family = decoder.get("descriptorPayloadFamilyHint")
        if not isinstance(payload_family, str) or not payload_family:
            continue

        summary = families.setdefault(
            payload_family,
            {
                "payloadFamily": payload_family,
                "packetCount": 0,
                "descriptorFamilies": {},
                "descriptorArchetypes": {},
                "objectKinds": {},
                "objectNames": set(),
                "controlSignatures": {},
                "objectProfiles": {},
                "payloadWords": {
                    "controlWord": {},
                    "paramWord": {},
                    "vectorWordA": {},
                    "vectorWordB": {},
                },
                "subtypeWordClusters": {},
                "controlWordHalf0Clusters": {},
                "paramWordHalf1Clusters": {},
                "controlWordHalf0VectorClusters": {},
                "paramWordHalf1VectorClusters": {},
                "sampleDescriptors": [],
                "_sampleKeys": set(),
                "_firstIndex": idx,
            },
        )
        summary["packetCount"] += 1

        descriptor_family = decoder.get("descriptorFamilyHint")
        if isinstance(descriptor_family, str) and descriptor_family:
            summary["descriptorFamilies"][descriptor_family] = summary["descriptorFamilies"].get(descriptor_family, 0) + 1
        descriptor_archetype = decoder.get("descriptorArchetypeHint")
        if isinstance(descriptor_archetype, str) and descriptor_archetype:
            summary["descriptorArchetypes"][descriptor_archetype] = summary["descriptorArchetypes"].get(descriptor_archetype, 0) + 1
        object_kind = decoder.get("objectKind")
        if isinstance(object_kind, str) and object_kind:
            summary["objectKinds"][object_kind] = summary["objectKinds"].get(object_kind, 0) + 1
        object_name = decoder.get("primaryObjectName")
        if isinstance(object_name, str) and object_name:
            summary["objectNames"].add(object_name)

        payload_hints = decoder.get("descriptorPayloadHints")
        if isinstance(payload_hints, dict):
            control_signature = payload_hints.get("controlSignature")
            if isinstance(control_signature, str) and control_signature:
                summary["controlSignatures"][control_signature] = summary["controlSignatures"].get(control_signature, 0) + 1

        descriptor_sketch = decoder.get("descriptorSketch")
        if isinstance(descriptor_sketch, dict):
            object_profile: dict[str, Any] | None = None
            object_ordinal = decoder.get("objectOrdinal") if isinstance(decoder.get("objectOrdinal"), int) else None
            if isinstance(object_name, str) and object_name:
                object_profile = summary["objectProfiles"].setdefault(
                    object_name,
                    {
                        "name": object_name,
                        "observationCount": 0,
                        "descriptorFamilies": {},
                        "subtypeWords": {},
                        "indices": [],
                        "observations": [],
                        "payloadWords": {
                            "controlWord": {},
                            "paramWord": {},
                            "vectorWordA": {},
                            "vectorWordB": {},
                        },
                    },
                )
                object_profile["observationCount"] += 1
                object_profile["indices"].append(idx)
                if isinstance(descriptor_family, str) and descriptor_family:
                    object_profile["descriptorFamilies"][descriptor_family] = object_profile["descriptorFamilies"].get(descriptor_family, 0) + 1
            control_word_raw = descriptor_sketch.get("controlWord")
            param_word_raw = descriptor_sketch.get("paramWord")
            control_word_value = _parse_hex_word(control_word_raw) if isinstance(control_word_raw, str) else None
            param_word_value = _parse_hex_word(param_word_raw) if isinstance(param_word_raw, str) else None
            vector_word_a_raw = descriptor_sketch.get("vectorWordA")
            vector_word_b_raw = descriptor_sketch.get("vectorWordB")
            vector_word_a_value = _parse_hex_word(vector_word_a_raw) if isinstance(vector_word_a_raw, str) else None
            vector_word_b_value = _parse_hex_word(vector_word_b_raw) if isinstance(vector_word_b_raw, str) else None
            subtype_word = descriptor_sketch.get("subtypeWord") if isinstance(descriptor_sketch.get("subtypeWord"), str) else None
            control_half0 = f"0x{(control_word_value >> 16) & 0xFFFF:04x}" if control_word_value is not None else None
            param_half0 = f"0x{(param_word_value >> 16) & 0xFFFF:04x}" if param_word_value is not None else None
            param_half1 = f"0x{param_word_value & 0xFFFF:04x}" if param_word_value is not None else None
            vector_a_half1 = f"0x{vector_word_a_value & 0xFFFF:04x}" if vector_word_a_value is not None else None
            vector_b_half0 = f"0x{(vector_word_b_value >> 16) & 0xFFFF:04x}" if vector_word_b_value is not None else None
            if isinstance(subtype_word, str) and subtype_word:
                subtype_summary = summary["subtypeWordClusters"].setdefault(
                    subtype_word,
                    {
                        "count": 0,
                        "objectNames": set(),
                        "ordinals": set(),
                        "controlHalf0": {},
                        "paramHalf1": {},
                    },
                )
                subtype_summary["count"] += 1
                if isinstance(object_name, str) and object_name:
                    subtype_summary["objectNames"].add(object_name)
                if isinstance(object_ordinal, int):
                    subtype_summary["ordinals"].add(object_ordinal)
                if isinstance(control_half0, str) and control_half0:
                    bucket = subtype_summary["controlHalf0"]
                    bucket[control_half0] = bucket.get(control_half0, 0) + 1
                if isinstance(param_half1, str) and param_half1:
                    bucket = subtype_summary["paramHalf1"]
                    bucket[param_half1] = bucket.get(param_half1, 0) + 1
                if isinstance(object_profile, dict):
                    bucket = object_profile["subtypeWords"]
                    bucket[subtype_word] = bucket.get(subtype_word, 0) + 1
            if isinstance(object_profile, dict):
                object_profile["observations"].append(
                    {
                        "index": idx,
                        "time": entry.get("time"),
                        "sourceLog": entry.get("sourceLog"),
                        "lineNumber": entry.get("lineNumber"),
                        "controlWord": control_word_raw,
                        "paramWord": param_word_raw,
                        "subtypeWord": subtype_word,
                        "controlHalf0": control_half0,
                        "paramHalf1": param_half1,
                    }
                )
            if isinstance(control_half0, str):
                _update_halfword_cluster(
                    summary["controlWordHalf0Clusters"],
                    control_half0,
                    object_name=object_name if isinstance(object_name, str) else None,
                    object_ordinal=object_ordinal,
                    paired_half0=param_half0,
                    paired_half1=param_half1,
                )
            if isinstance(param_half1, str):
                _update_halfword_cluster(
                    summary["paramWordHalf1Clusters"],
                    param_half1,
                    object_name=object_name if isinstance(object_name, str) else None,
                    object_ordinal=object_ordinal,
                    paired_half0=control_half0,
                    paired_half1=param_half0,
                )
                _update_vector_alignment_cluster(
                    summary["paramWordHalf1VectorClusters"],
                    param_half1,
                    object_name=object_name if isinstance(object_name, str) else None,
                    object_ordinal=object_ordinal,
                    paired_vector_a_half1=vector_a_half1,
                    paired_vector_b_half0=vector_b_half0,
                )
            if isinstance(control_half0, str):
                _update_vector_alignment_cluster(
                    summary["controlWordHalf0VectorClusters"],
                    control_half0,
                    object_name=object_name if isinstance(object_name, str) else None,
                    object_ordinal=object_ordinal,
                    paired_vector_a_half1=vector_a_half1,
                    paired_vector_b_half0=vector_b_half0,
                )
            for key in ("controlWord", "paramWord", "vectorWordA", "vectorWordB"):
                value = descriptor_sketch.get(key)
                if isinstance(value, str) and value:
                    bucket = summary["payloadWords"][key]
                    bucket[value] = bucket.get(value, 0) + 1
                    if isinstance(object_profile, dict):
                        object_bucket = object_profile["payloadWords"][key]
                        object_bucket[value] = object_bucket.get(value, 0) + 1

            sample_key = (
                decoder.get("primaryObjectName"),
                descriptor_sketch.get("controlWord"),
                descriptor_sketch.get("paramWord"),
                descriptor_sketch.get("vectorWordA"),
                descriptor_sketch.get("vectorWordB"),
            )
            if len(summary["sampleDescriptors"]) < 6 and sample_key not in summary["_sampleKeys"]:
                summary["_sampleKeys"].add(sample_key)
                sample_descriptor = {
                    "name": decoder.get("primaryObjectName"),
                    "descriptorFamilyHint": decoder.get("descriptorFamilyHint"),
                    "objectKind": decoder.get("objectKind"),
                    "controlWord": descriptor_sketch.get("controlWord"),
                    "paramWord": descriptor_sketch.get("paramWord"),
                    "vectorWordA": descriptor_sketch.get("vectorWordA"),
                    "vectorWordB": descriptor_sketch.get("vectorWordB"),
                }
                summary["sampleDescriptors"].append(sample_descriptor)

    ordered: list[dict[str, Any]] = []
    for summary in sorted(families.values(), key=lambda item: item["_firstIndex"]):
        payload_words = {
            "controlWord": _sorted_count_map(summary["payloadWords"]["controlWord"]),
            "paramWord": _sorted_count_map(summary["payloadWords"]["paramWord"]),
            "vectorWordA": _sorted_count_map(summary["payloadWords"]["vectorWordA"]),
            "vectorWordB": _sorted_count_map(summary["payloadWords"]["vectorWordB"]),
        }
        payload_word_byte_profiles = {
            field_name: _summarize_word_byte_profiles(counts)
            for field_name, counts in payload_words.items()
        }
        payload_word_half_profiles = {
            field_name: _summarize_word_half_profiles(counts)
            for field_name, counts in payload_words.items()
        }
        control_word_half0_vector_cluster_hints = _build_vector_alignment_hints(
            summary["controlWordHalf0VectorClusters"],
            len(summary["objectNames"]),
        )
        param_word_half1_vector_cluster_hints = _build_vector_alignment_hints(
            summary["paramWordHalf1VectorClusters"],
            len(summary["objectNames"]),
        )
        subtype_word_clusters_raw = summary["subtypeWordClusters"]
        subtype_word_clusters = {
            key: {
                "count": value["count"],
                "objectNames": sorted(value["objectNames"])[:8],
                "ordinals": sorted(value["ordinals"]),
                "controlHalf0": _sorted_count_map(value["controlHalf0"]),
                "paramHalf1": _sorted_count_map(value["paramHalf1"]),
            }
            for key, value in _sorted_count_map({k: v["count"] for k, v in subtype_word_clusters_raw.items()}).items()
            for value in [subtype_word_clusters_raw[key]]
        }
        reused_subtype_words = sorted(
            key for key, value in subtype_word_clusters_raw.items() if len(value["objectNames"]) > 1
        )
        subtype_word_assessment = "no subtypeWord clustering observed"
        if subtype_word_clusters_raw:
            if reused_subtype_words:
                varying_overlay_subtypes = sorted(
                    key
                    for key in reused_subtype_words
                    if len(subtype_word_clusters_raw[key]["controlHalf0"]) > 1 or len(subtype_word_clusters_raw[key]["paramHalf1"]) > 1
                )
                subtype_word_assessment = "some subtypeWord values span multiple named objects, so subtypeWord looks more like a family/category lane than a unique object id"
                if varying_overlay_subtypes:
                    highlighted = ", ".join(varying_overlay_subtypes[:4])
                    subtype_word_assessment += f"; {highlighted} also carry multiple control/param overlays inside the same subtype family"
            else:
                subtype_word_assessment = "observed subtypeWord values are currently one-to-one with object names in this sample"
        stable_fields: list[str] = []
        varying_fields: list[str] = []
        common_values: dict[str, str] = {}
        for key, counts in payload_words.items():
            if len(counts) == 1:
                stable_fields.append(key)
                common_values[key] = next(iter(counts))
            elif len(counts) > 1:
                varying_fields.append(key)

        payload_assessment = "insufficient descriptor diversity for payload comparison"
        if stable_fields or varying_fields:
            if "controlWord" in stable_fields and "paramWord" in stable_fields and any(
                field in varying_fields for field in ("vectorWordA", "vectorWordB")
            ):
                payload_assessment = "controlWord and paramWord look family-stable while vector words vary across descriptors"
            elif "controlWord" in stable_fields and any(
                field in varying_fields for field in ("paramWord", "vectorWordA", "vectorWordB")
            ):
                payload_assessment = "controlWord looks family-stable while later payload words vary across descriptors"
            elif varying_fields:
                payload_assessment = "multiple payload lanes vary inside this family; compare sampleDescriptors before naming fields"
            else:
                payload_assessment = "all observed payload words are currently stable within this family sample"

        likely_vector_candidates = [
            field_name for field_name in ("vectorWordA", "vectorWordB") if field_name in varying_fields
        ]
        likely_control_candidates = [
            field_name for field_name in ("controlWord", "paramWord") if field_name in stable_fields
        ]
        object_profiles_raw = summary["objectProfiles"]
        object_profiles: list[dict[str, Any]] = []
        mutable_object_mutations: list[dict[str, Any]] = []
        stable_within_object_names: list[str] = []
        varying_across_object_names: list[str] = []
        mutable_within_object_name_fields: list[str] = []
        mutable_fields_by_object_name: dict[str, list[str]] = {}
        if object_profiles_raw:
            for key in ("controlWord", "paramWord", "vectorWordA", "vectorWordB"):
                profile_values = []
                all_single_value = True
                any_multi_value = False
                for profile in object_profiles_raw.values():
                    values = profile["payloadWords"][key]
                    if not values:
                        continue
                    if len(values) != 1:
                        all_single_value = False
                        any_multi_value = True
                    else:
                        profile_values.append(next(iter(values)))
                if all_single_value and profile_values:
                    stable_within_object_names.append(key)
                if len(set(profile_values)) > 1:
                    varying_across_object_names.append(key)
                if any_multi_value:
                    mutable_within_object_name_fields.append(key)

            for object_name, profile in sorted(object_profiles_raw.items())[:8]:
                profile_common_values: dict[str, str] = {}
                mutable_fields: list[str] = []
                payload_word_counts = {
                    "controlWord": _sorted_count_map(profile["payloadWords"]["controlWord"]),
                    "paramWord": _sorted_count_map(profile["payloadWords"]["paramWord"]),
                    "vectorWordA": _sorted_count_map(profile["payloadWords"]["vectorWordA"]),
                    "vectorWordB": _sorted_count_map(profile["payloadWords"]["vectorWordB"]),
                }
                for key, counts in payload_word_counts.items():
                    if len(counts) == 1:
                        profile_common_values[key] = next(iter(counts))
                    elif len(counts) > 1:
                        mutable_fields.append(key)
                object_profiles.append(
                    {
                        "name": object_name,
                        "observationCount": profile["observationCount"],
                        "descriptorFamilies": _sorted_count_map(profile["descriptorFamilies"]),
                        "subtypeWords": _sorted_count_map(profile["subtypeWords"]),
                        "commonPayloadValues": profile_common_values,
                        "mutableFields": mutable_fields,
                    }
                )
                if mutable_fields:
                    mutable_fields_by_object_name[object_name] = mutable_fields
                    mutation_fields: dict[str, Any] = {}
                    for key in mutable_fields:
                        mutation_summary = _summarize_mutating_word(profile["payloadWords"][key])
                        if mutation_summary:
                            mutation_fields[key] = mutation_summary
                    mutable_object_mutations.append(
                        {
                            "name": object_name,
                            "observationCount": profile["observationCount"],
                            "mutableFields": mutable_fields,
                            "fieldMutations": mutation_fields,
                        }
                    )

        object_specific_payload_fields = [
            field_name for field_name in stable_within_object_names if field_name in varying_across_object_names
        ]
        object_stability_assessment = "insufficient repeated object names for per-object stability comparison"
        if object_profiles:
            if object_specific_payload_fields and mutable_within_object_name_fields:
                object_stability_assessment = "some payload words are stable within each named object while other lanes mutate inside at least one repeated object name"
            elif object_specific_payload_fields:
                object_stability_assessment = "payload words are stable within repeated object names and vary across different named objects"
            elif mutable_within_object_name_fields:
                object_stability_assessment = "some payload words mutate within repeated observations of the same named object"
            else:
                object_stability_assessment = "repeated object names currently show the same payload words across observations"

        subtype_mutation_correlations: dict[str, dict[str, Any]] = {}
        mutable_subtype_words: list[str] = []
        for subtype_word, subtype_info in subtype_word_clusters_raw.items():
            object_names = sorted(subtype_info["objectNames"])
            mutable_object_names = sorted(
                object_name for object_name in object_names if object_name in mutable_fields_by_object_name
            )
            if mutable_object_names:
                mutable_subtype_words.append(subtype_word)
            mutable_field_names: set[str] = set()
            mutable_control_half0: dict[str, int] = {}
            mutable_param_half1: dict[str, int] = {}
            for object_name in mutable_object_names:
                for field_name in mutable_fields_by_object_name.get(object_name, []):
                    mutable_field_names.add(field_name)
                profile = object_profiles_raw.get(object_name)
                if not isinstance(profile, dict):
                    continue
                control_counts = profile["payloadWords"].get("controlWord", {})
                param_counts = profile["payloadWords"].get("paramWord", {})
                for control_word, count in control_counts.items():
                    control_value = _parse_hex_word(control_word)
                    if control_value is None:
                        continue
                    key = f"0x{(control_value >> 16) & 0xFFFF:04x}"
                    mutable_control_half0[key] = mutable_control_half0.get(key, 0) + count
                for param_word, count in param_counts.items():
                    param_value = _parse_hex_word(param_word)
                    if param_value is None:
                        continue
                    key = f"0x{param_value & 0xFFFF:04x}"
                    mutable_param_half1[key] = mutable_param_half1.get(key, 0) + count
            subtype_mutation_correlations[subtype_word] = {
                "objectNames": object_names,
                "mutableObjectNames": mutable_object_names,
                "mutableFields": sorted(mutable_field_names),
                "controlHalf0": _sorted_count_map(subtype_info["controlHalf0"]),
                "paramHalf1": _sorted_count_map(subtype_info["paramHalf1"]),
                "mutableControlHalf0": _sorted_count_map(mutable_control_half0),
                "mutableParamHalf1": _sorted_count_map(mutable_param_half1),
            }
        mutable_subtype_words = sorted(mutable_subtype_words)
        subtype_mutation_assessment = "no mutable subtype families observed"
        if mutable_subtype_words:
            if (
                len(mutable_subtype_words) == 1
                and len(subtype_mutation_correlations[mutable_subtype_words[0]]["mutableObjectNames"]) == 1
                and len(subtype_mutation_correlations[mutable_subtype_words[0]]["objectNames"]) > 1
            ):
                subtype_word = mutable_subtype_words[0]
                mutable_name = subtype_mutation_correlations[subtype_word]["mutableObjectNames"][0]
                subtype_mutation_assessment = (
                    f"{subtype_word} is currently the only subtype family with an observed mutable object; "
                    f"the mutation still looks object-specific to {mutable_name} inside that subtype family"
                )
            else:
                highlighted = ", ".join(mutable_subtype_words[:4])
                subtype_mutation_assessment = (
                    f"mutable objects currently appear inside subtype families {highlighted}; compare mutableObjectNames before treating mutation as subtype-wide"
                )

        def _find_neighbor_index(index: int, direction: int) -> int | None:
            source_log = history[index].get("sourceLog")
            neighbor_index = index + direction
            while 0 <= neighbor_index < len(history):
                neighbor_entry = history[neighbor_index]
                if neighbor_entry.get("channel") != "udp":
                    neighbor_index += direction
                    continue
                if source_log and neighbor_entry.get("sourceLog") != source_log:
                    neighbor_index += direction
                    continue
                return neighbor_index
            return None

        def _label_event(decoder: dict[str, Any]) -> str:
            family = decoder.get("family")
            if family == "peer_control_marker":
                control_event = decoder.get("controlEvent")
                if isinstance(control_event, str) and control_event:
                    return control_event
            if family == "peer_object_descriptor":
                object_name = decoder.get("primaryObjectName")
                if isinstance(object_name, str) and object_name:
                    return object_name
            object_event = decoder.get("objectSetupEvent")
            if isinstance(object_event, str) and object_event:
                return object_event
            if isinstance(family, str) and family:
                return family
            return "unknown"

        def _label_short_control(decoder: dict[str, Any]) -> str | None:
            family = decoder.get("family")
            if family == "peer_control_marker":
                control_event = decoder.get("controlEvent")
                if isinstance(control_event, str) and control_event:
                    return control_event
                return "peer_control_marker"
            if family == "peer_object_control":
                return "peer_object_control"
            return None

        mutable_object_observation_contexts: dict[str, list[dict[str, Any]]] = {}
        mutable_object_transition_assessment = "no mutable object transition context observed"
        mutable_object_state_timelines: dict[str, dict[str, Any]] = {}
        mutable_object_state_timeline_assessment = "no mutable object state timeline observed"
        object_observation_context_signatures: dict[str, dict[str, int]] = {}
        po_sandwiched_objects: list[str] = []
        po_leading_edge_objects: list[str] = []
        repeated_object_context_assessment = "no repeated object observation context pattern observed"

        def _classify_observation_context(prev_short_control: str | None, next_short_control: str | None) -> str:
            if prev_short_control == "peer_control_unknown_po" and next_short_control == "peer_control_unknown_po":
                return "po_sandwiched"
            if prev_short_control == "peer_control_unknown_po" and next_short_control is None:
                return "po_leading_edge"
            if prev_short_control is None and next_short_control == "peer_control_unknown_po":
                return "po_trailing_edge"
            if prev_short_control is None and next_short_control is None:
                return "no_short_control"
            return "mixed_short_control"

        for object_name, profile in sorted(object_profiles_raw.items()):
            profile = object_profiles_raw.get(object_name)
            if not isinstance(profile, dict):
                continue
            contexts: list[dict[str, Any]] = []
            context_signature_counts: dict[str, int] = {}
            for observation_index in profile.get("indices", []):
                if not isinstance(observation_index, int):
                    continue
                decoder = history[observation_index].get("decoder") or {}
                descriptor_sketch = decoder.get("descriptorSketch")
                if not isinstance(descriptor_sketch, dict):
                    continue
                control_word = descriptor_sketch.get("controlWord")
                param_word = descriptor_sketch.get("paramWord")
                control_value = _parse_hex_word(control_word) if isinstance(control_word, str) else None
                param_value = _parse_hex_word(param_word) if isinstance(param_word, str) else None
                control_half0 = f"0x{(control_value >> 16) & 0xFFFF:04x}" if control_value is not None else None
                param_half1 = f"0x{param_value & 0xFFFF:04x}" if param_value is not None else None
                prev_index = _find_neighbor_index(observation_index, -1)
                next_index = _find_neighbor_index(observation_index, 1)
                prev_event = _label_event(history[prev_index].get("decoder") or {}) if prev_index is not None else None
                next_event = _label_event(history[next_index].get("decoder") or {}) if next_index is not None else None
                prev_short_control = _label_short_control(history[prev_index].get("decoder") or {}) if prev_index is not None else None
                next_short_control = _label_short_control(history[next_index].get("decoder") or {}) if next_index is not None else None
                context_signature = _classify_observation_context(prev_short_control, next_short_control)
                context_signature_counts[context_signature] = context_signature_counts.get(context_signature, 0) + 1
                contexts.append(
                    {
                        "controlHalf0": control_half0,
                        "paramHalf1": param_half1,
                        "subtypeWord": descriptor_sketch.get("subtypeWord"),
                        "immediatePrevEvent": prev_event,
                        "immediateNextEvent": next_event,
                        "immediatePrevShortControl": prev_short_control,
                        "immediateNextShortControl": next_short_control,
                        "contextSignature": context_signature,
                    }
                )
            if contexts:
                object_observation_context_signatures[object_name] = _sorted_count_map(context_signature_counts)
                if context_signature_counts.get("po_sandwiched"):
                    po_sandwiched_objects.append(object_name)
                if context_signature_counts.get("po_leading_edge"):
                    po_leading_edge_objects.append(object_name)
                if object_name in mutable_fields_by_object_name:
                    mutable_object_observation_contexts[object_name] = contexts

        for object_name, contexts in mutable_object_observation_contexts.items():
            bracketed_half0: dict[str, int] = {}
            unbracketed_half0: dict[str, int] = {}
            for context in contexts:
                control_half0 = context.get("controlHalf0")
                if not isinstance(control_half0, str) or not control_half0:
                    continue
                is_po_bracketed = (
                    context.get("immediatePrevShortControl") == "peer_control_unknown_po"
                    and context.get("immediateNextShortControl") == "peer_control_unknown_po"
                )
                target = bracketed_half0 if is_po_bracketed else unbracketed_half0
                target[control_half0] = target.get(control_half0, 0) + 1
            po_only_half0 = sorted(key for key in bracketed_half0 if key not in unbracketed_half0)
            non_po_half0 = sorted(unbracketed_half0)
            if po_only_half0 and non_po_half0:
                mutable_object_transition_assessment = (
                    f"{object_name} has a controlHalf0 transition context: "
                    f"{', '.join(po_only_half0)} is only observed when immediately bracketed by PO, "
                    f"while {', '.join(non_po_half0)} is not"
                )
                break

        for object_name in sorted(mutable_fields_by_object_name):
            profile = object_profiles_raw.get(object_name)
            if not isinstance(profile, dict):
                continue
            observations = profile.get("observations", [])
            if not isinstance(observations, list) or not observations:
                continue
            ordered_observations = sorted(
                observations,
                key=lambda item: (
                    str(item.get("time") or ""),
                    str(item.get("sourceLog") or ""),
                    int(item.get("lineNumber") or 0),
                    int(item.get("index") or 0),
                ),
            )
            control_half0_timeline = [
                observation.get("controlHalf0")
                for observation in ordered_observations
                if isinstance(observation.get("controlHalf0"), str) and observation.get("controlHalf0")
            ]
            unique_control_half0_sequence: list[str] = []
            for control_half0 in control_half0_timeline:
                if not unique_control_half0_sequence or unique_control_half0_sequence[-1] != control_half0:
                    unique_control_half0_sequence.append(control_half0)
            reverse_transition_observed = (
                len(unique_control_half0_sequence) >= 3
                and unique_control_half0_sequence[0] == unique_control_half0_sequence[-1]
            )
            mutable_object_state_timelines[object_name] = {
                "observationCount": len(ordered_observations),
                "controlHalf0Timeline": control_half0_timeline,
                "uniqueControlHalf0Sequence": unique_control_half0_sequence,
                "finalObservedControlHalf0": control_half0_timeline[-1] if control_half0_timeline else None,
                "reverseTransitionObserved": reverse_transition_observed,
                "timeRange": [
                    ordered_observations[0].get("time"),
                    ordered_observations[-1].get("time"),
                ] if ordered_observations else [],
                "sampleObservations": [
                    {
                        "time": observation.get("time"),
                        "sourceLog": observation.get("sourceLog"),
                        "lineNumber": observation.get("lineNumber"),
                        "controlHalf0": observation.get("controlHalf0"),
                        "paramHalf1": observation.get("paramHalf1"),
                    }
                    for observation in ordered_observations[:6]
                ],
            }
        if len(mutable_object_state_timelines) == 1:
            object_name, timeline = next(iter(mutable_object_state_timelines.items()))
            unique_sequence = timeline.get("uniqueControlHalf0Sequence", [])
            if (
                isinstance(unique_sequence, list)
                and len(unique_sequence) == 2
                and timeline.get("reverseTransitionObserved") is False
            ):
                mutable_object_state_timeline_assessment = (
                    f"{object_name} currently shows a one-way observed controlHalf0 sequence "
                    f"{unique_sequence[0]} -> {unique_sequence[1]} with no later reverse transition in the March 21 logs"
                )
            elif timeline.get("reverseTransitionObserved") is True and len(unique_sequence) >= 3:
                mutable_object_state_timeline_assessment = (
                    f"{object_name} shows a reversible observed controlHalf0 sequence {' -> '.join(unique_sequence)}"
                )
            else:
                mutable_object_state_timeline_assessment = (
                    f"{object_name} has a partially decoded mutable controlHalf0 timeline"
                )

        mutable_object_state_transitions: dict[str, list[dict[str, Any]]] = {}
        mutable_object_transition_trigger_assessment = "no mutable object transition trigger context observed"
        mutable_object_post_transition_revisits: dict[str, list[dict[str, Any]]] = {}
        mutable_object_post_transition_assessment = "no mutable object post-transition revisit context observed"
        for object_name in sorted(mutable_fields_by_object_name):
            profile = object_profiles_raw.get(object_name)
            if not isinstance(profile, dict):
                continue
            observations = profile.get("observations", [])
            if not isinstance(observations, list) or len(observations) < 2:
                continue
            ordered_observations = sorted(
                observations,
                key=lambda item: (
                    str(item.get("time") or ""),
                    str(item.get("sourceLog") or ""),
                    int(item.get("lineNumber") or 0),
                    int(item.get("index") or 0),
                ),
            )
            transitions: list[dict[str, Any]] = []
            last_observation: dict[str, Any] | None = None
            for observation in ordered_observations:
                current_half0 = observation.get("controlHalf0")
                if not isinstance(current_half0, str) or not current_half0:
                    continue
                if last_observation is None:
                    last_observation = observation
                    continue
                previous_half0 = last_observation.get("controlHalf0")
                if current_half0 == previous_half0:
                    last_observation = observation
                    continue
                observation_index = observation.get("index")
                prev_index = _find_neighbor_index(observation_index, -1) if isinstance(observation_index, int) else None
                next_index = _find_neighbor_index(observation_index, 1) if isinstance(observation_index, int) else None
                prev_decoder = history[prev_index].get("decoder") or {} if prev_index is not None else {}
                next_decoder = history[next_index].get("decoder") or {} if next_index is not None else {}
                preceding_event_families: dict[str, int] = {}
                preceding_control_events: dict[str, int] = {}
                preceding_object_names: dict[str, int] = {}
                nearest_preceding_state_block: dict[str, Any] | None = None
                following_event_families: dict[str, int] = {}
                following_object_names: dict[str, int] = {}
                later_same_object_control_half0: dict[str, int] = {}
                post_transition_packet_count = 0
                if isinstance(observation_index, int):
                    source_log = observation.get("sourceLog")
                    for window_index in range(max(0, observation_index - 8), observation_index):
                        window_entry = history[window_index]
                        if window_entry.get("channel") != "udp":
                            continue
                        if source_log and window_entry.get("sourceLog") != source_log:
                            continue
                        window_decoder = window_entry.get("decoder") or {}
                        family = window_decoder.get("family")
                        if isinstance(family, str) and family:
                            preceding_event_families[family] = preceding_event_families.get(family, 0) + 1
                        control_event = window_decoder.get("controlEvent")
                        if isinstance(control_event, str) and control_event:
                            preceding_control_events[control_event] = preceding_control_events.get(control_event, 0) + 1
                        object_name_window = window_decoder.get("primaryObjectName")
                        if isinstance(object_name_window, str) and object_name_window:
                            preceding_object_names[object_name_window] = preceding_object_names.get(object_name_window, 0) + 1
                    for state_index in range(observation_index - 1, -1, -1):
                        state_entry = history[state_index]
                        if state_entry.get("channel") != "udp":
                            continue
                        if source_log and state_entry.get("sourceLog") != source_log:
                            continue
                        state_decoder = state_entry.get("decoder") or {}
                        if state_decoder.get("family") != "peer_state_block":
                            continue
                        nearest_preceding_state_block = {
                            "time": state_entry.get("time"),
                            "lineNumber": state_entry.get("lineNumber"),
                            "stateHeader": state_decoder.get("stateHeader"),
                            "unitCodes": state_decoder.get("unitCodes"),
                            "stateObjectCount": state_decoder.get("stateObjectCount"),
                        }
                        break
                    for window_index in range(observation_index + 1, len(history)):
                        window_entry = history[window_index]
                        if window_entry.get("channel") != "udp":
                            continue
                        if source_log and window_entry.get("sourceLog") != source_log:
                            continue
                        post_transition_packet_count += 1
                        window_decoder = window_entry.get("decoder") or {}
                        family = window_decoder.get("family")
                        if isinstance(family, str) and family:
                            following_event_families[family] = following_event_families.get(family, 0) + 1
                        object_name_window = window_decoder.get("primaryObjectName")
                        if isinstance(object_name_window, str) and object_name_window:
                            following_object_names[object_name_window] = following_object_names.get(object_name_window, 0) + 1
                            if object_name_window == object_name:
                                descriptor_sketch_window = window_decoder.get("descriptorSketch")
                                if isinstance(descriptor_sketch_window, dict):
                                    control_word_window = descriptor_sketch_window.get("controlWord")
                                    control_value_window = _parse_hex_word(control_word_window) if isinstance(control_word_window, str) else None
                                    if control_value_window is not None:
                                        key = f"0x{(control_value_window >> 16) & 0xFFFF:04x}"
                                        later_same_object_control_half0[key] = later_same_object_control_half0.get(key, 0) + 1
                transition_entry = {
                    "fromControlHalf0": previous_half0,
                    "toControlHalf0": current_half0,
                    "previousObservationTime": last_observation.get("time"),
                    "changedObservationTime": observation.get("time"),
                    "previousObservationLineNumber": last_observation.get("lineNumber"),
                    "changedObservationLineNumber": observation.get("lineNumber"),
                    "immediatePrevEvent": _label_event(prev_decoder) if prev_decoder else None,
                    "immediateNextEvent": _label_event(next_decoder) if next_decoder else None,
                    "immediatePrevShortControl": _label_short_control(prev_decoder),
                    "immediateNextShortControl": _label_short_control(next_decoder),
                    "precedingEventFamilies": _sorted_count_map(preceding_event_families),
                    "precedingControlEvents": _sorted_count_map(preceding_control_events),
                    "precedingObjectNames": _sorted_count_map(preceding_object_names),
                    "nearestPrecedingStateBlock": nearest_preceding_state_block,
                    "postTransitionPacketCount": post_transition_packet_count if isinstance(observation_index, int) else 0,
                    "followingEventFamilies": _sorted_count_map(following_event_families) if isinstance(observation_index, int) else {},
                    "followingObjectNames": _sorted_count_map(following_object_names) if isinstance(observation_index, int) else {},
                    "laterSameObjectControlHalf0": _sorted_count_map(later_same_object_control_half0) if isinstance(observation_index, int) else {},
                }
                transitions.append(transition_entry)
                last_observation = observation
            if transitions:
                mutable_object_state_transitions[object_name] = transitions
                mutable_object_post_transition_revisits[object_name] = transitions

        if len(mutable_object_state_transitions) == 1:
            object_name, transitions = next(iter(mutable_object_state_transitions.items()))
            transition = transitions[0] if transitions else None
            if isinstance(transition, dict):
                preceding_families = transition.get("precedingEventFamilies", {})
                preceding_controls = transition.get("precedingControlEvents", {})
                if (
                    transition.get("immediatePrevShortControl") == "peer_control_unknown_po"
                    and transition.get("immediateNextShortControl") == "peer_control_unknown_po"
                    and preceding_controls.get("peer_control_unknown_po")
                    and preceding_families.get("peer_state_block")
                    and not preceding_families.get("peer_object_control")
                ):
                    nearest_state = transition.get("nearestPrecedingStateBlock")
                    nearest_state_unit_codes = nearest_state.get("unitCodes") if isinstance(nearest_state, dict) else None
                    if isinstance(nearest_state_unit_codes, list) and nearest_state_unit_codes:
                        mutable_object_transition_trigger_assessment = (
                            f"{object_name} changes state inside the late PO burst after recent peer_state_block traffic "
                            f"({', '.join(nearest_state_unit_codes)}), with no nearby peer_object_control"
                        )
                    else:
                        mutable_object_transition_trigger_assessment = (
                            f"{object_name} changes state inside the late PO burst after recent peer_state_block traffic, with no nearby peer_object_control"
                        )
                else:
                    mutable_object_transition_trigger_assessment = (
                        f"{object_name} has a partially decoded transition trigger context"
                    )

        if len(mutable_object_post_transition_revisits) == 1:
            object_name, transitions = next(iter(mutable_object_post_transition_revisits.items()))
            transition = transitions[0] if transitions else None
            if isinstance(transition, dict):
                later_same_object = transition.get("laterSameObjectControlHalf0", {})
                following_object_names = transition.get("followingObjectNames", {})
                if not later_same_object:
                    trailing_names = [name for name in following_object_names if name != object_name]
                    if trailing_names:
                        mutable_object_post_transition_assessment = (
                            f"{object_name} has no later descriptor revisit after the observed state change; "
                            f"the remaining log tail only shows {', '.join(trailing_names[:3])}"
                        )
                    else:
                        mutable_object_post_transition_assessment = (
                            f"{object_name} has no later descriptor revisit after the observed state change in the current log tail"
                        )
                else:
                    mutable_object_post_transition_assessment = (
                        f"{object_name} is revisited after the observed state change with controlHalf0 values "
                        f"{', '.join(later_same_object.keys())}"
                    )

        po_sandwiched_objects = sorted(po_sandwiched_objects)
        po_leading_edge_objects = sorted(po_leading_edge_objects)
        if po_sandwiched_objects:
            repeated_object_context_assessment = (
                f"PO-sandwiched descriptor observations appear for {', '.join(po_sandwiched_objects[:5])}"
            )
            if po_leading_edge_objects:
                repeated_object_context_assessment += (
                    f"; {', '.join(po_leading_edge_objects[:3])} also appears on a leading-PO trailing-edge observation"
                )

        po_context_control_comparisons: dict[str, dict[str, Any]] = {}
        po_context_control_assessment = "no PO-context object control comparison available"
        po_context_objects = sorted(set(po_sandwiched_objects) | set(po_leading_edge_objects))
        for object_name in po_context_objects:
            profile = object_profiles_raw.get(object_name)
            if not isinstance(profile, dict):
                continue
            control_half0_counts: dict[str, int] = {}
            for control_word, count in profile["payloadWords"].get("controlWord", {}).items():
                control_value = _parse_hex_word(control_word)
                if control_value is None:
                    continue
                key = f"0x{(control_value >> 16) & 0xFFFF:04x}"
                control_half0_counts[key] = control_half0_counts.get(key, 0) + count
            po_context_control_comparisons[object_name] = {
                "contextSignatures": object_observation_context_signatures.get(object_name, {}),
                "subtypeWords": _sorted_count_map(profile.get("subtypeWords", {})),
                "controlHalf0": _sorted_count_map(control_half0_counts),
                "paramWord": _sorted_count_map(profile["payloadWords"].get("paramWord", {})),
                "mutableFields": mutable_fields_by_object_name.get(object_name, []),
                "poContextRole": (
                    "po_sandwiched"
                    if object_name in po_sandwiched_objects
                    else "po_leading_edge"
                ),
            }
        if po_context_control_comparisons:
            multi_state_po_sandwiched = sorted(
                object_name
                for object_name in po_sandwiched_objects
                if len(po_context_control_comparisons.get(object_name, {}).get("controlHalf0", {})) > 1
            )
            single_state_po_sandwiched = sorted(
                object_name
                for object_name in po_sandwiched_objects
                if len(po_context_control_comparisons.get(object_name, {}).get("controlHalf0", {})) == 1
            )
            po_context_control_assessment = "PO-context object control comparison is partially decoded"
            if multi_state_po_sandwiched or single_state_po_sandwiched:
                assessment_parts: list[str] = []
                if multi_state_po_sandwiched:
                    assessment_parts.append(
                        f"among PO-sandwiched objects, only {', '.join(multi_state_po_sandwiched)} show multiple observed controlHalf0 values"
                    )
                if single_state_po_sandwiched:
                    assessment_parts.append(
                        f"{', '.join(single_state_po_sandwiched)} remain single-state across observed controlHalf0 values"
                    )
                if po_leading_edge_objects:
                    assessment_parts.append(
                        f"{', '.join(po_leading_edge_objects)} currently appears only on the PO-leading edge"
                    )
                po_context_control_assessment = "; ".join(assessment_parts)

        ordered.append(
            {
                "payloadFamily": summary["payloadFamily"],
                "packetCount": summary["packetCount"],
                "descriptorFamilies": _sorted_count_map(summary["descriptorFamilies"]),
                "descriptorArchetypes": _sorted_count_map(summary["descriptorArchetypes"]),
                "objectKinds": _sorted_count_map(summary["objectKinds"]),
                "objectNames": sorted(summary["objectNames"])[:8],
                "controlSignatures": _sorted_count_map(summary["controlSignatures"]),
                "payloadWords": payload_words,
                "payloadWordByteProfiles": payload_word_byte_profiles,
                "payloadWordHalfProfiles": payload_word_half_profiles,
                "subtypeWordClusters": subtype_word_clusters,
                "reusedSubtypeWords": reused_subtype_words,
                "subtypeWordAssessment": subtype_word_assessment,
                "controlWordHalf0Clusters": {
                    key: {
                        "count": value["count"],
                        "objectNames": sorted(value["objectNames"])[:8],
                        "ordinals": sorted(value["ordinals"]),
                        "pairedHalf0": _sorted_count_map(value["pairedHalf0"]),
                        "pairedHalf1": _sorted_count_map(value["pairedHalf1"]),
                    }
                    for key, value in _sorted_count_map({k: v["count"] for k, v in summary["controlWordHalf0Clusters"].items()}).items()
                    for value in [summary["controlWordHalf0Clusters"][key]]
                },
                "controlWordHalf0ClusterHints": _build_halfword_cluster_hints(
                    summary["controlWordHalf0Clusters"],
                    len(summary["objectNames"]),
                ),
                "paramWordHalf1Clusters": {
                    key: {
                        "count": value["count"],
                        "objectNames": sorted(value["objectNames"])[:8],
                        "ordinals": sorted(value["ordinals"]),
                        "pairedHalf0": _sorted_count_map(value["pairedHalf0"]),
                        "pairedHalf1": _sorted_count_map(value["pairedHalf1"]),
                    }
                    for key, value in _sorted_count_map({k: v["count"] for k, v in summary["paramWordHalf1Clusters"].items()}).items()
                    for value in [summary["paramWordHalf1Clusters"][key]]
                },
                "paramWordHalf1ClusterHints": _build_halfword_cluster_hints(
                    summary["paramWordHalf1Clusters"],
                    len(summary["objectNames"]),
                ),
                "controlWordHalf0VectorClusters": {
                    key: {
                        "count": value["count"],
                        "objectNames": sorted(value["objectNames"])[:8],
                        "ordinals": sorted(value["ordinals"]),
                        "pairedVectorAHalf1": _sorted_count_map(value["pairedVectorAHalf1"]),
                        "pairedVectorBHalf0": _sorted_count_map(value["pairedVectorBHalf0"]),
                    }
                    for key, value in _sorted_count_map({k: v["count"] for k, v in summary["controlWordHalf0VectorClusters"].items()}).items()
                    for value in [summary["controlWordHalf0VectorClusters"][key]]
                },
                "controlWordHalf0VectorClusterHints": control_word_half0_vector_cluster_hints,
                "paramWordHalf1VectorClusters": {
                    key: {
                        "count": value["count"],
                        "objectNames": sorted(value["objectNames"])[:8],
                        "ordinals": sorted(value["ordinals"]),
                        "pairedVectorAHalf1": _sorted_count_map(value["pairedVectorAHalf1"]),
                        "pairedVectorBHalf0": _sorted_count_map(value["pairedVectorBHalf0"]),
                    }
                    for key, value in _sorted_count_map({k: v["count"] for k, v in summary["paramWordHalf1VectorClusters"].items()}).items()
                    for value in [summary["paramWordHalf1VectorClusters"][key]]
                },
                "paramWordHalf1VectorClusterHints": param_word_half1_vector_cluster_hints,
                "controlWordHalf0VectorAssessment": _assess_vector_cluster_alignment(
                    control_word_half0_vector_cluster_hints,
                    "controlWordHalf0",
                ),
                "paramWordHalf1VectorAssessment": _assess_vector_cluster_alignment(
                    param_word_half1_vector_cluster_hints,
                    "paramWordHalf1",
                ),
                "stablePayloadFields": stable_fields,
                "varyingPayloadFields": varying_fields,
                "commonPayloadValues": common_values,
                "likelyControlFields": likely_control_candidates,
                "likelyVectorFields": likely_vector_candidates,
                "objectStablePayloadFields": stable_within_object_names,
                "objectVaryingAcrossNamesFields": varying_across_object_names,
                "objectSpecificPayloadFields": object_specific_payload_fields,
                "mutableWithinObjectNameFields": mutable_within_object_name_fields,
                "objectStabilityAssessment": object_stability_assessment,
                "subtypeMutationCorrelations": subtype_mutation_correlations,
                "mutableSubtypeWords": mutable_subtype_words,
                "subtypeMutationAssessment": subtype_mutation_assessment,
                "objectObservationContextSignatures": object_observation_context_signatures,
                "poSandwichedObjects": po_sandwiched_objects,
                "poLeadingEdgeObjects": po_leading_edge_objects,
                "objectObservationContextAssessment": repeated_object_context_assessment,
                "poContextControlComparisons": po_context_control_comparisons,
                "poContextControlAssessment": po_context_control_assessment,
                "mutableObjectObservationContexts": mutable_object_observation_contexts,
                "mutableObjectTransitionAssessment": mutable_object_transition_assessment,
                "mutableObjectStateTimelines": mutable_object_state_timelines,
                "mutableObjectStateTimelineAssessment": mutable_object_state_timeline_assessment,
                "mutableObjectStateTransitions": mutable_object_state_transitions,
                "mutableObjectTransitionTriggerAssessment": mutable_object_transition_trigger_assessment,
                "mutableObjectPostTransitionRevisits": mutable_object_post_transition_revisits,
                "mutableObjectPostTransitionAssessment": mutable_object_post_transition_assessment,
                "payloadAssessment": payload_assessment,
                "sampleDescriptors": summary["sampleDescriptors"],
                "sampleObjectProfiles": object_profiles,
                "mutableObjectMutations": mutable_object_mutations,
            }
        )
    return ordered


def summarize_peer_udp_sessions(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sessions: dict[str, dict[str, Any]] = {}
    for idx, entry in enumerate(history):
        if entry.get("channel") != "udp":
            continue
        decoder = entry.get("decoder") or {}
        family = str(decoder.get("family", ""))
        if not family.startswith("peer_"):
            continue

        peer = str(entry.get("peer", ""))
        if not peer:
            continue
        summary = sessions.setdefault(
            peer,
            {
                "peer": peer,
                "firstSeen": entry.get("time"),
                "lastSeen": entry.get("time"),
                "packetCount": 0,
                "families": {},
                "markers": set(),
                "controlEvents": {},
                "timingSamples": {},
                "objectSetupEvents": {},
                "steamIds": set(),
                "asciiStrings": set(),
                "unitCodes": set(),
                "objectCodes": set(),
                "objectNames": set(),
                "objectKinds": set(),
                "objectKindCounts": {},
                "descriptorPatterns": {},
                "descriptorFamilyHints": {},
                "descriptorArchetypeHints": {},
                "descriptorPayloadFamilyHints": {},
                "descriptorControlSignatures": {},
                "descriptorIdentityWords": {},
                "latestStateHeader": None,
                "latestObjectHeader": None,
                "latestObjectDescriptor": None,
                "phaseSequence": [],
                "_lastPhase": None,
                "_firstIndex": idx,
                "_lastIndex": idx,
            },
        )

        summary["lastSeen"] = entry.get("time")
        summary["packetCount"] += 1
        summary["families"][family] = summary["families"].get(family, 0) + 1
        summary["_lastIndex"] = idx

        marker = decoder.get("marker")
        if isinstance(marker, str) and marker:
            summary["markers"].add(marker)
        control_event = decoder.get("controlEvent")
        if isinstance(control_event, str) and control_event:
            summary["controlEvents"][control_event] = summary["controlEvents"].get(control_event, 0) + 1
            timing_fields = decoder.get("timingFields")
            if isinstance(timing_fields, dict) and timing_fields:
                summary["timingSamples"][control_event] = timing_fields
        object_event = decoder.get("objectSetupEvent")
        if isinstance(object_event, str) and object_event:
            summary["objectSetupEvents"][object_event] = summary["objectSetupEvents"].get(object_event, 0) + 1

        for key in ("steamIds", "asciiStrings"):
            values = decoder.get(key)
            if isinstance(values, list):
                for value in values:
                    if isinstance(value, str) and value:
                        summary[key].add(value)
        unit_codes = decoder.get("unitCodes")
        if isinstance(unit_codes, list):
            for value in unit_codes:
                if isinstance(value, str) and value:
                    summary["unitCodes"].add(value)
        for key in ("objectCodes", "objectNames"):
            values = decoder.get(key)
            if isinstance(values, list):
                for value in values:
                    if isinstance(value, str) and value:
                        summary[key].add(value)
        object_kind = decoder.get("objectKind")
        if isinstance(object_kind, str) and object_kind:
            summary["objectKinds"].add(object_kind)
            summary["objectKindCounts"][object_kind] = summary["objectKindCounts"].get(object_kind, 0) + 1
        descriptor_pattern = decoder.get("descriptorPattern")
        if isinstance(descriptor_pattern, str) and descriptor_pattern:
            summary["descriptorPatterns"][descriptor_pattern] = summary["descriptorPatterns"].get(descriptor_pattern, 0) + 1
        descriptor_family_hint = decoder.get("descriptorFamilyHint")
        if isinstance(descriptor_family_hint, str) and descriptor_family_hint:
            summary["descriptorFamilyHints"][descriptor_family_hint] = summary["descriptorFamilyHints"].get(descriptor_family_hint, 0) + 1
        descriptor_archetype_hint = decoder.get("descriptorArchetypeHint")
        if isinstance(descriptor_archetype_hint, str) and descriptor_archetype_hint:
            summary["descriptorArchetypeHints"][descriptor_archetype_hint] = summary["descriptorArchetypeHints"].get(descriptor_archetype_hint, 0) + 1
        descriptor_payload_family_hint = decoder.get("descriptorPayloadFamilyHint")
        if isinstance(descriptor_payload_family_hint, str) and descriptor_payload_family_hint:
            summary["descriptorPayloadFamilyHints"][descriptor_payload_family_hint] = summary["descriptorPayloadFamilyHints"].get(descriptor_payload_family_hint, 0) + 1
        descriptor_payload_hints = decoder.get("descriptorPayloadHints")
        if isinstance(descriptor_payload_hints, dict):
            control_signature = descriptor_payload_hints.get("controlSignature")
            if isinstance(control_signature, str) and control_signature:
                summary["descriptorControlSignatures"][control_signature] = summary["descriptorControlSignatures"].get(control_signature, 0) + 1
        descriptor_field_hints = decoder.get("descriptorFieldHints")
        if isinstance(descriptor_field_hints, dict):
            identity_words = descriptor_field_hints.get("identityWords")
            if isinstance(identity_words, dict):
                for key, value in identity_words.items():
                    if isinstance(value, str) and value:
                        bucket = summary["descriptorIdentityWords"].setdefault(key, {})
                        bucket[value] = bucket.get(value, 0) + 1
        state_header = decoder.get("stateHeader")
        if isinstance(state_header, dict) and state_header:
            summary["latestStateHeader"] = state_header
        object_header = decoder.get("objectHeader")
        if isinstance(object_header, dict) and object_header:
            summary["latestObjectHeader"] = object_header
        if family == "peer_object_descriptor":
            latest_descriptor: dict[str, Any] = {}
            for key in (
                "primaryObjectCode",
                "primaryObjectName",
                "objectNameStem",
                "objectKind",
                "objectOrdinal",
                "sideCodePrefix",
                "sideLabel",
                "personRole",
                "descriptorFamilyHint",
                "descriptorArchetypeHint",
                "descriptorPayloadFamilyHint",
                "descriptorSketch",
                "descriptorFieldHints",
                "descriptorPayloadHints",
            ):
                value = decoder.get(key)
                if value is not None:
                    latest_descriptor[key] = value
            if latest_descriptor:
                summary["latestObjectDescriptor"] = latest_descriptor

        phase = _phase_for_udp_family(family)
        if phase != "other" and phase != summary["_lastPhase"]:
            summary["phaseSequence"].append(phase)
            summary["_lastPhase"] = phase

    ordered: list[dict[str, Any]] = []
    for summary in sorted(sessions.values(), key=lambda item: item["_firstIndex"]):
        phase_sequence = list(summary["phaseSequence"])
        inferred_stage = "peer_unknown"
        if "state" in phase_sequence:
            inferred_stage = "state_exchange"
        elif "objects" in phase_sequence:
            inferred_stage = "object_setup"
        elif "control" in phase_sequence or "identity" in phase_sequence:
            inferred_stage = "p2p_bootstrap"

        ordered.append(
            {
                "peer": summary["peer"],
                "firstSeen": summary["firstSeen"],
                "lastSeen": summary["lastSeen"],
                "packetCount": summary["packetCount"],
                "families": summary["families"],
                "markers": sorted(summary["markers"]),
                "controlEvents": summary["controlEvents"],
                "timingSamples": summary["timingSamples"],
                "objectSetupEvents": summary["objectSetupEvents"],
                "steamIds": sorted(summary["steamIds"]),
                "asciiStrings": sorted(summary["asciiStrings"])[:8],
                "unitCodes": sorted(summary["unitCodes"])[:8],
                "objectCodes": sorted(summary["objectCodes"])[:8],
                "objectNames": sorted(summary["objectNames"])[:8],
                "objectKinds": sorted(summary["objectKinds"]),
                "objectKindCounts": summary["objectKindCounts"],
                "descriptorPatterns": summary["descriptorPatterns"],
                "descriptorFamilyHints": summary["descriptorFamilyHints"],
                "descriptorArchetypeHints": summary["descriptorArchetypeHints"],
                "descriptorPayloadFamilyHints": summary["descriptorPayloadFamilyHints"],
                "descriptorControlSignatures": summary["descriptorControlSignatures"],
                "descriptorIdentityWords": summary["descriptorIdentityWords"],
                "latestStateHeader": summary["latestStateHeader"],
                "latestObjectHeader": summary["latestObjectHeader"],
                "latestObjectDescriptor": summary["latestObjectDescriptor"],
                "phaseSequence": phase_sequence,
                "inferredStage": inferred_stage,
            }
        )
    return ordered


@dataclass
class UserSession:
    user_id: str
    name: str = "Unknown"
    player_name: str = "Unknown"
    authtype: str = "web"
    client_version: str = "2.2.301"
    key: str = ""
    lan_addresses: list[str] = field(default_factory=list)
    wan_address: str = ""
    in_lounge: bool = False
    is_admin: bool = False
    current_lobby_id: int | None = None
    extra_data: dict[str, Any] = field(default_factory=dict)
    ws: Any = None
    # OpenShim sends the literal "withheld" instead of a platform ticket to
    # non-Rebellion servers; recorded for operators only (never on the wire).
    ticket_withheld: bool = False
    # Read-only loopback watcher (--allow-loopback-watchers): receives public
    # chat-room chat without joining rooms; never a lobby member.
    is_watcher: bool = False
    # Chat flood bookkeeping (runtime only).
    chat_tokens: float | None = None
    chat_stamp: float = 0.0
    chat_strikes: list[float] = field(default_factory=list)
    chat_notice_stamp: float = 0.0
    chat_dropped: int = 0


@dataclass
class LobbyState:
    lobby_id: int
    owner: str
    metadata: dict[str, Any]
    member_limit: int = 20000
    is_private: bool = False
    is_locked: bool = False
    password: str = ""
    created_time: str = field(default_factory=utc_now_str)
    users: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Persistent lobbies (e.g. the seeded ~chat~pub~~default room) survive even
    # when empty. The official service keeps such bridge-owned rooms forever.
    persistent: bool = False


SECRET_FIELD_NAMES = frozenset({"password", "token", "ticket", "authticket", "steamticket", "steamappticket",
                                "gogappticket", "platformticket", "adminkey", "secret", "authorization"})
TICKET_WITHHELD_MARKER = "withheld"
TICKET_FIELDS = ("steamAppTicket", "gogAppTicket", "authTicket", "platformTicket")
DEFAULT_AUDIT_LOG = "data/audit.log"
DEFAULT_RESERVED_NAMES = "SERVER,Admin,Administrator,Moderator,Mod,Console,System,Rebellion,Staff"
LOOPBACK_NAMES = ("::1", "::ffff:127.0.0.1")
# Everything a read-only watcher session may send; all other verbs are ignored.
WATCHER_VERBS = frozenset({"DoEnterLounge", "DoExitLounge", "GetLobbyList", "Ping", "DoPing"})
PUBLIC_CHAT_PREFIX = "~chat~pub~~"


def is_loopback_ip(ip: str) -> bool:
    return ip.startswith("127.") or ip in LOOPBACK_NAMES


def ticket_is_withheld(content: dict[str, Any]) -> bool:
    return any(str(content.get(key, "") or "").strip().lower() == TICKET_WITHHELD_MARKER for key in TICKET_FIELDS)


def describe_authtype(authtype: str, ticket_withheld: bool) -> str:
    """Operator-facing auth label. The wire value (authType) is never altered."""
    return f"{authtype} (ticket withheld)" if ticket_withheld else authtype


def redact_secrets(value: Any, secrets: Any = ()) -> Any:
    """Recursively blank credential-named fields and any value containing a known secret."""
    if isinstance(value, dict):
        return {k: "<redacted>" if str(k).lower() in SECRET_FIELD_NAMES or str(k).lower() == "key"
                else redact_secrets(v, secrets) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_secrets(v, secrets) for v in value]
    if isinstance(value, str) and value and any(sec and (sec == value or (len(sec) >= 6 and sec in value)) for sec in secrets):
        return "<redacted>"
    return value


class AuditLog:
    """Append-only JSON Lines audit trail, rotated by size. Never stores credentials."""

    def __init__(self, path: str | None = None, *, max_bytes: int = 5_000_000, backups: int = 3) -> None:
        self.path = Path(path).expanduser() if path else None
        self.max_bytes = max(0, int(max_bytes))
        self.backups = max(0, int(backups))
        self.secrets: set[str] = set()
        self.records_written = 0

    @property
    def enabled(self) -> bool:
        return self.path is not None

    def _rotate(self) -> None:
        assert self.path is not None
        if self.backups <= 0:
            self.path.unlink(missing_ok=True)
            return
        for i in range(self.backups - 1, 0, -1):
            src = self.path.with_name(f"{self.path.name}.{i}")
            if src.exists():
                src.replace(self.path.with_name(f"{self.path.name}.{i + 1}"))
        self.path.replace(self.path.with_name(f"{self.path.name}.1"))

    def write(self, event: str, **fields: Any) -> None:
        if self.path is None:
            return
        record = {"time": utc_now_str(), "ts": round(time.time(), 3), "event": event}
        record.update({k: v for k, v in redact_secrets(fields, self.secrets).items() if v is not None})
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.max_bytes and self.path.exists() and self.path.stat().st_size >= self.max_bytes:
                self._rotate()
            with open(self.path, "a", encoding="utf-8", newline="\n") as fh:
                fh.write(json.dumps(record, separators=(",", ":"), ensure_ascii=True) + "\n")
            self.records_written += 1
        except OSError:
            logging.getLogger("bzr-dedicated.audit").exception("Audit log write failed: %s", self.path)


def parse_duration(value: Any) -> float | None:
    """Seconds from 90, "90", "30s", "15m", "2h", "7d"; None for empty/permanent."""
    if value is None or value == "" or value is False:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        seconds = float(value)
    else:
        match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smhdw]?)\s*", str(value).lower())
        if not match:
            raise ValueError(f"bad duration {value!r} (use e.g. 30m, 2h, 7d, or seconds)")
        seconds = float(match.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[match.group(2)]
    if seconds <= 0:
        raise ValueError("duration must be positive")
    return seconds


def sanitize_display_name(raw: Any, *, max_length: int = 32, reserved: Any = (), allow_reserved: bool = False) -> tuple[str, list[str]]:
    """Return (clean_name, adjustments).

    Strips control, bidi, zero-width and other invisible/format characters,
    collapses whitespace, trims and caps the length. An empty result returns ""
    so the caller can fall back. A reserved name (server/admin impersonation)
    also returns "" with a "reserved" adjustment.
    """
    notes: list[str] = []
    text = str(raw if raw is not None else "")
    kept = []
    for ch in text:
        cat = unicodedata.category(ch)
        if ch in ("\t", "\n", "\r"):
            kept.append(" ")
            continue
        if cat in ("Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"):
            continue
        kept.append(" " if cat == "Zs" else ch)
    clean = " ".join("".join(kept).split())
    if clean != text.strip():
        notes.append("stripped")
    if max_length > 0 and len(clean) > max_length:
        clean = clean[:max_length].rstrip()
        notes.append("truncated")
    if clean and not allow_reserved and reserved:
        def fold(x: str) -> str:
            return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", x).casefold())
        if fold(clean) in {fold(str(r)) for r in reserved}:
            notes.append("reserved")
            return "", notes
    return clean, notes


def parse_version(text: str) -> tuple[int, ...] | None:
    parts = re.findall(r"\d+", str(text))
    return tuple(int(p) for p in parts[:6]) if parts else None


@dataclass
class HardeningConfig:
    """Operator-tunable abuse controls. Defaults keep stock clients working."""

    name_max_length: int = 32
    reserved_names: tuple[str, ...] = tuple(x.strip() for x in DEFAULT_RESERVED_NAMES.split(",") if x.strip())
    version_policy: str = "log"            # off | log | enforce
    min_client_version: str = ""
    allowed_client_versions: tuple[str, ...] = ()
    chat_rate: float = 2.0                 # sustained messages per second, 0 = unlimited
    chat_burst: int = 8
    chat_flood_strikes: int = 5            # dropped messages inside chat_flood_window before auto-mute
    chat_flood_window: float = 30.0
    chat_automute_minutes: float = 5.0     # 0 disables auto-mute
    trusted_proxies: tuple[str, ...] = ()
    allow_plain_remote_admin: bool = False

    def version_verdict(self, version: str) -> tuple[bool, str]:
        """(acceptable, reason). Always (True, "") when no restriction is configured."""
        if self.allowed_client_versions:
            for pattern in self.allowed_client_versions:
                if pattern == version or (pattern.endswith("*") and version.startswith(pattern[:-1])):
                    break
            else:
                return False, f"client version {version!r} not in allowlist"
        if self.min_client_version:
            have, need = parse_version(version), parse_version(self.min_client_version)
            if need is not None and (have is None or have < need):
                return False, f"client version {version!r} older than {self.min_client_version}"
        return True, ""

    def is_trusted_proxy(self, ip: str) -> bool:
        try:
            addr = ipaddress.ip_address(ip.split("%")[0])
        except ValueError:
            return False
        for entry in self.trusted_proxies:
            try:
                if addr in ipaddress.ip_network(entry, strict=False):
                    return True
            except ValueError:
                continue
        return False


ACCESS_MODES = ("open", "approve", "allowlist")


class AccessControl:
    """Operator admission control for stock clients (layer 1, not cryptographic).

    Stock clients only present a claimed id (Steam tickets cannot be validated
    without Rebellion's publisher key; GOG sends none), so this keys on that id
    plus the connecting IP. It keeps strangers out of a friends server; it does
    not stop someone who knows an approved player's public Steam id, which is
    what IP pinning narrows.
    """

    PENDING_TTL_SECONDS = 600

    def __init__(self, logger: logging.Logger, on_change: Any) -> None:
        self.log = logger
        self._on_change = on_change
        self.mode = "open"
        self.pin_ips = False
        self.approval_wait_seconds = 30
        self.rate_limit_per_minute = 20
        self.approved: dict[str, dict[str, Any]] = {}
        self.banned_ids: dict[str, dict[str, Any]] = {}
        self.banned_ips: dict[str, dict[str, Any]] = {}
        self.muted_ids: dict[str, dict[str, Any]] = {}
        self.audit: AuditLog = AuditLog()
        self.clock = time.time  # epoch seconds; replaceable in tests
        # Runtime only: players waiting for a decision.
        self.pending: dict[str, dict[str, Any]] = {}
        self._attempts: dict[str, list[float]] = {}
        self.denied_total = 0

    # ---- persistence ----
    def to_payload(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "pinIps": self.pin_ips,
            "approved": self.approved,
            "bannedIds": self.banned_ids,
            "bannedIps": self.banned_ips,
            "mutedIds": self.muted_ids,
        }

    def load_payload(self, payload: Any) -> None:
        if not isinstance(payload, dict):
            return
        mode = str(payload.get("mode", self.mode))
        self.mode = mode if mode in ACCESS_MODES else "open"
        self.pin_ips = bool(payload.get("pinIps", self.pin_ips))
        for attr, key in (("approved", "approved"), ("banned_ids", "bannedIds"), ("banned_ips", "bannedIps"),
                           ("muted_ids", "mutedIds")):
            value = payload.get(key)
            if isinstance(value, dict):
                setattr(self, attr, {str(k): dict(v) if isinstance(v, dict) else {} for k, v in value.items()})
        self.prune_expired()

    # ---- expiry (entries without expiresEpoch are permanent) ----
    def _expiry(self, seconds: float | None) -> dict[str, Any]:
        if seconds is None:
            return {}
        epoch = self.clock() + seconds
        return {"expiresEpoch": round(epoch, 3),
                "expiresAt": datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")}

    def _live(self, entry: dict[str, Any]) -> bool:
        expires = entry.get("expiresEpoch")
        return not isinstance(expires, (int, float)) or expires > self.clock()

    def prune_expired(self) -> int:
        removed = 0
        for kind, table in (("ban_id", self.banned_ids), ("ban_ip", self.banned_ips), ("mute", self.muted_ids)):
            for key in [k for k, v in table.items() if not self._live(v)]:
                table.pop(key, None)
                removed += 1
                self.audit.write(f"{kind}_expired", target=key)
        if removed:
            self._changed()
        return removed

    def _changed(self) -> None:
        self._on_change()

    # ---- decisions ----
    def rate_limited(self, ip: str) -> bool:
        if self.rate_limit_per_minute <= 0 or not ip:
            return False
        now = time.monotonic()
        window = [t for t in self._attempts.get(ip, []) if now - t < 60]
        window.append(now)
        self._attempts[ip] = window
        return len(window) > self.rate_limit_per_minute

    def decide(self, user_id: str, ip: str) -> tuple[str, str]:
        """Return ("allow" | "pending" | "deny", reason)."""
        self.prune_expired()
        if user_id in self.banned_ids:
            return "deny", "banned id"
        if ip and ip in self.banned_ips:
            return "deny", "banned ip"
        if self.mode == "open":
            return "allow", "open"
        entry = self.approved.get(user_id)
        if entry is not None:
            known_ips = entry.get("ips") or []
            if not (self.pin_ips and known_ips and ip not in known_ips):
                return "allow", "approved"
            reason = "approved id from a new ip"
        else:
            reason = "not approved"
        if self.mode == "approve":
            return "pending", reason
        return "deny", reason

    def note_seen(self, user_id: str, ip: str, name: str) -> None:
        entry = self.approved.get(user_id)
        if entry is None:
            return
        entry["lastSeen"] = utc_now_str()
        if name:
            entry["name"] = name
        ips = entry.setdefault("ips", [])
        if ip and ip not in ips and not self.pin_ips:
            ips.append(ip)
            del ips[:-8]
        self._changed()

    # ---- pending ----
    def add_pending(self, user_id: str, name: str, ip: str, reason: str) -> asyncio.Future:
        self._expire_pending()
        old = self.pending.get(user_id)
        if old and not old["future"].done():
            old["future"].set_result(False)
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self.pending[user_id] = {
            "id": user_id, "name": name, "ip": ip, "reason": reason,
            "since": utc_now_str(), "monotonic": time.monotonic(), "future": fut,
        }
        self.log.info("Access: %s (%s) from %s waiting for approval (%s)", user_id, name, ip, reason)
        return fut

    def _expire_pending(self) -> None:
        now = time.monotonic()
        for uid in [u for u, p in self.pending.items() if now - p["monotonic"] > self.PENDING_TTL_SECONDS]:
            self.pending.pop(uid, None)

    # ---- operator actions ----
    def approve(self, user_id: str, name: str = "", ip: str = "") -> None:
        pend = self.pending.pop(user_id, None)
        if pend:
            name = name or pend["name"]
            ip = ip or pend["ip"]
        entry = self.approved.setdefault(user_id, {"name": name, "ips": [], "approvedAt": utc_now_str()})
        if name:
            entry["name"] = name
        if ip and ip not in entry.setdefault("ips", []):
            entry["ips"].append(ip)
        if pend and not pend["future"].done():
            pend["future"].set_result(True)
        self.log.info("Access: approved %s (%s)", user_id, entry.get("name", ""))
        self._changed()

    def deny(self, user_id: str) -> None:
        pend = self.pending.pop(user_id, None)
        if pend and not pend["future"].done():
            pend["future"].set_result(False)

    def remove_approved(self, user_id: str) -> None:
        if self.approved.pop(user_id, None) is not None:
            self._changed()

    def ban(self, user_id: str = "", ip: str = "", name: str = "", reason: str = "",
            duration: float | None = None, actor: dict[str, Any] | None = None) -> None:
        stamp = {"name": name, "reason": reason, "at": utc_now_str(), **self._expiry(duration)}
        if user_id:
            self.banned_ids[user_id] = dict(stamp)
            self.approved.pop(user_id, None)
            self.deny(user_id)
        if ip:
            self.banned_ips[ip] = dict(stamp)
        self.log.info("Access: banned id=%s ip=%s", user_id or "-", ip or "-")
        self.audit.write("ban", actor=actor, targetId=user_id or None, targetIp=ip or None, name=name or None,
                         reason=reason or None, expiresAt=stamp.get("expiresAt"))
        self._changed()

    def unban(self, user_id: str = "", ip: str = "", actor: dict[str, Any] | None = None) -> None:
        if user_id:
            self.banned_ids.pop(user_id, None)
        if ip:
            self.banned_ips.pop(ip, None)
        self.audit.write("unban", actor=actor, targetId=user_id or None, targetIp=ip or None)
        self._changed()

    def mute(self, user_id: str, name: str = "", reason: str = "", duration: float | None = None,
             actor: dict[str, Any] | None = None) -> None:
        self.muted_ids[user_id] = {"name": name, "reason": reason, "at": utc_now_str(), **self._expiry(duration)}
        self.log.info("Access: muted id=%s", user_id)
        self.audit.write("mute", actor=actor, targetId=user_id, name=name or None, reason=reason or None,
                         expiresAt=self.muted_ids[user_id].get("expiresAt"))
        self._changed()

    def unmute(self, user_id: str, actor: dict[str, Any] | None = None) -> None:
        self.muted_ids.pop(user_id, None)
        self.audit.write("unmute", actor=actor, targetId=user_id)
        self._changed()

    def is_muted(self, user_id: str) -> bool:
        entry = self.muted_ids.get(user_id)
        if entry is None:
            return False
        if self._live(entry):
            return True
        self.prune_expired()
        return False

    def set_mode(self, mode: str | None = None, pin_ips: bool | None = None) -> None:
        if mode is not None:
            if mode not in ACCESS_MODES:
                raise ValueError(f"mode must be one of {', '.join(ACCESS_MODES)}")
            self.mode = mode
            if mode == "open":
                for uid in list(self.pending):
                    self.approve(uid)  # going open admits whoever was waiting
        if pin_ips is not None:
            self.pin_ips = bool(pin_ips)
        self._changed()

    def snapshot(self) -> dict[str, Any]:
        self._expire_pending()
        self.prune_expired()
        return {
            "mode": self.mode,
            "pinIps": self.pin_ips,
            "approvalWaitSeconds": self.approval_wait_seconds,
            "pending": [
                {k: v for k, v in p.items() if k not in ("future", "monotonic")}
                | {"waiting": not p["future"].done()}
                for p in self.pending.values()
            ],
            "approved": [{"id": k, **v} for k, v in sorted(self.approved.items())],
            "bannedIds": [{"id": k, **v} for k, v in sorted(self.banned_ids.items())],
            "bannedIps": [{"ip": k, **v} for k, v in sorted(self.banned_ips.items())],
            "mutedIds": [{"id": k, **v} for k, v in sorted(self.muted_ids.items())],
            "deniedTotal": self.denied_total,
        }


def redact_protocol_trace_payload(value: Any, *, message_type: str) -> Any:
    """Retain full protocol shapes without persisting credentials."""
    secret_names = SECRET_FIELD_NAMES
    if isinstance(value, dict):
        password_update = str(value.get("key", "")).lower() == "password"
        return {
            key: "<redacted>" if (str(key).lower() in secret_names
                                  or str(key).lower() == "key" and message_type == "Authorization"
                                  or password_update and str(key).lower() in {"value", "val", "data"})
            else redact_protocol_trace_payload(item, message_type=message_type)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_protocol_trace_payload(item, message_type=message_type) for item in value]
    return value


class ProtocolTraceWriter:
    """Opt-in untruncated protocol event archive, independent of UI history."""

    def __init__(self, path: str, run_id: str) -> None:
        self.path = path
        self.run_id = run_id
        self.lines_written = 0
        self._fh = open(path, "a", encoding="utf-8", newline="\n")
        self._write({"format": "bzr-protocol-trace", "version": 1, "runId": run_id})

    def _write(self, record: dict[str, Any]) -> None:
        self._fh.write(json.dumps(record, separators=(",", ":")) + "\n")
        self._fh.flush()
        self.lines_written += 1

    def log(self, entry: dict[str, Any], payload: Any) -> None:
        redacted = redact_protocol_trace_payload(payload, message_type=entry["type"])
        self._write({**entry, "runId": self.run_id, "tsUnixNs": time.time_ns(),
                     "preview": compact_preview(redacted), "payload": redacted})

    def close(self) -> None:
        self._fh.close()


class ServerState:
    def __init__(
        self,
        logger: logging.Logger,
        *,
        state_file: str | None = None,
        admin_key: str = "",
        admin_ids: set[str] | None = None,
        protocol_trace: ProtocolTraceWriter | None = None,
        audit: AuditLog | None = None,
    ) -> None:
        self.log = logger
        self.audit = audit or AuditLog()
        if admin_key:
            self.audit.secrets.add(admin_key)
        self.next_lobby_id = 1000
        self.sessions_by_ws: dict[Any, UserSession] = {}
        self.sessions_by_user_id: dict[str, UserSession] = {}
        self.lobbies: dict[int, LobbyState] = {}
        self.state_file = Path(state_file).expanduser() if state_file else None
        self.admin_key = admin_key
        self.admin_ids = admin_ids or set()
        self.whitelist = json.loads(json.dumps(DEFAULT_WHITELIST))
        self.log_level_setting = 1
        self.relay_requested = False
        self.test_mode = False
        self.access = AccessControl(logger.getChild("access"), self.mark_dirty)
        self.access.audit = self.audit
        self.stdout_history: list[str] = []
        self.protocol_history: list[dict[str, Any]] = []
        self.protocol_trace = protocol_trace
        self._dirty = False

    def allocate_lobby_id(self) -> int:
        lid = self.next_lobby_id
        self.next_lobby_id += 1
        self.mark_dirty()
        return lid

    def build_user_snapshot(self, s: UserSession) -> dict[str, Any]:
        snapshot = {
            "id": s.user_id,
            "userId": s.user_id,
            "name": s.name,
            "realname": s.name,
            "playerName": s.player_name,
            "authType": s.authtype,
            "clientVersion": s.client_version,
            "inLounge": s.in_lounge,
            "isInLounge": s.in_lounge,
            "isAdmin": s.is_admin,
            # Client dispatcher (0x006BF2A0) reads isAuth off the user object.
            "isAuth": True,
            "lobby": -1 if s.current_lobby_id is None else s.current_lobby_id,
        }
        if s.wan_address:
            snapshot["wanAddress"] = s.wan_address
            snapshot["ipAddress"] = s.wan_address.split(":")[0]
        if s.lan_addresses:
            snapshot["lanAddresses"] = list(s.lan_addresses)
        # Per-player game values (ready, team, miniid, vehicle, launched, ...)
        # live only under "metadata" as strings; the official server does not
        # flatten them onto the user record.
        snapshot["metadata"] = {k: "" if v is None else str(v) for k, v in s.extra_data.items()}
        return snapshot

    def serialize_lobby(self, lobby: LobbyState) -> dict[str, Any]:
        # The client reads name/isChat/lobbyId directly off the lobby object
        # (dispatcher 0x006BF2A0). name/isChat aren't stored as struct fields;
        # BZR encodes them in the lobby "name" metadata (e.g. "~chat~pub~~..."
        # vs "~game~pub~~..."), so surface them from metadata here.
        name = str(lobby.metadata.get("name", ""))
        return {
            "id": lobby.lobby_id,
            "lobbyId": lobby.lobby_id,
            "name": name,
            "isChat": not self.is_game_lobby(lobby),
            # Official admin page (scripts/bzdatagrids.js) renders owner == -1 as
            # "none"; an ownerless/restored lobby must therefore serialize -1,
            # not "".
            "owner": lobby.owner if lobby.owner else -1,
            "createdTime": lobby.created_time,
            "metadata": lobby.metadata,
            "memberLimit": lobby.member_limit,
            "isPrivate": lobby.is_private,
            "isLocked": lobby.is_locked,
            "password": lobby.password,
            "userCount": len(lobby.users),
            "clientVersion": lobby.metadata.get("clientVersion", "2.2.301"),
            "users": lobby.users,
        }

    def serialize_lobbies(self) -> dict[str, dict[str, Any]]:
        return {str(lid): self.serialize_lobby(lobby) for lid, lobby in self.lobbies.items()}

    def serialize_users(self) -> dict[str, dict[str, Any]]:
        return {uid: self.build_user_snapshot(s) for uid, s in self.sessions_by_user_id.items()}

    # --- Address redaction (--redact-addresses) ---------------------------
    # Upstream sends every user's WAN/LAN address to every session. The stock
    # client only needs a peer's address once both sit in the same game lobby
    # (OnLobbyMemberP2PConnect/OnP2PRoute carry none; ipAddress is never read),
    # so redaction omits the keys for everyone else. Keys are omitted rather
    # than blanked so a redacted copy can't overwrite a good cached value.
    ADDRESS_KEYS = ("wanAddress", "ipAddress", "lanAddresses")
    SOURCE_ADDRESS_KEYS = ("sourceWanAddress", "sourceLanAddresses")

    @staticmethod
    def is_game_lobby(lobby: LobbyState) -> bool:
        return not str(lobby.metadata.get("name", "")).startswith("~chat~")

    def shares_game_lobby(self, viewer: UserSession, subject_uid: str) -> bool:
        if viewer.current_lobby_id is None:
            return False
        lobby = self.lobbies.get(viewer.current_lobby_id)
        return (
            lobby is not None
            and self.is_game_lobby(lobby)
            and viewer.user_id in lobby.users
            and subject_uid in lobby.users
        )

    def can_see_addresses(self, viewer: UserSession | None, subject_uid: str) -> bool:
        if viewer is None or not subject_uid:
            return False
        return viewer.is_admin or viewer.user_id == subject_uid or self.shares_game_lobby(viewer, subject_uid)

    def redact_for(self, viewer: UserSession | None, data: Any) -> Any:
        """Copy of an outgoing payload with addresses the viewer may not see removed."""
        if isinstance(data, list):
            return [self.redact_for(viewer, x) for x in data]
        if not isinstance(data, dict):
            return data
        hidden: set[str] = set()
        if any(k in data for k in self.ADDRESS_KEYS):
            subject = str(data.get("userId") or data.get("id") or "")
            if not self.can_see_addresses(viewer, subject):
                hidden.update(self.ADDRESS_KEYS)
        if any(k in data for k in self.SOURCE_ADDRESS_KEYS):
            if not self.can_see_addresses(viewer, str(data.get("sourceUserId") or "")):
                hidden.update(self.SOURCE_ADDRESS_KEYS)
        return {k: self.redact_for(viewer, v) for k, v in data.items() if k not in hidden}

    def metrics(self) -> dict[str, Any]:
        return {
            "activeConnections": len(self.sessions_by_ws),
            "activeUsers": len(self.sessions_by_user_id),
            "lobbyCount": len(self.lobbies),
            "nextLobbyId": self.next_lobby_id,
            "stdoutHistoryCount": len(self.stdout_history),
            "protocolHistoryCount": len(self.protocol_history),
            "whitelistEntries": len(self.whitelist.get("list", [])),
            "relayRequested": self.relay_requested,
            "testMode": self.test_mode,
            "logLevelSetting": self.log_level_setting,
            "stateDirty": self._dirty,
            "accessMode": self.access.mode,
            "accessPending": sum(1 for p in self.access.pending.values() if not p["future"].done()),
            "bansActive": len(self.access.banned_ids) + len(self.access.banned_ips),
            "mutesActive": len(self.access.muted_ids),
            "auditLogRecords": self.audit.records_written,
        }

    def record_stdout_entry(self, level: str, message: str) -> str:
        entry = f"[{utc_now_str()}] {str(level).upper()}: {message}"
        self.stdout_history.append(entry)
        if len(self.stdout_history) > MAX_STDOUT_HISTORY:
            self.stdout_history = self.stdout_history[-MAX_STDOUT_HISTORY:]
        return entry

    def record_protocol_event(
        self,
        *,
        channel: str,
        direction: str,
        peer: str,
        msg_type: str,
        payload: Any,
        decoder: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        entry = {
            "time": utc_now_str(),
            "channel": channel,
            "direction": direction,
            "peer": peer,
            "type": msg_type,
            "preview": compact_preview(payload),
        }
        if decoder:
            entry["decoder"] = decoder
        if self.protocol_trace is not None:
            self.protocol_trace.log(entry, payload)
        self.protocol_history.append(entry)
        if len(self.protocol_history) > MAX_PROTOCOL_HISTORY:
            self.protocol_history = self.protocol_history[-MAX_PROTOCOL_HISTORY:]
        return entry

    def mark_dirty(self) -> None:
        self._dirty = True

    def load_state(self, retain_empty_lobbies: bool = False) -> None:
        if not self.state_file:
            return
        if not self.state_file.exists():
            self.log.info("State file not found (fresh start): %s", self.state_file)
            return

        try:
            payload = json.loads(self.state_file.read_text(encoding="utf-8"))
            version = int(payload.get("version", 0))
            if version != STATE_FORMAT_VERSION:
                self.log.warning(
                    "Unsupported state format version %s (expected %s). Ignoring state file.",
                    version,
                    STATE_FORMAT_VERSION,
                )
                return

            self.next_lobby_id = int(payload.get("nextLobbyId", self.next_lobby_id))
            whitelist = payload.get("whitelist")
            if isinstance(whitelist, dict) and isinstance(whitelist.get("list"), list):
                self.whitelist = {
                    "list": [str(x) for x in whitelist.get("list", []) if isinstance(x, str)],
                    "major": int(whitelist.get("major", DEFAULT_WHITELIST["major"])),
                    "minor": int(whitelist.get("minor", DEFAULT_WHITELIST["minor"])),
                }
            self.log_level_setting = int(payload.get("logLevelSetting", self.log_level_setting))
            self.relay_requested = bool(payload.get("relayRequested", self.relay_requested))
            self.test_mode = bool(payload.get("testMode", self.test_mode))
            self.access.load_payload(payload.get("access"))
            self.lobbies = {}
            dropped = 0
            for raw_lobby in payload.get("lobbies", []):
                try:
                    lid = int(raw_lobby.get("id"))
                except Exception:
                    continue

                # Users are not restored; sessions are runtime-only.
                lobby = LobbyState(
                    lobby_id=lid,
                    owner=str(raw_lobby.get("owner", "") or ""),
                    metadata=dict(raw_lobby.get("metadata", {})),
                    member_limit=int(raw_lobby.get("memberLimit", 20000)),
                    is_private=bool(raw_lobby.get("isPrivate", False)),
                    is_locked=bool(raw_lobby.get("isLocked", False)),
                    password=str(raw_lobby.get("password", "")),
                    created_time=str(raw_lobby.get("createdTime", utc_now_str())),
                    users={},
                    persistent=bool(raw_lobby.get("persistent", False)),
                )
                # Members are never restored, and empty-lobby removal only runs
                # when a member leaves, so a restored game lobby would linger
                # forever (e.g. a stale full "2/2" game shadowing a new one of
                # the same name). Drop it unless empty lobbies are retained.
                if not (lobby.persistent or retain_empty_lobbies):
                    dropped += 1
                    continue
                self.lobbies[lid] = lobby

            self._dirty = False
            if dropped:
                self.mark_dirty()
            self.log.info(
                "Loaded state: %d lobbies (%d memberless non-persistent dropped), nextLobbyId=%d from %s",
                len(self.lobbies),
                dropped,
                self.next_lobby_id,
                self.state_file,
            )
        except Exception:
            self.log.exception("Failed to load state file: %s", self.state_file)

    DEFAULT_LOBBY_NAME = "~chat~pub~~default"

    @classmethod
    def _is_default_lobby(cls, lobby: LobbyState) -> bool:
        """Exact, normalized identification of the canonical default room.

        Substring matching would wrongly promote user-created rooms such as
        '~chat~pub~~Default Maps Discussion' to undeletable persistent status.
        """
        return str(lobby.metadata.get("name", "")).strip().lower() == cls.DEFAULT_LOBBY_NAME

    def ensure_default_lobby(self) -> None:
        """Seed the persistent ~chat~pub~~default chat lobby.

        Raw paired stock-client captures (2026-03-21) show every stock client
        joins ~chat~pub~~default immediately after entering the lounge and
        before creating/joining any game lobby; the official service keeps this
        bridge-owned room in existence at all times. Mirror that so the stock
        first-join flow works against this server. Only an EXACT canonical name
        match qualifies (see _is_default_lobby).
        """
        for lobby in self.lobbies.values():
            if self._is_default_lobby(lobby):
                lobby.persistent = True
                return
        lid = self.allocate_lobby_id()
        lobby = LobbyState(
            lobby_id=lid,
            owner="B1000001",
            member_limit=20000,
            metadata={
                "name": self.DEFAULT_LOBBY_NAME,
                "gameType": "1",
                "gameSettings": "*",
                "GameVersion": "2.2.301",
                "userCount": "0",
                "userPack": "!BRIDGE                         ",
            },
            persistent=True,
        )
        self.lobbies[lid] = lobby
        self.mark_dirty()
        self.log.info("Seeded persistent default chat lobby %d (~chat~pub~~default)", lid)

    def _state_payload(self) -> dict[str, Any]:
        persisted_lobbies = []
        for lid, lobby in self.lobbies.items():
            persisted_lobbies.append(
                {
                    "id": lid,
                    "owner": lobby.owner,
                    "metadata": lobby.metadata,
                    "memberLimit": lobby.member_limit,
                    "isPrivate": lobby.is_private,
                    "isLocked": lobby.is_locked,
                    "password": lobby.password,
                    "createdTime": lobby.created_time,
                    "persistent": lobby.persistent,
                }
            )
        return {
            "version": STATE_FORMAT_VERSION,
            "savedAt": utc_now_str(),
            "nextLobbyId": self.next_lobby_id,
            "whitelist": self.whitelist,
            "logLevelSetting": self.log_level_setting,
            "relayRequested": self.relay_requested,
            "testMode": self.test_mode,
            "access": self.access.to_payload(),
            "lobbies": persisted_lobbies,
        }

    def save_state(self, *, force: bool = False) -> None:
        if not self.state_file:
            return
        if not force and not self._dirty:
            return

        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            tmp_file = self.state_file.with_suffix(self.state_file.suffix + ".tmp")
            tmp_file.write_text(json.dumps(self._state_payload(), indent=2), encoding="utf-8")
            tmp_file.replace(self.state_file)
            self._dirty = False
            self.log.debug("State saved to %s", self.state_file)
        except Exception:
            self.log.exception("Failed to save state file: %s", self.state_file)


class BZRUDPProtocol(asyncio.DatagramProtocol):
    def __init__(self, logger: logging.Logger, state: ServerState) -> None:
        self.logger = logger
        self.state = state
        self.transport: asyncio.DatagramTransport | None = None
        self.rx_count = 0
        self.tx_count = 0

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]
        self.logger.info("UDP shim listening on %s", transport.get_extra_info("sockname"))

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self.rx_count += 1
        hex_data = data.hex()
        decoder = decode_bzr_udp_datagram(data)
        self.state.record_protocol_event(
            channel="udp",
            direction="rx",
            peer=f"{addr[0]}:{addr[1]}",
            msg_type="udp_datagram",
            payload=hex_data,
            decoder=decoder,
        )
        self.logger.debug("UDP RX <- %s:%d | %s", addr[0], addr[1], hex_data)

        reply = self._build_reply(data, addr)
        if reply and self.transport:
            reply_decoder = decode_bzr_udp_datagram(reply)
            self.transport.sendto(reply, addr)
            self.tx_count += 1
            self.state.record_protocol_event(
                channel="udp",
                direction="tx",
                peer=f"{addr[0]}:{addr[1]}",
                msg_type="udp_datagram",
                payload=reply.hex(),
                decoder=reply_decoder,
            )
            self.logger.debug("UDP TX -> %s:%d | %s", addr[0], addr[1], reply.hex())

    @staticmethod
    def _build_reply(data: bytes, addr: tuple[str, int]) -> bytes:
        # Live-verified against the official service (2026-08-24): UDP 1338 echoes
        # ANY received datagram verbatim and appends a 7-byte trailer:
        # 0x04 + observed IPv4 (network order) + observed UDP port (big-endian).
        # Empty datagrams get a trailer-only 7-byte reply.
        try:
            ip_bytes = socket.inet_aton(addr[0])
            port_bytes = int(addr[1]).to_bytes(2, "big")
            return data + b"\x04" + ip_bytes + port_bytes
        except (OSError, OverflowError):
            return data

    def error_received(self, exc: Exception) -> None:
        self.logger.warning("UDP error: %s", exc)


def _parse_ipv4_endpoint(value: str) -> tuple[str, int] | None:
    text = str(value or "").strip()
    host, sep, port_text = text.rpartition(":")
    if not sep or not host or not port_text:
        return None
    try:
        socket.inet_aton(host)
        port = int(port_text)
    except (OSError, ValueError):
        return None
    if not (1 <= port <= 65535):
        return None
    return host, port


@dataclass
class RelayRoute:
    lobby_id: int
    user_a: str
    user_b: str
    endpoint_a: tuple[str, int]
    endpoint_b: tuple[str, int]
    observed_a: tuple[str, int] | None = None
    observed_b: tuple[str, int] | None = None
    created_monotonic: float = field(default_factory=time.monotonic)
    last_activity_monotonic: float = field(default_factory=time.monotonic)


def _is_reliable_nonfinal_fragment(data: bytes) -> bool:
    """True when a datagram is a reliable BZRNet fragment that is not the final one.

    PROVEN-native flag semantics (BZRNET_PROTOCOL_ARCHAEOLOGY_20260825.md Â§4.1):
    bit 0x80 marks reliable traffic; bit 0x40 is added only on the final
    fragment (or on single-packet unreliable sends).  A bare 0x80 therefore
    identifies a middle reliable fragment.  This predicate is the ONLY packet
    interpretation performed by fault injection.
    """
    return (
        len(data) >= BZR_TRANSPORT_HEADER_BYTES
        and bool(data[0] & 0x80)
        and not bool(data[0] & 0x40)
    )


class RelayFaultInjector:
    """Opt-in Phase-2 impairment hook for the UDP relay (default OFF).

    Contract: BZRNET_STOCK_CLIENT_INTEROP_MATRIX_20260825.md Â§4.  The single
    supported mode drops the k-th bare-0x80 (reliable, non-final) datagram per
    directed peer stream.  It never rewrites, reorders, reassembles, or
    retransmits anything; when inactive it is not consulted at all so the hot
    path stays byte-transparent.
    """

    MODES = ("drop_reliable_midfrag",)

    def __init__(self, spec: str) -> None:
        spec = (spec or "").strip()
        if not spec:
            raise ValueError("empty fault-injection spec")
        mode_part, _, param_part = spec.partition(":")
        mode = mode_part.strip()
        if mode not in self.MODES:
            raise ValueError(f"unsupported fault-injection mode {mode!r}; known: {self.MODES}")
        params: dict[str, str] = {}
        for chunk in param_part.split(","):
            if not chunk:
                continue
            key, sep, value = chunk.partition("=")
            if not sep:
                raise ValueError(f"fault-injection parameter {chunk!r} must be key=value")
            params[key.strip()] = value.strip()
        if "k" not in params:
            raise ValueError("fault-injection requires k=<ordinal>")
        k = int(params["k"])
        if k <= 0:
            raise ValueError("fault-injection k must be >= 1")
        self.mode = mode
        self.k = k
        self.observed_count = 0
        self.forwarded_count = 0
        self.dropped_count = 0
        self._stream_match_ordinals: dict[tuple[str, int, str, int], int] = {}
        self.last_dropped: dict[str, Any] | None = None

    @property
    def active(self) -> bool:
        return True

    def decide_drop(self, data: bytes, stream_key: tuple[str, int, str, int]) -> bool:
        """Return True exactly once per stream, on the k-th matching fragment.

        Sole accounting point: every offered datagram increments ``observed``;
        passing datagrams increment ``forwarded``, a selected one ``dropped``.
        """
        self.observed_count += 1
        if not _is_reliable_nonfinal_fragment(data):
            self.forwarded_count += 1
            return False
        ordinal = self._stream_match_ordinals.get(stream_key, 0) + 1
        self._stream_match_ordinals[stream_key] = ordinal
        if ordinal == self.k:
            self.dropped_count += 1
            self.last_dropped = {
                "streamKey": stream_key,
                "matchOrdinal": ordinal,
                "seqA": int.from_bytes(data[10:14], "big"),
                "seqB": int.from_bytes(data[14:18], "big"),
                "payloadBytes": max(0, len(data) - BZR_TRANSPORT_HEADER_BYTES),
            }
            return True
        self.forwarded_count += 1
        return False

    def metrics(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "k": self.k,
            "active": True,
            "observed": self.observed_count,
            "forwarded": self.forwarded_count,
            "dropped": self.dropped_count,
            "lastDropped": self.last_dropped,
        }


class RelayTraceWriter:
    """Forensic JSONL trace of relayed datagrams (opt-in; local-only output).

    One line per routed datagram with transport-envelope fields only.  Payload
    bytes are omitted by default; opt-in maximum capture copies complete
    datagram bytes as hex without parsing them.  Traces may
    contain real endpoints; they live beside runtime state and must never be
    committed raw.
    """

    FORMAT_NAME = "bzr-relay-trace"
    FORMAT_VERSION = 2

    def __init__(self, path: str, run_id: str, *, include_payloads: bool = False) -> None:
        self.path = path
        self.run_id = run_id
        self.lines_written = 0
        self.include_payloads = bool(include_payloads)
        self.trace_id = uuid.uuid4().hex
        self._packet_sequence = 0
        self._socket_sequence = 0
        self._socket_ids: weakref.WeakKeyDictionary[Any, str] = weakref.WeakKeyDictionary()
        self.closed = False
        self._fh = open(path, "a", encoding="utf-8", newline="\n")
        self._write_line(
            {
                "format": self.FORMAT_NAME,
                "version": self.FORMAT_VERSION,
                "runId": run_id,
                "payloadCapture": self.include_payloads,
                "traceId": self.trace_id,
                "sendOutcomeCapture": True,
            }
        )

    def _write_line(self, record: dict[str, Any]) -> None:
        self._fh.write(json.dumps(record, separators=(",", ":")) + "\n")
        self._fh.flush()
        self.lines_written += 1

    def socket_id(self, transport: asyncio.DatagramTransport) -> str:
        identity = self._socket_ids.get(transport)
        if identity is None:
            self._socket_sequence += 1
            identity = f"{self.trace_id}:socket:{self._socket_sequence}"
            self._socket_ids[transport] = identity
        return identity

    def log(
        self,
        *,
        direction: str,
        source: tuple[str, int],
        target: tuple[str, int],
        data: bytes,
        disposition: str,
        match_ordinal: int | None = None,
        route: dict[str, Any] | None = None,
        relay_endpoint: tuple[str, int] | None = None,
        impairment: dict[str, Any] | None = None,
        send_context: dict[str, Any] | None = None,
    ) -> str:
        self._packet_sequence += 1
        packet_id = f"{self.trace_id}:{self._packet_sequence}"
        record: dict[str, Any] = {
            "packetId": packet_id,
            "runId": self.run_id,
            "tsMonotonic": round(time.monotonic(), 6),
            "tsUnixMs": int(time.time() * 1000),
            "tsUnixNs": time.time_ns(),
            "direction": direction,
            "source": f"{source[0]}:{source[1]}",
            "target": f"{target[0]}:{target[1]}",
            "disposition": disposition,
            "datagramSha256": hashlib.sha256(data).hexdigest(),
        }
        if route is not None:
            record["route"] = route
        if relay_endpoint is not None:
            record["relayEndpoint"] = f"{relay_endpoint[0]}:{relay_endpoint[1]}"
        if self.include_payloads:
            record["datagramHex"] = data.hex()
        header_hex = data[:18].hex() if len(data) >= 18 else ""
        record["transport"] = {
            "flagsByte": f"0x{data[0]:02x}" if data else None,
            "kindNibble": (data[1] & 0x0F) if len(data) >= 2 else None,
            "headerTimestampUnixMs": int.from_bytes(data[2:10], "big") if len(data) >= 10 else None,
            "seqA": int.from_bytes(data[10:14], "big") if len(data) >= 14 else None,
            "seqB": int.from_bytes(data[14:18], "big") if len(data) >= 18 else None,
            "headerBytes": len(header_hex) // 2,
            "payloadBytes": max(0, len(data) - BZR_TRANSPORT_HEADER_BYTES),
            "wireBytes": len(data),
            "headerHex": header_hex,
        }
        if match_ordinal is not None:
            record["matchOrdinal"] = match_ordinal
        if impairment is not None:
            record["impairment"] = impairment
        if send_context is not None:
            record.update(send_context)
        self._write_line(record)
        return packet_id

    def event(self, name: str, **fields: Any) -> None:
        # Delayed sends and asynchronous socket errors may outlive capture.
        # Their outcomes remain unknown in the closed trace.
        if self.closed:
            return
        self._write_line({"runId": self.run_id, "tsMonotonic": round(time.monotonic(), 6),
                          "tsUnixMs": int(time.time() * 1000), "tsUnixNs": time.time_ns(),
                          "event": name, **fields})

    def metrics(self) -> dict[str, Any]:
        return {
            "enabled": True,
            "path": self.path,
            "linesWritten": self.lines_written,
            "runId": self.run_id,
        }

    def close(self) -> None:
        if self.closed:
            return
        try:
            self._fh.flush()
        finally:
            self.closed = True
            self._fh.close()


class BZRPairRelayProtocol(asyncio.DatagramProtocol):
    """A dedicated socket supplies the peer-pair association, not the payload."""

    def __init__(self, owner: BZRRelayProtocol, route_key: tuple[str, str]) -> None:
        self.owner = owner
        self.route_key = route_key
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self.owner.datagram_received(data, addr, route_key=self.route_key, pair_transport=self.transport)

    def error_received(self, exc: Exception) -> None:
        self.owner.error_received(exc, route_key=self.route_key, transport=self.transport)


class BZRRelayProtocol(asyncio.DatagramProtocol):
    """Transparent datagram relay for WS-associated peer pairs.

    This intentionally does not deserialize gameplay payloads.  The official
    1339 allocation/association envelope is not yet recovered, so the community
    fallback associates routes from DoP2PRoute plus each user's known WAN
    endpoint.  If an endpoint maps to more than one candidate peer, forwarding
    stops rather than guessing and risking cross-session delivery.
    The opt-in pair-port mode allocates a separate UDP socket for each route,
    allowing the same endpoint to participate in several opaque peer streams.
    """

    def __init__(
        self,
        logger: logging.Logger,
        state: ServerState,
        *,
        route_ttl_seconds: int = DEFAULT_RELAY_ROUTE_TTL_SECONDS,
        fault_injector: RelayFaultInjector | None = None,
        trace_writer: RelayTraceWriter | None = None,
        impairment: RelayImpairment | None = None,
        pair_ports: bool = False,
        pair_port_start: int = 0,
        pair_port_end: int = 0,
        reserved_ports: set[int] | None = None,
    ) -> None:
        self.logger = logger
        self.state = state
        self.route_ttl_seconds = max(15, int(route_ttl_seconds))
        self.transport: asyncio.DatagramTransport | None = None
        self.listen_port = DEFAULT_RELAY_PORT
        self.routing_enabled = True
        self.routes: dict[tuple[str, str], RelayRoute] = {}
        self.pair_ports = bool(pair_ports)
        self.pair_port_start = int(pair_port_start)
        self.pair_port_end = int(pair_port_end)
        if (self.pair_port_start, self.pair_port_end) != (0, 0) and not (
                1 <= self.pair_port_start <= self.pair_port_end <= 65535):
            raise ValueError("relay pair port bounds must both be 0 (ephemeral) or an inclusive range within 1-65535")
        self.reserved_ports = set(reserved_ports or ())
        self._pair_port_cursor = self.pair_port_start
        self.pair_port_allocation_failures = 0
        self.shared_route_warnings = 0
        self._last_ambiguity_warning = float("-inf")
        self.pair_transports: dict[tuple[str, str], asyncio.DatagramTransport] = {}
        self._allocation_lock = asyncio.Lock()
        self.rx_count = 0
        self.tx_count = 0
        self.drop_count = 0
        self.ambiguous_drop_count = 0
        self.unmapped_drop_count = 0
        self.expired_route_count = 0
        # Phase-2 instrumentation. Both default to None: when absent the hot
        # path below is unchanged byte-transparent forwarding.
        self.fault_injector = fault_injector
        self.trace_writer = trace_writer
        # Opt-in network impairment for private test runs (relay_impairment.py).
        # None keeps the hot path immediate, byte-transparent forwarding.
        self.impairment = impairment
        self.impair_drop_count = 0
        self.impair_late_drop_count = 0
        # In-order delayed datagrams per (socket, target). Their deadlines are
        # monotonic per stream, but a burst often shares one deadline and the
        # event loop's timer heap does not keep equal deadlines in FIFO order;
        # every timer therefore sends the oldest pending datagram of its stream.
        self._impair_fifo: dict[tuple[int, tuple[str, int]], collections.deque] = {}

    def schedule_later(self, delay: float, callback: Any, *args: Any) -> None:
        asyncio.get_running_loop().call_later(delay, callback, *args)

    def _schedule_in_order(self, delay: float, args: tuple) -> None:
        key = (id(args[0]), args[2])
        self._impair_fifo.setdefault(key, collections.deque()).append(args)
        self.schedule_later(delay, self._send_impaired_head, key)

    def _send_impaired_head(self, key: tuple[int, tuple[str, int]]) -> None:
        fifo = self._impair_fifo.get(key)
        if not fifo:
            return
        args = fifo.popleft()
        if not fifo:
            del self._impair_fifo[key]
        self._send_impaired(*args)

    def set_impairment(self, spec_text: str) -> dict[str, Any]:
        """Apply a new impairment spec, or clear it with an empty one. Raises ValueError."""
        spec_text = (spec_text or "").strip()
        previous = self.impairment
        self.impairment = RelayImpairment(parse_impairment_spec(spec_text)) if spec_text else None
        if self.impairment is not None:
            self.logger.warning("RELAY IMPAIRMENT applied: %s -- private test runs only",
                                self.impairment.spec.describe())
        elif previous is not None:
            self.logger.warning("RELAY IMPAIRMENT cleared (was %s)", previous.spec.describe())
        self._impairment_event("impairment_set" if self.impairment else "impairment_cleared", previous)
        return self.impairment_metrics()

    def impairment_metrics(self) -> dict[str, Any]:
        return self.impairment.metrics() if self.impairment else {"active": False, "spec": ""}

    def _impairment_event(self, name: str, previous: RelayImpairment | None) -> None:
        if self.trace_writer is not None:
            self.trace_writer.event(name, spec=self.impairment.spec.describe() if self.impairment else "",
                                    previous=previous.metrics() if previous else None)

    def _user_labels(self, user_id: str) -> set[str]:
        labels = {user_id}
        session = self.state.sessions_by_user_id.get(user_id)
        if session is not None:
            labels.update(x for x in (session.name, session.player_name) if x)
        return labels

    def _send_impaired(self, transport: asyncio.DatagramTransport, data: bytes, target: tuple[str, int],
                       send_context: dict[str, Any] | None = None) -> None:
        if transport.is_closing():
            self.impair_late_drop_count += 1
            self._trace_send_outcome(send_context, "skipped_closing", transport)
            return
        self._send_relay(transport, data, target, send_context)
        self.tx_count += 1

    def _trace_send_outcome(self, context: dict[str, Any] | None, outcome: str,
                            transport: asyncio.DatagramTransport, exc: Exception | None = None,
                            attempt_monotonic: float | None = None) -> None:
        if self.trace_writer is None or self.trace_writer.closed or context is None:
            return
        fields = dict(context)
        received = fields.pop("receivedMonotonic")
        scheduled = fields.pop("scheduledMonotonic")
        now = time.monotonic()
        attempt = now if attempt_monotonic is None else attempt_monotonic
        closing = getattr(transport, "is_closing", None)
        fields.update(outcome=outcome, queueWaitMs=round((attempt - received) * 1000, 3),
                      schedulerLagMs=round(max(0.0, attempt - scheduled) * 1000, 3),
                      sendCallMs=round((now - attempt) * 1000, 3),
                      transportClosing=closing() if closing else None)
        if exc is not None:
            fields.update(errorType=type(exc).__name__, error=str(exc))
        # Deliberately no transport/disposition: legacy replay consumes only
        # the existing decision rows, never these socket submission events.
        self.trace_writer.event("socket_send", **fields)

    def _send_relay(self, transport: asyncio.DatagramTransport, data: bytes,
                    target: tuple[str, int], context: dict[str, Any] | None = None) -> None:
        if context is None:
            transport.sendto(data, target)
            return
        attempt = time.monotonic()
        try:
            transport.sendto(data, target)
        except Exception as exc:
            self._trace_send_outcome(context, "exception", transport, exc, attempt)
            raise
        self._trace_send_outcome(context, "submitted", transport, attempt_monotonic=attempt)

    @property
    def enabled(self) -> bool:
        return self.routing_enabled and self.transport is not None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]
        sockname = transport.get_extra_info("sockname")
        if isinstance(sockname, tuple) and len(sockname) >= 2:
            try:
                self.listen_port = int(sockname[1])
            except Exception:
                pass
        self.logger.info("UDP relay listening on %s", sockname)

    def set_enabled(self, enabled: bool) -> None:
        self.routing_enabled = bool(enabled)
        if not self.routing_enabled:
            for key in list(self.routes):
                self._remove_route(key)

    def _remove_route(self, key: tuple[str, str]) -> None:
        removed = self.routes.pop(key, None)
        transport = self.pair_transports.pop(key, None)
        if removed is not None and self.trace_writer is not None:
            self.trace_writer.event("route_removed", lobbyId=removed.lobby_id, users=list(key),
                                    relayEndpoint=transport.get_extra_info("sockname") if transport else None)
        if transport is not None:
            transport.close()

    def close(self) -> None:
        self.set_enabled(False)
        if self.transport is not None:
            self.transport.close()
            self.transport = None

    def metrics(self) -> dict[str, Any]:
        self._expire_routes()
        return {
            "enabled": self.enabled,
            "listenPort": self.listen_port if self.transport is not None else None,
            "activeRoutes": len(self.routes),
            "packetsRxTotal": self.rx_count,
            "packetsTxTotal": self.tx_count,
            "packetsDroppedTotal": self.drop_count,
            "ambiguousDropsTotal": self.ambiguous_drop_count,
            "unmappedDropsTotal": self.unmapped_drop_count,
            "expiredRoutesTotal": self.expired_route_count,
            "routeTtlSeconds": self.route_ttl_seconds,
            "associationMode": "ws-route-dedicated-port" if self.pair_ports else "ws-route-endpoint-pair",
            "activePairPorts": len(self.pair_transports),
            "pairPortRange": [self.pair_port_start, self.pair_port_end]
            if self.pair_ports and self.pair_port_start else None,
            "pairPortAllocationFailuresTotal": self.pair_port_allocation_failures,
            "sharedRouteWarningsTotal": self.shared_route_warnings,
            "officialAssociationParity": False,
            "faultInjector": self.fault_injector.metrics() if self.fault_injector else {"mode": "", "active": False},
            "trace": self.trace_writer.metrics() if self.trace_writer else {"enabled": False},
            "impairment": self.impairment_metrics(),
            "impairmentDropsTotal": self.impair_drop_count,
            "impairmentLateDropsTotal": self.impair_late_drop_count,
        }

    def _expire_routes(self) -> None:
        now = time.monotonic()
        expired = [
            key
            for key, route in self.routes.items()
            if now - route.last_activity_monotonic > self.route_ttl_seconds
        ]
        for key in expired:
            self._remove_route(key)
            self.expired_route_count += 1

    def remove_user(self, user_id: str, *, lobby_id: int | None = None) -> None:
        removed = []
        for key, route in self.routes.items():
            if user_id not in key:
                continue
            if lobby_id is not None and route.lobby_id != lobby_id:
                continue
            removed.append(key)
        for key in removed:
            self._remove_route(key)

    def register_route(self, source: UserSession, target: UserSession) -> tuple[bool, str]:
        self._expire_routes()
        if not self.enabled:
            return False, "relay transport is disabled"
        if source.user_id == target.user_id:
            return False, "relay target must be another player"
        if source.current_lobby_id is None or source.current_lobby_id != target.current_lobby_id:
            return False, "relay peers must be in the same lobby"

        source_endpoint = _parse_ipv4_endpoint(source.wan_address)
        target_endpoint = _parse_ipv4_endpoint(target.wan_address)
        if source_endpoint is None or target_endpoint is None:
            return False, "both relay peers need a WAN ip:port endpoint"

        key = tuple(sorted((source.user_id, target.user_id)))
        previous = self.routes.get(key)
        if previous is not None and previous.lobby_id != source.current_lobby_id:
            self._remove_route(key)
            previous = None
        if key[0] == source.user_id:
            endpoint_a, endpoint_b = source_endpoint, target_endpoint
        else:
            endpoint_a, endpoint_b = target_endpoint, source_endpoint

        route = RelayRoute(
            lobby_id=int(source.current_lobby_id),
            user_a=key[0],
            user_b=key[1],
            endpoint_a=endpoint_a,
            endpoint_b=endpoint_b,
        )
        if previous is not None:
            if previous.endpoint_a == endpoint_a:
                route.observed_a = previous.observed_a
            if previous.endpoint_b == endpoint_b:
                route.observed_b = previous.observed_b
            route.created_monotonic = previous.created_monotonic
        self.routes[key] = route
        if not self.pair_ports and previous is None and any(
                {route.endpoint_a, route.endpoint_b} & {other.endpoint_a, other.endpoint_b}
                for other_key, other in self.routes.items() if other_key != key):
            self.shared_route_warnings += 1
            warning = (f"Shared UDP relay routes overlap for {route.user_a}<->{route.user_b}; "
                       "multiple relayed peers cannot be distinguished. Enable --relay-pair-ports "
                       "and configure an accessible UDP pair-port range for LAN/WAN players.")
            self.logger.warning(warning)
            self.state.record_stdout_entry("warn", warning)
        self.state.record_stdout_entry(
            "info",
            f"Relay route registered lobby={route.lobby_id} {route.user_a}<->{route.user_b} "
            f"endpoints={route.endpoint_a[0]}:{route.endpoint_a[1]}<->{route.endpoint_b[0]}:{route.endpoint_b[1]}",
        )
        return True, "ok"

    async def allocate_route(self, source: UserSession, target: UserSession) -> tuple[bool, str, int | None]:
        """Register a route and return only a port with a live backing socket.

        Route signaling can arrive concurrently from both clients. Serializing
        allocation keeps their advertised port stable through retries and
        ensures that a peer-pair never accumulates duplicate sockets.
        """
        async with self._allocation_lock:
            ok, reason = self.register_route(source, target)
            if not ok:
                return False, reason, None
            if not self.pair_ports:
                return True, "ok", self.listen_port
            key = tuple(sorted((source.user_id, target.user_id)))
            transport = self.pair_transports.get(key)
            if transport is None:
                assert self.transport is not None
                try:
                    transport = await self._bind_pair_transport(key)
                except OSError as exc:
                    self._remove_route(key)
                    self.pair_port_allocation_failures += 1
                    self.logger.warning("Could not allocate relay port for %s: %s", key, exc)
                    reason = f"could not allocate relay pair port: {exc}"
                    self.state.record_stdout_entry("warn", reason)
                    if self.trace_writer is not None:
                        self.trace_writer.event("route_allocation_failed", users=list(key), reason=reason)
                    return False, reason, None
                # A disconnect, disable, or lobby change may occur while the
                # event loop creates the socket. Do not publish a stale route.
                route = self.routes.get(key)
                if (not self.enabled or route is None
                        or source.current_lobby_id != route.lobby_id
                        or target.current_lobby_id != route.lobby_id):
                    transport.close()
                    self._remove_route(key)
                    return False, "relay peers left before port allocation", None
                self.pair_transports[key] = transport
            port = int(transport.get_extra_info("sockname")[1])
            if self.trace_writer is not None:
                self.trace_writer.event("route_allocated", lobbyId=source.current_lobby_id,
                                        users=list(key), relayEndpoint=transport.get_extra_info("sockname"),
                                        endpointA=self.routes[key].endpoint_a, endpointB=self.routes[key].endpoint_b)
            return True, "ok", port

    async def _bind_pair_transport(self, key: tuple[str, str]) -> asyncio.DatagramTransport:
        """Bind a usable pair port; a bounded pool never falls back to shared/ephemeral."""
        assert self.transport is not None
        host = self.transport.get_extra_info("sockname")[0]
        loop = asyncio.get_running_loop()
        if not self.pair_port_start:
            transport, _ = await loop.create_datagram_endpoint(
                lambda: BZRPairRelayProtocol(self, key), local_addr=(host, 0))
            return transport

        used = {int(t.get_extra_info("sockname")[1]) for t in self.pair_transports.values()}
        unavailable = self.reserved_ports | used | {self.listen_port}
        span = self.pair_port_end - self.pair_port_start + 1
        collisions = 0
        last_error: OSError | None = None
        for offset in range(span):
            port = self.pair_port_start + (self._pair_port_cursor - self.pair_port_start + offset) % span
            if port in unavailable:
                continue
            try:
                transport, _ = await loop.create_datagram_endpoint(
                    lambda: BZRPairRelayProtocol(self, key), local_addr=(host, port))
            except OSError as exc:
                collisions += 1
                last_error = exc
                self.logger.debug("Relay pair UDP port %d cannot bind: %s", port, exc)
                continue
            self._pair_port_cursor = port + 1 if port < self.pair_port_end else self.pair_port_start
            return transport
        detail = f"; {collisions} bind failure(s), last: {last_error}" if last_error else ""
        raise OSError(f"UDP pair-port range {self.pair_port_start}-{self.pair_port_end} is exhausted "
                      f"or unavailable{detail}; free ports or enlarge the configured/forwarded range")

    def _match_sender(
        self, addr: tuple[str, int], route_key: tuple[str, str] | None = None,
    ) -> tuple[RelayRoute, str] | None:
        candidates = self.routes.items()
        if route_key is not None:
            route = self.routes.get(route_key)
            candidates = [(route_key, route)] if route is not None else []
        exact: dict[tuple[str, str], tuple[RelayRoute, str]] = {}
        for key, route in candidates:
            if addr == route.endpoint_a or addr == route.observed_a:
                exact[("|".join(key), "a")] = (route, "a")
            if addr == route.endpoint_b or addr == route.observed_b:
                exact[("|".join(key), "b")] = (route, "b")
        if len(exact) == 1:
            return next(iter(exact.values()))
        if len(exact) > 1:
            self.ambiguous_drop_count += 1
            return None

        # A changed NAT source port can be learned only when the source IP still
        # identifies one unique side.  Multiple candidates are deliberately not
        # guessed because the unrecovered stock relay may carry an association id.
        ip_matches: dict[tuple[str, str], tuple[RelayRoute, str]] = {}
        for key, route in candidates:
            a_ips = {route.endpoint_a[0]}
            b_ips = {route.endpoint_b[0]}
            if route.observed_a:
                a_ips.add(route.observed_a[0])
            if route.observed_b:
                b_ips.add(route.observed_b[0])
            if addr[0] in a_ips:
                ip_matches[("|".join(key), "a")] = (route, "a")
            if addr[0] in b_ips:
                ip_matches[("|".join(key), "b")] = (route, "b")
        if len(ip_matches) == 1:
            return next(iter(ip_matches.values()))
        if len(ip_matches) > 1:
            self.ambiguous_drop_count += 1
        return None

    def datagram_received(
        self, data: bytes, addr: tuple[str, int], *,
        route_key: tuple[str, str] | None = None,
        pair_transport: asyncio.DatagramTransport | None = None,
    ) -> None:
        received_monotonic = time.monotonic() if self.trace_writer is not None else None
        self.rx_count += 1
        self._expire_routes()

        # Relay forwarding stays deliberately opaque and cheap. Sample only the
        # common 18-byte wrapper for diagnostics instead of running the deep
        # 7a75/0x5f decoder on every gameplay datagram. Drops are always logged.
        sampled = self.rx_count <= 8 or self.rx_count % 64 == 0
        decoder: dict[str, Any] | None = None
        preview = {"bytes": len(data), "lead": data[:32].hex()}
        if sampled:
            decoder = _decode_bzr_transport_header(data)
            if decoder:
                decoder["family"] = "relay_opaque"
            self.state.record_protocol_event(
                channel="relay",
                direction="rx",
                peer=f"{addr[0]}:{addr[1]}",
                msg_type="relay_datagram",
                payload=preview,
                decoder=decoder,
            )

        if not self.enabled:
            self.drop_count += 1
            self._trace_unroutable(data, addr, "disabled", route_key, pair_transport)
            return

        ambiguous_before = self.ambiguous_drop_count
        if self.pair_ports:
            # The shared listener is not an association in this mode. Reject
            # it, and reject callbacks from a socket whose route was removed.
            matched = (self._match_sender(addr, route_key)
                       if route_key is not None and pair_transport is not None
                       and self.pair_transports.get(route_key) is pair_transport else None)
            send_transport = pair_transport
        else:
            matched = self._match_sender(addr)
            send_transport = self.transport
        if matched is None:
            self.drop_count += 1
            if self.ambiguous_drop_count == ambiguous_before:
                self.unmapped_drop_count += 1
            elif not self.pair_ports and time.monotonic() - self._last_ambiguity_warning >= 5:
                self._last_ambiguity_warning = time.monotonic()
                warning = ("Shared UDP relay dropped an ambiguous peer datagram; enable --relay-pair-ports "
                           "with an accessible UDP range when a player has multiple relayed peers.")
                self.logger.warning(warning)
                self.state.record_stdout_entry("warn", warning)
            self._trace_unroutable(data, addr, "ambiguous" if self.ambiguous_drop_count != ambiguous_before else "unmapped",
                                   route_key, pair_transport)
            if decoder is None:
                decoder = _decode_bzr_transport_header(data)
                if decoder:
                    decoder["family"] = "relay_opaque"
            self.state.record_protocol_event(
                channel="relay",
                direction="drop",
                peer=f"{addr[0]}:{addr[1]}",
                msg_type="relay_unroutable",
                payload=preview,
                decoder=decoder,
            )
            return

        route, side = matched
        if side == "a":
            route.observed_a = addr
            target = route.observed_b or route.endpoint_b
        else:
            route.observed_b = addr
            target = route.observed_a or route.endpoint_a
        route.last_activity_monotonic = time.monotonic()

        if target == addr or send_transport is None:
            self.drop_count += 1
            self._trace_unroutable(data, addr, "self-target-or-no-socket", route_key, pair_transport)
            return

        # Preserve the complete datagram and packet boundary exactly. The relay
        # never rewrites sequence numbers or interprets the payload after byte 18.
        #
        # Phase-2 instrumentation hook (work order Â§4): optional forensic trace
        # plus an optional single-shot reliable-midfragment drop. Both are OFF
        # unless explicitly enabled; with both off this path is identical to
        # previous behavior. The injector performs no interpretation beyond the
        # bare-0x80 predicate and never rewrites or reorders anything; the
        # sendto below always transmits `data` unmodified.
        impairment = self.impairment
        if impairment is not None and impairment.expired():
            self.impairment = None
            self.logger.warning("RELAY IMPAIRMENT expired (%s)", impairment.spec.describe())
            self._impairment_event("impairment_expired", impairment)
            impairment = None
        decision = IMPAIR_PASS
        if impairment is not None:
            src_user, dst_user = (route.user_a, route.user_b) if side == "a" else (route.user_b, route.user_a)
            decision = impairment.decide(len(data), (addr[0], addr[1], target[0], target[1]), src_user,
                                         self._user_labels(src_user), self._user_labels(dst_user))

        instrumented = self.fault_injector is not None or self.trace_writer is not None
        send_context = None
        if instrumented:
            stream_key = (addr[0], addr[1], target[0], target[1])
            direction = f"{addr[0]}:{addr[1]}->{target[0]}:{target[1]}"
            match_ordinal: int | None = None
            drop_for_faultinject = False
            if self.fault_injector is not None:
                drop_for_faultinject = self.fault_injector.decide_drop(data, stream_key)
                match_ordinal = self.fault_injector._stream_match_ordinals.get(stream_key)
            disposition = ("dropped_faultinject" if drop_for_faultinject
                           else f"dropped_impair_{decision.drop}" if decision.drop else "forwarded")
            if self.trace_writer is not None:
                src_user, dst_user = (route.user_a, route.user_b) if side == "a" else (route.user_b, route.user_a)
                identity = {"sourceUser": src_user, "targetUser": dst_user,
                            "routeKey": [route.user_a, route.user_b], "lobbyId": route.lobby_id,
                            "socketId": self.trace_writer.socket_id(send_transport),
                            "relayEndpoint": send_transport.get_extra_info("sockname")}
                packet_id = self.trace_writer.log(
                    direction=direction,
                    source=addr,
                    target=target,
                    data=data,
                    disposition=disposition,
                    match_ordinal=match_ordinal,
                    route={"lobbyId": route.lobby_id, "users": [route.user_a, route.user_b], "sourceSide": side},
                    relay_endpoint=send_transport.get_extra_info("sockname"),
                    impairment=None if decision is IMPAIR_PASS else {
                        "drop": decision.drop,
                        "delaysMs": [round(d * 1000.0, 3) for d in decision.delays],
                        "reordered": decision.reordered,
                    },
                    send_context={k: v for k, v in identity.items() if k != "relayEndpoint"},
                )
                send_context = {**identity, "packetId": packet_id, "source": f"{addr[0]}:{addr[1]}",
                                "target": f"{target[0]}:{target[1]}", "wireBytes": len(data),
                                "receivedMonotonic": received_monotonic}
                endpoint = identity["relayEndpoint"]
                send_context["relayEndpoint"] = f"{endpoint[0]}:{endpoint[1]}" if endpoint else None
            if drop_for_faultinject:
                assert self.fault_injector is not None
                info = self.fault_injector.last_dropped or {}
                self.drop_count += 1
                self.logger.warning(
                    "FAULT-INJECT dropped reliable midfragment stream=%s ordinal=%s seqA=%s seqB=%s payload=%s",
                    stream_key,
                    info.get("matchOrdinal"),
                    info.get("seqA"),
                    info.get("seqB"),
                    info.get("payloadBytes"),
                )
                return

        if decision.drop is not None:
            self.drop_count += 1
            self.impair_drop_count += 1
            return
        for copy_index, delay in enumerate(decision.delays):
            context = None
            if send_context is not None:
                context = {**send_context, "copyIndex": copy_index, "copyCount": len(decision.delays),
                           "scheduledDelayMs": round(delay * 1000, 3),
                           "scheduledMonotonic": time.monotonic() + delay}
            args = (send_transport, data, target) if context is None else (send_transport, data, target, context)
            if decision.reordered:
                # Deliberately held past the stream floor; it must not join the FIFO.
                self.schedule_later(delay, self._send_impaired, *args)
            elif delay > 0 or (id(send_transport), target) in self._impair_fifo:
                self._schedule_in_order(delay, args)
            else:
                if context is None:
                    send_transport.sendto(data, target)
                else:
                    self._send_relay(send_transport, data, target, context)
                self.tx_count += 1
        if sampled:
            self.state.record_protocol_event(
                channel="relay",
                direction="tx",
                peer=f"{target[0]}:{target[1]}",
                msg_type="relay_datagram",
                payload=preview,
                decoder=decoder,
            )

    def _trace_unroutable(
        self, data: bytes, addr: tuple[str, int], reason: str,
        route_key: tuple[str, str] | None, transport: asyncio.DatagramTransport | None,
    ) -> None:
        if self.trace_writer is not None:
            raw_capture = {"datagramHex": data.hex()} if self.trace_writer.include_payloads else {}
            self.trace_writer.event("unroutable_datagram", source=f"{addr[0]}:{addr[1]}",
                                    reason=reason, users=list(route_key) if route_key else None,
                                    relayEndpoint=transport.get_extra_info("sockname") if transport else None,
                                    wireBytes=len(data), headerHex=data[:BZR_TRANSPORT_HEADER_BYTES].hex(),
                                    datagramSha256=hashlib.sha256(data).hexdigest(), **raw_capture)

    def error_received(self, exc: Exception, *, route_key: tuple[str, str] | None = None,
                       transport: asyncio.DatagramTransport | None = None) -> None:
        self.logger.warning("UDP relay error: %s", exc)
        if self.trace_writer is not None:
            socket_transport = transport or self.transport
            self.trace_writer.event("socket_error", error=str(exc), users=list(route_key) if route_key else None,
                                    errorType=type(exc).__name__, packetCorrelation="unknown",
                                    socketId=self.trace_writer.socket_id(socket_transport) if socket_transport else None,
                                    relayEndpoint=socket_transport.get_extra_info("sockname") if socket_transport else None)


class BZRWebSocketServer:
    def __init__(
        self,
        state: ServerState,
        logger: logging.Logger,
        *,
        motd: str = "",
        max_lobbies: int = 500,
        retain_empty_lobbies: bool = False,
        relay_protocol: BZRRelayProtocol | None = None,
        hardening: HardeningConfig | None = None,
        redact_addresses: bool = False,
        allow_loopback_watchers: bool = False,
    ):
        self.state = state
        self.log = logger
        self.hardening = hardening or HardeningConfig()
        self.motd = motd.strip()
        self.max_lobbies = max(1, int(max_lobbies))
        self.retain_empty_lobbies = bool(retain_empty_lobbies)
        self.redact_addresses = bool(redact_addresses)
        self.allow_loopback_watchers = bool(allow_loopback_watchers)
        self.relay_protocol = relay_protocol
        self.ws_messages_total = 0
        self.auth_total = 0
        self.chat_total = 0
        self.lobbies_created_total = 0
        self.lobbies_joined_total = 0
        self.kicks_total = 0

    def metrics(self) -> dict[str, Any]:
        return {
            "messagesTotal": self.ws_messages_total,
            "authTotal": self.auth_total,
            "chatTotal": self.chat_total,
            "lobbiesCreatedTotal": self.lobbies_created_total,
            "lobbiesJoinedTotal": self.lobbies_joined_total,
            "kicksTotal": self.kicks_total,
        }

    def build_stat_log(self) -> str:
        lines = [
            "BZR Community Failover Server",
            f"time={utc_now_str()}",
        ]
        for key, value in self.state.metrics().items():
            lines.append(f"state.{key}={value}")
        for key, value in self.metrics().items():
            lines.append(f"ws.{key}={value}")
        lines.extend(
            [
                "compatibility.websocketLobby=true",
                "compatibility.udpHandshakeShim=true",
                "compatibility.directPeerSessionsExpected=true",
                f"compatibility.relayTransportImplemented={str(bool(self.relay_protocol)).lower()}",
                f"compatibility.relayTransportEnabled={str(bool(self.relay_protocol and self.relay_protocol.enabled)).lower()}",
                "compatibility.relayOfficialAssociationParity=false",
                "compatibility.steamTicketValidation=false",
            ]
        )
        for lid, lobby in sorted(self.state.lobbies.items()):
            lines.append(
                "lobby."
                + json.dumps(
                    {
                        "id": lid,
                        "name": str(lobby.metadata.get("name", "")),
                        "owner": lobby.owner,
                        "users": len(lobby.users),
                        "memberLimit": lobby.member_limit,
                        "isLocked": lobby.is_locked,
                        "isPrivate": lobby.is_private,
                    },
                    separators=(",", ":"),
                )
            )
        return "\n".join(lines)

    @staticmethod
    def _ws_peer(ws: Any) -> str:
        peer = getattr(ws, "remote_address", None)
        if isinstance(peer, tuple) and len(peer) >= 2:
            return f"{peer[0]}:{peer[1]}"
        return "unknown"

    @staticmethod
    def _coerce_bool(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    @staticmethod
    def _dedupe_strings(values: list[str]) -> list[str]:
        seen: set[str] = set()
        ordered: list[str] = []
        for raw in values:
            text = str(raw).strip()
            if not text or text in seen:
                continue
            seen.add(text)
            ordered.append(text)
        return ordered

    def _extract_wan_address(self, content: Any) -> str:
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, dict):
            for key in ("wanAddress", "address", "value", "ipAddress"):
                value = content.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return ""

    def _extract_lan_addresses(self, content: Any) -> list[str]:
        if isinstance(content, str):
            return self._dedupe_strings([content])
        if isinstance(content, list):
            return self._dedupe_strings([str(x) for x in content if isinstance(x, str)])
        if isinstance(content, dict):
            if isinstance(content.get("lanAddresses"), list):
                return self._dedupe_strings([str(x) for x in content.get("lanAddresses", []) if isinstance(x, str)])
            for key in ("lanAddress", "address", "value"):
                value = content.get(key)
                if isinstance(value, str):
                    return self._dedupe_strings([value])
        return []

    def _extract_target_user_id(self, content: Any) -> str:
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, dict):
            for key in ("player", "id", "userId", "targetId", "member", "peerId", "friendID"):
                value = content.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
                if value is not None and str(value).strip():
                    return str(value).strip()
        return ""

    def _build_user_change_payload(self, session: UserSession) -> dict[str, Any]:
        # Official OnUserDataChanged carries only member (the user id) and
        # lobbyId; the client then re-reads member data from the lobby's
        # users[id].metadata. A display name here misses the member lookup.
        return {
            "type": "OnUserDataChanged",
            "time": utc_now_str(),
            "success": True,
            "member": session.user_id,
            "lobbyId": -1 if session.current_lobby_id is None else session.current_lobby_id,
        }

    def _build_lobby_data_payload(
        self,
        lobby: LobbyState,
        *,
        key: str,
        value: Any = None,
        deleted: bool = False,
    ) -> dict[str, Any]:
        lobby_blob = self.state.serialize_lobby(lobby)
        payload = {
            "type": "OnLobbyDataChanged",
            "time": utc_now_str(),
            "success": True,
            "id": lobby.lobby_id,
            "lobbyId": lobby.lobby_id,
            "changedLobby": lobby.lobby_id,
            "key": key,
            "lobby": lobby_blob,
            "lobbies": {str(lobby.lobby_id): lobby_blob},
        }
        if deleted:
            payload["deleted"] = True
        else:
            payload["value"] = value
        return payload

    def _build_address_payload(self, session: UserSession, *, message_type: str) -> dict[str, Any]:
        snapshot = self.state.build_user_snapshot(session)
        payload = {
            "type": message_type,
            "time": utc_now_str(),
            "success": True,
            "id": session.user_id,
            "userId": session.user_id,
            "member": session.name,
            "memberName": session.name,
            "changedLobby": session.current_lobby_id,
            "user": snapshot,
            "users": {session.user_id: snapshot},
        }
        if message_type == "OnWANUpdated":
            payload["wanAddress"] = session.wan_address
        else:
            payload["lanAddresses"] = list(session.lan_addresses)
        return payload

    async def _broadcast_session_addresses(self, session: UserSession) -> None:
        if session.wan_address:
            await self.broadcast_all(
                "OnWANUpdated",
                self._build_address_payload(session, message_type="OnWANUpdated"),
                address_subject=session.user_id,
            )
        if session.lan_addresses:
            await self.broadcast_all(
                "OnLANUpdated",
                self._build_address_payload(session, message_type="OnLANUpdated"),
                address_subject=session.user_id,
            )

    async def send_stdout(self, ws: Any, level: str, message: str) -> None:
        entry = self.state.record_stdout_entry(level, message)
        await self.send(
            ws,
            "OnStdoutLogEntry",
            {
                "time": utc_now_str(),
                "level": level,
                "message": message,
                "txt": entry,
            },
        )

    async def send(self, ws: Any, msg_type: str, data: Any) -> None:
        if self.redact_addresses:
            data = self.state.redact_for(self.state.sessions_by_ws.get(ws), data)
        payload = {"type": msg_type, "data": data}
        self.state.record_protocol_event(
            channel="ws",
            direction="tx",
            peer=self._ws_peer(ws),
            msg_type=msg_type,
            payload=data,
        )
        await ws.send(json.dumps(payload, separators=(",", ":")))

    async def send_error(self, ws: Any, message: str) -> None:
        await self.send_stdout(ws, "error", message)

    @staticmethod
    def _ws_ip(ws: Any) -> str:
        peer = getattr(ws, "remote_address", None)
        return str(peer[0]) if isinstance(peer, tuple) and peer else ""

    @staticmethod
    def _ws_headers(ws: Any) -> Any:
        return getattr(getattr(ws, "request", None), "headers", None) or {}

    def admin_transport(self, ws: Any) -> tuple[bool, str]:
        """May this connection elevate with the admin key? (ok, why)

        The server speaks plain ws, so the key would cross the network in clear.
        Accept it from loopback peers, native TLS, or a trusted reverse proxy
        that marks the hop TLS (X-Forwarded-Proto / Forwarded: proto=https).
        """
        h = self.hardening
        if h.allow_plain_remote_admin:
            return True, "plain remote admin explicitly allowed"
        ip = self._ws_ip(ws)
        transport = getattr(ws, "transport", None)
        with contextlib.suppress(Exception):
            if transport is not None and transport.get_extra_info("ssl_object") is not None:
                return True, "tls"
        headers = self._ws_headers(ws)
        forwarded = [headers.get(n) for n in ("X-Forwarded-For", "X-Forwarded-Proto", "X-Real-IP", "Forwarded")]
        if any(forwarded):
            if not h.is_trusted_proxy(ip):
                return False, "forwarded request from an untrusted proxy"
            proto = str(headers.get("X-Forwarded-Proto") or "").split(",")[0].strip().lower()
            match = re.search(r"proto=([a-z]+)", str(headers.get("Forwarded") or "").lower())
            proto = proto or (match.group(1) if match else "")
            if proto in ("https", "wss"):
                return True, "trusted proxy marked TLS"
            return False, "trusted proxy did not mark the hop as TLS"
        if is_loopback_ip(ip):
            return True, "loopback"
        return False, "plain ws from a remote peer"

    def _watcher_allowed(self, ws: Any) -> tuple[bool, str]:
        """Watcher role: opt-in server flag, direct loopback peers only (no proxied hops)."""
        if not self.allow_loopback_watchers:
            return False, "watchers disabled"
        headers = self._ws_headers(ws)
        if any(headers.get(n) for n in ("X-Forwarded-For", "X-Real-IP", "Forwarded")):
            return False, "forwarded request"
        if not is_loopback_ip(self._ws_ip(ws)):
            return False, "not a loopback peer"
        return True, "loopback"

    def check_admin_key(self, ws: Any, user_id: str, key: str, info: dict[str, Any]) -> bool:
        """Constant-time key check plus transport policy; audited; result cached in info."""
        if "admin_key_ok" in info:
            return bool(info["admin_key_ok"])
        info["admin_key_ok"] = False
        admin_key = self.state.admin_key
        if not admin_key or not key:
            return False
        ip = self._ws_ip(ws)
        if not hmac.compare_digest(key.encode("utf-8"), admin_key.encode("utf-8")):
            self.state.audit.write("admin_key_failure", id=user_id, ip=ip)
            self.state.record_stdout_entry("warn", f"Admin key rejected for {user_id} from {ip}")
            return False
        ok, why = self.admin_transport(ws)
        if not ok:
            self.state.audit.write("admin_key_refused", id=user_id, ip=ip, reason=why)
            self.state.record_stdout_entry("warn", f"Admin key refused for {user_id} from {ip}: {why}")
            return False
        info["admin_key_ok"] = True
        return True

    async def _refuse_auth(self, ws: Any, user_id: str, reason: str, info: dict[str, Any] | None = None, name: str = "") -> None:
        # Same shape as the upstream steam-without-ticket refusal: the client
        # shows a failed login and retries the websocket on its own timer.
        self.state.access.denied_total += 1
        ip = self._ws_ip(ws)
        self.state.audit.write("auth_denied", id=user_id, name=name or None, ip=ip, reason=reason, **(info or {}))
        self.state.record_stdout_entry("warn", f"Access refused {user_id} from {ip}: {reason}")
        await self.send(ws, "OnAuthorization", {"type": "OnAuthorization", "time": utc_now_str(), "success": False, "id": user_id})
        await self._close_quietly(ws, 4002)

    async def _admit(self, ws: Any, user_id: str, name: str, key: str, info: dict[str, Any] | None = None) -> bool:
        access = self.state.access
        info = info if info is not None else {}
        ip = self._ws_ip(ws)
        if access.rate_limited(ip):
            await self._refuse_auth(ws, user_id, "too many login attempts", info, name)
            return False
        if self.check_admin_key(ws, user_id, key, info):
            return True
        verdict, reason = access.decide(user_id, ip)
        if verdict == "pending":
            fut = access.add_pending(user_id, name, ip, reason)
            self.state.record_stdout_entry("info", f"Access pending {user_id} ({name}) from {ip}: {reason}")
            try:
                approved = await asyncio.wait_for(asyncio.shield(fut), timeout=access.approval_wait_seconds)
            except asyncio.TimeoutError:
                # Stays in the pending list; approving later lets the client's
                # next automatic retry straight in.
                await self._refuse_auth(ws, user_id, "approval timed out", info, name)
                return False
            if not approved:
                await self._refuse_auth(ws, user_id, "denied", info, name)
                return False
            verdict = "allow"
        if verdict != "allow":
            await self._refuse_auth(ws, user_id, reason, info, name)
            return False
        access.note_seen(user_id, ip, name)
        return True

    async def _system_chat(self, ws: Any, text: str) -> None:
        """Server notice in the same shape as the MOTD."""
        with contextlib.suppress(Exception):
            await self.send(ws, "OnChatMessage", {
                "author": "SERVER", "speakerId": "SERVER", "text": text, "lobbyId": None,
                "isChat": True, "time": utc_now_str(), "system": True,
            })

    @staticmethod
    def _actor(session: UserSession | None) -> dict[str, Any] | None:
        if session is None:
            return None
        return {"id": session.user_id, "ip": BZRWebSocketServer._ws_ip(session.ws), "admin": session.is_admin}

    def _clean_name(self, raw: Any, user_id: str, *, allow_reserved: bool = False) -> tuple[str, list[str]]:
        h = self.hardening
        name, notes = sanitize_display_name(raw, max_length=h.name_max_length, reserved=h.reserved_names,
                                            allow_reserved=allow_reserved)
        if not name:
            if "reserved" in notes:
                tail = re.sub(r"\W+", "", user_id)[-4:]
                name = f"Player-{tail}" if tail else "Player"
            else:
                name, more = sanitize_display_name(user_id, max_length=h.name_max_length, reserved=h.reserved_names)
                name = name or "Player"
                notes = notes + more
            notes.append("fallback")
        return name, notes

    async def _chat_gate(self, session: UserSession) -> bool:
        """Mute + per-session token bucket. False means the message is dropped."""
        access = self.state.access
        if access.is_muted(session.user_id):
            session.chat_dropped += 1
            return False
        h = self.hardening
        if h.chat_rate <= 0:
            return True
        now = time.monotonic()
        burst = float(max(1, h.chat_burst))
        if session.chat_tokens is None:
            session.chat_tokens, session.chat_stamp = burst, now
        session.chat_tokens = min(burst, session.chat_tokens + (now - session.chat_stamp) * h.chat_rate)
        session.chat_stamp = now
        if session.chat_tokens >= 1.0:
            session.chat_tokens -= 1.0
            return True
        session.chat_dropped += 1
        session.chat_strikes = [t for t in session.chat_strikes if now - t < h.chat_flood_window] + [now]
        if h.chat_automute_minutes > 0 and h.chat_flood_strikes > 0 and len(session.chat_strikes) >= h.chat_flood_strikes:
            session.chat_strikes = []
            access.mute(session.user_id, session.name, "chat flood", h.chat_automute_minutes * 60,
                        actor={"id": "server", "auto": True})
            self.state.save_state()
            self.state.record_stdout_entry("warn", f"Auto-muted {session.user_id} ({session.name}) for chat flooding")
            await self._system_chat(session.ws, f"You have been muted for {h.chat_automute_minutes:g} minutes for flooding chat.")
        elif now - session.chat_notice_stamp >= 10.0:
            session.chat_notice_stamp = now
            self.state.audit.write("chat_flood_drop", id=session.user_id, ip=self._ws_ip(session.ws), dropped=session.chat_dropped)
            await self._system_chat(session.ws, "You are sending messages too quickly; slow down.")
        return False

    async def kick_user(self, user_id: str, actor: dict[str, Any] | None = None) -> bool:
        session = self.state.sessions_by_user_id.get(user_id)
        if session is None or session.ws is None:
            return False
        self.state.record_stdout_entry("warn", f"Kicked {user_id} ({session.name})")
        self.state.audit.write("kick", actor=actor, targetId=user_id, ip=self._ws_ip(session.ws), name=session.name)
        # Do not hold the operator's request on the client's close handshake.
        asyncio.create_task(self._close_quietly(session.ws, 4002))
        return True

    async def handle_access_action(self, body: dict[str, Any], actor: dict[str, Any] | None = None) -> dict[str, Any]:
        """Operator actions from the local launcher (POST /access)."""
        access = self.state.access
        actor = actor or {"via": "local-http"}
        action = str(body.get("action", ""))
        uid = str(body.get("id", "") or "").strip()
        ip = str(body.get("ip", "") or "").strip()
        name = str(body.get("name", "") or "")
        reason = str(body.get("reason", ""))
        duration = parse_duration(body.get("duration"))
        if duration is None and body.get("expiresAt") not in (None, ""):
            duration = self._duration_until(body["expiresAt"], access.clock())
        if action == "set":
            access.set_mode(body.get("mode"), body.get("pinIps"))
        elif action == "approve" and uid:
            session = self.state.sessions_by_user_id.get(uid)
            if session and not ip:
                ip = self._ws_ip(session.ws)
                name = name or session.name
            access.approve(uid, name, ip)
        elif action == "deny" and uid:
            access.deny(uid)
        elif action == "remove" and uid:
            access.remove_approved(uid)
        elif action == "kick" and uid:
            await self.kick_user(uid, actor)
        elif action == "ban" and (uid or ip):
            session = self.state.sessions_by_user_id.get(uid) if uid else None
            if session and body.get("withIp") and not ip:
                ip = self._ws_ip(session.ws)
            access.ban(uid, ip, name or (session.name if session else ""), reason, duration, actor)
            if uid:
                await self.kick_user(uid, actor)
            if ip:
                for s in list(self.state.sessions_by_user_id.values()):
                    if self._ws_ip(s.ws) == ip:
                        await self.kick_user(s.user_id, actor)
        elif action == "unban" and (uid or ip):
            access.unban(uid, ip, actor)
        elif action == "mute" and uid:
            session = self.state.sessions_by_user_id.get(uid)
            access.mute(uid, name or (session.name if session else ""), reason, duration, actor)
        elif action == "unmute" and uid:
            access.unmute(uid, actor)
        else:
            return {"ok": False, "error": f"bad action {action!r}"}
        if action in ("set", "approve", "deny", "remove", "kick"):
            self.state.audit.write("access_action", actor=actor, action=action, targetId=uid or None,
                                   targetIp=ip or None, mode=body.get("mode") if action == "set" else None,
                                   pinIps=body.get("pinIps") if action == "set" else None)
        self.state.save_state()
        return {"ok": True, "access": access.snapshot()}

    @staticmethod
    def _duration_until(expires_at: Any, now: float) -> float:
        """Seconds from now until an epoch number or 'YYYY-MM-DD HH:MM:SS' (UTC)."""
        if isinstance(expires_at, (int, float)) and not isinstance(expires_at, bool):
            epoch = float(expires_at)
        else:
            try:
                epoch = datetime.strptime(str(expires_at).strip().replace("T", " ").rstrip("Z"),
                                          "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
            except ValueError as exc:
                raise ValueError(f"bad expiresAt {expires_at!r} (use 'YYYY-MM-DD HH:MM:SS' UTC or epoch seconds)") from exc
        if epoch <= now:
            raise ValueError("expiresAt is in the past")
        return epoch - now

    async def _close_quietly(self, ws: Any, code: int) -> None:
        try:
            await ws.close(code=code)
        except Exception:
            pass

    async def broadcast_all(self, msg_type: str, data: Any, *, address_subject: str | None = None) -> None:
        """Send to every session. With redaction on, each recipient gets its own
        filtered copy, and when ``address_subject`` is set the message (an
        address update about that user) goes only to sessions allowed to see it."""
        if not self.state.sessions_by_ws:
            return
        payload = json.dumps({"type": msg_type, "data": data}, separators=(",", ":"))
        coros = []
        for session in self.state.sessions_by_ws.values():
            out = payload
            if self.redact_addresses:
                if address_subject is not None and not self.state.can_see_addresses(session, address_subject):
                    continue
                out = json.dumps(
                    {"type": msg_type, "data": self.state.redact_for(session, data)}, separators=(",", ":")
                )
            self.state.record_protocol_event(
                channel="ws",
                direction="tx",
                peer=self._ws_peer(session.ws),
                msg_type=msg_type,
                payload=data,
            )
            coros.append(session.ws.send(out))
        await asyncio.gather(*coros, return_exceptions=True)

    async def broadcast_lobby(self, lobby: LobbyState, msg_type: str, data: Any) -> None:
        payload = json.dumps({"type": msg_type, "data": data}, separators=(",", ":"))
        targets = []
        for uid in lobby.users:
            session = self.state.sessions_by_user_id.get(uid)
            if session and session.ws:
                self.state.record_protocol_event(
                    channel="ws",
                    direction="tx",
                    peer=self._ws_peer(session.ws),
                    msg_type=msg_type,
                    payload=data,
                )
                out = payload
                if self.redact_addresses:
                    out = json.dumps(
                        {"type": msg_type, "data": self.state.redact_for(session, data)}, separators=(",", ":")
                    )
                targets.append(session.ws.send(out))
        if targets:
            await asyncio.gather(*targets, return_exceptions=True)

    async def push_lobby_list(self, ws: Any, msg_type: str = "OnGetLobbyList") -> None:
        await self.send(ws, msg_type, self._lobby_list_payload())

    def _lobby_list_payload(self) -> dict[str, Any]:
        # The official service includes a "totalUsers" counter in list/changed
        # envelopes (observed live 2026-08-24; the official value can even go
        # negative via an internal wrap -- we always report the true count).
        return {
            "lobbies": self.state.serialize_lobbies(),
            "totalUsers": self._member_count(),
        }

    async def push_lobby_changed(self, lobby: LobbyState) -> None:
        await self.broadcast_all(
            "OnLobbyChanged",
            {
                "lobbies": {str(lobby.lobby_id): self.state.serialize_lobby(lobby)},
                "totalUsers": self._member_count(),
            },
        )

    async def push_full_lobby_list(self) -> None:
        await self.broadcast_all("OnLobbyListChanged", self._lobby_list_payload())

    def _apply_player_data(self, session: UserSession, key: str, value: Any) -> None:
        if key in ("name", "playerName"):
            clean, _notes = sanitize_display_name(value, max_length=self.hardening.name_max_length,
                                                  reserved=self.hardening.reserved_names, allow_reserved=session.is_admin)
            if clean:
                if key == "name":
                    session.name = clean
                else:
                    session.player_name = clean
        elif key == "clientVersion":
            session.client_version = str(value)
        elif key == "authType":
            session.authtype = str(value)
        else:
            session.extra_data[key] = value

        if session.current_lobby_id is not None:
            lobby = self.state.lobbies.get(session.current_lobby_id)
            if lobby and session.user_id in lobby.users:
                lobby.users[session.user_id] = self.state.build_user_snapshot(session)

    # SetPlayerData keys that describe the player rather than a lobby seat.
    # name/playerName/clientVersion/authType never reach extra_data.
    PERSISTENT_PLAYER_DATA_KEYS = frozenset({"friendID"})

    def _clear_lobby_player_data(self, session: UserSession) -> None:
        for key in [k for k in session.extra_data if k not in self.PERSISTENT_PLAYER_DATA_KEYS]:
            del session.extra_data[key]

    def _normalize_lobby_name(self, raw_name: str) -> str:
        if not raw_name:
            return "~chat~pub~~MyLobby"
        # CreateGame pre-normalizes its internal metadata name to ~game~. Do not
        # run that already-normalized value back through the chat-lobby prefix.
        if raw_name.startswith(("~chat~pub~~", "~game~pub~~")):
            return raw_name
        return f"~chat~pub~~{raw_name}"

    def _normalize_game_name(self, raw_name: str) -> str:
        if not raw_name:
            return "~game~pub~~MyGame"
        return raw_name if raw_name.startswith("~game~pub~~") else f"~game~pub~~{raw_name}"

    async def remove_from_lobby(
        self,
        session: UserSession,
        *,
        send_exit_ack: bool = False,
        ack_type: str = "OnDoExitLobby",
    ) -> None:
        lid = session.current_lobby_id
        if lid is None:
            if send_exit_ack:
                await self.send(session.ws, ack_type, {"success": False, "reason": "Not in lobby"})
            return

        lobby = self.state.lobbies.get(lid)
        if self.relay_protocol is not None:
            self.relay_protocol.remove_user(session.user_id, lobby_id=lid)
        session.current_lobby_id = None
        session.in_lounge = True
        # SetPlayerData values (ready, launched, team, miniid, vehicle, ...)
        # describe membership of one lobby. Left in place, a re-host's new room
        # inherits the last game's ready/launched flags: the host's button shows
        # "Not Ready" and Launch does nothing.
        self._clear_lobby_player_data(session)
        if not lobby:
            if send_exit_ack:
                await self.send(session.ws, ack_type, {"success": True, "id": lid})
            return

        if session.user_id in lobby.users:
            del lobby.users[session.user_id]
            self.state.mark_dirty()
            self.state.record_stdout_entry("info", f"{session.name} left lobby {lid}")
            # "member" carries the affected user's server-assigned ID upstream
            # (official behavior confirmed live 2026-08-24); memberName is our
            # human-readable addition.
            await self.broadcast_all(
                "OnLobbyMemberListChanged",
                {
                    "lobbyId": lid,
                    "id": session.user_id,
                    "member": session.user_id,
                    "memberName": session.name,
                    "removed": True,
                    "user": self.state.build_user_snapshot(session),
                    "users": {session.user_id: self.state.build_user_snapshot(session)},
                },
            )

        if lobby.owner == session.user_id:
            remaining = list(lobby.users.keys())
            lobby.owner = remaining[0] if remaining else ""
            self.state.mark_dirty()

        if not lobby.users:
            if self.retain_empty_lobbies or lobby.persistent:
                await self.push_lobby_changed(lobby)
            else:
                del self.state.lobbies[lid]
                self.state.mark_dirty()
                await self.broadcast_all("OnLobbyRemoved", {"id": lid, "time": utc_now_str()})
        else:
            await self.push_lobby_changed(lobby)

        if send_exit_ack:
            await self.send(session.ws, ack_type, {"success": True, "id": lid})
        await self.push_full_lobby_list()

    async def handle_authorization(self, ws: Any, content: dict[str, Any]) -> None:
        authtype = str(content.get("authtype", "") or "").strip()
        if not authtype:
            # Upstream parity (2026-08-25 evidence): missing authtype closes
            # the socket (4002) with no message frame.
            self.state.audit.write("auth_denied", id=str(content.get("id", "") or ""), ip=self._ws_ip(ws),
                                   reason="missing authtype", ticketWithheld=ticket_is_withheld(content))
            await self._close_quietly(ws, 4002)
            return
        info: dict[str, Any] = {
            "authtype": authtype,
            "clientVersion": str(content.get("clientVersion", "2.2.301")),
            "ticketWithheld": ticket_is_withheld(content),
        }
        if authtype.lower() == "steam" and not any(
            str(content.get(key, "") or "").strip()
            for key in ("steamAppTicket", "authTicket", "platformTicket")
        ):
            # Upstream parity (2026-08-25 evidence): steam without a ticket
            # yields OnAuthorization success:false, then close 4002. Only the
            # success+id shape is captured upstream; no reason string is
            # asserted here.
            requested = str(content.get("id", "") or "")
            self.state.audit.write("auth_denied", id=requested, ip=self._ws_ip(ws), reason="steam without ticket", **info)
            await self.send(
                ws,
                "OnAuthorization",
                {
                    "type": "OnAuthorization",
                    "time": utc_now_str(),
                    "success": False,
                    "id": requested,
                },
            )
            await self._close_quietly(ws, 4002)
            return
        requested_id = str(content.get("id", "")).strip()
        if authtype.lower() == "gog":
            # Upstream parity (2026-08-25 evidence): gog without a ticket is
            # ACCEPTED and the client-supplied id is echoed back unassigned.
            # Honor it verbatim instead of minting a replacement. (On a
            # verbatim-id collision the pre-existing uniqueness suffix below
            # still applies; that case has no upstream evidence.)
            user_id = requested_id
        # Official behavior (live 2026-08-24): web clients send id "0" and the
        # service ASSIGNS a unique identity (B100xxxx), ignoring the request.
        # Honor explicit non-placeholder ids from community tooling, but treat
        # "0"/empty as "assign me an id" so multiple such clients never collide.
        elif requested_id in {"", "0"}:
            user_id = f"U{uuid.uuid4().hex[:12]}"
        else:
            user_id = requested_id
        raw_name = content.get("name") or content.get("playerName") or user_id
        name, name_notes = self._clean_name(raw_name, user_id, allow_reserved=user_id in self.state.admin_ids)

        h = self.hardening
        if h.version_policy != "off":
            acceptable, why = h.version_verdict(info["clientVersion"])
            if not acceptable:
                if h.version_policy == "enforce":
                    await self._refuse_auth(ws, user_id, why, info, name)
                    return
                self.state.record_stdout_entry("warn", f"Client version policy (log only) {user_id}: {why}")
                self.state.audit.write("client_version_warning", id=user_id, ip=self._ws_ip(ws), reason=why)

        if not await self._admit(ws, user_id, name, str(content.get("key", "")), info):
            return

        if user_id in self.state.sessions_by_user_id:
            # Ensure uniqueness if duplicate ID reconnects.
            user_id = f"{user_id}_{uuid.uuid4().hex[:4]}"

        session = UserSession(
            user_id=user_id,
            name=name,
            player_name=self._clean_name(content.get("playerName") or name, user_id, allow_reserved=user_id in self.state.admin_ids)[0],
            authtype=authtype,
            client_version=info["clientVersion"],
            ticket_withheld=info["ticketWithheld"],
            key=str(content.get("key", "")),
            lan_addresses=[str(x) for x in content.get("lanAddresses", []) if isinstance(x, str)],
            wan_address=str(content.get("wanAddress", "")),
            in_lounge=bool(content.get("inLounge", False)),
            ws=ws,
        )
        admin_via = []
        if info.get("admin_key_ok"):
            session.is_admin = True
            admin_via.append("key")
        if session.user_id in self.state.admin_ids:
            session.is_admin = True
            admin_via.append("id")
        if session.is_admin and "reserved" in name_notes:
            session.name, _ = self._clean_name(raw_name, user_id, allow_reserved=True)
            session.player_name = self._clean_name(content.get("playerName") or session.name, user_id, allow_reserved=True)[0]
        if "watcher" in content and self._coerce_bool(content.get("watcher")):
            session.is_watcher, watcher_reason = self._watcher_allowed(ws)
            self.state.audit.write("watcher_granted" if session.is_watcher else "watcher_refused",
                                   id=user_id, ip=self._ws_ip(ws), reason=watcher_reason)

        self.state.sessions_by_ws[ws] = session
        self.state.sessions_by_user_id[user_id] = session
        self.auth_total += 1
        self.state.record_stdout_entry(
            "info",
            f"Authorized {session.user_id} ({session.name}) via {describe_authtype(session.authtype, session.ticket_withheld)}",
        )
        self.state.audit.write(
            "auth_accepted", id=session.user_id, name=session.name, ip=self._ws_ip(ws), authtype=session.authtype,
            clientVersion=session.client_version, ticketWithheld=session.ticket_withheld,
            nameAdjusted=[n for n in name_notes if n != "fallback"] or None, isAdmin=session.is_admin,
            adminVia=admin_via or None,
        )

        await self.send(
            ws,
            "OnAuthorization",
            {
                "type": "OnAuthorization",
                "time": utc_now_str(),
                "success": True,
                "id": session.user_id,
                "userId": session.user_id,
                "member": session.name,
                "name": session.name,
                "playerName": session.player_name,
                "realname": session.name,
                "authType": session.authtype,
                "isAdmin": session.is_admin,
                "isBB": False,
                # Community-only; omitted unless granted so stock clients see the upstream shape.
                **({"isWatcher": True} if session.is_watcher else {}),
                "isAuth": True,
                "isInLounge": session.in_lounge,
                "wanAddress": session.wan_address,
                "clientVersion": session.client_version,
                "lanAddresses": list(session.lan_addresses),
                "user": self.state.build_user_snapshot(session),
            },
        )
        await self.send(
            ws,
            "OnWhitelistUpdated",
            {
                "type": "OnWhitelistUpdated",
                "time": utc_now_str(),
                "success": True,
                "whitelist": self.state.whitelist,
            },
        )
        await self.send(
            ws,
            "OnRelayUpdated",
            {
                "type": "OnRelayUpdated",
                "time": utc_now_str(),
                "enabled": bool(self.relay_protocol and self.relay_protocol.enabled),
                "requested": self.state.relay_requested,
            },
        )
        if session.is_admin:
            await self.send(
                ws,
                "OnGetLogLevel",
                {
                    "type": "OnGetLogLevel",
                    "time": utc_now_str(),
                    "success": True,
                    "level": [self.state.log_level_setting, self.state.log_level_setting],
                },
            )
            await self.send(ws, "OnGetUserList", {"users": self.state.serialize_users()})
            await self.send(
                ws,
                "OnGetFullLog",
                {
                    "entries": list(self.state.stdout_history),
                    "generatedAt": utc_now_str(),
                    "stdout": {"history": list(self.state.stdout_history)},
                    "protocol": {"history": list(self.state.protocol_history)},
                },
            )

        if self.motd:
            await self.send(
                ws,
                "OnChatMessage",
                {
                    "author": "SERVER",
                    "speakerId": "SERVER",
                    "text": self.motd,
                    "lobbyId": None,
                    "isChat": True,
                    "time": utc_now_str(),
                    "system": True,
                },
            )

        await self._broadcast_session_addresses(session)
        self.log.info("Authorized user_id=%s name=%s", session.user_id, session.name)

    async def handle_create_lobby(self, session: UserSession, content: dict[str, Any]) -> None:
        if len(self.state.lobbies) >= self.max_lobbies:
            await self.send(
                session.ws,
                "OnLobbyCreated",
                {"success": False, "reason": f"Max lobby count reached ({self.max_lobbies})"},
            )
            return

        raw_name = str(content.get("name", "MyLobby"))
        name = self._normalize_lobby_name(raw_name)
        await self.remove_from_lobby(session, send_exit_ack=False)

        lid = self.state.allocate_lobby_id()
        lobby = LobbyState(
            lobby_id=lid,
            owner=session.user_id,
            member_limit=int(content.get("memberLimit", 20000)),
            is_private=bool(content.get("isPrivate", False)),
            is_locked=bool(content.get("isLocked", False)),
            password=str(content.get("password", "")),
            metadata={
                "name": name,
                "clientVersion": session.client_version,
                "GameVersion": session.client_version,
                "gameType": "1",
                "gameSettings": "*",
                "ready": "*",
            },
        )
        lobby.users[session.user_id] = self.state.build_user_snapshot(session)
        self.state.lobbies[lid] = lobby

        session.current_lobby_id = lid
        session.in_lounge = False
        self.lobbies_created_total += 1
        self.state.mark_dirty()
        self.state.record_stdout_entry("info", f"Created lobby {lid} owner={session.user_id} name={name}")

        # The stock client populates its lobby view from its cached lobby data
        # as soon as OnLobbyCreated arrives (CNetGameLobby::PopulateLobbyFromData,
        # GOG 0x00743F40). Without a cached "name" it shows "Loading Room..." and
        # the host's staging screen never finishes loading, so the snapshot must
        # reach the creator first.
        await self.push_lobby_changed(lobby)
        await self.push_full_lobby_list()
        await self.send(session.ws, "OnLobbyCreated", {"success": True, "id": lid, "time": utc_now_str()})

    async def handle_create_game(self, session: UserSession, content: dict[str, Any]) -> None:
        payload = dict(content)
        payload["name"] = self._normalize_game_name(str(content.get("name", "MyGame")))
        await self.handle_create_lobby(session, payload)
        lid = session.current_lobby_id
        if lid is None:
            return
        lobby = self.state.lobbies.get(lid)
        if not lobby:
            return
        lobby.metadata.setdefault("nextmid", str(content.get("nextmid", "1")))
        lobby.metadata.setdefault("gameType", str(content.get("gameType", "1")))
        lobby.metadata.setdefault("gameSettings", content.get("gameSettings", "*"))
        self.state.mark_dirty()
        await self.push_lobby_changed(lobby)

    async def handle_join_lobby(self, session: UserSession, content: Any) -> None:
        # Client reads reasonCode (numeric) off OnLobbyJoined/OnFailure.
        # Live-confirmed official behavior (2026-08-24):
        #   - content must be an object; scalar content yields
        #     OnFailure "DoJoinLobby failure bad content data type";
        #   - stock clients send {"id": <number>, "password": <string>}.
        # We accept the exact stock shape plus liberal variants (missing or
        # numeric-typed fields) because community tooling relies on them, but a
        # non-object/non-scalar content is rejected with the official wording.
        password = ""
        if isinstance(content, dict):
            requested_lid = content.get("id")
            raw_password = content.get("password", "")
            password = "" if raw_password is None else str(raw_password)
        elif isinstance(content, bool):
            await self.send(
                session.ws,
                "OnFailure",
                {
                    "type": "OnFailure",
                    "time": utc_now_str(),
                    "success": False,
                    "reason": "DoJoinLobby failure bad content data type",
                },
            )
            return
        elif isinstance(content, (int, str)):
            # Official admin page (scripts/bzadmin.js joinLobby) sends a bare id.
            # The current official service rejects it, but we stay liberal on input.
            requested_lid = content
        else:
            requested_lid = None

        if requested_lid is None:
            if not isinstance(content, dict) or "id" not in content:
                await self.send(
                    session.ws,
                    "OnFailure",
                    {
                        "type": "OnFailure",
                        "time": utc_now_str(),
                        "success": False,
                        "reason": "DoJoinLobby failure bad content data type",
                    },
                )
                return
            await self.send(
                session.ws,
                "OnLobbyJoined",
                {"success": False, "reasonCode": JOIN_FAIL_MISSING_ID, "reason": "Missing lobby id"},
            )
            return

        try:
            lid = int(requested_lid)
        except Exception:
            await self.send(session.ws, "OnLobbyJoined", {"success": False, "reasonCode": JOIN_FAIL_INVALID_ID, "reason": "Invalid lobby id"})
            return

        lobby = self.state.lobbies.get(lid)
        if not lobby:
            # Official wording + confirmed reasonCode 4 (2026-08-24).
            await self.send(
                session.ws,
                "OnLobbyJoined",
                {
                    "success": False,
                    "reasonCode": JOIN_FAIL_NOT_FOUND,
                    "reason": "Join Lobby Failed, Lobby no longer exists",
                },
            )
            return

        # Live-confirmed upstream (2026-08-25): a member rejoining their current
        # lobby gets a FAILURE with reasonCode 5, checked BEFORE lock/full.
        if session.current_lobby_id == lid:
            await self.send(
                session.ws,
                "OnLobbyJoined",
                {
                    "success": False,
                    "reasonCode": JOIN_FAIL_ALREADY_IN,
                    "reason": "Join Lobby Failed, Trying to join a lobby you're already in",
                },
            )
            return

        if lobby.is_locked:
            await self.send(session.ws, "OnLobbyJoined", {"success": False, "reasonCode": JOIN_FAIL_LOCKED, "reason": "Lobby is locked"})
            return

        if lobby.password and password != lobby.password:
            await self.send(session.ws, "OnLobbyJoined", {"success": False, "reasonCode": JOIN_FAIL_BAD_PASSWORD, "reason": "Invalid password"})
            return

        if len(lobby.users) >= max(1, lobby.member_limit) and session.user_id not in lobby.users:
            await self.send(session.ws, "OnLobbyJoined", {"success": False, "reasonCode": JOIN_FAIL_FULL, "reason": "Lobby full"})
            return

        if session.current_lobby_id != lid:
            await self.remove_from_lobby(session, send_exit_ack=False)
            lobby.users[session.user_id] = self.state.build_user_snapshot(session)
            if not lobby.owner:
                lobby.owner = session.user_id
            session.current_lobby_id = lid
            session.in_lounge = False
            self.lobbies_joined_total += 1
            self.state.mark_dirty()
            self.state.record_stdout_entry("info", f"{session.user_id} joined lobby {lid}")

            # Official join sequence observed live 2026-08-24 (for the joiner):
            #   OnLobbyChanged(full snapshot incl. joiner)
            #   -> OnLobbyMemberListChanged {member: <userId>, lobbyId, removed:false}
            #   -> OnLobbyJoined {success:true, reasonCode:0}
            #   -> OnLobbyChanged + OnLobbyDataChanged metadata sync pair.
            # "member" carries the affected user's server-assigned ID upstream;
            # memberName is our human-readable addition for community tooling.
            await self.push_lobby_changed(lobby)
            await self.broadcast_all(
                "OnLobbyMemberListChanged",
                {
                    "lobbyId": lid,
                    "id": session.user_id,
                    "member": session.user_id,
                    "memberName": session.name,
                    "removed": False,
                    "user": self.state.build_user_snapshot(session),
                    "users": {session.user_id: self.state.build_user_snapshot(session)},
                },
            )

        lobby_blob = self.state.serialize_lobby(lobby)
        await self.send(
            session.ws,
            "OnLobbyJoined",
            {
                "success": True,
                "reasonCode": REASON_OK,
                # Upstream acks are FLAT (raw paired pcaps 2026-03-21 + live
                # 2026-08-24). The nested lobby/lobbies snapshots below are a
                # community superset; the stock dispatcher tolerates extras.
                "reason": "",
                "id": lid,
                "lobbyId": lid,
                "time": utc_now_str(),
                "lobby": lobby_blob,
                "lobbies": {str(lid): lobby_blob},
            },
        )
        if session.current_lobby_id == lid and lobby:
            await self.push_lobby_changed(lobby)
            await self.broadcast_all(
                "OnLobbyDataChanged",
                {
                    "type": "OnLobbyDataChanged",
                    "time": utc_now_str(),
                    "success": True,
                    "changedLobby": lid,
                },
            )
        await self.push_full_lobby_list()

    async def handle_update_wan(self, session: UserSession, content: Any) -> None:
        wan = self._extract_wan_address(content)
        if not wan:
            await self.send_error(session.ws, "DoUpdateWAN ignored: missing address")
            return
        session.wan_address = wan
        if session.current_lobby_id is not None:
            lobby = self.state.lobbies.get(session.current_lobby_id)
            if lobby and session.user_id in lobby.users:
                lobby.users[session.user_id] = self.state.build_user_snapshot(session)
        self.state.record_stdout_entry("info", f"{session.user_id} updated WAN address to {wan}")
        await self.broadcast_all(
            "OnWANUpdated",
            self._build_address_payload(session, message_type="OnWANUpdated"),
            address_subject=session.user_id,
        )
        if session.current_lobby_id and session.current_lobby_id in self.state.lobbies:
            await self.push_lobby_changed(self.state.lobbies[session.current_lobby_id])

    async def handle_update_lan(self, session: UserSession, content: Any) -> None:
        lan_addresses = self._extract_lan_addresses(content)
        if not lan_addresses:
            await self.send_error(session.ws, "DoUpdateLAN ignored: missing address")
            return
        session.lan_addresses = lan_addresses
        if session.current_lobby_id is not None:
            lobby = self.state.lobbies.get(session.current_lobby_id)
            if lobby and session.user_id in lobby.users:
                lobby.users[session.user_id] = self.state.build_user_snapshot(session)
        self.state.record_stdout_entry(
            "info",
            f"{session.user_id} updated LAN addresses to {','.join(session.lan_addresses)}",
        )
        await self.broadcast_all(
            "OnLANUpdated",
            self._build_address_payload(session, message_type="OnLANUpdated"),
            address_subject=session.user_id,
        )
        if session.current_lobby_id and session.current_lobby_id in self.state.lobbies:
            await self.push_lobby_changed(self.state.lobbies[session.current_lobby_id])

    async def handle_p2p_signal(self, session: UserSession, content: Any, *, route: bool) -> None:
        target_id = self._extract_target_user_id(content)
        target_session = self.state.sessions_by_user_id.get(target_id) if target_id else None
        # With redaction on, signaling is only allowed between members of the
        # same game lobby; otherwise any session could fetch any user's address.
        outside_lobby = (
            self.redact_addresses
            and target_session is not None
            and target_session is not session
            and not self.state.shares_game_lobby(session, target_session.user_id)
        )
        if outside_lobby:
            target_session = None
        reset = False
        if isinstance(content, dict) and "reset" in content:
            reset = self._coerce_bool(content.get("reset"))
        event_name = "DoP2PRoute" if route else "DoP2PConnect"
        self.state.record_stdout_entry(
            "info",
            f"{event_name} from {session.user_id} target={target_id or '?'} route={compact_preview(content)}",
        )

        relay_route_ok = False
        relay_reason = ""
        relay_port: int | None = None
        if route:
            if outside_lobby:
                relay_reason = "target player is not in your game lobby"
            elif target_session is None:
                relay_reason = "target player is not connected"
            elif self.relay_protocol is None:
                relay_reason = "relay transport is unavailable"
            else:
                relay_route_ok, relay_reason, relay_port = await self.relay_protocol.allocate_route(session, target_session)

        success = target_session is not None and (not route or relay_route_ok)
        base_payload = {
            "type": "OnLobbyMemberP2PConnect",
            "time": utc_now_str(),
            "success": success,
            "lobbyId": session.current_lobby_id,
            "changedLobby": session.current_lobby_id,
            "connect": not route,
            # Client dispatcher reads player+reset on OnLobbyMemberP2PConnect and
            # player+port on OnP2PRoute. Never advertise a relay port unless the
            # backend has actually registered a routable pair.
            "reset": reset,
            "sourceUserId": session.user_id,
            "sourceName": session.name,
            "targetId": target_id,
            "requested": content,
            "routeRequested": route,
            "relayEnabled": bool(self.relay_protocol and self.relay_protocol.enabled),
            "sourceWanAddress": session.wan_address,
            "sourceLanAddresses": list(session.lan_addresses),
            "user": self.state.build_user_snapshot(session),
            "users": {session.user_id: self.state.build_user_snapshot(session)},
        }
        if route and not relay_route_ok:
            base_payload["relayReason"] = relay_reason
            self.state.record_stdout_entry(
                "warn",
                f"DoP2PRoute rejected source={session.user_id} target={target_id or '?'}: {relay_reason}",
            )
        if target_session:
            base_payload["target"] = self.state.build_user_snapshot(target_session)
            base_payload["users"][target_session.user_id] = self.state.build_user_snapshot(target_session)

            target_payload = dict(base_payload)
            target_payload["member"] = session.user_id
            target_payload["memberName"] = session.name
            target_payload["player"] = session.user_id
            await self._send_p2p(target_session.ws, target_payload, route_port=relay_port if relay_route_ok else None)

        session_payload = dict(base_payload)
        session_payload["member"] = target_id or session.user_id
        session_payload["memberName"] = (
            target_session.name if target_session else (target_id or session.name)
        )
        session_payload["player"] = target_id or session.user_id
        await self._send_p2p(session.ws, session_payload, route_port=relay_port if relay_route_ok else None)

    async def _send_p2p(self, ws: Any, payload: dict[str, Any], *, route_port: int | None) -> None:
        """Deliver direct-connect signaling and an allocated relay route when valid."""
        await self.send(ws, "OnLobbyMemberP2PConnect", payload)
        if route_port is not None and payload.get("success") is True:
            route_payload = dict(payload)
            route_payload["type"] = "OnP2PRoute"
            route_payload["port"] = int(route_port)
            await self.send(ws, "OnP2PRoute", route_payload)

    async def handle_chat(self, session: UserSession, text: str) -> None:
        text = text.strip()
        if not text:
            return
        if not await self._chat_gate(session):
            return
        self.chat_total += 1

        lid = session.current_lobby_id
        is_chat = True
        if lid is not None and lid in self.state.lobbies:
            is_chat = bool(self.state.serialize_lobby(self.state.lobbies[lid]).get("isChat"))
        chat_payload = {
            "author": session.name,
            "speakerId": session.user_id,
            "text": text,
            "lobbyId": lid,
            "isChat": is_chat,
            "time": utc_now_str(),
        }
        self.state.record_stdout_entry("info", f"Chat from {session.user_id} in lobby {lid}: {text}")

        # Support simple /t targetName message style whisper.
        if text.lower().startswith("/t "):
            parts = text.split(" ", 2)
            if len(parts) >= 3:
                target_name = parts[1].lower()
                private_msg = parts[2]
                for target in self.state.sessions_by_ws.values():
                    if target.name.lower() == target_name and not target.is_watcher:
                        whisper = dict(chat_payload)
                        whisper["text"] = private_msg
                        whisper["private"] = True
                        await self.send(target.ws, "OnChatMessage", whisper)
                        await self.send(session.ws, "OnChatMessage", whisper)
                        return

        if lid is not None and lid in self.state.lobbies:
            lobby = self.state.lobbies[lid]
            await self.broadcast_lobby(lobby, "OnChatMessage", chat_payload)
            await self._send_to_watchers(lobby, chat_payload)
            return

        # Lounge chat fanout (watchers are ordinary sessions here and already included).
        await self.broadcast_all("OnChatMessage", chat_payload)

    async def _send_to_watchers(self, lobby: LobbyState, chat_payload: dict[str, Any]) -> None:
        """Copy chat from open public chat rooms to watcher sessions (which never join rooms).
        Game lobbies and private or passworded rooms are never copied."""
        name = str(lobby.metadata.get("name", ""))
        if not name.startswith(PUBLIC_CHAT_PREFIX) or lobby.is_private or lobby.password:
            return
        for watcher in list(self.state.sessions_by_ws.values()):
            if watcher.is_watcher and watcher.user_id not in lobby.users:
                await self.send(watcher.ws, "OnChatMessage", chat_payload)

    def _member_count(self) -> int:
        """Connected players, not counting read-only watchers."""
        return sum(1 for s in self.state.sessions_by_user_id.values() if not s.is_watcher)

    async def handle_set_lobby_data(self, session: UserSession, content: dict[str, Any]) -> None:
        lid = session.current_lobby_id
        if lid is None:
            await self.send_error(session.ws, "SetLobbyData ignored: not in lobby")
            return

        lobby = self.state.lobbies.get(lid)
        if not lobby:
            await self.send_error(session.ws, "SetLobbyData ignored: lobby not found")
            return

        if str(lobby.owner) != str(session.user_id):
            await self.send_error(session.ws, "SetLobbyData denied: owner only")
            return

        key = str(content.get("key", "")).strip()
        if not key:
            await self.send_error(session.ws, "SetLobbyData ignored: missing key")
            return

        value = content.get("value")
        lobby.metadata[key] = value
        self.state.mark_dirty()
        await self.broadcast_all(
            "OnLobbyDataChanged",
            self._build_lobby_data_payload(lobby, key=key, value=value),
        )
        await self.push_lobby_changed(lobby)

    async def handle_set_lobby_owner(self, session: UserSession, target_id: str) -> None:
        lid = session.current_lobby_id
        if lid is None:
            await self.send_error(session.ws, "DoSetLobbyOwner ignored: not in lobby")
            return
        lobby = self.state.lobbies.get(lid)
        if not lobby:
            await self.send_error(session.ws, "DoSetLobbyOwner ignored: lobby not found")
            return
        if lobby.owner != session.user_id:
            await self.send_error(session.ws, "DoSetLobbyOwner denied: owner only")
            return
        if target_id not in lobby.users:
            await self.send_error(session.ws, "DoSetLobbyOwner denied: target not in lobby")
            return
        lobby.owner = target_id
        self.state.mark_dirty()
        await self.push_lobby_changed(lobby)

    async def handle_kick_user(self, session: UserSession, target_id: str) -> None:
        lid = session.current_lobby_id
        if lid is None:
            await self.send_error(session.ws, "DoKickUser ignored: not in lobby")
            return
        lobby = self.state.lobbies.get(lid)
        if not lobby:
            await self.send_error(session.ws, "DoKickUser ignored: lobby not found")
            return
        if lobby.owner != session.user_id:
            await self.send_error(session.ws, "DoKickUser denied: owner only")
            return
        if target_id == session.user_id:
            await self.send_error(session.ws, "DoKickUser ignored: cannot kick self")
            return
        target_session = self.state.sessions_by_user_id.get(target_id)
        if not target_session or target_session.current_lobby_id != lid:
            await self.send_error(session.ws, "DoKickUser ignored: user not in lobby")
            return
        self.kicks_total += 1
        self.state.audit.write("lobby_kick", actor=self._actor(session), targetId=target_id, lobbyId=lid)
        await self.remove_from_lobby(target_session, send_exit_ack=True)
        self.state.record_stdout_entry("warn", f"{target_id} was kicked from lobby {lid} by {session.user_id}")
        await self.send_stdout(target_session.ws, "warn", "You were kicked from the lobby")

    async def route_message(self, ws: Any, raw_message: str) -> None:
        try:
            envelope = json.loads(raw_message)
        except json.JSONDecodeError:
            # Live-verified upstream (2026-08-25): malformed JSON yields
            # OnFailure 'Invalid Message Data : <raw>' and the connection
            # stays usable. Mirror the wording; cap echoed length for safety.
            await self.send(
                ws,
                "OnFailure",
                {
                    "type": "OnFailure",
                    "time": utc_now_str(),
                    "success": False,
                    "reason": f"Invalid Message Data : {raw_message[:1000]}",
                },
            )
            return

        if not isinstance(envelope, dict):
            await self.send_error(ws, "Invalid message format")
            return

        msg_type = envelope.get("type")
        if not msg_type:
            await self.send_error(ws, "Missing message type")
            return
        self.ws_messages_total += 1

        content = envelope.get("content")
        if content is None:
            content = envelope.get("data", {})
        if content is None:
            content = {}
        self.state.record_protocol_event(
            channel="ws",
            direction="rx",
            peer=self._ws_peer(ws),
            msg_type=str(msg_type),
            payload=content,
        )

        if msg_type == "Authorization":
            # Upstream parity (2026-08-25 evidence): a second Authorization
            # on an already-bound socket is silently ignored; re-auth does
            # not rebind identity.
            if ws in self.state.sessions_by_ws:
                return
            await self.handle_authorization(ws, content if isinstance(content, dict) else {})
            return

        session = self.state.sessions_by_ws.get(ws)
        if not session:
            # Upstream parity (2026-08-25 evidence): pre-auth verbs are
            # silently ignored - no error frame is sent. (Older community
            # behavior replied "Authorization required"; removed for parity.)
            return

        if session.is_watcher and msg_type not in WATCHER_VERBS:
            # Watchers are read-only: no joins, rooms, chat, metadata or signaling.
            return

        if msg_type in ("SetLogLevel", "AddRegex", "RemoveRegex", "SetRelay", "SetTest"):
            self.state.audit.write("admin_verb", actor=self._actor(session), verb=msg_type, args=content,
                                   allowed=session.is_admin or msg_type == "SetTest")

        if msg_type == "DoEnterLounge":
            session.in_lounge = self._coerce_bool(content)
            await self.send(ws, "OnDoEnterLounge", {"success": True, "time": utc_now_str()})
            await self.push_lobby_list(ws, msg_type="OnLobbyListChanged")
            return

        if msg_type == "GetLobbyList":
            await self.push_lobby_list(ws, msg_type="OnLobbyList")
            await self.push_lobby_list(ws, msg_type="OnGetLobbyList")
            return

        if msg_type == "CreateLobby":
            await self.handle_create_lobby(session, content if isinstance(content, dict) else {})
            return

        if msg_type == "CreateGame":
            await self.handle_create_game(session, content if isinstance(content, dict) else {})
            return

        if msg_type == "DoJoinLobby":
            await self.handle_join_lobby(session, content)
            return

        if msg_type == "DoExitLobby":
            await self.remove_from_lobby(session, send_exit_ack=True, ack_type="OnDoExitLobby")
            await self.push_lobby_list(ws, msg_type="OnLobbyListChanged")
            return

        if msg_type == "DoExitLounge":
            # Upstream parity (2026-08-25 evidence): the lounge exit is a
            # SILENT state transition - no response frame is sent.
            session.in_lounge = False
            return

        if msg_type == "DoSendChat":
            await self.handle_chat(session, str(content))
            return

        if msg_type == "SetPlayerData":
            if isinstance(content, dict):
                key = str(content.get("key", "")).strip()
                if key:
                    self._apply_player_data(session, key, content.get("value"))
                    self.state.mark_dirty()
                    # Official order: OnLobbyChanged, OnUserDataChanged,
                    # OnLobbyChanged, OnLobbyDataChanged{changedLobby}.
                    lobby = self.state.lobbies.get(session.current_lobby_id) if session.current_lobby_id else None
                    if lobby:
                        await self.push_lobby_changed(lobby)
                    await self.broadcast_all("OnUserDataChanged", self._build_user_change_payload(session))
                    if lobby:
                        await self.push_lobby_changed(lobby)
                        await self.broadcast_all(
                            "OnLobbyDataChanged",
                            {
                                "type": "OnLobbyDataChanged",
                                "time": utc_now_str(),
                                "success": True,
                                "changedLobby": lobby.lobby_id,
                            },
                        )
            return

        if msg_type == "SetLobbyData":
            await self.handle_set_lobby_data(session, content if isinstance(content, dict) else {})
            return

        if msg_type == "DeleteLobbyData":
            lid = session.current_lobby_id
            if lid is not None and lid in self.state.lobbies and isinstance(content, dict):
                key = str(content.get("key", "")).strip()
                if key:
                    lobby = self.state.lobbies[lid]
                    # Upstream parity (2026-08-25 evidence): deleting a
                    # key that does not exist is silently ignored - no
                    # state mutation, no dirty flag, no broadcast.
                    if key in lobby.metadata:
                        lobby.metadata.pop(key, None)
                        self.state.mark_dirty()
                        await self.broadcast_all(
                            "OnLobbyDataChanged",
                            self._build_lobby_data_payload(lobby, key=key, deleted=True),
                        )
                        await self.push_lobby_changed(lobby)
            return

        if msg_type == "LockLobby":
            lid = session.current_lobby_id
            if lid is not None and lid in self.state.lobbies:
                lobby = self.state.lobbies[lid]
                if lobby.owner == session.user_id:
                    lobby.is_locked = bool(content)
                    self.state.mark_dirty()
                    await self.push_lobby_changed(lobby)
            return

        if msg_type == "SetLobbyMemberLimit":
            lid = session.current_lobby_id
            if lid is not None and lid in self.state.lobbies:
                lobby = self.state.lobbies[lid]
                if lobby.owner == session.user_id:
                    try:
                        lobby.member_limit = max(1, int(content))
                    except Exception:
                        pass
                    self.state.mark_dirty()
                    await self.push_lobby_changed(lobby)
            return

        if msg_type == "DoSetLobbyOwner":
            await self.handle_set_lobby_owner(session, str(content))
            return

        if msg_type == "DoKickUser":
            await self.handle_kick_user(session, str(content))
            return

        if msg_type == "DoUpdateWAN":
            await self.handle_update_wan(session, content)
            return

        if msg_type == "DoUpdateLAN":
            await self.handle_update_lan(session, content)
            return

        if msg_type == "DoP2PConnect":
            await self.handle_p2p_signal(session, content, route=False)
            return

        if msg_type == "DoP2PRoute":
            await self.handle_p2p_signal(session, content, route=True)
            return

        if msg_type in {"Ping", "DoPing"}:
            await self.send(
                ws,
                "OnHeartbeat",
                {"success": True, "serverTimeMs": int(time.time() * 1000), "time": utc_now_str()},
            )
            return

        # With redaction on, user lists and logs (which carry peer ip:port and
        # address updates) are admin-only.
        if msg_type in {"GetUserList", "GetFullLog", "TraceData"} and self.redact_addresses and not session.is_admin:
            await self.send_error(ws, f"{msg_type} denied: admin only")
            return

        if msg_type == "GetUserList":
            await self.send(ws, "OnGetUserList", {"users": self.state.serialize_users()})
            return

        if msg_type == "GetLogLevel":
            await self.send(
                ws,
                "OnGetLogLevel",
                {
                    "type": "OnGetLogLevel",
                    "time": utc_now_str(),
                    "success": True,
                    "level": [self.state.log_level_setting, self.state.log_level_setting],
                },
            )
            return

        if msg_type == "GetStatLog":
            await self.send(
                ws,
                "OnGetStatLog",
                {
                    "entries": [],
                    "generatedAt": utc_now_str(),
                    "stats": self.build_stat_log(),
                },
            )
            return

        if msg_type == "GetFullLog":
            await self.send(
                ws,
                "OnGetFullLog",
                {
                    "entries": list(self.state.stdout_history),
                    "generatedAt": utc_now_str(),
                    "stdout": {"history": list(self.state.stdout_history)},
                    "protocol": {"history": list(self.state.protocol_history)},
                },
            )
            return

        if msg_type == "SetLogLevel":
            if not session.is_admin:
                await self.send_error(ws, "SetLogLevel denied: admin only")
                return
            level = 0
            if isinstance(content, dict):
                try:
                    level = max(0, min(2, int(content.get("level", 0))))
                except Exception:
                    level = 0
            self.state.log_level_setting = level
            self.state.mark_dirty()
            await self.send(
                ws,
                "OnGetLogLevel",
                {
                    "type": "OnGetLogLevel",
                    "time": utc_now_str(),
                    "success": True,
                    "level": [level, level],
                },
            )
            return

        if msg_type == "AddRegex":
            if not session.is_admin:
                await self.send_error(ws, "AddRegex denied: admin only")
                return
            regex = ""
            if isinstance(content, dict):
                regex = str(content.get("regex", "")).strip()
            if not regex:
                await self.send_error(ws, "AddRegex ignored: missing regex")
                return
            entries = self.state.whitelist.setdefault("list", [])
            if regex not in entries:
                entries.append(regex)
                entries.sort()
                self.state.mark_dirty()
            await self.broadcast_all(
                "OnWhitelistUpdated",
                {
                    "type": "OnWhitelistUpdated",
                    "time": utc_now_str(),
                    "success": True,
                    "whitelist": self.state.whitelist,
                },
            )
            return

        if msg_type == "RemoveRegex":
            if not session.is_admin:
                await self.send_error(ws, "RemoveRegex denied: admin only")
                return
            regex = ""
            if isinstance(content, dict):
                regex = str(content.get("regex", "")).strip()
            if regex in self.state.whitelist.get("list", []):
                self.state.whitelist["list"] = [x for x in self.state.whitelist["list"] if x != regex]
                self.state.mark_dirty()
            await self.broadcast_all(
                "OnWhitelistUpdated",
                {
                    "type": "OnWhitelistUpdated",
                    "time": utc_now_str(),
                    "success": True,
                    "whitelist": self.state.whitelist,
                },
            )
            return

        if msg_type == "SetRelay":
            if not session.is_admin:
                await self.send_error(ws, "SetRelay denied: admin only")
                return
            requested = self._coerce_bool(content.get("toggle")) if isinstance(content, dict) else self._coerce_bool(content)
            available = self.relay_protocol is not None
            if self.relay_protocol is not None:
                self.relay_protocol.set_enabled(requested)
            enabled = bool(self.relay_protocol and self.relay_protocol.enabled)
            self.state.relay_requested = enabled
            self.state.mark_dirty()
            success = (enabled == requested) if available else (not requested)
            if requested and not enabled:
                self.state.record_stdout_entry("warn", "Relay enable requested, but UDP relay transport is unavailable")
            await self.send(
                ws,
                "OnEnableRelayChanged",
                {"enabled": enabled, "requested": requested, "success": success, "time": utc_now_str()},
            )
            await self.send(
                ws,
                "OnRelayUpdated",
                {"enabled": enabled, "requested": requested, "success": success, "time": utc_now_str()},
            )
            return

        if msg_type == "SetTest":
            enabled = self._coerce_bool(content.get("toggle")) if isinstance(content, dict) else self._coerce_bool(content)
            self.state.test_mode = enabled
            self.state.mark_dirty()
            await self.send_stdout(ws, "info", f"Test mode set to {enabled}")
            return

        if msg_type == "TraceData":
            await self.send(
                ws,
                "OnGetFullLog",
                {
                    "entries": list(self.state.stdout_history),
                    "generatedAt": utc_now_str(),
                    "stdout": {"history": list(self.state.stdout_history)},
                    "protocol": {"history": list(self.state.protocol_history)},
                },
            )
            return

        # Upstream parity (2026-08-25 evidence): unknown verbs are silently
        # ignored. Keep them non-fatal and server-side only - the previous
        # OnStdoutLogEntry reply was a client-visible protocol message the
        # real server never emits.
        self.log.info("Unhandled message type: %s", msg_type)

    async def unregister(self, ws: Any) -> None:
        session = self.state.sessions_by_ws.pop(ws, None)
        if not session:
            return

        try:
            await self.remove_from_lobby(session, send_exit_ack=False)
        finally:
            self.state.sessions_by_user_id.pop(session.user_id, None)
            self.state.record_stdout_entry("info", f"Disconnected {session.user_id} ({session.name})")
            self.log.info("Disconnected user_id=%s name=%s", session.user_id, session.name)

    async def handler(self, ws: Any) -> None:
        self.log.info("WS client connected: %s", ws.remote_address)
        try:
            async for message in ws:
                if isinstance(message, bytes):
                    self.state.record_protocol_event(
                        channel="ws",
                        direction="rx",
                        peer=self._ws_peer(ws),
                        msg_type="binary",
                        payload=message,
                    )
                    # NOTE: upstream rejects binary frames with the
                    # 'Binary Payload not supported' wording (2026-08-25
                    # evidence); the OnStdoutLogEntry envelope is the community
                    # transport for that rejection and is unchanged here.
                    await self.send_error(ws, "Binary Payload not supported")
                    continue
                await self.route_message(ws, message)
        except ConnectionClosed:
            pass
        except Exception:
            self.log.exception("Unhandled websocket exception")
        finally:
            await self.unregister(ws)


def _http_json(status_code: int, payload: dict[str, Any]) -> bytes:
    reason = {200: "OK", 400: "Bad Request", 403: "Forbidden", 404: "Not Found", 405: "Method Not Allowed"}.get(status_code, "OK")
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    headers = [
        f"HTTP/1.1 {status_code} {reason}",
        "Content-Type: application/json; charset=utf-8",
        f"Content-Length: {len(body)}",
        "Connection: close",
        "",
        "",
    ]
    return "\r\n".join(headers).encode("utf-8") + body


def build_status_payload(
    *,
    state: ServerState,
    ws_server: BZRWebSocketServer,
    udp_protocol: BZRUDPProtocol,
    relay_protocol: BZRRelayProtocol | None,
    started_monotonic: float,
) -> dict[str, Any]:
    return {
        "ok": True,
        "time": utc_now_str(),
        "uptimeSeconds": int(time.monotonic() - started_monotonic),
        "state": state.metrics(),
        "websocket": ws_server.metrics(),
        "udp": {
            "packetsRxTotal": udp_protocol.rx_count,
            "packetsTxTotal": udp_protocol.tx_count,
        },
        "relay": relay_protocol.metrics() if relay_protocol is not None else {
            "enabled": False,
            "listenPort": None,
            "activeRoutes": 0,
            "associationMode": "unavailable",
            "officialAssociationParity": False,
        },
        "compatibility": {
            "observedOfficialHost": "battlezone98mp.webdev.rebellion.co.uk",
            "websocketLobby": True,
            "udpHandshakeShim": True,
            "directPeerSessionsExpected": True,
            "relayControlMessages": True,
            "relayTransportImplemented": relay_protocol is not None,
            "relayTransportEnabled": bool(relay_protocol and relay_protocol.enabled),
            "relayOfficialAssociationParity": False,
            "steamTicketValidation": False,
            "adminLogPayloadCompatibility": True,
        },
        "supportedMessages": {
            "client": SUPPORTED_CLIENT_MESSAGES,
            "server": SUPPORTED_SERVER_MESSAGES,
        },
        "protocolSummary": summarize_protocol_history(state.protocol_history),
        "recentProtocolEvents": state.protocol_history[-20:],
    }


def build_lobbies_payload(state: ServerState) -> dict[str, Any]:
    """Operator view of live lobbies and connected users (local health port only)."""

    def _user_name(user_id: str) -> str:
        session = state.sessions_by_user_id.get(user_id)
        return session.player_name if session and session.player_name != "Unknown" else (session.name if session else "")

    lobbies = []
    for lid, lobby in sorted(state.lobbies.items()):
        lobbies.append(
            {
                "id": lid,
                "name": str(lobby.metadata.get("name", "")),
                "owner": lobby.owner,
                "ownerName": _user_name(lobby.owner),
                "users": [{"id": uid, "name": _user_name(uid)} for uid in lobby.users],
                "memberLimit": lobby.member_limit,
                "isLocked": lobby.is_locked,
                "isPrivate": lobby.is_private,
                "persistent": lobby.persistent,
                "created": lobby.created_time,
            }
        )
    users = [
        {
            "id": s.user_id,
            "name": s.name,
            "playerName": s.player_name,
            "authtype": describe_authtype(s.authtype, s.ticket_withheld),
            "ticketWithheld": s.ticket_withheld,
            "muted": state.access.is_muted(s.user_id),
            "clientVersion": s.client_version,
            "wanAddress": s.wan_address,
            "lobbyId": s.current_lobby_id,
            "inLounge": s.in_lounge,
            "isAdmin": s.is_admin,
        }
        for s in state.sessions_by_user_id.values()
    ]
    return {"ok": True, "time": utc_now_str(), "lobbies": lobbies, "users": users}


def start_stdin_control(loop: asyncio.AbstractEventLoop, request_stop: Any, logger: logging.Logger) -> None:
    """Stop cleanly when the parent (e.g. server_gui.py) writes "stop" or closes stdin.

    Windows has no SIGTERM for console children, so a launcher that terminated the
    process would skip the final state save. This gives it a graceful path.
    """
    import sys
    import threading

    def _reader() -> None:
        try:
            for line in sys.stdin:
                if line.strip().lower() in ("stop", "quit", "exit"):
                    break
        except Exception:
            pass
        logger.info("Stop requested over stdin.")
        loop.call_soon_threadsafe(request_stop)

    threading.Thread(target=_reader, name="stdin-control", daemon=True).start()


async def serve_health_http(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    *,
    state: ServerState,
    ws_server: BZRWebSocketServer,
    udp_protocol: BZRUDPProtocol,
    relay_protocol: BZRRelayProtocol | None,
    started_monotonic: float,
) -> None:
    try:
        first = await asyncio.wait_for(reader.readline(), timeout=2.0)
        line = first.decode("utf-8", errors="replace").strip()
        parts = line.split()
        if len(parts) < 2:
            writer.write(_http_json(404, {"ok": False, "error": "Invalid request"}))
            await writer.drain()
            return
        method, path = parts[0], parts[1]

        content_length = 0
        while True:
            h = await asyncio.wait_for(reader.readline(), timeout=2.0)
            if not h or h in (b"\r\n", b"\n"):
                break
            hname, _, hvalue = h.decode("latin-1").partition(":")
            if hname.strip().lower() == "content-length":
                with contextlib.suppress(ValueError):
                    content_length = max(0, min(int(hvalue.strip()), 65536))

        if method == "POST" and path == "/access":
            # Operator control: only from this machine, whatever --health-host is.
            peer = writer.get_extra_info("peername")
            peer_ip = str(peer[0]) if isinstance(peer, tuple) and peer else ""
            if not (peer_ip.startswith("127.") or peer_ip in ("::1", "::ffff:127.0.0.1")):
                writer.write(_http_json(403, {"ok": False, "error": "access control is local-only"}))
                await writer.drain()
                return
            raw = await asyncio.wait_for(reader.readexactly(content_length), timeout=2.0) if content_length else b"{}"
            try:
                body = json.loads(raw.decode("utf-8") or "{}")
                result = await ws_server.handle_access_action(body if isinstance(body, dict) else {}, {"via": "local-http", "ip": peer_ip})
                writer.write(_http_json(200 if result.get("ok") else 400, result))
            except ValueError as exc:
                writer.write(_http_json(400, {"ok": False, "error": str(exc)}))
            await writer.drain()
            return

        if method == "POST" and path == "/relay/impairment":
            # Test control: only from this machine, whatever --health-host is.
            peer = writer.get_extra_info("peername")
            peer_ip = str(peer[0]) if isinstance(peer, tuple) and peer else ""
            if not (peer_ip.startswith("127.") or peer_ip in ("::1", "::ffff:127.0.0.1")):
                writer.write(_http_json(403, {"ok": False, "error": "relay impairment is local-only"}))
            elif relay_protocol is None:
                writer.write(_http_json(409, {"ok": False, "error": "the relay is disabled"}))
            else:
                raw = await asyncio.wait_for(reader.readexactly(content_length), timeout=2.0) if content_length else b"{}"
                try:
                    body = json.loads(raw.decode("utf-8") or "{}")
                    spec = body.get("spec", "") if isinstance(body, dict) else None
                    if not isinstance(spec, str):
                        raise ValueError('body must be {"spec": "<impairment spec or empty to clear>"}')
                    writer.write(_http_json(200, {"ok": True, "impairment": relay_protocol.set_impairment(spec)}))
                except ValueError as exc:
                    writer.write(_http_json(400, {"ok": False, "error": str(exc)}))
            await writer.drain()
            return

        if method != "GET":
            writer.write(_http_json(405, {"ok": False, "error": "Only GET supported"}))
            await writer.drain()
            return

        if path not in ("/health", "/status", "/captures", "/protocol", "/lobbies", "/access", "/relay/impairment"):
            writer.write(_http_json(404, {"ok": False, "error": "Unknown path"}))
            await writer.drain()
            return

        if path == "/access":
            payload = {"ok": True, "time": utc_now_str(), "access": state.access.snapshot()}
        elif path == "/relay/impairment":
            payload = {"ok": True, "time": utc_now_str(),
                       "impairment": relay_protocol.impairment_metrics() if relay_protocol else {"active": False, "spec": ""}}
        elif path == "/lobbies":
            payload = build_lobbies_payload(state)
        elif path in ("/health", "/status"):
            payload = build_status_payload(
                state=state,
                ws_server=ws_server,
                udp_protocol=udp_protocol,
                relay_protocol=relay_protocol,
                started_monotonic=started_monotonic,
            )
        elif path == "/captures":
            payload = {
                "ok": True,
                "time": utc_now_str(),
                "summary": summarize_protocol_history(state.protocol_history),
                "protocol": {"history": list(state.protocol_history)},
                "stdout": {"history": list(state.stdout_history)},
            }
        else:
            payload = {
                "ok": True,
                "time": utc_now_str(),
                "observedOfficialHost": "battlezone98mp.webdev.rebellion.co.uk",
                "cutover": {
                    "dnsOrHostsOverrideRequired": True,
                    "hostname": "battlezone98mp.webdev.rebellion.co.uk",
                    "websocketPort": DEFAULT_WS_PORT,
                    "udpProbePort": DEFAULT_UDP_PORT,
                    "relayPortObserved": DEFAULT_RELAY_PORT,
                    "relayAssociationMode": "ws-route-endpoint-pair",
                    "relayOfficialAssociationParity": False,
                },
                "supportedMessages": {
                    "client": SUPPORTED_CLIENT_MESSAGES,
                    "server": SUPPORTED_SERVER_MESSAGES,
                },
            }
        writer.write(_http_json(200, payload))
        await writer.drain()
    except Exception:
        with contextlib.suppress(Exception):
            writer.write(_http_json(404, {"ok": False, "error": "Request failed"}))
            await writer.drain()
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


async def autosave_loop(
    stop_event: asyncio.Event,
    state: ServerState,
    interval_seconds: int,
    logger: logging.Logger,
) -> None:
    interval = max(1, int(interval_seconds))
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            state.save_state()
        except Exception:
            logger.exception("Autosave loop error")
            await asyncio.sleep(1)


def _csv(value: str) -> tuple[str, ...]:
    return tuple(x.strip() for x in str(value or "").split(",") if x.strip())


def hardening_from_args(args: argparse.Namespace) -> HardeningConfig:
    return HardeningConfig(
        name_max_length=max(0, args.name_max_length),
        reserved_names=_csv(args.reserved_names),
        version_policy=args.client_version_policy,
        min_client_version=args.min_client_version.strip(),
        allowed_client_versions=_csv(args.allowed_client_versions),
        chat_rate=max(0.0, args.chat_rate),
        chat_burst=max(1, args.chat_burst),
        chat_flood_strikes=max(0, args.chat_flood_strikes),
        chat_automute_minutes=max(0.0, args.chat_automute_minutes),
        trusted_proxies=_csv(args.trusted_proxies),
        allow_plain_remote_admin=bool(args.allow_plain_remote_admin),
    )


async def run_server(args: argparse.Namespace) -> None:
    global websockets, ConnectionClosed
    if websockets is None:
        try:
            import websockets as _websockets
            from websockets.exceptions import ConnectionClosed as _connection_closed
        except Exception as exc:  # pragma: no cover - runtime import guard
            raise SystemExit(
                "Missing dependency 'websockets'. Install with: pip install websockets"
            ) from exc
        websockets = _websockets
        ConnectionClosed = _connection_closed

    log_level = getattr(logging, args.log_level.upper(), logging.INFO)
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logger = logging.getLogger("bzr-dedicated")

    admin_ids = {x.strip() for x in args.admin_ids.split(",") if x.strip()}
    admin_key = args.admin_key or os.getenv("BZR_ADMIN_KEY", "")
    diagnostic_run_id = uuid.uuid4().hex[:12]
    protocol_trace = ProtocolTraceWriter(args.protocol_trace, diagnostic_run_id) if args.protocol_trace else None
    audit = AuditLog(args.audit_log or None, max_bytes=args.audit_log_max_bytes, backups=args.audit_log_backups)
    state = ServerState(
        logger.getChild("state"),
        state_file=args.state_file,
        admin_key=admin_key,
        admin_ids=admin_ids,
        protocol_trace=protocol_trace,
        audit=audit,
    )
    hardening = hardening_from_args(args)
    if hardening.allow_plain_remote_admin:
        logger.warning("--allow-plain-remote-admin: the admin key is accepted over plain ws from ANY peer "
                       "and will cross the network in clear text. Prefer loopback or a TLS-terminating proxy.")
    elif admin_key:
        logger.info("Admin key elevation: loopback%s only",
                    " or trusted TLS proxy " + ",".join(hardening.trusted_proxies) if hardening.trusted_proxies else "")
    if audit.enabled:
        logger.info("Audit log: %s", audit.path)
        audit.write("server_start", accessMode=state.access.mode, versionPolicy=hardening.version_policy,
                    plainRemoteAdmin=hardening.allow_plain_remote_admin)
    state.load_state(retain_empty_lobbies=args.retain_empty_lobbies)
    state.access.approval_wait_seconds = max(1, args.approval_wait_seconds)
    state.access.rate_limit_per_minute = max(0, args.auth_rate_limit)
    if args.access_mode is not None or args.pin_ips is not None:
        state.access.set_mode(args.access_mode, args.pin_ips)
    logger.info("Access mode: %s%s", state.access.mode, " (IP pinning)" if state.access.pin_ips else "")
    state.ensure_default_lobby()

    relay_protocol: BZRRelayProtocol | None = None
    if not args.disable_relay and args.relay_port > 0:
        fault_injector: RelayFaultInjector | None = None
        trace_writer: RelayTraceWriter | None = None
        if args.fault_inject:
            fault_injector = RelayFaultInjector(args.fault_inject)
            logger.warning(
                "FAULT-INJECTION ENABLED (%s) -- diagnostics only; never run in normal service",
                args.fault_inject,
            )
        if args.relay_trace:
            trace_writer = RelayTraceWriter(args.relay_trace, run_id=diagnostic_run_id,
                                            include_payloads=args.relay_trace_payloads)
            logger.info("Relay forensic trace writing to %s", args.relay_trace)
        relay_protocol = BZRRelayProtocol(
            logger.getChild("relay"),
            state,
            route_ttl_seconds=args.relay_route_ttl_seconds,
            fault_injector=fault_injector,
            trace_writer=trace_writer,
            pair_ports=args.relay_pair_ports,
            pair_port_start=args.relay_pair_port_start,
            pair_port_end=args.relay_pair_port_end,
            reserved_ports={args.udp_port, args.relay_port},
        )
        if args.impair:
            relay_protocol.set_impairment(args.impair)
        if args.relay_pair_ports:
            port_description = (f"UDP {args.relay_pair_port_start}-{args.relay_pair_port_end}"
                                if args.relay_pair_port_start else "ephemeral UDP ports (private/local use)")
            logger.info("Relay peer pairs use %s; LAN/WAN clients need access to every allocated port", port_description)
        state.relay_requested = True
    else:
        state.relay_requested = False

    ws_server = BZRWebSocketServer(
        state,
        logger.getChild("ws"),
        motd=args.motd,
        max_lobbies=args.max_lobbies,
        retain_empty_lobbies=args.retain_empty_lobbies,
        relay_protocol=relay_protocol,
        hardening=hardening,
        redact_addresses=args.redact_addresses,
        allow_loopback_watchers=args.allow_loopback_watchers,
    )
    if args.allow_loopback_watchers:
        logger.info("Loopback watchers allowed: local sessions may read public chat rooms without joining them")
    if args.redact_addresses:
        logger.info("Address redaction on: player addresses only go to members of the same game lobby")

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    started_monotonic = time.monotonic()

    def _request_stop() -> None:
        if not stop_event.is_set():
            logger.info("Shutdown requested.")
            stop_event.set()

    for signame in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, signame, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, _request_stop)
        except (NotImplementedError, RuntimeError):
            # Windows or non-main thread.
            pass
    if args.stdin_control:
        start_stdin_control(loop, _request_stop, logger)

    udp_transport, udp_protocol = await loop.create_datagram_endpoint(
        lambda: BZRUDPProtocol(logger.getChild("udp"), state),
        local_addr=(args.udp_host, args.udp_port),
    )

    relay_transport: asyncio.DatagramTransport | None = None
    if relay_protocol is not None:
        relay_transport, _ = await loop.create_datagram_endpoint(
            lambda: relay_protocol,
            local_addr=(args.relay_host, args.relay_port),
        )

    health_server = None
    if args.health_port > 0:
        health_server = await asyncio.start_server(
            lambda r, w: serve_health_http(
                r,
                w,
                state=state,
                ws_server=ws_server,
                udp_protocol=udp_protocol,
                relay_protocol=relay_protocol,
                started_monotonic=started_monotonic,
            ),
            host=args.health_host,
            port=args.health_port,
        )

    autosave_task = asyncio.create_task(
        autosave_loop(stop_event, state, args.autosave_seconds, logger.getChild("autosave"))
    )

    async with websockets.serve(
        ws_server.handler,
        args.ws_host,
        args.ws_port,
        ping_interval=20,
        ping_timeout=30,
        max_size=2**20,
    ):
        logger.info("WebSocket server listening on ws://%s:%d", args.ws_host, args.ws_port)
        logger.info("UDP shim listening on %s:%d", args.udp_host, args.udp_port)
        if relay_protocol is not None:
            logger.info("UDP relay listening on %s:%d", args.relay_host, args.relay_port)
        if health_server:
            logger.info("Health endpoint listening on http://%s:%d/health", args.health_host, args.health_port)
        logger.info("Press Ctrl+C to stop.")
        await stop_event.wait()

    state.save_state(force=True)
    autosave_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await autosave_task

    if health_server:
        health_server.close()
        await health_server.wait_closed()

    udp_transport.close()
    if relay_transport is not None:
        assert relay_protocol is not None
        relay_protocol.close()
    await asyncio.sleep(0.05)
    if relay_protocol is not None and relay_protocol.trace_writer is not None:
        relay_protocol.trace_writer.close()
    if protocol_trace is not None:
        protocol_trace.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Battlezone 98 Redux dedicated-server compatibility shim")
    parser.add_argument("--ws-host", default=DEFAULT_WS_HOST, help=f"WebSocket bind host (default: {DEFAULT_WS_HOST})")
    parser.add_argument("--ws-port", type=int, default=DEFAULT_WS_PORT, help=f"WebSocket bind port (default: {DEFAULT_WS_PORT})")
    parser.add_argument("--udp-host", default=DEFAULT_UDP_HOST, help=f"UDP bind host (default: {DEFAULT_UDP_HOST})")
    parser.add_argument("--udp-port", type=int, default=DEFAULT_UDP_PORT, help=f"UDP bind port (default: {DEFAULT_UDP_PORT})")
    parser.add_argument("--relay-host", default=DEFAULT_RELAY_HOST, help=f"UDP relay bind host (default: {DEFAULT_RELAY_HOST})")
    parser.add_argument("--relay-port", type=int, default=DEFAULT_RELAY_PORT, help=f"UDP relay port, 0 to disable (default: {DEFAULT_RELAY_PORT})")
    pair_mode = os.getenv("BZR_RELAY_PAIR_PORTS", "0").strip().lower()
    if pair_mode not in {"", "0", "false", "no", "off", "1", "true", "yes", "on"}:
        parser.error("BZR_RELAY_PAIR_PORTS must be 0/1 or false/true")
    parser.add_argument("--relay-pair-ports", action=argparse.BooleanOptionalAction,
                        default=pair_mode in {"1", "true", "yes", "on"},
                        help="One UDP port per peer pair; use explicit bounds for LAN/WAN. Default off (env BZR_RELAY_PAIR_PORTS); --no-relay-pair-ports restores shared relay")
    parser.add_argument("--relay-pair-port-start", type=int,
                        default=os.getenv("BZR_RELAY_PAIR_PORT_START", "0"),
                        help="First UDP pair port, inclusive (env BZR_RELAY_PAIR_PORT_START). Both bounds 0 uses ephemeral ports; bounds alone do not enable pair mode")
    parser.add_argument("--relay-pair-port-end", type=int,
                        default=os.getenv("BZR_RELAY_PAIR_PORT_END", "0"),
                        help="Last UDP pair port, inclusive (env BZR_RELAY_PAIR_PORT_END). Allow/forward this entire range for LAN/WAN players")
    parser.add_argument("--relay-route-ttl-seconds", type=int, default=DEFAULT_RELAY_ROUTE_TTL_SECONDS, help="Inactive relay route expiration interval")
    parser.add_argument("--disable-relay", action="store_true", help="Disable the community UDP relay transport")
    parser.add_argument(
        "--impair",
        default=os.getenv("BZR_RELAY_IMPAIR", ""),
        help=(
            "Relay network impairment for private test runs, DEFAULT OFF (env BZR_RELAY_IMPAIR). "
            "Example: loss=3,delay=80,jitter=20,seed=7. See relay_impairment.py for every key; "
            "POST /relay/impairment changes it at run time. Never enable in normal service."
        ),
    )
    parser.add_argument(
        "--fault-inject",
        default=os.getenv("BZRNET_FAULT_INJECT", ""),
        help=(
            "Phase-2 relay impairment, DEFAULT OFF. Format: drop_reliable_midfrag:k=N "
            "(drops the Nth bare-0x80 reliable non-final fragment per directed peer stream). "
            "Instrumentation only; never enable in normal service."
        ),
    )
    parser.add_argument(
        "--relay-trace",
        default=os.getenv("BZRNET_RELAY_TRACE", ""),
        help="Optional JSONL forensic trace of routed relay datagrams (transport envelope fields only; local-only output)",
    )
    parser.add_argument("--protocol-trace", default=os.getenv("BZRNET_PROTOCOL_TRACE", ""),
                        help="Optional untruncated protocol event JSONL with credentials redacted; pair with --relay-trace for every opaque relay datagram")
    parser.add_argument("--relay-trace-payloads", action="store_true",
                        help="Maximum diagnostic capture: copy complete opaque datagrams to --relay-trace as hex; default OFF, adds disk/CPU load")
    parser.add_argument("--health-host", default=DEFAULT_HEALTH_HOST, help=f"Health HTTP bind host (default: {DEFAULT_HEALTH_HOST})")
    parser.add_argument("--health-port", type=int, default=DEFAULT_HEALTH_PORT, help=f"Health HTTP bind port, 0 to disable (default: {DEFAULT_HEALTH_PORT})")
    parser.add_argument("--state-file", default=DEFAULT_STATE_FILE, help=f"Path to persistent state JSON (default: {DEFAULT_STATE_FILE})")
    parser.add_argument("--autosave-seconds", type=int, default=15, help="State autosave interval in seconds")
    parser.add_argument("--motd", default="", help="Optional message-of-the-day sent after authorization")
    parser.add_argument("--admin-key", default="", help="Admin key accepted during Authorization content.key")
    parser.add_argument("--admin-ids", default="", help="Comma-separated user IDs that are always treated as admins")
    parser.add_argument("--audit-log", default=os.getenv("BZR_AUDIT_LOG", DEFAULT_AUDIT_LOG),
                        help=f"Append-only JSONL audit trail of auth, admin actions, bans and mutes (default: {DEFAULT_AUDIT_LOG}; "
                             "empty string disables; never contains tickets, passwords or admin keys)")
    parser.add_argument("--audit-log-max-bytes", type=int, default=5_000_000, help="Rotate the audit log at this size, 0 never rotates")
    parser.add_argument("--audit-log-backups", type=int, default=3, help="Rotated audit files to keep (audit.log.1 ...)")
    parser.add_argument("--name-max-length", type=int, default=32,
                        help="Longest accepted player name; control/bidi/zero-width characters are always stripped (0 = no cap)")
    parser.add_argument("--reserved-names", default=DEFAULT_RESERVED_NAMES,
                        help="Comma-separated names players may not use (impersonating the server or staff); admins are exempt")
    parser.add_argument("--client-version-policy", choices=["off", "log", "enforce"], default="log",
                        help="What to do with clients failing --min-client-version / --allowed-client-versions: "
                             "log (default, admit and record), enforce (refuse like other admission denials), off")
    parser.add_argument("--min-client-version", default="", help="Oldest accepted clientVersion, e.g. 2.2.301")
    parser.add_argument("--allowed-client-versions", default="",
                        help="Comma-separated exact clientVersion values; a trailing * is a prefix match (2.2.*)")
    parser.add_argument("--chat-rate", type=float, default=2.0, help="Sustained chat messages per second per player, 0 disables the limit")
    parser.add_argument("--chat-burst", type=int, default=8, help="Chat messages a player may send in a burst")
    parser.add_argument("--chat-flood-strikes", type=int, default=5,
                        help="Dropped messages within 30 s that trigger an auto-mute, 0 disables auto-mute")
    parser.add_argument("--chat-automute-minutes", type=float, default=5.0, help="Auto-mute length for chat flooding, 0 disables")
    parser.add_argument("--trusted-proxies", default="",
                        help="Comma-separated IPs/CIDRs of reverse proxies whose X-Forwarded-Proto: https is believed for admin-key elevation")
    parser.add_argument("--allow-plain-remote-admin", action="store_true",
                        help="UNSAFE: accept the admin key over plain ws from any peer (default: loopback or trusted TLS proxy only)")
    parser.add_argument("--max-lobbies", type=int, default=500, help="Maximum simultaneous lobbies")
    parser.add_argument(
        "--retain-empty-lobbies",
        action="store_true",
        help="Do not delete lobbies when last user leaves (useful with persistence)",
    )
    parser.add_argument(
        "--redact-addresses",
        action="store_true",
        help="Only send a player's WAN/LAN address to members of the same game lobby (and admins); "
             "user lists and logs become admin-only",
    )
    parser.add_argument(
        "--allow-loopback-watchers",
        action="store_true",
        help="Let local (loopback) sessions that send watcher:true in Authorization read every open public "
             "chat room without joining it. Watchers are read-only and left out of player counts.",
    )
    parser.add_argument("--access-mode", choices=list(ACCESS_MODES), default=None,
                        help="Who may log in: open (default), approve (unknown players wait for operator approval), "
                             "allowlist (approved players only). Overrides the mode saved in the state file.")
    parser.add_argument("--pin-ips", action=argparse.BooleanOptionalAction, default=None,
                        help="Re-ask approval when an approved player connects from a new IP")
    parser.add_argument("--approval-wait-seconds", type=int, default=30,
                        help="How long an unknown player's login is held waiting for approval")
    parser.add_argument("--auth-rate-limit", type=int, default=20,
                        help="Max login attempts per IP per minute, 0 to disable")
    parser.add_argument(
        "--stdin-control",
        action="store_true",
        help='Stop gracefully (saving state) when stdin reads "stop" or closes; used by server_gui.py',
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity",
    )
    args = parser.parse_args()
    if (args.relay_pair_port_start, args.relay_pair_port_end) != (0, 0) and not (
            1 <= args.relay_pair_port_start <= args.relay_pair_port_end <= 65535):
        parser.error("relay pair port bounds must both be 0 (ephemeral) or an inclusive range within 1-65535")
    return args


def main() -> None:
    args = parse_args()
    try:
        asyncio.run(run_server(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
