# OpenShim LAN hosting and discovery

The `agent/lan-host-discovery` workstream adds an opt-in discovery service for
OpenShim's Host LAN and Find LAN controls. The existing lobby and game packet
protocols remain in use. Discovery advertises a lobby service; the player then
chooses or creates a game in the normal Redux lobby.

## Standalone server

Add `--lan-discovery --lan-name "My Battlezone LAN"` to the ordinary server
command. The responder binds UDP 21337 on the same IPv4 address as `--ws-host`.
The advertised lobby port is `--ws-port`, including a nondefault port.
`--lan-discovery-port` exists for isolated tests or explicit configurations;
ordinary OpenShim browsers use 21337.

Limitation: because the responder binds the `--ws-host` address, a specific
`--ws-host <ip>` on Windows may not receive broadcast queries. Use
`--ws-host 0.0.0.0` when LAN browsers need to discover the server.

Discovery defaults to **off**. It replies only to RFC 1918, loopback, and IPv4
link-local sources, and sends each reply to the UDP source address and port.
It never advertises an arbitrary address from a packet. There are per-source
and global response limits with a bounded source cache. Discovery is not an
identity or trust mechanism; host names and addresses are untrusted display data.

Allow the server through the local firewall on the private network. Discovery
cannot cross isolated Wi-Fi clients, routed subnets, or adapters that discard
broadcasts; typing the host's LAN address remains the fallback. This work does
not remove Redux's Steam/Galaxy sign-in requirements.

## Helper payload

Build with an installed **Windows 32-bit Python 3.10+**:

```powershell
python build_lan_helper.py --python C:\Python312-32\python.exe
```

The build checks both interpreter architecture and the resulting i386 PE.
`dist/OpenShimLAN.zip` contains `tools/lan/BZRServer.exe`, a source revision/hash
manifest, this guide, and the project license. Extract that layout into the game
root for OpenShim. Python is not needed on the player's PC. The existing general
`build_package.py` one-file package (`BZRDedicatedServer.exe`, with the launcher window) remains separate; this helper is built without it.

The payload also carries `tools/lan/licenses/python/LICENSE.txt`, the selected
websockets distribution's license under `licenses/websockets/`, and PyInstaller's
licensing terms (including its bootloader exception) under `licenses/pyinstaller/`.
The builder copies these documents byte-for-byte from the selected x86 runtime
and package metadata, fails if a required notice is absent, and records their
hashes and runtime versions in the manifest. Keep this licenses subtree with
the helper when staging or distributing it.

For an already prepared portable x86 environment containing PyInstaller and
the required websockets version, add `--existing-environment`. That path creates
no venv and installs no packages. It supports keeping build tools entirely under
the ignored `build/` directory without registering a Python installation.

OpenShim owns the hidden helper it starts and uses a dedicated scratch state
file. Its launch contract is:

```text
BZRServer.exe --lan-discovery --lan-managed --stdin-control --health-port 0 --state-file <scratch-state.json> --audit-log <scratch-audit.jsonl>
```

It may also set the bind addresses, lobby/probe/relay ports, discovery name, and
relay settings. `--lan-managed` requires discovery, stdin control, health port
zero, and an explicit nondefault state file. Once the lobby, probe/relay (when
enabled), and discovery listeners are all bound, stdout emits exactly:

```text
BZRLAN_READY <websocket-port>
```

The line is ASCII with an LF terminator and is flushed immediately. Diagnostics
go to stderr. A startup bind failure emits no readiness line. Writing `stop` to
stdin or closing stdin stops the service gracefully and saves state. Stopping a
host disconnects its lobby users; OpenShim must keep the helper alive throughout
the session, including while the pre-lobby screen is closed.

## Discovery wire contract (version 1)

This is a new OpenShim service protocol, not a recovered stock game packet.
All byte offsets below are zero-based.

| Datagram | Bytes | Contents |
|---|---:|---|
| Query | 0–7 | ASCII `BZRLAN1?` |
| Query | 8–15 | Eight opaque requester nonce bytes |
| Query | 16–95 | Eighty zero bytes; total datagram length exactly 96 |
| Reply | 0–7 | ASCII `BZRLAN1!` |
| Reply | 8–15 | The unchanged query nonce |
| Reply | 16–17 | WebSocket port, unsigned 16-bit, big-endian, 1–65534 |
| Reply | 18 onward | 1–64 printable ASCII name bytes, without a terminator |

Replies are 19–82 bytes, smaller than the request. The client verifies magic,
length, nonce, port and name, and connects to the UDP source IPv4 address plus
the advertised port. The UDP reply source port is the discovery port. An old
scan's replies cannot populate a later scan with a new nonce.

## Validation boundary

`python tests/lan_discovery_test.py -v` checks malformed queries, local-source ranges,
nonce/port encoding, response size, rate/cache bounds, defaults/CLI guards,
actual loopback UDP and WebSocket readiness, stdin/EOF shutdown, released socket
ports, and discovery bind failure. These are host tests. Physical two-PC
broadcast discovery, native gameplay, Windows firewall prompts, Steam, GOG,
Proton and Wine remain separate acceptance gates.

Set `BZR_LAN_HELPER_EXE` to the absolute path of the built `BZRServer.exe` to
run the same process/socket lifecycle tests against the frozen Win32 helper.
