# Stock Client Interoperability & Fault-Injection Matrix — Work Order (Phase 2)

**Date:** 2026-08-25
**Status:** DEFINED — not started. Freezes Protocol Archaeology Phase 1 (`BZRNET_PROTOCOL_ARCHAEOLOGY_20260825.md`, commit `67e4689` + layering addendum).
**Predecessor evidence:** native transport framing, game-opcode taxonomy, reliability semantics, and the layering/obligation model in §2.1 of the archaeology report.

---

## 1. Objective and success definition

Demonstrate that **a server implementing only the authority (WS :1337) and allocation (UDP :1338 / relay signaling) layers can carry an entire real stock-client match correctly.**

Success is **not** learning more opcodes. Success is:

- every matrix cell below PASSES with the server never needing game-payload semantics; and
- the controlled-impairment experiment answers whether opaque relay forwarding is sufficient, or names precisely which additional semantic handling (if any) the server requires.

Either outcome closes the milestone: it either opens the implementation gate confidently, or produces an exact statement of required semantic awareness.

## 2. Environment

- Two PCs on independent network paths preferred (same LAN acceptable for non-relay cells); stock clients: GOG 2.2.301 (`8D71F56C…`) + Steam build (post-settle).
- Server under test: current `agent/bzrnet-interoperability-research` build; `/health`, `/protocol`, `/captures` endpoints live.
- Client redirect: hosts file `battlezone98mp.webdev.rebellion.co.uk → <server>` or launch flags `/bzrserver=<ip> /bzrnetport=1338`; logging flags `/netpktlog /netlog=3 /bzrnetlog=3` per the Phase2 capture guide.
- Capture: Wireshark both sides (no port filter beyond `tcp port 1337 or udp`), SHA-256 recorded, raw kept private; classification via [`research/bzrnet_packet_decoder.py`](../bzrnet_packet_decoder.py) (never heuristic labels).
- Independent referee where available: GameWatcher observer profile.

## 3. Matrix

Smallest set that exercises every proven state transition. One variable per cell.

| # | Test | Proves | Record |
|---|---|---|---|
| 1 | Connect → lobby → match | control-plane → game-plane transition end-to-end | pcap pair, WS transcript, BZLogger tails |
| 2 | Two real clients, same match | relay correctness with two independent peer streams | per-endpoint stream summaries |
| 3 | Spawn / respawn | `PS` handling; NetState 3→4 gate on real traffic | dispatcher-gate timing vs capture |
| 4 | Chat (public + whisper) | `PC` / `pS` routing | decoded opcode counts |
| 5 | Kill / death | `MK` behavior | event-aligned pcap slices |
| 6 | Object create/destroy bursts | `PO/xo/io/xx/RO/GM` volumes pass untouched | decoder family counts |
| 7 | Lock/unlock lobby | `LS`/`US` propagation | UI text + wire tags |
| 8 | Sustained idle ≥10 min | `KA` cadence, sequence continuity, timestamp tolerance across clock skew | seq/ts continuity plots |
| 9 | Deliberate packet loss | reliable retransmission/reset behavior | §4 impairment protocol |
| 10 | Large reliable payload (>1450 B burst) | fragmentation/reassembly integrity | fragment flag census (`0x80`-only vs `0xC0`) |
| 11 | Mid-match disconnect + rejoin | connection-state cleanup; fresh sequence state | residual-state assertions |

Cell verdicts: PASS / PASS-with-notes / FAIL(+exact missing semantic). A FAIL that can only be fixed by decoding game payloads must quote the specific handler from §5 of the archaeology report before any implementation work opens.

## 4. Controlled impairment (highest-value experiment)

**Execution order (normative):**

1. **Implement relay instrumentation only.** Default-off flag, exact match predicate, counters/logging, correlation IDs. **No protocol interpretation** may be added while implementing it.
2. **Prove transparent forced-relay operation first.** Stock↔stock through :1339 must behave identically to the corresponding direct baseline *before* fault injection is ever enabled.
3. **Execute the matrix exactly as frozen.** This document is the test contract; surprising observations do not alter expected behavior.
4. **Every FAIL stops at a named §5 handler** (archaeology report). No generalized "interop failed" verdicts; no archaeology gate opened on symptoms alone.
5. **Impairment only where the matrix requires it**, and **every impairment cell has an immediately preceding forced-relay, impairment-OFF control run** — otherwise a relay bug could masquerade as evidence that bare-`0x80` fragments are semantically important.
6. After the matrix, obey §5 frozen priorities.

**Relay shall not help (non-goals, normative):** no resequencing, reassembly, retransmission, timeout compensation, normalization, duplicate suppression, or fragment awareness of any kind beyond the literal fault-selection predicate below. In normal mode the relay must remain as close to a byte-transparent datagram forwarder as possible, so any success is attributable to stock client behavior rather than an accidentally built compatibility layer.

**Injection point:** force the relayed path so match traffic traverses our UDP 1339 endpoint-pair relay (both clients run `/iprelay` against the community server). Relay instrumentation:

- `--fault-inject drop_reliable_midfrag:k=N` (or `BZRNET_FAULT_INJECT` env), **default OFF**;
- predicate: decoded header satisfies `len ≥ 18 AND (flags & 0x80) AND NOT (flags & 0x40)` — a reliable fragment that is not final (equality to literal `0x80` is what captures have shown so far; the masked form is the contract);
- selects the **k-th matching datagram within each directed peer-pair stream** (per `src→dst` 5-tuple), so impairment hits one stream deterministically;
- logs every drop (timestamp, seq_a/seq_b, direction, peer pair, per-stream match ordinal);
- counters for **observed / forwarded / dropped** datagrams exposed on `/status`.

This is test instrumentation, not protocol behavior change; it must ship behind the default-off flag and never activate in normal service. If the relay path proves impractical for a cell, fallback injector: OS-level UDP impairment (clumsy / Network Emulator) filtered by first-byte pattern `0x80…`; use the documented fallback rather than redesigning the experiment mid-run.

**Forensic relay-trace minimum (per relayed datagram, opt-in via `--relay-trace <file>` / `BZRNET_RELAY_TRACE`, JSON Lines, local-only — never commit raw traces):** experiment/run ID, monotonic + wall timestamp, direction, UDP source tuple, UDP destination tuple, transport flags byte, kind nibble, seq_a, seq_b, payload length, forwarding disposition (`forwarded|dropped|dropped_faultinject`), per-stream ordinal. Payload bytes are **not** logged; decoding beyond the header is not required for later comparison and is deliberately omitted.



**Expected outcomes and decision rule** (classify each impairment run, together with its impairment-OFF control run, against this table):

| Observation | Interpretation | Gate |
|---|---|---|
| Sender retransmits the fragment range (resend-tick path, §8) | client-owned recovery; opaque forwarding sufficient | stays CLOSED |
| Receiver logs `Resetting Connection Status` / `bzrinmap failure` then recovers | reset boundaries exist but are self-healing client-side | stays CLOSED; document latency |
| Session stalls until peers give up / auto-kick | recovery depends on conditions our relay could break | OPEN narrowly — define exact semantic needed |
| Any outcome requiring payload inspection to fix | architecture limit found | OPEN with named handler |

## 5. Post-matrix priority order (frozen)

1. **Capture A — 1339 allocation envelope.** Largest remaining architectural unknown; governs how the transport endpoint gets established before the (now understood) game plane starts.
2. **Official reasonCode captures** (Capture B scenarios). Fidelity/diagnostics value; unlikely to invalidate architecture.
3. **`protocol_contract.json` regeneration.** Once, after the above evidence lands — the contract becomes a generated representation of the final model instead of being edited twice.
4. **Payload structure archaeology only where interoperability fails.** 50 mapped records exist; no preemptive deep-decoding of object/state payloads while the server can stay transport-opaque.

## 6. Exit criteria

- All 11 cells executed with verdicts recorded and artifacts hashed.
- Impairment outcome row selected with evidence.
- Archaeology report §13 coverage table refreshed from results; go/no-go statement written into this doc's §7 (appended at completion).
