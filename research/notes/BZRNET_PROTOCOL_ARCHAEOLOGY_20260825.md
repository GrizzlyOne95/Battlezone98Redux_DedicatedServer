# BZRNet Protocol Archaeology — Layered Interoperability Map

**Date:** 2026-08-25
**Branch:** `agent/bzrnet-interoperability-research`
**Binary authority:** Battlezone 98 Redux **GOG 2.2.301**, SHA-256 `8D71F56C1314E69A8AD38F4EEAF20A8FF825965A84CF196E5F77EA4CC3377413` (clean, unpacked; runtime memory verified byte-identical to disk on 2026-08-20 via OpenShim runtime capture, base `0x400000`, no relocation delta).
**Companion artifacts:** [`research/bzrnet_message_map.csv`](../bzrnet_message_map.csv) (machine-readable), [`research/bzrnet_packet_decoder.py`](../bzrnet_packet_decoder.py) (deterministic decoder + self-test), [`research/decomp_evidence_20260825/`](../decomp_evidence_20260825/) (46 decompiled native functions cited below).

**Phase status:** this document closes **Protocol Archaeology Phase 1** (commit `67e4689` + layering addendum). Generic packet archaeology is frozen; the successor milestone is **"Stock Client Interoperability & Fault-Injection Matrix"** — defined in [`BZRNET_STOCK_CLIENT_INTEROP_MATRIX_20260825.md`](BZRNET_STOCK_CLIENT_INTEROP_MATRIX_20260825.md). Remaining unknowns (§14) are to be resolved experimentally, not by another broad decompile sweep.

**Relationship to prior work.** This report *extends*, and where stated *supersedes*, the 2026-08-24 interoperability report. The WS/matchmaking plane (TCP 1337), the UDP 1338 discovery reply rule, the reason-code registry, and the community-server fix history are **unchanged** and are not re-derived here. The new material is the native (decompiled) proof of the transport framing, the full in-match game-plane opcode table, the reliability machinery, and the session state machine.

Evidence labels used throughout:
**PROVEN-native** (decompiled send+receive paths in the authority binary) · **PROVEN-capture** (controlled/sanitized packet evidence) · **CORRELATED** (capture pattern + partial native anchor) · **INFERRED** · **UNKNOWN**.

---

## 1. Executive summary

1. The multiplayer stack is a **four-plane layered system**: WebSocket matchmaking (TCP 1337, text JSON), endpoint discovery (UDP 1338 probe/echo), direct peer match transport (UDP, BZRNet-framed), and an optional relay fallback (UDP 1339). This was already established; this pass adds native proofs for planes two through four.
2. **The common BZRNet transport header is now fully decoded natively** (both writer and parser recovered):
   `flags u8 | kind nibble u8 | timestamp u64 BE unix-ms | seq_a u32 BE | seq_b u32 BE | payload`.
   Flag semantics are refined against all prior captures: **bit7 (`0x80`) = reliable**, **bit6 (`0x40`) = final-fragment/single-packet**. The prior "0xC0 = reliable / 0x40 = unreliable" hint remains true observationally but the mechanism is fragmentation completion.
3. **The entire in-match game protocol opcode table has been recovered from the native dispatcher** (`Net::OnP2PReceived` equivalent at VA `0x570500`): every gameplay message is a payload beginning with **two ASCII characters** compared as a little-endian u16 — including `PP/PR/GA` ping family, `PC/pS` chat family, `MK` kill, `PS` spawn point, `SS` sync step, `KA` keepalive stamp, `PB` bandwidth, `AS/KS` kick notices, `LS/US` lock notices, `PO/xo/io/xx/RO/GM/PK` object replication family, and the `'_'` (0x5F) state-block stream. This confirms and promotes every marker heuristic from the March 21 paired-capture analysis.
4. **Reliability layer proven:** per-peer connection objects carry dual sequence counters (`+0x88` tx-seq incremented **per reliable fragment**; `+0x84` believed ack/echo counter); reliable payloads fragment at **1450 bytes (`0x5AA`) per datagram**; receiver enforces `18 ≤ len ≤ 2048`; mismatched sequences trigger "Resetting Connection Status" handling; a resend-tick path exists behind a 1000 ms timer call.
5. **Session gating proven:** gameplay handlers require `NetState == 4`; receipt of a `PS` spawn-point packet performs the **NetState 3 → 4 transition** (loading → in-match). Matchmaking-plane state lives in `CSteamLobby::EMPState`; route selection states 1/3/5/6 remain as documented 2026-08-24.
6. **UDP 1339 remains the single large UNKNOWN** (allocation envelope). Nothing in this pass contradicts the opaque-relay design; the relay can stay transport-transparent because no peer-plane packet carries routing metadata that the relay must interpret.
7. **Dedicated-server coverage verdict:** the WS plane is functionally covered; UDP 1338 is fully covered; the match plane needs **no decoding** to relay transparently but now has a complete taxonomy available for diagnostics; P0 gaps are unchanged from 2026-08-24 (real-client matrix runs, official reasonCodes B-captures, 1339 envelope).

---

## 2. Architecture diagram

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                          stock Redux client / community server              │
├───────────────────┬──────────────────┬──────────────────┬───────────────────┤
│ PLANE A           │ PLANE B          │ PLANE C          │ PLANE D           │
│ Matchmaking       │ Discovery        │ Match transport  │ Game protocol     │
│ WS TCP 1337       │ UDP 1338         │ UDP direct /1339 │ inside C payloads │
│                   │                  │                  │                   │
│ {type,content}    │ 18B header       │ 18B BZRNet hdr   │ 2-char ASCII tag  │
│ {type,data}       │ kind=2           │ flags/kind/ts/   │ + body (LE)       │
│ CSteamLobby       │ echo+endpoint    │ seqA/seqB        │ dispatcher gates: │
│ EMPState machine  │ ±60s skew check  │ REL fragments    │  NetState==4      │
│ (JSON builders    │ cadence 10s/30s  │ ≤1450B each      │  sender != myMini │
│  VA 6Cxxxx)       │                  │ (VA 75beb0)      │ (VA 570500)       │
└───────────────────┴──────────────────┴──────────────────┴───────────────────┘
        │                   │                    │                 │
        ▼                   ▼                    ▼                 ▼
   lobby/join/chat     NAT endpoint        opaque forwarding   simulation-level
   signaling only      learning only       (relay never needs  events (kill/chat/
                       (feeds Auth +        to decode D)       spawn/state sync)
                       DoUpdateWAN/LAN)
```

### 2.1 Layer model and semantic obligations (normative)

This is the canonical layering for all future work. Each layer states **how much the dedicated server must understand** — the distinction that keeps this project from drifting into a full Battlezone network reimplementation when the native evidence says one is not needed:

```
WS :1337
  matchmaking / session authority            [MUST understand]
      |   Authorization, lobby state, identity metadata,
      |   P2P signaling (DoP2PConnect / OnLobbyMemberP2PConnect /
      |   DoP2PRoute / OnP2PRoute), reason codes
      v
UDP :1338
  allocation / connection establishment      [MUST understand enough to route]
      |   unconditional echo + observed-endpoint trailer;
      |   feeds wanAddress / lanAddresses used for peer candidate selection
      v
P2P transport framing  (18-byte BZRNet header, both planes C)
      flags   0x80 reliable / 0x40 final-fragment    [MAY understand —
      ts      u64 BE unix-ms, ±60 s tolerance         validation &
      seq_a   u32 BE (reliable stream counter)        diagnostics only]
      seq_b   u32 BE (ack/secondary, role CORRELATED)
      fragmentation ≤ 1450 B per reliable datagram, ≤2048 B per receive
      |
      v
2-byte game opcode  (PP PR GA PC pS MK PS SS KA PB AS KS LS US AK KK GS
                      PO xo io xx RO GM PK NS LA LU PT RG AC '_')  [MAY observe]
      |
      v
opaque game payload                           [SHOULD REMAIN OPAQUE unless required]
```

Obligation ↔ coverage map:

| Layer | Obligation | Server today | Basis |
|---|---|---|---|
| WS :1337 | MUST understand | SUPPORTED (42-record contract) | CONFIRMED live + binary |
| UDP :1338 | MUST understand enough to establish routing | SUPPORTED (echo/trailer rule) | PROVEN-native + capture |
| Transport envelope | MAY understand (validation/diagnostics) | PASS-THROUGH; decoder available | §4, §8 |
| Game opcode | MAY observe | PASS-THROUGH; taxonomy mapped | §5 |
| Game payload | SHOULD remain opaque unless required | opaque by design | §5, §11 |

The decisive structural fact protecting this design: **no peer/game-plane message contains third-party addressing information** (exhaustive review of the recovered dispatcher, §11). The relay therefore never needs to read below the transport envelope to route correctly — opaque forwarding is an architecture supported natively, not an implementation guess.

---

Native module map (GOG 2.2.301 VAs; PDB advisory names cross-checked per `private_pdb_semantic_ranking.md` policy):

| Plane | Component | Evidence |
|---|---|---|
| A | `BZRNetInterface` WS dispatcher `0x6BF2A0`; JSON verb builders `0x6C3D50..0x6C6E60` | PROVEN-native (string-xref; matches 2026-07-16 schema doc) |
| B | discovery handler `0x6C6E60`; listener `0x6C83F0`; socket open `0x6BE8C5` | PROVEN-native |
| C | send `FUN_0075AAF0` (TRY/handshake), `FUN_0075BEB0` (REL/UNR); recv parse in `0x6C6E60` cluster; conn-reset `0x75CA90`/`0x75D090`; connect lifecycle `0x75EEA0`/`0x75D800`/`0x75F370` | PROVEN-native |
| D | game dispatcher `0x570500` (vtable slot `0x883B5C`); Net singleton fields at `+0x410..+0x868`; player registry `NetPlayer` (mini-id map) | PROVEN-native |

---

## 3. Transport layer

### 3.1 Planes and endpoints

| Surface | Transport | Port | Framing | Byte order |
|---|---|---|---|---|
| Matchmaking | WebSocket text frames (TCP) | 1337 | JSON `{type,content}` / `{type,data}` | n/a |
| Discovery | UDP datagrams | 1338 | 18-byte BZRNet header (+ trailer on replies) | **BE** header fields |
| Peer match | UDP datagrams (direct or via 1339) | ephemeral ↔ ephemeral / 1339 | same 18-byte header + game payload | **BE** header, **LE** payload fields |
| Relay signaling | WS verbs `DoP2PRoute`/`OnP2PRoute` | 1337 | JSON | n/a |

### 3.2 Socket facts (native)

- P2P socket open logs buffers and port: `"BZRNet P2P Socket Opened With %ld received buffer, %ld send buffer, port %ld"` (`0x6BE8C5`). Community netcode patch raises these to 512 KiB send / 4 MiB recv (`netcode_manifest.json` RVAs validated against this build).
- Receiver length gate: **`len < 0x12 || len > 0x800` → drop** (PROVEN-native, discovery path parse).
- Timestamp validation: echoed/probe timestamps must be within **±60000 ms** of local time or the datagram is dropped (PROVEN-native, `local_454 + 60000` compare).
- The discovery probe cadence (~10 s once, then ~30 s, continuing during matches) is CONFIRMED by capture (2026-03-21) and unchanged.

### 3.3 Build identity note

The Steam on-disk exe (updated 2026-08-13, `D298782F…`) is **SteamStub-packed**; the GOG exe is clean. Section layouts are identical across both, but code bytes differ wholesale — so the leaked PDB's addresses (and any RVA recorded from it) are **advisory for naming only** and were NOT used directly. All VAs in this report come from decompiling the GOG image itself (Ghidra project `pyghidra_mcp_projects/BZ98_Redux`, program `battlezone98redux.exe-007c64`, SHA-256-matched to the authority binary), with PDB names applied only where the repo's semantic-validation policy allows.

---

## 4. Packet framing

### 4.1 Common BZRNet transport header (all UDP planes)

```
offset  size  field                 notes
0       1     flags                 bit7 0x80 = RELIABLE
                                    bit6 0x40 = FINAL-FRAGMENT / single-packet
1       1     kind (low nibble)     observed: 2=discovery, 3/4/5=identity/handshake
2..9    8     timestamp             u64 BIG-ENDIAN unix-ms (writer FUN_00760670;
                                    validated ±60 s on discovery path)
10..13  4     seq_a                 u32 BE, sourced from conn+0x88
14..17  4     seq_b                 u32 BE, sourced from conn+0x84
18..    var   payload               game plane: 2-char ASCII opcode + body
```

- Writer order proven by disassembly of `FUN_0075BEB0`: buffer acquire → `flags |= …` → ts@2 → conn+0x88 → conn+0x84 → payload append → UDP send (`FUN_00760590`). Parser order proven in `0x6C6E60`: ts@+2, seq@+10, seq@+14 relative to packet start.
- **Flag semantics refinement (supersedes the 0xC0/0x40 shorthand):** unreliable sends set exactly `0x40`. Reliable sends set `0x80` on *every* fragment and additionally `0x40` on the *last* fragment (single reliable packets therefore appear as `0xC0`). Consequence: `0xC0` ⇒ reliable-and-complete; bare `0x80` ⇒ reliable fragment with more coming; bare `0x40` ⇒ unreliable.
- Byte-order statement: **every multibyte transport-header field is big-endian** (shift-loop writers emit MSB first). Game-payload multibyte fields read by the dispatcher are little-endian unless noted.
- Kind nibble construction site (writer side) was not pinned this pass; parse-side `& 0xF` is PROVEN-native. UNKNOWN: whether high nibble of byte 1 carries meaning.

### 4.2 Discovery (kind 2) request/reply

- Request: 18 bytes, `40 02`, ts, zeroed seqs (PROVEN-capture ×many).
- Reply: `echo(request) || len || addr || port(BE u16)`; IPv4 total 25 bytes; empty input yields a 7-byte reply (trailer only) — CONFIRMED live 2026-08-24 and implemented by the community server.
- IPv6 branch (native): accepts `len==16` with total `0x24`; one byte of the v6 layout remains unaccounted (36 vs expected 35) — minor UNKNOWN, irrelevant to interop.

### 4.3 Identity / handshake family (kinds 3/4/5)

Captured 37-byte packets `40 03|04|05 …` carrying NUL-terminated ASCII Steam IDs (`S<steam64>`) both directions before launch (PROVEN-capture, March 21 pair). Native dispatcher routes the post-handshake payload by the game-opcode switch below; the kind 4→5 progression semantics beyond identity exchange remain INFERRED (see §14).

---

## 5. Message taxonomy

Full table: [`research/bzrnet_message_map.csv`](../bzrnet_message_map.csv). Research IDs are stable wire tags (the two ASCII chars themselves) — no MSG_nn aliases needed anymore; promotion is complete for the previously heuristic markers:

| Captured marker (Mar 21 heuristic) | Native verdict (this pass) | Confidence change |
|---|---|---|
| `PP` peer_ping_request | `PP` = PING (PING_PACKET, 20-B body) | heuristic → **PROVEN-native** |
| `PR` peer_ping_response | `PR` = PING_REPLY (≥14 B; HandlePingReply `0x571E20`) | heuristic → **PROVEN-native** |
| `PB` bandwidth pacing | `PB` = BANDWIDTH (BANDWIDTH_PACKET: dwBandwidth u32 + interval4ms u8) | heuristic → **PROVEN-native** |
| `KA` peer_keepalive | `KA` = ACTIVITY_ACK (stamps last-seen; keepalive role confirmed) | medium → **PROVEN-native** |
| `PO` unknown object control | `PO` = OBJECT_POSITION stream handler | low → **CORRELATED+native** |
| `SS` peer_session_sync | `SS` = SYNC_STEP (subcode checked against current step) | medium → **PROVEN-native** |
| `0x5f` state block | payload-first-byte `'_'` branch → aux blob + state stream apply | sketch → **PROVEN-native structure** |

Newly identified (not visible as clean markers in old captures): `GA` ping array, `PC` public chat, `pS` whisper, `MK` kill, `PS` spawn point (**state gate!**), `AK/KK/GS` dialog-open trio, `AS/KS` kick notices, `LS/US` lock notices, `RO/GM/PK/xx/ns/la/lu/PT/RG` aux/object families, `AC` cheat probe.

Serialization primitives worth noting (the "shared WriteInt" equivalents):

- Transport: `FUN_00760670` (append u64 BE), `FUN_007606D0` (append u32 BE), `FUN_00760730` (append byte range), `FUN_007596B0` (send raw ≤1153-byte buffer to mini-id target — the shared game-send primitive), `FUN_006CF0F0/FUN_004BB390` (send-buffer access/size).
- Structs (PDB layout, sizes match captured bodies): `PING_PACKET` 20 B, `PINGREPLY_PACKET` 8 B, `BANDWIDTH_PACKET` 8 B, `SNIPE_PACKET` 8 B (dwHitObject+shooterTeam), `SPAWNPOINT_PACKET` 16 B (xyz floats + team u8).
- Matchmaking JSON: builder table already published 2026-07-16 (unchanged).

---

## 6. Handshake

Sequence (host H, joiner J), synthesizing 2026-08-24 §6 flow with new transport detail:

```
A.  WS: TCP1337 connect → HTTP upgrade
B.  WS: Authorization → OnAuthorization + OnWhitelistUpdated        (auth gate)
C.  UDP1338: probe immediately, +10s, then every ~30s forever       (NAT learn)
D.  WS: DoEnterLounge(true) → OnLobbyListChanged
E.  WS: join default chat lobby → CreateGame/SetLobbyData×n (host)
    or DoJoinLobby{id,password} (joiner) → OnLobbyJoined
F.  WS: SetPlayerData identity (miniid/team/vehicle/ready…),
    launch metadata (nextmid, gameSettings, userPack, knownPlayers)
G.  WS: DoP2PConnect{player,reset} ⇄ OnLobbyMemberP2PConnect        (signaling)
H.  UDP: kinds 3/4/5 identity exchange (ASCII SteamIDs)             (transport)
I.  UDP: SS sync-step exchange; launch barrier GotAllSyncs()        (sync)
J.  WS: launched:"1" on lobby+player                                (launch)
K.  UDP: PS spawn point host→client ⇒ NetState 3→4 (IN_MATCH)       (match start)
L.  UDP: PO/_state_/xo·io roster & position streams begin           (match)
```

Steps F–K ordering races are tolerated by design (first peer UDP may precede OnLobbyMemberP2PConnect — CONFIRMED 2026-03-21). Route selection before H follows the LAN/WAN/relay decision tree documented 2026-08-24 §11 (states 1/3/5/6; forced-relay global `0x946708`; `/iprelay` string at VA `0x7D5120`).

Version negotiation: none observed at transport level. Version strings ride WS (`clientVersion:"2.2.301"`, `apiVer:"3.6"`, `GameVersion` lobby metadata). Map parity uses `mapCRC/mapWorkshop/mapLatestVer` fields in `gameSettings/playerSettings` (PROVEN-native struct members).

---

## 7. Session state machine

Two cooperating machines:

**Matchmaking (`CSteamLobby::EMPState`)** — enum values not yet dumped (`EMPStateNames` PDB address invalid for this build; extraction attempt returned unrelated data). Functionally reconstructed states: Disconnected → Connecting(WS) → Authorized(+whitelist) → Lounge → JoiningLobby → InLobby(chat/game) → [Launch handoff]. Route-selection sub-states 1(LAN)/3(WAN)/5(relay-request)/6(relay-connect) PROVEN-native via log strings (`Starting/FAILED/Completing WAN|RELAY Connect For Client %s`).

**Match (`NetState`, `Net::state` @singleton+0xC)** — PROVEN-native transitions:

```
0..2  pre-match (lobby/sync setup; exact enum names UNKNOWN)
3     LOADING/SYNC  ── receive PS (spawn point) ──▶  4
4     IN_MATCH      ◄── all gameplay handlers gated here ──
      exit paths: AK/KK/GS one-shot dialog-open (match end / kicked UX),
                  AS/KS kick notices, auto-kick eviction, Net Execute failure
                  ("DoNextFrame: Quiting Game Due to Net Execute failure")
```

Invalid-message handling: unknown 2-char tags fall through the dispatcher silently; non-in-match arrival is dropped by the `NetState==4` gate; foreign mini-id senders are dropped by the `sender != myMiniId` guard on most handlers (PROVEN-native).

Timeout/keepalive: `KA` stamps last-seen per player; auto-kick governed by config vars `AutoKickTime/AutoKickLoss/AutoKickPing` (+`MinBandwidth/MaxBandwidth` etc., registered at `0x56FD20`); `"Auto kicking player "` log at `0x576B70`.

---

## 8. Reliability layer

All items PROVEN-native unless noted. Custom reliability over single UDP socket — no multiple channels, no ack bitfields observed.

| Property | Finding |
|---|---|
| Reliable class | flag `0x80`; queued per-peer; resend machinery behind `FUN_0075FE50(1000,…)` tick after reliable flush; conn fields `+0x19` retry flag, `+0x1A` connected flag, `+0x20/+0x28` timing words |
| Fragmentation | threshold **`0x5AA` = 1450 B** per datagram; loop emits fragments while remaining > 0 |
| Sequence space | dual counters: tx-seq `conn+0x88` **incremented once per reliable fragment** (PROVEN); `conn+0x84` second counter (role INFERRED: ack/echo — printed as the second `%u` in `Sent Packet (%u,%u)`; PR#1 instrumentation saw variation confined to its low 16 bits) |
| Receiver expectations | expected `(seq_a,seq_b)` tracked per incoming connection (`get_expected_seq` reads `*(conn_in)+0x84/+0x88`); logged as `Received Packet (%u,%u) expecting (%u,%u)` |
| Duplicate/stale handling | `"Resetting Connection Status For Client %s"` (`0x75CA90`/`0x75D090`); `"Failure with bzrinmap detected"` (`0x75D800`) |
| Rollover | natural u32 wrap implied by increment-only code; no special-case observed (UNKNOWN for edge behavior) |
| Max payload | receive gate 2048 B; send primitive cap 1153 B per call; reliable fragments capped 1450 B incl. header |
| Ordered classes | reliable stream ordered by tx-seq; unreliable unordered (no ack/retransmit path touched) |
| Keepalive | application-level: `PP` pings + `KA` stamps + AutoKick* thresholds (not transport-level) |

Interop consequence (unchanged from 2026-08-24, now structurally grounded): a relay/server that forwards whole datagrams byte-for-byte preserves all of the above; anything that re-frames or splits must reproduce the exact header layout, flag discipline, and 1450-byte chunking.

---

## 9. Gameplay replication

- **State broadcast:** `TempStateSendAll` (`0x4B9DA0`), called per-frame from the frame pump when active (`DoNextFrame` chain `0x618130` → `0x4DF3B0`). Log format exposes governance inputs: `t:%d, sia %d, Bps %5.0f, dwB %d, now-nppst %d` plus `Net::NextPositionPacketInterval` — i.e., bandwidth-budgeted, interval-gated emission. Wire form = `'_'`(0x5F) blocks: `[aux_count][aux blob][stream…]` applied via `0x584620` (aux) + `0x4B7750` (stream) (PROVEN-native structure; per-field semantics partially decoded in the March descriptor work, which this pass's opcode identities now anchor).
- **Positions:** `PO` handler (`0x4B7710`) consumes per-object position/control updates after stamping the sender active. Cadence constants live in `NextPositionPacketInterval/MaxPositionPacketInterval/NPPI divisor globals` (PROVEN-global, semantics INFERRED).
- **Object lifecycle:** create families `xo`/`io` (arg-differentiated `0x584C40`), event `xx` (`0x584BB0`), remove-ish `RO` (`0x584620` shared with aux apply). These correspond to the captured `7a75` descriptor bursts (roster/setup) preceding `0x5f` streams (CORRELATED; matches 2026-03-21 §11–12 observations).
- **Scores/lives:** `PT` handler applies value with `0x78`/`0x3C` limit constants (INFERRED purpose); `GameOverTime/dwNextScoreTime/bStopGame/GameWon()` exist as singleton/global state (PROVEN-members).

---

## 10. Object ownership

- Host authority model confirmed structurally: spawn points (`PS`), kicks (`KS/AS`), locks (`LS/US`), dialogs (`AK/KK/GS`), kills (`MK`) flow **host → clients**; chat and pings are symmetric; positions/state blocks flow both ways per-player (each client owns its own units' contributions — consistent with BZ-series co-sim heritage).
- Player identity: **mini-id = u16**, assigned from `m_nextAvailPlayerId` (host-side `CSteamLobby+0x34`), carried in `playerSettings.miniId` and registry maps `miniId→NetPlayer`, `CSteamID→NetPlayer` (PROVEN-native). All game-plane addressing is by mini-id (`param_3` throughout the dispatcher); SteamID↔mini-id binding happens at handshake/identity stage.
- Object ID assignment: object ordinals/subtype words as analyzed in the March descriptor corpus; assignment function not pinned this pass (UNKNOWN).
- Controlling-client departure: membership loss handled at matchmaking plane (ownership transfer, 2026-08-24 §13); match-plane residue handled by last-seen/auto-kick timers and the dialog-open trio (INFERRED from handler wiring; no dedicated "take over my objects" packet observed).

---

## 11. Relay / NAT

No changes to conclusions; strengthened rationale:

- Signaling context fully mapped (2026-08-24 §10): `/iprelay` → state 5 → `DoP2PRoute` → `OnP2PRoute{player,port:1339}` → state 6. Live-probe payload contains **no token/nonce/address** ⇒ endpoint-based association is the only consistent model.
- New: exhaustive review of the recovered dispatcher shows **no peer-plane packet type embeds a third-party address** (chat bodies, state blobs, descriptors contain object/unit strings only). Therefore the relay does not need to inspect game payloads — transport-transparent forwarding remains correct *by construction*, provided association stays at the WS/endpoint layer.
- Remaining 1339 UNKNOWNs unchanged: allocation envelope (if any beyond signaling), keepalive, timeout, cross-NAT forwarding proof (needs the scripted Capture A run).

---

## 12. Disconnect / recovery

- Normal exit: matchmaking teardown (`DoExitLobby`, `OnLobbyRemoved`, owner transfer) — CONFIRMED 2026-08-24; match plane adds `RG`(?)/dialog trio as UX path (partially INFERRED).
- Timeout: AutoKick* thresholds + last-seen stamps (PROVEN-native vars; numeric defaults not extracted this pass — see §14).
- Abrupt RST: server-side cleanup verified by soak suite (zero ghosts, convergence invariant) — CONFIRMED 2026-08-24 §13/§18.
- Reconnect: fresh session, honored explicit ids, unique-id assignment fixes — CONFIRMED 2026-08-24.
- Migration: `NotifyNowHost()` exists (PROVEN-symbol); no host-migration packet family observed in any capture; treat formal migration as UNKNOWN/not exercised.

---

## 13. Dedicated-server coverage

Classification per protocol family (standalone `server.py`):

| Family | Status | Notes |
|---|---|---|
| WS matchmaking verbs/events | SUPPORTED | 42-record contract; deliberate supersets documented |
| Reason codes | PARTIAL | 0/4/5 CONFIRMED; 1,2,3,6,7 provisional |
| UDP 1338 discovery | SUPPORTED | unconditional echo+trailer rule; regression-tested |
| BZRNet transport framing | PASS-THROUGH (by design) | relay forwards opaquely; framing now fully specified if ever needed |
| Reliability | PASS-THROUGH | clients own it; server must not interfere (COMMUNITY_FAILOVER_PLAN constraint holds) |
| Game plane (§5 table) | PASS-THROUGH / diagnostics-ready | taxonomy + decoder now available for tooling; no server behavior requires decoding |
| UDP 1339 relay | HARDCODED / EMULATED | endpoint-pair association; allocation envelope UNKNOWN |
| Kick/lock/owner moderation | EMULATED (documented supersets) | functional superset of silent-upstream behavior |
| Steam ticket validation | MISSING (deliberate failover mode) | upstream enforces (4002 path); ours accepts |
| Host migration | UNKNOWN | not observed anywhere |

Prioritized interoperability backlog (re-ranked; P0 items unchanged in substance from 2026-08-24 §21):

- **P0 — stock-client interop confidence:** run the **Stock Client Interoperability & Fault-Injection Matrix** ([`BZRNET_STOCK_CLIENT_INTEROP_MATRIX_20260825.md`](BZRNET_STOCK_CLIENT_INTEROP_MATRIX_20260825.md)) — real GOG + Steam clients vs community server via hosts redirect, the 11-cell transition matrix plus controlled reliable-fragment impairment. This is now the highest-leverage conversion of protocol knowledge into an implementation go/no-go decision.
- **P0 — official failure codes:** execute Capture B scenarios (bad-password first) to replace provisional reasonCodes.
- **P1 — relay completeness:** Capture A paired `/iprelay` run; feed results into `udp_1339`; then feature-flagged allocation-aware relay.
- **P1 — observability adoption:** integrate `research/bzrnet_packet_decoder.py` into `/captures` summaries and the compat suite so future captures classify under the *proven* taxonomy instead of heuristics.
- **P2 — contract hygiene:** regenerate `protocol_contract.json` udp_1338 block to drop the superseded timestamp/seq/ack *names* for the probe interior (keep only flags/kind/ts-as-opaque?/seq-fields distinction actually proven — note the ±60 s skew validation IS proven, so ts may stay named); reconcile README "real route association" wording.
- **P2 — auto-kick defaults:** extract `AutoKickTime/Loss/Ping` numeric defaults from the config-registration function `0x56FD20` for parity testing of timeout behavior.
- **P3 — EMPState name dump, kind-nibble writer site, `PK/ns/RG/la/lu/PT` semantic completion, PZ marker hunt in fresh captures.

---

## 14. Unknown fields/messages (explicit register)

> **Update 2026-10-06:** [`BZRNET_CLIENT_DECOMP_VALIDATION_20261006.md`](BZRNET_CLIENT_DECOMP_VALIDATION_20261006.md) resolves the 1339 envelope (there is none), the `seq_b` role (cumulative ack), the kind values (6 = resend request, 7 = ack), `EMPState` values, auto-kick defaults, and `PK`/`ns`/`RG`/`la`/`lu`/`PT`. It also documents a relay limitation for clients with two or more relayed peers.


| Item | Status | How to resolve |
|---|---|---|
| UDP 1339 allocation/association envelope, keepalive, timeout | UNKNOWN | scripted Capture A (tooling ready) |
| Official reasonCodes 1,2,3,6,7 | PROVISIONAL | Capture B scenarios |
| `seq_b` (+0x84) precise role | CORRELATED/INFERRED | one controlled capture correlating both counters during bidirectional reliable traffic |
| Kind-nibble writer site + high-nibble meaning | UNKNOWN | targeted xref of buffer-acquire callers setting byte 1 |
| `EMPState` enum values/names | UNKNOWN (advisory address invalid) | decompile `GetStateName`/`SetState` switch |
| `PK`, `NS`, `RG`, `LA`, `LU`, `PT` full semantics | handler-known, purpose INFERRED/UNKNOWN | smallest-scenario captures (one action at a time) |
| `PZ` marker seen once in March captures | UNKNOWN | fresh capture grep |
| Object-ID assignment function; migration behavior; auto-kick default numbers | UNKNOWN | targeted decomp (`TempStateSendAll` internals; `dwAutoKick*` init) |
| One extra byte in IPv6 discovery-reply layout | UNKNOWN | cosmetic; no interop impact |
| Whether `OnHeartbeat`/`Ping` matter to stock clients | LOW (unchanged) | existing drift harness |

Contradiction resolved this pass: the 2026-03-18 doc's "sequence at offset 13 u32le" claim is formally superseded (wrong build-era guess; native parser proves offsets 10/14 BE). The contract's udp_1338 block retains older *field names* for bytes 2..17 whose *layout* (ts/seq/ack) is now separately proven for the timestamp and structurally supported for the two u32 slots — see P2 hygiene item.

## 15. Recommended next implementation work

1. **No server-code changes are warranted by this pass** — every new finding either (a) confirms current behavior, (b) concerns planes the server passes through opaquely, or (c) adds diagnostics capability. The implementation gate stays closed.
2. Adopt the decoder in tooling (P1 above): classify `peer_control_marker` families by their proven tags in future capture reports; retire heuristic labels.
3. Regenerate the contract with the udp_1338 wording fix (P2) in its own commit with before/after matrix render.
4. Schedule the three missing live captures (matrix, B-scenarios, A-relay) — they remain the only path to upgrading the remaining PROVISIONAL/UNKNOWN rows.
5. Preserve this report's VA references alongside `research/decomp_evidence_20260825/` so future builds can re-validate by diffing prologues at the cited sites.

---

## Appendix A — Phase mapping (work-order traceability)

Phase 1 inventory → §1, §13 tables + prior reports; Phase 2 receive path → §3–§5 (`0x570500` dispatcher chain, `0x6C6E60` parse); Phase 3 send path → §4.1, §8 (`FUN_0075BEB0`/`FUN_0075AAF0`, primitive writers); Phase 4 capture matrix → existing sanitized corpora reused (March 21 pair + 2026-08-2x probes); no new hostile traffic generated; Phase 5 taxonomy → §5 + CSV; Phase 6 state machine → §7; Phase 7 reliability → §8; Phase 8 authority/replication → §9–§10; Phase 9 relay audit → §11; Phase 10 gap analysis → §13.

## Appendix B — Key native anchors (GOG 2.2.301 VAs)

| Anchor | Address |
|---|---|
| WS dispatcher / builders | `0x6BF2A0` / `0x6C3D50..0x6C6E60` (table in 2026-07-16 doc) |
| Discovery listener / probe handler | `0x6C83F0` / `0x6C6E60` |
| P2P socket open | `0x6BE8C5` |
| Send: TRY(handshake) / REL+UNR | `0x75AAF0` / `0x75BEB0` (vtable `0x89BE78..`) |
| Header writers / UDP send | `0x760670`, `0x7606D0`, `0x760730`, `0x760590` |
| Expected-seq getter / resets | `0x75F550` / `0x75CA90`, `0x75D090` |
| Connect lifecycle | `0x75EEA0` (HandleP2PConnect), `0x75D800`, `0x75F370` |
| Game dispatcher (vtable `0x883B5C`) | `0x570500` |
| Shared game send primitive | `0x7596B0` (≤1153 B) |
| Ping family | send `0x576480`, reply-handler `0x571E20`, history `0x577EB0` |
| Spawn point | send `0x576390`, recv `0x571C70` (**NetState 3→4**) |
| Sync-step / activity-ack | `0x577360` / `0x5773F0` |
| State broadcast | `0x4B9DA0` (+frame pump `0x618130`) |
| Bandwidth governor | `0x5732D0` (+config registration `0x56FD20`) |
| Team set / auto-kick | `0x577900` / `0x576B70` |
| Dialog-open one-shot | `0x5D48B0` → `0x5D4690(0xB)` |
