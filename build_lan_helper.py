#!/usr/bin/env python3
"""Freeze a Win32 OpenShim LAN helper without requiring Python on player PCs.

Run with --python pointing at a Windows 32-bit Python 3.10+ installation.
The helper is separate from the injected plugin, but remains Win32 so it can
run in the same Windows/Wine/Proton environment as Battlezone 98 Redux.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

HERE = Path(__file__).resolve().parent


def run(command: list[str]) -> None:
    subprocess.run(command, check=True, cwd=HERE)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", default=sys.executable, help="Windows 32-bit Python 3.10+ executable")
    parser.add_argument("--existing-environment", action="store_true",
                        help="Use an already prepared x86 PyInstaller/websockets environment without creating a venv or installing packages")
    args = parser.parse_args()
    if sys.platform != "win32":
        parser.error("Build the Windows helper on Windows with 32-bit Python")
    probe = subprocess.run(
        [args.python, "-c", "import struct,sys; print(struct.calcsize('P')); print(sys.version_info >= (3,10)); print(sys.platform)"],
        check=True, capture_output=True, text=True,
    ).stdout.splitlines()
    if probe != ["4", "True", "win32"]:
        parser.error("--python must name a Windows 32-bit Python 3.10+; install it alongside the 64-bit interpreter")

    build = HERE / "build" / "lan-helper"
    venv = build / "venv"
    if args.existing_environment:
        python = Path(args.python).resolve()
        run([str(python), "-c", "import PyInstaller,websockets; assert 12 <= int(websockets.__version__.split('.')[0]) < 16"])
    else:
        python = venv / "Scripts" / "python.exe"
        if not python.exists():
            run([args.python, "-m", "venv", str(venv)])
        run([str(python), "-m", "pip", "install", "-r", str(HERE / "requirements.txt"), "pyinstaller"])
    payload = HERE / "dist" / "OpenShimLAN"
    destination = payload / "tools" / "lan"
    destination.mkdir(parents=True, exist_ok=True)
    run([str(python), "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile", "--console",
         "--name", "BZRServer", "--distpath", str(destination), "--workpath", str(build / "freeze"),
         "--specpath", str(build), "--collect-submodules", "websockets", str(HERE / "server.py")])
    exe = destination / "BZRServer.exe"
    # Check the produced PE, not just the interpreter selected above.
    raw = exe.read_bytes()
    pe_offset = int.from_bytes(raw[0x3c:0x40], "little")
    if raw[pe_offset:pe_offset + 6] != b"PE\x00\x00\x4c\x01":
        raise SystemExit("Refusing helper payload: BZRServer.exe is not a Win32 i386 PE")
    shutil.copy2(HERE / "docs" / "LAN_DISCOVERY.md", destination / "LAN_DISCOVERY.md")
    shutil.copy2(HERE / "LICENSE", destination / "LICENSE")
    revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=HERE, capture_output=True, text=True, check=True).stdout.strip()
    dirty = bool(subprocess.run(["git", "status", "--porcelain", "--untracked-files=normal"],
                                cwd=HERE, capture_output=True, text=True, check=True).stdout.strip())
    (destination / "manifest.json").write_text(json.dumps({
        "format": 1, "revision": revision, "dirty": dirty, "architecture": "win32", "discoveryProtocol": "BZRLAN1",
        "sha256": hashlib.sha256(raw).hexdigest(),
    }, indent=2) + "\n", encoding="utf-8")
    output = HERE / "dist" / "OpenShimLAN.zip"
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(payload.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(payload))
    print(f"LAN helper payload: {output}")


if __name__ == "__main__":
    main()
