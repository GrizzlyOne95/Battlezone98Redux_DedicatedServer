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
*   UDP relay: `0.0.0.0:1339`
*   Health: `http://127.0.0.1:8080/health`
*   Protocol: `http://127.0.0.1:8080/protocol`
*   Captures: `http://127.0.0.1:8080/captures`
*   Lobbies/players: `http://127.0.0.1:8080/lobbies`

### Docker / Compose Deployment

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
*   Private tests with three or more clients can use the opt-in pair-port relay described below.
*   Run `python stock_client_compat_test.py` for in-process checks built around the executable-confirmed client message shapes.

### Private Multi-Client Relay

Start the isolated server with `--relay-host 127.0.0.1 --relay-pair-ports` for
multiple clients on one computer. Each negotiated peer pair gets its own
ephemeral UDP port, advertised to both clients in `OnP2PRoute`. Datagrams leave
through that same socket so the native client's source-endpoint lookup can
distinguish every peer. Complete payloads and packet boundaries are preserved.
Repeated and reversed route requests reuse the port. Leave, expiration, relay
disable and shutdown close the socket; a failed allocation advertises no port.
The shared listener rejects unassociated traffic in this mode.

Run `python relay_pair_ports_test.py` for real UDP tests of three- and four-peer
meshes, all six four-peer pairs in both directions, byte-exact payloads, native
source-port identity, negotiation retries, unrelated senders, and lifecycle
cleanup. The existing stock compatibility matrix (45/45), relay impairment
checks (5/5), and server smoke checks (6/6) also passed on 2026-10-07.

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
