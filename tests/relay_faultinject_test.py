#!/usr/bin/env python3
"""Phase-2 relay instrumentation tests (work order §4, instrumentation only).

Covers:
  1. bare-0x80 reliable-nonfinal predicate truth table;
  2. fault-injection spec parsing and per-stream k-th selection;
  3. relay hot-path transparency with instrumentation OFF (byte-exact forwarding);
  4. relay hot-path drop/forward behavior with instrumentation ON;
  5. forensic trace schema (required fields, payload bytes never present).

Run: python tests/relay_faultinject_test.py
Exits non-zero on any failure. Uses documentation-range IPs only.
"""

from __future__ import annotations

import json
import logging
import os
import struct
import sys
import tempfile
from typing import Any

from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT)]

from server import (
    BZR_TRANSPORT_HEADER_BYTES,
    BZRRelayProtocol,
    RelayFaultInjector,
    RelayTraceWriter,
    UserSession,
    _is_reliable_nonfinal_fragment,
)

logging.basicConfig(level=logging.CRITICAL)


def mk_datagram(
    *,
    flags: int = 0x80,
    kind: int = 5,
    ts_ms: int = 1_779_000_000_000,
    seq_a: int = 0,
    seq_b: int = 0,
    payload: bytes = b"\x00\x01\x02",
) -> bytes:
    return struct.pack(">BBQII", flags, kind, ts_ms, seq_a, seq_b) + payload


class FakeTransport:
    def __init__(self) -> None:
        self.sent: list[tuple[bytes, tuple[str, int]]] = []

    def get_extra_info(self, name: str) -> Any:
        if name == "sockname":
            return ("198.51.100.10", 1339)
        return None

    def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
        self.sent.append((bytes(data), addr))


class FakeState:
    def record_protocol_event(self, **kwargs: Any) -> None:  # pragma: no cover
        pass

    def record_stdout_entry(self, *args: Any, **kwargs: Any) -> None:  # pragma: no cover
        pass


def make_relay(fault_spec: str | None, trace_path: str | None) -> tuple[BZRRelayProtocol, FakeTransport]:
    logger = logging.getLogger("test-relay")
    kwargs: dict[str, Any] = {}
    if fault_spec is not None:
        kwargs["fault_injector"] = RelayFaultInjector(fault_spec)
    if trace_path is not None:
        kwargs["trace_writer"] = RelayTraceWriter(trace_path, run_id="testrun000001")
    relay = BZRRelayProtocol(logger, FakeState(), **kwargs)
    transport = FakeTransport()
    relay.connection_made(transport)  # type: ignore[arg-type]
    session_a = UserSession(user_id="userA", current_lobby_id=1, wan_address="203.0.113.10:40001")
    session_b = UserSession(user_id="userB", current_lobby_id=1, wan_address="203.0.113.20:40002")
    ok, reason = relay.register_route(session_a, session_b)
    assert ok, reason
    return relay, transport


def test_predicate() -> list[str]:
    failures = []
    cases = [
        (mk_datagram(flags=0x80), True),   # bare 0x80: reliable mid fragment
        (mk_datagram(flags=0xC0), False),  # reliable + final
        (mk_datagram(flags=0x40), False),  # unreliable single
        (mk_datagram(flags=0x00), False),
        (b"\x80", False),                  # shorter than header
        (b"\x80\x05" + b"\x00" * 14, False),  # exactly 16 bytes: still short
    ]
    for data, expected in cases:
        got = _is_reliable_nonfinal_fragment(data)
        if got != expected:
            failures.append(f"predicate({data[:2].hex()}… len={len(data)}) = {got}, want {expected}")
    return failures


def test_injector_parsing() -> list[str]:
    failures = []
    for spec in ("", "nonsense", "drop_reliable_midfrag", "drop_reliable_midfrag:k=0",
                 "drop_reliable_midfrag:k=x", "drop_reliable_midfrag:k"):
        try:
            RelayFaultInjector(spec)
        except ValueError:
            continue
        failures.append(f"spec {spec!r} unexpectedly accepted")
    inj = RelayFaultInjector("drop_reliable_midfrag:k=2")
    if not inj.active or inj.mode != "drop_reliable_midfrag" or inj.k != 2:
        failures.append(f"valid spec parsed wrong: {inj.mode}/{inj.k}")
    return failures


def test_injector_selection() -> list[str]:
    failures = []
    inj = RelayFaultInjector("drop_reliable_midfrag:k=2")
    stream = ("203.0.113.10", 40001, "203.0.113.20", 40002)
    other = ("203.0.113.20", 40002, "203.0.113.10", 40001)
    d1 = mk_datagram(flags=0x80, seq_a=1)
    d2 = mk_datagram(flags=0x80, seq_a=2)
    d3 = mk_datagram(flags=0xC0, seq_a=3)   # final: never matches
    d4 = mk_datagram(flags=0x40, seq_a=4)   # unreliable: never matches
    seq_expect = [
        (d1, stream, False),  # match ordinal 1 -> forward
        (d2, stream, True),   # match ordinal 2 -> DROP
        (d3, stream, False),  # no match
        (d4, stream, False),  # no match
        (d1, other, False),   # independent stream, ordinal 1
        (d2, other, True),    # independent stream, ordinal 2
    ]
    for i, (data, key, want_drop) in enumerate(seq_expect):
        got_drop = inj.decide_drop(data, key)
        if got_drop != want_drop:
            failures.append(f"selection[{i}] drop={got_drop}, want {want_drop}")
    if inj.dropped_count != 2 or inj.forwarded_count != 4:
        failures.append(f"counters wrong: dropped={inj.dropped_count} forwarded={inj.forwarded_count}")
    metrics = inj.metrics()
    if metrics["dropped"] != 2 or metrics["lastDropped"]["seqA"] != 2:
        failures.append(f"metrics wrong: {metrics}")
    return failures


def test_hotpath_transparent_without_instrumentation() -> list[str]:
    failures = []
    relay, transport = make_relay(fault_spec=None, trace_path=None)
    data = mk_datagram(flags=0x80, seq_a=7, payload=b"\xAA" * 32)
    relay.datagram_received(data, ("203.0.113.10", 40001))
    if len(transport.sent) != 1:
        failures.append(f"expected exactly one sendto, got {len(transport.sent)}")
    elif transport.sent[0][0] != data:
        failures.append("forwarded datagram is not byte-exact")
    elif transport.sent[0][1] != ("203.0.113.20", 40002):
        failures.append(f"forwarded to wrong target {transport.sent[0][1]}")
    m = relay.metrics()
    if m["faultInjector"] != {"mode": "", "active": False}:
        failures.append(f"inactive injector leaked into metrics: {m['faultInjector']}")
    if m["trace"] != {"enabled": False}:
        failures.append(f"inactive trace leaked into metrics: {m['trace']}")
    return failures


def test_hotpath_with_instrumentation() -> list[str]:
    failures = []
    tmp = tempfile.mkdtemp(prefix="bzr_faultinject_")
    try:
        trace_path = os.path.join(tmp, "relay_trace.jsonl")
        relay, transport = make_relay(
            fault_spec="drop_reliable_midfrag:k=1",
            trace_path=trace_path,
        )
        first = mk_datagram(flags=0x80, seq_a=11)   # k=1 -> DROPPED
        relay.datagram_received(first, ("203.0.113.10", 40001))
        second = mk_datagram(flags=0xC0, seq_a=12)  # final -> forwarded
        relay.datagram_received(second, ("203.0.113.10", 40001))

        # Exactly one sendto: the final fragment. The bare-0x80 first datagram
        # must have been dropped before any send.
        if len(transport.sent) != 1:
            failures.append(f"expected exactly 1 forwarded send, got {len(transport.sent)}")
        elif transport.sent[0][0] != second:
            failures.append("forwarded final fragment is not byte-exact")
        elif transport.sent[0][1] != ("203.0.113.20", 40002):
            failures.append(f"forwarded to wrong target {transport.sent[0][1]}")
        m = relay.metrics()
        fi = m["faultInjector"]
        if fi["dropped"] != 1 or fi["forwarded"] != 1:
            failures.append(f"injector counters wrong after hot path: {fi}")

        assert relay.trace_writer is not None
        relay.trace_writer.close()
        with open(trace_path, encoding="utf-8") as fh:
            lines = [json.loads(line) for line in fh if line.strip()]
        header = lines[0] if lines else {}
        if header.get("format") != RelayTraceWriter.FORMAT_NAME or header.get("runId") != "testrun000001":
            failures.append(f"trace header wrong: {header}")
        rows = [r for r in lines if r.get("disposition") in ("forwarded", "dropped_faultinject")]
        if len(rows) != 2:
            failures.append(f"expected 2 traced routed datagrams, got {len(rows)}")
        required = {
            "runId", "tsMonotonic", "tsUnixMs", "direction", "source", "target",
            "disposition", "transport",
        }
        dispositions = sorted(r.get("disposition", "") for r in rows)
        if dispositions != ["dropped_faultinject", "forwarded"]:
            failures.append(f"dispositions wrong: {dispositions}")
        for r in rows:
            missing = required - set(r)
            if missing:
                failures.append(f"trace row missing fields: {missing}")
            t = r.get("transport", {})
            for field in ("flagsByte", "kindNibble", "headerTimestampUnixMs", "seqA", "seqB", "payloadBytes", "wireBytes"):
                if field not in t:
                    failures.append(f"trace transport missing {field}")
            if t.get("payloadBytes") != max(0, int(t.get("wireBytes", 0)) - BZR_TRANSPORT_HEADER_BYTES):
                failures.append(f"payloadBytes/wireBytes inconsistent: {t}")
        # Payload bytes must never appear anywhere in the trace file.
        raw_text = open(trace_path, encoding="utf-8").read()
        if "aaaa" in raw_text.lower():
            failures.append("payload-like bytes leaked into trace")
    finally:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)
    return failures


def main() -> int:
    failures: list[str] = []
    for test in (
        test_predicate,
        test_injector_parsing,
        test_injector_selection,
        test_hotpath_transparent_without_instrumentation,
        test_hotpath_with_instrumentation,
    ):
        problems = test()
        status = "PASS" if not problems else "FAIL"
        print(f"{status} - {test.__name__}")
        failures.extend(problems)
    if failures:
        print("\nFailures:")
        for f in failures:
            print(f"  - {f}")
        print(f"\n{len(failures)} check(s) FAILED")
        return 1
    print("\nAll relay instrumentation checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
