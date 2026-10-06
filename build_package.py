#!/usr/bin/env python3
"""Build the standalone Windows package (no Python needed on the target PC).

    python build_package.py

Creates build/venv with websockets + PyInstaller, freezes two executables, and
zips them with the operator docs:

    dist/BZRDedicatedServer/
        BZRServerGUI.exe   launcher window (server_gui.py)
        BZRServer.exe      the server itself (server.py); also usable headless
        README.txt
        LAN_COOP.md
    dist/BZRDedicatedServer-<version>.zip
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import venv
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
BUILD = HERE / "build"
VENV = BUILD / "venv"
DIST = HERE / "dist"
PKG_NAME = "BZRDedicatedServer"
PKG = DIST / PKG_NAME

README = """\
Battlezone 98 Redux Dedicated Server
=====================================

Double-click BZRServerGUI.exe, check the settings, and press Start.
Keep BZRServerGUI.exe and BZRServer.exe in the same folder.

Players connect to the address shown under "Players connect to".
Ports: TCP 1337 (lobby), UDP 1338 (handshake), UDP 1339 (relay).
Windows will ask to allow BZRServer.exe through the firewall the first
time; allow it on Private networks. See LAN_COOP.md for setting up the
game PCs to use this server.

Settings are saved in server_gui.json next to the exe. The default state
file is data\\server_state.json in this folder.

Headless (no window):
    BZRServer.exe --state-file data\\server_state.json
    BZRServer.exe --help        lists every option
Status page while running: http://127.0.0.1:8080/status

Build: {version}
"""


def run(cmd: list[str], **kw) -> None:
    print("+", subprocess.list2cmdline(cmd), flush=True)
    subprocess.run(cmd, check=True, **kw)


def version() -> str:
    try:
        out = subprocess.run(
            ["git", "describe", "--always", "--dirty"], cwd=HERE, capture_output=True, text=True, check=True
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
    run([str(py), "-m", "pip", "install", "-q", "-r", str(HERE / "requirements.txt"), "pyinstaller"])

    if PKG.exists():
        shutil.rmtree(PKG)
    PKG.mkdir(parents=True)
    work = BUILD / "pyinstaller"
    common = ["--noconfirm", "--clean", "--onefile", "--distpath", str(PKG), "--workpath", str(work), "--specpath", str(work)]

    # server.py imports websockets lazily inside run_server(), so name it explicitly.
    run([str(py), "-m", "PyInstaller", *common, "--console", "--name", "BZRServer",
         "--collect-submodules", "websockets", str(HERE / "server.py")])
    run([str(py), "-m", "PyInstaller", *common, "--windowed", "--name", "BZRServerGUI", str(HERE / "server_gui.py")])

    ver = version()
    (PKG / "README.txt").write_text(README.format(version=ver), encoding="utf-8")
    shutil.copy2(HERE / "docs" / "LAN_COOP.md", PKG / "LAN_COOP.md")

    zip_path = DIST / f"{PKG_NAME}-{ver}.zip"
    zip_path.unlink(missing_ok=True)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(PKG.rglob("*")):
            zf.write(f, Path(PKG_NAME) / f.relative_to(PKG))
    print(f"\nPackage: {PKG}\nZip:     {zip_path} ({zip_path.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
