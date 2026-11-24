# BZRNet Multiplayer Server Guide

How Redux multiplayer works and what it takes to run a compatible server.
Consolidates the capture evidence (`PROTOCOL_CONTRACT_MATRIX.md`,
`research/notes/BZRNET_TRANSPORT_HEADER_20260812.md`, live captures through 2026-08-25),
the OpenShim instrumentation notes, and the behavior implemented in
`server.py`. Confidence is stated per section; unknowns are marked as such.

## 1. Architecture

Redux multiplayer is not DirectPlay. The 1.5 engine still ships a
`dp_s*`/`dp_session_t*` core, but Redux replaced it with BZRNet, split
across a control plane and a data plane:

- **Lobby/control**: JSON over unencrypted WebSocket to a central server.
- **Endpoint discovery**: short UDP probe service.
- **Gameplay**: direct peer-to-peer UDP between clients after a
  server-mediated introduction. No simulation payload crosses the server on
  a working direct path (same-LAN capture: 10,561 UDP packets with zero
  game payload traversing the service).
- **Fallback**: UDP relay when direct P2P fails (envelope unrecovered;
  see §8).

A replacement server never needs to decode simulation datagrams
(`Net`/`NetPlayer` payloads stay opaque peer-to-peer).

## 2. Ports and protocols

| Port | Transport | Purpose | Implementation status |
|---|---|---|---|
| TCP 1337 | HTTP Upgrade to WebSocket, unencrypted JSON text frames (masked client frames, fragmented + 16/64-bit lengths) | All lobby/matchmaking control | Implemented (`server.py`) |
| UDP 1338 | Custom 18-byte header + short reply | Public-endpoint discovery probe | Implemented |
| UDP 1339 | Unknown envelope, forwarded byte-for-byte | Relay fallback | Opaque forward only; envelope unknown |
| Dynamic UDP | Same 18-byte header + sim payload | Direct peer candidates + gameplay | Header documented; payload opaque by design |
| TCP 8080 | HTTP JSON (community-only) | `/health`, `/status`, `/protocol`, `/captures` | Implemented |

Defaults live in `server.py:38-44` (`DEFAULT_WS_PORT`,
`DEFAULT_UDP_PORT`, `DEFAULT_RELAY_PORT`, relay route TTL 180 s,
`BZR_TRANSPORT_HEADER_BYTES=18`) and are env/docker-overridable
(`BZR_WS_PORT`, `BZR_UDP_PORT`, `BZR_RELAY_PORT`, `BZR_HEALTH_PORT`).

## 3. Official service endpoints

- Host: `battlezone98mp.webdev.rebellion.co.uk` (observed `68.183.35.188`).
- Hardcoded in the client binary: `ws://battlezone98mp.webdev.rebellion.co.uk:1337/`.
- Probe target: same host, UDP `1338`.

## 4. Connection flow

1. Client opens the WebSocket (`BZRNetInterface starting connection` …
   `Websocket established!`) and probes UDP 1338.
2. **`Authorization` must be the first message or stock aborts.**
   `{authtype: steam/gog/web, steamAppTicket?, wanAddress, lanAddresses,
   id, …}` → `OnAuthorization{success,id,isAdmin,isBB}` +
   `OnWhitelistUpdated{list, major:8, minor:0}`. Auth edge rules (§6)
   apply here.
3. `DoEnterLounge → OnLobbyListChanged`. The list already contains the
   persistent `~chat~pub~~default` lobby plus a bridge user with
   stock-derived `userCount`/`userPack` — the client **joins** it and hangs
   on "gathering information" without this seed. `server.py` seeds it at
   startup.
4. Browse/create: `GetLobbyList`, `CreateLobby`/`CreateGame` →
   `OnLobbyCreated`, `DoJoinLobby{id,password}` → `OnLobbyJoined`
   (`reasonCode` 0 success, 4 missing, 5 already-in; others provisional) /
   `OnFailure`, `DoExitLobby` → `OnLobbyRemoved`.
5. Ready-up: `SetPlayerData(metadata)` → `OnUserDataChanged` +
   `OnLobbyChanged` (per-player game state such as `miniid`, `team`,
   `ready`, `launched` rides opaque inside `user.metadata` — preserve,
   do not parse). Host: `SetLobbyData`/`DeleteLobbyData` →
   `OnLobbyDataChanged`, `SetLobbyMemberLimit(int)`, `LockLobby(bool)`,
   `DoSetLobbyOwner`, `DoKickUser` (owner only), `DoSendChat(string)` →
   `OnChatMessage`.
6. Launch: `DoP2PConnect{player:TARGET,reset}` is routed to the target as
   `OnLobbyMemberP2PConnect{member:REQUESTER,…connect:true}` (self-target
   echoed to bootstrap the local peer). Peers then run the `0x03 → 0x04 →
   0x05` handshake against advertised `wanAddress`/`lanAddresses` and go
   direct. On direct failure: `DoP2PRoute` → `OnP2PRoute{player,port:1339}`
   (no allocation token exists in signaling; association is endpoint-based).

## 5. Message catalog (`server.py` coverage)

Client→server: `Authorization`, `CreateGame`, `CreateLobby`,
`DeleteLobbyData`, `DoEnterLounge`, `DoExitLobby`, `DoExitLounge` (silent),
`DoJoinLobby`, `DoKickUser`, `DoPing`, `DoP2PConnect`, `DoP2PRoute`,
`DoSendChat`, `DoSetLobbyOwner`, `DoUpdateLAN`, `DoUpdateWAN`,
`GetLobbyList`, `LockLobby`, `Ping`, `SetLobbyData`,
`SetLobbyMemberLimit`, `SetPlayerData`, `SetRelay`, `SetTest`, plus
community-tooling verbs (`GetUserList`, log-level controls, regex/trace
admin verbs). Server→client: `OnAuthorization`, `OnWhitelistUpdated`,
`OnLobbyListChanged`/`OnLobbyList`, `OnLobbyCreated/Joined/Changed/Removed`,
`OnLobbyMemberListChanged`, `OnLobbyDataChanged`, `OnUserDataChanged`,
`OnChatMessage`, `OnLobbyMemberP2PConnect`, `OnP2PRoute`,
`OnWANUpdated`/`OnLANUpdated`, `OnHeartbeat`, `OnRelayUpdated`,
`OnEnableRelayChanged`, `OnFailure`. Full request/response shapes live in
`PROTOCOL_CONTRACT_MATRIX.md` (message records section).

## 6. Authorization edge behavior (upstream parity, 2026-08-25)

Implemented in `handle_authorization` / `route_message` / `handler`,
covered by `tests/protocol_parity_test.py` E4–E8:

- Pre-auth verbs, unknown verbs, and binary-adjacent noise before
  `Authorization` produce **zero** frames (previously the server replied
  `"Authorization required"`; removed).
- A second `Authorization` on a bound socket is **silently ignored**;
  identity is never rebound.
- Missing `authtype` closes the socket (**4002**) with no message frame.
- `steam` without a ticket sends `OnAuthorization{success:false,
  id:<requested>}` (shape-only assertion; no reason string is evidenced)
  and then closes **4002**. Steam *with* a ticket is accepted and the
  client-supplied `S…` id is kept; tickets are not validated (development
  posture — Valve-side validation does not exist here).
- `gog` without a ticket is **accepted** and the client-supplied id is
  echoed back verbatim (no generated replacement). Other `authtype`
  values are accepted leniently; that leniency is a community choice,
  not upstream evidence.
- Binary frames are rejected with the upstream wording `Binary Payload
  not supported` over the existing error envelope.

Community hardening layered on top (not upstream evidence; stock-visible
shapes unchanged): the literal ticket `withheld` is admitted like any
non-empty ticket and flagged operator-side only; names are sanitized;
`clientVersion`, chat rate, bans/mutes (optionally timed) and admin-key
elevation are operator-policed and audited. See README "Admin and moderation
hardening" for the options.

## 7. UDP details

18-byte big-endian header (`research/notes/BZRNET_TRANSPORT_HEADER_20260812.md`): flags
`0x40` final / `0x80` reliable / `0xC0` both; kind `0` gameplay/keepalive,
`2` discovery, `3` offer/probe, `4` accept, `5` confirmation, `6`
endpoint-update, `7` ack; unix-ms timestamp; send-seq; ack-seq.
Probe cadence ~30 s, staleness cut ~60 s; an empty datagram earns a
trailer-only 7-byte reply upstream. `research/bzrnet_packet_decoder.py`
classifies discovery traffic (`DISCOVERY_KIND=0x2`).

## 8. Running your own server

1. `pip install -r requirements.txt`, then `python server.py`
   (Docker: `docker-compose up`; health at `:8080/health`).
   Shared relay is the compatibility default. For three or more relayed players, enable
   `--relay-pair-ports --relay-pair-port-start 1340 --relay-pair-port-end 1369` (or select pair mode
   and these bounds in the GUI). Allow/forward the whole configured UDP range. Docker users can
   merge `docker-compose.pair-ports.yml` with the base file. A fully relayed `n`-player lobby needs
   `n*(n-1)/2` ports; the 30-port example is shared across all active lobbies. Ephemeral `0`/`0`
   bounds remain available for private loopback tests. See `README.md` for failure/capacity details.
2. Validate without the game: `python tests/smoke_test.py --host 127.0.0.1`
   (WS/UDP/decoder/HTTP/protocol/capture checks),
   `python tests/protocol_parity_test.py` (edge parity E1–E8),
   `python tests/stock_client_compat_test.py` (45-check stock profile).
3. Validate with a real client: `Battlezone_LobbyMonitor` against
   `127.0.0.1:1337`, or the game with `net.ini [OpenShimSocket]
   MatchmakingRedirectAddress=<host>` (hostname/IP only, stock ports
   kept), or the stock launch flag `/bzrserver=ws://<host>:1337/` (full URL;
   verified 2026-10-05 on the GOG exe, see docs/LAN_COOP.md).
4. Operate: never commit live `data/server_state.json`, `.env`, keys, or
   raw captures (see `AGENTS.md`); use RFC 5737/3849 blocks in fixtures.

## 9. Known gaps (not implemented — evidence missing)

- UDP 1339 envelope internals (allocation, keepalive, timeout, per-datagram
  prefix, cross-NAT behavior): needs a two-network forced-relay
  (`/iprelay`) capture. Current relay is opaque endpoint-pair forwarding.
- Official `reasonCode` enum beyond 0/4/5; whitelist/admin policy;
  security review (basic abuse limits exist; see README).
- Production Steam-ticket validation.

## 10. Tooling map

- `server.py` — the server. `tools/probe.py` — UDP probe client/fake-server
  for RE work. `tests/smoke_test.py`, `tests/protocol_parity_test.py`,
  `tests/stock_client_compat_test.py`, `tests/relay_faultinject_test.py`,
  `tests/soak_test.py`, `tools/oracle_drift_check.py` — validation. `build/run/render
  _protocol_contract.py` + `tools/protocol_contract.json` — generated contract.
  `tools/run_capture_consensus_workflow.py`, `tools/compare_capture_consensus.py` —
  capture workflows. OpenShim side: `bzrnet_capture_windows.ps1`
  (Start/Mark/Stop, `-RelayCapture`, `-PrivateForensic`, `-AllUdp`) and
  the `analyze/align/compare/validate/correlate/session_report` scripts.
