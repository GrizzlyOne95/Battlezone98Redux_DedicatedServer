# Battlezone 98 Redux Dedicated Server

A community-hosted replacement for the **Battlezone 98 Redux BZRNet matchmaking service**. It handles game lobbies, chat, peer discovery, and optional UDP relay so players can connect to a server they control.

This is a **matchmaking/relay server**, not a game simulation host: gameplay still runs between players. The project is unofficial and under active development. LAN and local multi-client scenarios have been tested; internet and difficult-NAT compatibility still need broader validation.

## Getting started

### Windows: graphical launcher

1. Install Python 3 (3.12 recommended), then install dependencies from the repository directory:

   ```powershell
   python -m pip install -r requirements.txt
   ```

2. Double-click `ServerGUI.bat`, or run `python server_gui.py`.
3. Review the **Setup Guide** and **Access** tabs, choose your relay ports, and click **Start**. The launcher shows the address players should use, live lobbies, connected players, and logs.

For a console-only one-click start, use `StartServer.bat`. It installs missing dependencies, offers to configure Windows Firewall, and enables separate UDP relay ports `1340–1369`.

A standalone Windows package can also be built with `python build_package.py`. It produces `BZRServerGUI.exe` and `BZRServer.exe` without requiring Python on the destination PC.

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

**Do not expose HTTP port 8080 to the internet.** The Compose configuration publishes it by default; restrict it with a host firewall or bind it to a trusted interface.

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
- Lobby metadata and moderation state are saved in `data/server_state.json`; active client connections are not restored after restart. Audit logs and GUI settings are stored locally.

**Security limitation:** Steam/GOG tickets cannot be independently verified by this community server. ID approval and IP pinning reduce casual unwanted access but do not prove identity; community keys protect continuity only after first registration. Use a private VPN for a friends-only server, and protect the state, settings, and admin credentials.

## Testing and technical documentation

With the server running, check its local status at `http://127.0.0.1:8080/health` and run:

```bash
python smoke_test.py
```

Additional checks: `python stock_client_compat_test.py`, `python protocol_parity_test.py`, and `python server_gui_test.py`. These do not replace testing with real players across different networks.

More detail lives in dedicated documents:

- [LAN and Campaign Reimagined co-op guide](docs/LAN_COOP.md)
- [BZRNet architecture, ports, and protocol behavior](BZRNET_MULTIPLAYER_SERVER_GUIDE.md)
- [Protocol message contract and evidence](PROTOCOL_CONTRACT_MATRIX.md)
- [Community failover architecture and remaining gaps](COMMUNITY_FAILOVER_PLAN.md)

Licensed under [MIT](LICENSE).
