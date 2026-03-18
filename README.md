# Battlezone 98 Redux Dedicated Server Project

This repository contains tools and documentation for reverse engineering the Battlezone 98 Redux server protocols (BZRNet). The goal is to develop a custom dedicated server to ensure multiplayer longevity.

For operations and cutover notes, see `NOTES.md`.
For the ANet-to-BZR comparison and the recommended community failover architecture, see `COMMUNITY_FAILOVER_PLAN.md`.

## New: MVP Dedicated Server (`server.py`)

This repo now includes a runnable compatibility shim that emulates key BZRNet behavior:

*   **WebSocket Lobby Service** on `:1337`
*   **UDP P2P Handshake Shim** on `:1338`
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
*   `DoP2PConnect` / `DoP2PRoute` -> `OnLobbyMemberP2PConnect`
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
*   UDP: `0.0.0.0:1338`
*   Health: `http://127.0.0.1:8080/health`
*   Protocol: `http://127.0.0.1:8080/protocol`
*   Captures: `http://127.0.0.1:8080/captures`

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
battlezone98redux.exe /netpktlog /netlog=3 /bzrnetlog=3 /bzrserver=127.0.0.1 /bzrnetport=1338
```

Notes:

*   The game binary also contains a hardcoded WebSocket URL for BZRNet (`...:1337`), so DNS/hosts override may be needed for full end-to-end local redirection.
*   The UDP shim currently focuses on handshake compatibility and iterative packet probing.
*   A community-hosted matchmaking replacement appears feasible for most cases, but relay-assisted NAT traversal is still an open gap for strict-NAT users.

### Preemptive Ops Checklist

*   Open/forward ports:
    *   TCP `1337` (WebSocket lobby)
    *   UDP `1338` (P2P handshake/probe)
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
