#!/usr/bin/env python3
"""Relay impairment tests (relay_impairment.py and its relay hook).

Covers:
  1. spec parsing, rejection and canonical round trip;
  2. seeded determinism and loss rate;
  3. outage windows, delay/jitter ordering, reorder, duplication;
  4. the per-player uplink cap and tail drop;
  5. peer/direction selection and for= expiry;
  6. the relay hot path: drops, scheduled sends, trace records, live set/clear,
     and byte-exact forwarding once cleared.

Run: python relay_impairment_test.py
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

from relay_impairment import ImpairmentSpec, RelayImpairment, parse_spec
from server import BZRRelayProtocol, RelayTraceWriter, UserSession

logging.basicConfig(level=logging.CRITICAL)

A = ("203.0.113.10", 40001)
B = ("203.0.113.20", 40002)
STREAM = (A[0], A[1], B[0], B[1])


def mk_datagram(seq: int, size: int = 64) -> bytes:
    header = struct.pack(">BBQII", 0x40, 5, 1_779_000_000_000, seq, 0)
    return header + bytes(max(0, size - len(header)))


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_parsing() -> list[str]:
    failures = []
    for bad in ("", "loss", "loss=", "loss=x", "loss=101", "loss=-1", "delay=nan", "outage=5",
                "outage=0/100", "outage=200/100", "rate=0", "dir=from", "peer=", "peer=a,dir=up",
                "for=0", "seed=1.5", "bogus=1", "loss=1,loss=2"):
        try:
            parse_spec(bad)
        except ValueError:
            continue
        failures.append(f"spec {bad!r} unexpectedly accepted")
    spec = parse_spec("loss=3%,delay=80,jitter=20,reorder=1,reorder_ms=40,dup=0.5,rate=256,queue=150,"
                      "outage=1000/20000,peer=Coop1|Coop2,dir=from,for=30,seed=7")
    want = ImpairmentSpec(seed=7, loss=0.03, outage_ms=1000, outage_every_ms=20000, delay_ms=80, jitter_ms=20,
                          reorder=0.01, reorder_ms=40, dup=0.005, rate_kbps=256, queue_ms=150,
                          peers=frozenset({"Coop1", "Coop2"}), direction="from", duration_s=30)
    if spec != want:
        failures.append(f"parsed {spec}, want {want}")
    if parse_spec(spec.describe()) != spec:
        failures.append(f"describe() does not round trip: {spec.describe()}")
    if parse_spec("loss=3") != parse_spec("loss=3%"):
        failures.append("loss=3 and loss=3% differ")
    return failures


def run_stream(spec: str, count: int, *, step: float = 0.01, size: int = 64) -> list[tuple[int, Any]]:
    clock = Clock()
    imp = RelayImpairment(parse_spec(spec), clock=clock)
    out = []
    for i in range(count):
        clock.now = 1000.0 + i * step
        out.append((i, imp.decide(size, STREAM, "userA")))
    return out


def test_determinism_and_loss() -> list[str]:
    failures = []
    first = run_stream("loss=10,seed=3", 5000)
    second = run_stream("loss=10,seed=3", 5000)
    other = run_stream("loss=10,seed=4", 5000)
    if first != second:
        failures.append("same seed gave different decisions")
    if first == other:
        failures.append("different seeds gave identical decisions")
    dropped = sum(1 for _, d in first if d.drop == "loss")
    if not 400 <= dropped <= 600:
        failures.append(f"loss=10 dropped {dropped}/5000")
    if any(d.drop is None and d.delays != (0.0,) for _, d in first):
        failures.append("loss-only spec delayed a datagram")
    if sum(1 for _, d in run_stream("loss=100", 200) if d.drop) != 200:
        failures.append("loss=100 let a datagram through")
    if any(d.drop for _, d in run_stream("seed=9", 200)):
        failures.append("empty impairment dropped a datagram")
    return failures


def test_outage() -> list[str]:
    failures = []
    # 10 ms steps over 3 s with a 200 ms outage every 1000 ms: windows at
    # [0.8, 1.0), [1.8, 2.0), [2.8, 3.0) seconds after applying.
    decisions = run_stream("outage=200/1000", 300)
    dropped = [i for i, d in decisions if d.drop == "outage"]
    want = [i for i in range(300) if (i * 10) % 1000 >= 800]
    if dropped != want:
        failures.append(f"outage dropped {len(dropped)} at {dropped[:5]}..., want {len(want)} at {want[:5]}...")
    return failures


def test_delay_order_reorder_dup() -> list[str]:
    failures = []
    # Jitter alone never reorders a stream.
    decisions = run_stream("delay=80,jitter=60,seed=5", 2000, step=0.002)
    send_times = [i * 0.002 + d.delays[0] for i, d in decisions]
    if any(b < a - 1e-9 for a, b in zip(send_times, send_times[1:])):
        failures.append("jitter reordered a stream")
    if min(d.delays[0] for _, d in decisions) < 0.020 - 1e-9:
        failures.append("a delay fell below delay - jitter")

    # reorder=100 holds every datagram past the floor, so each one is sent
    # reorder_ms after it arrives and the floor never moves.
    decisions = run_stream("reorder=100,reorder_ms=40", 50, step=0.005)
    if not all(d.reordered and abs(d.delays[0] - 0.040) < 1e-9 for _, d in decisions):
        failures.append("reorder=100 did not hold every datagram by reorder_ms")
    # A mix produces at least one overtaken datagram.
    decisions = run_stream("reorder=20,reorder_ms=50,seed=2", 500, step=0.005)
    send_times = [i * 0.005 + d.delays[0] for i, d in decisions]
    if not any(b < a for a, b in zip(send_times, send_times[1:])):
        failures.append("reorder=20 never let a datagram overtake")

    decisions = run_stream("dup=100", 20)
    if not all(len(d.delays) == 2 for _, d in decisions):
        failures.append("dup=100 did not duplicate every datagram")
    decisions = run_stream("dup=10,seed=11", 2000)
    dups = sum(1 for _, d in decisions if len(d.delays) == 2)
    if not 140 <= dups <= 260:
        failures.append(f"dup=10 duplicated {dups}/2000")
    return failures


def test_rate_cap() -> list[str]:
    failures = []
    clock = Clock()
    # 80 kbit/s = 10 bytes per ms: a 1000-byte datagram takes 100 ms.
    imp = RelayImpairment(parse_spec("rate=80,queue=250"), clock=clock)
    other = (B[0], B[1], A[0], A[1])
    delays = []
    for i in range(5):
        d = imp.decide(1000, STREAM if i % 2 == 0 else other, "userA")
        delays.append(None if d.drop else round(d.delays[0], 6))
    # Shared uplink: 100, 200, 300 ms; the 4th would wait 300 ms > queue.
    if delays[:3] != [0.1, 0.2, 0.3] or delays[3] is not None:
        failures.append(f"uplink delays {delays}")
    if imp.totals["dropped_queue"] < 1:
        failures.append("queue overflow was not counted")
    # Another player's uplink is independent (on a stream with no backlog).
    d = imp.decide(1000, ("203.0.113.30", 40003, A[0], A[1]), "userB")
    if d.drop or round(d.delays[0], 6) != 0.1:
        failures.append(f"second uplink not independent: {d}")
    # The queue drains with time.
    clock.now += 1.0
    d = imp.decide(1000, STREAM, "userA")
    if d.drop or round(d.delays[0], 6) != 0.1:
        failures.append(f"uplink did not drain: {d}")
    return failures


def test_peer_selection_and_expiry() -> list[str]:
    failures = []
    clock = Clock()
    imp = RelayImpairment(parse_spec("loss=100,peer=Coop1"), clock=clock)
    if not imp.decide(10, STREAM, "u1", {"u1", "Coop1"}, {"u2", "Coop2"}).drop:
        failures.append("peer=Coop1 did not match its outgoing datagram")
    if not imp.decide(10, STREAM, "u2", {"u2", "Coop2"}, {"u1", "Coop1"}).drop:
        failures.append("peer=Coop1 did not match its incoming datagram")
    if imp.decide(10, STREAM, "u2", {"u2", "Coop2"}, {"u3", "Coop3"}).drop:
        failures.append("peer=Coop1 matched an unrelated stream")
    imp = RelayImpairment(parse_spec("loss=100,peer=Coop1,dir=from"), clock=clock)
    if imp.decide(10, STREAM, "u2", {"Coop2"}, {"Coop1"}).drop:
        failures.append("dir=from matched an incoming datagram")
    imp = RelayImpairment(parse_spec("loss=100,peer=Coop1,dir=to"), clock=clock)
    if imp.decide(10, STREAM, "u1", {"Coop1"}, {"Coop2"}).drop:
        failures.append("dir=to matched an outgoing datagram")

    imp = RelayImpairment(parse_spec("loss=100,for=5"), clock=clock)
    if not imp.decide(10, STREAM, "u1").drop:
        failures.append("for=5 inactive at once")
    clock.now += 5.0
    if not imp.expired() or imp.decide(10, STREAM, "u1").drop:
        failures.append("for=5 still active after 5 s")
    return failures


# ------------------------------------------------------------- relay --
class FakeTransport:
    def __init__(self) -> None:
        self.sent: list[tuple[bytes, tuple[str, int]]] = []
        self.closing = False

    def get_extra_info(self, name: str) -> Any:
        return ("198.51.100.10", 1339) if name == "sockname" else None

    def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
        self.sent.append((bytes(data), addr))

    def is_closing(self) -> bool:
        return self.closing


class FakeState:
    def __init__(self) -> None:
        self.sessions_by_user_id: dict[str, UserSession] = {}

    def record_protocol_event(self, **kwargs: Any) -> None:
        pass

    def record_stdout_entry(self, *args: Any, **kwargs: Any) -> None:
        pass


def make_relay(trace_path: str | None = None) -> tuple[BZRRelayProtocol, FakeTransport, list]:
    state = FakeState()
    kwargs: dict[str, Any] = {}
    if trace_path is not None:
        kwargs["trace_writer"] = RelayTraceWriter(trace_path, run_id="testrun000002")
    relay = BZRRelayProtocol(logging.getLogger("test-relay"), state, **kwargs)  # type: ignore[arg-type]
    scheduled: list[tuple[float, Any, tuple]] = []
    relay.schedule_later = lambda delay, cb, *args: scheduled.append((delay, cb, args))  # type: ignore[method-assign]
    transport = FakeTransport()
    relay.connection_made(transport)  # type: ignore[arg-type]
    session_a = UserSession(user_id="userA", name="Coop0", current_lobby_id=1, wan_address=f"{A[0]}:{A[1]}")
    session_b = UserSession(user_id="userB", name="Coop1", current_lobby_id=1, wan_address=f"{B[0]}:{B[1]}")
    state.sessions_by_user_id = {"userA": session_a, "userB": session_b}
    ok, reason = relay.register_route(session_a, session_b)
    assert ok, reason
    return relay, transport, scheduled


def test_relay_hotpath() -> list[str]:
    failures = []
    tmp = tempfile.mkdtemp(prefix="bzr_impair_")
    trace_path = os.path.join(tmp, "relay_trace.jsonl")
    relay, transport, scheduled = make_relay(trace_path)

    relay.set_impairment("loss=100,peer=Coop1,dir=to")
    relay.datagram_received(mk_datagram(1), A)          # to Coop1: dropped
    relay.datagram_received(mk_datagram(2), B)          # from Coop1: passes at once
    if [d for d, _ in transport.sent] != [mk_datagram(2)] or scheduled:
        failures.append(f"peer drop: sent={len(transport.sent)} scheduled={len(scheduled)}")
    m = relay.metrics()
    if m["impairmentDropsTotal"] != 1 or m["impairment"]["dropped_loss"] != 1:
        failures.append(f"drop counters wrong: {m['impairmentDropsTotal']} {m['impairment']}")

    transport.sent.clear()
    relay.set_impairment("delay=50,dup=100")
    data = mk_datagram(3)
    relay.datagram_received(data, A)
    if transport.sent or len(scheduled) != 2 or any(abs(d - 0.05) > 1e-6 for d, _, _ in scheduled):
        failures.append(f"delay+dup: sent={len(transport.sent)} scheduled={[(d) for d, _, _ in scheduled]}")
    for _, cb, args in scheduled:
        cb(*args)
    if transport.sent != [(data, B), (data, B)]:
        failures.append("delayed copies not byte-exact to the right target")
    scheduled.clear()
    transport.closing = True
    relay.datagram_received(data, A)
    for _, cb, args in scheduled:
        cb(*args)
    if len(transport.sent) != 2 or relay.impair_late_drop_count != 2:
        failures.append("a delayed send went out on a closing socket")
    transport.closing = False
    scheduled.clear()

    transport.sent.clear()
    relay.set_impairment("")
    data = mk_datagram(4)
    relay.datagram_received(data, A)
    if transport.sent != [(data, B)] or scheduled:
        failures.append("forwarding not immediate and byte-exact once cleared")
    if relay.metrics()["impairment"] != {"active": False, "spec": ""}:
        failures.append(f"cleared impairment still in metrics: {relay.metrics()['impairment']}")
    try:
        relay.set_impairment("loss=banana")
        failures.append("relay accepted a bad spec")
    except ValueError:
        pass

    assert relay.trace_writer is not None
    relay.trace_writer.close()
    with open(trace_path, encoding="utf-8") as fh:
        lines = [json.loads(line) for line in fh if line.strip()]
    dispositions = [r.get("disposition") for r in lines if "disposition" in r]
    if dispositions[:2] != ["dropped_impair_loss", "forwarded"]:
        failures.append(f"trace dispositions {dispositions}")
    delayed = [r for r in lines if r.get("impairment", {}).get("delaysMs") == [50.0, 50.0]]
    if not delayed:
        failures.append("trace lacks the delay detail")
    events = [r["event"] for r in lines if "event" in r]
    if events.count("impairment_set") != 2 or "impairment_cleared" not in events:
        failures.append(f"trace events {events}")
    if "impairment" in lines[-1]:
        failures.append("unimpaired datagram carries impairment detail")
    return failures


def test_relay_expiry() -> list[str]:
    failures = []
    relay, transport, scheduled = make_relay()
    relay.set_impairment("loss=100,for=0.001")
    assert relay.impairment is not None
    relay.impairment.applied_at -= 1.0
    relay.datagram_received(mk_datagram(5), A)
    if relay.impairment is not None or len(transport.sent) != 1:
        failures.append("expired impairment still applied by the relay")
    return failures


def main() -> int:
    failures = []
    for test in (test_parsing, test_determinism_and_loss, test_outage, test_delay_order_reorder_dup,
                 test_rate_cap, test_peer_selection_and_expiry, test_relay_hotpath, test_relay_expiry):
        got = test()
        print(f"{'PASS' if not got else 'FAIL'} {test.__name__}")
        failures += [f"{test.__name__}: {f}" for f in got]
    for f in failures:
        print("  " + f)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
