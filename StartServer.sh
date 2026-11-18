#!/usr/bin/env bash
# Starts the dedicated server in this terminal (Ctrl+C to stop) on Linux/macOS.
# Extra arguments go straight to server.py, e.g.  ./StartServer.sh --log-level DEBUG
# The first run creates .venv/ with the dependencies if this Python lacks them.
set -euo pipefail
cd "$(dirname "$0")"
# shellcheck source=scripts/find_python.sh
. ./scripts/find_python.sh

echo
echo "This computer's network addresses (use the one on the same network as the players):"
if command -v ip >/dev/null 2>&1; then
  ip -4 -o addr show scope global 2>/dev/null | awk '{split($4, a, "/"); print "   " a[1] "   (" $2 ")"}'
elif command -v ifconfig >/dev/null 2>&1; then
  ifconfig 2>/dev/null | awk '/inet / && $2 != "127.0.0.1" {print "   " $2}'
fi
echo
echo "How players connect (replace <address> with one of the addresses above):"
echo "   OpenShim:        Multiplayer setup screen > Custom > <address>"
echo "   Stock Steam/GOG: launch option  /bzrserver=ws://<address>:1337/"
echo "If a firewall is active, allow TCP 1337 and UDP 1338-1369 (e.g. sudo ufw allow 1337/tcp && sudo ufw allow 1338:1369/udp)."
echo "Players over the internet need TCP 1337 and UDP 1338-1369 forwarded to this computer."
echo

exec "$PY" server.py --relay-pair-ports --relay-pair-port-start 1340 --relay-pair-port-end 1369 "$@"
