#!/usr/bin/env python3
"""Desktop launcher for the Battlezone 98 Redux dedicated server.

Starts/stops server.py as a child process, shows its log, and polls the local
health port (/status, /lobbies) for players and lobbies. Uses only the standard
library (tkinter), so it runs with any Python that can run server.py.

    python server_gui.py        (or pythonw server_gui.py for no console)

Settings are saved to server_gui.json next to this file.
"""

from __future__ import annotations

import json
import os
import queue
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.request
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

FROZEN = getattr(sys, "frozen", False)
# Packaged build (build_package.py): BZRServerGUI.exe runs BZRServer.exe beside it.
HERE = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent
SERVER_SCRIPT = HERE / ("BZRServer.exe" if FROZEN else "server.py")
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
    "health_port": 8080,
    "state_file": str(HERE / "data" / "server_state.json"),
    "motd": "",
    "admin_key": "",
    "admin_ids": "",
    "max_lobbies": 500,
    "retain_empty_lobbies": False,
    "log_level": "INFO",
    "access_mode": "open",
    "pin_ips": False,
    "extra_args": "",
    "public_host": "",
}
PUBLIC_IP_URL = "https://api.ipify.org"
ACCESS_CHOICES = {
    "Open (anyone)": "open",
    "Approve new players": "approve",
    "Allowlist only": "allowlist",
}


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


class ServerGUI:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.proc: subprocess.Popen[str] | None = None
        self.log_queue: queue.Queue[str] = queue.Queue()
        self.poll_result: dict | None = None
        self.poll_lock = threading.Lock()
        self.stopping = False
        self.started_at = 0.0

        root.title("BZR Dedicated Server")
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.cfg = dict(DEFAULTS)
        self.cfg.update(self.load_config())
        self.vars: dict[str, tk.Variable] = {}
        self.build_ui()
        # Size from content so Windows display scaling never pushes the tabs off-screen.
        root.update_idletasks()
        w = min(root.winfo_reqwidth() + 16, root.winfo_screenwidth() - 40)
        h = min(root.winfo_reqheight() + 16, root.winfo_screenheight() - 80)
        root.geometry(f"{w}x{h}")
        root.minsize(min(w, root.winfo_reqwidth()), min(h, root.winfo_reqheight()))
        for key in ("ws_port", "udp_port", "relay_port", "bind", "public_host"):
            self.vars[key].trace_add("write", lambda *_: self.update_switch())
        self.refresh_buttons()

        threading.Thread(target=self.poll_loop, name="poll", daemon=True).start()
        self.root.after(100, self.pump)

    # ---------- config ----------
    def load_config(self) -> dict:
        try:
            return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def save_config(self) -> None:
        data = {}
        for key, var in self.vars.items():
            value = var.get()
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
        outer = ttk.Frame(self.root, padding=8)
        outer.pack(fill="both", expand=True)

        top = ttk.Frame(outer)
        top.pack(fill="x")

        left = ttk.Frame(top)
        left.pack(side="left", fill="both", expand=True)
        settings = ttk.LabelFrame(left, text="Settings", padding=8)
        settings.pack(fill="x")
        self.settings_frame = settings

        def var(key: str, kind=tk.StringVar):
            v = kind(value=self.cfg.get(key, DEFAULTS[key]))
            self.vars[key] = v
            return v

        row = 0

        def label(text: str, r: int, c: int = 0) -> None:
            ttk.Label(settings, text=text).grid(row=r, column=c, sticky="w", padx=(0, 6), pady=2)

        label("Listen on", row)
        bind_label = next((k for k, v in BIND_CHOICES.items() if v == self.cfg.get("bind")), self.cfg.get("bind"))
        bind_var = tk.StringVar(value=bind_label)
        self.vars["bind"] = bind_var
        ttk.Combobox(settings, textvariable=bind_var, values=list(BIND_CHOICES), width=30).grid(
            row=row, column=1, columnspan=3, sticky="we", pady=2
        )
        row += 1

        label("Lobby TCP", row)
        ttk.Spinbox(settings, from_=1, to=65535, textvariable=var("ws_port", tk.IntVar), width=7).grid(row=row, column=1, sticky="w")
        label("Handshake UDP", row, 2)
        ttk.Spinbox(settings, from_=1, to=65535, textvariable=var("udp_port", tk.IntVar), width=7).grid(row=row, column=3, sticky="w")
        row += 1

        label("Relay UDP", row)
        ttk.Spinbox(settings, from_=1, to=65535, textvariable=var("relay_port", tk.IntVar), width=7).grid(row=row, column=1, sticky="w")
        ttk.Checkbutton(settings, text="Relay enabled", variable=var("relay_enabled", tk.BooleanVar)).grid(row=row, column=2, columnspan=2, sticky="w")
        row += 1

        label("Status port", row)
        ttk.Spinbox(settings, from_=0, to=65535, textvariable=var("health_port", tk.IntVar), width=7).grid(row=row, column=1, sticky="w")
        label("Max lobbies", row, 2)
        ttk.Spinbox(settings, from_=1, to=100000, textvariable=var("max_lobbies", tk.IntVar), width=7).grid(row=row, column=3, sticky="w")
        row += 1

        label("State file", row)
        sf = ttk.Frame(settings)
        sf.grid(row=row, column=1, columnspan=3, sticky="we")
        ttk.Entry(sf, textvariable=var("state_file")).pack(side="left", fill="x", expand=True)
        ttk.Button(sf, text="…", width=3, command=self.browse_state).pack(side="left", padx=(4, 0))
        row += 1

        label("MOTD", row)
        ttk.Entry(settings, textvariable=var("motd")).grid(row=row, column=1, columnspan=3, sticky="we", pady=2)
        row += 1

        label("Admin key", row)
        ttk.Entry(settings, textvariable=var("admin_key"), show="•").grid(row=row, column=1, sticky="we", pady=2)
        label("Admin IDs", row, 2)
        ttk.Entry(settings, textvariable=var("admin_ids"), width=16).grid(row=row, column=3, sticky="we", pady=2)
        row += 1

        label("Log level", row)
        ttk.Combobox(settings, textvariable=var("log_level"), values=["DEBUG", "INFO", "WARNING", "ERROR"], width=9, state="readonly").grid(row=row, column=1, sticky="w")
        ttk.Checkbutton(settings, text="Keep empty lobbies", variable=var("retain_empty_lobbies", tk.BooleanVar)).grid(row=row, column=2, columnspan=2, sticky="w")
        row += 1

        python_var = var("python")
        if not FROZEN:
            label("Python", row)
            pf = ttk.Frame(settings)
            pf.grid(row=row, column=1, columnspan=3, sticky="we")
            ttk.Entry(pf, textvariable=python_var).pack(side="left", fill="x", expand=True)
            ttk.Button(pf, text="…", width=3, command=self.browse_python).pack(side="left", padx=(4, 0))
            row += 1

        label("Extra args", row)
        ttk.Entry(settings, textvariable=var("extra_args")).grid(row=row, column=1, columnspan=3, sticky="we", pady=2)
        settings.columnconfigure(1, weight=1)
        settings.columnconfigure(3, weight=1)

        # Right column: controls + live status.
        right = ttk.Frame(top, padding=(8, 0, 0, 0))
        right.pack(side="left", fill="y")

        controls = ttk.LabelFrame(right, text="Server", padding=8)
        controls.pack(fill="x")
        self.state_label = ttk.Label(controls, text="Stopped", font=("Segoe UI", 12, "bold"), foreground="#a33")
        self.state_label.pack(anchor="w")
        btns = ttk.Frame(controls)
        btns.pack(fill="x", pady=(6, 0))
        self.start_btn = ttk.Button(btns, text="Start", command=self.start)
        self.stop_btn = ttk.Button(btns, text="Stop", command=self.stop)
        self.restart_btn = ttk.Button(btns, text="Restart", command=self.restart)
        for b in (self.start_btn, self.stop_btn, self.restart_btn):
            b.pack(side="left", padx=(0, 4))
        btns2 = ttk.Frame(controls)
        btns2.pack(fill="x", pady=(4, 0))
        ttk.Button(btns2, text="Status page", command=self.open_status).pack(side="left", padx=(0, 4))
        ttk.Button(btns2, text="State folder", command=self.open_state_folder).pack(side="left")

        stats = ttk.LabelFrame(right, text="Live", padding=8)
        stats.pack(fill="x", pady=(8, 0))
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
            ttk.Label(stats, text=text).grid(row=i, column=0, sticky="w", padx=(0, 10))
            v = tk.StringVar(value="–")
            self.stat_vars[key] = v
            ttk.Label(stats, textvariable=v).grid(row=i, column=1, sticky="w")

        # Under the settings: what players put in their launch options.
        conn = ttk.LabelFrame(left, text="Players connect with", padding=8)
        conn.pack(fill="x", pady=(8, 0))
        conn.columnconfigure(1, weight=1)
        self.lan_var = tk.StringVar()
        self.switch_var = tk.StringVar()
        self.public_var = tk.StringVar(value=self.cfg.get("public_host", ""))
        self.vars["public_host"] = self.public_var
        self.wan_switch_var = tk.StringVar()

        ttk.Label(conn, text="LAN").grid(row=0, column=0, sticky="w", padx=(0, 6))
        ttk.Entry(conn, textvariable=self.switch_var, state="readonly").grid(row=0, column=1, columnspan=2, sticky="we")
        ttk.Button(conn, text="Copy", width=6, command=self.copy_switch).grid(row=0, column=3, padx=(4, 0))
        ttk.Label(conn, textvariable=self.lan_var, foreground="#666").grid(row=1, column=1, columnspan=3, sticky="w")
        self.port_warn = ttk.Label(conn, foreground="#b80")
        self.port_warn.grid(row=2, column=1, columnspan=3, sticky="w")

        ttk.Label(conn, text="Internet").grid(row=3, column=0, sticky="w", padx=(0, 6), pady=(6, 0))
        ttk.Entry(conn, textvariable=self.public_var, width=18).grid(row=3, column=1, sticky="we", pady=(6, 0))
        self.detect_btn = ttk.Button(conn, text="Detect", width=7, command=self.detect_public_ip)
        self.detect_btn.grid(row=3, column=2, padx=(4, 0), pady=(6, 0))
        ttk.Entry(conn, textvariable=self.wan_switch_var, state="readonly").grid(row=4, column=1, columnspan=2, sticky="we", pady=(2, 0))
        ttk.Button(conn, text="Copy", width=6, command=self.copy_wan_switch).grid(row=4, column=3, padx=(4, 0), pady=(2, 0))
        self.wan_note = ttk.Label(conn, foreground="#666")
        self.wan_note.grid(row=5, column=1, columnspan=3, sticky="w")
        self.update_lan()

        # Bottom: tabs.
        nb = ttk.Notebook(outer)
        nb.pack(fill="both", expand=True, pady=(8, 0))

        log_frame = ttk.Frame(nb)
        self.log_text = tk.Text(log_frame, wrap="none", height=12, font=("Consolas", 9), state="disabled", background="#111", foreground="#ddd", insertbackground="#ddd")
        ys = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        xs = ttk.Scrollbar(log_frame, orient="horizontal", command=self.log_text.xview)
        self.log_text.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        ys.grid(row=0, column=1, sticky="ns")
        xs.grid(row=1, column=0, sticky="ew")
        log_frame.rowconfigure(0, weight=1)
        log_frame.columnconfigure(0, weight=1)
        self.log_text.tag_configure("warn", foreground="#e6c35c")
        self.log_text.tag_configure("error", foreground="#ff6b6b")
        self.log_text.tag_configure("gui", foreground="#7fb2ff")
        lb = ttk.Frame(log_frame)
        lb.grid(row=2, column=0, columnspan=2, sticky="ew", pady=2)
        self.autoscroll = tk.BooleanVar(value=True)
        ttk.Checkbutton(lb, text="Auto-scroll", variable=self.autoscroll).pack(side="left")
        ttk.Button(lb, text="Clear", command=self.clear_log).pack(side="left", padx=4)
        nb.add(log_frame, text="Log")

        self.lobby_tree = self.make_tree(
            nb, "Lobbies",
            [("id", "ID", 60), ("name", "Name", 260), ("owner", "Host", 160), ("players", "Players", 70), ("flags", "Flags", 140), ("members", "Members", 300)],
        )
        self.user_tree = self.make_tree(
            nb, "Players",
            [("id", "User ID", 160), ("name", "Name", 180), ("auth", "Auth", 70), ("lobby", "Lobby", 70), ("where", "Where", 90), ("wan", "Address", 180), ("admin", "Admin", 60)],
            buttons=[
                ("Approve", lambda: self.act_selected(self.user_tree, "approve")),
                ("Kick", lambda: self.act_selected(self.user_tree, "kick")),
                ("Ban", lambda: self.act_selected(self.user_tree, "ban", confirm="Ban {n}?")),
                ("Ban + IP", lambda: self.act_selected(self.user_tree, "ban", withIp=True, confirm="Ban {n} and their IP?")),
            ],
        )
        self.build_access_tab(nb)

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
        self.notebook = nb
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
        ttk.Label(frame, foreground="#666", wraplength=900, justify="left", text=(
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
            [("what", "Banned", 200), ("kind", "Type", 60), ("name", "Name", 170), ("reason", "Reason", 200), ("at", "When", 150)],
            height=3,
            buttons=[("Unban", self.unban_selected)],
        )
        self.last_pending_count = 0

    # ---------- access actions ----------
    def apply_access_settings(self) -> None:
        mode = ACCESS_CHOICES.get(self.access_mode_var.get(), "open")
        self.cfg["access_mode"], self.cfg["pin_ips"] = mode, bool(self.pin_var.get())
        self.save_config()
        if self.running():
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

    def unban_selected(self) -> None:
        if not self.running():
            messagebox.showinfo("Access", "Start the server to manage bans.")
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
                self.root.after(0, lambda: self.show_access(data.get("access") or {}))
            except Exception as exc:
                self.log_queue.put(f"[gui] access action {body.get('action')} failed: {exc}\n")

        threading.Thread(target=_send, name="access-post", daemon=True).start()

    @staticmethod
    def fill_tree(tree: ttk.Treeview, rows: list[tuple[str, tuple]]) -> None:
        """Replace rows keyed by iid, keeping the operator's selection."""
        selected = set(tree.selection())
        tree.delete(*tree.get_children())
        for iid, values in rows:
            if not tree.exists(iid):
                tree.insert("", "end", iid=iid, values=values)
        keep = [i for i in selected if tree.exists(i)]
        if keep:
            tree.selection_set(keep)

    def show_access(self, access: dict) -> None:
        self.fill_tree(self.pending_tree, [
            (p["id"], (p["id"], p.get("name", ""), p.get("ip", ""), p.get("reason", ""),
                       p.get("since", "") + ("" if p.get("waiting") else "  (refused, will retry)")))
            for p in access.get("pending", [])
        ])
        self.fill_tree(self.approved_tree, [
            (a["id"], (a["id"], a.get("name", ""), ", ".join(a.get("ips", [])), a.get("lastSeen", ""), a.get("approvedAt", "")))
            for a in access.get("approved", [])
        ])
        self.fill_tree(self.bans_tree, [
            (f"id:{b['id']}", (b["id"], "ID", b.get("name", ""), b.get("reason", ""), b.get("at", "")))
            for b in access.get("bannedIds", [])
        ] + [
            (f"ip:{b['ip']}", (b["ip"], "IP", b.get("name", ""), b.get("reason", ""), b.get("at", "")))
            for b in access.get("bannedIps", [])
        ])
        waiting = sum(1 for p in access.get("pending", []) if p.get("waiting"))
        self.notebook.tab(self.access_frame, text=f"Access ({waiting} waiting)" if waiting else "Access")
        if waiting > self.last_pending_count:
            self.root.bell()
            self.append_log(f"[gui] {waiting} player(s) waiting for approval (Access tab)\n", "gui")
        self.last_pending_count = waiting

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
        path = filedialog.askopenfilename(title="Python interpreter", filetypes=[("Python", "python*.exe"), ("All", "*.*")])
        if path:
            self.vars["python"].set(path)

    def build_command(self) -> list[str]:
        v = {k: var.get() for k, var in self.vars.items()}
        bind = BIND_CHOICES.get(v["bind"], v["bind"]).strip() or "0.0.0.0"
        launcher = [str(SERVER_SCRIPT)] if FROZEN else [v["python"], "-u", str(SERVER_SCRIPT)]
        cmd = launcher + [
            "--ws-host", bind, "--ws-port", str(v["ws_port"]),
            "--udp-host", bind, "--udp-port", str(v["udp_port"]),
            "--relay-host", bind, "--relay-port", str(v["relay_port"]),
            "--health-port", str(v["health_port"]),
            "--state-file", v["state_file"],
            "--max-lobbies", str(v["max_lobbies"]),
            "--log-level", v["log_level"],
            "--stdin-control",
        ]
        if not v["relay_enabled"]:
            cmd.append("--disable-relay")
        cmd += ["--access-mode", ACCESS_CHOICES.get(v["access_mode"], v["access_mode"]),
                "--pin-ips" if v["pin_ips"] else "--no-pin-ips"]
        if v["retain_empty_lobbies"]:
            cmd.append("--retain-empty-lobbies")
        if v["motd"].strip():
            cmd += ["--motd", v["motd"]]
        if v["admin_key"].strip():
            cmd += ["--admin-key", v["admin_key"]]
        if v["admin_ids"].strip():
            cmd += ["--admin-ids", v["admin_ids"]]
        if v["extra_args"].strip():
            import shlex
            cmd += shlex.split(v["extra_args"], posix=os.name != "nt")
        return cmd

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self) -> None:
        if self.running():
            return
        try:
            cmd = self.build_command()
        except (tk.TclError, ValueError) as exc:
            messagebox.showerror("Invalid setting", str(exc))
            return
        self.save_config()
        if not FROZEN and not self.check_python(cmd[0]):
            return
        state_dir =Path(self.vars["state_file"].get()).expanduser().parent
        state_dir.mkdir(parents=True, exist_ok=True)

        shown = [("***" if i > 0 and cmd[i - 1] == "--admin-key" else a) for i, a in enumerate(cmd)]
        self.append_log("[gui] " + subprocess.list2cmdline(shown) + "\n", "gui")
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
        try:
            self.proc = subprocess.Popen(
                cmd, cwd=str(HERE), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1, creationflags=flags, env=env,
            )
        except OSError as exc:
            self.proc = None
            messagebox.showerror("Could not start server", f"{exc}\n\nCheck the Python path in Settings.")
            return
        self.started_at = time.time()
        self.stopping = False
        threading.Thread(target=self.read_output, args=(self.proc,), name="server-stdout", daemon=True).start()
        self.refresh_buttons()

    def check_python(self, python: str) -> bool:
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            ok = subprocess.run([python, "-c", "import websockets"], capture_output=True, timeout=20, creationflags=flags).returncode == 0
        except (OSError, subprocess.TimeoutExpired) as exc:
            messagebox.showerror("Python not usable", f"{python}\n\n{exc}\n\nPick a Python interpreter in Settings.")
            return False
        if ok:
            return True
        if messagebox.askyesno(
            "Missing dependency",
            f"{python}\ncannot import 'websockets', which the server needs.\n\n"
            "Install it now with pip install -r requirements.txt?\n"
            "(Or choose No and pick a Python/venv that has it.)",
        ):
            self.append_log("[gui] installing requirements…\n", "gui")
            self.root.update_idletasks()
            res = subprocess.run(
                [python, "-m", "pip", "install", "-r", str(HERE / "requirements.txt")],
                capture_output=True, text=True, creationflags=flags,
            )
            self.append_log(res.stdout + res.stderr)
            if res.returncode == 0:
                return True
            messagebox.showerror("pip failed", "See the log tab for details.")
        return False

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
        self.stopping = True
        self.refresh_buttons()
        self.append_log("[gui] stopping (saving state)…\n", "gui")

        def _stop() -> None:
            try:
                assert proc.stdin is not None
                proc.stdin.write("stop\n")
                proc.stdin.flush()
                proc.stdin.close()
            except OSError:
                pass
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.log_queue.put("[gui] server did not stop in time; killing it\n")
                proc.kill()
                proc.wait()
            if then:
                self.root.after(0, then)

        threading.Thread(target=_stop, name="stopper", daemon=True).start()

    def restart(self) -> None:
        self.stop(then=self.start)

    def open_status(self) -> None:
        port = self.vars["health_port"].get()
        if port:
            webbrowser.open(f"http://127.0.0.1:{port}/status")

    def open_state_folder(self) -> None:
        folder = Path(self.vars["state_file"].get()).expanduser().parent
        folder.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            os.startfile(folder)  # type: ignore[attr-defined]
        else:
            webbrowser.open(folder.as_uri())

    def update_lan(self) -> None:
        bind = BIND_CHOICES.get(self.vars["bind"].get(), self.vars["bind"].get())
        if bind.startswith("127."):
            self.lan_var.set("Addresses: 127.0.0.1 (this PC only)")
        else:
            addrs = lan_addresses()
            self.lan_var.set(("Addresses: " + ", ".join(map(describe_address, addrs))) if addrs else "no LAN address found")
        self.update_switch()
        self.root.after(15000, self.update_lan)

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
        host = self.public_var.get().strip()
        for prefix in ("ws://", "http://", "https://"):
            if host.lower().startswith(prefix):
                host = host[len(prefix):]
        host = host.split("/")[0].split(":")[0]
        self.wan_switch_var.set(f"/bzrserver=ws://{host}:{ws}/" if host else "enter or detect the public address")
        try:
            relay = int(self.vars["relay_port"].get())
        except (tk.TclError, ValueError):
            relay = ws + 2
        self.wan_note.configure(
            text=f"Public IP or DDNS name. Forward TCP {ws} + UDP {udp}, {relay} to this PC."
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
            self.root.after(0, lambda: self._detected(*result))

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
        self.start_btn.state(["disabled"] if running or self.stopping else ["!disabled"])
        self.stop_btn.state(["!disabled"] if running and not self.stopping else ["disabled"])
        self.restart_btn.state(["!disabled"] if running and not self.stopping else ["disabled"])
        for child in self.settings_frame.winfo_children():
            self.set_enabled(child, not running)
        if self.stopping and running:
            self.state_label.configure(text="Stopping…", foreground="#b80")
        elif running:
            self.state_label.configure(text=f"Running  (PID {self.proc.pid})", foreground="#2a7")
        else:
            self.state_label.configure(text="Stopped", foreground="#a33")

    def set_enabled(self, widget: tk.Widget, enabled: bool) -> None:
        try:
            widget.state(["!disabled"] if enabled else ["disabled"])  # type: ignore[attr-defined]
        except (AttributeError, tk.TclError):
            pass
        for child in widget.winfo_children():
            self.set_enabled(child, enabled)

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

    def pump(self) -> None:
        lines = 0
        while lines < 500:
            try:
                line = self.log_queue.get_nowait()
            except queue.Empty:
                break
            self.append_log(line)
            lines += 1

        if self.proc is not None and not self.running():
            self.proc = None
            self.stopping = False
            self.clear_live()
            self.refresh_buttons()

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
        for tree in (self.pending_tree, self.approved_tree, self.bans_tree):
            tree.delete(*tree.get_children())
        self.notebook.tab(self.access_frame, text="Access")
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
        self.lobby_tree.delete(*self.lobby_tree.get_children())
        for lob in data.get("lobbies", []):
            flags = [f for f, on in (("private", lob.get("isPrivate")), ("locked", lob.get("isLocked")), ("persistent", lob.get("persistent"))) if on]
            members = ", ".join(u.get("name") or u.get("id", "") for u in lob.get("users", []))
            limit = lob.get("memberLimit")
            count = f"{len(lob.get('users', []))}/{limit}" if limit and limit < 1000 else str(len(lob.get("users", [])))
            self.lobby_tree.insert("", "end", values=(
                lob.get("id"), lob.get("name") or "(unnamed)", lob.get("ownerName") or lob.get("owner", ""),
                count, " ".join(flags), members,
            ))
        rows = []
        for u in data.get("users", []):
            name = u.get("playerName") if u.get("playerName") not in (None, "", "Unknown") else u.get("name", "")
            rows.append((str(u.get("id")), (
                u.get("id"), name, u.get("authtype", ""), u.get("lobbyId") or "",
                "lounge" if u.get("inLounge") else ("in lobby" if u.get("lobbyId") else ""),
                u.get("wanAddress", ""), "yes" if u.get("isAdmin") else "",
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
            self.stop(then=self.root.destroy)
        else:
            self.root.destroy()


def main() -> None:
    if not SERVER_SCRIPT.exists():
        msg = f"{SERVER_SCRIPT.name} not found in {HERE}"
        if FROZEN:
            tk.Tk().withdraw()
            messagebox.showerror("BZR Dedicated Server", msg + "\n\nKeep both exe files in the same folder.")
        raise SystemExit(msg)
    try:
        if os.name == "nt":
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    root = tk.Tk()
    style = ttk.Style(root)
    if "vista" in style.theme_names():
        style.theme_use("vista")
    ServerGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
