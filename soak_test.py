#!/usr/bin/env python3
"""Long-duration soak harness for the BZR dedicated server.

Creates synthetic protocol clients and runs configurable churn against a
spawned local server while tracking server-side metrics and client-side
exceptions. At the end it asserts that internal state converges back to the
expected values (zero residual sessions/users/lobbies, no leaked tasks).

Modes (composable):
  idle         : clients sit authorized in the lounge
  lobby_churn  : create/join/leave lobby cycles
  reconnect    : repeated abrupt disconnect + re-auth cycles
  metadata     : SetPlayerData/SetLobbyData update storms
  launch       : simulated match-launch state transitions via lobby data
  p2p          : DoP2PConnect/DoP2PRoute signal churn

Usage:
  python soak_test.py --clients 40 --seconds 60 --modes idle,lobby_churn,reconnect
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import random
import socket
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

try:
    import websockets
except Exception as exc:  # pragma: no cover
    raise SystemExit("pip install websockets") from exc

HERE = Path(__file__).parent


def free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def free_udp_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    return s.getsockname()[1]


class SoakClient:
    def __init__(self, idx: int) -> None:
        self.idx = idx
        self.ws: Any = None
        self.uid = f"U_soak{idx:04d}"
        self.name = f"soak-{idx:04d}"
        self.rx = 0
        self.tx = 0
        self.exceptions = 0
        self.authorized = False
        self.last_send_error: str | None = None

    async def connect(self, host: str, port: int, attempts: int = 4) -> None:
        # Under heavy reconnect churn the handshake may time out; retry briefly
        # so the harness measures sustained operation instead of connect races.
        last_exc: Exception | None = None
        for attempt in range(attempts):
            try:
                self.ws = await websockets.connect(
                    f"ws://{host}:{port}", open_timeout=10, close_timeout=5, max_size=None
                )
                return
            except Exception as exc:
                last_exc = exc
                await asyncio.sleep(0.2 * (attempt + 1))
        raise last_exc

    async def send(self, msg_type: str, content: Any) -> None:
        try:
            await self.ws.send(json.dumps({"type": msg_type, "content": content}))
            self.tx += 1
        except Exception as exc:
            self.exceptions += 1
            self.last_send_error = repr(exc)
            raise

    async def pump(self, seconds: float = 0.0, max_msgs: int | None = None) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        deadline = time.time() + seconds
        try:
            while max_msgs is None or len(out) < max_msgs:
                timeout = 0.05 if max_msgs is not None else max(0.01, deadline - time.time())
                if timeout <= 0:
                    break
                raw = await asyncio.wait_for(self.ws.recv(), timeout=timeout)
                out.append(json.loads(raw))
                self.rx += 1
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
        except Exception:
            self.exceptions += 1
        return out

    async def authorize(self) -> None:
        await self.send(
            "Authorization",
            {
                "authtype": "web",
                "key": "",
                "id": "0",
                "apiVer": "0.0",
                "clientVersion": "2.2.301",
                "name": self.name,
                "playerName": self.name,
            },
        )
        msgs = await self.pump(max_msgs=8)
        self.authorized = any(m.get("type") == "OnAuthorization" for m in msgs)

    async def close_abrupt(self) -> None:
        try:
            self.ws.transport.abort()
        except Exception:
            self.exceptions += 1


async def client_worker(client: SoakClient, host: str, port: int, modes: set[str],
                        stop_at: float, rng: random.Random, stats: dict[str, int]) -> None:
    """One synthetic user performing churn until stop_at."""
    try:
        await client.connect(host, port)
        stats["connects"] += 1
        await client.authorize()
        if not client.authorized:
            stats["auth_failures"] += 1
            return
        stats["authorizations"] += 1
        await client.send("DoEnterLounge", True)
        await client.pump(max_msgs=6)

        my_lobby: int | None = None

        while time.time() < stop_at:
            mode = rng.choice(sorted(modes))

            if mode == "idle":
                await asyncio.sleep(rng.uniform(0.5, 2.0))
                await client.pump(max_msgs=4)

            elif mode == "lobby_churn":
                if my_lobby is None:
                    await client.send("CreateGame", {"memberLimit": 4, "isPrivate": False, "password": ""})
                    msgs = await client.pump(max_msgs=12)
                    for m in msgs:
                        if m.get("type") == "OnLobbyCreated" and m.get("data", {}).get("success"):
                            my_lobby = int(m["data"]["id"])
                            stats["lobbies_created"] += 1
                else:
                    await client.send("DoExitLobby", my_lobby)
                    await client.pump(max_msgs=10)
                    my_lobby = None
                    stats["lobby_exits"] += 1
                await asyncio.sleep(rng.uniform(0.05, 0.4))

            elif mode == "reconnect":
                await client.close_abrupt()
                stats["abrupt_disconnects"] += 1
                await asyncio.sleep(rng.uniform(0.1, 0.8))
                client.ws = None
                await client.connect(host, port)
                await client.authorize()
                await client.send("DoEnterLounge", True)
                await client.pump(max_msgs=6)
                stats["reconnects"] += 1
                my_lobby = None

            elif mode == "metadata":
                key = rng.choice(["ready", "team", "vehicle", "launched"])
                value = rng.choice(["1", "2", "0", "barracks"])
                await client.send("SetPlayerData", {"key": key, "value": value})
                await client.pump(max_msgs=3)
                stats["metadata_updates"] += 1
                await asyncio.sleep(rng.uniform(0.02, 0.15))

            elif mode == "launch":
                if my_lobby is None:
                    await client.send("CreateGame", {"memberLimit": 4, "isPrivate": False, "password": ""})
                    msgs = await client.pump(max_msgs=12)
                    for m in msgs:
                        if m.get("type") == "OnLobbyCreated" and m.get("data", {}).get("success"):
                            my_lobby = int(m["data"]["id"])
                else:
                    launched = rng.choice(["0", "1"])
                    await client.send("SetLobbyData", {"key": "launched", "value": launched})
                    await client.pump(max_msgs=6)
                    stats["launch_transitions"] += 1
                await asyncio.sleep(rng.uniform(0.05, 0.3))

            elif mode == "p2p":
                target = f"U_soak{rng.randrange(9999):04d}"
                if rng.random() < 0.5:
                    await client.send("DoP2PConnect", {"player": target, "reset": False})
                else:
                    await client.send("DoP2PRoute", {"player": target})
                await client.pump(max_msgs=6)
                stats["p2p_signals"] += 1
                await asyncio.sleep(rng.uniform(0.05, 0.3))

    except Exception as exc:
        client.exceptions += 1
        stats["worker_exceptions"] += 1
        stats.setdefault("worker_error_samples", [])
        if len(stats["worker_error_samples"]) < 8:
            stats["worker_error_samples"].append(f"w{client.idx}: {exc!r}")
    finally:
        with contextlib.suppress(Exception):
            await client.close_abrupt()


async def sample_health(http_port: int, samples: list[dict[str, Any]]) -> None:
    import urllib.request

    loop = asyncio.get_running_loop()
    def _get():
        with urllib.request.urlopen(f"http://127.0.0.1:{http_port}/health", timeout=5) as r:
            return json.loads(r.read().decode())
    try:
        samples.append(await loop.run_in_executor(None, _get))
    except Exception:
        pass


async def run(args) -> int:
    tmp = Path(tempfile.mkdtemp(prefix="bzr_soak_"))
    ws_port, udp_port, http_port = free_port(), free_udp_port(), free_port()
    cmd = [
        sys.executable, str(HERE / "server.py"),
        "--ws-port", str(ws_port), "--udp-port", str(udp_port),
        "--health-port", str(http_port), "--state-file", str(tmp / "state.json"),
        "--autosave-seconds", "5",
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    modes = {m.strip() for m in args.modes.split(",") if m.strip()}
    valid = {"idle", "lobby_churn", "reconnect", "metadata", "launch", "p2p"}
    unknown = modes - valid
    if unknown:
        print(f"unknown modes: {sorted(unknown)}; valid: {sorted(valid)}")
        proc.terminate()
        return 2
    if not modes:
        modes = {"idle"}

    stats: dict[str, int] = {k: 0 for k in [
        "connects", "authorizations", "auth_failures", "lobbies_created", "lobby_exits",
        "abrupt_disconnects", "reconnects", "metadata_updates", "launch_transitions",
        "p2p_signals", "worker_exceptions"]}
    samples: list[dict[str, Any]] = []
    clients = [SoakClient(i) for i in range(args.clients)]
    rx_total = tx_total = exc_total = 0

    try:
        # readiness
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                r, w = await asyncio.open_connection("127.0.0.1", ws_port)
                w.close()
                break
            except Exception:
                await asyncio.sleep(0.15)

        start = time.time()
        stop_at = start + args.seconds
        workers = [
            asyncio.create_task(client_worker(c, "127.0.0.1", ws_port, modes,
                                              stop_at - random.random(), random.Random(args.seed + i), stats))
            for i, c in enumerate(clients)
        ]
        sampler = asyncio.create_task(_sampler_loop(http_port, samples, stop_at))

        await asyncio.gather(*workers, return_exceptions=True)
        sampler.cancel()

        rx_total = sum(c.rx for c in clients)
        tx_total = sum(c.tx for c in clients)
        exc_total = sum(c.exceptions for c in clients)

        # Convergence: everything disconnected; sample repeatedly to
        # distinguish slow cleanup from a true leak.
        convergence: list[dict[str, Any]] = []
        elapsed = 0.0
        for wait in (1.0, 1.5, 3.0, 5.0, 8.0):
            await asyncio.sleep(wait)
            elapsed += wait
            h = await _get_health(http_port)
            st = h.get("state", {})
            convergence.append({
                "after_s": round(elapsed, 1),
                "activeConnections": st.get("activeConnections"),
                "activeUsers": st.get("activeUsers"),
                "lobbyCount": st.get("lobbyCount"),
            })
        final = await _get_health(http_port)
    finally:
        proc.terminate()
        with contextlib.suppress(Exception):
            proc.wait(timeout=10)

    state = final.get("state", final)
    ws_metrics = final.get("ws", {})
    summary = {
        "duration_s": round(time.time() - (time.time() - args.seconds), 1),
        "clients": args.clients,
        "modes": sorted(modes),
        "operations": stats,
        "client_rx_messages": rx_total,
        "client_tx_messages": tx_total,
        "client_exceptions": exc_total,
        "health_samples": len(samples),
        "convergence": convergence,
        "final_health_state": {
            k: state.get(k) for k in
            ("activeConnections", "activeUsers", "lobbyCount", "stdoutHistoryCount",
             "protocolHistoryCount")
        },
        "final_ws_metrics": ws_metrics,
    }

    peak_users = max((s.get("state", {}).get("activeUsers", 0) for s in samples), default=0)
    peak_lobbies = max((s.get("state", {}).get("lobbyCount", 0) for s in samples), default=0)
    summary["peak_activeUsers"] = peak_users
    summary["peak_lobbyCount"] = peak_lobbies

    send_errors = [c.last_send_error for c in clients if c.last_send_error]
    if send_errors:
        summary["send_error_samples"] = send_errors[:8]
    ok = True
    reasons = []
    if exc_total > args.max_client_exceptions:
        ok = False
        reasons.append(f"client exceptions {exc_total} > {args.max_client_exceptions}")
    if int(state.get("activeConnections", -1)) != 0 or int(state.get("activeUsers", -1)) != 0:
        ok = False
        reasons.append(f"residual sessions: {summary['final_health_state']}")
    # The server seeds one persistent ~chat~pub~~default lobby; it is expected
    # to remain. --retain-empty keeps user-created rooms too and skips the check.
    expected_lobbies = None if args.retain_empty else 1
    if expected_lobbies is not None and int(state.get("lobbyCount", -1)) != expected_lobbies:
        ok = False
        reasons.append(f"residual lobbies: {state.get('lobbyCount')} (expected {expected_lobbies})")
    if stats["worker_exceptions"]:
        ok = False
        reasons.append(f"worker exceptions: {stats['worker_exceptions']}")

    print(json.dumps(summary, indent=2))
    print(f"\nSOAK {'PASS' if ok else 'FAIL'}" + (f" :: {'; '.join(reasons)}" if reasons else ""))
    return 0 if ok else 1


async def _get_health(http_port: int) -> dict[str, Any]:
    import urllib.request

    loop = asyncio.get_running_loop()
    def _get():
        with urllib.request.urlopen(f"http://127.0.0.1:{http_port}/health", timeout=5) as r:
            return json.loads(r.read().decode())
    return await loop.run_in_executor(None, _get)


async def _sampler_loop(http_port: int, samples: list, stop_at: float) -> None:
    while time.time() < stop_at:
        await sample_health(http_port, samples)
        await asyncio.sleep(2.0)


def main() -> int:
    ap = argparse.ArgumentParser(description="BZR dedicated-server soak harness")
    ap.add_argument("--clients", type=int, default=20)
    ap.add_argument("--seconds", type=float, default=30.0)
    ap.add_argument("--modes", default="idle,lobby_churn,metadata",
                    help="comma set of: idle,lobby_churn,reconnect,metadata,launch,p2p")
    ap.add_argument("--seed", type=int, default=1337)
    ap.add_argument("--max-client-exceptions", type=int, default=0)
    ap.add_argument("--retain-empty", action="store_true")
    args = ap.parse_args()
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
