# Sourced by StartServer.sh / ServerGUI.sh: sets PY to a Python 3.9+ that can
# import websockets (and tkinter when NEED_TK=1), creating .venv/ if needed.

need_tk="${NEED_TK:-0}"

usable() {  # usable <python>: 0 when it is 3.9+ (and has tkinter if needed)
  "$1" -c "import sys; sys.exit(sys.version_info < (3, 9))" >/dev/null 2>&1 || return 1
  if [ "$need_tk" = 1 ]; then "$1" -c "import tkinter" >/dev/null 2>&1 || return 1; fi
}

PY=""
if [ -x .venv/bin/python ] && usable .venv/bin/python; then
  PY=.venv/bin/python
else
  for candidate in python3.13 python3.12 python3.11 python3.10 python3.9 python3; do
    if command -v "$candidate" >/dev/null 2>&1 && usable "$candidate"; then
      PY="$(command -v "$candidate")"
      break
    fi
  done
fi

if [ -z "$PY" ]; then
  echo "No Python 3.9+ found${NEED_TK:+ with tkinter}." >&2
  if [ "$need_tk" = 1 ]; then
    echo "Install Python with Tk support, e.g. sudo apt install python3 python3-tk python3-venv (Debian/Ubuntu)," >&2
    echo "sudo dnf install python3 python3-tkinter (Fedora), or the python.org installer / brew install python-tk on macOS." >&2
  else
    echo "Install Python 3 (3.12 recommended) from your package manager or https://www.python.org/." >&2
  fi
  exit 1
fi

if ! "$PY" -c "import websockets" >/dev/null 2>&1; then
  # Many Linux distributions forbid pip installs into the system Python
  # (PEP 668), so keep the dependencies in a private virtual environment.
  echo "Installing server dependencies into .venv/ ..."
  if ! "$PY" -m venv .venv; then
    echo "Could not create .venv. On Debian/Ubuntu install python3-venv first: sudo apt install python3-venv" >&2
    exit 1
  fi
  .venv/bin/python -m pip install -q -r requirements.txt
  PY=.venv/bin/python
fi
