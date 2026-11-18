# Battlezone 98 Redux Dedicated Server

A community-hosted replacement for the **Battlezone 98 Redux BZRNet matchmaking service**. It handles game lobbies, chat, peer discovery, and optional UDP relay so players can connect to a server they control.

This is a **matchmaking/relay server**, not a game simulation host: gameplay still runs between players. The project is unofficial and under active development. LAN and local multi-client scenarios have been tested; internet and difficult-NAT compatibility still need broader validation.

## Getting started

### Windows: one-file download (no Python)

Download `BZRDedicatedServer.exe` from the [latest release](https://github.com/GrizzlyOne95/Battlezone98Redux_DedicatedServer/releases/latest), put it in its own folder, and double-click it. It opens the launcher window described below; started with arguments (`BZRDedicatedServer.exe --help`) it runs headless instead.

### Windows: graphical launcher from source

1. Install Python 3 (3.12 recommended), then install dependencies from the repository directory:

   ```powershell
   python -m pip install -r requirements.txt
   ```

2. Double-click `ServerGUI.bat`, or run `python server.py --gui`.
3. Review the **Setup Guide** and **Access** tabs, choose your relay ports, and click **Start**. The launcher shows the address players should use, live lobbies, connected players, and logs.

For a console-only one-click start, use `StartServer.bat`. It installs missing dependencies, offers once to configure Windows Firewall (Private networks), and enables separate UDP relay ports `1340–1369`.

### Linux and macOS

Install Python 3.9+ (3.12 recommended). For the launcher window you also need Tk: `sudo apt install python3-tk python3-venv` on Debian/Ubuntu, `sudo dnf install python3-tkinter` on Fedora, or the python.org installer on macOS.

- `./ServerGUI.sh` opens the launcher window. On macOS you can double-click `ServerGUI.command` in Finder.
- `./StartServer.sh` runs the console server with separate UDP relay ports `1340–1369`, like `StartServer.bat`.

Both scripts put the dependencies in a private `.venv/` folder the first time if your Python lacks them, so they work on distributions that block `pip install` into the system Python. The launcher's **Setup Guide** shows firewall and port-test commands for the system it runs on.

`python build_package.py` builds the same one-file `dist/BZRDedicatedServer.exe` locally. Pushing a `v*` tag (`git tag v0.2.0 && git push origin v0.2.0`) makes the Release workflow build it and attach it to a GitHub release.

### Command line

```bash
python -m pip install -r requirements.txt
python server.py --relay-pair-ports --relay-pair-port-start 1340 --relay-pair-port-end 1369
```

Run `python server.py --help` for all options. Without the relay flags, the CLI uses a shared UDP relay port; see [Networking](#networking) for the limitation.

### Docker Compose

```bash
cp .env.example .env
docker compose -f docker-compose.yml -f docker-compose.pair-ports.yml up -d --build
```

Edit `.env` before starting. The extra Compose file enables separate relay ports and publishes the configured range (`1340–1369` by default). The base `docker-compose.yml` alone uses shared relay. State is kept in `./data`.

The status port (8080) is published on the host's `127.0.0.1` only. **Do not expose it to the internet.** Its `/access`, `/lobbies` and `/captures` pages show player addresses and bans, so the server answers them only for loopback callers. From the Docker host the request arrives from the bridge network, so add that network to `--health-detail-hosts` (for example `172.16.0.0/12`) if you need those pages there.

## Connecting players

The server PC and players must be able to reach each other over the configured network or VPN.

- **OpenShim:** In the game's **Multiplayer setup** screen, choose **Custom** and enter the server's LAN/VPN/public hostname or IP. Choose **Local** if the game and server run on the same PC. This UI uses the default lobby/probe ports (`1337`/`1338`).
- **Stock Steam/GOG game:** Set the following game launch option, replacing the example address with your server's:

  ```text
  /bzrserver=ws://192.168.1.50:1337/
  ```

  Include the full `ws://` URL, port, and trailing slash. Remove the option to return to the official service.

Steam players need Steam running; GOG players must be signed into GOG Galaxy so the client supplies a player ID. Everyone in a modded match must have matching mod versions.

See [LAN/co-op setup](docs/LAN_COOP.md) for the full checklist.

Optional LAN discovery (`--lan-discovery`, off by default) lets OpenShim browsers find a server on the local network; see [LAN discovery and hosting](docs/LAN_DISCOVERY.md). With a specific `--ws-host <ip>` on Windows it may not receive broadcasts; use `0.0.0.0` for discovery.

## Networking

| Port | Protocol | Purpose |
| --- | --- | --- |
| `1337` | TCP | WebSocket lobby and matchmaking |
| `1338` | UDP | Public-endpoint discovery |
| `1339` | UDP | Shared relay/fallback listener |
| `1340–1369` | UDP | Optional per-peer-pair relay range (configurable) |
| `8080` | TCP | Health, status, and local administration; **keep private** |

Allow the game ports through the server firewall. For internet access, forward TCP `1337`, UDP `1338–1339`, and the entire configured UDP peer-pair range to the server PC. VPNs such as Tailscale can avoid public forwarding for private groups.

The default shared relay can become ambiguous when a player needs to relay to multiple peers. **Enable separate UDP port per peer pair** in the GUI, use `StartServer.bat`, or pass `--relay-pair-ports` with a fixed range. Four fully relayed players require six pair ports; the sample range has 30 ports shared across all active lobbies.

The official relay association protocol and strict/symmetric-NAT behavior are not yet fully reproduced or qualified. A healthy lobby connection alone does not guarantee that every network pairing can launch a match.

## Access and privacy

- The GUI's **Access** tab supports open, approval, and allowlist modes, along with bans, mutes, and player management. The CLI equivalents include `--access-mode approve|allowlist` and `--pin-ips`.
- `--identity-policy optional|required|off` controls optional trust-on-first-use community identity keys. Stock clients do not supply these keys; `optional` is the default.
- `--redact-addresses` limits player address visibility to their own match, themselves, and admins. It is off by default pending wider network compatibility testing.
- Lobby metadata and moderation state are saved in `data/server_state.json`; active client connections are not restored after restart. Audit logs and GUI settings are stored locally. If the state file cannot be read, the server keeps it as `server_state.json.corrupt-<time>` beside the new one so bans and approvals can be restored.
- Lobby passwords stay on the server: lobby lists show `*` for a passworded lobby, and joins are checked against the real password.

**Security limitation:** Steam/GOG tickets cannot be independently verified by this community server. ID approval and IP pinning reduce casual unwanted access but do not prove identity; community keys protect continuity only after first registration. Use a private VPN for a friends-only server, and protect the state, settings, and admin credentials.

## Testing and technical documentation

With the server running, check its local status at `http://127.0.0.1:8080/health` and run:

```bash
python smoke_test.py
```

Additional checks: `python stock_client_compat_test.py`, `python protocol_parity_test.py`, `python robustness_test.py`, and `python server_gui_test.py`. The Tests workflow runs these on Windows, macOS and Linux. `python lan_discovery_test.py` is a manual test (no CI runs it). These do not replace testing with real players across different networks.

More detail lives in dedicated documents:

- [LAN and Campaign Reimagined co-op guide](docs/LAN_COOP.md)
- [BZRNet architecture, ports, and protocol behavior](BZRNET_MULTIPLAYER_SERVER_GUIDE.md)
- [Protocol message contract and evidence](PROTOCOL_CONTRACT_MATRIX.md)
- [Community failover architecture and remaining gaps](COMMUNITY_FAILOVER_PLAN.md)

Licensed under [MIT](LICENSE).
