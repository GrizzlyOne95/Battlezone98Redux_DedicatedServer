# Battlezone 98 Redux Dedicated Server Project

This repository contains tools and documentation for reverse engineering the Battlezone 98 Redux server protocols (BZRNet). The goal is to develop a custom dedicated server to ensure multiplayer longevity.

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