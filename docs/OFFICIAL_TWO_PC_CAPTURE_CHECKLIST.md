# Official Two-PC Capture Checklist

Date: 2026-03-18

> Status (2026-07-16): the clean end-to-end run in this checklist was completed
> by the March 21 host/joiner captures and re-verified by SHA-256. Do not repeat
> Run 1. Use `BZRNET_PHASE2_CAPTURE_GUIDE_20260716.md` for the remaining forced
> relay and `reasonCode` work.

## Goal

Capture one or more clean sessions against the official BZRNet service so we
can finish reverse engineering the matchmaking and pre-P2P server behavior.

This checklist is intentionally practical. Follow it as written and keep the
artifacts.

## What To Install

On both PCs:

- Wireshark
- Battlezone 98 Redux

Optional but useful:

- 7-Zip for packaging artifacts
- a text editor to note event timestamps

## Best Test Topology

Best:

- PC A on one public Internet connection
- PC B on a different public Internet connection

Acceptable:

- both PCs on the same home LAN

Avoid for this capture:

- localhost redirection
- hosts overrides
- custom failover server

This run should target the official service only.

## Before You Start

On both PCs:

1. Sync the system clock as closely as possible.
2. Close background apps that spam the network if practical.
3. Back up or clear the current `BZLogger.txt`.
4. Prepare a folder for this run.

Suggested artifact folder names:

- `PC_A_run1_official`
- `PC_B_run1_official`

## Launch Command

Use this on both PCs:

```bat
battlezone98redux.exe /netpktlog /netlog=3 /bzrnetlog=3
```

Do not add:

- `/bzrserver=...`
- `/bzrnetport=...`

For this capture, let the stock game talk to the official backend normally.

## Wireshark Setup

On both PCs:

1. Open Wireshark.
2. Select the active network adapter only.
3. Start capture before launching the game.

Recommended capture filter:

- none

If disk space is a concern, use:

```text
tcp port 1337 or udp
```

Do not capture-filter only to UDP `1338` or `1339`. Actual gameplay traffic may
move to dynamic UDP ports after matchmaking completes.

Useful display filters after the fact:

```text
tcp.port == 1337 || udp.port == 1338 || udp.port == 1339
```

```text
ip.addr == 68.183.35.188
```

```text
tcp.port == 1337 || udp
```

## Run 1: Clean End-to-End Match

This is the highest-value capture.

### Sequence

1. Start Wireshark capture on both PCs.
2. Launch Battlezone 98 Redux on both PCs with the logging flags above.
3. Enter multiplayer on both PCs.
4. PC A creates a game lobby.
5. PC B joins PC A's lobby.
6. Wait 20-30 seconds in the lobby.
7. Change a few lobby/player settings if available:
   - team
   - vehicle
   - ready state
8. Launch the match.
9. Stay in match for 60-120 seconds.
10. Move around and exchange a little gameplay traffic.
11. Return to lobby if possible, or exit cleanly.
12. Stop Wireshark capture on both PCs.

### What To Write Down

On both PCs, note approximate local clock times for:

- game launch
- click multiplayer
- websocket connected if visible in logs later
- PC A creates lobby
- PC B joins lobby
- ready state changes
- launch clicked
- match begins
- match ends
- return to lobby or game exit

Even rough timestamps help align both logs and both packet captures.

## Run 2: Lobby Control / Admin Behavior

Do this if time allows.

### Sequence

1. Start new Wireshark captures on both PCs.
2. Repeat lobby creation and join.
3. Exercise lobby-control actions:
   - lock lobby
   - unlock lobby
   - change member limit
   - transfer owner if possible
   - kick user
4. Exit cleanly.
5. Stop captures.

This run is for WS admin and lobby-delta fidelity, not gameplay traffic.

## Run 3: NAT / Relay Failure Path

Only do this if you can reproduce a difficult network path.

Best candidates:

- one PC on mobile tether
- one PC behind stricter NAT
- a known user who has relay fallback issues

The goal is to capture:

- `FAILED WAN Connect`
- `Starting RELAY Connect`
- `Completed RELAY Connect`
- any UDP on `1339`

## Files To Save

From each PC, keep:

- Wireshark capture `.pcapng`
- `BZLogger.txt`
- exact launch command used
- a short timestamp note file

Suggested filenames:

- `PC_A_run1_official.pcapng`
- `PC_A_run1_BZLogger.txt`
- `PC_A_run1_notes.txt`
- `PC_B_run1_official.pcapng`
- `PC_B_run1_BZLogger.txt`
- `PC_B_run1_notes.txt`

## Where `BZLogger.txt` Usually Lives

Typical local path:

- `%USERPROFILE%\Documents\Battlezone 98 Redux\BZLogger.txt`

## What We Need From The Capture

The most important questions this should answer are:

1. What is the exact WebSocket message ordering on TCP `1337`?
2. What are the exact UDP bootstrap request/reply shapes on `1338`?
3. What metadata is exchanged before launch?
4. Does the server keep participating after peers connect, or does traffic go
   mostly quiet on the WS side?
5. When relay fallback happens, what messages and packets precede `1339` use?

## Post-Capture Triage

After the run, do not edit the raw artifacts.

Instead:

1. Copy them to a safe folder.
2. Zip each PC's artifact folder.
3. Preserve original filenames and times if possible.

## Quick Sanity Checks

Before handing artifacts off, quickly confirm:

- `BZLogger.txt` contains `Websocket established!`
- `BZLogger.txt` contains `Authorization`
- the `.pcapng` is not zero bytes
- the capture duration covers lobby plus match

## Notes

- Same-LAN captures are still useful.
- Different-network captures are better.
- Clean, boring runs are more valuable than chaotic ones.
- One good end-to-end two-PC official run is enough to close a lot of the
  remaining uncertainty.

