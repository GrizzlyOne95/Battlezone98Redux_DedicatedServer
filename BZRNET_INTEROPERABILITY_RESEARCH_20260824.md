# BZRNet Interoperability Research Report

**Date:** 2026-08-24
**Branch:** `agent/bzrnet-interoperability-research`
**Scope:** Cross-repository investigation of the Battlezone 98 Redux multiplayer ecosystem and an executable interoperability/regression framework for the community BZRNet server.

---

## 1. Executive summary

This work order produced a live-verified, evidence-ranked understanding of the current Rebellion BZRNet service and turned that understanding into tooling the community server can be continuously validated against.

Headline results:

1. **The official service was re-verified live on 2026-08-24** (WebSocket TCP 1337 + UDP 1338 at `battlezone98mp.webdev.rebellion.co.uk` → `68.183.35.188`). The admin page and `scripts/bzadmin.js` are still served unchanged in shape.
2. **A second authorized live-probe round (2026-08-25)** using two self-owned synthetic sessions inside own-created lobbies confirmed a second official reasonCode (5 = already-in), captured the exact `OnP2PRoute` allocation payload, proved upstream does not enforce lobby passwords on the tested path, and showed owner `DoKickUser` silently no-ops upstream.
3. **Several prior protocol beliefs were corrected with hard evidence:**
   - Stock `DoEnterLounge` sends **boolean `true`** (raw pcap), not a numeric payload.
   - `OnLobbyJoined` success acks are **flat**; nested lobby/user structures are *not* sent upstream.
   - Official `DoJoinLobby` requires exactly `{"id": <number>, "password": <string>}`; anything else yields `OnFailure "DoJoinLobby failure bad content data type"`.
   - `reasonCode` **4 = "Join Lobby Failed, Lobby no longer exists"** — the first officially confirmed failure code.
   - `OnLobbyMemberListChanged.member` carries the affected user's **server-assigned ID**, not a display name.
   - UDP 1338 replies to **every** datagram (even empty ones) with echo + 7-byte endpoint trailer.
4. **The community server was brought measurably closer to stock parity** (join ordering, member semantics, reason codes, id assignment, seeded default chat lobby, UDP trailer rule).
5. **An auditable protocol contract** (`protocol_contract.json`, 42 records), a rendered disagreement matrix, a drift harness against sanitized live baselines, a 42-check multi-profile interoperability suite, and a convergence-asserting soak harness were added.
6. **UDP 1339 relay remains the one large unknown**; this work narrowed its context (stock state machine, `/iprelay`, `OnP2PRoute` consumer) but did not recover the allocation envelope.

---

## 2. Architecture as now understood

```
                    ┌────────────────────────────────────────────┐
                    │        Rebellion BZRNet service            │
                    │  battlezone98mp.webdev.rebellion.co.uk     │
                    │  = 68.183.35.188 (verified 2026-08-24)     │
                    ├──────────────┬──────────────┬──────────────┤
                    │ TCP 1337     │ UDP 1338     │ UDP 1339     │
                    │ WebSocket    │ endpoint     │ relay / NAT  │
                    │ JSON lobby/  │ discovery    │ fallback     │
                    │ matchmaking  │ probe        │ (envelope    │
                    │ + admin      │              │  UNKNOWN)    │
                    └──────┬───────┴──────┬───────┴──────┬───────┘
                           │              │              │
             ┌─────────────┘       ┌──────┘              └──────┐
             ▼                     ▼                            ▼
      stock Redux client     direct P2P game traffic      relayed game traffic
      (Steam/GOG/Web)        (legacy Net/NetPlayer        (same payload inside an
                             datagrams behind the         un-recovered envelope;
                             confirmed 18-byte header)    forwarded opaquely by us)
```

- **Coordination plane** (TCP 1337): text-JSON envelopes `{type, content}` client→server and `{type, data}` server→client. The dispatcher hard-requires `type` first; binary frames are rejected upstream.
- **Discovery plane** (UDP 1338): any datagram → verbatim echo + `0x04 + observed IPv4 + observed port (BE u16)`. The client feeds the discovered WAN endpoint into `Authorization` / periodic `DoUpdateWAN`.
- **Match plane**: peers connect **directly** using exchanged WAN/LAN metadata (normal path). UDP 1339 exists for relay fallback (`/iprelay` forces it); its allocation/framing envelope has never been captured.
- The original ANet-era design ("central matchmaking, direct play, optional assist") survives intact under a WebSocket skin.

## 3. Evidence sources

| Source | What it provided | Trust |
|---|---|---|
| Live official service (read-only oracle sessions, 2026-08-24) | Envelope shapes, auth flow, join/exit flows, failure wording, silence behavior, 1338 edge cases | CONFIRMED (as of date) |
| Live official service (authorized self-contained probes, 2026-08-25) | reasonCode 5 + evaluation order, exact OnP2PRoute payload, password non-enforcement, kick no-op, 1339 same-NAT silence | CONFIRMED (as of date) |
| Raw paired pcaps `game1.pcapng` / `Kat_PC.pcapng` (SHA-256 verified against recorded hashes) | Real stock-client send shapes and event ordering for a full host+join+launch session | CONFIRMED |
| Static decomp of GOG 2.2.301 (`BZRNET_WS_SCHEMA_BINARY_CONFIRMED_20260716.md`, OpenShim corpus) | Builder addresses, exact JSON keys incl. non-literal key `id` at `DAT_00897b9c`, setter types (number vs string vs bool) | CONFIRMED |
| Official web assets `scripts/bzadmin.js`, `bzdatagrids.js` (fetched 2026-08-24) | Complete admin-client contract | CONFIRMED |
| `Battlezone_LobbyMonitor` (`bzr_monitor.py`) | Independent client implementation; divergences catalogued | HIGH |
| `BZ1-GameWatcher` production parser + fixture corpus | Normalization semantics, privacy boundary, authType authority, gameSettings tuple | CONFIRMED (corpus-tested) |
| OpenShim RE artifacts (decomp, strings, capture tooling) | Engine-side P2P state machine, `/iprelay` global `0x00946708`, route states, PP/PR markers, 18-byte header | CONFIRMED unless noted |
| Community `server.py` history | Prior provisional assumptions now re-derived or corrected | varies |

Privacy: raw captures, tickets, addresses, and stable identifiers were used **only** locally; every committed artifact is synthetic or structurally sanitized (documentation ranges only).

## 4. Complete WS message matrix

The authoritative machine-readable version is [`protocol_contract.json`](protocol_contract.json); the human-readable disagreement table is [`PROTOCOL_CONTRACT_MATRIX.md`](PROTOCOL_CONTRACT_MATRIX.md). Summary of the most decision-relevant rows:

| Message | Direction | Stock (pcap/decomp) | Official web JS | Official live 2026-08-24 | LobbyMonitor | GameWatcher | Community server | Confidence |
|---|---|---|---|---|---|---|---|---|
| Authorization | C→S | `{authtype:'steam', apiVer:'3.6', clientVersion:'2.2.301', id:'S<steam64>', inLounge:false, lanAddresses[], name, realname, steamAppTicket, wanAddress}` | `{authtype:'web', key, id:'0', apiVer:'0.0'}` | accepted; assigns `B100xxxx` for web | `{...web..., name, playerName, clientVersion}` | same as admin JS + SetPlayerData identity | accepts all shapes; assigns U-id when id missing/"0" | CONFIRMED |
| DoEnterLounge | C→S | `true` (bool, pcap) | `true` | accepted; reply = list only | `true` | `true` | bool-coerced; extra `OnDoEnterLounge` ack (deliberate superset) | CONFIRMED |
| CreateGame | C→S | `{memberLimit,isPrivate,password}` only | n/a (admin uses CreateLobby) | not exercised (policy) | never sends (chat-only tool) | n/a | wraps CreateLobby + defaults | CONFIRMED |
| DoJoinLobby | C→S | `{"id":<int>,"password":<str>}`; joins default chat first | bare numeric id (**broken upstream**) | object w/o password or scalar ⇒ OnFailure | `{"id":int-or-str,"password":""}` | `{"id":int,"password":""}` | liberal input; official wording for malformed content | CONFIRMED |
| DoExitLobby | C→S | bare int (pcap) | bare int | accepted; no explicit ack | int | n/a | int/object both accepted; emits ack superset | CONFIRMED |
| SetLobbyData | C→S | `{key,value}` incl. `name:"~game~pub~*~..."` | `{key:'name',value}` | reflected via OnLobbyDataChanged | seeds 5 keys | reads side | owner-gated | CONFIRMED |
| OnLobbyJoined | S→C | flat `{type,time,success,id,reason:'',reasonCode:0}` | handled upstream-style | flat confirmed | reads flat success/id/reason | passes through | flat + optional supersets tolerated | CONFIRMED |
| OnFailure | S→C | handled (reasonCode reader) | not read | `{type,time,success:false,reason}` — **no reasonCode** | ignored | ignored | emitted for malformed joins | CONFIRMED |
| OnLobbyChanged | S→C broadcast | primary membership signal; redundant multi-broadcasts | merge-single | 2–4 copies per change observed | merges | add/update | one snapshot per change (+full list) | CONFIRMED |
| OnLobbyMemberListChanged | S→C | `member`=userId, `lobbyId`, `removed` | not read | confirmed | treats member as name (tolerated divergence) | bot hooks | member=userId since this branch | CONFIRMED |
| OnChatMessage | S→C | `{text,lobbyId,speakerId,time,success}` (no author) | author/text/color/time | same minimal shape | author‖speakerId | speakerId/text/time | author+speakerId+text+lobbyId+time | CONFIRMED |
| Ping / DoPing | C→S | not observed in captures | n/a | **silently ignored** for non-admin web | manual button | n/a | answers OnHeartbeat (deliberate superset) | MEDIUM |
| GetUserList/GetStatLog/etc. | C→S admin | n/a | sent by page | silently ignored for non-admin | ignored | n/a | answered to everyone (documented divergence) | HIGH |
| OnServerShutdown | S→C | handler present | n/a | not observed | ignored | n/a | not emitted | LOW |

Reason-code registry:

| Code | Meaning | Status |
|---|---|---|
| 0 | success | CONFIRMED (live + pcap) |
| 4 | `"Join Lobby Failed, Lobby no longer exists"` | CONFIRMED (live 2026-08-24) |
| 5 | `"Join Lobby Failed, Trying to join a lobby you're already in"` (evaluated BEFORE lock/full) | CONFIRMED (live 2026-08-25) |
| 1,2 | missing id / invalid id | PROVISIONAL |
| 3 | bad password | PROVISIONAL — upstream did NOT enforce passwords via CreateGame/SetLobbyData (wrong-password joins succeeded twice, 2026-08-25); community enforcement is a conservative superset |
| 6,7 | locked / full | PROVISIONAL |

## 5. Official admin-client findings (live assets, 2026-08-24)

From `scripts/bzadmin.js` + `bzdatagrids.js` served by the production host:

- Sends exactly: `SetLogLevel{level}`, `SetTest{toggle}`, `SetRelay{toggle}`, `AddRegex{regex}`, `RemoveRegex{regex}`, `CreateLobby{name,memberLimit:20000}`, `LockLobby:true/false`, `DoJoinLobby:<numeric>` (broken upstream today), `DoSetLobbyOwner:<id>`, `SetLobbyData{name,value}`, `SetLobbyMemberLimit:<int>`, `DoSendChat:<str>`, `GetStatLog:true`, `TraceData` (no content), plus the shared `Authorization`.
- Reads: `OnAuthorization.data.isAdmin/.isBB`; `OnGetLogLevel.data.level[1]` (**array index 1**); `OnWhitelistUpdated.data.whitelist.{list,major,minor}`; `OnRelayUpdated/OnEnableRelayChanged.data.enabled`; `OnGetUserList.data.users`; `OnGetFullLog.data.stdout.history[]`; `OnStdoutLogEntry.data.txt`; `OnGetStatLog.data.stats` (multiline string, first 200 lines); `OnLobbyRemoved.data.id`.
- Admin grid fields — lobbies: `id, owner(-1 renders "none"), isLocked, isChat, isPrivate, password(!), memberLimit, users(names), userCount, metadata`; users: `id, name, authType, clientVersion, ipAddress, lobby, isAdmin, isInLounge, metadata, wanAddress, lanAddresses`. Note the upstream lobby snapshot includes a `password` field readable by the grid; our serializer matches.
- The README's admin message list was verified **still complete**; nothing new appeared in the live JS.

## 6. Stock executable findings

Synthesis of decomp + raw paired captures (GOG 2.2.301; Steam build treated as matched after settle per OpenShim policy):

- Outbound builders are thin JSON wrappers over `content`; key evidence: `DoJoinLobby` builds `content` with key `id` from `DAT_00897b9c` (resolved to `"id"` in-binary) set via the **number** setter `FUN_006dc0f0`, then `password` via string setter — matching live frames byte-for-byte in shape.
- Client-side dispatch gate: `OnAuthorization must be first message`; unknown inbound verbs log harmlessly; `data.type` duplication ignored.
- Full observed stock sequence (host): `Authorization → OnAuthorization+OnWhitelistUpdated → DoEnterLounge(true) → OnLobbyListChanged → DoJoinLobby(default chat) → [OnLobbyChanged, OnLobbyMemberListChanged, OnLobbyJoined] → SetPlayerData×n → DoExitLobby(chat) → CreateGame(minimal) → OnLobbyCreated → SetLobbyData(name/gameSettings/...) ×n each acked by OnLobbyChanged+OnLobbyDataChanged → DoP2PConnect(peer)` … later `LockLobby(true/false)`, `SetLobbyMemberLimit(n)`, exit → `OnLobbyRemoved + OnLobbyListChanged`.
- Joiner mirrors: lounge → join default chat → exit → join game lobby → identity SetPlayerData → DoP2PConnect.
- The engine keeps probing 1338 (~10 s, then ~30 s cadence) and refreshes WAN via `DoUpdateWAN`.
- In-match peer transport: the **18-byte header boundary is CONFIRMED** and the sequence field at **bytes 16..17 is a big-endian u16, CONFIRMED** by PR #1's independent instrumentation (`BZRNET_TRANSPORT_HEADER_20260812.md`, `server._decode_bzr_transport_header`). Byte 0 behaves as a flags byte (`0x40` unreliable / `0xC0` reliable) and byte 1 as a kind nibble carrier in captures; **bytes 2..15 remain deliberately OPAQUE** - the earlier March-capture interpretation of those bytes as an 8-byte BE unix-ms timestamp plus separate u32 seq/ack fields was NOT independently proven for the peer transport and is superseded by PR #1's conservative hierarchy. Gameplay payloads start at byte 18 and are legacy `Net`/`NetPlayer` bytes; they stay opaque for our purposes.
- The UDP **1338 discovery probe** is a distinct protocol surface: an 18-byte packet beginning `40 02 ...` whose internal field meanings are likewise not independently proven (the relay/probe interop only requires the echo+trailer rule below). Do not transfer semantics between the two surfaces.
- Route selection state machine (decomp): LAN/WAN attempts with retry budgets; forced-relay global `0x00946708` (`/iprelay`) jumps to state 5 → `DoP2PRoute`; `OnP2PRoute` success stores an endpoint pair and enters state 6 ("Starting RELAY Connect"); failures log `FAILED WAN Connect ... ATTEMPTING RELAY` / `FAILED RELAY Connect`.

## 7. LobbyMonitor findings (independent implementation)

`bzr_monitor.py` is a read-mostly Web-auth client. Notable properties and divergences:

- Sends `Authorization{id:"0"...}` (pre-fix servers gave it literal userId "0"), `DoEnterLounge(true)`, `GetLobbyList(true)`, chat-lobby `CreateLobby{name:"~chat~pub~~X",isPrivate:false,memberLimit:20000,password:""}`, `DoJoinLobby{"id","password":""}`, scalar `DoExitLobby`, string `DoSendChat` (`/t` whisper), `Ping`+`DoPing`, and five `SetPlayerData`/`SetLobbyData` seeding calls.
- Never sends `CreateGame`, WAN/LAN updates, Lock/limit/owner/P2P verbs.
- Handles only a subset of events; ignores whitelist/relay/log/admin events silently.
- Divergences worth tolerating (we do): treats `OnLobbyMemberListChanged.member` as display name; assumes flat `OnLobbyJoined`; no heartbeat; no reconnect backoff beyond a timer.
- No BZRNet relay/P2P code exists there despite "Discord relay" naming (that's chat bridging).

## 8. GameWatcher / corpus findings

- Production normalization lives in `BZ98ProtocolParser`: `authType` is authoritative for platform (`steam`/`gog`/`web`; identifier prefixes only enrich); the 13-field `gameSettings` tuple decode; tri-state `launched`; `gameType` as validity marker; privacy boundary (IP/WAN/LAN never serialized publicly).
- The observer pattern (`BZ98ChatObserver`) — web auth, `SetPlayerData` identity, `DoJoinLobby {"id",password:""}`, accept `data` **or** `content` envelope — was reused as the model for safe live observation here and matched upstream behavior.
- Fixture corpus rules preserved: unknown stays unknown; missing ≠ zero ≠ false; documentation-range addresses only; fixtures are executable documentation, not recordings.

## 9. UDP 1338 findings

- **Reply rule (upgraded to unconditional):** response = `echo(request) || 0x04 || observedIPv4(network order) || observedPort(BE u16)`. Verified for empty (7-byte reply!), 1-byte, 7-byte ASCII, 17-byte, kind-0x02 18-byte, and other 18-byte inputs. Community shim previously replied only to `0x40`-prefixed ≥18-byte probes and never to empties — fixed.
- Canonical request shape: 18 bytes beginning `40 02`; per PR #1's tightened evidence hierarchy the only named fields anywhere in a BZRNet UDP wrapper are byte 0 (flags), byte 1 (kind) and bytes 16..17 (u16-BE sequence). Any timestamp/seq/ack layout inside the *discovery probe* specifically remains UNPROVEN and irrelevant to interop: the server's reply rule operates on raw bytes.
- Stock cadence ~10 s then ~30 s; the client validates the echoed timestamp (~60 s skew tolerance per decomp) and emits `DoUpdateLAN`/`DoUpdateWAN`.
- Platform caveat: Windows ProactorEventLoop never delivers zero-length datagrams to Python, so the empty case is observable only on Linux/Docker deployments (documented in tests as INFO, not failure).

## 10. UDP 1339 findings

**Live allocation-signaling capture (2026-08-25, authorized self-contained probe):** with two synthetic Web sessions inside one own-created game lobby on the official service, `DoP2PRoute{player}` succeeded both ways and delivered the EXACT payload:

```json
{"type":"OnP2PRoute","time":"...","success":true,"player":"<OTHER userId>","port":1339}
```

delivered to each party with `player` set to the other's id. There is **no allocation token, route id, nonce, or host address** anywhere in the message. Combined with `FUN_0075f370` storing an endpoint pair on success, the only consistent association model is **endpoint-based learning at the relay** (the relay observes datagram source endpoints within WS-known peer context) — precisely the community implementation's `ws-route-endpoint-pair` design.

Same-NAT stress test: after route registration, two sockets sharing one public IP sent header-framed and raw datagrams to 1339 and received **no echo, relay, or acknowledgement** — consistent with ip-level ambiguity rejection rather than echo behavior. Forwarding between genuinely distinct network paths remains unverified pending the paired `/iprelay` capture.

Also established/narrowed:

- Signaling context fully mapped from decomp: `/iprelay` sets global `0x00946708`; forced-relay enters state 5 → `DoP2PRoute`; `OnP2PRoute` success stores the endpoint pair and enters state 6 ("Starting RELAY Connect"); failures log "FAILED WAN Connect ..., ATTEMPTING RELAY" / "Failed RELAY Connect".
- Relayed traffic shares the framed packet format ("BZRNet P2P REL Sent Packet (%u,%u)"), suggesting any per-datagram prefix is small around the same wrapper — hypothesis only.
- Remaining unknowns: allocation envelope (if any beyond signaling), keepalive, timeout, per-datagram prefix bytes, cross-NAT forwarding.

## 11. Direct-vs-relay decision path (current understanding)

```
HandleP2PConnect(peer):
  if force_relay (/iprelay):           -> state 5: send DoP2PRoute; wait OnP2PRoute
                                            ok -> state 6: RELAY connect to <pair>
                                            fail -> reset ("Failed RELAY Connect")
  else:
    try LAN candidates (state 1, 15 retries)
    else try WAN candidates (state 3, 30 retries)
         fail -> "FAILED WAN Connect ..., ATTEMPTING RELAY" -> relay branch above
```

Community implications: keep direct P2P normal; `OnP2PRoute.port` should eventually carry a real allocation; until the envelope is recovered, our relay stays conservative-opaque and diagnostics-first.

## 12. Steam / GOG / Web differences

- **Web** (observed live): placeholder id `"0"` replaced by server-assigned `B100xxxx`; no ticket; `isAdmin`/`isBB` gates UI/admin surface; admin queries silently ignored without admin rights.
- **Steam** (pcap): supplies `S<steam64>` id + `steamAppTicket`; server keeps the supplied id; otherwise identical messaging.
- **GOG**: not directly exercised in this environment. Corpus evidence (`authType:"GOG"`, `G…` ids in GameWatcher fixtures) shows the same post-auth surface; **no behavioral difference is claimed beyond auth material**. Parity remains untested with a live GOG client.

## 13. Disconnect / reconnect behavior

Verified against the community server by the compat suite and soak:

- Abrupt RST while in lobby: membership removed, ownership transferred to first remaining member, empty lobbies removed (`persistent` lobbies excepted), broadcasts issued. No ghost users/lobbies across a 60-client churn storm.
- Reconnect: fresh session; requested explicit ids honored; placeholder `"0"` ids now always assigned unique identities (fixes cross-tool collisions).
- Duplicate identity: suffixed uniqueness prevents takeover/collision.
- Ownerless/restored lobbies serialize `owner:-1` (matches official admin rendering).
- Restart with `--retain-empty-lobbies` (or persistent flag): metadata reloadable, stable ids, immediately joinable.

## 14. Protocol uncertainties (ranked)

1. UDP 1339 association/allocation envelope — UNKNOWN (the single biggest gap).
2. Official reasonCodes other than 0/4 — UNKNOWN (community values are provisional).
3. Exact semantics of `totalUsers` (official can emit negative — internal wrap suspected).
4. Whether `OnHeartbeat` is ever sent unsolicited upstream; what `Ping` means to the official server (silently ignored for us).
5. `~AT~` chat protocol strings and remaining `userPack` conventions.
6. Whether joining without prior `SetPlayerData` identity is what caused the observed "no longer exists" rejection for a freshly-authorized web user, or whether that lobby/state was special (single observation; flagged MEDIUM confidence in fixtures).
7. Steam-vs-GOG client deltas beyond auth material.

Resolved since the first draft of this report: reasonCode 5 semantics (confirmed), upstream password non-enforcement via the CreateGame/SetLobbyData path (observed twice), owner `DoKickUser` silently no-ops upstream across scalar/`{'id'}`/`{'player'}` content shapes (kicked client receives no event; likely gated on admin/BB status — our functional owner-kick is a documented moderation superset), `DoExitLounge` and `DeleteLobbyData`(unknown key) silence.

Protocol-edge behaviors confirmed 2026-08-25 (round 3):

| Input | Upstream behavior |
|---|---|
| Malformed JSON | `OnFailure` `"Invalid Message Data : <raw>"`, connection stays usable - mirrored by the community server now (**intentional safety divergence**: echoed `<raw>` is capped at ~1000 characters, so longer payloads are NOT byte-for-byte identical to upstream) |
| Verb before Authorization | silently ignored, socket stays open |
| Binary WS frame | silently ignored |
| Unknown verb | silently ignored |
| Second `Authorization` on one socket | silently ignored (first identity sticks) |
| `Authorization` without `authtype` | socket closed, code **4002**, no message |
| `authtype:"steam"` without ticket | `OnAuthorization success:false` then close **4002** — Steam ticket validation EXISTS upstream |
| `authtype:"gog"` without ticket | ACCEPTED (`success:true`) but the placeholder id is echoed back unassigned |
| Wrong admin key | accepted as normal user (admin surface simply not enabled) |

Notable upstream quirk: `OnChatMessage` carries no `author` field (re-confirmed live), so Rebellion's own admin page renders an undefined chat author. The community server includes `author` as a deliberate display fix.

Community divergences retained deliberately: permissive auth (no ticket validation, no 4002 closes) for failover resilience; re-auth allowed (tools rely on it); pre-auth verbs answered with an error instead of silence; chat includes `author`; functional owner-kick.

## 15. Bugs discovered

In the community server / tooling:

1. UDP 1338 did not answer short/unknown/empty datagrams per official behavior.
2. `OnLobbyMemberListChanged.member` carried a display name instead of userId.
3. Join event ordering diverged from upstream (list pushed before ack pair).
4. Provisional reasonCode numbering collided with the (now known) official slot 4; not-found wording differed.
5. Clients sending `Authorization.id="0"` (admin JS, LobbyMonitor, GameWatcher) collided on literal userId "0".
6. No persistent default chat lobby existed although every stock client joins one first.
7. Malformed `DoJoinLobby` content got a generic error instead of upstream's `OnFailure` wording; numeric admin-JS joins were rejected outright.
8. Ownerless/restored lobbies serialized `owner:""` where the official admin UI expects `-1`.
9. Lobby envelopes lacked `totalUsers`.
10. Soak harness itself: un-awaited `close_abrupt()` (harness bug, fixed) masked true behavior during early runs.

## 16. Fixes implemented (this branch)

See commits `f5164fc`, `2c7b1d1`, `be2ff94`: items 1–9 above addressed in `server.py`; contract/fixtures/matrix regenerated; compat suite extended to mirror the real stock sequence.

## 17. Tests added

- `stock_client_compat_test.py` — 42 checks across four authentic client profiles (stock exe, official bzadmin.js, LobbyMonitor, GameWatcher): profile flows, shared/passworded/chat/game lobbies, join-order assertions, member=userId, WAN/LAN updates, owner transfer/transfer-on-disconnect, unclean disconnects, reconnect/duplicate identity, lobby cleanup, restart persistence reload, malformed-input battery, unknown verbs, binary frames, UDP garbage battery, end-state convergence.
- `oracle_drift_check.py` + `fixtures/official_live_20260824.json` — structural drift reporting vs a sanitized live baseline; drift is informational and demands corroboration.
- `soak_test.py` — six churn modes, staged convergence sampling, zero-residual assertions (expecting exactly the seeded default lobby).
- `build_protocol_contract.py` / `render_protocol_contract.py` — regenerate contract + disagreement matrix.

## 18. Soak results

Representative run (Windows, local):

| Config | Result |
|---|---|
| 60 clients, 75 s, all six modes, seed 2026 | PASS — ops: 1122 reconnects, 1146 metadata updates, 1124 P2P signals, 142 lobby creates; 0 client exceptions; convergence 95→0 connections within ~2.5 s of churn stop; residual = seeded default lobby only |
| 40 clients, 40 s, five modes, seed 77 | PASS |
| 24 clients, 25 s, five modes (early harness bug run) | exposed harness `close_abrupt` await bug; after fix, clean |

Convergence invariant holds: `sessions=0, users=0, lobbies=expected(1), no worker exceptions`.

## 19. Real Redux test matrix

No Battlezone 98 Redux client was runnable in this workstation's automation context, so the launch-flag matrix was **not** executed here; it must be filled by the two-PC procedure already scripted in `BZRNET_PHASE2_CAPTURE_GUIDE_20260716.md` (flags `/netpktlog /netlog=3 /bzrnetlog=3 [/iprelay] /bzrserver=<ip> /bzrnetport=1338` + hosts/DNS override). Honest status:

| Client | auth | lobby | join | launch | direct | relay | stable |
|---|---|---|---|---|---|---|---|
| Stock GOG vs community | NOT RUN THIS SESSION (prior manual runs: pass) | ? | ? | ? | ? | ? | ? |
| Stock Steam vs community | NOT RUN | ? | ? | ? | ? | ? | ? |
| OpenShim variants vs community | NOT RUN | ? | ? | ? | ? | ? | ? |

Prior manual testing (repo history, March 21 session) exercised stock clients against the **official** service, not ours; claiming Steam/GOG parity against the community server would be false until the matrix above is run.

## 20. Remaining unknowns

- 1339 envelope/allocation/keepalive/timeout (requires one forced-relay paired capture; tooling ready).
- Official reasonCodes for locked/full/bad-password/not-found-beyond-4 (Capture B scenarios).
- Semantics behind `userPack`, `~AT~` chat strings, `totalUsers` wrap.
- Whether stock tolerates our deliberate output supersets long-term (all evidence says yes: extras are ignored by the dispatcher, and every community tool works).

## 21. Recommended next research steps

1. Execute Capture A (paired `/iprelay`) with the existing OpenShim relay-capture scripts; feed results into `udp_1339` section of the contract and implement a minimal allocation-aware relay behind a feature flag.
2. Execute Capture B scenarios (bad password first) to replace provisional reasonCodes.
3. Run the §19 matrix with real Steam + GOG clients against the community server (hosts-file redirect), ideally with GameWatcher observing as independent referee (Phase 11 of the work order).
4. Schedule monthly `oracle_drift_check.py --target official` runs from two networks; promote changes only with corroboration.

---

## Answers to the closing questions

**A. How close are we now to a protocol-compatible replacement of Rebellion's matchmaking service?**
The WebSocket surface is, on current evidence, functionally covered: every verb/event a stock client or community tool emits is implemented, and this branch removed the last known wire-shape mismatches (ordering, member semantics, reasonCode slot 4, id assignment, seeded default lobby, UDP trailer rule). Remaining risk concentrates in untested-with-real-clients areas (§19) and the provisional reasonCodes. Estimate: **high compatibility for lobby/matchmaking; parity claims require the real-client matrix**.

**B. Is direct P2P sufficiently understood?**
Yes for our purposes: address exchange, candidate use, retry states, and logging strings are decomp+capture confirmed, and gameplay payloads remain opaque by design. We do not need deeper Net/NetPlayer decoding to interoperate. **CONFIRMED-sufficient.**

**C. Is UDP 1338 sufficiently understood?**
Yes — request/response rule now covers arbitrary datagrams including empty, cadence and validation windows included, regression-tested. **CONFIRMED.**

**D. Is UDP 1339 sufficiently understood?**
No. Context and signaling are mapped, but allocation/routing framing is unrecovered. **LOW.**

**E. What exact evidence remains before calling the community service production-ready emergency failover?**
1. One successful end-to-end stock-client match hosted on the community server per platform row of §19 (auth→lobby→join→launch→stable 30 min);
2. One strict-NAT pair either connecting directly or demonstrably failing without relay (quantifying relay necessity);
3. Confirmed reasonCode map for the failure paths users actually hit;
4. A deployed public node with DNS cutover rehearsed (ops checklist already in repo);
5. If NAT-failure rate is non-trivial: the forced-relay capture and a working 1339 implementation.

**F. Could the existing Rebellion service disappear tomorrow and our implementation preserve normal multiplayer for most users?**
Probably yes for the majority topology (non-hostile NAT): clients reach the lounge, see/join/create lobbies, exchange endpoints, and play direct P2P — the path the official captures show is the norm. Users behind restrictive NAT would be at risk until 1339 works. This is a HIGH-confidence judgment for lobby functionality, MEDIUM for end-to-end matches until §19 runs happen.

**G. What classes of NAT/network topology would still be at risk?**
Symmetric-NAT pairs (no shared mapping), CGNAT-to-CGNAT without hole-punch luck, carrier-grade mobile hotspots vs residential, hard firewalls blocking unsolicited inbound to dynamic ports, and double-NAT chains where neither side can receive on the probed port. These are precisely the cases the official relay exists for; until 1339 is recovered they fall back to whatever direct traversal achieves.
