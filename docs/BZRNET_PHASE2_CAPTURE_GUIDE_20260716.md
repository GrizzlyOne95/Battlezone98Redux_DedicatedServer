# BZRNet Phase 2 Capture Guide

Date: 2026-07-16

## Purpose

The ordinary two-player official-service capture is complete. The March 21
host and joiner files were re-verified by SHA-256 and already establish the
WebSocket control flow, UDP `1338` endpoint discovery, direct peer handshake,
and the common 18-byte BZRNet UDP header.

Do not repeat that run. The remaining high-value capture work is:

1. one forced-relay (`/iprelay`) two-player run to recover `DoP2PRoute`,
   `OnP2PRoute`, and UDP `1339` framing;
2. short, isolated stock-UI failure runs to map official `reasonCode` values.

Deep decoding of the in-match `Net`/`NetPlayer` payload is not required for a
matchmaking replacement or transparent relay. Those datagrams can remain
opaque.

## Known Capture Baseline

| Role | Drive file | SHA-256 |
|---|---|---|
| Host | `Wireshark_IanPC/game1.pcapng` | `c83b79c8a31961afae0ff0af1ec789b2cbd1018c9bf5491a023ce105eeac97c2` |
| Joiner | `Wireshark_KateriPC/Kat_PC.pcapng` | `75006cc7570f44567382dced1b75fe7b5845d1d8b4ac505161a254b5c4429310` |

The raw captures contain Steam authorization tickets, player identifiers, and
private/public endpoint data. Keep all raw artifacts private and commit only
redacted summaries and hashes.

## Capture A: Forced Relay

### Goal

Capture the complete route-allocation sequence and relay traffic from both
clients:

- outbound `DoP2PRoute`;
- inbound `OnP2PRoute`, including `player` and `port`;
- the first UDP datagrams to and from `1339`;
- relay identifiers or envelopes surrounding opaque gameplay payloads;
- retransmission, acknowledgement, keepalive, and shutdown behavior;
- any timeout or fallback behavior if the relay path fails.

### Required setup on both PCs

- Use the same current OpenShim build on both PCs.
- Use the official BZRNet service. Remove `/bzrserver`, `/bzrnetport`, hosts-file
  overrides, and local failover-server redirection.
- Prefer two different Internet connections. The same LAN is acceptable if
  `/iprelay` is confirmed in both launch commands and UDP `1339` appears.
- Synchronize both Windows clocks.
- Start with fresh or backed-up `BZLogger.txt` and OpenShim logs.
- Record the SHA-256 of each PC's `winmm.dll` and save the active `net.ini`.

Disable packet-changing experiments for this protocol run. At minimum, ensure
these effective OpenShim values are used:

```ini
[OpenShimSocket]
EnablePacketReorder=0
SendDup=0
GovernorStart=0
GovernorScan=0
AutoKickRelax=0
Dscp=0
```

The relay capture logger itself is observational. Do not enable peer/socket
filters: the route socket is not known in advance and relay mode intentionally
captures all candidate sockets.

### Start the OpenShim capture

From the current `BZR-OpenShim` checkout on each PC:

```powershell
.\buffer_logger_windows.ps1 -Action Start -RelayCapture
```

The command creates a session directory containing:

- `steam_launch_options.txt`;
- `launch_with_buffer_log.cmd`;
- `README_NEXT_STEPS.txt`.

For Steam, copy the generated launch line into the game's launch options. Add
the stock logging flags after `%command%` if they are not already present:

```text
/iprelay /netpktlog /netlog=3 /bzrnetlog=3
```

Verify that both PCs' final launch commands contain `/iprelay`. Relay mode sets
`BZ_RELAY_CAPTURE=1`, captures up to 2048 bytes per UDP datagram, and enables
IOCP completion capture.

### Start Wireshark

On both PCs:

1. Select the active network adapter only.
2. Start capturing before launching Redux.
3. Prefer no capture filter. If disk space is constrained, use:

```text
tcp port 1337 or udp
```

Do not restrict capture to ports `1338` and `1339`; peer or preliminary traffic
may use dynamic UDP ports.

### Run sequence

Use PC A as host and PC B as joiner.

1. Start OpenShim relay capture and Wireshark on both PCs.
2. Launch both clients with `/iprelay` and the three stock logging flags.
3. Enter the official multiplayer lounge.
4. PC A creates a passworded game lobby.
5. PC B joins it.
6. Wait 20 seconds after the join.
7. Add a marker on both PCs:

   ```powershell
   .\buffer_logger_windows.ps1 -Action Mark -Message "relay lobby joined"
   ```

8. Launch the match and remain in it for 90-120 seconds.
9. Both players should move, fire weapons, and generate traffic. Avoid unrelated
   workshop downloads or background network tests.
10. Add a second marker while the match is active:

    ```powershell
    .\buffer_logger_windows.ps1 -Action Mark -Message "relay gameplay active"
    ```

11. Exit the match and game normally on both PCs. A normal exit is important
    because OpenShim flushes its in-memory capture ring during shutdown.
12. Stop and save both Wireshark captures.
13. On each PC, collect the OpenShim bundle:

    ```powershell
    .\buffer_logger_windows.ps1 -Action Stop
    ```

### Save these artifacts from each PC

- `.pcapng` from Wireshark;
- generated OpenShim `test_bundles` ZIP and unzipped session directory;
- `BZLogger.txt`;
- `openshim.log`;
- `bz_buffer_log.bin`;
- `bz_buffer_log.meta.txt`;
- `bz_relay_control.jsonl`;
- active `net.ini` and `multi.ini`;
- exact launch command;
- notes containing role, local timezone, and approximate timestamps;
- SHA-256 values for the PCAP, ZIP, `winmm.dll`, and executable.

Suggested names:

```text
PC_A_host_iprelay_20260716.pcapng
PC_A_host_iprelay_20260716_notes.txt
PC_B_joiner_iprelay_20260716.pcapng
PC_B_joiner_iprelay_20260716_notes.txt
```

Do not upload these artifacts to a public repository.

### Immediate success checks

In Wireshark, verify:

```text
udp.port == 1339
```

and:

```text
websocket.payload.text contains "P2PRoute"
```

In `BZLogger.txt`, search for:

```text
Starting RELAY Connect
Completed RELAY Connect
FAILED WAN Connect
```

Not every line must be present, but there must be clear route or relay activity.

In `openshim.log`, confirm the configuration line reports:

```text
enableRelayCapture=1
enableBufferLog=1
bufferLogPayloadBytes=2048
```

Confirm `bz_relay_control.jsonl` contains route-related messages and contains no
`Authorization` records. Confirm `bz_buffer_log.bin` is non-empty and its
metadata sidecar reports capture format version 2.

From `BZR-OpenShim`, create a redacted structural summary:

```powershell
python reverse_engineering/analyze_relay_capture.py `
  <session>\bz_buffer_log.bin
```

For the paired Wireshark files:

```powershell
python reverse_engineering/analyze_bzrnet_capture.py `
  <host.pcapng> <joiner.pcapng>
```

These analyzers intentionally omit tickets, identities, endpoints, and packet
bodies.

### If relay does not appear

Keep the run; a partial route attempt is still useful. Classify it before
retrying:

- `P2PRoute` message present, UDP `1339` absent: preserve everything; this may
  reveal a route-allocation or client-validation failure.
- Neither `P2PRoute` nor UDP `1339` present: verify `/iprelay` was applied on
  both clients and that `enableRelayCapture=1` appears in `openshim.log`.
- Direct peer traffic appears despite `/iprelay`: retry with the PCs on separate
  Internet connections.
- UDP `1339` appears but the match fails: remain connected for about 30 seconds,
  note the exact UI/log failure, then exit normally. This is still a valuable
  protocol capture.

Do not add ad-hoc firewall blocks during the first retry. They can accidentally
block the relay itself and make the result ambiguous.

## Capture B: Official `reasonCode` Mapping

### Goal

Map numeric `reasonCode` values to ordinary client-visible join outcomes.
Success code `0` is already observed. The local server currently uses a
provisional `1`-`6` failure mapping; do not assume those numbers match the
official service.

### Capture method

Enhanced relay logging is not required. Use stock Redux UI actions, Wireshark,
and the three stock log flags:

```text
/netpktlog /netlog=3 /bzrnetlog=3
```

Capture TCP `1337` from before game launch until immediately after the failure.
Use a separate PCAP for each scenario so the result cannot be misattributed.
The raw PCAP still contains an authorization ticket and must remain private.

### Priority scenarios

Run these in order and stop once the UI cannot naturally produce the case.

#### B1: Bad password

1. Host creates a passworded lobby.
2. Joiner selects it and deliberately enters the wrong password.
3. Record the exact UI text and timestamp.
4. Stop after the failure response arrives.

This is the highest-confidence failure case.

#### B2: Locked lobby

1. Host creates an unlocked lobby.
2. Joiner selects it but does not join yet.
3. Host locks the lobby.
4. Joiner immediately attempts to join.
5. Record whether the UI sends a request or prevents the action locally.

If no `DoJoinLobby` is sent, label the run `client_blocked` rather than assigning
a reason code.

#### B3: Full lobby

1. Host creates a lobby and lets the joiner select it.
2. Host reduces the member limit so the host already fills the lobby.
3. Joiner immediately attempts to join.
4. Record whether the server rejects the request or the client blocks it.

#### B4: Lobby removed race

1. Host creates a lobby.
2. Joiner selects it.
3. Host exits/removes the lobby.
4. Joiner immediately attempts to join before the list refresh removes it.

This may recover the official not-found result, but it is timing-sensitive.

Do not synthesize missing IDs, malformed JSON, or arbitrary requests against the
official service. Missing/invalid-ID mappings can remain unresolved until there
is a controlled test surface or an instrumented stock-client replay method.

### Reason-code success criteria

A usable scenario must contain:

- one outbound `DoJoinLobby` attributable to the scenario;
- one inbound `OnLobbyJoined` or `OnFailure` with `success: false`;
- a numeric `reasonCode`;
- the corresponding human `reason` or exact client UI text;
- a timestamp note accurate enough to align both PCs.

Record results in a redacted table only:

| Scenario | Reply verb | `reasonCode` | Human/UI meaning | Confidence |
|---|---|---:|---|---|
| bad password | | | | |
| locked | | | | |
| full | | | | |
| removed/not found | | | | |

Do not paste complete WebSocket `Authorization`, lobby, or user JSON into an
issue, commit, or chat transcript.

## Optional Later Capture: Two-Network Direct Mode

A paired `/ipdirect` run with clients on different public networks would
validate WAN candidate selection and NAT traversal. It is useful but not a
blocker for the current direct-on-LAN plus relay recovery work. Do it only after
Capture A is successful.

## Completion Criteria

Phase 2 capture work is complete when:

1. one paired run contains route control messages plus UDP `1339` from both
   viewpoints, with matching OpenShim buffer logs;
2. relay allocation and datagram framing can be described without exposing
   identities or endpoints;
3. at least the bad-password official `reasonCode` is confirmed, with any other
   naturally reachable failures recorded separately;
4. all raw captures remain private and repository documentation contains only
   redacted summaries and cryptographic hashes.
