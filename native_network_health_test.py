#!/usr/bin/env python3
"""Meaningful health-gate fixtures: stale retries, sustained rejection, gaps."""

from datetime import datetime, timedelta
import json
from pathlib import Path
import tempfile
import unittest

from native_network_health import analyze_native_text, analyze_run, analyze_trace


def line(second, content):
    timestamp = datetime(2026, 10, 7, 12) + timedelta(seconds=second)
    return f"{timestamp:%Y-%m-%d %H:%M:%S.%f} {content}"


def connected_log():
    return [line(0, 'BZRNet P2P Completed RELAY Connect For Client PEER_B, using address 127.0.0.1:40001'),
            line(1, '[CRFLOW] {"k":"attach"} #END')]


def rejected(second, sequence, expected):
    return line(second, f"BZRNet P2P Dropping Packet Type 0 For Client PEER_B (Packet #{sequence} received, #{expected} expected)")


class HealthTests(unittest.TestCase):
    def test_loading_duplicates_are_ignored(self):
        logs = connected_log() + [rejected(2, 8, 0)] * 200 + [line(30, "still running")]
        result = analyze_native_text("\n".join(logs))
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["peers"][0]["loadingGraceRejections"], 200)

    def test_reliable_stale_retransmissions_are_not_failure(self):
        logs = connected_log() + [rejected(t, 247, 250) for t in range(7, 30) for _ in range(30)]
        result = analyze_native_text("\n".join(logs))
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["peers"][0]["staleAfterGrace"], 690)

    def test_sustained_zero_expected_fails_with_advancing_acks(self):
        logs = connected_log()
        for t in range(7, 17):
            logs.append(line(t, f'BZRNet P2P TRY Sent Packet (42,{t * 10}) to 127.0.0.1:40001: c000'))
            logs.extend(rejected(t + n * .01, t * 10 + 50, 0) for n in range(10))
        result = analyze_native_text("\n".join(logs))
        peer = result["peers"][0]
        self.assertEqual(result["status"], "fail")
        self.assertEqual(peer["ackFirst"], 70)
        self.assertEqual(peer["ackLast"], 160)
        self.assertEqual(peer["lastZeroExpectedMinusAck"], 50)

    def test_isolated_burst_is_not_sustained(self):
        logs = connected_log() + [rejected(10, 3, 0)] * 150 + [line(30, "still running")]
        self.assertEqual(analyze_native_text("\n".join(logs))["status"], "pass")

    def test_close_excludes_teardown_traffic(self):
        logs = connected_log() + [line(15, "Closing application due to WM_CLOSE message")]
        logs += [rejected(t, 3, 0) for t in range(16, 25) for _ in range(30)]
        self.assertEqual(analyze_native_text("\n".join(logs))["status"], "pass")

    def test_missing_mission_marker_is_incomplete(self):
        self.assertEqual(analyze_native_text(line(10, "lobby only"))["status"], "incomplete")

    def test_fourth_client_failure_cannot_hide(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for i in range(4):
                path = root / f"client{i}" / "logs" / "BZLogger.txt"
                path.parent.mkdir(parents=True)
                logs = connected_log() + [line(2, 'BZRNet P2P Completed RELAY Connect For Client PEER_C, using address 127.0.0.1:40002'),
                                         line(3, 'BZRNet P2P Completed RELAY Connect For Client PEER_D, using address 127.0.0.1:40003'),
                                         line(30, "still running")]
                if i == 3:
                    logs = connected_log() + [rejected(t, 3, 0) for t in range(7, 20) for _ in range(30)]
                path.write_text("\n".join(logs), encoding="utf-8")
            self.assertEqual(analyze_run(root)["status"], "fail")
            (root / "client3" / "logs" / "BZLogger.txt").unlink()
            self.assertEqual(analyze_run(root)["status"], "incomplete")

    def test_missing_peer_connection_is_incomplete(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for i in range(4):
                path = root / f"client{i}" / "logs" / "BZLogger.txt"
                path.parent.mkdir(parents=True)
                path.write_text("\n".join(connected_log() + [line(30, "still running")]), encoding="utf-8")
            self.assertEqual(analyze_run(root)["status"], "incomplete")

    def test_drop_only_peer_cannot_replace_completed_connection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for i in range(4):
                path = root / f"client{i}" / "logs" / "BZLogger.txt"
                path.parent.mkdir(parents=True)
                logs = connected_log() + [
                    line(2, 'BZRNet P2P Completed RELAY Connect For Client PEER_C, using address 127.0.0.1:40002'),
                    line(10, 'BZRNet P2P Dropping Packet Type 0 For Client PEER_D (Packet #3 received, #0 expected)'),
                    line(30, "still running")]
                path.write_text("\n".join(logs), encoding="utf-8")
            self.assertEqual(analyze_run(root)["status"], "incomplete")

    def test_trace_counters_do_not_override_native_result(self):
        with tempfile.TemporaryDirectory() as directory:
            trace = Path(directory) / "trace.jsonl"
            records = [{"source": "127.0.0.1:40001", "target": "127.0.0.1:40002", "disposition": "forwarded",
                        "tsUnixMs": 1000 + sequence, "transport": {"seqA": sequence, "seqB": sequence - 1,
                        "flagsByte": "0xc0", "headerTimestampUnixMs": 1000 + sequence}} for sequence in range(10, 20)]
            trace.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
            result = analyze_trace(trace)
            self.assertEqual(result["streams"][0]["forwarded"], 10)
            self.assertEqual(result["streams"][0]["lastAck"], 18)
            self.assertNotIn("status", result)


if __name__ == "__main__":
    unittest.main()
