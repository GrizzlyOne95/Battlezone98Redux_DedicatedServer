#!/usr/bin/env python3
"""Build the one-file Windows server (no Python needed on the target PC).

    python build_package.py

Creates build/venv with websockets + PyInstaller and freezes one executable:

    dist/BZRDedicatedServer.exe   double-click: launcher window
                                  with arguments: headless server (--help)
    dist/RELEASE_NOTES.md         quick start, used as the GitHub release text

The release workflow (.github/workflows/release.yml) runs this on a v* tag and
attaches the exe to the GitHub release.
"""

from __future__ import annotations

import os
import subprocess
import sys
import venv
from pathlib import Path

HERE = Path(__file__).resolve().parent
BUILD = HERE / "build"
VENV = BUILD / "venv"
DIST = HERE / "dist"
EXE_NAME = "BZRDedicatedServer"
REPO_URL = "https://github.com/GrizzlyOne95/Battlezone98Redux_DedicatedServer"

NOTES = f"""\
## Quick start

1. Download **{EXE_NAME}.exe** below and put it in its own folder (settings and
   `data\\server_state.json` are saved next to it).
2. Double-click it, check the **Setup Guide** tab, and press **Start**.
   Windows asks to allow it through the firewall the first time: allow Private networks.
3. Players with OpenShim pick **Custom** on the Multiplayer setup screen and type
   this PC's address. Without OpenShim they use the launch option the window shows
   (Copy option), e.g. `/bzrserver=ws://192.168.0.44:1337/`.

Ports: TCP 1337 (lobby), UDP 1338 (handshake), UDP 1339 (relay); with
*Separate UDP port per peer pair* also UDP 1340-1369. Forward them for internet play.

Headless (no window), e.g. as a service or in a terminal:

    {EXE_NAME}.exe --state-file data\\server_state.json
    {EXE_NAME}.exe --help
    {EXE_NAME}.exe --headless     (default settings, no window)

Status page while running: http://127.0.0.1:8080/status.
Game PC setup: [LAN co-op guide]({REPO_URL}/blob/main/docs/LAN_COOP.md).

Build: {{version}}
"""


def run(cmd: list[str], **kw) -> None:
    print("+", subprocess.list2cmdline(cmd), flush=True)
    subprocess.run(cmd, check=True, **kw)


def version() -> str:
    try:
        out = subprocess.run(
            ["git", "describe", "--tags", "--always", "--dirty"], cwd=HERE, capture_output=True, text=True, check=True
        )
        return out.stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def main() -> None:
    if sys.platform != "win32":
        raise SystemExit("The package is Windows-only; run this on Windows.")
    py = VENV / "Scripts" / "python.exe"
    if not py.exists():
        print(f"Creating {VENV}")
        venv.EnvBuilder(with_pip=True).create(VENV)
    run([str(py), "-m", "pip", "install", "-q", "-r", str(HERE / "requirements.txt"), "pyinstaller>=6.10,<7"])

    DIST.mkdir(exist_ok=True)
    exe = DIST / f"{EXE_NAME}.exe"
    exe.unlink(missing_ok=True)
    work = BUILD / "pyinstaller"
    # Console exe so the headless mode prints to a terminal; on a double-click
    # the window reopens itself detached so that console closes. server.py imports websockets inside
    # run_server() and server_gui only for --gui, so name both explicitly.
    run([str(py), "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile", "--console",
         "--name", EXE_NAME, "--icon", str(HERE / "branding" / "app.ico"), "--distpath", str(DIST), "--workpath", str(work), "--specpath", str(work),
         "--collect-submodules", "websockets", "--hidden-import", "server_gui", str(HERE / "server.py")])

    ver = version()
    (DIST / "RELEASE_NOTES.md").write_text(NOTES.format(version=ver), encoding="utf-8")
    print(f"\nExe: {exe} ({exe.stat().st_size / 1e6:.1f} MB), build {ver}")
    # Smoke check: the frozen exe parses its arguments headless.
    run([str(exe), "--help"], stdout=subprocess.DEVNULL, env=dict(os.environ, PYTHONIOENCODING="utf-8"))


if __name__ == "__main__":
    main()
