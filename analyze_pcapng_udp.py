from __future__ import annotations

import argparse
import json
import socket
import struct
from pathlib import Path
from typing import Any

from server import decode_bzr_udp_datagram, summarize_protocol_history


PCAPNG_SECTION_HEADER = 0x0A0D0D0A
PCAPNG_INTERFACE_DESCRIPTION = 0x00000001
PCAPNG_SIMPLE_PACKET = 0x00000003
PCAPNG_ENHANCED_PACKET = 0x00000006
PCAPNG_BYTE_ORDER_MAGIC = 0x1A2B3C4D
PCAPNG_OPT_END = 0
PCAPNG_OPT_IF_TSRESOL = 9
PCAPNG_OPT_IF_TSOFFSET = 14

DLT_EN10MB = 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract Battlezone 98 Redux UDP protocol summaries from one or more pcapng files."
    )
    parser.add_argument("pcaps", nargs="+", help="One or more .pcapng paths")
    parser.add_argument(
        "--object",
        dest="object_name",
        action="append",
        help="Emit compact transition windows for a specific decoded object name",
    )
    parser.add_argument(
        "--subtype",
        dest="subtype_words",
        action="append",
        help="Emit subtype-wide summaries for a specific decoded descriptor subtype word (for example 0x0e00)",
    )
    parser.add_argument(
        "--window",
        type=int,
        default=4,
        help="Neighbor packet count to include on each side of an object transition window (default: 4)",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print JSON output",
    )
    parser.add_argument(
        "--consensus-only",
        action="store_true",
        help="When using --object or --subtype, emit only cross-pcap consensus sections",
    )
    parser.add_argument(
        "--write",
        dest="write_path",
        help="Optional output path to write JSON instead of only printing it",
    )
    return parser.parse_args()


def _align4(value: int) -> int:
    return (value + 3) & ~3


def _unpack_u16(data: bytes, offset: int, endian: str) -> int:
    return struct.unpack_from(f"{endian}H", data, offset)[0]


def _unpack_u32(data: bytes, offset: int, endian: str) -> int:
    return struct.unpack_from(f"{endian}I", data, offset)[0]


def _unpack_i64(data: bytes, offset: int, endian: str) -> int:
    return struct.unpack_from(f"{endian}q", data, offset)[0]


def _decode_idb_options(options: bytes, endian: str) -> dict[str, Any]:
    parsed: dict[str, Any] = {}
    offset = 0
    while offset + 4 <= len(options):
        code = _unpack_u16(options, offset, endian)
        length = _unpack_u16(options, offset + 2, endian)
        offset += 4
        value = options[offset : offset + length]
        offset += _align4(length)
        if code == PCAPNG_OPT_END:
            break
        if code == PCAPNG_OPT_IF_TSRESOL and value:
            raw = value[0]
            if raw & 0x80:
                parsed["timestamp_scale"] = 2 ** (raw & 0x7F)
            else:
                parsed["timestamp_scale"] = 10 ** raw
        elif code == PCAPNG_OPT_IF_TSOFFSET and len(value) >= 8:
            parsed["timestamp_offset"] = _unpack_i64(value + b"\x00" * max(0, 8 - len(value)), 0, endian)
    return parsed


def _format_timestamp(timestamp_seconds: float) -> str:
    integer_part = int(timestamp_seconds)
    fractional = timestamp_seconds - integer_part
    micros = int(round(fractional * 1_000_000))
    if micros >= 1_000_000:
        integer_part += 1
        micros -= 1_000_000
    from datetime import datetime, timezone

    dt = datetime.fromtimestamp(integer_part, tz=timezone.utc).replace(microsecond=micros)
    return dt.strftime("%Y-%m-%d %H:%M:%S.%f")


def _decode_udp_from_ethernet(packet_data: bytes) -> dict[str, Any] | None:
    if len(packet_data) < 14:
        return None
    ethertype = struct.unpack_from("!H", packet_data, 12)[0]
    if ethertype != 0x0800:
        return None
    ip_offset = 14
    if len(packet_data) < ip_offset + 20:
        return None
    version_ihl = packet_data[ip_offset]
    version = version_ihl >> 4
    ihl = (version_ihl & 0x0F) * 4
    if version != 4 or ihl < 20 or len(packet_data) < ip_offset + ihl:
        return None
    protocol = packet_data[ip_offset + 9]
    if protocol != 17:
        return None
    src_ip = socket.inet_ntoa(packet_data[ip_offset + 12 : ip_offset + 16])
    dst_ip = socket.inet_ntoa(packet_data[ip_offset + 16 : ip_offset + 20])
    udp_offset = ip_offset + ihl
    if len(packet_data) < udp_offset + 8:
        return None
    src_port, dst_port, udp_length = struct.unpack_from("!HHH", packet_data, udp_offset)
    payload_offset = udp_offset + 8
    payload = packet_data[payload_offset : payload_offset + max(0, udp_length - 8)]
    return {
        "src_ip": src_ip,
        "dst_ip": dst_ip,
        "src_port": src_port,
        "dst_port": dst_port,
        "payload": payload,
    }


def _parse_hex_word(value: Any) -> int | None:
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    if not text:
        return None
    if text.startswith("0x"):
        text = text[2:]
    try:
        return int(text, 16)
    except ValueError:
        return None


def _control_half0_from_decoder(decoder: dict[str, Any]) -> str | None:
    descriptor_sketch = decoder.get("descriptorSketch")
    if not isinstance(descriptor_sketch, dict):
        return None
    control_word = descriptor_sketch.get("controlWord")
    control_value = _parse_hex_word(control_word)
    if control_value is None:
        return None
    return f"0x{(control_value >> 16) & 0xFFFF:04x}"


def _label_history_entry(entry: dict[str, Any]) -> str:
    decoder = entry.get("decoder") or {}
    family = decoder.get("family")
    object_name = decoder.get("primaryObjectName")
    control_event = decoder.get("controlEvent")
    if isinstance(object_name, str) and object_name:
        return object_name
    if isinstance(control_event, str) and control_event:
        return control_event
    if isinstance(family, str) and family:
        return family
    return "unknown"


def _state_block_snapshot(decoder: dict[str, Any]) -> dict[str, Any] | None:
    if decoder.get("family") != "peer_state_block":
        return None
    return {
        "stateHeader": decoder.get("stateHeader"),
        "unitCodes": decoder.get("unitCodes"),
        "stateObjectCount": decoder.get("stateObjectCount"),
    }


def extract_protocol_history_from_pcapng(pcap_path: Path) -> list[dict[str, Any]]:
    data = pcap_path.read_bytes()
    offset = 0
    endian = "<"
    interfaces: list[dict[str, Any]] = []
    history: list[dict[str, Any]] = []

    while offset + 12 <= len(data):
        block_type_le = struct.unpack_from("<I", data, offset)[0]
        block_total_length_le = struct.unpack_from("<I", data, offset + 4)[0]
        if block_total_length_le < 12 or offset + block_total_length_le > len(data):
            break
        block_data = data[offset : offset + block_total_length_le]

        if block_type_le == PCAPNG_SECTION_HEADER:
            byte_order_magic = struct.unpack_from("<I", block_data, 8)[0]
            if byte_order_magic == PCAPNG_BYTE_ORDER_MAGIC:
                endian = "<"
            elif byte_order_magic == 0x4D3C2B1A:
                endian = ">"
            else:
                offset += block_total_length_le
                continue
            interfaces.clear()
        else:
            block_type = _unpack_u32(block_data, 0, endian)
            block_total_length = _unpack_u32(block_data, 4, endian)
            if block_total_length != block_total_length_le:
                offset += block_total_length_le
                continue
            if block_type == PCAPNG_INTERFACE_DESCRIPTION:
                if len(block_data) < 16:
                    offset += block_total_length_le
                    continue
                linktype = _unpack_u16(block_data, 8, endian)
                snaplen = _unpack_u32(block_data, 12, endian)
                options = block_data[16:-4]
                interface = {
                    "linktype": linktype,
                    "snaplen": snaplen,
                    "timestamp_scale": 1_000_000,
                    "timestamp_offset": 0,
                }
                interface.update(_decode_idb_options(options, endian))
                interfaces.append(interface)
            elif block_type == PCAPNG_ENHANCED_PACKET:
                if len(block_data) < 32:
                    offset += block_total_length_le
                    continue
                interface_id = _unpack_u32(block_data, 8, endian)
                if interface_id >= len(interfaces):
                    offset += block_total_length_le
                    continue
                captured_len = _unpack_u32(block_data, 20, endian)
                packet_data_offset = 28
                packet_data_end = packet_data_offset + _align4(captured_len)
                if packet_data_end > len(block_data) - 4:
                    offset += block_total_length_le
                    continue
                packet_data = block_data[packet_data_offset : packet_data_offset + captured_len]
                interface = interfaces[interface_id]
                if interface["linktype"] != DLT_EN10MB:
                    offset += block_total_length_le
                    continue
                decoded_udp = _decode_udp_from_ethernet(packet_data)
                if not decoded_udp:
                    offset += block_total_length_le
                    continue
                decoder = decode_bzr_udp_datagram(decoded_udp["payload"])
                family = decoder.get("family")
                if not isinstance(family, str) or not family.startswith("peer_"):
                    offset += block_total_length_le
                    continue
                timestamp_units = (_unpack_u32(block_data, 12, endian) << 32) | _unpack_u32(block_data, 16, endian)
                timestamp_seconds = (
                    timestamp_units / float(interface.get("timestamp_scale", 1_000_000))
                    + float(interface.get("timestamp_offset", 0))
                )
                history.append(
                    {
                        "channel": "udp",
                        "peer": f"{decoded_udp['src_ip']}:{decoded_udp['src_port']}->{decoded_udp['dst_ip']}:{decoded_udp['dst_port']}",
                        "time": _format_timestamp(timestamp_seconds),
                        "decoder": decoder,
                        "sourceLog": str(pcap_path),
                    }
                )
        offset += block_total_length_le

    return history


def extract_object_transition_windows(
    history: list[dict[str, Any]],
    object_name: str,
    window: int,
) -> list[dict[str, Any]]:
    transitions: list[dict[str, Any]] = []
    observations: list[dict[str, Any]] = []
    clamped_window = max(0, window)

    for index, entry in enumerate(history):
        decoder = entry.get("decoder") or {}
        if decoder.get("primaryObjectName") != object_name:
            continue
        control_half0 = _control_half0_from_decoder(decoder)
        if not isinstance(control_half0, str):
            continue
        observations.append(
            {
                "index": index,
                "entry": entry,
                "controlHalf0": control_half0,
                "subtypeWord": (decoder.get("descriptorSketch") or {}).get("subtypeWord"),
                "paramWord": (decoder.get("descriptorSketch") or {}).get("paramWord"),
            }
        )

    for prev_observation, current_observation in zip(observations, observations[1:]):
        if prev_observation["controlHalf0"] == current_observation["controlHalf0"]:
            continue
        current_index = current_observation["index"]
        prev_index = prev_observation["index"]
        current_entry = current_observation["entry"]
        nearest_preceding_state_block = None
        for state_index in range(current_index - 1, -1, -1):
            state_entry = history[state_index]
            state_decoder = state_entry.get("decoder") or {}
            state_snapshot = _state_block_snapshot(state_decoder)
            if state_snapshot is None:
                continue
            nearest_preceding_state_block = {
                "time": state_entry.get("time"),
                "peer": state_entry.get("peer"),
                **state_snapshot,
            }
            break

        window_entries: list[dict[str, Any]] = []
        start = max(0, current_index - clamped_window)
        stop = min(len(history), current_index + clamped_window + 1)
        for window_index in range(start, stop):
            window_entry = history[window_index]
            window_decoder = window_entry.get("decoder") or {}
            descriptor_sketch = window_decoder.get("descriptorSketch") or {}
            window_entries.append(
                {
                    "relativeIndex": window_index - current_index,
                    "time": window_entry.get("time"),
                    "peer": window_entry.get("peer"),
                    "family": window_decoder.get("family"),
                    "label": _label_history_entry(window_entry),
                    "controlEvent": window_decoder.get("controlEvent"),
                    "primaryObjectName": window_decoder.get("primaryObjectName"),
                    "objectKind": window_decoder.get("objectKind"),
                    "subtypeWord": descriptor_sketch.get("subtypeWord"),
                    "controlHalf0": _control_half0_from_decoder(window_decoder),
                    "paramHalf1": (
                        f"0x{_parse_hex_word(descriptor_sketch.get('paramWord')) & 0xFFFF:04x}"
                        if _parse_hex_word(descriptor_sketch.get("paramWord")) is not None
                        else None
                    ),
                    "stateBlock": _state_block_snapshot(window_decoder),
                }
            )

        transitions.append(
            {
                "objectName": object_name,
                "fromControlHalf0": prev_observation["controlHalf0"],
                "toControlHalf0": current_observation["controlHalf0"],
                "previousObservationTime": prev_observation["entry"].get("time"),
                "changedObservationTime": current_entry.get("time"),
                "previousObservationPeer": prev_observation["entry"].get("peer"),
                "changedObservationPeer": current_entry.get("peer"),
                "previousObservationSubtypeWord": prev_observation.get("subtypeWord"),
                "changedObservationSubtypeWord": current_observation.get("subtypeWord"),
                "changedObservationParamWord": current_observation.get("paramWord"),
                "nearestPrecedingStateBlock": nearest_preceding_state_block,
                "window": window_entries,
                "windowAssessment": (
                    f"{object_name} changes from {prev_observation['controlHalf0']} to "
                    f"{current_observation['controlHalf0']} with a {len(window_entries)}-packet local window"
                ),
                "postTransitionTailPacketCount": len(history) - current_index - 1,
            }
        )

    return transitions


def extract_object_observation_summary(
    history: list[dict[str, Any]],
    object_name: str,
) -> dict[str, Any]:
    observations: list[dict[str, Any]] = []
    control_half0_sequence: list[str] = []
    last_control_half0: str | None = None

    for entry in history:
        decoder = entry.get("decoder") or {}
        if decoder.get("primaryObjectName") != object_name:
            continue
        descriptor_sketch = decoder.get("descriptorSketch") or {}
        control_half0 = _control_half0_from_decoder(decoder)
        param_word = descriptor_sketch.get("paramWord")
        subtype_word = descriptor_sketch.get("subtypeWord")
        param_half1 = None
        param_value = _parse_hex_word(param_word)
        if param_value is not None:
            param_half1 = f"0x{param_value & 0xFFFF:04x}"
        observation = {
            "time": entry.get("time"),
            "peer": entry.get("peer"),
            "subtypeWord": subtype_word,
            "controlHalf0": control_half0,
            "paramWord": param_word,
            "paramHalf1": param_half1,
        }
        observations.append(observation)
        if isinstance(control_half0, str) and control_half0 and control_half0 != last_control_half0:
            control_half0_sequence.append(control_half0)
            last_control_half0 = control_half0

    unique_subtype_words = sorted(
        {
            value
            for value in (observation.get("subtypeWord") for observation in observations)
            if isinstance(value, str) and value
        }
    )
    unique_control_half0 = sorted(
        {
            value
            for value in (observation.get("controlHalf0") for observation in observations)
            if isinstance(value, str) and value
        }
    )
    unique_param_half1 = sorted(
        {
            value
            for value in (observation.get("paramHalf1") for observation in observations)
            if isinstance(value, str) and value
        }
    )
    return {
        "objectName": object_name,
        "observationCount": len(observations),
        "uniqueSubtypeWords": unique_subtype_words,
        "uniqueControlHalf0": unique_control_half0,
        "uniqueParamHalf1": unique_param_half1,
        "uniqueControlHalf0Sequence": control_half0_sequence,
        "firstObservation": observations[0] if observations else None,
        "lastObservation": observations[-1] if observations else None,
        "observations": observations,
    }


def extract_subtype_observation_summary(
    history: list[dict[str, Any]],
    subtype_word: str,
) -> dict[str, Any]:
    object_summaries: list[dict[str, Any]] = []
    object_names: set[str] = set()
    unique_control_half0: set[str] = set()
    unique_param_half1: set[str] = set()

    for entry in history:
        decoder = entry.get("decoder") or {}
        descriptor_sketch = decoder.get("descriptorSketch") or {}
        if descriptor_sketch.get("subtypeWord") != subtype_word:
            continue
        object_name = decoder.get("primaryObjectName")
        if isinstance(object_name, str) and object_name:
            object_names.add(object_name)

    for object_name in sorted(object_names):
        summary = extract_object_observation_summary(history, object_name)
        object_summaries.append(summary)
        for control_half0 in summary.get("uniqueControlHalf0", []):
            if isinstance(control_half0, str) and control_half0:
                unique_control_half0.add(control_half0)
        for param_half1 in summary.get("uniqueParamHalf1", []):
            if isinstance(param_half1, str) and param_half1:
                unique_param_half1.add(param_half1)

    mutable_objects = sorted(
        summary["objectName"]
        for summary in object_summaries
        if len(summary.get("uniqueControlHalf0", [])) > 1
    )
    single_sample_objects = sorted(
        summary["objectName"]
        for summary in object_summaries
        if int(summary.get("observationCount", 0)) == 1
    )

    if mutable_objects:
        assessment = (
            f"subtype {subtype_word} has observed multi-state objects: {', '.join(mutable_objects)}"
        )
        if single_sample_objects:
            assessment += f"; single-sample objects remain: {', '.join(single_sample_objects)}"
    elif object_summaries:
        assessment = f"subtype {subtype_word} shows no observed multi-state objects in this capture"
        if single_sample_objects:
            assessment += f"; single-sample objects remain: {', '.join(single_sample_objects)}"
    else:
        assessment = f"subtype {subtype_word} was not observed in this capture"

    return {
        "subtypeWord": subtype_word,
        "objectCount": len(object_summaries),
        "objectNames": [summary["objectName"] for summary in object_summaries],
        "uniqueControlHalf0": sorted(unique_control_half0),
        "uniqueParamHalf1": sorted(unique_param_half1),
        "mutableObjects": mutable_objects,
        "singleSampleObjects": single_sample_objects,
        "objectSummaries": object_summaries,
        "assessment": assessment,
    }


def build_object_consensus_reports(object_reports: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for report in object_reports:
        object_name = report.get("objectName")
        observation_summary = report.get("observationSummary")
        if not isinstance(object_name, str) or not isinstance(observation_summary, dict):
            continue
        grouped.setdefault(object_name, []).append(report)

    consensus_reports: list[dict[str, Any]] = []
    for object_name in sorted(grouped):
        reports = grouped[object_name]
        observation_summaries = [report["observationSummary"] for report in reports]
        unique_sequences = {
            tuple(summary.get("uniqueControlHalf0Sequence", []))
            for summary in observation_summaries
        }
        unique_counts = {
            int(summary.get("observationCount", 0))
            for summary in observation_summaries
        }
        unique_subtypes = {
            tuple(summary.get("uniqueSubtypeWords", []))
            for summary in observation_summaries
        }
        unique_param_half1 = {
            tuple(summary.get("uniqueParamHalf1", []))
            for summary in observation_summaries
        }
        transition_counts = {
            len(report.get("objectTransitionWindows", []))
            for report in reports
        }
        pcaps = [str(report.get("pcap", "")) for report in reports]
        assessments: list[str] = []
        if len(unique_sequences) == 1:
            sequence = next(iter(unique_sequences), ())
            if sequence:
                assessments.append(
                    f"all pcaps agree on controlHalf0 sequence {' -> '.join(sequence)}"
                )
            else:
                assessments.append("all pcaps agree that no controlHalf0 sequence was decoded")
        else:
            assessments.append("pcaps disagree on the observed controlHalf0 sequence")
        if len(unique_counts) == 1:
            assessments.append(f"all pcaps saw {next(iter(unique_counts), 0)} observations")
        else:
            assessments.append("pcaps disagree on observation count")
        if len(transition_counts) == 1:
            assessments.append(
                f"all pcaps saw {next(iter(transition_counts), 0)} transition window(s)"
            )
        else:
            assessments.append("pcaps disagree on transition-window count")
        consensus_reports.append(
            {
                "objectName": object_name,
                "pcapCount": len(reports),
                "pcaps": pcaps,
                "observationCounts": sorted(unique_counts),
                "uniqueSubtypeWords": [list(item) for item in sorted(unique_subtypes)],
                "uniqueParamHalf1Sets": [list(item) for item in sorted(unique_param_half1)],
                "uniqueControlHalf0Sequences": [list(item) for item in sorted(unique_sequences)],
                "transitionWindowCounts": sorted(transition_counts),
                "assessment": "; ".join(assessments),
            }
        )
    return consensus_reports


def build_subtype_consensus_reports(subtype_reports: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for report in subtype_reports:
        subtype_word = report.get("subtypeWord")
        if not isinstance(subtype_word, str):
            continue
        grouped.setdefault(subtype_word, []).append(report)

    consensus_reports: list[dict[str, Any]] = []
    for subtype_word in sorted(grouped):
        reports = grouped[subtype_word]
        mutable_objects = {tuple(report.get("mutableObjects", [])) for report in reports}
        single_sample_objects = {tuple(report.get("singleSampleObjects", [])) for report in reports}
        object_names = {tuple(report.get("objectNames", [])) for report in reports}
        object_counts = {int(report.get("objectCount", 0)) for report in reports}
        pcaps = [str(report.get("pcap", "")) for report in reports]
        assessments: list[str] = []
        if len(mutable_objects) == 1:
            mutable = next(iter(mutable_objects), ())
            if mutable:
                assessments.append(f"all pcaps agree mutable objects are {', '.join(mutable)}")
            else:
                assessments.append("all pcaps agree no mutable objects were observed")
        else:
            assessments.append("pcaps disagree on mutable objects")
        if len(single_sample_objects) == 1:
            single_sample = next(iter(single_sample_objects), ())
            if single_sample:
                assessments.append(f"all pcaps agree single-sample objects are {', '.join(single_sample)}")
        else:
            assessments.append("pcaps disagree on single-sample objects")
        if len(object_names) == 1 and len(object_counts) == 1:
            names = next(iter(object_names), ())
            assessments.append(
                f"all pcaps agree subtype roster is {', '.join(names)}"
            )
        else:
            assessments.append("pcaps disagree on subtype roster")
        consensus_reports.append(
            {
                "subtypeWord": subtype_word,
                "pcapCount": len(reports),
                "pcaps": pcaps,
                "objectCounts": sorted(object_counts),
                "objectNameSets": [list(item) for item in sorted(object_names)],
                "mutableObjectSets": [list(item) for item in sorted(mutable_objects)],
                "singleSampleObjectSets": [list(item) for item in sorted(single_sample_objects)],
                "assessment": "; ".join(assessments),
            }
        )
    return consensus_reports


def build_consensus_only_summary(summary: dict[str, Any]) -> dict[str, Any]:
    return {
        "pcaps": summary.get("pcaps", []),
        "objectNames": summary.get("objectNames", []),
        "subtypeWords": summary.get("subtypeWords", []),
        "window": summary.get("window"),
        "objectConsensusReports": summary.get("objectConsensusReports", []),
        "subtypeConsensusReports": summary.get("subtypeConsensusReports", []),
    }


def build_targeted_summary(
    pcap_paths: list[Path],
    object_names: list[str] | None = None,
    subtype_words: list[str] | None = None,
    window: int = 4,
) -> dict[str, Any]:
    normalized_object_names = list(dict.fromkeys(object_names or []))
    normalized_subtype_words = list(dict.fromkeys(subtype_words or []))
    per_pcap_windows = []
    combined_object_reports = []
    combined_subtype_reports = []
    for pcap_path in pcap_paths:
        history = extract_protocol_history_from_pcapng(pcap_path)
        object_reports = []
        for object_name in normalized_object_names:
            observation_summary = extract_object_observation_summary(history, object_name)
            windows = extract_object_transition_windows(history, object_name, window)
            report = {
                "objectName": object_name,
                "observationSummary": observation_summary,
                "objectTransitionWindows": windows,
            }
            object_reports.append(report)
            combined_object_reports.append({"pcap": str(pcap_path), **report})
        subtype_reports = []
        for subtype_word in normalized_subtype_words:
            report = extract_subtype_observation_summary(history, subtype_word)
            subtype_reports.append(report)
            combined_subtype_reports.append({"pcap": str(pcap_path), **report})
        per_pcap_windows.append(
            {
                "pcap": str(pcap_path),
                "packetCount": len(history),
                "objectReports": object_reports,
                "subtypeReports": subtype_reports,
            }
        )
    return {
        "pcaps": [str(path) for path in pcap_paths],
        "objectNames": normalized_object_names,
        "subtypeWords": normalized_subtype_words,
        "window": max(0, window),
        "objectReports": combined_object_reports,
        "subtypeReports": combined_subtype_reports,
        "objectConsensusReports": build_object_consensus_reports(combined_object_reports),
        "subtypeConsensusReports": build_subtype_consensus_reports(combined_subtype_reports),
        "perPcap": per_pcap_windows,
    }


def summarize_pcaps(pcap_paths: list[Path]) -> dict[str, Any]:
    all_history: list[dict[str, Any]] = []
    per_pcap: list[dict[str, Any]] = []
    for pcap_path in pcap_paths:
        history = extract_protocol_history_from_pcapng(pcap_path)
        all_history.extend(history)
        summary = summarize_protocol_history(history)
        per_pcap.append(
            {
                "pcap": str(pcap_path),
                "packetCount": len(history),
                "descriptorPayloadComparisons": summary.get("descriptorPayloadComparisons", []),
                "descriptorStateCorrelations": summary.get("descriptorStateCorrelations", {}),
                "descriptorFollowups": summary.get("descriptorFollowups", {}),
                "unresolvedControlComparisons": summary.get("unresolvedControlComparisons", {}),
            }
        )

    combined_summary = summarize_protocol_history(all_history)
    return {
        "pcaps": [str(path) for path in pcap_paths],
        "packetCount": len(all_history),
        "descriptorPayloadComparisons": combined_summary.get("descriptorPayloadComparisons", []),
        "descriptorStateCorrelations": combined_summary.get("descriptorStateCorrelations", {}),
        "descriptorFollowups": combined_summary.get("descriptorFollowups", {}),
        "unresolvedControlComparisons": combined_summary.get("unresolvedControlComparisons", {}),
        "perPcap": per_pcap,
    }


def main() -> int:
    args = parse_args()
    pcap_paths = [Path(path) for path in args.pcaps]
    if args.object_name or args.subtype_words:
        summary = build_targeted_summary(
            pcap_paths,
            object_names=args.object_name or [],
            subtype_words=args.subtype_words or [],
            window=args.window,
        )
        if args.consensus_only:
            summary = build_consensus_only_summary(summary)
    else:
        summary = summarize_pcaps(pcap_paths)
    indent = 2 if args.pretty else None
    rendered = json.dumps(summary, indent=indent, sort_keys=False)
    if args.write_path:
        write_path = Path(args.write_path)
        write_path.write_text(rendered + ("\n" if not rendered.endswith("\n") else ""), encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
