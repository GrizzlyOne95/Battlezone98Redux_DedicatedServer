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


def received(second, sequence, expected, *, endpoint="127.0.0.1:40001", flags=0x40,
             header_sequence=None, header_ack=12, logged_ack=12, kind=0):
    header = bytes([flags, kind]) + bytes(8)
    header += (sequence if header_sequence is None else header_sequence).to_bytes(4, "big")
    header += header_ack.to_bytes(4, "big")
    return line(second, f"BZRNet P2P Received Packet ({sequence},{logged_ack}) expecting ({expected},12) "
                f"from {endpoint} sent at 1000, received at 1001, diff 1: {header.hex()}")


class HealthTests(unittest.TestCase):
    def test_printed_zero_is_not_actual_receiver_zero(self):
        logs = connected_log() + [received(7, 353, 352), rejected(7, 353, 0),
                                 received(8, 1, 0), rejected(8, 1, 0),
                                 rejected(9, 2, 0), line(30, "still running")]
        peer = analyze_native_text("\n".join(logs))["peers"][0]
        self.assertEqual(peer["zeroExpectedAfterGrace"], 3)
        self.assertEqual(peer["actualReceiveExpectedNonzero"], 1)
        self.assertEqual(peer["actualReceiveExpectedZero"], 1)
        self.assertEqual(peer["actualReceiveExpectedUnknown"], 1)
        self.assertEqual(peer["unreliableRejectedAfterGrace"], 2)

    def test_ambiguous_receive_associations_stay_unknown(self):
        cases = {
            "different_peer": [received(7, 353, 352, endpoint="127.0.0.1:40002")],
            "old_receive": [received(6.9, 353, 352)],
            "different_sequence": [received(7, 354, 352)],
            "wrong_header_sequence": [received(7, 353, 352, header_sequence=354)],
            "wrong_header_ack": [received(7, 353, 352, header_ack=13)],
            "wrong_type": [received(7, 353, 352, kind=6)],
            "short_header": [received(7, 353, 352)[:-4]],
            "nonadjacent": [received(7, 353, 352), line(7, "intervening record")],
            "untimestamped_intervening": [received(7, 353, 352), "intervening record"],
            "later_receive": [received(7.1, 353, 352)],
        }
        for name, before in cases.items():
            with self.subTest(name=name):
                logs = connected_log() + [line(2, 'BZRNet P2P Completed RELAY Connect For Client PEER_C, using address 127.0.0.1:40002')]
                logs += before + [rejected(7, 353, 0), line(30, "still running")]
                peer = next(p for p in analyze_native_text("\n".join(logs))["peers"] if p["peer"] == "PEER_B")
                self.assertEqual(peer["actualReceiveExpectedUnknown"], 1)
                self.assertEqual(peer["actualReceiveExpectedNonzero"], 0)

    def test_repeated_short_bursts_preserve_frequency_gate(self):
        logs = connected_log()
        for second in range(7, 11):
            for n in range(6):
                t = second + n * .001
                logs += [received(t, 353, 352), rejected(t, 353, 0)]
        logs += [line(30, "still running")]
        result = analyze_native_text("\n".join(logs))
        self.assertEqual(result["status"], "fail")
        peer = result["peers"][0]
        self.assertEqual(peer["actualReceiveExpectedNonzero"], 24)
        self.assertEqual(peer["actualReceiveExpectedZero"], 0)
        self.assertIn("not continuous blackout", result["criterion"])

    def test_reliable_flag_is_not_counted_as_unreliable(self):
        logs = connected_log() + [received(7, 353, 352, flags=0xc0), rejected(7, 353, 0), line(30, "still running")]
        peer = analyze_native_text("\n".join(logs))["peers"][0]
        self.assertEqual(peer["unreliableRejectedAfterGrace"], 0)
        self.assertEqual(peer["actualReceiveExpectedNonzero"], 1)

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
