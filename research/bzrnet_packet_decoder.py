#!/usr/bin/env python3
"""BZRNet packet decoder — deterministic parser for Battlezone 98 Redux UDP surfaces.

Layers covered:
  1. Common BZRNet transport header (18 bytes) used by the peer/discovery planes.
  2. UDP 1338 discovery probe / reply classification (echo + endpoint trailer).
  3. In-match game payload opcodes (2-char ASCII tags dispatched natively by
     the Net::OnP2PReceived-equivalent dispatcher at VA 0x570500 in GOG 2.2.301).

Evidence provenance: BZRNET_PROTOCOL_ARCHAEOLOGY_20260825.md (this repo).
Byte order is big-endian for all transport header fields; game-payload
multi-byte fields are little-endian unless a specific opcode says otherwise.

Usage:
  python research/bzrnet_packet_decoder.py --hex 40030000019d1199210b0000000000000000533736353631...
  python research/bzrnet_packet_decoder.py --selftest
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
from datetime import datetime, timezone

HEADER_LEN = 18
MAX_PACKET = 0x800          # receiver hard limit (native length gate)
RELIABLE_FLAG = 0x80        # set on every reliable fragment
FINAL_FRAG_FLAG = 0x40      # set on single packets and on the LAST reliable fragment
FRAG_THRESHOLD = 0x5AA      # 1450-byte reliable fragmentation chunk (native constant)
DISCOVERY_KIND = 0x2        # kind nibble value used by the 1338 probe

# Game-plane payload opcodes. Keys are the WIRE-ORDER two ASCII bytes at payload
# offset 0..1 (the native dispatcher compares the little-endian u16, e.g. wire
# bytes 'P','P' -> u16 0x5050). Handler VAs are GOG 2.2.301.
GAME_OPCODES = {
    "PP": {"name": "PING", "min": 22, "note": "PING_PACKET body (20 B): master_send_time,offset,host_start_time,delay,BAddOffset"},
    "GA": {"name": "PING_ARRAY", "min": 26, "note": "batched ping array, 6 words per player entry"},
    "PR": {"name": "PING_REPLY", "min": 14, "note": "requires len>=14; HandlePingReply(CSteamID,PINGREPLY_PACKET*,time)"},
    "_S": {"name": "STATE_BLOCK_0x5F", "min": 6, "alt_tag": "'_'+byte1",
           "note": "first byte '_'(0x5F); byte1=aux size; aux blob then state stream (TempStateSendAll)"},
    "PC": {"name": "CHAT_PUBLIC", "min": 4, "note": "<name(<=32)> message"},
    "pS": {"name": "CHAT_WHISPER", "min": 4, "note": "'<From: name> msg' format"},
    "MK": {"name": "KILL_MESSAGE", "min": 9, "note": "killer/victim/team/weapon byte fields"},
    "PS": {"name": "SPAWN_POINT", "min": 18, "note": "SPAWNPOINT_PACKET xyz floats + team u8; receipt transitions NetState 3->4"},
    "SS": {"name": "SYNC_STEP", "min": 3, "note": "subcode byte checked against current sync step"},
    "KA": {"name": "ACTIVITY_ACK", "min": 2, "note": "stamps per-player last-seen time (keepalive role)"},
    "PB": {"name": "BANDWIDTH", "min": 10, "note": "BANDWIDTH_PACKET dwBandwidth + interval4ms"},
    "AC": {"name": "CHEAT_CHECK", "min": 6, "note": "score-integrity probe; mismatch logs MAY BE CHEATING"},
    "RG": {"name": "GAME_RESET?", "min": 2, "note": "purpose inferred from handler only"},
    "AK": {"name": "DIALOG_OPEN_A", "min": 2, "note": "one-shot in-game dialog open (match-end/kick UX path)"},
    "KK": {"name": "DIALOG_OPEN_B", "min": 2, "note": "same one-shot dialog path as AK"},
    "GS": {"name": "DIALOG_OPEN_C", "min": 2, "note": "same one-shot dialog path as AK/KK"},
    "RO": {"name": "OBJECT_REMOVE?", "min": 2, "note": "object event family"},
    "PK": {"name": "OBJECT_PREP?", "min": 2, "note": "calls per-player stamp then UI-gated handler"},
    "GM": {"name": "STATE_DELTA?", "min": 2, "note": "state delta block"},
    "PO": {"name": "OBJECT_POSITION", "min": 2, "note": "object position/control stream (matches captured PO marker)"},
    "AS": {"name": "HOST_AUTOKICKED_NOTICE", "min": 5, "note": "target mini-id word; logs multi_message host_autokicked"},
    "KS": {"name": "HOST_KICKED_NOTICE", "min": 5, "note": "target mini-id word; logs multi_message host_kicked"},
    "LS": {"name": "LOBBY_LOCKED_NOTICE", "min": 2, "note": ""},
    "US": {"name": "LOBBY_UNLOCKED_NOTICE", "min": 2, "note": ""},
    "PT": {"name": "TEAM_SCORE?", "min": 6, "note": "u32 arg with 0x78/0x3c limit constants"},
    "xo": {"name": "OBJ_CREATE_A", "min": 2, "note": "object create/roster family (captured 7a75 descriptor semantics)"},
    "io": {"name": "OBJ_CREATE_B", "min": 2, "note": "object create/roster family"},
    "xx": {"name": "OBJ_EVENT_XX", "min": 2, "note": "object event family"},
    "ns": {"name": "NEW_SYNC?", "min": 6, "note": ""},
    "la": {"name": "PAIR_A?", "min": 12, "note": "two bounded i32 values (<16)"},
    "lu": {"name": "PAIR_B?", "min": 12, "note": "two bounded i32 values (<16)"},
}


def decode_header(pkt: bytes) -> dict:
    """Decode the common 18-byte BZRNet transport header."""
    if len(pkt) < HEADER_LEN:
        return {"error": f"packet shorter than {HEADER_LEN}-byte header", "length": len(pkt)}
    flags = pkt[0]
    kind = pkt[1] & 0x0F
    ts_ms = struct.unpack_from(">Q", pkt, 2)[0]
    seq_a = struct.unpack_from(">I", pkt, 10)[0]
    seq_b = struct.unpack_from(">I", pkt, 14)[0]
    out = {
        "header_len": HEADER_LEN,
        "flags_byte_hex": f"0x{flags:02X}",
        "reliable": bool(flags & RELIABLE_FLAG),
        "final_fragment_or_single": bool(flags & FINAL_FRAG_FLAG),
        "kind_nibble": kind,
        "timestamp_unix_ms": ts_ms,
        "timestamp_utc": datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat()
        if ts_ms > 10**11 else None,
        "seq_a_be_u32_at10": seq_a,
        "seq_b_be_u32_at14": seq_b,
        "seq_b_low16": seq_b & 0xFFFF,
        "payload_len": len(pkt) - HEADER_LEN,
    }
    if len(pkt) > MAX_PACKET:
        out["warning"] = f"exceeds native receiver limit {MAX_PACKET}"
    return out


def classify_discovery(pkt: bytes) -> dict | None:
    """Classify a UDP 1338 probe or reply (returns None if not discovery-shaped)."""
    if len(pkt) < HEADER_LEN:
        return None
    hdr = decode_header(pkt)
    if hdr.get("kind_nibble") != DISCOVERY_KIND:
        return None
    if len(pkt) == HEADER_LEN:
        return {"surface": "discovery_probe", **hdr}
    if len(pkt) >= HEADER_LEN + 7:
        ln = pkt[HEADER_LEN]
        trailer = pkt[HEADER_LEN:]
        if ln == 4 and len(trailer) == 7:
            ip = ".".join(str(b) for b in trailer[1:5])
            port = struct.unpack_from(">H", trailer, 5)[0]
            return {"surface": "discovery_reply_ipv4", "observed_ip": ip, "observed_port": port, **hdr}
        if ln == 16:
            return {"surface": "discovery_reply_ipv6", "trailer_len": len(trailer), **hdr}
    return {"surface": "discovery_other", **hdr}


def classify_game_payload(payload: bytes) -> dict:
    """Identify an in-match game payload by its 2-char ASCII opcode."""
    if not payload:
        return {"opcode_raw": "", "match": None}
    if payload[0] == 0x5F:  # '_'
        info = GAME_OPCODES["_S"]
        key = "_S"
    elif payload[:2].decode("latin1") in GAME_OPCODES:
        key = payload[:2].decode("latin1")
        info = GAME_OPCODES[key]
    else:
        key, info = None, None
    out = {
        "opcode_raw": (payload[:2] if len(payload) >= 2 else payload).decode("latin1"),
        "opcode_hex_u16le": f"0x{struct.unpack_from('<H', payload, 0)[0]:04X}" if len(payload) >= 2 else "",
        "match": key,
    }
    if info:
        out.update({"name": info["name"], "notes": info["note"]})
        if len(payload) < info["min"]:
            out["warning"] = f"shorter than native minimum {info['min']}"
    return out


def decode_packet(pkt: bytes, surface: str = "auto") -> dict:
    result = {"wire_len": len(pkt)}
    disc = classify_discovery(pkt) if surface in ("auto", "discovery") else None
    if disc and surface != "game":
        result.update(disc)
        return result
    result.update(decode_header(pkt))
    payload = pkt[HEADER_LEN:]
    result["game_payload"] = classify_game_payload(payload)
    return result


SELFTEST_VECTORS = [
    # (label, hex, expectations dict subset)
    ("march21_identity_host",
     "40030000019d1199210b000000000000000053373635363131393832343132353937303000",
     {"kind_nibble": 3, "reliable": False, "final_fragment_or_single": True,
      "seq_a_be_u32_at10": 0, "seq_b_be_u32_at14": 0}),
    ("march21_discovery_req",
     "40020000019d119834f30000000000000000",
     {"surface": "discovery_probe", "kind_nibble": 2}),
    ("march21_discovery_reply",
     "40020000019d119834f3000000000000000004d8472e2ffe2f",
     {"surface": "discovery_reply_ipv4", "observed_ip": "216.71.46.47"}),
]


def selftest() -> int:
    failures = 0
    for label, hexstr, expect in SELFTEST_VECTORS:
        got = decode_packet(bytes.fromhex(hexstr))
        for k, v in expect.items():
            if got.get(k) != v:
                print(f"FAIL {label}: {k} expected {v!r} got {got.get(k)!r}")
                failures += 1
        print(f"ok   {label}: {json.dumps(got, default=str)[:180]}")
    print("selftest", "FAILED" if failures else "PASSED")
    return 1 if failures else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--hex", help="hex-encoded datagram to decode")
    ap.add_argument("--surface", choices=["auto", "discovery", "game"], default="auto")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    if not args.hex:
        ap.error("provide --hex or --selftest")
    print(json.dumps(decode_packet(bytes.fromhex(args.hex), args.surface), indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
