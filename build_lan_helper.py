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


def runtime_notice_sources(python: Path) -> dict:
    """Find exact notices for the runtime/modules the selected interpreter uses.

    Match package metadata to each imported module's version: --target installs
    can leave an older/newer dist-info directory beside the selected module.
    """
    code = r'''
import importlib.metadata as metadata
import json
from pathlib import Path
import sys
import PyInstaller
import websockets

sources = {"python/LICENSE.txt": str(Path(sys.base_prefix) / "LICENSE.txt")}
versions = {"python": sys.version.split()[0]}
for component, version in (("websockets", websockets.__version__), ("pyinstaller", PyInstaller.__version__)):
    versions[component] = version
    distributions = [d for d in metadata.distributions()
                     if d.metadata["Name"].lower() == component and d.version == version]
    if len(distributions) != 1:
        raise SystemExit("Cannot identify unique metadata for " + component + " " + version)
    distribution = distributions[0]
    found = False
    for item in sorted(distribution.files or [], key=str):
        # Copy the actual package's license/notice documents, not unrelated
        # files from another distribution or generated summaries.
        if not any(part.endswith(".dist-info") for part in item.parts):
            continue
        if not item.name.lower().startswith(("license", "licence", "copying", "notice")):
            continue
        relative = component + "/" + item.name
        if relative in sources:
            raise SystemExit("Ambiguous runtime notice name: " + relative)
        sources[relative] = str(distribution.locate_file(item))
        found = True
    if not found:
        raise SystemExit("Required license/notice is missing from " + component + " " + version)
for path in sources.values():
    if not Path(path).is_file():
        raise SystemExit("Required runtime notice is missing: " + path)
print(json.dumps({"versions": versions, "sources": sources}))
'''
    result = subprocess.run([str(python), "-c", code], cwd=HERE, capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def copy_runtime_notices(destination: Path, notices: dict) -> dict[str, str]:
    root = destination / "licenses"
    # This is a generated subtree in dist/, never an input/runtime directory.
    if root.resolve().parent != destination.resolve() or root.is_symlink():
        raise SystemExit("Refusing an unexpected licenses output path")
    if root.exists():
        shutil.rmtree(root)
    hashes = {}
    for relative, source in sorted(notices["sources"].items()):
        target = root / relative
        if root.resolve() not in target.resolve().parents:
            raise SystemExit("Refusing a runtime notice path outside licenses/")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        hashes["licenses/" + relative] = hashlib.sha256(target.read_bytes()).hexdigest()
    return hashes


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
    notices = runtime_notice_sources(python)
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
    notice_hashes = copy_runtime_notices(destination, notices)
    revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=HERE, capture_output=True, text=True, check=True).stdout.strip()
    dirty = bool(subprocess.run(["git", "status", "--porcelain", "--untracked-files=normal"],
                                cwd=HERE, capture_output=True, text=True, check=True).stdout.strip())
    (destination / "manifest.json").write_text(json.dumps({
        "format": 1, "revision": revision, "dirty": dirty, "architecture": "win32", "discoveryProtocol": "BZRLAN1",
        "sha256": hashlib.sha256(raw).hexdigest(),
        "bundledRuntime": notices["versions"], "licenseSha256": notice_hashes,
    }, indent=2) + "\n", encoding="utf-8")
    output = HERE / "dist" / "OpenShimLAN.zip"
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(payload.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(payload))
    print(f"LAN helper payload: {output}")


if __name__ == "__main__":
    main()
