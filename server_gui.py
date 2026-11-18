#!/usr/bin/env python3
"""Desktop launcher for the Battlezone 98 Redux dedicated server.

Starts/stops server.py as a child process, shows its log, and polls the local
health port (/status, /lobbies) for players and lobbies. Uses only the standard
library (tkinter), so it runs with any Python that can run server.py.

    python server.py --gui      (or pythonw server.py --gui for no console;
                                 python server_gui.py also works)

The packaged BZRDedicatedServer.exe opens this window when double-clicked.
Settings are saved to server_gui.json next to this file (or the exe).
"""

from __future__ import annotations

import sys

if sys.version_info < (3, 9):  # pragma: no cover
    sys.exit("The launcher needs Python 3.9 or newer (3.12 recommended): https://www.python.org/")

import json
import os
import queue
import socket
import subprocess
import threading
import time
import tkinter as tk
import urllib.request
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

FROZEN = getattr(sys, "frozen", False)
# Packaged build (build_package.py): one exe holds both the window and the
# server, so the window starts another copy of itself with server arguments.
HERE = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent
SERVER_SCRIPT = Path(sys.executable).resolve() if FROZEN else HERE / "server.py"
CONFIG_FILE = HERE / "server_gui.json"
POLL_SECONDS = 2.0
MAX_LOG_LINES = 5000

BIND_CHOICES = {
    "All interfaces (LAN / internet)": "0.0.0.0",
    "This PC only (127.0.0.1)": "127.0.0.1",
}


def default_python() -> str:
    exe = Path(sys.executable)
    # pythonw has no stdout; the child needs a console interpreter.
    if exe.name.lower() == "pythonw.exe":
        candidate = exe.with_name("python.exe")
        if candidate.exists():
            return str(candidate)
    return str(exe)


DEFAULTS = {
    "python": default_python(),
    "bind": "0.0.0.0",
    "ws_port": 1337,
    "udp_port": 1338,
    "relay_port": 1339,
    "relay_enabled": True,
    "relay_pair_ports": False,
    "relay_pair_port_start": 1340,
    "relay_pair_port_end": 1369,
    "health_port": 8080,
    "state_file": str(HERE / "data" / "server_state.json"),
    "motd": "",
    "admin_key": "",
    "admin_ids": "",
    "max_lobbies": 500,
    "retain_empty_lobbies": False,
    "redact_addresses": False,
    "log_level": "INFO",
    "access_mode": "open",
    "pin_ips": False,
    "extra_args": "",
    "public_host": "",
    "lan_discovery": False,
}
PUBLIC_IP_URL = "https://api.ipify.org"
MIN_DEFAULT_WIDTH = 1400  # wide enough that the connect panel's text wraps in few lines
MAX_DEFAULT_HEIGHT = 950  # keep the default window inside a 1080p screen
ACCESS_SYNC_HOLDOFF = 3.0  # seconds polls may not overwrite the access-mode combobox after a GUI change
OPENSHIM_WS_PORT = 1337  # the only lobby port OpenShim's setup screen can reach
ACCESS_CHOICES = {
    "Open (anyone)": "open",
    "Approve new players": "approve",
    "Allowlist only": "allowlist",
}


def pair_port_notice(enabled: bool, start: int, end: int) -> str:
    if not enabled:
        return "Shared relay supports one relayed peer per player. Use separate pair ports for 3+ relayed players."
    if start == end == 0:
        return "Ephemeral pair ports: this PC only. Choose a fixed UDP range for LAN/internet players."
    return (f"Pair relay UDP {start}-{end}: allow/forward this range too. "
            f"{end - start + 1} pair ports total across all lobbies; four fully relayed players need six.")


def lan_addresses() -> list[str]:
    addrs: set[str] = set()
    primary = ""
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addrs.add(info[4][0])
    except OSError:
        pass
    try:
        # No packets are sent; this just asks the OS which interface routes outward.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))
            primary = s.getsockname()[0]
            addrs.add(primary)
    except OSError:
        pass
    import ipaddress

    def rank(a: str) -> tuple[int, str]:
        # The default-route address first; VM/WSL switches are private too.
        if a == primary and not a.startswith("100."):
            return (-1, a)
        ip = ipaddress.ip_address(a)
        if ip in ipaddress.ip_network("100.64.0.0/10"):
            return (1, a)  # CGNAT range: Tailscale and similar VPNs
        return (0 if ip.is_private else 2, a)

    keep = [a for a in addrs if not a.startswith("127.") and not a.startswith("169.254.")]
    return sorted(keep, key=rank)


def describe_address(a: str) -> str:
    import ipaddress

    if ipaddress.ip_address(a) in ipaddress.ip_network("100.64.0.0/10"):
        return f"{a}  (Tailscale/VPN)"
    return a


def fmt_duration(seconds: int) -> str:
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}"


APP_TITLE = "BATTLEZONE 98 REDUX — DEDICATED SERVER"

# ---------- theme (Battlezone vector-HUD: near-black olive, phosphor green, amber) ----------
BG = "#0e110d"
HEADER_BG = "#080a07"
PANEL = "#151a13"
FIELD = "#0a0d09"
BORDER = "#2c3a28"
FG = "#d4dbd0"
MUTED = "#8a9686"
GREEN = "#7cd36b"
GREEN_DK = "#4caf50"
AMBER = "#e0a030"
RED = "#e5534b"
GUI_BLUE = "#7fb2ff"
SELECT_BG = "#27402a"
FONT_FAMILY = "Segoe UI"
MONO_FAMILY = "Consolas"
TITLE_FAMILY = "Segoe UI"


def apply_theme(root: tk.Misc) -> ttk.Style:
    """Dark Battlezone look on the 'clam' base (vista/winnative cannot be recoloured)."""
    global FONT_FAMILY, MONO_FAMILY, TITLE_FAMILY
    from tkinter import font as tkfont

    families = set(tkfont.families(root))
    FONT_FAMILY = "Segoe UI" if "Segoe UI" in families else "TkDefaultFont"
    MONO_FAMILY = "Consolas" if "Consolas" in families else "Courier"
    TITLE_FAMILY = "Bahnschrift" if "Bahnschrift" in families else FONT_FAMILY
    for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
        try:
            tkfont.nametofont(name).configure(family=FONT_FAMILY if FONT_FAMILY != "TkDefaultFont" else "Arial", size=9)
        except tk.TclError:
            pass
    root.configure(background=BG)
    style = ttk.Style(root)
    style.theme_use("clam")
    scale = max(1.0, float(root.winfo_fpixels("1i")) / 96.0)

    style.configure(".", background=BG, foreground=FG, fieldbackground=FIELD, bordercolor=BORDER,
                    lightcolor=BORDER, darkcolor=BORDER, troughcolor=PANEL, focuscolor=GREEN_DK,
                    selectbackground=SELECT_BG, selectforeground=FG, insertcolor=GREEN)
    style.map(".", foreground=[("disabled", "#5d6959")])
    style.configure("TFrame", background=BG)
    style.configure("TLabel", background=BG, foreground=FG)
    style.configure("Muted.TLabel", foreground=MUTED)
    style.configure("Warn.TLabel", foreground=AMBER)
    style.configure("BadgeWarn.TLabel", background=AMBER, foreground="#1a1200", font=(FONT_FAMILY, 10, "bold"))
    style.configure("BadgeOk.TLabel", background="#2f7d32", foreground="#f2fff0", font=(FONT_FAMILY, 10, "bold"))
    style.configure("Attn.TLabel", background=RED, foreground="#ffffff", font=(FONT_FAMILY, 10, "bold"))
    style.configure("TLabelframe", background=BG, bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER)
    style.configure("TLabelframe.Label", background=BG, foreground=GREEN, font=(FONT_FAMILY, 9, "bold"))

    style.configure("TButton", background="#1d261b", foreground=FG, bordercolor=BORDER, lightcolor="#1d261b",
                    darkcolor="#1d261b", padding=(10, 4), focusthickness=1, relief="flat")
    style.map("TButton",
              background=[("disabled", "#11150f"), ("pressed", "#0f1a0d"), ("active", "#2a3a27")],
              foreground=[("disabled", "#4f594c"), ("active", GREEN)],
              bordercolor=[("active", GREEN_DK), ("disabled", "#1c2319")],
              lightcolor=[("pressed", "#0f1a0d"), ("active", "#2a3a27")],
              darkcolor=[("pressed", "#0f1a0d"), ("active", "#2a3a27")])
    for name, base, hover, down in (("Accent", "#2f7d32", "#3a9a3e", "#256428"),
                                    ("Danger", "#8f2d2a", "#b03a36", "#6e211f")):
        style.configure(f"{name}.TButton", background=base, foreground="#ffffff", bordercolor=base,
                        lightcolor=base, darkcolor=base, font=(FONT_FAMILY, 9, "bold"), padding=(14, 5))
        style.map(f"{name}.TButton",
                  background=[("disabled", "#1a211a"), ("pressed", down), ("active", hover)],
                  foreground=[("disabled", "#4f594c")],
                  bordercolor=[("disabled", "#1a211a")],
                  lightcolor=[("disabled", "#1a211a"), ("pressed", down), ("active", hover)],
                  darkcolor=[("disabled", "#1a211a"), ("pressed", down), ("active", hover)])
    style.configure("Flat.TButton", background=BG, foreground=GREEN, bordercolor=BG, lightcolor=BG,
                    darkcolor=BG, padding=(2, 3))
    style.map("Flat.TButton", background=[("active", PANEL)], lightcolor=[("active", PANEL)],
              darkcolor=[("active", PANEL)], bordercolor=[("active", PANEL)])

    # Locked settings (server running) stay readable: disabled + selected still draws green.
    style.configure("TCheckbutton", background=BG, foreground=FG, indicatorbackground=FIELD,
                    indicatorforeground=GREEN, indicatorcolor=FIELD, upperbordercolor=BORDER,
                    lowerbordercolor=BORDER)
    style.map("TCheckbutton",
              background=[("active", BG)],
              foreground=[("disabled", FG)],
              indicatorbackground=[("disabled", "selected", "#2f7d32"), ("disabled", PANEL),
                                   ("selected", "#2f7d32"), ("pressed", PANEL)],
              indicatorcolor=[("disabled", "selected", "#2f7d32"), ("disabled", PANEL),
                              ("selected", "#2f7d32"), ("pressed", PANEL)],
              indicatorforeground=[("disabled", "selected", "#f2fff0"), ("selected", "#f2fff0")])

    for widget in ("TEntry", "TSpinbox", "TCombobox"):
        style.configure(widget, fieldbackground=FIELD, foreground=FG, insertcolor=GREEN, bordercolor=BORDER,
                        lightcolor=BORDER, darkcolor=BORDER, arrowcolor=GREEN, padding=3)
        style.map(widget,
                  fieldbackground=[("disabled", PANEL), ("readonly", PANEL if widget != "TCombobox" else FIELD)],
                  foreground=[("disabled", MUTED), ("readonly", FG)],
                  bordercolor=[("focus", GREEN_DK)],
                  arrowcolor=[("disabled", "#4f594c")],
                  selectbackground=[("readonly", FIELD)], selectforeground=[("readonly", FG)])
    style.configure("TSpinbox", arrowsize=int(12 * scale))
    style.configure("TCombobox", arrowsize=int(14 * scale))
    root.option_add("*TCombobox*Listbox.background", FIELD)
    root.option_add("*TCombobox*Listbox.foreground", FG)
    root.option_add("*TCombobox*Listbox.selectBackground", SELECT_BG)
    root.option_add("*TCombobox*Listbox.selectForeground", GREEN)

    style.configure("TNotebook", background=BG, bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER,
                    tabmargins=(2, 4, 2, 0))
    style.configure("TNotebook.Tab", background=PANEL, foreground=MUTED, padding=(16, 6),
                    bordercolor=BORDER, lightcolor=PANEL, darkcolor=PANEL, font=(FONT_FAMILY, 9, "bold"))
    style.map("TNotebook.Tab",
              background=[("selected", SELECT_BG), ("active", "#1d261b")],
              foreground=[("selected", GREEN), ("active", FG)],
              lightcolor=[("selected", GREEN_DK)], bordercolor=[("selected", GREEN_DK)],
              padding=[("selected", (16, 8))], expand=[("selected", (0, 0, 0, 2))])

    style.configure("Treeview", background=FIELD, fieldbackground=FIELD, foreground=FG, bordercolor=BORDER,
                    lightcolor=BORDER, darkcolor=BORDER, rowheight=int(22 * scale))
    style.map("Treeview", background=[("selected", SELECT_BG)], foreground=[("selected", GREEN)])
    style.configure("Treeview.Heading", background=PANEL, foreground=GREEN, bordercolor=BORDER,
                    lightcolor=PANEL, darkcolor=PANEL, relief="flat", font=(FONT_FAMILY, 9, "bold"), padding=(6, 4))
    style.map("Treeview.Heading", background=[("active", "#1d261b")])

    for orient in ("Vertical", "Horizontal"):
        style.configure(f"{orient}.TScrollbar", background="#2a3527", troughcolor=PANEL, bordercolor=PANEL,
                        lightcolor="#2a3527", darkcolor="#2a3527", arrowcolor=GREEN)
        style.map(f"{orient}.TScrollbar", background=[("active", "#3b5236"), ("pressed", GREEN_DK)],
                  lightcolor=[("active", "#3b5236")], darkcolor=[("active", "#3b5236")])
    return style


# ---------- pure helpers (unit-tested) ----------
def clean_host(text: str) -> str:
    """Bare host from a pasted address: strips scheme, path and port."""
    host = text.strip()
    for prefix in ("ws://", "http://", "https://"):
        if host.lower().startswith(prefix):
            host = host[len(prefix):]
    return host.split("/")[0].split(":")[0]


def access_badge(mode: str) -> tuple[str, str]:
    """(badge text, style level) for an access mode id ('open'/'approve'/'allowlist') or its label."""
    mode = ACCESS_CHOICES.get(mode, mode)
    label = next((k for k, v in ACCESS_CHOICES.items() if v == mode), "Open (anyone)")
    if mode not in ACCESS_CHOICES.values():
        mode = "open"
    return f"Access: {label}", ("warn" if mode == "open" else "ok")


def pending_texts(count: int) -> tuple[str, str]:
    """(Access tab label, Server panel banner) for N players waiting for approval."""
    if count <= 0:
        return "Access", ""
    noun = "player" if count == 1 else "players"
    return f"Access ({count} pending)", f"{count} {noun} waiting for approval → Access tab"


def split_extra_args(text: str, *, windows: bool | None = None) -> list[str]:
    """Split the Extra args box like a shell would.

    Windows keeps backslashes in paths (non-POSIX mode), but non-POSIX shlex
    also keeps the quote characters, so strip one matching pair per token:
    --lan-name "My LAN" must reach the server as My LAN.
    """
    import shlex
    windows = os.name == "nt" if windows is None else windows
    if not windows:
        return shlex.split(text)
    lexer = shlex.shlex(text, posix=False)
    lexer.whitespace_split = True
    lexer.commenters = ""
    out = []
    for token in lexer:
        if len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'":
            token = token[1:-1]
        out.append(token)
    return out


def tcp_probe(host: str, port: int, timeout: float = 3.0) -> dict:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return {"ok": True, "error": ""}
    except OSError as exc:
        return {"ok": False, "error": str(exc) or exc.__class__.__name__}


def mask_command(cmd: list[str]) -> list[str]:
    """Copy of cmd with the admin key hidden, for logging."""
    out = []
    for i, a in enumerate(cmd):
        if i > 0 and cmd[i - 1] == "--admin-key":
            out.append("***")
        elif a.startswith("--admin-key="):
            out.append("--admin-key=***")
        else:
            out.append(a)
    return out


def reachability_report(f: dict) -> list[tuple[str, str]]:
    """Turn a dict of facts into [(level, message)]; level is ok / warn / error / info.

    Keys (all optional): running, bind, pair_enabled, pair_start, pair_end, public_ip, ws_port,
    tcp ({'ok': bool, 'error': str} or None), status (the /status dict or None).
    """
    out: list[tuple[str, str]] = []
    running = bool(f.get("running"))
    ws = f.get("ws_port", 1337)
    out.append(("ok", "Server is running.") if running else ("error", "Server is not running. Press Start first."))

    bind = str(f.get("bind", ""))
    if bind.startswith("127."):
        out.append(("error", "Listening on 127.0.0.1 only: nobody else can connect. Set 'Listen on' to All interfaces."))
    elif bind in ("", "0.0.0.0", "::"):
        out.append(("ok", f"Listening on all interfaces ({bind or '0.0.0.0'})."))
    else:
        out.append(("info", f"Listening only on {bind}: players must use that address (or a forward to it)."))

    start, end = int(f.get("pair_start") or 0), int(f.get("pair_end") or 0)
    if not f.get("pair_enabled"):
        out.append(("warn", "Pair mode is off: only one relayed peer per player. Turn it on for 3+ players."))
    elif start <= 0 or end < start:
        out.append(("warn", "Pair mode is on but no fixed UDP range is set; ephemeral ports only work on this PC."))
    else:
        out.append(("ok", f"Pair mode on, UDP range {start}-{end} ({end - start + 1} ports): forward this range too."))

    ip = str(f.get("public_ip") or "").strip()
    tcp = f.get("tcp")
    if not ip:
        out.append(("warn", "Public IP unknown. Press Detect (or enter your DDNS name) first."))
    elif tcp is None:
        out.append(("info", f"Public address {ip}: TCP test skipped (server not running)."))
    elif tcp.get("ok"):
        out.append(("ok", f"TCP {ip}:{ws} connected: reachable (or your router does hairpin NAT)."))
    else:
        out.append(("warn", (
            f"TCP {ip}:{ws} failed ({tcp.get('error', 'no answer')}). From inside the LAN this is inconclusive: "
            "many routers don't support hairpin NAT. Ask a friend to run "
            f"Test-NetConnection {ip} -Port {ws} (Windows PowerShell) or nc -vz {ip} {ws} (macOS/Linux), "
            "or use an online port checker.")))

    status = f.get("status")
    if running and not status:
        out.append(("warn", "Could not read /status from the local status port."))
    elif status:
        relay = status.get("relay") or {}
        if not relay.get("enabled", True):
            out.append(("warn", "Relay is disabled in the server, so UDP traffic is never relayed."))
        else:
            out.append(("info", f"Relay packets: {relay.get('packetsRxTotal', 0)} in / {relay.get('packetsTxTotal', 0)} out."))
            rng = relay.get("pairPortRange")
            out.append(("info", f"pairPortRange: {rng[0]}-{rng[1]}" if rng else "pairPortRange: none (shared relay)"))
            for key, level, hint in (
                ("pairPortAllocationFailuresTotal", "error", "range exhausted or ports busy/blocked"),
                ("ambiguousDropsTotal", "warn", "enable pair mode with an open UDP range"),
                ("unmappedDropsTotal", "warn", "packets from unknown senders"),
            ):
                n = relay.get(key)
                if n is None:
                    continue
                out.append((level if n else "ok", f"{key}: {n}" + (f" ({hint})" if n else "")))
    out.append(("info", "UDP can't be tested from inside. When a friend connects, the log shows 'UDP RX' from their public IP."))
    return out


# ---------- setup guide (tag, text) ----------
# Tags: h1/h2 headings, p body, b bold, li bullet, code = selectable command (gets a Copy button).
def _platform_kind(platform: str) -> str:
    if platform.startswith("win"):
        return "windows"
    return "mac" if platform == "darwin" else "linux"


def guide_ip_hint(kind: str) -> str:
    return {"windows": "run ipconfig", "mac": "run ipconfig getifaddr en0",
            "linux": "run ip -4 addr or hostname -I"}[kind]


def guide_firewall(kind: str) -> list[tuple[str, str]]:
    if kind == "windows":
        return [
            ("h2", "3. Windows Firewall\n"),
            ("p", "In an administrator PowerShell. The network profile must be Private.\n"),
            ("code", 'New-NetFirewallRule -DisplayName "BZR server TCP" -Direction Inbound -Protocol TCP -LocalPort 1337 -Action Allow -Profile Private'),
            ("code", 'New-NetFirewallRule -DisplayName "BZR server UDP" -Direction Inbound -Protocol UDP -LocalPort 1338-1369 -Action Allow -Profile Private'),
            ("p", "Match the UDP range to your pair-port range. (StartServer.bat offers to add these same rules.)\n"),
        ]
    if kind == "mac":
        return [
            ("h2", "3. macOS firewall\n"),
            ("p", "If System Settings > Network > Firewall is on, allow incoming connections for Python when macOS asks "
                  "(or add it under Options). The built-in firewall works per app, so no port list is needed.\n"),
        ]
    return [
        ("h2", "3. Linux firewall\n"),
        ("p", "Only needed if a firewall is active. With ufw:\n"),
        ("code", "sudo ufw allow 1337/tcp && sudo ufw allow 1338:1369/udp"),
        ("p", "With firewalld:\n"),
        ("code", "sudo firewall-cmd --permanent --add-port=1337/tcp --add-port=1338-1369/udp && sudo firewall-cmd --reload"),
        ("p", "Match the UDP range to your pair-port range.\n"),
    ]


def guide_port_test(kind: str) -> str:
    return "Test-NetConnection <public-ip> -Port 1337" if kind == "windows" else "nc -vz <public-ip> 1337"


def build_guide(platform: str = sys.platform) -> list[tuple[str, str]]:
    """Setup guide segments with commands for the operator's own OS."""
    kind = _platform_kind(platform)
    return [
        ("h1", "Setup guide: LAN and internet play\n"),
        ("p", "Follow the numbered steps. Select any text, or use the Copy buttons next to commands.\n"),

        ("h2", "1. Start the server\n"),
        ("p", "Tick "), ("b", "Separate UDP port per peer pair"), ("p", ", set the range "), ("b", "1340"), ("p", " through "),
        ("b", "1369"), ("p", ", set "), ("b", "Listen on"), ("p", " to All interfaces, pick a state file, then press "),
        ("b", "Start"), ("p", ".\n"),

        ("h2", "2. Find this PC's LAN IP\n"),
        ("p", f"It is shown under \"Players connect with\" (or {guide_ip_hint(kind)}). Give this PC a DHCP address reservation in the "
              "router so it never changes. TP-Link: Advanced > Network > DHCP Server > Address Reservation.\n"),

        *guide_firewall(kind),

        ("h2", "4. LAN players: launch option\n"),
        ("p", "Use exactly (with ws://, the port and the trailing slash):\n"),
        ("code", "/bzrserver=ws://<LAN-IP>:1337/"),
        ("li", "Steam: Properties > Launch Options.\n"),
        ("li", "GOG Galaxy: Manage installation > Configure > Additional executables, or a desktop shortcut.\n"),
        ("li", "OpenShim players: Multiplayer setup screen > Custom (or net.ini [OpenShimSocket] "
               "MatchmakingRedirectAddress=<IP>, IP only).\n"),
        ("p", "Use the LAN IP even on the server PC, not 127.0.0.1.\n"),

        ("h2", "5. Same mods, launcher running\n"),
        ("p", "Everyone runs the same mod builds. GOG needs GOG Galaxy running and signed in; Steam needs Steam running.\n"),

        ("h2", "6. Playing\n"),
        ("p", "Multiplayer > Internet > Create Game / Join Game; the host clicks Launch when everyone is in. Match the map's "
              "player count (for example Tapping The Core is 2 players). CR co-op needs everyone to start together.\n"),

        ("h2", "7. Over the internet\n"),
        ("li", "(a) The router's WAN IP must equal your public IP (press Detect). If not, you are behind CGNAT: use Tailscale.\n"),
        ("li", "(b) Port-forward TCP 1337 and UDP 1338-1369 to this PC's LAN IP, same port numbers. TP-Link: Advanced > "
               "NAT Forwarding > Port Forwarding > Add, choose Port Range for UDP. "),
        ("b", "NEVER forward 8080"), ("p", " (the status/admin page).\n"),
        ("li", "(c) Send friends the Internet line:\n"), ("code", "/bzrserver=ws://<public-ip>:1337/"),
        ("p", "People in your house keep the LAN line.\n"),
        ("li", "(d) Set Access to \"Approve new players\" or \"Allowlist\" before friends connect. The server can't verify "
               "Steam/GOG tickets, so anyone who finds the port can claim any ID. Consider disabling the forwards between sessions.\n"),

        ("h2", "8. Verify\n"),
        ("li", "Press \"Check internet reachability\".\n"),
        ("li", "From outside, a friend runs this "
               + ("in PowerShell (on macOS/Linux: nc -vz <public-ip> 1337):\n" if kind == "windows"
                  else "in a terminal (on Windows PowerShell: Test-NetConnection <public-ip> -Port 1337):\n")),
        ("code", guide_port_test(kind)),
        ("li", "The log shows UDP RX from the friend's IP, and relay packet counts rise in a game.\n"),
        ("li", "If stuck connecting, check pairPortRange / pairPortAllocationFailuresTotal / ambiguousDropsTotal / "
               "unmappedDropsTotal on:\n"),
        ("code", "http://127.0.0.1:8080/status"),

        ("h2", "9. Alternative: Tailscale\n"),
        ("p", "Everyone joins your tailnet and uses the Tailscale/VPN address shown in \"Players connect with\". No port "
              "forwarding, and it works behind CGNAT.\n"),

        ("h2", "10. Troubleshooting\n"),
        ("li", "Lobby list empty / connection failed: server running? IP and launch option exact? Firewall open"
               + (" (network profile Private)? " if kind == "windows" else "? ")
               + "Port test (step 8) to 1337.\n"),
        ("li", "Stale full game listed: restart the server.\n"),
        ("li", "Stuck after Launch / desync: UDP 1338-1369 blocked, pair range exhausted, or mod files differ.\n"),
        ("li", "Disconnected at login: the client is still pointed at the official servers.\n"),
        ("li", "Pair ports needed: n(n-1)/2 per fully relayed lobby (4 players = 6). The 30-port range is shared across lobbies.\n"),
    ]


GUIDE: list[tuple[str, str]] = build_guide()


def render_guide(text: tk.Text, segments: list[tuple[str, str]], copy) -> None:
    """Fill a Text widget from GUIDE segments, then make it read-only (still selectable)."""
    text.tag_configure("h1", font=(TITLE_FAMILY, 15, "bold"), foreground=GREEN, spacing3=6)
    text.tag_configure("h2", font=(FONT_FAMILY, 11, "bold"), foreground=AMBER, spacing1=10, spacing3=2)
    text.tag_configure("p", foreground=FG)
    text.tag_configure("b", font=(FONT_FAMILY, 10, "bold"), foreground=GREEN)
    text.tag_configure("li", foreground=FG, lmargin1=18, lmargin2=34)
    text.tag_configure("code", font=(MONO_FAMILY, 10), foreground=GREEN, background="#162015",
                       lmargin1=34, lmargin2=34, spacing1=3, spacing3=3)
    text.configure(state="normal")
    for tag, value in segments:
        text.insert("end", value, tag)
        if tag == "code":
            btn = ttk.Button(text, text="Copy", width=6, command=lambda v=value: copy(v))
            text.insert("end", "  ")
            text.window_create("end", window=btn)
            text.insert("end", "\n")
    text.configure(state="disabled")


class ServerGUI:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.proc: subprocess.Popen[str] | None = None
        self.log_queue: queue.Queue[str] = queue.Queue()
        # Worker threads never touch Tk directly: they queue callables that
        # pump() runs on the UI thread.
        self.ui_queue: queue.Queue = queue.Queue()
        self._after_stop: list = []
        self._lan_busy = False
        self._starting = False
        self.closed = False
        self.poll_result: dict | None = None
        self.poll_lock = threading.Lock()
        self.stopping = False
        self.started_at = 0.0
        self._access_dirty_until = 0.0

        root.title("BZR Dedicated Server")
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.first_run = not CONFIG_FILE.exists()
        self.cfg = dict(DEFAULTS)
        if self.first_run:
            # New installs start in the mode the Setup Guide recommends; existing
            # configs without the key keep the shared relay they ran with.
            self.cfg.update(relay_pair_ports=True)
        self.cfg.update(self.load_config())
        self.vars: dict[str, tk.Variable] = {}
        self.build_ui()
        # Size from content so Windows display scaling never pushes the tabs off-screen.
        root.update_idletasks()
        w = min(max(root.winfo_reqwidth() + 16, MIN_DEFAULT_WIDTH), root.winfo_screenwidth() - 40)
        h = min(root.winfo_reqheight() + 16, root.winfo_screenheight() - 80, MAX_DEFAULT_HEIGHT)
        root.geometry(f"{w}x{h}")
        root.minsize(min(w, root.winfo_reqwidth()), min(h, root.winfo_reqheight()))
        for key in ("ws_port", "udp_port", "relay_port", "relay_pair_ports", "relay_pair_port_start",
                    "relay_pair_port_end", "bind", "public_host"):
            self.vars[key].trace_add("write", lambda *_: self.update_switch())
        self.update_access_badge()
        self.refresh_buttons()

        threading.Thread(target=self.poll_loop, name="poll", daemon=True).start()
        self.root.after(100, self.pump)

    # ---------- config ----------
    def load_config(self) -> dict:
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        # Drop values of the wrong type (hand edits) so they fall back to defaults.
        return {k: v for k, v in data.items()
                if k not in DEFAULTS or isinstance(v, type(DEFAULTS[k])) or
                (isinstance(DEFAULTS[k], int) and isinstance(v, int))}

    def save_config(self) -> None:
        data = {}
        for key, var in self.vars.items():
            try:
                value = var.get()
            except tk.TclError:  # blank/invalid spinbox: keep the previous saved value
                continue
            if key == "bind":
                value = BIND_CHOICES.get(value, value)
            elif key == "access_mode":
                value = ACCESS_CHOICES.get(value, value)
            data[key] = value
        self.cfg.update(data)
        try:
            CONFIG_FILE.write_text(json.dumps(self.cfg, indent=2), encoding="utf-8")
        except OSError as exc:
            self.append_log(f"[gui] could not save settings: {exc}\n")

    # ---------- layout ----------
    def build_ui(self) -> None:
        outer = ttk.Frame(self.root)
        outer.pack(fill="both", expand=True)

        header = tk.Frame(outer, bg=HEADER_BG)
        header.pack(fill="x")
        tk.Label(header, text=APP_TITLE, bg=HEADER_BG, fg=GREEN, anchor="w",
                 font=(TITLE_FAMILY, 15, "bold")).pack(side="left", padx=14, pady=8)
        tk.Label(header, text="LAN / internet lobby + UDP relay", bg=HEADER_BG, fg=MUTED,
                 font=(FONT_FAMILY, 9)).pack(side="right", padx=14)
        tk.Frame(outer, height=2, bg=GREEN_DK).pack(fill="x")

        body = ttk.Frame(outer, padding=8)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=0)  # settings: fixed width
        body.columnconfigure(1, weight=1)  # tabs + log take the rest
        body.rowconfigure(0, weight=1)

        left = ttk.Frame(body)
        left.grid(row=0, column=0, sticky="ns")
        self.settings_frame = left
        self.settings_hint = ttk.Label(left, text="", style="Warn.TLabel")
        self.settings_hint.pack(fill="x", pady=(0, 4))

        def var(key: str, kind=tk.StringVar):
            v = kind(value=self.cfg.get(key, DEFAULTS[key]))
            self.vars[key] = v
            return v

        def group(title: str) -> ttk.LabelFrame:
            g = ttk.LabelFrame(left, text=title, padding=8)
            g.pack(fill="x", pady=(0, 8))
            return g

        def label(parent: tk.Widget, text: str, r: int, c: int = 0) -> None:
            ttk.Label(parent, text=text).grid(row=r, column=c, sticky="w", padx=(0, 6), pady=2)

        def spin(parent: tk.Widget, key: str, r: int, c: int, lo: int = 1, hi: int = 65535) -> None:
            ttk.Spinbox(parent, from_=lo, to=hi, textvariable=var(key, tk.IntVar), width=7).grid(row=r, column=c, sticky="w", pady=2)

        # Network / ports
        net = group("Network / ports")
        label(net, "Listen on", 0)
        bind_label = next((k for k, v in BIND_CHOICES.items() if v == self.cfg.get("bind")), self.cfg.get("bind"))
        bind_var = tk.StringVar(value=bind_label)
        self.vars["bind"] = bind_var
        ttk.Combobox(net, textvariable=bind_var, values=list(BIND_CHOICES), width=30).grid(
            row=0, column=1, columnspan=3, sticky="w", pady=2)
        label(net, "Lobby TCP", 1)
        spin(net, "ws_port", 1, 1)
        label(net, "Handshake UDP", 1, 2)
        spin(net, "udp_port", 1, 3)
        label(net, "Status port", 2)
        spin(net, "health_port", 2, 1)
        ttk.Checkbutton(net, text="Advertise to OpenShim LAN browsers", variable=var("lan_discovery", tk.BooleanVar)).grid(
            row=3, column=0, columnspan=4, sticky="w")

        # Relay
        rel = group("Relay")
        ttk.Checkbutton(rel, text="Relay enabled", variable=var("relay_enabled", tk.BooleanVar)).grid(
            row=0, column=0, columnspan=2, sticky="w")
        label(rel, "Relay UDP", 0, 2)
        spin(rel, "relay_port", 0, 3)
        ttk.Checkbutton(rel, text="Separate UDP port per peer pair (3+ relayed players)",
                        variable=var("relay_pair_ports", tk.BooleanVar)).grid(row=1, column=0, columnspan=4, sticky="w")
        label(rel, "Pair UDP from", 2)
        spin(rel, "relay_pair_port_start", 2, 1, lo=0)
        label(rel, "through", 2, 2)
        spin(rel, "relay_pair_port_end", 2, 3, lo=0)

        # Server / state
        srv = group("Server / state")
        label(srv, "State file", 0)
        sf = ttk.Frame(srv)
        sf.grid(row=0, column=1, columnspan=3, sticky="w")
        ttk.Entry(sf, textvariable=var("state_file"), width=30).pack(side="left")
        ttk.Button(sf, text="…", width=3, command=self.browse_state).pack(side="left", padx=(4, 0))
        label(srv, "Max lobbies", 1)
        spin(srv, "max_lobbies", 1, 1, hi=100000)
        ttk.Checkbutton(srv, text="Keep empty lobbies", variable=var("retain_empty_lobbies", tk.BooleanVar)).grid(
            row=1, column=2, columnspan=2, sticky="w")

        # Logging / privacy
        logg = group("Logging")
        label(logg, "Log level", 0)
        ttk.Combobox(logg, textvariable=var("log_level"), values=["DEBUG", "INFO", "WARNING", "ERROR"],
                     width=9, state="readonly").grid(row=0, column=1, sticky="w", pady=2)
        ttk.Checkbutton(logg, text="Hide player IPs outside their game",
                        variable=var("redact_addresses", tk.BooleanVar)).grid(row=1, column=0, columnspan=4, sticky="w")

        # Advanced (collapsed by default)
        adv_box = ttk.Frame(left)
        adv_box.pack(fill="x")
        self.adv_open = False
        self.adv_toggle = ttk.Button(adv_box, text="▸ Advanced", command=self.toggle_advanced, style="Flat.TButton")
        self.adv_toggle.pack(anchor="w")
        self.adv_frame = ttk.Frame(adv_box, padding=(0, 4, 0, 0))
        label(self.adv_frame, "Admin key", 0)
        ttk.Entry(self.adv_frame, textvariable=var("admin_key"), show="•", width=30).grid(row=0, column=1, sticky="w", pady=2)
        label(self.adv_frame, "Admin IDs", 1)
        ttk.Entry(self.adv_frame, textvariable=var("admin_ids"), width=30).grid(row=1, column=1, sticky="w", pady=2)
        label(self.adv_frame, "MOTD", 2)
        ttk.Entry(self.adv_frame, textvariable=var("motd"), width=30).grid(row=2, column=1, sticky="w", pady=2)
        python_var = var("python")
        nxt = 3
        if not FROZEN:
            label(self.adv_frame, "Python", nxt)
            pf = ttk.Frame(self.adv_frame)
            pf.grid(row=nxt, column=1, sticky="w")
            ttk.Entry(pf, textvariable=python_var, width=26).pack(side="left")
            ttk.Button(pf, text="…", width=3, command=self.browse_python).pack(side="left", padx=(4, 0))
            nxt += 1
        label(self.adv_frame, "Extra args", nxt)
        ttk.Entry(self.adv_frame, textvariable=var("extra_args"), width=30).grid(row=nxt, column=1, sticky="w", pady=2)

        # Right: controls + live + connect info above, tabs below.
        right = ttk.Frame(body, padding=(10, 0, 0, 0))
        right.grid(row=0, column=1, sticky="nsew")
        top = ttk.Frame(right)
        top.pack(fill="x")
        top.columnconfigure(2, weight=1)

        controls = ttk.LabelFrame(top, text="Server", padding=8)
        controls.grid(row=0, column=0, sticky="nsew")
        self.state_label = ttk.Label(controls, text="Stopped", font=(FONT_FAMILY, 12, "bold"), foreground=RED)
        self.state_label.pack(anchor="w")
        self.access_badge = ttk.Label(controls, text="", style="BadgeWarn.TLabel", padding=(8, 2))
        self.access_badge.pack(anchor="w", pady=(4, 0))
        self.pending_banner = ttk.Label(controls, text="", style="Attn.TLabel", padding=(8, 3), cursor="hand2")
        self.pending_banner.bind("<Button-1>", lambda _e: self.show_access_tab())
        btns = ttk.Frame(controls)
        btns.pack(fill="x", pady=(8, 0))
        self.start_btn = ttk.Button(btns, text="Start", command=self.start, style="Accent.TButton")
        self.stop_btn = ttk.Button(btns, text="Stop", command=self.stop, style="Danger.TButton")
        self.restart_btn = ttk.Button(btns, text="Restart", command=self.restart)
        for b in (self.start_btn, self.stop_btn, self.restart_btn):
            b.pack(side="left", padx=(0, 4))
        btns2 = ttk.Frame(controls)
        btns2.pack(fill="x", pady=(4, 0))
        ttk.Button(btns2, text="Status page", command=self.open_status).pack(side="left", padx=(0, 4))
        ttk.Button(btns2, text="State folder", command=self.open_state_folder).pack(side="left")

        stats = ttk.LabelFrame(top, text="Live", padding=8)
        stats.grid(row=0, column=1, sticky="nsew", padx=8)
        self.stat_vars: dict[str, tk.StringVar] = {}
        for i, (key, text) in enumerate(
            [
                ("uptime", "Uptime"),
                ("users", "Players online"),
                ("lobbies", "Lobbies"),
                ("routes", "Relay routes"),
                ("messages", "WS messages"),
                ("relaypk", "Relay packets"),
            ]
        ):
            ttk.Label(stats, text=text, style="Muted.TLabel").grid(row=i, column=0, sticky="w", padx=(0, 10))
            v = tk.StringVar(value="–")
            self.stat_vars[key] = v
            ttk.Label(stats, textvariable=v).grid(row=i, column=1, sticky="w")

        conn = ttk.LabelFrame(top, text="Players connect with", padding=8)
        conn.grid(row=0, column=2, sticky="nsew")
        conn.columnconfigure(1, weight=1)
        self.lan_var = tk.StringVar()
        self.switch_var = tk.StringVar()
        self.public_var = tk.StringVar(value=self.cfg.get("public_host", ""))
        self.vars["public_host"] = self.public_var
        self.wan_switch_var = tk.StringVar()

        ttk.Label(conn, text="LAN").grid(row=0, column=0, sticky="w", padx=(0, 6))
        ttk.Entry(conn, textvariable=self.switch_var, state="readonly").grid(row=0, column=1, columnspan=2, sticky="we")
        ttk.Button(conn, text="Copy", width=6, command=self.copy_switch).grid(row=0, column=3, padx=(4, 0))
        self.lan_var_label = ttk.Label(conn, textvariable=self.lan_var, style="Muted.TLabel", wraplength=430, justify="left")
        self.lan_var_label.grid(row=1, column=1, columnspan=3, sticky="we")
        self.port_warn = ttk.Label(conn, style="Warn.TLabel")
        self.port_warn.grid(row=2, column=1, columnspan=3, sticky="w")

        ttk.Label(conn, text="Internet").grid(row=3, column=0, sticky="w", padx=(0, 6), pady=(6, 0))
        ttk.Entry(conn, textvariable=self.public_var, width=18).grid(row=3, column=1, sticky="we", pady=(6, 0))
        self.detect_btn = ttk.Button(conn, text="Detect", width=7, command=self.detect_public_ip)
        self.detect_btn.grid(row=3, column=2, padx=(4, 0), pady=(6, 0))
        ttk.Entry(conn, textvariable=self.wan_switch_var, state="readonly").grid(row=4, column=1, columnspan=2, sticky="we", pady=(2, 0))
        ttk.Button(conn, text="Copy", width=6, command=self.copy_wan_switch).grid(row=4, column=3, padx=(4, 0), pady=(2, 0))
        self.wan_note = ttk.Label(conn, style="Muted.TLabel", wraplength=430, justify="left")
        self.wan_note.grid(row=5, column=1, columnspan=3, sticky="we")
        self.openshim_note = ttk.Label(conn, style="Muted.TLabel", wraplength=430, justify="left")
        self.openshim_note.grid(row=6, column=1, columnspan=3, sticky="we", pady=(6, 0))
        wrapped = (self.lan_var_label, self.wan_note, self.openshim_note)

        def _rewrap(event: tk.Event) -> None:
            wl = max(200, event.width - 90)
            for lbl in wrapped:
                lbl.configure(wraplength=wl)
        conn.bind("<Configure>", _rewrap)
        self.diag_btn = ttk.Button(conn, text="Check internet reachability", command=self.check_reachability)
        self.diag_btn.grid(row=7, column=0, columnspan=4, sticky="w", pady=(8, 0))
        diag_frame = ttk.Frame(conn)
        diag_frame.grid(row=8, column=0, columnspan=4, sticky="we", pady=(4, 0))
        diag_frame.columnconfigure(0, weight=1)
        self.diag_text = tk.Text(diag_frame, height=6, width=1, wrap="word", font=(MONO_FAMILY, 9), state="disabled",
                                 background=FIELD, foreground=FG, relief="flat", borderwidth=0, padx=6, pady=4)
        diag_scroll = ttk.Scrollbar(diag_frame, orient="vertical", command=self.diag_text.yview)
        self.diag_text.configure(yscrollcommand=diag_scroll.set)
        self.diag_text.grid(row=0, column=0, sticky="we")
        diag_scroll.grid(row=0, column=1, sticky="ns")
        for tag, colour in (("ok", GREEN), ("warn", AMBER), ("error", RED), ("info", MUTED)):
            self.diag_text.tag_configure(tag, foreground=colour)
        self.update_lan()

        # Tabs.
        nb = ttk.Notebook(right)
        nb.pack(fill="both", expand=True, pady=(8, 0))
        self.notebook = nb

        log_frame = ttk.Frame(nb)
        self.log_text = tk.Text(log_frame, wrap="none", height=12, font=(MONO_FAMILY, 9), state="disabled",
                                background=FIELD, foreground=FG, insertbackground=GREEN, relief="flat",
                                selectbackground=SELECT_BG)
        ys = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        xs = ttk.Scrollbar(log_frame, orient="horizontal", command=self.log_text.xview)
        self.log_text.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        ys.grid(row=0, column=1, sticky="ns")
        xs.grid(row=1, column=0, sticky="ew")
        log_frame.rowconfigure(0, weight=1)
        log_frame.columnconfigure(0, weight=1)
        self.log_text.tag_configure("warn", foreground=AMBER)
        self.log_text.tag_configure("error", foreground=RED)
        self.log_text.tag_configure("gui", foreground=GUI_BLUE)
        lb = ttk.Frame(log_frame)
        lb.grid(row=2, column=0, columnspan=2, sticky="ew", pady=2)
        self.autoscroll = tk.BooleanVar(value=True)
        ttk.Checkbutton(lb, text="Auto-scroll", variable=self.autoscroll).pack(side="left")
        ttk.Button(lb, text="Clear", command=self.clear_log).pack(side="left", padx=4)
        nb.add(log_frame, text="Log")
        self.log_frame = log_frame

        self.build_guide_tab(nb)

        self.lobby_tree = self.make_tree(
            nb, "Lobbies",
            [("id", "ID", 60), ("name", "Name", 260), ("owner", "Host", 160), ("players", "Players", 70), ("flags", "Flags", 140), ("members", "Members", 300)],
        )
        self.user_tree = self.make_tree(
            nb, "Players",
            [("id", "User ID", 160), ("name", "Name", 180), ("auth", "Auth", 150), ("lobby", "Lobby", 70), ("where", "Where", 90), ("wan", "Address", 180), ("admin", "Admin", 60), ("muted", "Muted", 60)],
            buttons=[
                ("Approve", lambda: self.act_selected(self.user_tree, "approve")),
                ("Kick", lambda: self.act_selected(self.user_tree, "kick")),
                ("Ban 1 h", lambda: self.act_selected(self.user_tree, "ban", duration="1h", reason="kicked for 1 hour",
                                                      confirm="Ban {n} for one hour?")),
                ("Ban", lambda: self.act_selected(self.user_tree, "ban", confirm="Ban {n}?")),
                ("Ban + IP", lambda: self.act_selected(self.user_tree, "ban", withIp=True, confirm="Ban {n} and their IP?")),
                ("Mute 15 min", lambda: self.act_selected(self.user_tree, "mute", duration="15m")),
                ("Unmute", lambda: self.act_selected(self.user_tree, "unmute")),
            ],
        )
        self.build_access_tab(nb)
        if self.first_run:
            nb.select(self.guide_frame)

    def toggle_advanced(self) -> None:
        self.adv_open = not self.adv_open
        if self.adv_open:
            self.adv_frame.pack(fill="x", pady=(4, 0))
        else:
            self.adv_frame.pack_forget()
        self.adv_toggle.configure(text="▾ Advanced" if self.adv_open else "▸ Advanced")
        # Grow the window (within the screen) so the expanded fields are not clipped.
        self.root.update_idletasks()
        short = self.settings_frame.winfo_reqheight() - self.settings_frame.winfo_height()
        if short > 0:
            self.root.geometry(f"{self.root.winfo_width()}x{min(self.root.winfo_height() + short, self.root.winfo_screenheight() - 80)}")

    def show_access_tab(self) -> None:
        self.notebook.select(self.access_frame)

    def build_guide_tab(self, nb: ttk.Notebook) -> None:
        frame = ttk.Frame(nb)
        self.guide_frame = frame
        text = tk.Text(frame, wrap="word", height=12, font=(FONT_FAMILY, 10), background=FIELD, foreground=FG,
                       relief="flat", padx=14, pady=10, cursor="arrow", selectbackground=SELECT_BG,
                       spacing1=2, spacing3=2)
        ys = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=ys.set)
        text.grid(row=0, column=0, sticky="nsew")
        ys.grid(row=0, column=1, sticky="ns")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        render_guide(text, GUIDE, self.copy_text)
        self.guide_text = text
        nb.add(frame, text="Setup Guide")

    def copy_text(self, value: str) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(value)

    def make_tree(self, parent: tk.Widget, title: str, cols: list[tuple[str, str, int]], *,
                  buttons: list | None = None, height: int = 8) -> ttk.Treeview:
        if isinstance(parent, ttk.Notebook):
            frame = ttk.Frame(parent)
            parent.add(frame, text=title)
        else:
            frame = ttk.LabelFrame(parent, text=title, padding=4)
            frame.pack(fill="both", expand=True, pady=(0, 4))
        body = ttk.Frame(frame)
        body.pack(fill="both", expand=True)
        tree = ttk.Treeview(body, columns=[c[0] for c in cols], show="headings", height=height)
        for key, text, width in cols:
            tree.heading(key, text=text)
            tree.column(key, width=width, anchor="w")
        ys = ttk.Scrollbar(body, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=ys.set)
        tree.pack(side="left", fill="both", expand=True)
        ys.pack(side="left", fill="y")
        if buttons:
            bar = ttk.Frame(frame)
            bar.pack(fill="x", pady=(4, 0))
            for text, cmd in buttons:
                ttk.Button(bar, text=text, command=cmd).pack(side="left", padx=(0, 4))
        return tree

    def build_access_tab(self, nb: ttk.Notebook) -> None:
        frame = ttk.Frame(nb, padding=4)
        nb.add(frame, text="Access")
        self.access_frame = frame
        top = ttk.Frame(frame)
        top.pack(fill="x", pady=(0, 4))
        ttk.Label(top, text="Who can join").pack(side="left")
        mode_label = next((k for k, v in ACCESS_CHOICES.items() if v == self.cfg.get("access_mode")), "Open (anyone)")
        self.access_mode_var = tk.StringVar(value=mode_label)
        self.vars["access_mode"] = self.access_mode_var
        box = ttk.Combobox(top, textvariable=self.access_mode_var, values=list(ACCESS_CHOICES), state="readonly", width=22)
        box.pack(side="left", padx=(6, 12))
        box.bind("<<ComboboxSelected>>", lambda _e: self.apply_access_settings())
        self.pin_var = tk.BooleanVar(value=bool(self.cfg.get("pin_ips", False)))
        self.vars["pin_ips"] = self.pin_var
        ttk.Checkbutton(top, text="Re-approve players who connect from a new IP", variable=self.pin_var,
                        command=self.apply_access_settings).pack(side="left")
        ttk.Label(frame, style="Muted.TLabel", wraplength=900, justify="left", text=(
            "Players are recognised by the ID their game reports. Steam/GOG logins cannot be verified without "
            "the publisher's keys, so this keeps strangers out but cannot stop someone copying a friend's public "
            "Steam ID. In Approve mode an unknown player's login waits here for 30 s; approve later and their "
            "game gets in on its next automatic retry."
        )).pack(fill="x", pady=(0, 4))
        self.pending_tree = self.make_tree(
            frame, "Waiting for approval",
            [("id", "User ID", 170), ("name", "Name", 170), ("ip", "IP", 130), ("reason", "Why", 200), ("since", "Since", 190)],
            height=3,
            buttons=[
                ("Approve", lambda: self.act_selected(self.pending_tree, "approve")),
                ("Deny", lambda: self.act_selected(self.pending_tree, "deny")),
                ("Ban", lambda: self.act_selected(self.pending_tree, "ban", confirm="Ban {n}?")),
            ],
        )
        self.approved_tree = self.make_tree(
            frame, "Approved players",
            [("id", "User ID", 170), ("name", "Name", 170), ("ips", "Known IPs", 200), ("seen", "Last seen", 150), ("since", "Approved", 150)],
            height=4,
            buttons=[
                ("Remove", lambda: self.act_selected(self.approved_tree, "remove", confirm="Remove {n} from approved players?")),
                ("Ban", lambda: self.act_selected(self.approved_tree, "ban", confirm="Ban {n}?")),
            ],
        )
        self.bans_tree = self.make_tree(
            frame, "Bans",
            [("what", "Banned", 200), ("kind", "Type", 60), ("name", "Name", 170), ("reason", "Reason", 200), ("at", "When", 150), ("expires", "Expires", 150)],
            height=3,
            buttons=[("Unban", self.unban_selected)],
        )
        self.mutes_tree = self.make_tree(
            frame, "Muted players",
            [("id", "User ID", 170), ("name", "Name", 170), ("reason", "Reason", 200), ("at", "When", 150), ("expires", "Expires", 150)],
            height=3,
            buttons=[("Unmute", lambda: self.act_selected(self.mutes_tree, "unmute"))],
        )
        self.identities_tree = self.make_tree(
            frame, "Protected identities (OpenShim community key)",
            [("id", "User ID", 170), ("name", "Name", 170), ("registered", "Registered", 150), ("seen", "Last seen", 150), ("failed", "Wrong key", 80)],
            height=3,
            buttons=[("Reset", lambda: self.act_selected(
                self.identities_tree, "resetIdentity",
                confirm="Forget {n}'s key? Their next login with a key registers a new one."))],
        )
        self.last_pending_count = 0

    # ---------- access actions ----------
    def apply_access_settings(self) -> None:
        mode = ACCESS_CHOICES.get(self.access_mode_var.get(), "open")
        self.cfg["access_mode"], self.cfg["pin_ips"] = mode, bool(self.pin_var.get())
        self.update_access_badge()
        try:
            self.save_config()
        except tk.TclError as exc:
            self.append_log(f"[gui] could not save settings: {exc}\n")
        if self.running():
            self._access_dirty_until = time.time() + ACCESS_SYNC_HOLDOFF
            self.post_access({"action": "set", "mode": mode, "pinIps": bool(self.pin_var.get())})

    def act_selected(self, tree: ttk.Treeview, action: str, confirm: str = "", **extra) -> None:
        sel = tree.selection()
        if not sel:
            messagebox.showinfo("Access", "Select a player first.")
            return
        if not self.running():
            messagebox.showinfo("Access", "Start the server to manage players.")
            return
        for iid in sel:
            name = str(tree.set(iid, "name")) if "name" in tree["columns"] else ""
            if confirm and not messagebox.askyesno("Access", confirm.format(n=name or iid)):
                continue
            self.post_access({"action": action, "id": iid, "name": name, **extra})
        if action == "kick" and ACCESS_CHOICES.get(self.access_mode_var.get(), "open") == "open":
            self.append_log("[gui] Kicked. In Open mode the game reconnects by itself within seconds; "
                            "use Ban 1 h (or Approve/Allowlist mode) to keep someone out.\n", "gui")

    def unban_selected(self) -> None:
        if not self.running():
            messagebox.showinfo("Access", "Start the server to manage bans.")
            return
        if not self.bans_tree.selection():
            messagebox.showinfo("Access", "Select a ban first.")
            return
        for iid in self.bans_tree.selection():
            kind, _, value = iid.partition(":")
            self.post_access({"action": "unban", ("ip" if kind == "ip" else "id"): value})

    def post_access(self, body: dict) -> None:
        port = self.cfg.get("health_port", 8080)

        def _send() -> None:
            try:
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}/access", data=json.dumps(body).encode("utf-8"),
                    method="POST", headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=5) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                self.call_main(lambda: self.show_access(data.get("access") or {}))
            except Exception as exc:
                self.log_queue.put(f"[gui] access action {body.get('action')} failed: {exc}\n")

        threading.Thread(target=_send, name="access-post", daemon=True).start()

    @staticmethod
    def fill_tree(tree: ttk.Treeview, rows: list[tuple[str, tuple]]) -> None:
        """Update rows keyed by iid in place: selection, scroll position and
        order survive the 2-second refresh."""
        wanted: dict[str, tuple] = {}
        for iid, values in rows:
            wanted.setdefault(str(iid), values)
        for iid in tree.get_children():
            if iid not in wanted:
                tree.delete(iid)
        for index, (iid, values) in enumerate(wanted.items()):
            values = tuple("" if v is None else v for v in values)
            if tree.exists(iid):
                if tuple(str(x) for x in tree.item(iid, "values")) != tuple(str(x) for x in values):
                    tree.item(iid, values=values)
                if tree.index(iid) != index:
                    tree.move(iid, "", index)
            else:
                tree.insert("", index, iid=iid, values=values)

    def show_access(self, access: dict) -> None:
        self.fill_tree(self.pending_tree, [
            (p["id"], (p["id"], p.get("name", ""), p.get("ip", ""), p.get("reason", ""),
                       p.get("since", "") + ("" if p.get("waiting") else "  (refused, will retry)")))
            for p in access.get("pending", []) if p.get("id")
        ])
        self.fill_tree(self.approved_tree, [
            (a["id"], (a["id"], a.get("name", ""), ", ".join(a.get("ips", [])), a.get("lastSeen", ""), a.get("approvedAt", "")))
            for a in access.get("approved", []) if a.get("id")
        ])
        self.fill_tree(self.bans_tree, [
            (f"id:{b['id']}", (b["id"], "ID", b.get("name", ""), b.get("reason", ""), b.get("at", ""), b.get("expiresAt", "never")))
            for b in access.get("bannedIds", []) if b.get("id")
        ] + [
            (f"ip:{b['ip']}", (b["ip"], "IP", b.get("name", ""), b.get("reason", ""), b.get("at", ""), b.get("expiresAt", "never")))
            for b in access.get("bannedIps", []) if b.get("ip")
        ])
        self.fill_tree(self.mutes_tree, [
            (m["id"], (m["id"], m.get("name", ""), m.get("reason", ""), m.get("at", ""), m.get("expiresAt", "until unmuted")))
            for m in access.get("mutedIds", []) if m.get("id")
        ])
        self.fill_tree(self.identities_tree, [
            (i["id"], (i["id"], i.get("name", ""), i.get("registeredAt", ""), i.get("lastSeen", ""), i.get("failedAttempts", 0)))
            for i in access.get("identities", []) if i.get("id")
        ])
        live_mode = access.get("mode")
        if live_mode in ACCESS_CHOICES.values() and time.time() >= self._access_dirty_until:
            self.access_mode_var.set(next(k for k, v in ACCESS_CHOICES.items() if v == live_mode))
        self.update_access_badge()
        waiting = sum(1 for p in access.get("pending", []) if p.get("waiting"))
        self.show_pending(waiting)
        if waiting > self.last_pending_count:
            self.root.bell()
            self.append_log(f"[gui] {waiting} player(s) waiting for approval (Access tab)\n", "gui")
        self.last_pending_count = waiting

    def update_access_badge(self) -> None:
        text, level = access_badge(self.access_mode_var.get())
        self.access_badge.configure(text=text, style="BadgeWarn.TLabel" if level == "warn" else "BadgeOk.TLabel")

    def show_pending(self, waiting: int) -> None:
        tab_text, banner = pending_texts(waiting)
        self.notebook.tab(self.access_frame, text=tab_text)
        if banner:
            self.pending_banner.configure(text=banner)
            if not self.pending_banner.winfo_manager():
                self.pending_banner.pack(anchor="w", pady=(6, 0), after=self.access_badge)
        else:
            self.pending_banner.pack_forget()

    # ---------- actions ----------
    def browse_state(self) -> None:
        path = filedialog.asksaveasfilename(
            title="State file", defaultextension=".json", filetypes=[("JSON", "*.json")],
            initialfile=Path(self.vars["state_file"].get()).name,
            initialdir=str(Path(self.vars["state_file"].get()).parent),
            confirmoverwrite=False,
        )
        if path:
            self.vars["state_file"].set(path)

    def browse_python(self) -> None:
        types = [("Python", "python*.exe"), ("All", "*.*")] if os.name == "nt" else [("All files", "*")]
        path = filedialog.askopenfilename(title="Python interpreter (python3 or .venv/bin/python)", filetypes=types)
        if path:
            self.vars["python"].set(path)

    def build_command(self) -> list[str]:
        v = {k: var.get() for k, var in self.vars.items()}
        bind = BIND_CHOICES.get(v["bind"], v["bind"]).strip() or "0.0.0.0"
        launcher = [str(SERVER_SCRIPT)] if FROZEN else [v["python"], "-u", str(SERVER_SCRIPT)]
        if not 1 <= int(v["health_port"]) <= 65535:
            raise ValueError("Status port must be 1-65535: the launcher reads players, lobbies and access through it.")
        state_file = str(v["state_file"]).strip()
        if not state_file:
            raise ValueError("Choose a state file.")
        cmd = launcher + [
            "--ws-host", bind, "--ws-port", str(v["ws_port"]),
            "--udp-host", bind, "--udp-port", str(v["udp_port"]),
            "--relay-host", bind, "--relay-port", str(v["relay_port"]),
            "--health-port", str(v["health_port"]),
            "--state-file", state_file,
            # Keep the audit trail beside the state it describes.
            "--audit-log", str(Path(state_file).expanduser().parent / "audit.log"),
            "--max-lobbies", str(v["max_lobbies"]),
            "--log-level", v["log_level"],
            "--stdin-control",
        ]
        if not v["relay_enabled"]:
            cmd.append("--disable-relay")
        if v["relay_pair_ports"]:
            start, end = int(v["relay_pair_port_start"]), int(v["relay_pair_port_end"])
            if (start, end) != (0, 0) and not 1 <= start <= end <= 65535:
                raise ValueError("Pair UDP bounds must both be 0 (this PC only), or a range within 1-65535.")
            if start == end == 0 and not bind.startswith("127."):
                raise ValueError("Choose a fixed pair UDP range for LAN/internet, or listen on 127.0.0.1 for ephemeral ports.")
            cmd += ["--relay-pair-ports", "--relay-pair-port-start", str(start), "--relay-pair-port-end", str(end)]
        else:
            cmd.append("--no-relay-pair-ports")
        cmd += ["--access-mode", ACCESS_CHOICES.get(v["access_mode"], v["access_mode"]),
                "--pin-ips" if v["pin_ips"] else "--no-pin-ips"]
        if v["retain_empty_lobbies"]:
            cmd.append("--retain-empty-lobbies")
        if v["redact_addresses"]:
            cmd.append("--redact-addresses")
        if v.get("lan_discovery"):
            cmd.append("--lan-discovery")
        if v["motd"].strip():
            cmd += ["--motd", v["motd"]]
        if v["admin_key"].strip():
            cmd += ["--admin-key", v["admin_key"]]
        if v["admin_ids"].strip():
            cmd += ["--admin-ids", v["admin_ids"]]
        if v["extra_args"].strip():
            cmd += split_extra_args(v["extra_args"])
        return cmd

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self) -> None:
        if self.running() or self.stopping or getattr(self, "_starting", False):
            return
        try:
            cmd = self.build_command()
        except (tk.TclError, ValueError) as exc:
            messagebox.showerror("Invalid setting", str(exc))
            return
        self.save_config()
        state_dir = Path(self.vars["state_file"].get()).expanduser().parent
        try:
            state_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showerror("State folder", f"Cannot create the state folder:\n{state_dir}\n\n{exc}\n\n"
                                                 "Choose a state file in a folder you can write to.")
            return
        if FROZEN:
            self._launch(cmd)
            return
        # Checking (and possibly installing) dependencies can take a while:
        # do it off the UI thread so the window stays responsive.
        self._starting = True
        self.start_btn.state(["disabled"])
        self.append_log("[gui] checking Python and dependencies…\n", "gui")
        threading.Thread(target=self._check_then_launch, args=(cmd,), name="check-python", daemon=True).start()

    def _check_then_launch(self, cmd: list[str]) -> None:
        python = cmd[0]
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            probe = subprocess.run([python, "-c", "import sys, websockets; assert sys.version_info >= (3, 9)"],
                                   capture_output=True, timeout=30, creationflags=flags)
            ok = probe.returncode == 0
        except (OSError, subprocess.TimeoutExpired) as exc:
            # Build the text now: `exc` is unbound once this block ends.
            message = f"{python}\n\n{exc}\n\nPick a Python interpreter under Advanced."
            self.call_main(lambda: self._start_failed("Python not usable", message))
            return
        if ok:
            self.call_main(lambda: self._launch(cmd))
        else:
            self.call_main(lambda: self._offer_install(cmd))

    def _start_failed(self, title: str, message: str) -> None:
        self._starting = False
        self.refresh_buttons()
        messagebox.showerror(title, message)

    def _offer_install(self, cmd: list[str]) -> None:
        python = cmd[0]
        if not messagebox.askyesno(
            "Missing dependency",
            f"{python}\ncannot run the server: it needs Python 3.9+ with the 'websockets' package.\n\n"
            "Install the requirements now with pip install -r requirements.txt?\n"
            "(Or choose No and pick a Python/venv that has it under Advanced.)",
        ):
            self._starting = False
            self.refresh_buttons()
            return
        self.append_log("[gui] installing requirements… (the window stays usable)\n", "gui")

        def _install() -> None:
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            try:
                res = subprocess.run([python, "-m", "pip", "install", "-r", str(HERE / "requirements.txt")],
                                     capture_output=True, text=True, creationflags=flags, timeout=600)
                output, code = res.stdout + res.stderr, res.returncode
            except (OSError, subprocess.TimeoutExpired) as exc:
                output, code = f"{exc}\n", 1
            self.log_queue.put(output)
            if code == 0:
                self.call_main(lambda: self._launch(cmd))
                return
            hint = ""
            if "externally-managed-environment" in output:
                hint = ("\n\nThis Python is managed by the operating system. Create a virtual environment:\n"
                        f"  python3 -m venv {HERE / '.venv'}\n"
                        f"  {HERE / '.venv' / 'bin' / 'pip'} install -r requirements.txt\n"
                        "then pick .venv/bin/python under Advanced (or start with ServerGUI.sh, which does this).")
            self.call_main(lambda: self._start_failed("pip failed", "See the Log tab for details." + hint))

        threading.Thread(target=_install, name="pip-install", daemon=True).start()

    def _launch(self, cmd: list[str]) -> None:
        self._starting = False
        if self.running():
            self.refresh_buttons()
            return
        shown = mask_command(cmd)
        self.append_log("[gui] " + subprocess.list2cmdline(shown) + "\n", "gui")
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
        # Let the packaged server reuse this window's onefile extraction (it lives
        # no longer than the window) instead of unpacking a new copy on every Start.
        env.pop("PYINSTALLER_RESET_ENVIRONMENT", None)
        env.pop("BZR_GUI_RELAUNCHED", None)
        try:
            self.proc = subprocess.Popen(
                cmd, cwd=str(HERE), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1, creationflags=flags, env=env,
            )
        except OSError as exc:
            self.proc = None
            self.refresh_buttons()
            messagebox.showerror("Could not start server", f"{exc}\n\nCheck the Python path under Advanced.")
            return
        self.started_at = time.time()
        self.stopping = False
        threading.Thread(target=self.read_output, args=(self.proc,), name="server-stdout", daemon=True).start()
        self.refresh_buttons()

    def read_output(self, proc: subprocess.Popen[str]) -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            self.log_queue.put(line)
        code = proc.wait()
        self.log_queue.put(f"[gui] server exited with code {code}\n")

    def stop(self, then=None) -> None:
        proc = self.proc
        if not self.running() or proc is None:
            if then:
                then()
            return
        if then:
            self._after_stop.append(then)
        if self.stopping:
            return  # a stop is already under way; it runs `then` when done
        self.stopping = True
        self.refresh_buttons()
        self.append_log("[gui] stopping (saving state)…\n", "gui")

        def _stop() -> None:
            try:
                assert proc.stdin is not None
                proc.stdin.write("stop\n")
                proc.stdin.flush()
                proc.stdin.close()
            except (OSError, ValueError):
                pass
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.log_queue.put("[gui] server did not stop in time; killing it\n")
                kill_tree(proc)
                proc.wait()
            self.call_main(self._run_after_stop)

        threading.Thread(target=_stop, name="stopper", daemon=True).start()

    def _run_after_stop(self) -> None:
        callbacks, self._after_stop = self._after_stop, []
        for callback in callbacks:
            callback()

    def restart(self) -> None:
        self.stop(then=self.start)

    def open_status(self) -> None:
        try:
            port = int(self.cfg.get("health_port") if self.running() else self.vars["health_port"].get())
        except (tk.TclError, TypeError, ValueError):
            return
        if port:
            webbrowser.open(f"http://127.0.0.1:{port}/status")

    def open_state_folder(self) -> None:
        folder = Path(self.vars["state_file"].get()).expanduser().resolve().parent
        try:
            folder.mkdir(parents=True, exist_ok=True)
            open_in_file_manager(folder)
        except OSError as exc:
            messagebox.showerror("State folder", f"Cannot open {folder}:\n{exc}")

    def update_lan(self) -> None:
        self.root.after(15000, self.update_lan)
        bind = BIND_CHOICES.get(self.vars["bind"].get(), self.vars["bind"].get())
        if bind.startswith("127."):
            self.lan_var.set("Addresses: 127.0.0.1 (this PC only)")
            self.update_switch()
            return
        if self._lan_busy:
            return
        self._lan_busy = True

        def _lookup() -> None:
            # Hostname lookups can stall for seconds (notably on macOS); never on the UI thread.
            try:
                addrs = lan_addresses()
            except Exception:
                addrs = []
            text = ("Addresses: " + ", ".join(map(describe_address, addrs))) if addrs else "no LAN address found"

            def _apply() -> None:
                self._lan_busy = False
                if not BIND_CHOICES.get(self.vars["bind"].get(), self.vars["bind"].get()).startswith("127."):
                    self.lan_var.set(text)
                self.update_switch()
            self.call_main(_apply)

        threading.Thread(target=_lookup, name="lan-addresses", daemon=True).start()

    def update_switch(self) -> None:
        # Stock clients take the full lobby URL via /bzrserver= and send the UDP
        # handshake to the same host on lobby port + 1, so those two must line up.
        ip = self.first_lan()
        try:
            ws, udp = int(self.vars["ws_port"].get()), int(self.vars["udp_port"].get())
        except (tk.TclError, ValueError):
            return
        self.switch_var.set(f"/bzrserver=ws://{ip or '<server-ip>'}:{ws}/")
        self.port_warn.configure(
            text="" if udp == ws + 1 else f"Handshake UDP must be lobby port + 1 ({ws + 1}) for game clients."
        )
        # OpenShim's multiplayer setup screen swaps only the host, so it reaches
        # this server only on the stock ports.
        self.openshim_note.configure(
            text="OpenShim players need no launch option: Multiplayer setup screen, Custom, then the LAN or "
                 "Internet address above (Local when the game runs on this PC)."
            if ws == OPENSHIM_WS_PORT and udp == ws + 1
            else f"OpenShim's setup screen only reaches lobby port {OPENSHIM_WS_PORT} with UDP {OPENSHIM_WS_PORT + 1}; "
                 "on these ports players must use the launch option."
        )
        host = clean_host(self.public_var.get())
        self.wan_switch_var.set(f"/bzrserver=ws://{host}:{ws}/" if host else "enter or detect the public address")
        try:
            relay = int(self.vars["relay_port"].get())
        except (tk.TclError, ValueError):
            relay = ws + 2
        try:
            pair_enabled = bool(self.vars["relay_pair_ports"].get())
            pair_start = int(self.vars["relay_pair_port_start"].get())
            pair_end = int(self.vars["relay_pair_port_end"].get())
        except (tk.TclError, ValueError):
            return
        self.wan_note.configure(
            text=f"Public IP or DDNS name. Forward TCP {ws} + UDP {udp}, {relay} to this PC. "
                 + pair_port_notice(pair_enabled, pair_start, pair_end)
        )

    def first_lan(self) -> str:
        text = self.lan_var.get().removeprefix("Addresses: ")
        first = text.split(",")[0].split()[0] if text.strip() else ""
        return first if first[:1].isdigit() else ""

    def copy_switch(self) -> None:
        self.update_switch()
        self.root.clipboard_clear()
        self.root.clipboard_append(self.switch_var.get())

    def copy_wan_switch(self) -> None:
        self.update_switch()
        if self.wan_switch_var.get().startswith("/bzrserver="):
            self.root.clipboard_clear()
            self.root.clipboard_append(self.wan_switch_var.get())

    def detect_public_ip(self) -> None:
        # Only on request: this asks an outside service which address we appear from.
        self.detect_btn.state(["disabled"])

        def _fetch() -> None:
            try:
                with urllib.request.urlopen(PUBLIC_IP_URL, timeout=5) as resp:
                    ip = resp.read().decode("ascii", "replace").strip()
                socket.inet_aton(ip)
                result = ("ok", ip)
            except Exception as exc:
                result = ("err", str(exc))
            self.call_main(lambda: self._detected(*result))

        threading.Thread(target=_fetch, name="public-ip", daemon=True).start()

    def _detected(self, status: str, value: str) -> None:
        self.detect_btn.state(["!disabled"])
        if status == "ok":
            self.public_var.set(value)
            self.append_log(f"[gui] public IP detected: {value}\n", "gui")
        else:
            messagebox.showerror("Detect public IP", f"Could not reach {PUBLIC_IP_URL}:\n{value}")

    def clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def append_log(self, line: str, tag: str | None = None) -> None:
        if tag is None:
            if " ERROR " in line or "Traceback" in line or "CRITICAL" in line:
                tag = "error"
            elif " WARNING " in line:
                tag = "warn"
            elif line.startswith("[gui]"):
                tag = "gui"
        self.log_text.configure(state="normal")
        self.log_text.insert("end", line, tag or ())
        over = int(self.log_text.index("end-1c").split(".")[0]) - MAX_LOG_LINES
        if over > 0:
            self.log_text.delete("1.0", f"{over + 1}.0")
        self.log_text.configure(state="disabled")
        if self.autoscroll.get():
            self.log_text.see("end")

    def refresh_buttons(self) -> None:
        running = self.running()
        self.start_btn.state(["disabled"] if running or self.stopping or self._starting else ["!disabled"])
        self.stop_btn.state(["!disabled"] if running and not self.stopping else ["disabled"])
        self.restart_btn.state(["!disabled"] if running and not self.stopping else ["disabled"])
        for child in self.settings_frame.winfo_children():
            self.set_enabled(child, not running)
        self.settings_hint.configure(text="Stop the server to change settings" if running else "")
        if self.stopping and running:
            self.state_label.configure(text="Stopping…", foreground=AMBER)
        elif running:
            self.state_label.configure(text=f"Running  (PID {self.proc.pid})", foreground=GREEN)
        else:
            self.state_label.configure(text="Stopped", foreground=RED)

    def set_enabled(self, widget: tk.Widget, enabled: bool) -> None:
        """Lock/unlock a settings widget. Locked inputs are read-only (not greyed out) so the
        values stay legible; checkbuttons use the themed disabled+selected look."""
        if widget is self.adv_toggle:
            return
        try:
            if isinstance(widget, ttk.Entry):  # also Combobox and Spinbox
                if enabled:
                    free = getattr(widget, "_free_state", None)
                    if free:
                        widget.configure(state=free)
                    rng = getattr(widget, "_free_range", None)
                    if rng:
                        widget.configure(from_=rng[0], to=rng[1])
                        del widget._free_range  # type: ignore[attr-defined]
                else:
                    if not hasattr(widget, "_free_state") or str(widget.cget("state")) != "readonly":
                        widget._free_state = str(widget.cget("state"))  # type: ignore[attr-defined]
                    widget.configure(state="readonly")
                    if isinstance(widget, ttk.Spinbox) and not hasattr(widget, "_free_range"):
                        # readonly Spinbox arrows still step the value; pin the range to the current value.
                        widget._free_range = (widget.cget("from"), widget.cget("to"))  # type: ignore[attr-defined]
                        try:
                            cur = float(widget.get() or widget._free_range[0])  # type: ignore[attr-defined]
                            widget.configure(from_=cur, to=cur)
                        except ValueError:
                            pass
            elif isinstance(widget, (ttk.Checkbutton, ttk.Button)):
                widget.state(["!disabled"] if enabled else ["disabled"])
        except (AttributeError, tk.TclError):
            pass
        for child in widget.winfo_children():
            self.set_enabled(child, enabled)

    # ---------- reachability check ----------
    def check_reachability(self) -> None:
        def num(key: str) -> int:
            try:
                return int(self.vars[key].get())
            except (tk.TclError, ValueError):
                return 0

        facts = {
            "running": self.running(),
            "bind": BIND_CHOICES.get(self.vars["bind"].get(), self.vars["bind"].get()).strip(),
            "pair_enabled": bool(self.vars["relay_pair_ports"].get()),
            "pair_start": num("relay_pair_port_start"),
            "pair_end": num("relay_pair_port_end"),
            "public_ip": clean_host(self.public_var.get()),
            "ws_port": num("ws_port"),
        }
        health = num("health_port")
        self.diag_btn.state(["disabled"])
        self.show_diag([("info", "Checking…")])

        def _work() -> None:
            try:
                if facts["running"] and facts["public_ip"]:
                    facts["tcp"] = tcp_probe(facts["public_ip"], facts["ws_port"], 3.0)
                if facts["running"] and health:
                    try:
                        facts["status"] = self.fetch(health, "/status")
                    except Exception:
                        facts["status"] = None
                findings = reachability_report(facts)
            except Exception as exc:  # never leave the button disabled
                findings = [("error", f"Check failed: {exc}")]
            for level, msg in findings:
                self.log_queue.put(f"[gui] reachability [{level}] {msg}\n")
            self.call_main(lambda: self._diag_done(findings))

        threading.Thread(target=_work, name="reachability", daemon=True).start()

    def _diag_done(self, findings: list[tuple[str, str]]) -> None:
        self.diag_btn.state(["!disabled"])
        self.show_diag(findings)

    def show_diag(self, findings: list[tuple[str, str]]) -> None:
        marks = {"ok": "[ OK ]", "warn": "[WARN]", "error": "[FAIL]", "info": "[ .. ]"}
        self.diag_text.configure(state="normal")
        self.diag_text.delete("1.0", "end")
        for level, msg in findings:
            self.diag_text.insert("end", f"{marks.get(level, '')} {msg}\n", level)
        self.diag_text.configure(state="disabled")

    # ---------- polling ----------
    def poll_loop(self) -> None:
        while True:
            time.sleep(POLL_SECONDS)
            result = None
            port = self.cfg.get("health_port", 8080)
            if self.running() and port:
                try:
                    result = {
                        "status": self.fetch(port, "/status"),
                        "lobbies": self.fetch(port, "/lobbies"),
                        "access": self.fetch(port, "/access"),
                    }
                except Exception:
                    result = None
            with self.poll_lock:
                self.poll_result = result

    @staticmethod
    def fetch(port: int, path: str) -> dict:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=1.5) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def call_main(self, fn) -> None:
        """Run fn on the Tk thread (safe to call from any thread)."""
        self.ui_queue.put(fn)

    def pump(self) -> None:
        while True:
            try:
                fn = self.ui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                fn()
            except Exception as exc:  # keep the UI loop alive
                if self.closed:
                    return
                self.append_log(f"[gui] internal error: {exc}\n", "error")
            if self.closed:
                return  # a callback closed the window
        lines = 0
        while lines < 500:
            try:
                line = self.log_queue.get_nowait()
            except queue.Empty:
                break
            self.append_log(line)
            lines += 1

        if self.proc is not None and not self.running():
            code = self.proc.returncode
            unexpected = not self.stopping
            self.proc = None
            self.stopping = False
            self.clear_live()
            self.refresh_buttons()
            if unexpected:
                # Crashed or refused to start (e.g. a port already in use): show why.
                self.notebook.select(self.log_frame)
                messagebox.showerror(
                    "Server stopped",
                    f"The server exited unexpectedly (code {code}).\n\n"
                    "The Log tab shows the reason, for example a port that another program already uses.")

        with self.poll_lock:
            result, self.poll_result = self.poll_result, None
        if result and self.running():
            self.show_live(result)
        self.root.after(200, self.pump)

    def clear_live(self) -> None:
        for v in self.stat_vars.values():
            v.set("–")
        self.lobby_tree.delete(*self.lobby_tree.get_children())
        self.user_tree.delete(*self.user_tree.get_children())
        for tree in (self.pending_tree, self.approved_tree, self.bans_tree, self.mutes_tree, self.identities_tree):
            tree.delete(*tree.get_children())
        self.show_pending(0)
        self.last_pending_count = 0

    def show_live(self, result: dict) -> None:
        status = result.get("status") or {}
        st = status.get("state", {})
        relay = status.get("relay", {})
        self.stat_vars["uptime"].set(fmt_duration(status.get("uptimeSeconds", 0)))
        self.stat_vars["users"].set(str(st.get("activeUsers", "–")))
        self.stat_vars["lobbies"].set(str(st.get("lobbyCount", "–")))
        self.stat_vars["routes"].set(str(relay.get("activeRoutes", "–")) if relay.get("enabled") else "relay off")
        self.stat_vars["messages"].set(str(status.get("websocket", {}).get("messagesTotal", "–")))
        if relay.get("enabled"):
            self.stat_vars["relaypk"].set(f"{relay.get('packetsRxTotal', 0)} in / {relay.get('packetsTxTotal', 0)} out")
        else:
            self.stat_vars["relaypk"].set("–")

        data = result.get("lobbies") or {}
        lobby_rows = []
        for lob in data.get("lobbies", []):
            flags = [f for f, on in (("private", lob.get("isPrivate")), ("locked", lob.get("isLocked")), ("persistent", lob.get("persistent"))) if on]
            members = ", ".join(u.get("name") or u.get("id", "") for u in lob.get("users", []))
            limit = lob.get("memberLimit")
            count = f"{len(lob.get('users', []))}/{limit}" if limit and limit < 1000 else str(len(lob.get("users", [])))
            lobby_rows.append((str(lob.get("id")), (
                lob.get("id"), lob.get("name") or "(unnamed)", lob.get("ownerName") or lob.get("owner", ""),
                count, " ".join(flags), members,
            )))
        self.fill_tree(self.lobby_tree, lobby_rows)
        rows = []
        for u in data.get("users", []):
            name = u.get("playerName") if u.get("playerName") not in (None, "", "Unknown") else u.get("name", "")
            rows.append((str(u.get("id")), (
                u.get("id"), name, u.get("authtype", ""), u.get("lobbyId") or "",
                "lounge" if u.get("inLounge") else ("in lobby" if u.get("lobbyId") else ""),
                u.get("wanAddress", ""), "yes" if u.get("isAdmin") else "", "yes" if u.get("muted") else "",
            )))
        self.fill_tree(self.user_tree, rows)
        if result.get("access"):
            self.show_access(result["access"].get("access") or {})

    def on_close(self) -> None:
        try:
            self.save_config()
        except tk.TclError:
            pass
        if self.running():
            if not messagebox.askyesno("Stop server?", "The server is running. Stop it and exit?"):
                return
            self.stop(then=self._destroy)
        else:
            self._destroy()

    def _destroy(self) -> None:
        self.closed = True
        self.root.destroy()


def open_in_file_manager(folder: Path) -> None:
    """Show a folder in Explorer / Finder / the desktop's file manager."""
    if os.name == "nt":
        os.startfile(folder)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(folder)])
    else:
        subprocess.Popen(["xdg-open", str(folder)])


def kill_tree(proc: subprocess.Popen) -> None:
    """Kill the server and its children: the packaged exe is a onefile
    bootloader whose child process is the actual server."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True,
                       creationflags=subprocess.CREATE_NO_WINDOW)
    proc.kill()


def relaunch_without_console() -> bool:
    """Reopen the window detached from a console it got by a double-click.

    The packaged exe is a console program so it can also run headless in a
    terminal. Started from Explorer it gets a console of its own, shared only
    by the onefile bootloader and this process (Windows Terminal may host it,
    so it cannot simply be hidden). Start a detached copy and let this one
    exit, which closes that console. Started from a terminal, keep it.
    """
    # The relaunched copy has a console of its own too (hidden), so it must not
    # relaunch again.
    if os.name != "nt" or os.environ.get("BZR_GUI_RELAUNCHED"):
        return False
    try:
        import ctypes
        procs = (ctypes.c_uint32 * 4)()
        if not 0 < ctypes.windll.kernel32.GetConsoleProcessList(procs, 4) <= 2:
            return False
        # CREATE_NO_WINDOW: the copy and its onefile child share a hidden console.
        # DETACHED_PROCESS would make the child open a new visible one.
        flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        # Fresh onefile extraction: this process's temp dir goes when it exits.
        env = dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT="1", BZR_GUI_RELAUNCHED="1")
        subprocess.Popen([sys.executable, "--gui"], cwd=str(HERE), env=env, creationflags=flags,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False


def main() -> None:
    if not FROZEN and not SERVER_SCRIPT.exists():
        raise SystemExit(f"{SERVER_SCRIPT.name} not found in {HERE}")
    if FROZEN and relaunch_without_console():
        return
    try:
        if os.name == "nt":
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        raise SystemExit(f"Cannot open the launcher window ({exc}). On a machine without a display, "
                         "run the server headless instead: python server.py --help")
    try:
        apply_theme(root)
        ServerGUI(root)
    except Exception:
        # Under pythonw / the packaged exe there is no console, so a startup
        # failure would otherwise vanish without a trace.
        import traceback
        details = traceback.format_exc()
        log = HERE / "server_gui_error.log"
        try:
            log.write_text(details, encoding="utf-8")
        except OSError:
            log = None
        try:
            messagebox.showerror("BZR Dedicated Server", "The launcher could not start.\n\n"
                                 + details.strip().splitlines()[-1]
                                 + (f"\n\nDetails were saved to {log}" if log else ""))
        finally:
            root.destroy()
        raise SystemExit(1)
    root.mainloop()


if __name__ == "__main__":
    main()
