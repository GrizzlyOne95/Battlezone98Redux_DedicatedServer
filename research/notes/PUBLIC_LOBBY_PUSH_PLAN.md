# Public Lobby Push Plan

Status: draft, pre-launch. Nothing here is implemented yet.

## Goal

Show live community lobbies on `battlezonecommunity.com` without exposing the server's IP or any player's address, and without opening an inbound port for the website.

## Design

The server pushes; the website never calls the server.

```text
server.py  --HTTPS POST (every ~15 s, bearer secret)-->  Cloudflare Worker  -->  KV (TTL ~45 s)
                                                              ^
browser  --GET /api/lobbies (cached ~10 s)--------------------+
```

- Outbound HTTPS only. The origin keeps no new inbound exposure for the site.
- If the server stops pushing, the KV entry expires and the page shows "No live matches".
- Players still connect to the lobby service by hostname (e.g. `mp.battlezonecommunity.com`), never by a published IP. The match itself stays peer to peer.

## Snapshot payload (public-safe allowlist)

Per lobby: `name`, `map`, `players` (count), `memberLimit`, `isLocked`. Top level: `updated` (UTC), `lobbies[]`.

Never included: user or lobby IDs, player names (decide later whether to add), `wanAddress`, `lanAddresses`, `ipAddress`, the owner, chat. Build the payload from an explicit allowlist, not by redacting `build_lobbies_payload()`, which carries `wanAddress` and is for the local health port only. Private lobbies are omitted entirely.

## Work items

### 1. Server (this repo)
- New module `public_lobby_push.py`: builds the allowlisted snapshot from `ServerState` and POSTs it on an interval.
- Flags (all off by default, so LAN use is unchanged): `--publish-url`, `--publish-secret` (also env `BZR_PUBLISH_SECRET`), `--publish-interval` (default 15).
- Minimal hook in `server.py` to start the task alongside the health endpoint; failures are logged and retried, never fatal to the lobby service.
- Tests: payload contains no address fields (assert on keys, and on a fixture with addresses set); private lobbies omitted; push is a no-op without a URL.
- Docs: a short section in `README.md` and `NOTES.md`. Recommend running with `--redact-addresses`.

### 2. Worker (`PiercingXX/battlezonecommunity.com`)
- Add `src/worker.js` and a KV binding in `wrangler.jsonc`; keep static assets and the existing single-page fallback.
- `POST /api/lobbies`: require `Authorization: Bearer <secret>` (Worker secret `PUBLISH_SECRET`), constant-time compare, size cap (~16 KB), schema-validate against the allowlist, store with `expirationTtl` 45 s.
- `GET /api/lobbies`: return the stored snapshot or `{lobbies: []}`; `Cache-Control: public, max-age=10`.
- Everything else falls through to static assets.

### 3. Page (`site/index.html`)
- "Live matches" panel under the scope: lobby name, map, players/limit, locked badge. Hidden when empty, polled every ~20 s, reduced-motion safe, no new fonts or libraries.
- Join action v1: text instructions ("Open Battlezone 98 Redux, Multiplayer, find the lobby"). Upgrade to a one-click link only if the client turns out to have a join-by-lobby URL or flag (open question below).

## Validation
- Unit tests above; `wrangler dev` locally with the server pushing to `http://localhost:8787`.
- Negative checks: missing or wrong secret returns 401; oversize or unexpected fields are rejected; stopping the server empties the page within ~45 s.
- Confirm the live `/api/lobbies` response contains no IPs by grepping for dotted quads and `:1337` style strings.

## Rollout
1. Server patch and tests, merged here.
2. Worker and page on a branch of the site repo; preview deploy.
3. Set `PUBLISH_SECRET` on the Worker and the server; enable `--publish-url` on the production host.
4. At launch, point the public hostname at the lobby service; no change to the website.

## Open questions
- Does the Redux client support a join-by-lobby URL or command-line flag?
- Publish player names, or counts only? (Default: counts only.)
- Does the lobby host need to be hidden at the network level too (relay or Spectrum), or is a DNS name enough? Out of scope for this plan; revisit before launch.
- Write access to the `PiercingXX` site repo (direct branch or fork and PR)?

## Not in scope
Relaying game UDP, replacing the BZRNet protocol work, a server browser inside the game, accounts or auth on the website.
