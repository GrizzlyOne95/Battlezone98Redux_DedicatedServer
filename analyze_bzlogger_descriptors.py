from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from server import decode_bzr_udp_datagram, summarize_protocol_history


PACKET_LINE_RE = re.compile(
    r"^(?P<time>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+)\s+"
    r"(?P<kind>BZRNet P2P .*?Packet.*?):\s+"
    r"(?P<hex>[0-9a-fA-F]+)\s*$"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract and compare Battlezone 98 Redux 7a75 object descriptors from BZLogger.txt files."
    )
    parser.add_argument("logs", nargs="+", help="One or more BZLogger.txt paths")
    parser.add_argument(
        "--family",
        help="Optional descriptorPayloadFamilyHint filter, e.g. b_side_mobile_unit_payload",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print JSON output",
    )
    return parser.parse_args()


def extract_descriptor_history(log_path: Path) -> list[dict[str, Any]]:
    history: list[dict[str, Any]] = []
    with log_path.open("r", encoding="utf-8", errors="replace") as handle:
        for line_number, line in enumerate(handle, start=1):
            match = PACKET_LINE_RE.match(line.rstrip("\n"))
            if not match:
                continue
            payload_hex = match.group("hex")
            if len(payload_hex) < 20:
                continue
            try:
                payload = bytes.fromhex(payload_hex)
            except ValueError:
                continue
            decoder = decode_bzr_udp_datagram(payload)
            if decoder.get("family") != "peer_object_descriptor":
                continue
            history.append(
                {
                    "channel": "udp",
                    "peer": f"{log_path.stem}:{match.group('kind')}",
                    "time": match.group("time"),
                    "decoder": decoder,
                    "sourceLog": str(log_path),
                    "lineNumber": line_number,
                }
            )
    return history


def summarize_logs(log_paths: list[Path], family_filter: str | None) -> dict[str, Any]:
    all_history: list[dict[str, Any]] = []
    log_summaries: list[dict[str, Any]] = []
    for log_path in log_paths:
        history = extract_descriptor_history(log_path)
        if family_filter:
            history = [
                entry
                for entry in history
                if entry["decoder"].get("descriptorPayloadFamilyHint") == family_filter
            ]
        all_history.extend(history)
        summary = summarize_protocol_history(history)
        log_summaries.append(
            {
                "log": str(log_path),
                "descriptorCount": len(history),
                "payloadComparisons": summary.get("descriptorPayloadComparisons", []),
            }
        )

    combined_summary = summarize_protocol_history(all_history)
    return {
        "logs": [str(path) for path in log_paths],
        "familyFilter": family_filter,
        "descriptorCount": len(all_history),
        "combinedPayloadComparisons": combined_summary.get("descriptorPayloadComparisons", []),
        "perLog": log_summaries,
    }


def main() -> int:
    args = parse_args()
    log_paths = [Path(path) for path in args.logs]
    summary = summarize_logs(log_paths, args.family)
    indent = 2 if args.pretty else None
    print(json.dumps(summary, indent=indent, sort_keys=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
