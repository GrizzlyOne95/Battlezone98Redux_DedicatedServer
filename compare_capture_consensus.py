from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare two Battlezone capture consensus JSON artifacts."
    )
    parser.add_argument("baseline", help="Baseline consensus JSON path")
    parser.add_argument("candidate", help="Candidate consensus JSON path")
    parser.add_argument(
        "--markdown",
        action="store_true",
        help="Render a human-readable Markdown report instead of JSON",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print JSON output",
    )
    parser.add_argument(
        "--write",
        dest="write_path",
        help="Optional output path to write the rendered report",
    )
    return parser.parse_args()


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _normalize_scalar_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    normalized: list[str] = []
    for item in value:
        if isinstance(item, (str, int, float, bool)) or item is None:
            normalized.append(str(item))
    return sorted(normalized)


def _normalize_nested_string_lists(value: Any) -> list[list[str]]:
    if not isinstance(value, list):
        return []
    normalized: list[list[str]] = []
    for item in value:
        if isinstance(item, list):
            normalized.append(sorted(str(part) for part in item))
    return sorted(normalized)


def _normalize_subtype_report(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "subtypeWord": str(report.get("subtypeWord", "")),
        "objectCounts": _normalize_scalar_list(report.get("objectCounts", [])),
        "objectNameSets": _normalize_nested_string_lists(report.get("objectNameSets", [])),
        "mutableObjectSets": _normalize_nested_string_lists(report.get("mutableObjectSets", [])),
        "singleSampleObjectSets": _normalize_nested_string_lists(report.get("singleSampleObjectSets", [])),
        "assessment": str(report.get("assessment", "")),
    }


def _normalize_object_report(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "objectName": str(report.get("objectName", "")),
        "observationCounts": _normalize_scalar_list(report.get("observationCounts", [])),
        "uniqueSubtypeWords": _normalize_nested_string_lists(report.get("uniqueSubtypeWords", [])),
        "uniqueParamHalf1Sets": _normalize_nested_string_lists(report.get("uniqueParamHalf1Sets", [])),
        "uniqueControlHalf0Sequences": _normalize_nested_string_lists(report.get("uniqueControlHalf0Sequences", [])),
        "transitionWindowCounts": _normalize_scalar_list(report.get("transitionWindowCounts", [])),
        "assessment": str(report.get("assessment", "")),
    }


def _compare_report_maps(
    baseline_reports: list[dict[str, Any]],
    candidate_reports: list[dict[str, Any]],
    key_name: str,
    normalize_fn,
) -> dict[str, Any]:
    baseline_map = {
        str(report.get(key_name, "")): normalize_fn(report)
        for report in baseline_reports
        if isinstance(report, dict) and isinstance(report.get(key_name), str)
    }
    candidate_map = {
        str(report.get(key_name, "")): normalize_fn(report)
        for report in candidate_reports
        if isinstance(report, dict) and isinstance(report.get(key_name), str)
    }

    baseline_keys = set(baseline_map)
    candidate_keys = set(candidate_map)
    added = sorted(candidate_keys - baseline_keys)
    removed = sorted(baseline_keys - candidate_keys)
    changed: list[dict[str, Any]] = []

    for shared_key in sorted(baseline_keys & candidate_keys):
        baseline_value = baseline_map[shared_key]
        candidate_value = candidate_map[shared_key]
        if baseline_value == candidate_value:
            continue
        field_changes: dict[str, dict[str, Any]] = {}
        for field_name in sorted(set(baseline_value) | set(candidate_value)):
            if baseline_value.get(field_name) == candidate_value.get(field_name):
                continue
            field_changes[field_name] = {
                "baseline": baseline_value.get(field_name),
                "candidate": candidate_value.get(field_name),
            }
        changed.append(
            {
                key_name: shared_key,
                "fieldChanges": field_changes,
            }
        )

    return {
        "added": added,
        "removed": removed,
        "changed": changed,
        "unchangedCount": len((baseline_keys & candidate_keys)) - len(changed),
    }


def compare_consensus(baseline: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    subtype_diff = _compare_report_maps(
        baseline.get("subtypeConsensusReports", []),
        candidate.get("subtypeConsensusReports", []),
        "subtypeWord",
        _normalize_subtype_report,
    )
    object_diff = _compare_report_maps(
        baseline.get("objectConsensusReports", []),
        candidate.get("objectConsensusReports", []),
        "objectName",
        _normalize_object_report,
    )

    identical = (
        not subtype_diff["added"]
        and not subtype_diff["removed"]
        and not subtype_diff["changed"]
        and not object_diff["added"]
        and not object_diff["removed"]
        and not object_diff["changed"]
    )

    return {
        "baseline": baseline.get("pcaps", []),
        "candidate": candidate.get("pcaps", []),
        "baselineSubtypeWords": baseline.get("subtypeWords", []),
        "candidateSubtypeWords": candidate.get("subtypeWords", []),
        "identical": identical,
        "summary": (
            "baseline and candidate consensus artifacts match exactly"
            if identical
            else "baseline and candidate consensus artifacts differ"
        ),
        "subtypeConsensusDiff": subtype_diff,
        "objectConsensusDiff": object_diff,
    }


def _format_value(value: Any) -> str:
    if isinstance(value, list):
        if value and all(isinstance(item, list) for item in value):
            return "; ".join(", ".join(str(part) for part in item) for item in value)
        return ", ".join(str(item) for item in value)
    if isinstance(value, dict):
        return json.dumps(value, sort_keys=True)
    return str(value)


def render_markdown_report(result: dict[str, Any], baseline_path: Path, candidate_path: Path) -> str:
    lines: list[str] = []
    lines.append("# Capture Consensus Comparison")
    lines.append("")
    lines.append(f"- Baseline: `{baseline_path}`")
    lines.append(f"- Candidate: `{candidate_path}`")
    lines.append(f"- Summary: `{result.get('summary', '')}`")
    lines.append(f"- Identical: `{result.get('identical', False)}`")
    lines.append("")

    def append_diff_section(title: str, key_name: str, diff: dict[str, Any]) -> None:
        lines.append(f"## {title}")
        lines.append("")
        lines.append(f"- Added: `{len(diff.get('added', []))}`")
        if diff.get("added"):
            for item in diff["added"]:
                lines.append(f"  - `{item}`")
        lines.append(f"- Removed: `{len(diff.get('removed', []))}`")
        if diff.get("removed"):
            for item in diff["removed"]:
                lines.append(f"  - `{item}`")
        lines.append(f"- Changed: `{len(diff.get('changed', []))}`")
        lines.append(f"- Unchanged: `{diff.get('unchangedCount', 0)}`")
        if not diff.get("changed"):
            lines.append("- No field-level changes detected.")
        else:
            lines.append("")
            for changed_item in diff["changed"]:
                item_name = changed_item.get(key_name, "")
                lines.append(f"### `{item_name}`")
                lines.append("")
                field_changes = changed_item.get("fieldChanges", {})
                for field_name in sorted(field_changes):
                    field_change = field_changes[field_name]
                    lines.append(f"- `{field_name}`")
                    lines.append(f"  - baseline: `{_format_value(field_change.get('baseline'))}`")
                    lines.append(f"  - candidate: `{_format_value(field_change.get('candidate'))}`")
                lines.append("")
        lines.append("")

    append_diff_section("Subtype Diff", "subtypeWord", result.get("subtypeConsensusDiff", {}))
    append_diff_section("Object Diff", "objectName", result.get("objectConsensusDiff", {}))
    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    args = parse_args()
    baseline_path = Path(args.baseline)
    candidate_path = Path(args.candidate)
    result = compare_consensus(_load_json(baseline_path), _load_json(candidate_path))
    if args.markdown:
        rendered = render_markdown_report(result, baseline_path, candidate_path)
    else:
        indent = 2 if args.pretty else None
        rendered = json.dumps(result, indent=indent, sort_keys=False)
    if args.write_path:
        write_path = Path(args.write_path)
        write_path.write_text(rendered + ("" if rendered.endswith("\n") else "\n"), encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
