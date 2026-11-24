# Battlezone 98 Redux Matchmaking RE Report

Date: 2026-03-18

## Scope

This report summarizes the current best-effort reverse engineering results for
Battlezone 98 Redux networking as relevant to a community-hosted dedicated
matchmaking service.

This is based on:

- the dedicated server repo in this directory
- local `BZLogger.txt` evidence from the installed game
- workspace RE notes in OpenShim
- the `Battlezone_LobbyMonitor` tool
- the live admin page and JS assets at
  `http://battlezone98mp.webdev.rebellion.co.uk:1337/`
- netcode-testing notes about P2P and relay behavior
- string extraction from the local game executable

This is not a claim that the full in-match gameplay protocol is completely
decoded. The evidence strongly supports a workable replacement for the
matchmaking / lounge / coordination layer. It does not yet prove a full
authoritative dedicated game server.

## Bottom Line

The practical architecture appears to be:

1. WebSocket control plane on TCP `1337`
2. UDP probe / NAT-assist bootstrap on UDP `1338`
3. optional relay fallback on UDP `1339`
4. direct peer-to-peer gameplay after lobby coordination succeeds

That means the most credible community-hosted "dedicated server" today is:

- a dedicated matchmaking and lobby coordinator
- plus UDP bootstrap compatibility
- plus optional future relay support for strict-NAT users

It does not appear that Redux expects a fully authoritative game simulation
server for normal multiplayer sessions.

## Confirmed Evidence

### 1. Hardcoded public WebSocket endpoint

Binary string extraction from the local Steam executable confirmed:

- `ws://battlezone98mp.webdev.rebellion.co.uk:1337/`

This is the strongest proof that the lounge / matchmaking plane is a WebSocket
service.

### 2. Initial live client bootstrap

Local `BZLogger.txt` confirms this sequence:

1. `BZRNetInterface starting connection to websocket`
2. `Websocket established!`
3. UDP packet sent to `68.183.35.188:1338`
4. WebSocket `Authorization` message sent
5. `OnWhitelistUpdated` received immediately after auth

Observed first UDP probe from client:

```text
40020000019c744772360000000000000000
```

Observed server response:

```text
40020000019c74477236000000000000000004d8472e2fd148
```

Current best interpretation:

- client sends an 18-byte bootstrap/probe packet on UDP `1338`
- server replies with the original payload plus a 7-byte trailer
- in the March 21 two-PC capture, the trailer decodes as
  `0x04 + public IPv4 + public UDP port`

March 21 examples:

- `04 d8 47 2e 2f fe 2f` -> `216.71.46.47:65071`
- `04 d8 47 2e 2f c6 39` -> `216.71.46.47:50745`

### 3. Live WebSocket auth payload fields

Observed outbound `Authorization` contains at least:

- `apiVer`
- `authtype`
- `clientVersion`
- `id`
- `inLounge`
- `lanAddresses`
- `name`
- `realname`
- `steamAppTicket`
- `wanAddress`

Observed example shape:

```json
{
  "type": "Authorization",
  "content": {
    "apiVer": "3.6",
    "authtype": "steam",
    "clientVersion": "2.2.301",
    "id": "S7656119...",
    "inLounge": false,
    "lanAddresses": ["127.0.0.1:53576", "192.168.0.44:53576"],
    "name": "GrizzlyOne95",
    "realname": "GrizzlyOne95",
    "steamAppTicket": "<ticket bytes>",
    "wanAddress": "216.71.46.47:53576"
  }
}
```

### 4. Live server push after auth

Observed immediately after auth:

- `OnAuthorization`
- `OnWhitelistUpdated`

The whitelist payload includes a versioned file allowlist and appears to be part
of normal client initialization, not an optional admin feature.

### 5. Binary-confirmed WebSocket verb inventory

The executable contains these BZRNet message strings:

- `Authorization`
- `OnAuthorization`
- `DoEnterLounge`
- `DoExitLounge`
- `GetLobbyList`
- `CreateLobby`
- `DoJoinLobby`
- `DoExitLobby`
- `SetLobbyData`
- `DeleteLobbyData`
- `SetPlayerData`
- `LockLobby`
- `SetLobbyMemberLimit`
- `DoSetLobbyOwner`
- `DoSendChat`
- `OnChatMessage`
- `OnLobbyChanged`
- `OnLobbyListChanged`
- `OnLobbyDataChanged`
- `OnUserDataChanged`
- `OnLobbyMemberListChanged`
- `OnLobbyJoined`
- `OnLobbyCreated`
- `OnLobbyRemoved`
- `OnWhitelistUpdated`
- `OnHeartbeat`
- `OnWANUpdated`
- `OnLANUpdated`
- `DoP2PConnect`
- `DoP2PRoute`
- `OnLobbyMemberP2PConnect`
- `OnRelayUpdated`
- `OnEnableRelayChanged`

This confirms the client knows about:

- lobby management
- user metadata updates
- WAN/LAN address updates
- explicit P2P coordination
- relay state changes

### 5b. Live admin-page JS confirms admin verb surface

The live admin page loads `scripts/bzadmin.js`, which confirms an additional
admin-facing WebSocket surface built on the same endpoint. That JS sends or
expects:

- `Authorization`
- `DoEnterLounge`
- `GetStatLog`
- `SetLogLevel`
- `SetRelay`
- `SetTest`
- `AddRegex`
- `RemoveRegex`
- `CreateLobby`
- `DoJoinLobby`
- `DoSetLobbyOwner`
- `SetLobbyData`
- `SetLobbyMemberLimit`
- `DoSendChat`
- `OnGetLogLevel`
- `OnGetUserList`
- `OnGetFullLog`
- `OnStdoutLogEntry`

This is strong evidence that the official service is not just a bare game
backend. It also exposes a first-party admin/control surface over the same WS
contract.

### 6. Lobby and game metadata writes observed in RE notes

OpenShim notes from a live walkthrough confirm the client sends:

- `CreateLobby`
- `SetLobbyData("name", "~chat~pub~~...")`
- `SetLobbyData("gameType", "1")`
- `SetLobbyData("gameSettings", "*")`
- `SetLobbyData("GameVersion", "2.2.301 + OpenShim")`
- `SetLobbyData("userCount", "1")`
- `SetLobbyData("userPack", "GrizzlyOne95                    ")`
- `SetPlayerData("friendID", "S7656119...")`

For game launch flow the notes also confirm:

- `SetLobbyData("name", "~game~pub~~test")`
- `SetLobbyData("nextmid", "2")`
- `SetPlayerData("miniid", "1")`
- `SetPlayerData("team", "1")`
- `SetPlayerData("knownPlayers", "S7656119...,1,1,")`
- `SetPlayerData("vehicle", "bvtump")`
- `SetPlayerData("ready", "...")`
- `SetLobbyData("launched", "1")`
- `SetPlayerData("launched", "1")`

This is strong evidence that the WebSocket service is not just a server browser.
It coordinates pre-match state that the peers then use for direct session setup.

### 7. Relay fallback exists

Binary strings and workspace notes confirm relay logic:

- `BZRNet P2P FAILED WAN Connect For Client %s, ATTEMPTING RELAY`
- `BZRNet P2P Starting RELAY Connect For Client %s - P2P Relay Response`
- `BZRNet P2P Completed RELAY Connect For Client %s, using address RELAY_HOST:1339`

Ports classified in OpenShim:

- WebSocket `1337`
- probe `1338`
- relay `1339`

This means strict-NAT failure is a real case, not a hypothetical one.

### 8. In-match BZRNet UDP packet field partially identified

The netcode-testing proxy work has one concrete gameplay-packet fact:

- BZRNet sequence number is at UDP payload byte offset `13`
- format: `u32le`

That is useful for packet-order and relay work, but it is not yet a full decode
of the gameplay packet format.

## Current Network Model

## A. Control Plane

Protocol:

- WebSocket over TCP `1337`
- JSON messages with envelope shape:

```json
{
  "type": "MessageName",
  "content": { "...": "..." }
}
```

and server replies commonly shaped as:

```json
{
  "type": "OnSomething",
  "data": { "...": "..." }
}
```

Responsibilities:

- auth
- whitelist delivery
- lounge entry
- lobby list snapshots and deltas
- user metadata propagation
- lobby metadata propagation
- chat
- owner/lock/member-limit controls
- relay status signaling
- P2P address coordination

## B. UDP Bootstrap Plane

Protocol:

- UDP `1338`

Observed behavior:

- client sends a short bootstrap packet beginning with `0x40`
- server replies with echo-plus-trailer

Likely purpose:

- NAT mapping bootstrap
- reachability validation
- server-side time or token echo
- seed for later P2P routing steps

## C. Direct Gameplay Plane

Most likely transport:

- UDP peer-to-peer between clients after matchmaking completes

Evidence:

- auth payload includes both `wanAddress` and `lanAddresses`
- lobby/user payloads retain peer address metadata
- explicit client verbs exist for P2P connect and route
- relay is only invoked after WAN connect failure
- packet-sequence work in netcode-testing targets live gameplay UDP packets

## D. Relay Plane

Protocol:

- likely UDP `1339`

Observed role:

- fallback when direct WAN hole-punch fails

Status:

- definitely exists in the client
- definitely observed in logs
- not yet sufficiently RE'd to implement a trustworthy production relay

## Practical Conclusion

For a community backup service, you do not need to fully decode gameplay UDP to
replace matchmaking. You need:

1. WebSocket message compatibility
2. UDP `1338` bootstrap compatibility
3. preservation of WAN/LAN address metadata
4. enough lobby mutation compatibility to let the client reach launch state

You only need full gameplay packet decoding if you want:

- authoritative dedicated game hosting
- transparent packet relay
- man-in-the-middle gameplay transport

## Minimum Viable Community Matchmaking Server

The minimum credible service should implement:

- TCP `1337` WebSocket endpoint
- UDP `1338` bootstrap shim
- whitelist push on auth
- `Authorization`
- `DoEnterLounge`
- `GetLobbyList`
- `CreateLobby`
- `DoJoinLobby`
- `DoExitLobby`
- `DoExitLounge`
- `DoSendChat`
- `SetPlayerData`
- `SetLobbyData`
- `DeleteLobbyData`
- `LockLobby`
- `SetLobbyMemberLimit`
- `DoSetLobbyOwner`
- `DoKickUser`
- `Ping` / heartbeat
- relay status replies, even if relay is disabled

The current `server.py` in this repo already covers most of that shape.

## What The Current `server.py` Gets Right

- listens on WebSocket `1337`
- listens on UDP `1338`
- returns `OnAuthorization`
- returns `OnWhitelistUpdated`
- returns `OnRelayUpdated`
- handles lounge and lobby CRUD
- handles chat
- handles user and lobby data mutations
- exposes `GetUserList`, `GetStatLog`, `GetFullLog`
- exposes health/status endpoints on `8080`
- persists lobby metadata to JSON

It is already a valid phase-1 failover prototype.

## What `server.py` Does Not Yet Prove

- full compatibility for the game's exact `OnWANUpdated` / `OnLANUpdated` flows
- exact `DoP2PConnect` / `DoP2PRoute` semantics
- production relay on `1339`
- Steam ticket validation
- complete parity with all lobby delta ordering expectations of the stock client

## Setup Steps For A Dedicated Matchmaking Server

### Phase 1: Stand Up The Service

1. Install Python deps or run Docker:
   - `pip install -r requirements.txt`
   - or `docker compose up -d --build`
2. Start:
   - `python server.py`
3. Validate locally:
   - `python tests/smoke_test.py --host 127.0.0.1`

### Phase 2: Open The Required Ports

- TCP `1337` for WebSocket matchmaking
- UDP `1338` for bootstrap / probe
- TCP `8080` for health checks

Optional future:

- UDP `1339` if you build relay support

### Phase 3: Redirect Clients

The executable has a hardcoded WebSocket URL, so full redirection requires DNS
or hosts override.

Recommended cutover:

1. point `battlezone98mp.webdev.rebellion.co.uk` to your server IP
2. keep `1337` and `1338` reachable publicly
3. use `/bzrserver=<ip>` and `/bzrnetport=1338` for emergency testing where
   useful

### Phase 4: Validate With Real Clients

Successful validation should show:

- `Websocket established!`
- `Authorization` succeeds
- `OnWhitelistUpdated` arrives
- lobby list populates
- clients can create and join lobbies
- game reaches launch state
- direct peer connection succeeds for at least two public Internet clients

### Phase 5: Decide Whether Relay Is Necessary

If strict-NAT users fail often:

- implement `1339` relay
- or implement a narrower NAT-assist coordinator first

Do not block initial community failover on relay unless testing proves it is
required for a large share of players.

## Best Current State Machine

Current best reconstruction:

1. client opens WebSocket to `ws://battlezone98mp.webdev.rebellion.co.uk:1337/`
2. client sends UDP bootstrap packet to `host:1338`
3. client sends `Authorization` with Steam identity and address data
4. server replies:
   - `OnAuthorization`
   - `OnWhitelistUpdated`
   - likely relay status update
5. client enters lounge with `DoEnterLounge`
6. client requests `GetLobbyList`
7. client creates or joins a lobby
8. lobby owner and members exchange metadata via:
   - `SetLobbyData`
   - `SetPlayerData`
9. launch state is signaled via metadata such as `ready`, `knownPlayers`,
   `launched`, team and vehicle selections
10. peers attempt direct UDP connectivity using exchanged WAN/LAN metadata
11. if WAN connect fails, client may fall back to relay on `1339`

## What Is Still Unknown

These are the biggest remaining RE gaps:

- exact semantics of `DoP2PConnect`
- exact semantics of `DoP2PRoute`
- exact payloads for `OnWANUpdated` and `OnLANUpdated`
- whether the UDP `1338` response includes anything beyond server time echo
- whether any auth/session token from the UDP bootstrap is later checked
- full relay packet format on `1339`
- whether some stock clients depend on message ordering subtleties not yet
  covered by `server.py`
- whether Steam/GOG auth verification is ever required in practice for service
  continuity

## Recommendation

Treat the current goal as:

- "community-hosted matchmaking and session coordination failover"

not:

- "authoritative dedicated gameplay server"

That narrower goal is already well-supported by workspace evidence and is likely
achievable with the current repo plus some compatibility refinement.

If you want to go beyond that, the next RE target should be relay and P2P route
messages, not the basic lounge stack.
