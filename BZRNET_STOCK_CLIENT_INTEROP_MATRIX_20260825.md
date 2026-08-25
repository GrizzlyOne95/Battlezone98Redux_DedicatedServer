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
- Capture: Wireshark both sides (no port filter beyond `tcp port 1337 or udp`), SHA-256 recorded, raw kept private; classification via [`research/bzrnet_packet_decoder.py`](research/bzrnet_packet_decoder.py) (never heuristic labels).
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

**Goal:** determine what a stock client actually does when one middle reliable fragment disappears — distinguishing "opaque forwarding is sufficient" from "server eventually needs retransmission/reset boundary awareness".

**Injection point:** force the relayed path so match traffic traverses our UDP 1339 endpoint-pair relay (both clients run `/iprelay` against the community server). Add to the relay an **opt-in test-mode drop rule**, default OFF, diagnostics-only:

- env/flag e.g. `BZRNET_FAULT_INJECT=drop_reliable_midfrag:k=N`
- drops the Nth datagram whose decoded header matches `flags & 0x80 && !(flags & 0x40)` (a bare-`0x80` reliable fragment, i.e., NOT final);
- logs every drop (timestamp, seq_a/seq_b, direction, peer pair).

This is test instrumentation, not protocol behavior change; it must ship behind the default-off flag and never activate in normal service. If the relay path proves impractical for a cell, fallback injector: OS-level UDP impairment (clumsy / Network Emulator) filtered by first-byte pattern `0x80…`.

**Expected outcomes and decision rule:**

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
