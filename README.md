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
retransmissions. Its default gate flags at least 20 type-0, zero-expected
sequence rejections across four active one-second bins within a five-second
window. Exit status is 0 when that pattern is absent, 1 when it occurs, and 2
when logs, peer connections or observation time are incomplete. This is an
explicit, configurable rejection-pattern gate, not a measured loss percentage.
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
history, with authentication credentials and passwords redacted. The relay
JSONL records every routed datagram and unroutable, socket-error and route
lifecycle events. Routed records include the peer-pair/lobby identity,
advertised relay socket, nanosecond wall timestamp, opaque datagram SHA-256 and
first transport header. `--relay-trace-payloads` also copies the complete opaque
datagram as hex, without decoding gameplay payloads or changing forwarding.
It is off by default and adds logging overhead. Both traces share a run ID;
their raw files, endpoints and game payloads stay outside Git.

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
