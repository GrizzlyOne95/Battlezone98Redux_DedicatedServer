#!/usr/bin/env python3
"""Headless GUI settings tests: exact server arguments, persistence and forwarding help."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from server import parse_args
from server_gui import DEFAULTS, GUIDE, ServerGUI, access_badge, clean_host, mask_command, pair_port_notice, pending_texts, reachability_report
import server_gui


class Value:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value


def gui(values=None):
    config = {**DEFAULTS, **(values or {})}
    return SimpleNamespace(cfg=config.copy(), vars={k: Value(v) for k, v in config.items()})


def parsed_command(launcher, env=None):
    command = ServerGUI.build_command(launcher)
    with patch.dict(os.environ, env or {}, clear=True), patch.object(sys, "argv", ["server.py", *command[3:]]):
        return parse_args()


class ServerGUIRelayTests(unittest.TestCase):
    def test_existing_config_without_pair_settings_keeps_shared_mode(self):
        previous = {"bind": "0.0.0.0", "relay_enabled": True, "extra_args": ""}
        args = parsed_command(gui(previous), {"BZR_RELAY_PAIR_PORTS": "1"})
        self.assertFalse(args.relay_pair_ports)
        self.assertEqual((args.relay_pair_port_start, args.relay_pair_port_end), (0, 0))

    def test_explicit_pair_selection_passes_bounds_and_control_ports(self):
        args = parsed_command(gui({"relay_pair_ports": True, "relay_pair_port_start": 2340,
                                   "relay_pair_port_end": 2369, "udp_port": 2338, "relay_port": 2339}))
        self.assertTrue(args.relay_pair_ports)
        self.assertEqual((args.relay_pair_port_start, args.relay_pair_port_end), (2340, 2369))
        self.assertEqual((args.udp_port, args.relay_port), (2338, 2339))

    def test_legacy_extra_args_still_override_explicit_shared_mode(self):
        args = parsed_command(gui({"extra_args": "--relay-pair-ports --relay-pair-port-start 2340 --relay-pair-port-end 2345"}))
        self.assertTrue(args.relay_pair_ports)
        self.assertEqual((args.relay_pair_port_start, args.relay_pair_port_end), (2340, 2345))

    def test_extra_args_can_explicitly_restore_shared_mode(self):
        args = parsed_command(gui({"relay_pair_ports": True, "extra_args": "--no-relay-pair-ports"}))
        self.assertFalse(args.relay_pair_ports)

    def test_saved_pair_settings_roundtrip_to_server_command(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gui.json"
            launcher = gui({"relay_pair_ports": True, "relay_pair_port_start": 30000, "relay_pair_port_end": 30029})
            with patch.object(server_gui, "CONFIG_FILE", path):
                ServerGUI.save_config(launcher)
                saved = ServerGUI.load_config(launcher)
            self.assertEqual(json.loads(path.read_text())["relay_pair_port_start"], 30000)
            args = parsed_command(gui(saved))
            self.assertTrue(args.relay_pair_ports)
            self.assertEqual((args.relay_pair_port_start, args.relay_pair_port_end), (30000, 30029))

    def test_incomplete_or_invalid_bounds_do_not_build_a_command(self):
        for start, end in ((0, 1340), (1340, 0), (1341, 1340), (-1, 0), (1, 65536)):
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                ServerGUI.build_command(gui({"relay_pair_ports": True, "relay_pair_port_start": start, "relay_pair_port_end": end}))

    def test_ephemeral_gui_selection_requires_loopback(self):
        values = {"relay_pair_ports": True, "relay_pair_port_start": 0, "relay_pair_port_end": 0}
        with self.assertRaisesRegex(ValueError, "fixed pair UDP range"):
            ServerGUI.build_command(gui(values))
        args = parsed_command(gui({**values, "bind": "127.0.0.1"}))
        self.assertTrue(args.relay_pair_ports)
        self.assertEqual((args.relay_pair_port_start, args.relay_pair_port_end), (0, 0))

    def test_operator_help_distinguishes_shared_ephemeral_and_bounded_capacity(self):
        self.assertIn("one relayed peer", pair_port_notice(False, 1340, 1369))
        self.assertIn("this PC only", pair_port_notice(True, 0, 0))
        bounded = pair_port_notice(True, 1340, 1369)
        self.assertIn("1340-1369", bounded)
        self.assertIn("30 pair ports total across all lobbies", bounded)
        self.assertIn("four fully relayed players need six", bounded)


class ServerGUIHelperTests(unittest.TestCase):
    def test_clean_host_strips_scheme_path_port(self):
        self.assertEqual(clean_host(" ws://1.2.3.4:1337/ "), "1.2.3.4")
        self.assertEqual(clean_host("example.dyn.net"), "example.dyn.net")
        self.assertEqual(clean_host(""), "")

    def test_access_badge_levels(self):
        self.assertEqual(access_badge("open"), ("Access: Open (anyone)", "warn"))
        self.assertEqual(access_badge("approve"), ("Access: Approve new players", "ok"))
        self.assertEqual(access_badge("Allowlist only"), ("Access: Allowlist only", "ok"))

    def test_pending_texts(self):
        self.assertEqual(pending_texts(0), ("Access", ""))
        self.assertEqual(pending_texts(2)[0], "Access (2 pending)")
        self.assertIn("2 players waiting for approval", pending_texts(2)[1])
        self.assertIn("1 player waiting", pending_texts(1)[1])

    def facts(self, **kw):
        base = {"running": True, "bind": "0.0.0.0", "pair_enabled": True, "pair_start": 1340, "pair_end": 1369,
                "public_ip": "1.2.3.4", "ws_port": 1337, "tcp": {"ok": True, "error": ""}, "status": {"relay": {
                    "enabled": True, "packetsRxTotal": 5, "packetsTxTotal": 4, "pairPortRange": [1340, 1369],
                    "pairPortAllocationFailuresTotal": 0, "ambiguousDropsTotal": 0, "unmappedDropsTotal": 0}}}
        base.update(kw)
        return base

    def levels(self, report):
        return [lvl for lvl, _ in report]

    def test_report_all_good(self):
        report = reachability_report(self.facts())
        self.assertNotIn("error", self.levels(report))
        self.assertNotIn("warn", self.levels(report))
        text = " ".join(m for _, m in report)
        self.assertIn("reachable", text)
        self.assertIn("UDP RX", text)

    def test_report_flags_loopback_stopped_and_missing_ip(self):
        report = reachability_report(self.facts(running=False, bind="127.0.0.1", public_ip="", tcp=None, status=None))
        self.assertGreaterEqual(self.levels(report).count("error"), 2)
        self.assertTrue(any("Public IP unknown" in m for _, m in report))

    def test_tcp_failure_is_inconclusive_not_error(self):
        report = reachability_report(self.facts(tcp={"ok": False, "error": "timed out"}))
        msg = next(m for lvl, m in report if "failed" in m)
        self.assertIn("inconclusive", msg)
        self.assertIn("Test-NetConnection 1.2.3.4 -Port 1337", msg)
        self.assertNotIn("error", self.levels(report))

    def test_report_uses_relay_counters_defensively(self):
        relay = {"enabled": True, "pairPortAllocationFailuresTotal": 3, "unmappedDropsTotal": 2}
        report = reachability_report(self.facts(status={"relay": relay}))
        self.assertIn(("error", "pairPortAllocationFailuresTotal: 3 (range exhausted or ports busy/blocked)"), report)
        self.assertTrue(any(m.startswith("unmappedDropsTotal: 2") for _, m in report))
        reachability_report(self.facts(status={}))  # missing keys must not raise

    def test_setup_guide_has_key_content(self):
        text = "".join(t for _, t in GUIDE)
        for needle in ("1337", "1338-1369", "NEVER forward 8080", "Tailscale", "/bzrserver=ws://<LAN-IP>:1337/",
                       "New-NetFirewallRule", "Test-NetConnection", "unmappedDropsTotal"):
            self.assertIn(needle, text)

    def test_mask_command_hides_admin_key(self):
        cmd = ["x", "--admin-key", "SECRET", "--admin-key=SECRET2", "--motd", "hi"]
        shown = mask_command(cmd)
        self.assertEqual(shown, ["x", "--admin-key", "***", "--admin-key=***", "--motd", "hi"])
        self.assertEqual(cmd[2], "SECRET")


if __name__ == "__main__":
    unittest.main()
