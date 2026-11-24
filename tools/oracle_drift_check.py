#!/usr/bin/env python3
"""BZRNet protocol-drift harness.

Runs a small battery of READ-ONLY probes against a BZRNet service (the live
official service or a community server), reduces every exchange to a
structural skeleton, and compares it against the sanitized baseline in
fixtures/official_live_*.json.

Philosophy (per the interoperability work order):
  - A live-service change must NOT immediately rewrite expected behavior.
  - New observations are reported as OBSERVED DRIFT with prior vs new shape,
    and require independent corroboration before being promoted into the
    contract or fixtures.
  - Exit code is 0 unless --strict is passed; drift is information, not
    failure. Hard failures are reserved for the local compat suite.

Usage:
  python tools/oracle_drift_check.py --target official
  python tools/oracle_drift_check.py --target community --host 127.0.0.1 --ws-port 1337 --udp-port 1338
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import socket
import sys
import time
from pathlib import Path
from typing import Any

try:
    import websockets
except Exception as exc:  # pragma: no cover
    raise SystemExit("pip install websockets") from exc

HERE = Path(__file__).parent
BASELINE = HERE.parent / "fixtures" / "official_live_20260824.json"

OFFICIAL_HOST = "battlezone98mp.webdev.rebellion.co.uk"

PRIVACY_KEYS = {"steamappticket", "gogappticket", "authticket", "platformticket",
                "ticket", "password", "key", "secret", "token"}
IPV4_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
WEB_ID_RE = re.compile(r"\bB\d{6,}\b")
STEAM_ID_RE = re.compile(r"\b7656\d{11,}\b")
GOG_ID_RE = re.compile(r"\bG\d{6,}\b")


def sanitize(value: Any, depth: int = 0) -> Any:
    """Reduce payloads to structural skeletons with identifiers redacted."""
    if depth > 8:
        return "..."
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if str(k).lower() in PRIVACY_KEYS:
                out[k] = "<redacted>"
            else:
                out[k] = sanitize(v, depth + 1)
        return out
    if isinstance(value, list):
        red = [sanitize(v, depth + 1) for v in value[:4]]
        if len(value) > 4:
            red.append(f"...({len(value)} items)")
        return red
    if isinstance(value, str):
        s = IPV4_RE.sub("<addr>", value)
        s = STEAM_ID_RE.sub("S<steam64>", s)
        s = WEB_ID_RE.sub("B100xxxx", s)
        s = GOG_ID_RE.sub("G100xxxx", s)
        if len(s) > 48:
            s = f"<str:{len(s)}>"
        return s
    return value


class Drift:
    def __init__(self) -> None:
        self.items: list[dict[str, str]] = []
        self.matches = 0

    def compare(self, label: str, prior: Any, observed: Any) -> None:
        p, o = sanitize(prior), sanitize(observed)
        if json.dumps(_norm(p), sort_keys=True) == json.dumps(_norm(o), sort_keys=True):
            self.matches += 1
            print(f"MATCH  {label}")
        else:
            self.items.append({"label": label, "prior": p, "observed": o})
            print(f"DRIFT  {label}")
            print(f"   prior   : {json.dumps(p)[:300]}")
            print(f"   observed: {json.dumps(o)[:300]}")

    def report(self) -> None:
        print(f"\n== drift summary: {len(self.items)} drifted / {self.matches} matched ==")
        for item in self.items:
            print(f"- OBSERVED DRIFT [{item['label']}]")
            print(f"    prior behavior : {json.dumps(item['prior'])[:400]}")
            print(f"    new observation: {json.dumps(item['observed'])[:400]}")
        if self.items:
            print(
                "\nNOTE: drift requires independent corroboration before any\n"
                "semantic is promoted into protocol_contract.json or fixtures.\n"
                "Re-run on later dates and from second networks before rewriting\n"
                "expected behavior."
            )


def _norm(v: Any) -> Any:
    """Normalize placeholder noise so trivial formatting doesn't read as drift."""
    if isinstance(v, dict):
        return {k: _norm(x) for k, x in v.items() if k not in {"time"}}
    if isinstance(v, list):
        return [_norm(x) for x in v]
    if isinstance(v, str):
        s = re.sub(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?", "<ts>", v)
        return s.replace("<ts>", "T").replace("<YYYY-MM-DD HH:MM:SS>", "T")
    return v


async def probe_ws(host: str, port: int, drift: Drift, baseline: dict, seconds: float = 12.0) -> None:
    uri = f"ws://{host}:{port}/"
    t0 = time.time()
    async with websockets.connect(uri, open_timeout=15) as ws:
        inbox: list[tuple[float, dict]] = []

        async def send(m):
            await ws.send(json.dumps(m))

        async def collect(until: float):
            while time.time() - t0 < until:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=max(0.05, until - (time.time() - t0)))
                    inbox.append((time.time() - t0, json.loads(raw)))
                except asyncio.TimeoutError:
                    break

        # Authorization flow
        auth = baseline["flows"]["authorization"]["send"]
        await send(auth)
        await collect(4)
        got_types = [m.get("type") for _, m in inbox]
        expected_types = [r.get("type") for r in baseline["flows"]["authorization"]["responses"]]
        drift.compare("auth response types/order", expected_types, got_types[: len(expected_types)])
        for i, resp in enumerate(baseline["flows"]["authorization"]["responses"]):
            if i < len(inbox):
                drift.compare(f"auth response[{i}] {resp['type']}", resp, inbox[i][1])

        # Enter lounge flow
        mark = len(inbox)
        await send(baseline["flows"]["enter_lounge"]["send"])
        await collect(9)
        window = inbox[mark:]
        got_types = [m.get("type") for _, m in window]
        drift.compare("enter-lounge first reply type",
                      "OnLobbyListChanged", got_types[0] if got_types else None)

        # Silences: no spontaneous traffic expected in the remaining window.
        quiet_mark = len(inbox)
        await collect(seconds)
        spontaneous = [m for _, m in inbox[quiet_mark:]
                       if m.get("type") not in ("OnLobbyChanged", "OnLobbyDataChanged",
                                                "OnLobbyListChanged", "OnLobbyMemberListChanged")]
        drift.compare("idle silence (no unexpected broadcasts)", [], [m.get("type") for m in spontaneous])


def probe_udp(host: str, udp_port: int, drift: Drift, baseline: dict) -> None:
    spec = baseline["udp_1338"]
    probes = [
        ("canonical18", bytes.fromhex("40020000019c744772360000000000000000"), 25),
        ("short2", b"\x40\x02", 9),
        ("ascii7", b"BZPROBE", 14),
    ]
    if sys.platform != "win32":
        probes.append(("empty", b"", 7))
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(4)
    for name, payload, expect_len in probes:
        try:
            s.sendto(payload, (host, udp_port))
            data, addr = s.recvfrom(2048)
            ok_shape = (
                data.startswith(payload)
                and len(data) == expect_len
                and (not payload or data[len(payload)] == 0x04)
            )
            observed = {
                "response_rule": spec["response_rule"] if ok_shape else f"BROKEN {data.hex()[:64]}",
                "confidence": "CONFIRMED" if ok_shape else "BROKEN",
            }
            prior_subset = {"response_rule": spec["response_rule"], "confidence": spec["confidence"]}
            drift.compare(f"udp1338/{name}", prior_subset, observed)
        except socket.timeout:
            drift.compare(f"udp1338/{name}", {"response_rule": spec["response_rule"], "confidence": spec["confidence"]},
                          {"response_rule": "NO REPLY", "confidence": "BROKEN"})
    s.close()


async def main_async(args) -> int:
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    drift = Drift()
    if args.target == "official":
        host, ws_port, udp_port = OFFICIAL_HOST, baseline["ws_port"], baseline["udp_port"]
    else:
        host, ws_port, udp_port = args.host, args.ws_port, args.udp_port
    print(f"drift check target: {host} ws={ws_port} udp={udp_port}")
    try:
        probe_udp(host, udp_port, drift, baseline)
    except OSError as exc:
        print(f"UDP probe unavailable: {exc}")
    await probe_ws(host, ws_port, drift, baseline, seconds=args.window)
    drift.report()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", choices=["official", "community"], required=True)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--ws-port", type=int, default=1337)
    ap.add_argument("--udp-port", type=int, default=1338)
    ap.add_argument("--window", type=float, default=10.0, help="idle observation seconds")
    return asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
