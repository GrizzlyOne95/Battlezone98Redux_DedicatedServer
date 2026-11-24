from __future__ import annotations

import argparse
import json
from pathlib import Path

import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from analyze_pcapng_udp import build_consensus_only_summary, build_targeted_summary
from compare_capture_consensus import compare_consensus, render_markdown_report


DEFAULT_SCRAP_SUBTYPES = ["0x0c00", "0x0d00", "0x0e00", "0x0f00"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate Battlezone capture consensus artifacts and optional baseline diffs."
    )
    parser.add_argument("pcaps", nargs="+", help="One or more .pcapng paths for the candidate run")
    parser.add_argument(
        "--output-prefix",
        required=True,
        help="Prefix path for generated artifacts (without extension)",
    )
    parser.add_argument(
        "--baseline-consensus",
        help="Optional existing consensus JSON to compare against",
    )
    parser.add_argument(
        "--subtype",
        dest="subtype_words",
        action="append",
        help="Subtype word to include. Defaults to the observed scrap ladder if omitted.",
    )
    parser.add_argument(
        "--object",
        dest="object_names",
        action="append",
        help="Object name to include in the consensus artifact",
    )
    parser.add_argument(
        "--window",
        type=int,
        default=4,
        help="Neighbor packet count for object transition windows (default: 4)",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print generated JSON artifacts",
    )
    return parser.parse_args()


def _write_text(path: Path, text: str) -> None:
    path.write_text(text + ("" if text.endswith("\n") else "\n"), encoding="utf-8")


def main() -> int:
    args = parse_args()
    pcap_paths = [Path(path) for path in args.pcaps]
    output_prefix = Path(args.output_prefix)
    subtype_words = list(dict.fromkeys(args.subtype_words or DEFAULT_SCRAP_SUBTYPES))
    object_names = list(dict.fromkeys(args.object_names or []))

    candidate_summary = build_targeted_summary(
        pcap_paths,
        object_names=object_names,
        subtype_words=subtype_words,
        window=args.window,
    )
    candidate_consensus = build_consensus_only_summary(candidate_summary)

    indent = 2 if args.pretty else None
    candidate_consensus_path = output_prefix.with_name(output_prefix.name + "_consensus.json")
    _write_text(candidate_consensus_path, json.dumps(candidate_consensus, indent=indent, sort_keys=False))

    result: dict[str, object] = {
        "candidateConsensusPath": str(candidate_consensus_path),
        "baselineConsensusPath": args.baseline_consensus,
        "generatedCompareJsonPath": None,
        "generatedCompareMarkdownPath": None,
        "subtypeWords": subtype_words,
        "objectNames": object_names,
        "summary": "generated candidate consensus artifact",
    }

    if args.baseline_consensus:
        baseline_path = Path(args.baseline_consensus)
        baseline_consensus = json.loads(baseline_path.read_text(encoding="utf-8"))
        compare_result = compare_consensus(baseline_consensus, candidate_consensus)
        compare_json_path = output_prefix.with_name(output_prefix.name + "_compare.json")
        compare_markdown_path = output_prefix.with_name(output_prefix.name + "_compare.md")
        _write_text(compare_json_path, json.dumps(compare_result, indent=indent, sort_keys=False))
        _write_text(compare_markdown_path, render_markdown_report(compare_result, baseline_path, candidate_consensus_path))
        result["generatedCompareJsonPath"] = str(compare_json_path)
        result["generatedCompareMarkdownPath"] = str(compare_markdown_path)
        result["summary"] = compare_result.get("summary", "generated candidate consensus artifact and comparison outputs")

    print(json.dumps(result, indent=2 if args.pretty else None, sort_keys=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
