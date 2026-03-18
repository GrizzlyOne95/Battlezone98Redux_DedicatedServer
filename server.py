#!/usr/bin/env python3
"""
Battlezone 98 Redux dedicated-server compatibility shim.

This service provides:
1. A WebSocket lobby server (default port 1337) for BZRNet-style JSON messages.
2. A UDP shim (default port 1338) for initial BZRNet P2P handshake probing.

It is intentionally lightweight and in-memory to accelerate local testing and
community-hosted failover scenarios.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
from pathlib import Path
import signal
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

websockets = None
ConnectionClosed = Exception


DEFAULT_WS_HOST = "0.0.0.0"
DEFAULT_WS_PORT = 1337
DEFAULT_UDP_HOST = "0.0.0.0"
DEFAULT_UDP_PORT = 1338
DEFAULT_HEALTH_HOST = "127.0.0.1"
DEFAULT_HEALTH_PORT = 8080
DEFAULT_STATE_FILE = "data/server_state.json"
STATE_FORMAT_VERSION = 1
MAX_STDOUT_HISTORY = 200
MAX_PROTOCOL_HISTORY = 400

DEFAULT_WHITELIST = {
    "list": [
        "*.wav",
        "*.ogg",
        "*.ini",
        "*.inf",
        "*.jpg",
        "*.txt",
        "*.stb",
        "*.des",
        "*.dds",
        "*.png",
        "*.sta",
        "*mod.material",
    ],
    "major": 8,
    "minor": 0,
}

SUPPORTED_CLIENT_MESSAGES = [
    "Authorization",
    "CreateGame",
    "CreateLobby",
    "DeleteLobbyData",
    "DoEnterLounge",
    "DoExitLobby",
    "DoExitLounge",
    "DoJoinLobby",
    "DoKickUser",
    "DoPing",
    "DoP2PConnect",
    "DoP2PRoute",
    "DoSendChat",
    "DoSetLobbyOwner",
    "DoUpdateLAN",
    "DoUpdateWAN",
    "GetFullLog",
    "GetLobbyList",
    "GetStatLog",
    "GetUserList",
    "LockLobby",
    "Ping",
    "SetLobbyData",
    "SetLobbyMemberLimit",
    "SetPlayerData",
    "SetRelay",
    "SetTest",
]

SUPPORTED_SERVER_MESSAGES = [
    "OnAuthorization",
    "OnChatMessage",
    "OnDoEnterLounge",
    "OnDoExitLobby",
    "OnDoExitLounge",
    "OnEnableRelayChanged",
    "OnGetFullLog",
    "OnGetLobbyList",
    "OnGetStatLog",
    "OnGetUserList",
    "OnHeartbeat",
    "OnLANUpdated",
    "OnLobbyChanged",
    "OnLobbyCreated",
    "OnLobbyDataChanged",
    "OnLobbyJoined",
    "OnLobbyList",
    "OnLobbyListChanged",
    "OnLobbyMemberListChanged",
    "OnLobbyMemberP2PConnect",
    "OnLobbyRemoved",
    "OnRelayUpdated",
    "OnStdoutLogEntry",
    "OnUserDataChanged",
    "OnWANUpdated",
    "OnWhitelistUpdated",
]


def utc_now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def compact_preview(value: Any, *, limit: int = 240) -> str:
    if isinstance(value, (bytes, bytearray)):
        text = bytes(value).hex()
    else:
        try:
            text = json.dumps(value, separators=(",", ":"), sort_keys=True)
        except Exception:
            text = str(value)
    if len(text) > limit:
        return text[: limit - 3] + "..."
    return text


@dataclass
class UserSession:
    user_id: str
    name: str = "Unknown"
    player_name: str = "Unknown"
    authtype: str = "web"
    client_version: str = "2.2.301"
    key: str = ""
    lan_addresses: list[str] = field(default_factory=list)
    wan_address: str = ""
    in_lounge: bool = False
    is_admin: bool = False
    current_lobby_id: int | None = None
    extra_data: dict[str, Any] = field(default_factory=dict)
    ws: Any = None


@dataclass
class LobbyState:
    lobby_id: int
    owner: str
    metadata: dict[str, Any]
    member_limit: int = 20000
    is_private: bool = False
    is_locked: bool = False
    password: str = ""
    created_time: str = field(default_factory=utc_now_str)
    users: dict[str, dict[str, Any]] = field(default_factory=dict)


class ServerState:
    def __init__(
        self,
        logger: logging.Logger,
        *,
        state_file: str | None = None,
        admin_key: str = "",
        admin_ids: set[str] | None = None,
    ) -> None:
        self.log = logger
        self.next_lobby_id = 1000
        self.sessions_by_ws: dict[Any, UserSession] = {}
        self.sessions_by_user_id: dict[str, UserSession] = {}
        self.lobbies: dict[int, LobbyState] = {}
        self.state_file = Path(state_file).expanduser() if state_file else None
        self.admin_key = admin_key
        self.admin_ids = admin_ids or set()
        self.whitelist = json.loads(json.dumps(DEFAULT_WHITELIST))
        self.log_level_setting = 1
        self.relay_requested = False
        self.test_mode = False
        self.stdout_history: list[str] = []
        self.protocol_history: list[dict[str, Any]] = []
        self._dirty = False

    def allocate_lobby_id(self) -> int:
        lid = self.next_lobby_id
        self.next_lobby_id += 1
        self.mark_dirty()
        return lid

    def build_user_snapshot(self, s: UserSession) -> dict[str, Any]:
        snapshot = {
            "id": s.user_id,
            "userId": s.user_id,
            "name": s.name,
            "realname": s.name,
            "playerName": s.player_name,
            "authType": s.authtype,
            "clientVersion": s.client_version,
            "inLounge": s.in_lounge,
            "isInLounge": s.in_lounge,
            "isAdmin": s.is_admin,
            "lobby": -1 if s.current_lobby_id is None else s.current_lobby_id,
        }
        if s.wan_address:
            snapshot["wanAddress"] = s.wan_address
            snapshot["ipAddress"] = s.wan_address.split(":")[0]
        if s.lan_addresses:
            snapshot["lanAddresses"] = list(s.lan_addresses)
        if s.extra_data:
            snapshot["metadata"] = dict(s.extra_data)
            snapshot.update(s.extra_data)
        return snapshot

    def serialize_lobby(self, lobby: LobbyState) -> dict[str, Any]:
        return {
            "id": lobby.lobby_id,
            "owner": lobby.owner,
            "createdTime": lobby.created_time,
            "metadata": lobby.metadata,
            "memberLimit": lobby.member_limit,
            "isPrivate": lobby.is_private,
            "isLocked": lobby.is_locked,
            "password": lobby.password,
            "userCount": len(lobby.users),
            "clientVersion": lobby.metadata.get("clientVersion", "2.2.301"),
            "users": lobby.users,
        }

    def serialize_lobbies(self) -> dict[str, dict[str, Any]]:
        return {str(lid): self.serialize_lobby(lobby) for lid, lobby in self.lobbies.items()}

    def serialize_users(self) -> dict[str, dict[str, Any]]:
        return {uid: self.build_user_snapshot(s) for uid, s in self.sessions_by_user_id.items()}

    def metrics(self) -> dict[str, Any]:
        return {
            "activeConnections": len(self.sessions_by_ws),
            "activeUsers": len(self.sessions_by_user_id),
            "lobbyCount": len(self.lobbies),
            "nextLobbyId": self.next_lobby_id,
            "stdoutHistoryCount": len(self.stdout_history),
            "protocolHistoryCount": len(self.protocol_history),
            "whitelistEntries": len(self.whitelist.get("list", [])),
            "relayRequested": self.relay_requested,
            "testMode": self.test_mode,
            "logLevelSetting": self.log_level_setting,
            "stateDirty": self._dirty,
        }

    def record_stdout_entry(self, level: str, message: str) -> str:
        entry = f"[{utc_now_str()}] {str(level).upper()}: {message}"
        self.stdout_history.append(entry)
        if len(self.stdout_history) > MAX_STDOUT_HISTORY:
            self.stdout_history = self.stdout_history[-MAX_STDOUT_HISTORY:]
        return entry

    def record_protocol_event(
        self,
        *,
        channel: str,
        direction: str,
        peer: str,
        msg_type: str,
        payload: Any,
    ) -> dict[str, Any]:
        entry = {
            "time": utc_now_str(),
            "channel": channel,
            "direction": direction,
            "peer": peer,
            "type": msg_type,
            "preview": compact_preview(payload),
        }
        self.protocol_history.append(entry)
        if len(self.protocol_history) > MAX_PROTOCOL_HISTORY:
            self.protocol_history = self.protocol_history[-MAX_PROTOCOL_HISTORY:]
        return entry

    def mark_dirty(self) -> None:
        self._dirty = True

    def load_state(self) -> None:
        if not self.state_file:
            return
        if not self.state_file.exists():
            self.log.info("State file not found (fresh start): %s", self.state_file)
            return

        try:
            payload = json.loads(self.state_file.read_text(encoding="utf-8"))
            version = int(payload.get("version", 0))
            if version != STATE_FORMAT_VERSION:
                self.log.warning(
                    "Unsupported state format version %s (expected %s). Ignoring state file.",
                    version,
                    STATE_FORMAT_VERSION,
                )
                return

            self.next_lobby_id = int(payload.get("nextLobbyId", self.next_lobby_id))
            whitelist = payload.get("whitelist")
            if isinstance(whitelist, dict) and isinstance(whitelist.get("list"), list):
                self.whitelist = {
                    "list": [str(x) for x in whitelist.get("list", []) if isinstance(x, str)],
                    "major": int(whitelist.get("major", DEFAULT_WHITELIST["major"])),
                    "minor": int(whitelist.get("minor", DEFAULT_WHITELIST["minor"])),
                }
            self.log_level_setting = int(payload.get("logLevelSetting", self.log_level_setting))
            self.relay_requested = bool(payload.get("relayRequested", self.relay_requested))
            self.test_mode = bool(payload.get("testMode", self.test_mode))
            self.lobbies = {}
            for raw_lobby in payload.get("lobbies", []):
                try:
                    lid = int(raw_lobby.get("id"))
                except Exception:
                    continue

                # Users are not restored; sessions are runtime-only.
                lobby = LobbyState(
                    lobby_id=lid,
                    owner="",
                    metadata=dict(raw_lobby.get("metadata", {})),
                    member_limit=int(raw_lobby.get("memberLimit", 20000)),
                    is_private=bool(raw_lobby.get("isPrivate", False)),
                    is_locked=bool(raw_lobby.get("isLocked", False)),
                    password=str(raw_lobby.get("password", "")),
                    created_time=str(raw_lobby.get("createdTime", utc_now_str())),
                    users={},
                )
                self.lobbies[lid] = lobby

            self._dirty = False
            self.log.info(
                "Loaded state: %d lobbies, nextLobbyId=%d from %s",
                len(self.lobbies),
                self.next_lobby_id,
                self.state_file,
            )
        except Exception:
            self.log.exception("Failed to load state file: %s", self.state_file)

    def _state_payload(self) -> dict[str, Any]:
        persisted_lobbies = []
        for lid, lobby in self.lobbies.items():
            persisted_lobbies.append(
                {
                    "id": lid,
                    "owner": lobby.owner,
                    "metadata": lobby.metadata,
                    "memberLimit": lobby.member_limit,
                    "isPrivate": lobby.is_private,
                    "isLocked": lobby.is_locked,
                    "password": lobby.password,
                    "createdTime": lobby.created_time,
                }
            )
        return {
            "version": STATE_FORMAT_VERSION,
            "savedAt": utc_now_str(),
            "nextLobbyId": self.next_lobby_id,
            "whitelist": self.whitelist,
            "logLevelSetting": self.log_level_setting,
            "relayRequested": self.relay_requested,
            "testMode": self.test_mode,
            "lobbies": persisted_lobbies,
        }

    def save_state(self, *, force: bool = False) -> None:
        if not self.state_file:
            return
        if not force and not self._dirty:
            return

        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            tmp_file = self.state_file.with_suffix(self.state_file.suffix + ".tmp")
            tmp_file.write_text(json.dumps(self._state_payload(), indent=2), encoding="utf-8")
            tmp_file.replace(self.state_file)
            self._dirty = False
            self.log.debug("State saved to %s", self.state_file)
        except Exception:
            self.log.exception("Failed to save state file: %s", self.state_file)


class BZRUDPProtocol(asyncio.DatagramProtocol):
    def __init__(self, logger: logging.Logger, state: ServerState) -> None:
        self.logger = logger
        self.state = state
        self.transport: asyncio.DatagramTransport | None = None
        self.rx_count = 0
        self.tx_count = 0

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]
        self.logger.info("UDP shim listening on %s", transport.get_extra_info("sockname"))

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self.rx_count += 1
        hex_data = data.hex()
        self.state.record_protocol_event(
            channel="udp",
            direction="rx",
            peer=f"{addr[0]}:{addr[1]}",
            msg_type="udp_datagram",
            payload=hex_data,
        )
        self.logger.debug("UDP RX <- %s:%d | %s", addr[0], addr[1], hex_data)

        reply = self._build_reply(data)
        if reply and self.transport:
            self.transport.sendto(reply, addr)
            self.tx_count += 1
            self.state.record_protocol_event(
                channel="udp",
                direction="tx",
                peer=f"{addr[0]}:{addr[1]}",
                msg_type="udp_datagram",
                payload=reply.hex(),
            )
            self.logger.debug("UDP TX -> %s:%d | %s", addr[0], addr[1], reply.hex())

    @staticmethod
    def _build_reply(data: bytes) -> bytes:
        if not data:
            return b""

        # Observed BZRNet probe packet starts with 0x40 and server replies with
        # the original payload plus small server-time trailer bytes.
        if data[0] == 0x40 and len(data) >= 18:
            ms = int(time.time() * 1000) & 0xFFFFFFFFFFFF
            return data + b"\x04" + ms.to_bytes(6, "big")

        # For unknown packet types, echo to keep local probes iterative.
        return data

    def error_received(self, exc: Exception) -> None:
        self.logger.warning("UDP error: %s", exc)


class BZRWebSocketServer:
    def __init__(
        self,
        state: ServerState,
        logger: logging.Logger,
        *,
        motd: str = "",
        max_lobbies: int = 500,
        retain_empty_lobbies: bool = False,
    ):
        self.state = state
        self.log = logger
        self.motd = motd.strip()
        self.max_lobbies = max(1, int(max_lobbies))
        self.retain_empty_lobbies = bool(retain_empty_lobbies)
        self.ws_messages_total = 0
        self.auth_total = 0
        self.chat_total = 0
        self.lobbies_created_total = 0
        self.lobbies_joined_total = 0
        self.kicks_total = 0

    def metrics(self) -> dict[str, Any]:
        return {
            "messagesTotal": self.ws_messages_total,
            "authTotal": self.auth_total,
            "chatTotal": self.chat_total,
            "lobbiesCreatedTotal": self.lobbies_created_total,
            "lobbiesJoinedTotal": self.lobbies_joined_total,
            "kicksTotal": self.kicks_total,
        }

    def build_stat_log(self) -> str:
        lines = [
            "BZR Community Failover Server",
            f"time={utc_now_str()}",
        ]
        for key, value in self.state.metrics().items():
            lines.append(f"state.{key}={value}")
        for key, value in self.metrics().items():
            lines.append(f"ws.{key}={value}")
        lines.extend(
            [
                "compatibility.websocketLobby=true",
                "compatibility.udpHandshakeShim=true",
                "compatibility.directPeerSessionsExpected=true",
                "compatibility.relayTransportImplemented=false",
                "compatibility.steamTicketValidation=false",
            ]
        )
        for lid, lobby in sorted(self.state.lobbies.items()):
            lines.append(
                "lobby."
                + json.dumps(
                    {
                        "id": lid,
                        "name": str(lobby.metadata.get("name", "")),
                        "owner": lobby.owner,
                        "users": len(lobby.users),
                        "memberLimit": lobby.member_limit,
                        "isLocked": lobby.is_locked,
                        "isPrivate": lobby.is_private,
                    },
                    separators=(",", ":"),
                )
            )
        return "\n".join(lines)

    @staticmethod
    def _ws_peer(ws: Any) -> str:
        peer = getattr(ws, "remote_address", None)
        if isinstance(peer, tuple) and len(peer) >= 2:
            return f"{peer[0]}:{peer[1]}"
        return "unknown"

    @staticmethod
    def _coerce_bool(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    @staticmethod
    def _dedupe_strings(values: list[str]) -> list[str]:
        seen: set[str] = set()
        ordered: list[str] = []
        for raw in values:
            text = str(raw).strip()
            if not text or text in seen:
                continue
            seen.add(text)
            ordered.append(text)
        return ordered

    def _extract_wan_address(self, content: Any) -> str:
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, dict):
            for key in ("wanAddress", "address", "value", "ipAddress"):
                value = content.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return ""

    def _extract_lan_addresses(self, content: Any) -> list[str]:
        if isinstance(content, str):
            return self._dedupe_strings([content])
        if isinstance(content, list):
            return self._dedupe_strings([str(x) for x in content if isinstance(x, str)])
        if isinstance(content, dict):
            if isinstance(content.get("lanAddresses"), list):
                return self._dedupe_strings([str(x) for x in content.get("lanAddresses", []) if isinstance(x, str)])
            for key in ("lanAddress", "address", "value"):
                value = content.get(key)
                if isinstance(value, str):
                    return self._dedupe_strings([value])
        return []

    def _extract_target_user_id(self, content: Any) -> str:
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, dict):
            for key in ("id", "userId", "targetId", "member", "peerId", "friendID"):
                value = content.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
                if value is not None and str(value).strip():
                    return str(value).strip()
        return ""

    def _build_user_change_payload(self, session: UserSession, key: str, value: Any) -> dict[str, Any]:
        snapshot = self.state.build_user_snapshot(session)
        return {
            "type": "OnUserDataChanged",
            "time": utc_now_str(),
            "success": True,
            "id": session.user_id,
            "userId": session.user_id,
            "member": session.name,
            "memberName": session.name,
            "key": key,
            "value": value,
            "changedLobby": session.current_lobby_id,
            "user": snapshot,
            "users": {session.user_id: snapshot},
        }

    def _build_lobby_data_payload(
        self,
        lobby: LobbyState,
        *,
        key: str,
        value: Any = None,
        deleted: bool = False,
    ) -> dict[str, Any]:
        lobby_blob = self.state.serialize_lobby(lobby)
        payload = {
            "type": "OnLobbyDataChanged",
            "time": utc_now_str(),
            "success": True,
            "id": lobby.lobby_id,
            "lobbyId": lobby.lobby_id,
            "changedLobby": lobby.lobby_id,
            "key": key,
            "lobby": lobby_blob,
            "lobbies": {str(lobby.lobby_id): lobby_blob},
        }
        if deleted:
            payload["deleted"] = True
        else:
            payload["value"] = value
        return payload

    def _build_address_payload(self, session: UserSession, *, message_type: str) -> dict[str, Any]:
        snapshot = self.state.build_user_snapshot(session)
        payload = {
            "type": message_type,
            "time": utc_now_str(),
            "success": True,
            "id": session.user_id,
            "userId": session.user_id,
            "member": session.name,
            "memberName": session.name,
            "changedLobby": session.current_lobby_id,
            "user": snapshot,
            "users": {session.user_id: snapshot},
        }
        if message_type == "OnWANUpdated":
            payload["wanAddress"] = session.wan_address
        else:
            payload["lanAddresses"] = list(session.lan_addresses)
        return payload

    async def _broadcast_session_addresses(self, session: UserSession) -> None:
        if session.wan_address:
            await self.broadcast_all("OnWANUpdated", self._build_address_payload(session, message_type="OnWANUpdated"))
        if session.lan_addresses:
            await self.broadcast_all("OnLANUpdated", self._build_address_payload(session, message_type="OnLANUpdated"))

    async def send_stdout(self, ws: Any, level: str, message: str) -> None:
        entry = self.state.record_stdout_entry(level, message)
        await self.send(
            ws,
            "OnStdoutLogEntry",
            {
                "time": utc_now_str(),
                "level": level,
                "message": message,
                "txt": entry,
            },
        )

    async def send(self, ws: Any, msg_type: str, data: Any) -> None:
        payload = {"type": msg_type, "data": data}
        self.state.record_protocol_event(
            channel="ws",
            direction="tx",
            peer=self._ws_peer(ws),
            msg_type=msg_type,
            payload=data,
        )
        await ws.send(json.dumps(payload, separators=(",", ":")))

    async def send_error(self, ws: Any, message: str) -> None:
        await self.send_stdout(ws, "error", message)

    async def broadcast_all(self, msg_type: str, data: Any) -> None:
        if not self.state.sessions_by_ws:
            return
        payload = json.dumps({"type": msg_type, "data": data}, separators=(",", ":"))
        coros = []
        for session in self.state.sessions_by_ws.values():
            self.state.record_protocol_event(
                channel="ws",
                direction="tx",
                peer=self._ws_peer(session.ws),
                msg_type=msg_type,
                payload=data,
            )
            coros.append(session.ws.send(payload))
        await asyncio.gather(*coros, return_exceptions=True)

    async def broadcast_lobby(self, lobby: LobbyState, msg_type: str, data: Any) -> None:
        payload = json.dumps({"type": msg_type, "data": data}, separators=(",", ":"))
        targets = []
        for uid in lobby.users:
            session = self.state.sessions_by_user_id.get(uid)
            if session and session.ws:
                self.state.record_protocol_event(
                    channel="ws",
                    direction="tx",
                    peer=self._ws_peer(session.ws),
                    msg_type=msg_type,
                    payload=data,
                )
                targets.append(session.ws.send(payload))
        if targets:
            await asyncio.gather(*targets, return_exceptions=True)

    async def push_lobby_list(self, ws: Any, msg_type: str = "OnGetLobbyList") -> None:
        await self.send(ws, msg_type, {"lobbies": self.state.serialize_lobbies()})

    async def push_lobby_changed(self, lobby: LobbyState) -> None:
        await self.broadcast_all(
            "OnLobbyChanged", {"lobbies": {str(lobby.lobby_id): self.state.serialize_lobby(lobby)}}
        )

    async def push_full_lobby_list(self) -> None:
        await self.broadcast_all("OnLobbyListChanged", {"lobbies": self.state.serialize_lobbies()})

    def _apply_player_data(self, session: UserSession, key: str, value: Any) -> None:
        if key == "name":
            session.name = str(value)
        elif key == "playerName":
            session.player_name = str(value)
        elif key == "clientVersion":
            session.client_version = str(value)
        elif key == "authType":
            session.authtype = str(value)
        else:
            session.extra_data[key] = value

        if session.current_lobby_id is not None:
            lobby = self.state.lobbies.get(session.current_lobby_id)
            if lobby and session.user_id in lobby.users:
                lobby.users[session.user_id] = self.state.build_user_snapshot(session)

    def _normalize_lobby_name(self, raw_name: str) -> str:
        if not raw_name:
            return "~chat~pub~~MyLobby"
        return raw_name if raw_name.startswith("~chat~pub~~") else f"~chat~pub~~{raw_name}"

    def _normalize_game_name(self, raw_name: str) -> str:
        if not raw_name:
            return "~game~pub~~MyGame"
        return raw_name if raw_name.startswith("~game~pub~~") else f"~game~pub~~{raw_name}"

    async def remove_from_lobby(
        self,
        session: UserSession,
        *,
        send_exit_ack: bool = False,
        ack_type: str = "OnDoExitLobby",
    ) -> None:
        lid = session.current_lobby_id
        if lid is None:
            if send_exit_ack:
                await self.send(session.ws, ack_type, {"success": False, "reason": "Not in lobby"})
            return

        lobby = self.state.lobbies.get(lid)
        session.current_lobby_id = None
        session.in_lounge = True
        if not lobby:
            if send_exit_ack:
                await self.send(session.ws, ack_type, {"success": True, "id": lid})
            return

        if session.user_id in lobby.users:
            del lobby.users[session.user_id]
            self.state.mark_dirty()
            self.state.record_stdout_entry("info", f"{session.name} left lobby {lid}")
            await self.broadcast_all(
                "OnLobbyMemberListChanged",
                {
                    "lobbyId": lid,
                    "id": session.user_id,
                    "member": session.name,
                    "memberId": session.user_id,
                    "removed": True,
                    "user": self.state.build_user_snapshot(session),
                    "users": {session.user_id: self.state.build_user_snapshot(session)},
                },
            )

        if lobby.owner == session.user_id:
            remaining = list(lobby.users.keys())
            lobby.owner = remaining[0] if remaining else ""
            self.state.mark_dirty()

        if not lobby.users:
            if self.retain_empty_lobbies:
                await self.push_lobby_changed(lobby)
            else:
                del self.state.lobbies[lid]
                self.state.mark_dirty()
                await self.broadcast_all("OnLobbyRemoved", {"id": lid, "time": utc_now_str()})
        else:
            await self.push_lobby_changed(lobby)

        if send_exit_ack:
            await self.send(session.ws, ack_type, {"success": True, "id": lid})
        await self.push_full_lobby_list()

    async def handle_authorization(self, ws: Any, content: dict[str, Any]) -> None:
        requested_id = str(content.get("id", "")).strip()
        user_id = requested_id or f"U{uuid.uuid4().hex[:12]}"
        name = str(content.get("name") or content.get("playerName") or user_id)

        if user_id in self.state.sessions_by_user_id:
            # Ensure uniqueness if duplicate ID reconnects.
            user_id = f"{user_id}_{uuid.uuid4().hex[:4]}"

        session = UserSession(
            user_id=user_id,
            name=name,
            player_name=str(content.get("playerName") or name),
            authtype=str(content.get("authtype", "web")),
            client_version=str(content.get("clientVersion", "2.2.301")),
            key=str(content.get("key", "")),
            lan_addresses=[str(x) for x in content.get("lanAddresses", []) if isinstance(x, str)],
            wan_address=str(content.get("wanAddress", "")),
            in_lounge=bool(content.get("inLounge", False)),
            ws=ws,
        )
        key = str(content.get("key", ""))
        if self.state.admin_key and key == self.state.admin_key:
            session.is_admin = True
        if session.user_id in self.state.admin_ids:
            session.is_admin = True

        self.state.sessions_by_ws[ws] = session
        self.state.sessions_by_user_id[user_id] = session
        self.auth_total += 1
        self.state.record_stdout_entry(
            "info",
            f"Authorized {session.user_id} ({session.name}) via {session.authtype}",
        )

        await self.send(
            ws,
            "OnAuthorization",
            {
                "type": "OnAuthorization",
                "time": utc_now_str(),
                "success": True,
                "id": session.user_id,
                "userId": session.user_id,
                "member": session.name,
                "name": session.name,
                "playerName": session.player_name,
                "realname": session.name,
                "authType": session.authtype,
                "isAdmin": session.is_admin,
                "isBB": False,
                "isAuth": True,
                "isInLounge": session.in_lounge,
                "wanAddress": session.wan_address,
                "clientVersion": session.client_version,
                "lanAddresses": list(session.lan_addresses),
                "user": self.state.build_user_snapshot(session),
            },
        )
        await self.send(
            ws,
            "OnWhitelistUpdated",
            {
                "type": "OnWhitelistUpdated",
                "time": utc_now_str(),
                "success": True,
                "whitelist": self.state.whitelist,
            },
        )
        await self.send(
            ws,
            "OnRelayUpdated",
            {
                "type": "OnRelayUpdated",
                "time": utc_now_str(),
                "enabled": False,
                "requested": self.state.relay_requested,
            },
        )
        if session.is_admin:
            await self.send(
                ws,
                "OnGetLogLevel",
                {
                    "type": "OnGetLogLevel",
                    "time": utc_now_str(),
                    "success": True,
                    "level": [self.state.log_level_setting, self.state.log_level_setting],
                },
            )
            await self.send(ws, "OnGetUserList", {"users": self.state.serialize_users()})
            await self.send(
                ws,
                "OnGetFullLog",
                {
                    "entries": list(self.state.stdout_history),
                    "generatedAt": utc_now_str(),
                    "stdout": {"history": list(self.state.stdout_history)},
                    "protocol": {"history": list(self.state.protocol_history)},
                },
            )

        if self.motd:
            await self.send(
                ws,
                "OnChatMessage",
                {
                    "author": "SERVER",
                    "speakerId": "SERVER",
                    "text": self.motd,
                    "lobbyId": None,
                    "time": utc_now_str(),
                    "system": True,
                },
            )

        await self._broadcast_session_addresses(session)
        self.log.info("Authorized user_id=%s name=%s", session.user_id, session.name)

    async def handle_create_lobby(self, session: UserSession, content: dict[str, Any]) -> None:
        if len(self.state.lobbies) >= self.max_lobbies:
            await self.send(
                session.ws,
                "OnLobbyCreated",
                {"success": False, "reason": f"Max lobby count reached ({self.max_lobbies})"},
            )
            return

        raw_name = str(content.get("name", "MyLobby"))
        name = self._normalize_lobby_name(raw_name)
        await self.remove_from_lobby(session, send_exit_ack=False)

        lid = self.state.allocate_lobby_id()
        lobby = LobbyState(
            lobby_id=lid,
            owner=session.user_id,
            member_limit=int(content.get("memberLimit", 20000)),
            is_private=bool(content.get("isPrivate", False)),
            is_locked=bool(content.get("isLocked", False)),
            password=str(content.get("password", "")),
            metadata={
                "name": name,
                "clientVersion": session.client_version,
                "GameVersion": session.client_version,
                "gameType": "1",
                "gameSettings": "*",
                "ready": "*",
            },
        )
        lobby.users[session.user_id] = self.state.build_user_snapshot(session)
        self.state.lobbies[lid] = lobby

        session.current_lobby_id = lid
        session.in_lounge = False
        self.lobbies_created_total += 1
        self.state.mark_dirty()
        self.state.record_stdout_entry("info", f"Created lobby {lid} owner={session.user_id} name={name}")

        await self.send(session.ws, "OnLobbyCreated", {"success": True, "id": lid, "time": utc_now_str()})
        await self.push_lobby_changed(lobby)
        await self.push_full_lobby_list()

    async def handle_create_game(self, session: UserSession, content: dict[str, Any]) -> None:
        payload = dict(content)
        payload["name"] = self._normalize_game_name(str(content.get("name", "MyGame")))
        await self.handle_create_lobby(session, payload)
        lid = session.current_lobby_id
        if lid is None:
            return
        lobby = self.state.lobbies.get(lid)
        if not lobby:
            return
        lobby.metadata.setdefault("nextmid", str(content.get("nextmid", "1")))
        lobby.metadata.setdefault("gameType", str(content.get("gameType", "1")))
        lobby.metadata.setdefault("gameSettings", content.get("gameSettings", "*"))
        self.state.mark_dirty()
        await self.push_lobby_changed(lobby)

    async def handle_join_lobby(self, session: UserSession, content: dict[str, Any]) -> None:
        requested_lid = content.get("id")
        if requested_lid is None:
            await self.send(session.ws, "OnLobbyJoined", {"success": False, "reason": "Missing lobby id"})
            return

        try:
            lid = int(requested_lid)
        except Exception:
            await self.send(session.ws, "OnLobbyJoined", {"success": False, "reason": "Invalid lobby id"})
            return

        lobby = self.state.lobbies.get(lid)
        if not lobby:
            await self.send(session.ws, "OnLobbyJoined", {"success": False, "reason": "Lobby not found"})
            return

        if lobby.is_locked:
            await self.send(session.ws, "OnLobbyJoined", {"success": False, "reason": "Lobby is locked"})
            return

        supplied_password = str(content.get("password", ""))
        if lobby.password and supplied_password != lobby.password:
            await self.send(session.ws, "OnLobbyJoined", {"success": False, "reason": "Invalid password"})
            return

        if len(lobby.users) >= max(1, lobby.member_limit) and session.user_id not in lobby.users:
            await self.send(session.ws, "OnLobbyJoined", {"success": False, "reason": "Lobby full"})
            return

        if session.current_lobby_id != lid:
            await self.remove_from_lobby(session, send_exit_ack=False)
            lobby.users[session.user_id] = self.state.build_user_snapshot(session)
            if not lobby.owner:
                lobby.owner = session.user_id
            session.current_lobby_id = lid
            session.in_lounge = False
            self.lobbies_joined_total += 1
            self.state.mark_dirty()
            self.state.record_stdout_entry("info", f"{session.user_id} joined lobby {lid}")

            await self.broadcast_all(
                "OnLobbyMemberListChanged",
                {
                    "lobbyId": lid,
                    "id": session.user_id,
                    "member": session.name,
                    "memberId": session.user_id,
                    "removed": False,
                    "user": self.state.build_user_snapshot(session),
                    "users": {session.user_id: self.state.build_user_snapshot(session)},
                },
            )

        await self.send(session.ws, "OnLobbyJoined", {"success": True, "id": lid, "time": utc_now_str()})
        await self.push_lobby_changed(lobby)
        await self.push_full_lobby_list()

    async def handle_update_wan(self, session: UserSession, content: Any) -> None:
        wan = self._extract_wan_address(content)
        if not wan:
            await self.send_error(session.ws, "DoUpdateWAN ignored: missing address")
            return
        session.wan_address = wan
        if session.current_lobby_id is not None:
            lobby = self.state.lobbies.get(session.current_lobby_id)
            if lobby and session.user_id in lobby.users:
                lobby.users[session.user_id] = self.state.build_user_snapshot(session)
        self.state.record_stdout_entry("info", f"{session.user_id} updated WAN address to {wan}")
        await self.broadcast_all("OnWANUpdated", self._build_address_payload(session, message_type="OnWANUpdated"))
        if session.current_lobby_id and session.current_lobby_id in self.state.lobbies:
            await self.push_lobby_changed(self.state.lobbies[session.current_lobby_id])

    async def handle_update_lan(self, session: UserSession, content: Any) -> None:
        lan_addresses = self._extract_lan_addresses(content)
        if not lan_addresses:
            await self.send_error(session.ws, "DoUpdateLAN ignored: missing address")
            return
        session.lan_addresses = lan_addresses
        if session.current_lobby_id is not None:
            lobby = self.state.lobbies.get(session.current_lobby_id)
            if lobby and session.user_id in lobby.users:
                lobby.users[session.user_id] = self.state.build_user_snapshot(session)
        self.state.record_stdout_entry(
            "info",
            f"{session.user_id} updated LAN addresses to {','.join(session.lan_addresses)}",
        )
        await self.broadcast_all("OnLANUpdated", self._build_address_payload(session, message_type="OnLANUpdated"))
        if session.current_lobby_id and session.current_lobby_id in self.state.lobbies:
            await self.push_lobby_changed(self.state.lobbies[session.current_lobby_id])

    async def handle_p2p_signal(self, session: UserSession, content: Any, *, route: bool) -> None:
        target_id = self._extract_target_user_id(content)
        target_session = self.state.sessions_by_user_id.get(target_id) if target_id else None
        event_name = "DoP2PRoute" if route else "DoP2PConnect"
        self.state.record_stdout_entry(
            "info",
            f"{event_name} from {session.user_id} target={target_id or '?'} route={compact_preview(content)}",
        )
        payload = {
            "type": "OnLobbyMemberP2PConnect",
            "time": utc_now_str(),
            "success": target_session is not None,
            "lobbyId": session.current_lobby_id,
            "changedLobby": session.current_lobby_id,
            "sourceUserId": session.user_id,
            "sourceName": session.name,
            "targetId": target_id,
            "requested": content,
            "routeRequested": route,
            "relayEnabled": False,
            "sourceWanAddress": session.wan_address,
            "sourceLanAddresses": list(session.lan_addresses),
            "user": self.state.build_user_snapshot(session),
            "users": {session.user_id: self.state.build_user_snapshot(session)},
        }
        if target_session:
            payload["target"] = self.state.build_user_snapshot(target_session)
            payload["users"][target_session.user_id] = self.state.build_user_snapshot(target_session)
            await self.send(target_session.ws, "OnLobbyMemberP2PConnect", payload)
        await self.send(session.ws, "OnLobbyMemberP2PConnect", payload)

    async def handle_chat(self, session: UserSession, text: str) -> None:
        text = text.strip()
        if not text:
            return
        self.chat_total += 1

        lid = session.current_lobby_id
        chat_payload = {
            "author": session.name,
            "speakerId": session.user_id,
            "text": text,
            "lobbyId": lid,
            "time": utc_now_str(),
        }
        self.state.record_stdout_entry("info", f"Chat from {session.user_id} in lobby {lid}: {text}")

        # Support simple /t targetName message style whisper.
        if text.lower().startswith("/t "):
            parts = text.split(" ", 2)
            if len(parts) >= 3:
                target_name = parts[1].lower()
                private_msg = parts[2]
                for target in self.state.sessions_by_ws.values():
                    if target.name.lower() == target_name:
                        whisper = dict(chat_payload)
                        whisper["text"] = private_msg
                        whisper["private"] = True
                        await self.send(target.ws, "OnChatMessage", whisper)
                        await self.send(session.ws, "OnChatMessage", whisper)
                        return

        if lid is not None and lid in self.state.lobbies:
            await self.broadcast_lobby(self.state.lobbies[lid], "OnChatMessage", chat_payload)
            return

        # Lounge chat fanout.
        await self.broadcast_all("OnChatMessage", chat_payload)

    async def handle_set_lobby_data(self, session: UserSession, content: dict[str, Any]) -> None:
        lid = session.current_lobby_id
        if lid is None:
            await self.send_error(session.ws, "SetLobbyData ignored: not in lobby")
            return

        lobby = self.state.lobbies.get(lid)
        if not lobby:
            await self.send_error(session.ws, "SetLobbyData ignored: lobby not found")
            return

        if str(lobby.owner) != str(session.user_id):
            await self.send_error(session.ws, "SetLobbyData denied: owner only")
            return

        key = str(content.get("key", "")).strip()
        if not key:
            await self.send_error(session.ws, "SetLobbyData ignored: missing key")
            return

        value = content.get("value")
        lobby.metadata[key] = value
        self.state.mark_dirty()
        await self.broadcast_all(
            "OnLobbyDataChanged",
            self._build_lobby_data_payload(lobby, key=key, value=value),
        )
        await self.push_lobby_changed(lobby)

    async def handle_set_lobby_owner(self, session: UserSession, target_id: str) -> None:
        lid = session.current_lobby_id
        if lid is None:
            await self.send_error(session.ws, "DoSetLobbyOwner ignored: not in lobby")
            return
        lobby = self.state.lobbies.get(lid)
        if not lobby:
            await self.send_error(session.ws, "DoSetLobbyOwner ignored: lobby not found")
            return
        if lobby.owner != session.user_id:
            await self.send_error(session.ws, "DoSetLobbyOwner denied: owner only")
            return
        if target_id not in lobby.users:
            await self.send_error(session.ws, "DoSetLobbyOwner denied: target not in lobby")
            return
        lobby.owner = target_id
        self.state.mark_dirty()
        await self.push_lobby_changed(lobby)

    async def handle_kick_user(self, session: UserSession, target_id: str) -> None:
        lid = session.current_lobby_id
        if lid is None:
            await self.send_error(session.ws, "DoKickUser ignored: not in lobby")
            return
        lobby = self.state.lobbies.get(lid)
        if not lobby:
            await self.send_error(session.ws, "DoKickUser ignored: lobby not found")
            return
        if lobby.owner != session.user_id:
            await self.send_error(session.ws, "DoKickUser denied: owner only")
            return
        if target_id == session.user_id:
            await self.send_error(session.ws, "DoKickUser ignored: cannot kick self")
            return
        target_session = self.state.sessions_by_user_id.get(target_id)
        if not target_session or target_session.current_lobby_id != lid:
            await self.send_error(session.ws, "DoKickUser ignored: user not in lobby")
            return
        self.kicks_total += 1
        await self.remove_from_lobby(target_session, send_exit_ack=True)
        self.state.record_stdout_entry("warn", f"{target_id} was kicked from lobby {lid} by {session.user_id}")
        await self.send_stdout(target_session.ws, "warn", "You were kicked from the lobby")

    async def route_message(self, ws: Any, raw_message: str) -> None:
        try:
            envelope = json.loads(raw_message)
        except json.JSONDecodeError:
            await self.send_error(ws, "Invalid JSON")
            return

        if not isinstance(envelope, dict):
            await self.send_error(ws, "Invalid message format")
            return

        msg_type = envelope.get("type")
        if not msg_type:
            await self.send_error(ws, "Missing message type")
            return
        self.ws_messages_total += 1

        content = envelope.get("content")
        if content is None:
            content = envelope.get("data", {})
        if content is None:
            content = {}
        self.state.record_protocol_event(
            channel="ws",
            direction="rx",
            peer=self._ws_peer(ws),
            msg_type=str(msg_type),
            payload=content,
        )

        if msg_type == "Authorization":
            await self.handle_authorization(ws, content if isinstance(content, dict) else {})
            return

        session = self.state.sessions_by_ws.get(ws)
        if not session:
            await self.send_error(ws, "Authorization required")
            return

        if msg_type == "DoEnterLounge":
            session.in_lounge = self._coerce_bool(content)
            await self.send(ws, "OnDoEnterLounge", {"success": True, "time": utc_now_str()})
            await self.push_lobby_list(ws, msg_type="OnLobbyListChanged")
            return

        if msg_type == "GetLobbyList":
            await self.push_lobby_list(ws, msg_type="OnLobbyList")
            await self.push_lobby_list(ws, msg_type="OnGetLobbyList")
            return

        if msg_type == "CreateLobby":
            await self.handle_create_lobby(session, content if isinstance(content, dict) else {})
            return

        if msg_type == "CreateGame":
            await self.handle_create_game(session, content if isinstance(content, dict) else {})
            return

        if msg_type == "DoJoinLobby":
            await self.handle_join_lobby(session, content if isinstance(content, dict) else {})
            return

        if msg_type == "DoExitLobby":
            await self.remove_from_lobby(session, send_exit_ack=True, ack_type="OnDoExitLobby")
            await self.push_lobby_list(ws, msg_type="OnLobbyListChanged")
            return

        if msg_type == "DoExitLounge":
            session.in_lounge = False
            await self.send(ws, "OnDoExitLounge", {"success": True, "time": utc_now_str()})
            return

        if msg_type == "DoSendChat":
            await self.handle_chat(session, str(content))
            return

        if msg_type == "SetPlayerData":
            if isinstance(content, dict):
                key = str(content.get("key", "")).strip()
                if key:
                    self._apply_player_data(session, key, content.get("value"))
                    self.state.mark_dirty()
                    await self.broadcast_all(
                        "OnUserDataChanged",
                        self._build_user_change_payload(session, key, content.get("value")),
                    )
                    if session.current_lobby_id and session.current_lobby_id in self.state.lobbies:
                        await self.push_lobby_changed(self.state.lobbies[session.current_lobby_id])
            return

        if msg_type == "SetLobbyData":
            await self.handle_set_lobby_data(session, content if isinstance(content, dict) else {})
            return

        if msg_type == "DeleteLobbyData":
            lid = session.current_lobby_id
            if lid is not None and lid in self.state.lobbies and isinstance(content, dict):
                key = str(content.get("key", "")).strip()
                if key:
                    lobby = self.state.lobbies[lid]
                    lobby.metadata.pop(key, None)
                    self.state.mark_dirty()
                    await self.broadcast_all(
                        "OnLobbyDataChanged",
                        self._build_lobby_data_payload(lobby, key=key, deleted=True),
                    )
                    await self.push_lobby_changed(lobby)
            return

        if msg_type == "LockLobby":
            lid = session.current_lobby_id
            if lid is not None and lid in self.state.lobbies:
                lobby = self.state.lobbies[lid]
                if lobby.owner == session.user_id:
                    lobby.is_locked = bool(content)
                    self.state.mark_dirty()
                    await self.push_lobby_changed(lobby)
            return

        if msg_type == "SetLobbyMemberLimit":
            lid = session.current_lobby_id
            if lid is not None and lid in self.state.lobbies:
                lobby = self.state.lobbies[lid]
                if lobby.owner == session.user_id:
                    try:
                        lobby.member_limit = max(1, int(content))
                    except Exception:
                        pass
                    self.state.mark_dirty()
                    await self.push_lobby_changed(lobby)
            return

        if msg_type == "DoSetLobbyOwner":
            await self.handle_set_lobby_owner(session, str(content))
            return

        if msg_type == "DoKickUser":
            await self.handle_kick_user(session, str(content))
            return

        if msg_type == "DoUpdateWAN":
            await self.handle_update_wan(session, content)
            return

        if msg_type == "DoUpdateLAN":
            await self.handle_update_lan(session, content)
            return

        if msg_type == "DoP2PConnect":
            await self.handle_p2p_signal(session, content, route=False)
            return

        if msg_type == "DoP2PRoute":
            await self.handle_p2p_signal(session, content, route=True)
            return

        if msg_type in {"Ping", "DoPing"}:
            await self.send(
                ws,
                "OnHeartbeat",
                {"success": True, "serverTimeMs": int(time.time() * 1000), "time": utc_now_str()},
            )
            return

        if msg_type == "GetUserList":
            await self.send(ws, "OnGetUserList", {"users": self.state.serialize_users()})
            return

        if msg_type == "GetLogLevel":
            await self.send(
                ws,
                "OnGetLogLevel",
                {
                    "type": "OnGetLogLevel",
                    "time": utc_now_str(),
                    "success": True,
                    "level": [self.state.log_level_setting, self.state.log_level_setting],
                },
            )
            return

        if msg_type == "GetStatLog":
            await self.send(
                ws,
                "OnGetStatLog",
                {
                    "entries": [],
                    "generatedAt": utc_now_str(),
                    "stats": self.build_stat_log(),
                },
            )
            return

        if msg_type == "GetFullLog":
            await self.send(
                ws,
                "OnGetFullLog",
                {
                    "entries": list(self.state.stdout_history),
                    "generatedAt": utc_now_str(),
                    "stdout": {"history": list(self.state.stdout_history)},
                    "protocol": {"history": list(self.state.protocol_history)},
                },
            )
            return

        if msg_type == "SetLogLevel":
            if not session.is_admin:
                await self.send_error(ws, "SetLogLevel denied: admin only")
                return
            level = 0
            if isinstance(content, dict):
                try:
                    level = max(0, min(2, int(content.get("level", 0))))
                except Exception:
                    level = 0
            self.state.log_level_setting = level
            self.state.mark_dirty()
            await self.send(
                ws,
                "OnGetLogLevel",
                {
                    "type": "OnGetLogLevel",
                    "time": utc_now_str(),
                    "success": True,
                    "level": [level, level],
                },
            )
            return

        if msg_type == "AddRegex":
            if not session.is_admin:
                await self.send_error(ws, "AddRegex denied: admin only")
                return
            regex = ""
            if isinstance(content, dict):
                regex = str(content.get("regex", "")).strip()
            if not regex:
                await self.send_error(ws, "AddRegex ignored: missing regex")
                return
            entries = self.state.whitelist.setdefault("list", [])
            if regex not in entries:
                entries.append(regex)
                entries.sort()
                self.state.mark_dirty()
            await self.broadcast_all(
                "OnWhitelistUpdated",
                {
                    "type": "OnWhitelistUpdated",
                    "time": utc_now_str(),
                    "success": True,
                    "whitelist": self.state.whitelist,
                },
            )
            return

        if msg_type == "RemoveRegex":
            if not session.is_admin:
                await self.send_error(ws, "RemoveRegex denied: admin only")
                return
            regex = ""
            if isinstance(content, dict):
                regex = str(content.get("regex", "")).strip()
            if regex in self.state.whitelist.get("list", []):
                self.state.whitelist["list"] = [x for x in self.state.whitelist["list"] if x != regex]
                self.state.mark_dirty()
            await self.broadcast_all(
                "OnWhitelistUpdated",
                {
                    "type": "OnWhitelistUpdated",
                    "time": utc_now_str(),
                    "success": True,
                    "whitelist": self.state.whitelist,
                },
            )
            return

        if msg_type == "SetRelay":
            requested = self._coerce_bool(content.get("toggle")) if isinstance(content, dict) else self._coerce_bool(content)
            self.state.relay_requested = requested
            self.state.mark_dirty()
            if requested:
                self.state.record_stdout_entry(
                    "warn",
                    "Relay enable requested, but packet relay is not implemented in community failover mode",
                )
            await self.send(
                ws,
                "OnEnableRelayChanged",
                {"enabled": False, "requested": requested, "success": False, "time": utc_now_str()},
            )
            await self.send(
                ws,
                "OnRelayUpdated",
                {"enabled": False, "requested": requested, "success": False, "time": utc_now_str()},
            )
            return

        if msg_type == "SetTest":
            enabled = self._coerce_bool(content.get("toggle")) if isinstance(content, dict) else self._coerce_bool(content)
            self.state.test_mode = enabled
            self.state.mark_dirty()
            await self.send_stdout(ws, "info", f"Test mode set to {enabled}")
            return

        if msg_type == "TraceData":
            await self.send(
                ws,
                "OnGetFullLog",
                {
                    "entries": list(self.state.stdout_history),
                    "generatedAt": utc_now_str(),
                    "stdout": {"history": list(self.state.stdout_history)},
                    "protocol": {"history": list(self.state.protocol_history)},
                },
            )
            return

        # Keep unknown messages non-fatal for compatibility.
        await self.send_stdout(ws, "info", f"Unhandled message type: {msg_type}")

    async def unregister(self, ws: Any) -> None:
        session = self.state.sessions_by_ws.pop(ws, None)
        if not session:
            return

        try:
            await self.remove_from_lobby(session, send_exit_ack=False)
        finally:
            self.state.sessions_by_user_id.pop(session.user_id, None)
            self.state.record_stdout_entry("info", f"Disconnected {session.user_id} ({session.name})")
            self.log.info("Disconnected user_id=%s name=%s", session.user_id, session.name)

    async def handler(self, ws: Any) -> None:
        self.log.info("WS client connected: %s", ws.remote_address)
        try:
            async for message in ws:
                if isinstance(message, bytes):
                    self.state.record_protocol_event(
                        channel="ws",
                        direction="rx",
                        peer=self._ws_peer(ws),
                        msg_type="binary",
                        payload=message,
                    )
                    await self.send_error(ws, "Binary messages are not supported")
                    continue
                await self.route_message(ws, message)
        except ConnectionClosed:
            pass
        except Exception:
            self.log.exception("Unhandled websocket exception")
        finally:
            await self.unregister(ws)


def _http_json(status_code: int, payload: dict[str, Any]) -> bytes:
    reason = {200: "OK", 404: "Not Found", 405: "Method Not Allowed"}.get(status_code, "OK")
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    headers = [
        f"HTTP/1.1 {status_code} {reason}",
        "Content-Type: application/json; charset=utf-8",
        f"Content-Length: {len(body)}",
        "Connection: close",
        "",
        "",
    ]
    return "\r\n".join(headers).encode("utf-8") + body


def build_status_payload(
    *,
    state: ServerState,
    ws_server: BZRWebSocketServer,
    udp_protocol: BZRUDPProtocol,
    started_monotonic: float,
) -> dict[str, Any]:
    return {
        "ok": True,
        "time": utc_now_str(),
        "uptimeSeconds": int(time.monotonic() - started_monotonic),
        "state": state.metrics(),
        "websocket": ws_server.metrics(),
        "udp": {
            "packetsRxTotal": udp_protocol.rx_count,
            "packetsTxTotal": udp_protocol.tx_count,
        },
        "compatibility": {
            "observedOfficialHost": "battlezone98mp.webdev.rebellion.co.uk",
            "websocketLobby": True,
            "udpHandshakeShim": True,
            "directPeerSessionsExpected": True,
            "relayControlMessages": True,
            "relayTransportImplemented": False,
            "steamTicketValidation": False,
            "adminLogPayloadCompatibility": True,
        },
        "supportedMessages": {
            "client": SUPPORTED_CLIENT_MESSAGES,
            "server": SUPPORTED_SERVER_MESSAGES,
        },
        "recentProtocolEvents": state.protocol_history[-20:],
    }


async def serve_health_http(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    *,
    state: ServerState,
    ws_server: BZRWebSocketServer,
    udp_protocol: BZRUDPProtocol,
    started_monotonic: float,
) -> None:
    try:
        first = await asyncio.wait_for(reader.readline(), timeout=2.0)
        line = first.decode("utf-8", errors="replace").strip()
        parts = line.split()
        if len(parts) < 2:
            writer.write(_http_json(404, {"ok": False, "error": "Invalid request"}))
            await writer.drain()
            return
        method, path = parts[0], parts[1]

        # Drain remaining headers.
        while True:
            h = await asyncio.wait_for(reader.readline(), timeout=2.0)
            if not h or h in (b"\r\n", b"\n"):
                break

        if method != "GET":
            writer.write(_http_json(405, {"ok": False, "error": "Only GET supported"}))
            await writer.drain()
            return

        if path not in ("/health", "/status", "/captures", "/protocol"):
            writer.write(_http_json(404, {"ok": False, "error": "Unknown path"}))
            await writer.drain()
            return

        if path in ("/health", "/status"):
            payload = build_status_payload(
                state=state,
                ws_server=ws_server,
                udp_protocol=udp_protocol,
                started_monotonic=started_monotonic,
            )
        elif path == "/captures":
            payload = {
                "ok": True,
                "time": utc_now_str(),
                "protocol": {"history": list(state.protocol_history)},
                "stdout": {"history": list(state.stdout_history)},
            }
        else:
            payload = {
                "ok": True,
                "time": utc_now_str(),
                "observedOfficialHost": "battlezone98mp.webdev.rebellion.co.uk",
                "cutover": {
                    "dnsOrHostsOverrideRequired": True,
                    "hostname": "battlezone98mp.webdev.rebellion.co.uk",
                    "websocketPort": DEFAULT_WS_PORT,
                    "udpProbePort": DEFAULT_UDP_PORT,
                    "relayPortObserved": 1339,
                },
                "supportedMessages": {
                    "client": SUPPORTED_CLIENT_MESSAGES,
                    "server": SUPPORTED_SERVER_MESSAGES,
                },
            }
        writer.write(_http_json(200, payload))
        await writer.drain()
    except Exception:
        with contextlib.suppress(Exception):
            writer.write(_http_json(404, {"ok": False, "error": "Request failed"}))
            await writer.drain()
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


async def autosave_loop(
    stop_event: asyncio.Event,
    state: ServerState,
    interval_seconds: int,
    logger: logging.Logger,
) -> None:
    interval = max(1, int(interval_seconds))
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            state.save_state()
        except Exception:
            logger.exception("Autosave loop error")
            await asyncio.sleep(1)


async def run_server(args: argparse.Namespace) -> None:
    global websockets, ConnectionClosed
    if websockets is None:
        try:
            import websockets as _websockets
            from websockets.exceptions import ConnectionClosed as _connection_closed
        except Exception as exc:  # pragma: no cover - runtime import guard
            raise SystemExit(
                "Missing dependency 'websockets'. Install with: pip install websockets"
            ) from exc
        websockets = _websockets
        ConnectionClosed = _connection_closed

    log_level = getattr(logging, args.log_level.upper(), logging.INFO)
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logger = logging.getLogger("bzr-dedicated")

    admin_ids = {x.strip() for x in args.admin_ids.split(",") if x.strip()}
    admin_key = args.admin_key or os.getenv("BZR_ADMIN_KEY", "")
    state = ServerState(
        logger.getChild("state"),
        state_file=args.state_file,
        admin_key=admin_key,
        admin_ids=admin_ids,
    )
    state.load_state()

    ws_server = BZRWebSocketServer(
        state,
        logger.getChild("ws"),
        motd=args.motd,
        max_lobbies=args.max_lobbies,
        retain_empty_lobbies=args.retain_empty_lobbies,
    )

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    started_monotonic = time.monotonic()

    def _request_stop() -> None:
        if not stop_event.is_set():
            logger.info("Shutdown requested.")
            stop_event.set()

    for signame in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, signame, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, _request_stop)
        except (NotImplementedError, RuntimeError):
            # Windows or non-main thread.
            pass

    udp_transport, udp_protocol = await loop.create_datagram_endpoint(
        lambda: BZRUDPProtocol(logger.getChild("udp"), state),
        local_addr=(args.udp_host, args.udp_port),
    )

    health_server = None
    if args.health_port > 0:
        health_server = await asyncio.start_server(
            lambda r, w: serve_health_http(
                r,
                w,
                state=state,
                ws_server=ws_server,
                udp_protocol=udp_protocol,
                started_monotonic=started_monotonic,
            ),
            host=args.health_host,
            port=args.health_port,
        )

    autosave_task = asyncio.create_task(
        autosave_loop(stop_event, state, args.autosave_seconds, logger.getChild("autosave"))
    )

    async with websockets.serve(
        ws_server.handler,
        args.ws_host,
        args.ws_port,
        ping_interval=20,
        ping_timeout=30,
        max_size=2**20,
    ):
        logger.info("WebSocket server listening on ws://%s:%d", args.ws_host, args.ws_port)
        logger.info("UDP shim listening on %s:%d", args.udp_host, args.udp_port)
        if health_server:
            logger.info("Health endpoint listening on http://%s:%d/health", args.health_host, args.health_port)
        logger.info("Press Ctrl+C to stop.")
        await stop_event.wait()

    state.save_state(force=True)
    autosave_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await autosave_task

    if health_server:
        health_server.close()
        await health_server.wait_closed()

    udp_transport.close()
    await asyncio.sleep(0.05)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Battlezone 98 Redux dedicated-server compatibility shim")
    parser.add_argument("--ws-host", default=DEFAULT_WS_HOST, help=f"WebSocket bind host (default: {DEFAULT_WS_HOST})")
    parser.add_argument("--ws-port", type=int, default=DEFAULT_WS_PORT, help=f"WebSocket bind port (default: {DEFAULT_WS_PORT})")
    parser.add_argument("--udp-host", default=DEFAULT_UDP_HOST, help=f"UDP bind host (default: {DEFAULT_UDP_HOST})")
    parser.add_argument("--udp-port", type=int, default=DEFAULT_UDP_PORT, help=f"UDP bind port (default: {DEFAULT_UDP_PORT})")
    parser.add_argument("--health-host", default=DEFAULT_HEALTH_HOST, help=f"Health HTTP bind host (default: {DEFAULT_HEALTH_HOST})")
    parser.add_argument("--health-port", type=int, default=DEFAULT_HEALTH_PORT, help=f"Health HTTP bind port, 0 to disable (default: {DEFAULT_HEALTH_PORT})")
    parser.add_argument("--state-file", default=DEFAULT_STATE_FILE, help=f"Path to persistent state JSON (default: {DEFAULT_STATE_FILE})")
    parser.add_argument("--autosave-seconds", type=int, default=15, help="State autosave interval in seconds")
    parser.add_argument("--motd", default="", help="Optional message-of-the-day sent after authorization")
    parser.add_argument("--admin-key", default="", help="Admin key accepted during Authorization content.key")
    parser.add_argument("--admin-ids", default="", help="Comma-separated user IDs that are always treated as admins")
    parser.add_argument("--max-lobbies", type=int, default=500, help="Maximum simultaneous lobbies")
    parser.add_argument(
        "--retain-empty-lobbies",
        action="store_true",
        help="Do not delete lobbies when last user leaves (useful with persistence)",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    try:
        asyncio.run(run_server(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
