# Community Failover Plan

## Goal

Keep Battlezone 98 Redux multiplayer usable if Rebellion's hosted BZRNet service disappears or becomes unreliable.

The practical target is not "rewrite the entire game netcode." It is:

1. Replace the official lobby / matchmaking / whitelist service.
2. Preserve the game's existing peer-to-peer match flow where possible.
3. Add relay or NAT-assist only if community testing proves it is required.

## What ANet Did

Legacy ANet was already a hybrid model rather than a fully authoritative game server.

Key points from the Activision / Anet SDK docs:

* `overview.htm`: ANet had an Internet game server where players could find opponents and chat, plus a client shell that launched games.
* `server.htm`: the game server acted "mostly as a player-matching service."
* `anet.h`: clients called `dpSetGameServerEx()` to talk to the game server, then `dpOpen()` to create or join sessions.
* `dppkt.h`: ANet had indirect join / firewall-assist packets so the service could help peers connect.

That means the original Battlezone-era architecture was already:

* central service for discovery and matchmaking
* direct game session after launch
* extra assistance for difficult network topologies

## What Redux Does Now

Observed locally and against the live official host on March 13, 2026:

* DNS for `battlezone98mp.webdev.rebellion.co.uk` resolved to `68.183.35.188`.
* Plain HTTP on `http://battlezone98mp.webdev.rebellion.co.uk:1337/` served a BZ server admin page.
* The game log showed:
  * `BZRNetInterface starting connection to websocket`
  * `Websocket established!`
  * UDP probe to `68.183.35.188:1338`
  * `Authorization` over WebSocket containing `steamAppTicket`, `wanAddress`, and `lanAddresses`
  * `OnWhitelistUpdated` and lobby data responses over WebSocket
* A direct probe to the live UDP service confirmed that the server echoes the observed client probe and appends a 7-byte trailer.
* A direct probe to the live WebSocket service confirmed:
  * `OnAuthorization`
  * `OnWhitelistUpdated`
  * `OnLobbyListChanged`
  * lobby and user payloads containing peer WAN/LAN addresses

The strongest inference is that Redux kept the old "central coordination plus direct match traffic" shape, but refactored the service layer into:

* WebSocket + JSON for auth, lobby state, chat, whitelist, and admin tooling
* UDP on `1338` for BZRNet probe / handshake behavior
* Steam-flavored identity instead of pure legacy ANet identity
* explicit relay-related control messages, even if relay is not always needed

## Decision

Yes, a community replacement for the matchmaking service is a viable backup plan.

But the correct framing is:

* Replacing the lobby / matchmaking service is probably enough for many or most players.
* It is not yet proven to be enough for every player behind hostile NAT or firewall setups.
* Therefore the community solution should ship in phases.

## Recommended Architecture

### Phase 1: Community Matchmaking Failover

This is the minimum useful service.

Components:

* TCP `1337`: WebSocket lobby service
* UDP `1338`: capture-confirmed public endpoint probe
* UDP `1339`: opaque fallback relay for WS-associated peer pairs
* TCP `8080`: health and status endpoint
* persistent lobby state snapshots
* MOTD, admin controls, whitelist delivery

Behavior:

* accept `Authorization`, `DoEnterLounge`, `GetLobbyList`, `CreateLobby`, `DoJoinLobby`, chat, and lobby metadata updates
* retain and return `wanAddress` and `lanAddresses` exactly because the match flow appears to depend on peers learning each other's reachable addresses
* respond to the initial UDP probe with compatible echo-plus-timestamp behavior
* distribute the whitelist so clients do not stall waiting for it

Cutover options:

* preferred: DNS failover for `battlezone98mp.webdev.rebellion.co.uk`
* fallback: hosts-file override
* per-client override: `/bzrserver=ws://<ip>:1337/` (full URL; `/bzrnetport` is the client's local UDP port, not the server's)

Expected outcome:

* players can authenticate into the lounge
* players can see lobbies
* players can create and join lobbies
* the game should attempt to connect peers directly once enough metadata is exchanged

### Phase 2: Operational Hardening

Do this before public community cutover.

Required:

* deploy at least two nodes or one node plus a standby
* back up `data/server_state.json`
* add external uptime checks against `/health`
* log and review auth, lobby create/join, and chat events
* publish a client fallback guide for hosts-file override and direct flags

Recommended:

* reverse proxy / TLS termination for admin-facing pages if you expose any HTTP UI
* optional Steam ticket verification if you later want stronger abuse controls
* moderator banlist / allowlist persistence

### Phase 3: Relay Parity and NAT Hardening

A conservative UDP `1339` relay now exists. The remaining work is to recover and
validate the official allocation/association semantics and strict-NAT behavior.

Why it may be needed:

* the official admin UI exposes relay controls
* the original ANet stack had firewall-assist and indirect-join concepts
* strict-NAT users may fail even if the lobby service is fully replaced

Options:

* lightweight NAT-assist coordinator that helps peers open paths
* full packet relay for the subset of sessions that cannot connect directly
* host quality filtering so public lobbies prefer reachable hosts

Recommendation:

* keep direct P2P as the normal path and use relay only as fallback
* capture the stock `1339` association flow before claiming exact protocol parity
* preserve datagram contents and boundaries; do not reimplement client reliability/state machinery server-side

## Auth Policy Recommendation

Short term:

* accept Steam-style IDs and tickets but do not require ticket validation for community failover mode
* treat this as compatibility mode, not strong identity proof

Why:

* the immediate risk is service disappearance, not perfect trust
* operational continuity is more important than reproducing every backend dependency

Medium term:

* add optional Steam ticket validation behind a config flag
* keep open mode available for offline failover and emergency recovery

## Deployment Recommendation

Public service:

* `battlezone98mp.community.example` or equivalent
* TCP `1337` open
* UDP `1338` open
* TCP `8080` restricted if possible

Community cutover:

1. Validate local smoke tests.
2. Validate two public clients on different networks.
3. Validate one strict-NAT case if available.
4. Publish hosts-file override instructions before an outage happens.
5. Only attempt DNS replacement of the original hostname if you control that routing path.

## Success Criteria

The community backup is good enough when:

* the game reaches `Websocket established!`
* `Authorization` succeeds
* `OnWhitelistUpdated` arrives
* lobbies appear and update
* two external players can launch a match and remain connected

Relay is only mandatory if those final peer connections fail often enough to matter.

## Current Repo Implications

The existing `server.py` already covers the right shape for Phase 1:

* WebSocket lobby compatibility
* UDP probe compatibility
* health and persistence

Immediate priorities are:

1. preserve payload compatibility with the observed admin and monitor tooling
2. validate stock-client WS field parity and exact builder shapes
3. test direct-peer success across real Internet clients
4. capture and graduate the provisional `1339` endpoint-pair relay to official allocation/association parity
