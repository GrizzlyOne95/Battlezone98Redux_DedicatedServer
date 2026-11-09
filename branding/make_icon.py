#!/usr/bin/env python3
"""Regenerate branding/app.ico (the exe icon) from repo_icon.svg.

Needs ImageMagick 7 (`magick`) on PATH:

    python branding/make_icon.py

app.ico is committed, so builds (and CI) do not need ImageMagick.
"""

import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
subprocess.run(["magick", "-background", "none", "-density", "384", str(HERE / "repo_icon.svg"),
                "-resize", "256x256", "-define", "icon:auto-resize=256,128,64,48,32,24,16",
                str(HERE / "app.ico")], check=True)
print(HERE / "app.ico")
