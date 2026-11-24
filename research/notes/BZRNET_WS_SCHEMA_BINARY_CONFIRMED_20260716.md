# BZRNet WebSocket Protocol — Binary-Confirmed Field Schema

Date: 2026-07-16
Source: static extraction from `battlezone98redux.exe` (GOG, client version `2.2.301`)
via the OpenShim decomp corpus (`ghidrecomp`) + capstone string-xref resolution.

This supersedes the field-level guesses in `WORKSPACE_MATCHMAKING_RE_20260318.md`.
Everything below is taken from the exact string constants each handler function
references in the binary, so the JSON keys are authoritative (not inferred from
capture).

## Message dispatch map (function addresses, GOG 2.2.301)

All **receive** (`On*`) messages are handled by one master dispatcher:

- `0x006BF2A0` — `BZRNetInterface` receive dispatcher (parses `type`, switches on it)

Each **send** verb has its own builder:

| Verb | Builder addr |
|------|--------------|
| `Authorization` / `DoUpdateWAN` / `DoUpdateLAN` | `0x006C6E60` |
| `DoEnterLounge` | `0x006C6320` |
| `DoExitLounge` | `0x006C65B0` |
| `CreateLobby` | `0x006C3FB0` |
| `CreateGame` | `0x006C4350` |
| `DoJoinLobby` | `0x006C46B0` |
| `DoExitLobby` | `0x006C5760` |
| `DoSetLobbyOwner` | `0x006C49F0` |
| `SetLobbyData` | `0x006C4C50` |
| `SetPlayerData` | `0x006C4F70` |
| `DeleteLobbyData` | `0x006C6040` |
| `LockLobby` | `0x006C5290` |
| `SetLobbyMemberLimit` | `0x006C54F0` |
| `DoSendChat` | `0x006C3D50` |
| `DoP2PConnect` | `0x006C59C0` |
| `DoP2PRoute` | `0x006C5D20` |

## Envelopes

- **Outbound (client → server)** uses `content`:
  ```json
  { "type": "<Verb>", "content": { ... } }
  ```
- **Inbound (server → client)** uses `data`:
  ```json
  { "type": "On<Verb>", "data": { ... } }
  ```

The dispatcher hard-requires `type` first. If `type` is missing or parse fails it
logs `JSON Message Not understood` and resets the connection. Binary WS frames are
explicitly rejected (`Binary Payload not supported`) — everything is text JSON.

## Auth gate

- `OnAuthorization` **must be the first inbound message** or the client aborts
  (`OnAuthorization must be first message`).
- Any other inbound verb before auth → `BZR Web Socket Message When Not Authorized`
  and the connection is reset.
- On success the client logs `Authenticated to BZRNet As %s:%s`.

## Inbound message schemas (`data` fields the dispatcher actually reads)

| Message | `data` fields read by client |
|---------|------------------------------|
| `OnAuthorization` | `time`, `success` |
| `OnChatMessage` | `text`, `speakerId`, `lobbyId`, `isChat` |
| `OnHeartbeat` | *(presence only)* |
| `OnWANUpdated` | `userId`, `wanAddress` |
| `OnLANUpdated` | `userId`, `lanAddresses` |
| `OnLobbyMemberListChanged` | `member`, `removed`, `lobbyId`, `users` |
| `OnLobbyDataChanged` | `changedLobby` (+ lobby object) |
| `OnUserDataChanged` | user object (see below) |
| `OnLobbyListChanged` / `OnLobbyChanged` | `lobbies` (array of lobby objects) |
| `OnLobbyCreated` | lobby object / id |
| `OnLobbyRemoved` | lobby id |
| `OnLobbyJoined` | `reasonCode`, lobby object |
| `OnDoExitLobby` | — |
| `OnFailure` | `reasonCode` |
| `OnWhitelistUpdated` | `whitelist: { major, minor, list }` → logs `Whitelist updated to version : %ld.%ld` |
| `OnLobbyMemberP2PConnect` | `player`, `reset` |
| `OnP2PRoute` | `player`, `port` |
| `OnServerShutdown` | — |
| `OnLobbySampleData` | debug/sample only (string/bool/double/int demo) |

### Lobby object shape (keys the client parses)

`lobby`, `lobbyId`, `name`, `owner`, `userCount`, `memberLimit`, `isLocked`,
`isPrivate`, `isChat`, `metadata`, `users`

### User object shape (keys the client parses)

`userId`, `name`, `isAuth`, `isAdmin`, `isInLounge`, `wanAddress`,
`lanAddresses`, `clientVersion`

## Outbound message schemas (`content` fields the client sends)

| Verb | `content` fields |
|------|------------------|
| `Authorization` | `name`, `realname`, `authtype` (`"steam"`), `steamAppTicket`, `clientVersion` (`"2.2.301"`), `apiVer`, `inLounge`, `wanAddress`, `lanAddresses` |
| `DoUpdateWAN` | `wanAddress` (shares builder w/ Authorization) |
| `DoUpdateLAN` | `lanAddresses` |
| `CreateLobby` | `memberLimit`, `isPrivate`, `password` |
| `CreateGame` | `memberLimit`, `isPrivate`, `password` |
| `DoJoinLobby` | `password` (+ lobby id) |
| `SetLobbyData` | key, `value` |
| `SetPlayerData` | key, `value` |
| `DoSendChat` | chat body |
| `DoP2PConnect` | `player`, `reset` |
| `DoP2PRoute` | `player` |
| `DoEnterLounge` | JSON number `1` (builder initializes the serialized content value to integer `1`) |

## Current `server.py` alignment (2026-07-16 working tree)

The current working tree now:

- emits numeric `reasonCode` alongside the human `reason` (failure values remain
  provisional until the official enum is captured);
- exposes `lobbyId`, `name`, `isPrivate`, and derived `isChat` directly on lobby
  snapshots;
- exposes `isAuth` and the other binary-read fields on user snapshots;
- carries the binary-confirmed `player` field in P2P signals;
- emits `OnChatMessage.isChat` and includes the parsed lobby object directly in
  successful `OnLobbyJoined` acknowledgements;
- emits the distinct `OnP2PRoute` reply only after a real UDP `1339` route has
  been associated for the requested same-lobby peer pair;
- emits `whitelist.{major,minor,list}`.

The inner `data.type` on server events is not a parity bug: the March 21 stock
capture shows the official service sending it. The client ignores the duplicate
type after dispatch, but retaining it matches the captured service envelope.

UDP `1339` forwarding is implemented as a provisional community endpoint-pair relay.
The official relay allocation/association envelope is still unrecovered, so this is
not yet a claim of stock relay protocol parity. Ambiguous multi-peer endpoint mappings
are rejected rather than guessed.

## Capture-confirmed runtime values

- `Authorization.content.apiVer` is `"3.6"` in the March 21 Redux `2.2.301`
  capture.
- UDP `1338` endpoint discovery is fully confirmed for IPv4: the client sends an
  18-byte kind-`0x2` common header; the server echoes it and appends address
  length `0x04`, the observed source IPv4 address in network byte order, and the
  observed source port as big-endian `u16`, producing a 25-byte response.
- The common UDP header and the direct kind-`0x3 -> 0x4 -> 0x5` peer handshake
  are documented in the paired-capture report.

## Still unresolved (needs live capture, not static)

- Official `reasonCode` enum values beyond observed success code `0`.
- Official relay `1339` allocation/association envelope, routing identifiers,
  keepalive, and timeout semantics. The community relay now forwards registered
  peer-pair datagrams opaquely, but official association parity still needs capture.
- Full in-match `Net`/`NetPlayer` payload semantics. This is separate from
  matchmaking/relay parity because a transparent relay can forward the payload
  without decoding it.

Follow `BZRNET_PHASE2_CAPTURE_GUIDE_20260716.md` for the targeted live runs.
