# BZRNet Relay Capture Quickstart

Purpose: capture the **official Redux relay path** so the remaining UDP `1339` association/framing behavior can be reproduced accurately by the community backend.

For full background and fallback cases, see `BZRNET_PHASE2_CAPTURE_GUIDE_20260716.md`.

## Target result

Capture this sequence from both clients:

```text
UDP 1338 endpoint discovery
        ↓
WS DoP2PRoute
        ↓
WS OnP2PRoute
        ↓
UDP 1339 association / relay setup
        ↓
opaque BZRNet gameplay traffic
```

The important unknown is whether UDP `1339` is raw BZRNet, has a relay/session prefix, uses an association handshake, or carries a stable route identifier.

## Recommended setup

Use two PCs on **different Internet connections** if possible:

- **PC A / Host:** normal home Internet.
- **PC B / Joiner:** phone hotspot or USB tether.

Use the **official Rebellion backend**. Remove any `/bzrserver`, `/bzrnetport`, hosts-file override, or local failover-server redirection.

Do **not** add firewall blocking rules for the first run. `/iprelay` is the preferred forcing mechanism and avoids accidentally blocking the relay itself.

Use the same current OpenShim build on both PCs.

Disable packet-changing experiments:

```ini
[OpenShimSocket]
EnablePacketReorder=0
SendDup=0
GovernorStart=0
GovernorScan=0
AutoKickRelax=0
Dscp=0
```

## 1. Start OpenShim relay capture

On both PCs, from the current `BZR-OpenShim` checkout:

```powershell
.\buffer_logger_windows.ps1 -Action Start -RelayCapture
```

Ensure the final Redux launch options contain:

```text
/iprelay /netpktlog /netlog=3 /bzrnetlog=3
```

Verify `/iprelay` is present on **both** PCs before launch.

## 2. Start Wireshark

On both PCs:

- Capture the active network adapter.
- Start capture **before launching Redux**.
- Prefer no capture filter.

If a capture filter is required:

```text
tcp port 1337 or udp
```

Do not restrict capture to only `1338`/`1339`; preliminary peer traffic may use dynamic UDP ports.

## 3. Run the test

Use PC A as host and PC B as joiner.

1. Start OpenShim relay capture and Wireshark on both PCs.
2. Launch Redux on both PCs.
3. Enter the official multiplayer lounge.
4. PC A creates a **passworded game lobby**.
5. PC B joins it.
6. Wait about 20 seconds.
7. Add a marker on both PCs:

   ```powershell
   .\buffer_logger_windows.ps1 -Action Mark -Message "relay lobby joined"
   ```

8. Launch the match.
9. Stay in-game for about 90–120 seconds.
10. Both players should move and fire weapons to generate replication traffic.
11. While gameplay is active, add another marker:

   ```powershell
   .\buffer_logger_windows.ps1 -Action Mark -Message "relay gameplay active"
   ```

12. Continue briefly, then exit the match and Redux **normally** on both PCs.
13. Stop/save both Wireshark captures.
14. Stop OpenShim capture:

   ```powershell
   .\buffer_logger_windows.ps1 -Action Stop
   ```

Normal game shutdown matters because OpenShim flushes its in-memory capture ring during shutdown.

## 4. Immediate success checks

### WebSocket route allocation

Wireshark display filter:

```text
websocket.payload.text contains "P2PRoute"
```

We want to see `DoP2PRoute` and `OnP2PRoute` control messages.

### UDP relay traffic

```text
udp.port == 1339
```

If this shows packets, preserve the run. The most valuable data is:

- first client → relay datagrams;
- first relay → client datagrams;
- several packets in each direction;
- steady-state gameplay traffic;
- idle/keepalive behavior;
- shutdown behavior.

### Endpoint discovery

```text
udp.port == 1338
```

This lets us correlate endpoint discovery with route allocation and relay startup.

### Redux/OpenShim logs

Search `BZLogger.txt` for:

```text
Starting RELAY Connect
Completed RELAY Connect
FAILED WAN Connect
```

Not every line must be present, but there should be clear relay/route activity.

Confirm `openshim.log` reports:

```text
enableRelayCapture=1
enableBufferLog=1
bufferLogPayloadBytes=2048
```

## 5. Save these artifacts from each PC

Keep all raw artifacts **private**. They may contain Steam authorization tickets, player identifiers, and public/private endpoint data.

Collect:

- Wireshark `.pcapng`;
- OpenShim test-bundle ZIP and session directory;
- `BZLogger.txt`;
- `openshim.log`;
- `bz_buffer_log.bin`;
- `bz_buffer_log.meta.txt`;
- `bz_relay_control.jsonl`;
- active `net.ini` and `multi.ini`;
- exact launch options;
- approximate timestamps and which PC was host/joiner.

Suggested layout:

```text
RelayTest_YYYY-MM-DD/
├── PC_A_HOST/
│   ├── host_iprelay.pcapng
│   ├── BZLogger.txt
│   ├── openshim.log
│   ├── bz_buffer_log.bin
│   ├── bz_buffer_log.meta.txt
│   ├── bz_relay_control.jsonl
│   ├── net.ini
│   └── multi.ini
└── PC_B_JOINER/
    ├── joiner_iprelay.pcapng
    ├── BZLogger.txt
    ├── openshim.log
    ├── bz_buffer_log.bin
    ├── bz_buffer_log.meta.txt
    ├── bz_relay_control.jsonl
    ├── net.ini
    └── multi.ini
```

Do **not** commit the raw captures or authorization-bearing logs to a public repository.

## If UDP 1339 does not appear

Preserve the run anyway and classify it:

- **P2PRoute present, UDP 1339 absent:** route allocation/client validation likely failed; still useful.
- **No P2PRoute and no UDP 1339:** verify `/iprelay` was active on both clients and relay capture was enabled.
- **Direct peer traffic despite `/iprelay`:** retry with the PCs on separate Internet connections.
- **UDP 1339 appears but the match fails:** stay connected briefly, record the failure, then exit normally; this is still a high-value capture.

Do not introduce firewall blocking rules until the normal `/iprelay` run has been classified.
