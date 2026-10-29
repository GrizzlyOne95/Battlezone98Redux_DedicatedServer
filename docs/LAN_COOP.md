# LAN co-op on your own server

How to play BZR multiplayer or Campaign Reimagined co-op on a home network, with one PC running
this server.

## Server PC

1. Start the server. It listens on every network interface by default:

   ```
   cd Battlezone98Redux_DedicatedServer
   python server.py --state-file C:\BZRCoop\server\state.json --relay-pair-ports --relay-pair-port-start 1340 --relay-pair-port-end 1369
   ```

   Ports: TCP 1337 (lobby), UDP 1338 (handshake), UDP 1339 (shared relay listener), and UDP 1340–1369
   (separate relay peer pairs). The 8080 status page stays
   local-only (http://127.0.0.1:8080/status) and clients never use it.

   Or double-click `ServerGUI.bat` (same as `pythonw server_gui.py`) for a window with
   Start/Stop/Restart, the live log, connected players, open lobbies, and the LAN address players
   should use. Enable **Separate UDP port per peer pair**, set its range to 1340–1369 (or your chosen
   accessible range), and set the state file there once. Settings are remembered in `server_gui.json`.

   Separate pair ports prevent shared-relay ambiguity when one player has multiple relayed peers.
   The CLI and existing launcher settings keep legacy shared mode until explicitly enabled.
   A fully relayed `n`-player lobby needs `n*(n-1)/2` ports; four players need six. The default GUI
   range supplies 30 ports total across all active lobbies. Occupied ports reduce that capacity.
   An exhausted range rejects a new route with a clear error while existing routes keep working.

2. Find the PC's LAN address with `ipconfig` (the `Ethernet` or `Wi-Fi` IPv4 address, for example
   `192.168.0.44`). Give this PC a DHCP reservation in the router so the address never changes.

3. Firewall: Windows must allow inbound traffic to the Python that runs the server. If Windows
   asked "Allow access?" the first time and you said yes, you're done. Otherwise run this once in an
   admin PowerShell:

   ```
   New-NetFirewallRule -DisplayName "BZR server TCP" -Direction Inbound -Protocol TCP -LocalPort 1337 -Action Allow -Profile Private
   New-NetFirewallRule -DisplayName "BZR server UDP" -Direction Inbound -Protocol UDP -LocalPort 1338-1369 -Action Allow -Profile Private
   ```

   Match the rule to your configured pair-port range if it differs. The network must be set to
   **Private** (Settings > Network > Properties), not Public.

## Every PC (the server PC too, if someone plays on it)

1. Point the game at the server. Pick one:

   - **Launch option (stock game, no mods needed).** Add this to the game's launch options, using
     the server's LAN IP:

     ```
     /bzrserver=ws://192.168.0.44:1337/
     ```

     Steam: right-click the game > Properties > Launch Options. GOG Galaxy: game > Manage
     installation > Configure > Additional executables, or a desktop shortcut whose target ends
     with the option. Type it exactly, including `ws://`, the port, and the closing `/`: a bare
     `/bzrserver=192.168.0.44` connects to nothing. The server launcher shows the line ready to copy.
     The game sends its UDP handshake to the same IP on the lobby port + 1 (1338), so keep the
     server's default ports.
   - **OpenShim:** In the game's **Multiplayer setup** screen, choose **Custom** and enter this PC's LAN IP, or choose **Local** if the game runs on the server PC. The setup screen uses the default TCP 1337 / UDP 1338 ports; use the full `/bzrserver=` launch option above for nonstandard ports.

   Use the LAN IP on the server PC as well, not `127.0.0.1`.
2. Every PC must run the **same build** of each mod in use. For CR, copy the server PC's
   `mods\3686673790` folder to each PC: `<game>\mods\3686673790` on GOG, or
   `steamapps\workshop\content\301650\3686673790` on Steam. If the files differ, you get map or mod
   mismatches or desyncs.
3. Launch the game. GOG copies need GOG Galaxy running and signed in: the player's id is their
   GOG Galaxy id, and without a Galaxy sign-in the game has no id to send. Steam copies need Steam
   running, because a Steam login without a ticket is rejected.

## Playing

1. Multiplayer > Internet. Everyone should see the same lobby list, served by your PC.
2. One player clicks **Create Game** and picks the map, for example *CR: Eagle's Nest 1 Coop*. The
   others click **Join Game**.
3. The host clicks **Launch** once everyone is in. CR co-op missions need everyone to start
   together: a player who joins late means restarting the mission.

To go back to official online play, remove the `/bzrserver=` launch option, or select **Rebellion** in OpenShim's Multiplayer setup screen.

## Over the internet (untested)

Not yet tested end to end over the internet. The steps are the LAN ones plus:

1. Check you can receive inbound traffic: the WAN IP on the router's status page must match
   what whatismyip.com shows. If they differ you are behind carrier-grade NAT and port forwarding
   cannot work; use Tailscale instead (below).
2. Enable pair mode with bounded UDP ports as above. On the router, forward TCP 1337, UDP 1338–1339,
   and the entire configured pair range (1340–1369 in this example) to the server PC's LAN IP,
   preserving port numbers. Ephemeral pair ports (`0`/`0`) do not fit ordinary static WAN forwarding.
   Never forward 8080.
3. In the launcher's **Internet (WAN)** box, press **Detect** (asks api.ipify.org for your public IP)
   or type a dynamic-DNS name, then send players the **Copy option** line, for example
   `/bzrserver=ws://203.0.113.25:1337/`. Players inside your house keep the LAN line.
4. Check: from outside, `Test-NetConnection <public-ip> -Port 1337` succeeds; the launcher log shows
   `UDP RX` from the player's public IP; relay packet counts rise once a game is joined. If players
   stall at connecting, look at `pairPortRange`, `pairPortAllocationFailuresTotal`,
   `ambiguousDropsTotal` / `unmappedDropsTotal` on
   http://127.0.0.1:8080/status.

The server cannot validate Steam/GOG tickets, so anyone who finds the open port can claim any ID.
In the launcher's **Access** tab set *Who can join* to **Approve new players** (each newcomer waits
for you to press Approve) or **Allowlist only**, and tick IP re-approval if your friends' IPs are
stable. That keeps strangers out, not a determined impersonator. Open the forwards for test
sessions and close them afterwards.

**Tailscale (simplest):** everyone joins your tailnet and uses the server's Tailscale address
(the launcher labels it `Tailscale/VPN`) in the launch option. No port forwarding, works behind
CGNAT, and the game sees it as a LAN.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Lobby list empty or "connection failed" | The server is running; the IP in the launch option or OpenShim Custom server setting is right (launch option needs the full `ws://IP:1337/`); the network is Private; `Test-NetConnection 192.168.0.44 -Port 1337` from the client PC succeeds |
| Old full game with the same name listed | Restart the server (memberless game lobbies are dropped on load) |
| Stuck in the lobby after Launch, or desync | UDP 1338–1339 or the configured pair-port range is blocked, the range is exhausted, shared relay has ambiguous routes, or mod files differ between PCs |
| Disconnected right at login | The client is still pointed at the official servers (launch option missing, or OpenShim still set to Rebellion) |
