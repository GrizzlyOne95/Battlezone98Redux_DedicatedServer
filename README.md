# Battlezone 98 Redux Dedicated Server Project

This repository contains tools and documentation for reverse engineering the Battlezone 98 Redux server protocols (BZRNet). The goal is to develop a custom dedicated server to ensure multiplayer longevity.

For operations and cutover notes, see `NOTES.md`.
For the ANet-to-BZR comparison and the recommended community failover architecture, see `COMMUNITY_FAILOVER_PLAN.md`.
For the full multiplayer picture (ports, traffic, connection flow, auth rules, setup, gaps), see `BZRNET_MULTIPLAYER_SERVER_GUIDE.md`.

## New: MVP Dedicated Server (`server.py`)

This repo now includes a runnable compatibility shim that emulates key BZRNet behavior:

*   **WebSocket Lobby Service** on `:1337`
*   **UDP Public-Endpoint Probe** on `:1338`
*   **Opaque UDP Relay Fallback** on `:1339` (WS-associated peer pairs; provisional stock association parity)
*   **Health/Status HTTP Endpoint** on `:8080` (`/health`, `/status`)
*   **Protocol/Capture Endpoints** on `:8080` (`/protocol`, `/captures`)
*   **Persistent state snapshots** (lobby metadata) to JSON
*   **Admin controls** (`--admin-key`, `--admin-ids`)

Observed against the official service on March 13, 2026:

*   The public host is `battlezone98mp.webdev.rebellion.co.uk`
*   Clients establish a WebSocket session on TCP `1337`
*   Clients perform a UDP probe / handshake on `68.183.35.188:1338`
*   WebSocket `Authorization` includes `steamAppTicket`, `wanAddress`, and `lanAddresses`
*   Lobby data returned by the service includes peer address metadata, which strongly suggests the actual match remains peer-to-peer once players connect

### Implemented WebSocket Message Support

The server currently handles:

*   `Authorization` -> `OnAuthorization`
*   `DoEnterLounge` -> `OnDoEnterLounge`
*   `DoExitLounge` -> `OnDoExitLounge`
*   `GetLobbyList` -> `OnGetLobbyList`
*   `CreateLobby` -> `OnLobbyCreated`
*   `CreateGame` -> `OnLobbyCreated`
*   `DoJoinLobby` -> `OnLobbyJoined`
*   `DoExitLobby` / `DoExitLounge` -> `OnDoExitLobby`
*   `DoSendChat` -> `OnChatMessage`
*   `SetPlayerData` -> `OnUserDataChanged`
*   `SetLobbyData` / `DeleteLobbyData` -> `OnLobbyDataChanged`
*   `DoUpdateWAN` / `DoUpdateLAN` -> `OnWANUpdated` / `OnLANUpdated`
*   `DoP2PConnect` -> `OnLobbyMemberP2PConnect`; `DoP2PRoute` -> real route association plus `OnP2PRoute` when successful
*   `LockLobby`, `SetLobbyMemberLimit`, `DoSetLobbyOwner`, `DoKickUser`
*   `Ping` / `DoPing` -> `OnHeartbeat`
*   `GetUserList` / `GetStatLog` / `GetFullLog` / `GetLogLevel`
*   admin-side `SetLogLevel`, `SetRelay`, `SetTest`, `AddRegex`, `RemoveRegex`, `TraceData`

### Quick Start

1.  Install dependencies:
    ```bash
    pip install -r requirements.txt
    ```
2.  Run the server:
    ```bash
    python server.py
    ```
3.  Optional verbose mode:
    ```bash
    python server.py --log-level DEBUG
    ```
4.  Optional smoke test (in another terminal while server is running):
    ```bash
    python smoke_test.py
    ```

#### Desktop launcher (`server_gui.py`)

`python server_gui.py` (or double-click `ServerGUI.bat`) opens a small tkinter window that starts and
stops `server.py`, edits the common options, streams the log, and lists connected players and
lobbies from the local health port. It needs no extra packages. Stop sends `stop` over stdin
(`--stdin-control`) so the server saves state before exiting. Settings, including the admin key,
are saved to `server_gui.json` (gitignored). The players/lobbies view comes from
`GET http://127.0.0.1:8080/lobbies`, which is also usable on its own.

Players with [OpenShim](https://github.com/GrizzlyOne95/Battlezone98Redux_Shim) need no launch
option: its Multiplayer setup screen offers **Rebellion**, **Custom** (type the server's LAN or
public address) and **Local** (`127.0.0.1`, a server on the same PC). That screen swaps only the
host, so it reaches a server on the default ports (TCP 1337, UDP 1338) only; on other ports
players use `/bzrserver=`. The launcher shows the matching hint under the connect options.

For three or more players who may need relay fallback, enable **Separate UDP port per peer pair**
and choose an inclusive UDP range (the launcher suggests `1340`–`1369`). Allow/forward that entire
range as well as the lobby/probe ports. Existing saved settings keep shared relay selected until
you enable pair mode. The connection help shows the required range and its capacity.

#### Access control (who may join)

Stock clients only present a claimed id: Steam's `steamAppTicket` cannot be validated without
Rebellion's publisher key and GOG sends no ticket. So admission control keys on that id plus the
connecting IP. It keeps strangers out of a friends server; it cannot stop someone who copies an
approved player's public Steam id (IP pinning narrows that). For a real gate, put the server behind
Tailscale.

*   `--access-mode open|approve|allowlist` (default `open`, also saved in the state file).
    `approve` holds an unknown player's login for `--approval-wait-seconds` (default 30) while the
    operator decides, then refuses it; approving afterwards admits the client's next automatic retry.
    `allowlist` refuses unknown ids at once.
*   `--pin-ips` re-asks approval when an approved id connects from a new IP.
*   Bans by id and/or IP; `--auth-rate-limit` login attempts per IP per minute (default 20).
*   `GET /access` shows the mode, pending, approved and banned lists; `POST /access` with
    `{"action": "set|approve|deny|remove|kick|ban|unban", ...}` changes them. POST is accepted only
    from loopback even if `--health-host` is widened. The launcher's Access tab and the Players tab's
    Approve/Kick/Ban buttons use these.

#### Admin and moderation hardening

Community-only controls; none of them change the messages stock clients see, and the defaults keep
stock clients working.

*   **Audit log** (`--audit-log`, default `data/audit.log`, empty disables; `--audit-log-max-bytes`
    5000000, `--audit-log-backups` 3): append-only JSON Lines of auth accept/deny (id, name, ip,
    authtype, clientVersion, reason, `ticketWithheld`), every `POST /access` action with its actor,
    bans, unbans, mutes, kicks, admin WebSocket verbs (allowed or denied), admin-key failures and
    refusals, and expiries. Tickets, passwords and admin keys are never written (credential-named
    fields are blanked and the configured admin key is scrubbed from every value). `data/` is git-ignored.
*   **Names**: control, bidi and zero-width characters are stripped, whitespace is collapsed and the
    name is capped at `--name-max-length` (32; the stock client limit is not documented in this repo).
    An empty result falls back to the id; names that match `--reserved-names` (default `SERVER, Admin,
    Moderator, ...`, compared after case/punctuation folding) become `Player-<id tail>`. Admins are
    exempt. Cyrillic homoglyphs are not folded. `SetPlayerData` name changes follow the same rules.
*   **Client version** (`--client-version-policy off|log|enforce`, default `log`;
    `--min-client-version`, `--allowed-client-versions`, `*` suffix is a prefix match): `log` admits
    and records mismatches, `enforce` refuses with the usual failed login and close `4002`.
*   **Chat flood** (`--chat-rate` 2/s, `--chat-burst` 8, `--chat-flood-strikes` 5 drops inside 30 s,
    `--chat-automute-minutes` 5; `--chat-rate 0` disables): over-limit messages are dropped, the sender
    gets a throttled server notice (same shape as the MOTD), and repeated flooding auto-mutes.
*   **Timed bans and mutes**: `POST /access` accepts `duration` (`90`, `30m`, `2h`, `7d`) or an
    absolute `expiresAt` (`YYYY-MM-DD HH:MM:SS` UTC or epoch seconds) with `ban` and `mute`; `unmute`
    lifts a mute. Expired entries are pruned on check, on load and in snapshots; entries without an
    expiry stay permanent. Muted players' chat is dropped server-side. Bans and mutes persist in the
    state file; both appear in `GET /access` (`bannedIds`, `bannedIps`, `mutedIds`) and the launcher.
*   **Admin key transport**: the key is compared in constant time and only elevates a session from a
    loopback peer, a socket the server sees as TLS, or a peer in `--trusted-proxies` (IPs/CIDRs) whose
    `X-Forwarded-Proto: https` (or `Forwarded: proto=https`) is believed. Any other forwarded request
    is refused, including through a loopback proxy. `--allow-plain-remote-admin` is an explicit,
    unsafe opt-in and logs a warning at startup. A refused key still admits the player, as a normal
    user. `--admin-ids` is a claimed id and is not transport gated.
*   **Ticket-withheld clients**: OpenShim sends the literal `withheld` as the platform ticket to
    non-Rebellion servers. It is admitted like any non-empty ticket (an empty Steam ticket is still
    refused), recorded as `ticket_withheld`, written to the audit log and shown as
    `steam (ticket withheld)` in `/lobbies` and the launcher's Players tab. The wire `authType` is unchanged.

Run `python admin_hardening_test.py` for the in-process checks.

#### Address privacy (`--redact-addresses`)

Like the official service, the server by default sends every player's WAN/LAN address to every
connected session, so anyone watching the lounge can collect them. `--redact-addresses` (launcher:
"Hide player IPs outside their game") sends a player's `wanAddress`/`ipAddress`/`lanAddresses`
only to themselves, admins, and members of the same game lobby (`~chat~` lobbies, including the
default lounge lobby, don't count). The keys are omitted for everyone else. With it on,
`DoP2PConnect`/`DoP2PRoute` only work between members of the same game lobby, and `GetUserList`,
`GetFullLog` and `TraceData` are admin-only.

The stock client only appears to need peer addresses after joining the same game lobby
(`OnLobbyMemberP2PConnect` carries none), but this has not yet been confirmed in a two-network
match, so the flag is off by default. Players still see each other's addresses once in a match
together, as with any peer-to-peer game.

#### Read-only chat watchers (`--allow-loopback-watchers`)

Lets a tool on the same machine, such as BZ1 Game Watcher, read chat rooms without putting an
account in each room. A session gets the watcher role when it sends `"watcher": true` in its
`Authorization` content, the server runs with `--allow-loopback-watchers`, and the connection comes
straight from loopback (forwarded/proxied requests are refused). `OnAuthorization` then carries
`"isWatcher": true`; the field is omitted otherwise, so stock clients see the usual shape.

A watcher receives a copy of every `OnChatMessage` posted in an open public chat room
(`~chat~pub~~*`, not private or passworded), with `lobbyId` and `speakerId`, plus the normal lounge
and lobby-list traffic. It never joins a room, is never a whisper target and is left out of
`totalUsers`. Everything except `DoEnterLounge`, `DoExitLounge`, `GetLobbyList` and `Ping`/`DoPing`
is ignored, so it cannot join, create rooms, chat or change metadata. It gets no player addresses
unless it is also an admin. Grants and refusals go to the audit log.

#### Standalone Windows package

`python build_package.py` builds `dist/BZRDedicatedServer-<version>.zip` for PCs without Python.
It holds `BZRServerGUI.exe` (the launcher), `BZRServer.exe` (the server, also usable headless with
the same options as `server.py`), a README and the LAN guide. The build uses its own venv under
`build/` with PyInstaller; `build/` and `dist/` are gitignored.

Useful runtime options:

```bash
python server.py ^
  --state-file data/server_state.json ^
  --autosave-seconds 15 ^
  --motd "Welcome to failover BZR server" ^
  --admin-key "your-secret" ^
  --admin-ids "S7656119...,U1234" ^
  --retain-empty-lobbies
```

Default bind addresses:

*   WebSocket: `ws://0.0.0.0:1337`
*   UDP endpoint probe: `0.0.0.0:1338`
*   UDP relay: `0.0.0.0:1339` (shared mode by default; optional bounded peer-pair ports)
*   Health: `http://127.0.0.1:8080/health`
*   Protocol: `http://127.0.0.1:8080/protocol`
*   Captures: `http://127.0.0.1:8080/captures`
*   Lobbies/players: `http://127.0.0.1:8080/lobbies`

### Docker / Compose Deployment

The base Compose file keeps shared relay. For multi-client relay, use the optional overlay:

```bash
docker compose -f docker-compose.yml -f docker-compose.pair-ports.yml up --build
```

Set `BZR_RELAY_PAIR_PORT_START` / `BZR_RELAY_PAIR_PORT_END` in `.env` to the range you allow/forward.
The overlay enables pair mode and publishes the same UDP range with identical host/container
port numbers. Its default `1340`–`1369` gives 30 pair ports total across all active lobbies.
`EXPOSE` in the Dockerfile is metadata; the Compose overlay performs port publishing.

1.  Copy env template:
    ```bash
    copy .env.example .env
    ```
2.  Edit `.env` values.
3.  Build and run:
    ```bash
    docker compose up -d --build
    ```
4.  Check health:
    ```bash
    python smoke_test.py --host 127.0.0.1
    ```

The compose stack mounts `./data` into the container so state survives restarts.

### Persistence Behavior

*   State file default: `data/server_state.json`
*   Persisted: lobby metadata/settings, next lobby ID
*   Not persisted: active websocket sessions
*   On restart, restored lobbies are empty (`users={}`) and can be re-joined
*   `--retain-empty-lobbies` keeps empty rooms so they can survive restart

### Validate with Battlezone Lobby Monitor

In your `Battlezone_LobbyMonitor` tool:

1.  Game: **Battlezone 98 Redux**
2.  Host: `127.0.0.1:1337`
3.  Key: leave blank (for local test)
4.  Click **Connect**

Then test:

*   Create lobby
*   Join selected lobby from another monitor instance
*   Send chat
*   Leave lobby

### Validate with Official Admin Page Compatibility

The official service exposes an admin UI at `http://battlezone98mp.webdev.rebellion.co.uk:1337/`
that loads `scripts/bzadmin.js` and related assets. That JS expects:

*   `Authorization`
*   `DoEnterLounge`
*   `GetStatLog`
*   `OnAuthorization`
*   `OnLobbyListChanged`
*   `OnLobbyChanged`
*   `OnLobbyRemoved`
*   `OnGetLogLevel`
*   `OnGetUserList`
*   `OnWhitelistUpdated`
*   `OnRelayUpdated`
*   `OnEnableRelayChanged`
*   `OnGetFullLog`
*   `OnStdoutLogEntry`
*   `OnGetStatLog`
*   `SetLogLevel`
*   `SetRelay`
*   `SetTest`
*   `AddRegex`
*   `RemoveRegex`

The current server now implements compatibility stubs or responses for that
admin surface as well.

### Validate with Game Client

Use these launch flags:

```bash
battlezone98redux.exe /netpktlog /netlog=3 /bzrnetlog=3 /bzrserver=ws://127.0.0.1:1337/
```

Notes:

*   `/bzrserver=` replaces the stock lobby URL verbatim, so it must be a full `ws://host:port/` URL; a bare IP fails to connect. The client sends its UDP probe to the same host on the lobby port + 1 and uses the relay port the server advertises. Verified end to end on the GOG exe (2026-10-05).
*   UDP `1338` performs the capture-confirmed public endpoint probe.
*   UDP `1339` now forwards WS-associated peer-pair datagrams byte-for-byte; it does not deserialize `7a75`, `0x5f`, or other gameplay payloads.
*   The official `1339` association/allocation envelope is still unrecovered. The community relay intentionally drops ambiguous multi-target mappings, so strict/symmetric-NAT parity remains a live validation target.
*   A shared relay endpoint cannot distinguish multiple relayed peers for one player. The server
    warns when shared routes overlap and drops ambiguous traffic. Use bounded pair ports for
    LAN/WAN fallback meshes, or ephemeral pair ports for private loopback tests as described below.
*   Run `python stock_client_compat_test.py` for in-process checks built around the executable-confirmed client message shapes.

### Multi-Client Relay Ports

For LAN/internet clients, explicitly enable one UDP port per peer pair and choose a range the
clients can reach:

```bash
python server.py --relay-pair-ports --relay-pair-port-start 1340 --relay-pair-port-end 1369
```

Allow/forward UDP `1340`–`1369` as well as TCP `1337` and UDP `1338`–`1339`; substitute your chosen
range everywhere. Only UDP probe/shared relay ports are excluded from allocation; TCP services
can use the same numeric port without a UDP collision. Occupied UDP ports are skipped. When no
usable port remains, negotiation fails with a range/bind error and advertises no endpoint;
existing pairs keep working and the server never falls back to an ambiguous shared port.

A fully relayed lobby with `n` players uses `n*(n-1)/2` pair ports: four players need six. Capacity
is shared across all active lobbies. A `1340`–`1369` pool contains 30 ports, reduced by occupied or
reserved ports. Disconnect, lobby leave, idle expiration and relay disable release the port.
`/status` reports `pairPortRange`, `activePairPorts` and `pairPortAllocationFailuresTotal`.

CLI defaults preserve shared relay. `BZR_RELAY_PAIR_PORTS=1` explicitly enables pair mode;
`BZR_RELAY_PAIR_PORT_START` / `_END` supply the bounds. Bounds alone do not enable the mode.
`--no-relay-pair-ports` overrides an environment opt-in. Both bounds `0` select ephemeral ports.

Start the isolated server with `--relay-host 127.0.0.1 --relay-pair-ports` for
multiple clients on one computer. Each negotiated peer pair gets its own
ephemeral UDP port, advertised to both clients in `OnP2PRoute`. Datagrams leave
through that same socket so the native client's source-endpoint lookup can
distinguish every peer. Complete payloads and packet boundaries are preserved.
Repeated and reversed route requests reuse the port. Leave, expiration, relay
disable and shutdown close the socket; a failed allocation advertises no port.
The shared listener rejects unassociated traffic in this mode.

Ephemeral ports are intended for isolated/private use; ordinary WAN port forwarding needs the
bounded mode above. The launcher permits `0`/`0` only when listening on `127.0.0.1`. Its **Extra
args** field still permits explicit CLI overrides, including older `--relay-pair-ports` settings.

Run `python relay_pair_ports_test.py` for real UDP tests of three- and four-peer
meshes, all six four-peer pairs in both directions, byte-exact payloads, native
source-port identity, negotiation retries, unrelated senders, and lifecycle
cleanup. The existing stock compatibility matrix (45/45), relay impairment
checks (5/5), and server smoke checks (6/6) also passed on 2026-10-07.

Run `python -m unittest relay_bounded_pair_ports_test server_gui_test` for bounded four-player
endpoint identity, real UDP bind collisions, exhaustion cleanup, release/reuse, allocation/leave
races, shared-mode warnings, CLI/environment configuration and GUI persistence/propagation.

Native four-client validation used the isolated coopflow run
`C:\BZRCoop\runs\cr-misn05-first-4p-20261007-170003`. All three guests joined as
teams 2/3/4, all four clients entered misn05, and their complete player
registries, local ownership, directional alliances and host authority checks
passed. The relay reported six active pair ports, 11,658 received/forwarded
datagrams and zero drops or ambiguous routes. Guest opening presentation,
script-error and audio checks passed. That mission run subsequently failed a
starting-resource assertion; this evidence validates four-client routing and
mission entry, not a complete mission victory.

This mode remains opt-in. Remote use requires access to the allocated UDP
ports; official relay association parity and ambiguous NAT rebinding remain
unqualified. Same-IP peers require distinct, known source ports. Route expiry
is evaluated on route registration, datagrams and metrics reads, as in the
shared-port mode.

### Native Acceptance Check

Opaque relay counters show forwarding, not whether the game accepted those
packets. Analyze every client after a coopflow run with:

```powershell
python native_network_health.py --run-dir C:\BZRCoop\runs\<run-name> --clients 4 --json-out C:\BZRCoop\runs\<run-name>\native-network-health.json
```

The analyzer ignores a five-second loading grace interval and stale reliable
retransmissions. Its default gate flags at least 20 type-0 rejections whose
Drop diagnostic prints expected=0, across four active one-second bins within a five-second
window. Exit status is 0 when that pattern is absent, 1 when it occurs, and 2
when logs, peer connections or observation time are incomplete. This is an
explicit, configurable rejection-frequency gate, not a measured loss percentage
or a continuous five-second blackout. The Drop line's expected=0 is not the
receiver's actual expected sequence: a matching preceding Received line can
show a nonzero expectation. The analyzer reports those paired expectations
separately and leaves unmatched expectations unknown. Historical gate results
and legacy field names retain their original diagnostic-count meaning.
ACK comparisons include sample age because a native ACK log entry may be old;
they are diagnostics and do not determine the gate result.
`python native_network_health_test.py` covers the gate with sanitized fixtures.

The native receive guard at `0x75D800` compares reliable and unreliable
sequence numbers with the connection's receive counter. When an unreliable
packet fails that comparison, the diagnostic deliberately prints expected
zero; that does not establish a counter reset. The reliable send path at
`0x75BEB0` keeps increasing its sequence as it queues packets, while retry mode
in `0x75AAF0` drains limited batches. A reliable backlog can therefore make
newer unreliable state/ping packets fail the strict sequence check.

The four-client win attempt `cr-misn05-win-Overridemisn05-coop-4p-20261007-181309`
exhibited this pattern on host/client 3. Client 3's cumulative ACK toward the
host advanced from 250 to 1097, while its last rejected unreliable host
sequence was 1383. Route callbacks had completed before the mission interval;
there was no supported evidence of a signaling reset. Reliable retry logs
contained substantial repeated Lua presentation traffic. This establishes
backlog symptoms, but does not isolate the initial cause or prove a relay fix.

The subsequent `cr-misn05-net-trace-4p-20261007-183555` run completed the full
four-client win after campaign subtitle and scenario-timing fixes, using the
same relay implementation. Every client passed the native rejection-pattern
gate. Client 3 had zero post-grace zero-expected rejections from the host.
The host still observed 113 sporadic rejections from client 1, with 17 in its
worst five-second window, below the configured sustained-pattern threshold.
The final native HUD showed all peers at 15 ms with displayed losses 10/0/0;
the evidence supports recovery from the sustained client-3 failure, without
claiming zero loss on every link. These local run artifacts remain in
`C:\BZRCoop\runs`, including `native-network-health.json` and relay traces.

Enable `--relay-trace <run-dir>/relay-trace.jsonl` for additional transport-header
evidence. The analyzer summarizes it alongside the native logs. It reads only
the first transport header per datagram; cumulative ACK/header ages and
forwarded counts cannot independently establish native acceptance or relay-hop
latency. Raw run logs and traces stay outside Git.

### Maximum Network Capture

Use all of these server options for a private diagnostic run:

```text
--log-level DEBUG --relay-pair-ports --relay-trace <run-dir>/relay-trace.jsonl --relay-trace-payloads --protocol-trace <run-dir>/protocol-trace.jsonl
```

`DEBUG` is the maximum Python log level. The protocol JSONL additionally
archives untruncated WS/UDP event payloads beyond the 400-entry `/captures`
history, with authentication credentials and passwords redacted, including
the supported `steamAppTicket`, `authTicket`, and `platformTicket` fields in
both archived payloads and previews. The relay
JSONL records every routed datagram and unroutable, socket-error and route
lifecycle events. Routed records include the peer-pair/lobby identity,
advertised relay socket, nanosecond wall timestamp, opaque datagram SHA-256 and
first transport header. `--relay-trace-payloads` also copies the complete opaque
datagram as hex, without decoding gameplay payloads or changing forwarding.
It is off by default and adds logging overhead. Both traces share a run ID;
their raw files, endpoints and game payloads stay outside Git.

With `--relay-trace`, routed decision records also have a unique `packetId`,
`sourceUser`/`targetUser`, route identity, and a process-local `socketId` scoped
to the trace instance. Each forwarded copy emits a separate `socket_send`
event after `sendto` returns, or when it raises or a delayed callback skips a
closing transport. Join on `packetId` and `copyIndex`; duplicate copies share
one arrival ID. `outcome=submitted` means the transport call returned, accepting
the datagram for OS submission (possibly buffered); it does not prove OS send
success, network delivery, or client receipt. A closing transport may silently accept an immediate call;
`transportClosing` records that state without changing the existing send path.
Events record scheduled delay, actual queue wait from relay callback entry,
scheduler lateness, and send-call duration in milliseconds. Existing decision
timestamps and impairment counters describe planned forwarding; these outcome
events provide submission evidence. Copies with no outcome at capture end
remain unknown. Asynchronous `socket_error` events have socket identity but
`packetCorrelation=unknown` because they cannot reliably identify one datagram.
Send events omit `transport` and `disposition` so existing replay tools retain
their original decision-based behavior. The trace header advertises
`sendOutcomeCapture=true`. Both trace writers already flush every record.
Diagnostics are disabled when no relay trace writer is configured.

`python relay_observability_test.py` checks immediate and delayed outcomes,
duplicate correlation, closing sockets and send failures, legacy replay
filtering, and opaque byte-exact UDP forwarding on ephemeral loopback ports.

Native same-PC qualification on 2026-10-09 used four private GOG clients and
clean relay settings with mixed AI battles, nav beacons and ammo/repair pickups.
The relay implementation tested was `fcc350d`; raw endpoint/payload captures
remain private. Each run passed 32 gameplay/cleanup checks and native rejection
health, with four clean, untruncated captures and zero capture drops:

| AI / beacons / pickups | Source TX = decisions = submissions | Target RX gaps, whole run | Conservative post-ramp RX gaps |
|---|---:|---:|---:|
| 40 / 8 / 32 | 45,774 | 64 | 6 (5 unreliable, 1 reliable) |
| 80 / 16 / 64 | 50,585 | 54 | 2 (unreliable) |
| 160 / 16 / 128 | 57,709 | 58 | 5 (2 unreliable, 3 reliable) |

All twelve directed links matched source bytes and correlated submitted outcomes;
no missing outcomes, submission exceptions or byte mismatches were found. Inner
windows exclude ramp and the finished sample, spanning 55.9 / 56.4 / 55.0 seconds,
rather than exact 60-second intervals. Most whole-run gaps follow final receive
activity on a target P2P socket generation, mostly after sampled combat. Binary RX also
lacks those datagrams; this does not establish OS/network loss or native game
acceptance. Native guest shutdown logs include receive error 10054 and invalid
close error 10038, without proven causal attribution. Do not score a whole-run
gap ratio as sustained-combat loss. The 160 run's queue wait p95/max was
0.113/0.420 ms and send call p95/max 0.043/0.241 ms. This exercises server logging
through the native chain; WAN, repeated impaired battle A/B and soak
remain separate qualification gates. RDP PrintWindow images were black/stale;
rendering and FPS are not qualified by this evidence.

One completed 80-AI / 16-beacon / 64-pickup fix ON versus stock pair at 3%
loss also exercised this logging. ON: 57,224 decisions, 1,719 intentional drops,
55,505 forwarded/submitted; stock: 47,305 decisions, 1,417 drops, 45,888
forwarded/submitted. All source bytes and socket outcomes matched across 12
links, with complete clean captures. Both gameplay/cleanup passed, but both
native rejection-health checks failed. Excluding intentional drops, additional
target RX deficits were 86/87, including 1/11 in conservative post-ramp windows;
80/72 occurred beyond final P2P RX. These are missing receive records, not
proven OS delivery failures. The four-arm repeat stopped on the third arm's
pre-impairment co-op admission failure. Retain incomplete qualification and the
separate completed services matrix verdict; raw captures remain private.

An unchanged-policy synchronized respawn diagnostic under periodic one-second
outages passed 14 steps, 24 checks and native rejection health. All four owners
died within 2 ms during the logged outage and chose rally fallback. Across the
complete trace, 65,641 decisions equal 60,817 forwarded/submitted plus 4,824
intentional drops; source TX matches on every link, with no missing outcomes
or socket errors. Four complete clean captures retain 108 additional RX gaps,
99 beyond final P2P RX, and none during the aligned outage-to-placement window
after intentional drops are excluded. A successful synchronized reproduction
does not erase the earlier staggered-death failure or qualify every outage
offset. Native submission still does not prove delivery or application acceptance.

The remaining seed-112 ON/stock battle pair completed too: gameplay/cleanup
passed in all four completed arms, while every whole-mission rejection-frequency
gate failed. Matched post-ramp modeled blackout seconds per link-minute were
0.6334/17.1155 (seed 12) and 0.5566/26.3344 (seed 112), ON/stock. Those are
forwarding-based replay durations, not measured native acceptance. Frequency
windows entirely inside post-ramp combat affected 1/11 and 0/12 directed links.
The interrupted pre-impairment startup attempts remain separate INCOMPLETE
records; native DLL, server implementation and battle workload matched.

Seed-112 ON logging has 56,284 decisions = 54,601 submitted + 1,683 deliberate
drops; stock has 50,282 = 48,782 submitted + 1,500 drops. Exact source bytes
and every correlated outcome match, with four clean captures per arm. Extra
target JSON RX deficits excluding deliberate drops are 106/83, including 5/11
post-ramp. All 83 stock deficits are also missing by binary multiset count;
two duplicate hashes cannot uniquely identify a missing occurrence. Three
other datagrams are in JSON without a matching binary receive, an unresolved
capture discrepancy despite complete ring retention. Stock deficits precede
its final P2P RX, unlike the mostly late deficits in other runs. Full network
qualification remains open.

The analyzer correction passed 14 focused fixtures and preserved every legacy
field/status across all four battle logs. All 1,168/5,503/865/8,921 counted
printed-zero rejections (ON1/stock1/ON2/stock2) paired to actual nonzero receiver
expectations. One investigated ON1 window contained 36 received-but-rejected
datagrams and four short replay gaps of 94/86/120/94 ms, explaining a frequency
failure without a continuous five-second blackout. Raw diagnostics stay private.

`python network_capture_test.py` validates history overflow, full message
retention, credential redaction, byte-exact maximum capture, default payload
omission, unroutable/error records and real pair-socket lifecycle capture.
The native clients separately need `/netpktlog /netlog=3 /bzrnetlog=3` to log
their original reliable and unreliable sends; server counters cannot replace
native receive evidence.

The host-skip case `cr-misn05-win-Overridemisn05-coop-Skipperhost-4p-20261007-191204`
completed every mission/camera step but failed native acceptance in both
directions between clients 1 and 2. The first offending unreliable sequence221
reached the relay at 19:19:25.411, almost eight seconds before the first camera.
The receiver rejected it against sequence220, while the required reliable
sequence220 reached the relay only at 19:19:26.386. Corresponding native kind6
rejection ACKs confirmed that all zero-expected rejections were ahead of the
receiver's counter. Reliable sequence numbers remained contiguous and ACKs
kept advancing; first-header timestamps did not reverse order in the relay.
Rejections continued after camera release, so the evidence supports native
reliable queue delay followed by unreliable overtaking, without isolating why
the clients entered retry mode. The existing header-only trace cannot exclude
batched reliable tails; maximum native/relay captures are needed for that
comparison. No relay behavior change was made on this evidence.

### Network Impairment (private test runs only)

The relay can stand in for a bad network so netcode changes can be tested
under loss, latency and congestion without a packet-filter driver. It never
rewrites a datagram, only decides whether each one is sent, when and how many
times. It is off by default; never enable it on a server real players use.

```text
--impair "loss=3,delay=80,jitter=20,seed=7"
```

Keys (all optional, comma separated; full reference in `relay_impairment.py`):
`loss=P` and `dup=P` (percent), `outage=D/T` (drop everything for D ms every
T ms), `delay=MS` with `jitter=MS` (each stream stays in order), `reorder=P`
with `reorder_ms=MS`, `rate=KBPS` with `queue=MS` (a per-player uplink cap with
tail drop), `peer=A|B` with `dir=both|from|to` (only those players' streams),
`for=S` (clear after S seconds) and `seed=N`. `loss=100,peer=<name>,for=20`
blackholes one player for 20 seconds.

`POST /relay/impairment` with `{"spec": "..."}` changes it while a session runs
and `{"spec": ""}` clears it; like `/access` it only answers this machine.
`GET /relay/impairment` and `/health` report its counters. With `--relay-trace`
every impaired datagram records its drop reason or delivery delays, and each
change is a trace event, so a run can be replayed in the order the receivers
got it. `python relay_impairment_test.py` covers parsing, determinism, every
mode and the relay hook.

### Preemptive Ops Checklist

*   Open/forward ports:
    *   TCP `1337` (WebSocket lobby)
    *   UDP `1338` (public endpoint probe)
    *   UDP `1339` (opaque relay fallback)
    *   TCP `8080` (health endpoint; can be LAN-only)
*   Set DNS/hosts failover for `battlezone98mp.webdev.rebellion.co.uk` to your server IP if official host is unavailable.
*   Run periodic health checks against `http://<server>:8080/health`.
*   Use `http://<server>:8080/protocol` to inspect supported message surface.
*   Use `http://<server>:8080/captures` to inspect recent WS/UDP traffic summaries.
*   Keep backups of `data/server_state.json`.
*   Use `--admin-key` and keep it private.

## BZRNet Protocol Probe (`probe.py`)

A Python/Tkinter utility included in this repo to assist with packet analysis and emulation.

### Features
*   **UDP Client**: Probe the official server with custom hex payloads.
*   **Fake Server**: Listen on port 1338 to intercept game client traffic.
*   **Log Parser**: Helper to extract and analyze hex dumps from game logs.

## Reverse Engineering Process

The following workflow outlines how to use `probe.py` and Wireshark to decipher the protocol.

### Phase 1: Establish a Baseline (Wireshark)
Before trying to emulate the server, you need to know what a successful conversation looks like.

1.  **Prepare Wireshark:**
    *   Start Wireshark and select your active network adapter.
    *   Apply the display filter: `udp.port == 1338`.
2.  **Capture Traffic:**
    *   Launch Battlezone 98 Redux.
    *   Connect to the official multiplayer lobby/server.
    *   Once connected (or attempting to connect), stop the capture.
3.  **Analyze the Handshake:**
    *   Look for the very first packet sent by your computer (Source) to the server (Destination). This is likely the "Client Hello" or "Auth Request".
    *   Look for the server's immediate response.
    *   *Note:* Keep this capture open. You will need to copy the Hex bytes from these packets later.

### Phase 2: Validate Packet Structure (Probe Client)
Use the `probe.py` tool to see if you can trigger a response from the official server manually. This checks if packets are static or require dynamic session data.

1.  **Run the Tool:**
    *   Execute `python probe.py`.
2.  **Configure UDP Client:**
    *   Go to the **UDP Client (Probe)** tab.
    *   **Target IP:** `68.183.35.188` (Official Server).
    *   **Port:** `1338`.
    *   **Hex Payload:** Copy the "Data" payload from the *first* packet you captured in Wireshark (Right-click packet -> Copy -> ...as Hex Stream).
3.  **Send & Observe:**
    *   Click **Send UDP**.
    *   **If you get a response:** The server accepted your packet. This implies the protocol might not be heavily encrypted or time-sensitive at this stage.
    *   **If no response:** The server ignored it. The packet might contain a timestamp, sequence number, or session ID that is now stale.

### Phase 3: Trap the Client (Probe Server)
Now, trick the game into talking to your Python script instead of the real server. This allows you to inspect the game's behavior in a controlled environment.

1.  **Start the Fake Server:**
    *   Go to the **UDP Server (Listen)** tab in `probe.py`.
    *   **Listen Port:** `1338`.
    *   Click **Start Server**.
2.  **Redirect the Game:**
    *   Launch Battlezone 98 Redux with the startup parameter to connect to localhost.
    *   *Example:* `battlezone98redux.exe /server 127.0.0.1:1338`
3.  **Capture the "Hello":**
    *   Watch the **Incoming Packets** log in your tool.
    *   When the game starts, you should see a packet appear (e.g., `RX <- 127.0.0.1:xxxxx | 4002...`).
4.  **Compare:**
    *   Compare this new packet with the one from Phase 1.
    *   **Identical?** The packet is static.
    *   **Different?** The changing bytes likely correspond to a Timestamp, Sequence ID, or Random Nonce.

### Phase 4: Mock the Handshake (Code Modification)
This is the core reverse-engineering loop. You want to trick the game into sending the *second* packet in the sequence.

1.  **Hardcode the Reply:**
    *   Copy the **Server Response** (the 2nd packet) from your Wireshark capture in Phase 1.
    *   Modify `probe.py` inside the `server_loop` method to send this data back whenever it receives data.
    
    ```python
    # Example modification in server_loop
    data, addr = self.server_socket.recvfrom(4096)
    # ... logging code ...
    
    # Mock Response (Replace with actual hex from Wireshark)
    mock_reply = binascii.unhexlify("41020000...") 
    self.server_socket.sendto(mock_reply, addr)
    ```

2.  **Test:**
    *   Restart your Python server and the Game.
    *   If the game sends a **new, different packet** after receiving your reply, you have successfully emulated the first step of the handshake!
    *   Repeat this process for the next packet in the sequence.

### Phase 5: Decode the Data (Log Parser)
Use the **Log Parser** tab to make sense of the hex data as you collect it.

1.  Paste the hex strings into the parser.
2.  Look for patterns:
    *   **0x40 / 0x41**: Often Request/Response pairs.
    *   **Strings**: Player names often appear as plain ASCII or UTF-16.
    *   **Integers**: 4 bytes (e.g., `01 00 00 00` is 1).
3.  Update the `analyze_packet` function in the script as you identify fields.
