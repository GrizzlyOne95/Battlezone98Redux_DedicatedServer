# LAN co-op on your own server

How to play BZR multiplayer or Campaign Reimagined co-op on a home network, with one PC running
this server.

## Server PC

1. Start the server. It listens on every network interface by default:

   ```
   cd Battlezone98Redux_DedicatedServer
   python server.py --state-file C:\BZRCoop\server\state.json
   ```

   Ports: TCP 1337 (lobby), UDP 1338 (handshake), UDP 1339 (relay). The 8080 status page stays
   local-only (http://127.0.0.1:8080/status) and clients never use it.

   Or double-click `ServerGUI.bat` (same as `pythonw server_gui.py`) for a window with
   Start/Stop/Restart, the live log, connected players, open lobbies, and the LAN address players
   should use. Set the state file there once; settings are remembered in `server_gui.json`.

2. Find the PC's LAN address with `ipconfig` (the `Ethernet` or `Wi-Fi` IPv4 address, for example
   `192.168.0.44`). Give this PC a DHCP reservation in the router so the address never changes.

3. Firewall: Windows must allow inbound traffic to the Python that runs the server. If Windows
   asked "Allow access?" the first time and you said yes, you're done. Otherwise run this once in an
   admin PowerShell:

   ```
   New-NetFirewallRule -DisplayName "BZR server TCP" -Direction Inbound -Protocol TCP -LocalPort 1337 -Action Allow -Profile Private
   New-NetFirewallRule -DisplayName "BZR server UDP" -Direction Inbound -Protocol UDP -LocalPort 1338-1339 -Action Allow -Profile Private
   ```

   The network must be set to **Private** (Settings > Network > Properties), not Public.

## Every PC (the server PC too, if someone plays on it)

1. Install OpenShim **v1.0.0.9 or newer**.
2. In the game folder, edit `net.ini`. Under `[OpenShimSocket]`, set:

   ```
   MatchmakingRedirectAddress=192.168.0.44
   ```

   Enter the IP only. Ports are fixed, and any value containing `/` is ignored. Use the LAN IP on
   the server PC as well, not `127.0.0.1`.
3. Every PC must run the **same build** of each mod in use. For CR, copy the server PC's
   `mods\3686673790` folder to each PC: `<game>\mods\3686673790` on GOG, or
   `steamapps\workshop\content\301650\3686673790` on Steam. If the files differ, you get map or mod
   mismatches or desyncs.
4. Launch the game normally. GOG copies work with or without Galaxy. Steam copies need Steam running,
   because a Steam login without a ticket is rejected.

## Playing

1. Multiplayer > Internet. Everyone should see the same lobby list, served by your PC.
2. One player clicks **Create Game** and picks the map, for example *CR: Eagle's Nest 1 Coop*. The
   others click **Join Game**.
3. The host clicks **Launch** once everyone is in. CR co-op missions need everyone to start
   together: a player who joins late means restarting the mission.

To go back to official online play, clear the value: `MatchmakingRedirectAddress=`.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Lobby list empty or "connection failed" | The server is running; the IP in `net.ini` is right; the network is Private; `Test-NetConnection 192.168.0.44 -Port 1337` from the client PC succeeds |
| Old full game with the same name listed | Restart the server (memberless game lobbies are dropped on load) |
| Stuck in the lobby after Launch, or desync | UDP 1338-1339 is blocked, or mod files differ between PCs |
| Disconnected right at login | The client is still pointed at the official servers (`net.ini` not saved, or an old OpenShim) |
