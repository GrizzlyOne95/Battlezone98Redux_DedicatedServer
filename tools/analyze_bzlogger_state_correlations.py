from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from analyze_bzlogger_descriptors import PACKET_LINE_RE
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server import decode_bzr_udp_datagram, summarize_protocol_history


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract Battlezone 98 Redux descriptor/state correlations and descriptor followup summaries from BZLogger.txt files."
    )
    parser.add_argument("logs", nargs="+", help="One or more BZLogger.txt paths")
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print JSON output",
    )
    return parser.parse_args()


def extract_protocol_history(log_path: Path) -> list[dict[str, Any]]:
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
            family = decoder.get("family")
            if not isinstance(family, str) or not family.startswith("peer_"):
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


def summarize_logs(log_paths: list[Path]) -> dict[str, Any]:
    all_history: list[dict[str, Any]] = []
    per_log: list[dict[str, Any]] = []
    for log_path in log_paths:
        history = extract_protocol_history(log_path)
        all_history.extend(history)
        summary = summarize_protocol_history(history)
        per_log.append(
            {
                "log": str(log_path),
                "packetCount": len(history),
                "descriptorStateCorrelations": summary.get("descriptorStateCorrelations", {}),
                "descriptorFollowups": summary.get("descriptorFollowups", {}),
                "unresolvedControlComparisons": summary.get("unresolvedControlComparisons", {}),
            }
        )

    combined_summary = summarize_protocol_history(all_history)
    return {
        "logs": [str(path) for path in log_paths],
        "packetCount": len(all_history),
        "descriptorStateCorrelations": combined_summary.get("descriptorStateCorrelations", {}),
        "descriptorFollowups": combined_summary.get("descriptorFollowups", {}),
        "unresolvedControlComparisons": combined_summary.get("unresolvedControlComparisons", {}),
        "perLog": per_log,
    }


def main() -> int:
    args = parse_args()
    log_paths = [Path(path) for path in args.logs]
    summary = summarize_logs(log_paths)
    indent = 2 if args.pretty else None
    print(json.dumps(summary, indent=indent, sort_keys=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
