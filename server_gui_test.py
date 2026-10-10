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
from server_gui import DEFAULTS, ServerGUI, pair_port_notice
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


if __name__ == "__main__":
    unittest.main()
