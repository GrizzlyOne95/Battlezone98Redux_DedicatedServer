# Battlezone98Redux Dedicated Server Notes

## Purpose

This repository now contains a practical BZR failover stack:

- `server.py`: WebSocket lobby compatibility server (`1337`) + UDP shim (`1338`)
- health/status endpoint (`8080`)
- optional state persistence to JSON (`data/server_state.json`)
- Docker/Compose deployment scaffolding

## Operational Defaults

- WS: `0.0.0.0:1337`
- UDP: `0.0.0.0:1338`
- Health: `127.0.0.1:8080` (`/health`, `/status`)
- Autosave interval: 15 seconds

## Failover Cutover Checklist

1. Start server (`python server.py` or `docker compose up -d --build`).
2. Validate using `python smoke_test.py --host <server-ip>`.
3. Update DNS/hosts for `battlezone98mp.webdev.rebellion.co.uk` to your failover host.
4. Ensure firewall/NAT forwards:
   - TCP 1337
   - UDP 1338
   - TCP 8080 (optionally restricted to LAN/admin network)
5. Monitor `/health` and logs for connection/auth/chat/lobby activity.

## Admin/Security Notes

- Use `--admin-key` (or `BZR_ADMIN_KEY`) to grant admin privileges via `Authorization.content.key`.
- Use `--admin-ids` for fixed trusted IDs.
- Treat `.env` as secret material (already gitignored).
- Keep regular backups of `data/server_state.json`.

## Persistence Notes

- Persisted:
  - lobby metadata/settings
  - next lobby ID
- Not persisted:
  - active websocket sessions
- Recommended for restart continuity:
  - run with `--retain-empty-lobbies`

## Known Gaps / Next Priority

1. Full game-client WS redirection still depends on DNS/hosts override.
2. UDP path is a compatibility shim, not full production relay/NAT traversal logic.
3. No auth backend integration yet (Steam/GOG ticket validation not implemented).
4. No persistent moderation store yet (banlists/whitelists are currently client-driven).
