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

HERE = Path(__file__).resolve().parent
SERVER_SCRIPT = HERE / "server.py"
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
    "extra_args": "",
}


def lan_addresses() -> list[str]:
    addrs: set[str] = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addrs.add(info[4][0])
    except OSError:
        pass
    try:
        # No packets are sent; this just asks the OS which interface routes outward.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))
            addrs.add(s.getsockname()[0])
    except OSError:
        pass
    return sorted(a for a in addrs if not a.startswith("127.") and not a.startswith("169.254."))


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
        root.geometry("980x680")
        root.minsize(820, 560)
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.cfg = dict(DEFAULTS)
        self.cfg.update(self.load_config())
        self.vars: dict[str, tk.Variable] = {}
        self.build_ui()
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

        settings = ttk.LabelFrame(top, text="Settings", padding=8)
        settings.pack(side="left", fill="both", expand=True)
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

        label("Python", row)
        pf = ttk.Frame(settings)
        pf.grid(row=row, column=1, columnspan=3, sticky="we")
        ttk.Entry(pf, textvariable=var("python")).pack(side="left", fill="x", expand=True)
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

        lan = ttk.LabelFrame(right, text="Players connect to", padding=8)
        lan.pack(fill="x", pady=(8, 0))
        self.lan_var = tk.StringVar()
        ttk.Label(lan, textvariable=self.lan_var, justify="left").pack(anchor="w")
        ttk.Button(lan, text="Copy", command=self.copy_lan).pack(anchor="w", pady=(4, 0))
        self.update_lan()

        # Bottom: tabs.
        nb = ttk.Notebook(outer)
        nb.pack(fill="both", expand=True, pady=(8, 0))

        log_frame = ttk.Frame(nb)
        self.log_text = tk.Text(log_frame, wrap="none", font=("Consolas", 9), state="disabled", background="#111", foreground="#ddd", insertbackground="#ddd")
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
        )

    def make_tree(self, nb: ttk.Notebook, title: str, cols: list[tuple[str, str, int]]) -> ttk.Treeview:
        frame = ttk.Frame(nb)
        tree = ttk.Treeview(frame, columns=[c[0] for c in cols], show="headings")
        for key, text, width in cols:
            tree.heading(key, text=text)
            tree.column(key, width=width, anchor="w")
        ys = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=ys.set)
        tree.pack(side="left", fill="both", expand=True)
        ys.pack(side="left", fill="y")
        nb.add(frame, text=title)
        return tree

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
        cmd = [
            v["python"], "-u", str(SERVER_SCRIPT),
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
        if not self.check_python(cmd[0]):
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
            self.lan_var.set("127.0.0.1 (this PC only)")
        else:
            addrs = lan_addresses()
            self.lan_var.set("\n".join(addrs) if addrs else "no LAN address found")
        self.root.after(15000, self.update_lan)

    def copy_lan(self) -> None:
        first = self.lan_var.get().splitlines()[0].split()[0] if self.lan_var.get() else ""
        self.root.clipboard_clear()
        self.root.clipboard_append(first)

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
        self.user_tree.delete(*self.user_tree.get_children())
        for u in data.get("users", []):
            name = u.get("playerName") if u.get("playerName") not in (None, "", "Unknown") else u.get("name", "")
            self.user_tree.insert("", "end", values=(
                u.get("id"), name, u.get("authtype", ""), u.get("lobbyId") or "",
                "lounge" if u.get("inLounge") else ("in lobby" if u.get("lobbyId") else ""),
                u.get("wanAddress", ""), "yes" if u.get("isAdmin") else "",
            ))

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
        raise SystemExit(f"server.py not found next to {__file__}")
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
