# BZRNet Research Artifacts

Protocol research outputs accompanying `research/notes/BZRNET_PROTOCOL_ARCHAEOLOGY_20260825.md`.

| Path | Contents |
|---|---|
| `bzrnet_message_map.csv` | Machine-readable message/opcode table: 50 records across WS(1337), UDP 1338, transport header, identity handshake, and in-match game plane, with direction, length bounds, reliability class, native send/receive anchors (GOG 2.2.301 VAs), session states, confidence, notes |
| `bzrnet_packet_decoder.py` | Deterministic decoder for the 18-byte BZRNet transport header, UDP-1338 discovery probe/reply, and game-plane opcode classification. Run `--selftest`; validated against March 21 capture vectors |
| `decomp_evidence_20260825/` | Decompiled native functions cited by the report (`decomp_<tag>_<VA>.c`), produced from the analyzed GOG authority binary (Ghidra program `battlezone98redux.exe-007c64`, SHA-256-matched) |
| `snapshots/` | Bounded/sanitized capture-derived fixtures land here |

## Evidence standards

1. Raw captures containing tickets, public IPs, Steam IDs, or stable identifiers stay local and are **never committed**. Committed artifacts must be synthetic or structurally sanitized (documentation-range values only), per the rule set established in `docs/BZRNET_PHASE2_CAPTURE_GUIDE_20260716.md`.
2. Every protocol claim carries a label — PROVEN-native, PROVEN-capture, CORRELATED, INFERRED, UNKNOWN — as defined in the archaeology report.
3. PDB-derived names/addresses are advisory; native anchors in this directory come from decompiling the authority binary itself and are recorded as VAs so future builds can re-validate prologue bytes at each site.
4. The decoder self-test uses only already-published sanitized capture bytes; extend it whenever a new message family is proven.
