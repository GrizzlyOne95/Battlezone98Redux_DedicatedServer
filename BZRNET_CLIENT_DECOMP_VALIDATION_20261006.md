# BZRNet client decompile validation (2026-10-06)

Source: Ghidra decompile of the GOG `battlezone98redux.exe` 2.2.301 client.
Every claim below is read from the client's own code and is PROVEN-native
unless it says otherwise. Addresses are GOG VAs.

This pass resolves most of the decompile-resolvable rows in the unknowns
register (`BZRNET_PROTOCOL_ARCHAEOLOGY_20260825.md` §14). It also finds one
real limitation in the community relay (§2).

## 1. UDP 1339 relay: no envelope

The client sends the relay exactly the bytes it would send a peer directly.

- `OnP2PRoute` is handled in the WebSocket dispatcher (`0x006BF2A0`, around
  `0x006C2790`). The relay endpoint is built from the address of the lobby
  server the client is already connected to (`this+0x328`) with the port
  replaced by `content.port`. Nothing else in the message is used, so there
  is no token or allocation id.
- `0x0075F370` (relay response) sets the peer's connection state to 6,
  stores that endpoint as the peer's address, and resets the retry budget to
  30.
- The connection tick (`0x0075AAF0`) then sends ordinary kind-3 connect
  packets (`0x0075A710`, the same builder used for LAN and WAN) to that
  endpoint once per tick. State 7 sends kind 4. There is no relay-only packet
  type, prefix or keepalive anywhere in the send path.
- After the handshake, game traffic to a relayed peer goes through the same
  writer (`0x0075BEB0`) to the same stored endpoint.

Consequence: endpoint association is the whole protocol. The open
"allocation envelope, keepalive, timeout" row can close. Keepalive is the
game-plane `PP`/`KA` traffic. The client gives up a relay connect after 30
ticks with "FAILED RELAY Connect".

## 2. Relay limitation: one relay endpoint per client

The receiving client files each connected peer under the peer's source
endpoint. Completing a handshake (`0x0075D800`, kind-5 case) stores
`map[conn.endpoint] = conn`. Later data packets are matched to a peer only
by that map, and a packet from an unknown source that is not a connect packet
is dropped ("Not a connected client or a connection packet").

`server.py` relays every route through the single 1339 socket. That works
when each client has at most one relayed peer. It breaks when a client
relays to two or more peers:

- **Outbound:** client A sends to `relay:1339` for both B and C. The relay
  sees the same source for two routes, `_match_sender` finds two exact
  matches, and it drops the packet as ambiguous.
- **Inbound:** B and C would both reach A from `relay:1339`. A can file only
  one peer under that endpoint, so the second handshake overwrites the
  first.

Because the client takes the port from `OnP2PRoute.port`, the official relay
can give each route its own port, and the stock client supports that as is.
**Recommended fix:** allocate one relay port per route (a small port range,
for example 1339–1369), so every relayed peer appears to the client at a
distinct `relay:port`. Routing inside one route's socket then needs only to
tell its two members apart. Operators would forward the range instead of a
single port. This hits only games where one player needs relay to two or
more others, and has not been seen live.

## 3. Transport header, settled

| Field | Meaning |
|---|---|
| byte 0 | `0x80` reliable, `0x40` final fragment / unfragmented (unchanged) |
| byte 1 | kind = low nibble. Writers store the kind into a zeroed byte, so the high nibble is always 0 |
| 2..9 | send time, u64 BE |
| 10..13 | **sequence**: sender's `conn+0x88`, the next reliable number |
| 14..17 | **acknowledgement**: sender's `conn+0x84`, the next sequence it expects from the receiver |

Kinds, from writer and reader sites:

| Kind | Meaning | Sent by |
|---|---|---|
| 0 | data (game payload) | `0x0075BEB0` (header byte 1 left 0) |
| 2 | discovery (UDP 1338) | unchanged |
| 3 | connect request, NUL-terminated id string at byte 18 | `0x0075A710` |
| 4 | connect reply | `0x0075A710` |
| 5 | connect complete | `0x0075A710` |
| 6 | **resend request** (gap seen) | `0x0075A490` |
| 7 | **standalone ack** | `0x0075A490` from the tick when an ack is pending |

`seq_b` role (was CORRELATED): it is a cumulative acknowledgement. On every
inbound packet the receiver pops its resend queue while the queued
sequence < the packet's bytes 14..17 (`0x0075F9E0` loop in `0x0075D800`).

Sequence rules:

- `conn+0x88` goes up once per **reliable** fragment. Unreliable packets
  carry the current value without advancing it, so they repeat the next
  reliable number. Connect packets send 0 at bytes 10..13 and the sender's
  `+0x88` at bytes 14..17.
- The receiver accepts a packet only when its sequence equals `conn+0x84`
  (connect kinds are exempt). Reliable packets then advance `+0x84` and mark
  an ack pending. Anything else is dropped and answered with kind 6, which
  carries the receiver's expected number.
- Because unreliable packets carry the next reliable number, **one lost
  reliable fragment also drops every unreliable packet from that peer until
  the resend arrives** (head-of-line blocking across both classes).
- Kind 6 on the sender makes the resend timer due right away. Otherwise
  unacked reliable fragments are resent from the queue about 2.5 s
  (`0x9C4` ms) after the last flush, in a size-limited batch logged as
  "TRY Sent".
- Rollover: plain u32 arithmetic with `!=` and `<` compares, and no wrap
  handling. Unreachable in a real match.

## 4. Per-peer connection states

`*conn` at `0x0075AAF0` / `0x0075D800`:

| State | Meaning | Tick budget |
|---|---|---|
| 0 | idle / failed | — |
| 1 | LAN connecting (sends kind 3) | 15 |
| 2 | LAN completing | 10 |
| 3 | WAN connecting | 30 |
| 4 | WAN completing | 10 |
| 5 | relay requested (`DoP2PRoute` sent, waiting for `OnP2PRoute`) | — |
| 6 | relay connecting (kind 3 to relay endpoint) | 30 |
| 7 | relay completing (kind 4) | 10 |
| 8 | connected ("Completed %s Connect ... using address %s") | — |

A failed WAN attempt falls through to relay. `DAT_00946708` (forced relay)
skips straight to state 5.

## 5. `EMPState` (lobby screen) values

From the lobby screen status switch (`0x00799D70`, `multi_message` strings):

| Value | Shown as |
|---|---|
| 0 | "Outside" |
| 1 | "Entering Lounge" |
| 2 | "In Lounge" |
| 3 | "Enterring" (joining a lobby; stock typo) |
| 4 | in lobby ("Entered NO LOBBY" when the lobby id is unset) |
| other | "BAD STATE" |

## 6. Auto-kick and bandwidth defaults

Initial values of the globals read from the exe. `0x0056FD20` loads them from
`net.ini [Net]` with these defaults:

| Key | Default |
|---|---|
| MaxPing | 700 |
| MaxPingsLost | 20 |
| UpCount / DownCount | 10 / 5 |
| MinBandwidth / MaxBandwidth | 1000 / 4000 |
| AutoKickStart | 10000 |
| AutoKickPing | 1000 |
| AutoKickLoss | 50 |
| AutoKickTime | 10000 |
| LimitLowNPPI / LimitHiNPPI | 50 / 333 |
| DivisorMPPI2NPPI / DivisorPing2NPPI | 3 / 3 |

## 7. Game-plane messages that were INFERRED/UNKNOWN

| Tag | Meaning | Evidence |
|---|---|---|
| `la` / `lu` | **ally / un-ally**: `[tag][my team i32][other team i32]`, 10 bytes. The receiver sets or clears bit `team` in the team's alliance mask (`+0x17C`) | Sent by the `/ally N` chat command handler `0x0046CED0` ("Ally with team %d" / "UnAlly with team %d") |
| `PK` | **kill credit** to a remote killer: `[tag][killer team u8]`, 3 bytes, sent only to that player. The receiver adds 1 to local stat slot 15 | `0x00577290` (kill bookkeeping: victim deaths `+0x6C`, killer kills `+0x70` unless allied) |
| `ns` | **snipe**: a client whose `SniperShell` hit a pilot inside the cockpit volume tells the craft's owner. The receiver kills the pilot and hands the empty craft to the shooter's team | Sender is `SniperShell` vtable slot 5 (`0x005D6E00`); receiver `0x00573AE0` |
| `RG` | **powerup pickup request**: `[tag][powerup handle][toucher handle]`, sent to the host by a client whose craft touched a powerup. The host applies the pickup | Sender is slot 18 of `PowerUp`/`AmmoPowerup`/`HealthPowerup`/`WeaponPowerup`/`CameraPod`/`ScrapDropoff` (`0x005A9EA0`) |
| `PT` | **deathmatch time remaining**: `[tag][seconds left i32]`, 6 bytes, reliable broadcast from the host every 10 s and when it reaches 0 | `MultDMEvent` vtable slot 7 (`0x0056BCD0`). The 0x78/0x3C constants are the display's minute/second split |

## 8. Still open

- `PZ` (seen once in March captures): no sender or dispatcher case for
  `0x5A50` exists in this build, so it was not a game-plane tag from this
  client. It was probably payload bytes misread as a marker.
- Host migration: `NotifyNowHost` exists but no migration packet family was
  found. Unchanged.
- Object-id assignment and the extra IPv6 discovery byte: not looked at in
  this pass.
- Official reasonCodes 1/2/3/6/7: these are server behaviour, so the client
  decompile cannot settle them. Capture B is still the way.
