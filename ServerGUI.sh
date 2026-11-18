#!/usr/bin/env bash
# Opens the dedicated-server launcher window on Linux/macOS (same as: python3 server.py --gui).
set -euo pipefail
cd "$(dirname "$0")"
NEED_TK=1
# shellcheck source=scripts/find_python.sh
. ./scripts/find_python.sh
exec "$PY" server.py --gui "$@"
