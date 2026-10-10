#!/usr/bin/env python3
"""Read native sequence rejection logs; relay forwarding alone is not health.

This does not compute a packet-loss percentage. It flags a sustained native
rejection frequency after a configurable loading grace interval. A Drop line
can print expected=0 even when the adjacent Received line has a nonzero
expectation. Pair those records conservatively; unmatched expectations remain
unknown. The historical frequency gate is unchanged and does not measure a
continuous blackout or imply a sequence reset.
Optional relay traces describe opaque forwarding and cumulative ACK progress;
they cannot independently prove native packet acceptance.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime
import json
from pathlib import Path
import re

TIMESTAMP = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d+)")
DROP = re.compile(r"Dropping Packet Type (\d+) For Client (\S+) \(Packet #(\d+) received, #(\d+) expected\)")
CONNECT = re.compile(r"Completed (?:RELAY|WAN|LAN) Connect For Client (\S+), using address (\S+)")
SENT = re.compile(r"(?:TRY|REL|UNR) Sent Packet \((\d+),(\d+)\) to ([^: ]+:\d+):")
RECEIVED = re.compile(r"Received Packet \((\d+),(\d+)\) expecting \((\d+),(\d+)\) from (\S+) sent at \d+, received at \d+, diff -?\d+: ([0-9a-fA-F]+)\s*$")


def paired_receive(previous: str, timestamp: float, rejected, routes: dict):
    """Only an adjacent, matching, complete native header can identify a drop."""
    stamp = TIMESTAMP.match(previous)
    received = RECEIVED.search(previous)
    if not stamp or not received:
        return None, None
    age = timestamp - datetime.fromisoformat(stamp[1]).timestamp()
    if not 0 <= age <= 0.05 or routes.get(received[5]) != rejected[2]:
        return None, None
    try:
        header = bytes.fromhex(received[6])
    except ValueError:
        return None, None
    if (len(header) < 18 or int.from_bytes(header[10:14], "big") != int(received[1]) or
            int.from_bytes(header[14:18], "big") != int(received[2]) or
            int(received[1]) != int(rejected[3]) or header[1] != int(rejected[1])):
        return None, None
    return int(received[3]), header[0]


def analyze_native_text(text: str, *, grace_seconds: float = 5,
                        window_seconds: int = 5, rejection_threshold: int = 20,
                        active_seconds_threshold: int = 4) -> dict:
    lines = []
    raw_lines = text.splitlines()
    for line in raw_lines:
        match = TIMESTAMP.match(line)
        if match:
            lines.append((datetime.fromisoformat(match[1]).timestamp(), line))
    starts = [t for t, line in lines if '[CRFLOW]' in line and '"k":"attach"' in line]
    if not starts:
        starts = [t for t, line in lines if "SetRunning:" in line and "now RUN_STARTED" in line]
    if not starts:
        return {"status": "incomplete", "reason": "no mission-entry marker", "peers": []}
    start = starts[0]
    stop = next((t for t, line in lines if t >= start and "Closing application due to WM_CLOSE" in line),
                lines[-1][0] if lines else start)
    routes = {}
    drops = defaultdict(list)
    acks = defaultdict(list)
    for index, line in enumerate(raw_lines):
        stamp = TIMESTAMP.match(line)
        if not stamp:
            continue
        timestamp = datetime.fromisoformat(stamp[1]).timestamp()
        connected = CONNECT.search(line)
        if connected:
            routes[connected[2]] = connected[1]
        sent = SENT.search(line)
        if sent and sent[3] in routes and start <= timestamp <= stop:
            acks[routes[sent[3]]].append((timestamp, int(sent[2])))
        rejected = DROP.search(line)
        if rejected and start <= timestamp <= stop:
            expected, flags = paired_receive(raw_lines[index - 1] if index else "", timestamp, rejected, routes)
            drops[rejected[2]].append((timestamp, int(rejected[1]), int(rejected[3]), int(rejected[4]), expected, flags))
    peers = []
    for peer in sorted(set(routes.values()) | set(drops)):
        events = drops[peer]
        after_grace = [e for e in events if e[0] >= start + grace_seconds]
        zero_expected = [e for e in after_grace if e[1] == 0 and e[3] == 0]
        # Stale reliable retransmissions have expected>received. They are
        # diagnostics, not sustained-unreliable-health failures.
        stale = [e for e in after_grace if e[3] > e[2]]
        bins = Counter(int(e[0] - start) for e in zero_expected)
        worst = {"count": 0, "activeSeconds": 0, "startSeconds": None}
        sustained = []
        for second in sorted(bins):
            interval = [bins[s] for s in range(second, second + window_seconds)]
            count = sum(interval)
            active = sum(value > 0 for value in interval)
            if count > worst["count"]:
                worst = {"count": count, "activeSeconds": active, "startSeconds": second}
            if count >= rejection_threshold and active >= active_seconds_threshold:
                sustained.append(second)
        ack_values = acks[peer]
        latest_drop = zero_expected[-1] if zero_expected else None
        preceding_acks = [(t, ack) for t, ack in ack_values if latest_drop and t <= latest_drop[0]]
        lag = latest_drop[2] - preceding_acks[-1][1] if latest_drop and preceding_acks else None
        ack_age = latest_drop[0] - preceding_acks[-1][0] if latest_drop and preceding_acks else None
        peers.append({
            "peer": peer, "status": "fail" if sustained else "pass",
            "rejectionsTotal": len(events), "loadingGraceRejections": len(events) - len(after_grace),
            "zeroExpectedAfterGrace": len(zero_expected), "staleAfterGrace": len(stale),
            "unreliableRejectedAfterGrace": sum(e[5] is not None and not (e[5] & 0x80) for e in after_grace),
            "actualReceiveExpectedZero": sum(e[4] == 0 for e in zero_expected),
            "actualReceiveExpectedNonzero": sum(e[4] is not None and e[4] != 0 for e in zero_expected),
            "actualReceiveExpectedUnknown": sum(e[4] is None for e in zero_expected),
            "worstWindow": worst, "sustainedWindowStartsSeconds": sustained,
            "ackFirst": ack_values[0][1] if ack_values else None,
            "ackLast": ack_values[-1][1] if ack_values else None,
            "lastZeroExpectedReceivedSequence": latest_drop[2] if latest_drop else None,
            "lastZeroExpectedMinusAck": lag,
            "ackSampleAgeAtLastRejectionSeconds": round(ack_age, 3) if ack_age is not None else None,
        })
    status = "fail" if any(p["status"] == "fail" for p in peers) else "pass"
    if stop - start < grace_seconds + window_seconds or not routes:
        status = "incomplete"
    return {"status": status, "connectedPeers": sorted(set(routes.values())),
            "missionObservedSeconds": round(stop - start, 3),
            "graceSeconds": grace_seconds, "windowSeconds": window_seconds,
            "rejectionThreshold": rejection_threshold,
            "activeSecondsThreshold": active_seconds_threshold, "peers": peers,
            "criterion": "frequency of type-0 Drop diagnostics printing expected=0; not continuous blackout duration",
            "legacyFieldNote": "zeroExpected* fields retain printed Drop values, not actual receiver expectation",
            "receiveExpectationScope": "actualReceiveExpected counts partition zeroExpectedAfterGrace using adjacent matching Received headers within 50 ms; unknown is not zero"}


def analyze_trace(path: Path) -> dict:
    streams = defaultdict(list)
    bad_lines = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            bad_lines += 1
            continue
        if "disposition" not in record:
            continue
        streams[record["source"], record["target"]].append(record)
    summaries = []
    for (source, target), records in sorted(streams.items()):
        header = [r for r in records if isinstance(r.get("transport", {}).get("seqA"), int)]
        reliable = [r for r in header if int(r["transport"].get("flagsByte") or "0", 16) & 0x80]
        unreliable = [r for r in header if not int(r["transport"].get("flagsByte") or "0", 16) & 0x80]
        summaries.append({"source": source, "target": target,
                          "forwarded": sum(r["disposition"] == "forwarded" for r in records),
                          "dropped": sum(r["disposition"] != "forwarded" for r in records),
                          "reliableHeaders": len(reliable), "unreliableHeaders": len(unreliable),
                          "firstAck": header[0]["transport"]["seqB"] if header else None,
                          "lastAck": header[-1]["transport"]["seqB"] if header else None,
                          "lastReliableSequence": reliable[-1]["transport"]["seqA"] if reliable else None,
                          "lastUnreliableSequence": unreliable[-1]["transport"]["seqA"] if unreliable else None,
                          "maxHeaderAgeAtRelayMs": max((r["tsUnixMs"] - r["transport"]["headerTimestampUnixMs"]
                                                         for r in header if r["transport"].get("headerTimestampUnixMs")),
                                                        default=None)})
    return {"source": str(path), "malformedLines": bad_lines, "streams": summaries,
            "limitation": "forwarded counts and header ACKs do not prove native acceptance; only the first transport header per datagram is inspected; header age includes native queuing/retries and is not relay-hop latency"}


def analyze_run(run_dir: Path, *, clients: int = 4, **options) -> dict:
    results = []
    for index in range(clients):
        path = run_dir / f"client{index}" / "logs" / "BZLogger.txt"
        result = analyze_native_text(path.read_text(encoding="utf-8", errors="replace"), **options) if path.exists() else {
            "status": "incomplete", "reason": "missing native client log", "peers": []}
        completed_count = len(result.get("connectedPeers", []))
        if result["status"] == "pass" and completed_count != clients - 1:
            result["status"] = "incomplete"
            result["reason"] = f"expected {clients - 1} completed native peer connections; observed {completed_count}"
        results.append({"client": index, "source": str(path), **result})
    status = "fail" if any(r["status"] == "fail" for r in results) else (
        "incomplete" if any(r["status"] == "incomplete" for r in results) else "pass")
    output = {"status": status, "runDir": str(run_dir), "clients": results,
              "interpretation": "pass means no sustained native rejection-frequency pattern in the observed mission interval; printed expected=0 is not actual receiver expectation, and the gate does not measure packet loss or continuous blackout duration"}
    trace = run_dir / "relay-trace.jsonl"
    if trace.exists():
        output["relayTrace"] = analyze_trace(trace)
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--clients", type=int, choices=range(2, 5), default=4)
    parser.add_argument("--json-out", type=Path)
    parser.add_argument("--grace-seconds", type=float, default=5)
    parser.add_argument("--window-seconds", type=int, default=5)
    parser.add_argument("--rejection-threshold", type=int, default=20)
    parser.add_argument("--active-seconds-threshold", type=int, default=4)
    args = parser.parse_args()
    if args.grace_seconds < 0 or args.window_seconds < 1 or args.rejection_threshold < 1 or not 1 <= args.active_seconds_threshold <= args.window_seconds:
        parser.error("thresholds must be positive and active seconds must fit within the window")
    result = analyze_run(args.run_dir, clients=args.clients, grace_seconds=args.grace_seconds,
                         window_seconds=args.window_seconds, rejection_threshold=args.rejection_threshold,
                         active_seconds_threshold=args.active_seconds_threshold)
    if args.json_out:
        args.json_out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(f"Native network health: {result['status'].upper()}")
    for client in result["clients"]:
        print(f"  client{client['client']}: {client['status']}")
        for peer in client["peers"]:
            ack_age = peer["ackSampleAgeAtLastRejectionSeconds"]
            age_text = f"{ack_age}s" if ack_age is not None else "unavailable"
            print(f"    {peer['peer']}: printed-zero rejection count={peer['zeroExpectedAfterGrace']} "
                  f"actual expected zero/nonzero/unknown={peer['actualReceiveExpectedZero']}/{peer['actualReceiveExpectedNonzero']}/{peer['actualReceiveExpectedUnknown']} "
                  f"worst window={peer['worstWindow']['count']} "
                  f"ACK={peer['ackFirst']}->{peer['ackLast']} sampled sequence/ACK lag={peer['lastZeroExpectedMinusAck']} "
                  f"ACK sample age={age_text}")
    print(result["interpretation"])
    return 0 if result["status"] == "pass" else (1 if result["status"] == "fail" else 2)


if __name__ == "__main__":
    raise SystemExit(main())
