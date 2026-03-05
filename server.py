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


def utc_now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


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
        self._dirty = False

    def allocate_lobby_id(self) -> int:
        lid = self.next_lobby_id
        self.next_lobby_id += 1
        self.mark_dirty()
        return lid

    def build_user_snapshot(self, s: UserSession) -> dict[str, Any]:
        snapshot = {
            "id": s.user_id,
            "name": s.name,
            "playerName": s.player_name,
            "authType": s.authtype,
            "clientVersion": s.client_version,
        }
        if s.wan_address:
            snapshot["ipAddress"] = s.wan_address.split(":")[0]
        if s.lan_addresses:
            snapshot["lanAddresses"] = s.lan_addresses
        if s.extra_data:
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
            "stateDirty": self._dirty,
        }

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
        self.logger.debug("UDP RX <- %s:%d | %s", addr[0], addr[1], hex_data)

        reply = self._build_reply(data)
        if reply and self.transport:
            self.transport.sendto(reply, addr)
            self.tx_count += 1
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

    async def send(self, ws: Any, msg_type: str, data: Any) -> None:
        payload = {"type": msg_type, "data": data}
        await ws.send(json.dumps(payload, separators=(",", ":")))

    async def send_error(self, ws: Any, message: str) -> None:
        await self.send(
            ws,
            "OnStdoutLogEntry",
            {"time": utc_now_str(), "level": "error", "message": message},
        )

    async def broadcast_all(self, msg_type: str, data: Any) -> None:
        if not self.state.sessions_by_ws:
            return
        payload = json.dumps({"type": msg_type, "data": data}, separators=(",", ":"))
        coros = [session.ws.send(payload) for session in self.state.sessions_by_ws.values()]
        await asyncio.gather(*coros, return_exceptions=True)

    async def broadcast_lobby(self, lobby: LobbyState, msg_type: str, data: Any) -> None:
        payload = json.dumps({"type": msg_type, "data": data}, separators=(",", ":"))
        targets = []
        for uid in lobby.users:
            session = self.state.sessions_by_user_id.get(uid)
            if session and session.ws:
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

    async def remove_from_lobby(
        self,
        session: UserSession,
        *,
        send_exit_ack: bool = False,
    ) -> None:
        lid = session.current_lobby_id
        if lid is None:
            if send_exit_ack:
                await self.send(session.ws, "OnDoExitLobby", {"success": False, "reason": "Not in lobby"})
            return

        lobby = self.state.lobbies.get(lid)
        session.current_lobby_id = None
        session.in_lounge = True
        if not lobby:
            if send_exit_ack:
                await self.send(session.ws, "OnDoExitLobby", {"success": True, "id": lid})
            return

        if session.user_id in lobby.users:
            del lobby.users[session.user_id]
            self.state.mark_dirty()
            await self.broadcast_all(
                "OnLobbyMemberListChanged",
                {
                    "lobbyId": lid,
                    "id": session.user_id,
                    "member": session.name,
                    "removed": True,
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
            await self.send(session.ws, "OnDoExitLobby", {"success": True, "id": lid})
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

        await self.send(
            ws,
            "OnAuthorization",
            {
                "type": "OnAuthorization",
                "time": utc_now_str(),
                "success": True,
                "id": session.user_id,
                "name": session.name,
                "isAdmin": session.is_admin,
                "isAuth": True,
                "isInLounge": session.in_lounge,
                "wanAddress": session.wan_address,
                "clientVersion": session.client_version,
                "lanAddresses": session.lan_addresses,
            },
        )
        await self.send(
            ws,
            "OnWhitelistUpdated",
            {
                "type": "OnWhitelistUpdated",
                "time": utc_now_str(),
                "success": True,
                "whitelist": DEFAULT_WHITELIST,
            },
        )
        await self.send(
            ws,
            "OnRelayUpdated",
            {
                "type": "OnRelayUpdated",
                "time": utc_now_str(),
                "enabled": False,
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

        await self.send(session.ws, "OnLobbyCreated", {"success": True, "id": lid, "time": utc_now_str()})
        await self.push_lobby_changed(lobby)
        await self.push_full_lobby_list()

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

            await self.broadcast_all(
                "OnLobbyMemberListChanged",
                {
                    "lobbyId": lid,
                    "id": session.user_id,
                    "member": session.name,
                    "removed": False,
                },
            )

        await self.send(session.ws, "OnLobbyJoined", {"success": True, "id": lid, "time": utc_now_str()})
        await self.push_lobby_changed(lobby)
        await self.push_full_lobby_list()

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
        await self.broadcast_all("OnLobbyDataChanged", {"id": lid, "key": key, "value": value})
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
        await self.send(
            target_session.ws,
            "OnStdoutLogEntry",
            {"time": utc_now_str(), "level": "warn", "message": "You were kicked from the lobby"},
        )

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

        if msg_type == "Authorization":
            await self.handle_authorization(ws, content if isinstance(content, dict) else {})
            return

        session = self.state.sessions_by_ws.get(ws)
        if not session:
            await self.send_error(ws, "Authorization required")
            return

        if msg_type == "DoEnterLounge":
            session.in_lounge = bool(content)
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

        if msg_type == "DoJoinLobby":
            await self.handle_join_lobby(session, content if isinstance(content, dict) else {})
            return

        if msg_type in {"DoExitLobby", "DoExitLounge"}:
            await self.remove_from_lobby(session, send_exit_ack=True)
            await self.push_lobby_list(ws, msg_type="OnLobbyListChanged")
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
                        {"id": session.user_id, "key": key, "value": content.get("value")},
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
                    self.state.lobbies[lid].metadata.pop(key, None)
                    self.state.mark_dirty()
                    await self.broadcast_all("OnLobbyDataChanged", {"id": lid, "key": key, "deleted": True})
                    await self.push_lobby_changed(self.state.lobbies[lid])
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

        if msg_type == "GetStatLog":
            await self.send(ws, "OnGetStatLog", {"entries": [], "generatedAt": utc_now_str()})
            return

        if msg_type == "GetFullLog":
            await self.send(ws, "OnGetFullLog", {"entries": [], "generatedAt": utc_now_str()})
            return

        # Keep unknown messages non-fatal for compatibility.
        await self.send(
            ws,
            "OnStdoutLogEntry",
            {
                "time": utc_now_str(),
                "level": "info",
                "message": f"Unhandled message type: {msg_type}",
            },
        )

    async def unregister(self, ws: Any) -> None:
        session = self.state.sessions_by_ws.pop(ws, None)
        if not session:
            return

        try:
            await self.remove_from_lobby(session, send_exit_ack=False)
        finally:
            self.state.sessions_by_user_id.pop(session.user_id, None)
            self.log.info("Disconnected user_id=%s name=%s", session.user_id, session.name)

    async def handler(self, ws: Any) -> None:
        self.log.info("WS client connected: %s", ws.remote_address)
        try:
            async for message in ws:
                if isinstance(message, bytes):
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

        if path not in ("/health", "/status"):
            writer.write(_http_json(404, {"ok": False, "error": "Unknown path"}))
            await writer.drain()
            return

        payload = {
            "ok": True,
            "time": utc_now_str(),
            "uptimeSeconds": int(time.monotonic() - started_monotonic),
            "state": state.metrics(),
            "websocket": ws_server.metrics(),
            "udp": {
                "packetsRxTotal": udp_protocol.rx_count,
                "packetsTxTotal": udp_protocol.tx_count,
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
