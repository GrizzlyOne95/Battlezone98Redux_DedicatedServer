#!/usr/bin/env python3
"""Generate protocol_contract.json -- the canonical BZRNet interoperability
truth table. Evidence keys:

- stock-binary   : decomp of battlezone98redux.exe 2.2.301 GOG
- official-web-js: scripts/bzadmin.js + bzdatagrids.js served by the official host
- official-live  : read-only observation against the live service (2026-08-24)
- lobby-monitor  : Battlezone_LobbyMonitor bzr_monitor.py
- gamewatcher    : BZ1-GameWatcher production parser + fixture corpus
- dedicated-server: this repository's server.py

Confidence levels: CONFIRMED / HIGH / MEDIUM / LOW / UNKNOWN.
"""

from __future__ import annotations

import json
from pathlib import Path

OUT = Path(__file__).with_name("protocol_contract.json")

DOC = {
    "$schema_note": (
        "Canonical BZRNet interoperability contract: one auditable truth table "
        "per message record. Regenerate derived docs with "
        "render_protocol_contract.py."
    ),
    "last_live_verification": "2026-08-24",
    "envelope": {
        "client_to_server": {"type": "string", "content": "any"},
        "server_to_client": {
            "type": "string",
            "data": "object",
            "data_type_duplicate": (
                "official duplicates the message name inside data.type; stock "
                "ignores it after dispatch"
            ),
        },
        "notes": [
            "Stock dispatcher hard-requires top-level type first; missing type or "
            "failed parse logs 'JSON Message Not understood' and resets the "
            "connection (stock-binary).",
            "Binary WS frames are rejected upstream ('Binary Payload not "
            "supported'); everything is text JSON (stock-binary).",
            "OnAuthorization must be the first inbound message or stock aborts "
            "('OnAuthorization must be first message') (stock-binary).",
            "Official payloads ride under 'data'; GameWatcher also accepts "
            "'content' because the service has emitted both historically "
            "(gamewatcher BZ98ChatObserver.cs).",
            "Official time format inside data: 'YYYY-MM-DD HH:MM:SS' UTC; "
            "lobby createdTime is ISO-8601 with ms and Z.",
        ],
    },
    "udp_1338": {
        "purpose": "public endpoint discovery probe",
        "request": (
            "any datagram; canonical client probe is 18 bytes: flags=0x40, "
            "kind=0x02, 8-byte big-endian unix-ms at offset 2, reliable seq u32 "
            "at 10, peer ack u32 at 14"
        ),
        "response": (
            "verbatim echo of request payload + 7-byte trailer: 0x04, observed "
            "IPv4 network order, observed UDP port big-endian u16; an EMPTY "
            "datagram yields a trailer-only 7-byte reply (official-live "
            "2026-08-24)"
        ),
        "official_host": "battlezone98mp.webdev.rebellion.co.uk -> 68.183.35.188 (2026-08-24)",
        "client_cadence": "stock probes ~10s after connect then every ~30s (raw pcaps 2026-03-21)",
        "confidence": "CONFIRMED",
    },
    "udp_1339": {
        "purpose": "relay / NAT fallback transport",
        "association": (
            "UNKNOWN - allocation envelope not recovered. Signaling context: "
            "/iprelay sets global 0x00946708; HandleP2PConnect state 5 sends "
            "DoP2PRoute; OnP2PRoute success stores an endpoint pair and enters "
            "state 6 ('Starting RELAY Connect'); failure resets ('Failed RELAY "
            "Connect'). Relayed sends share the seq/ack packet format per "
            "'BZRNet P2P REL Sent Packet (%u,%u)' strings."
        ),
        "confidence": "LOW",
    },
}

R = []


def rec(**kw):
    R.append(kw)


rec(
    message="Authorization", direction="client->server",
    content_shape={
        "authtype": '"steam"|"gog"|"web"', "key": "string", "id": 'string ("0" for web)',
        "apiVer": "string", "name?": "string", "playerName?": "string",
        "realname?": "string (steam)", "steamAppTicket?": "string (steam)",
        "clientVersion?": "string", "inLounge?": "bool",
        "wanAddress?": "string", "lanAddresses?": "[string]",
    },
    required_fields=["authtype"],
    stock_binary="builder 0x006C6E60 shared with DoUpdateWAN/DoUpdateLAN; steam branch adds steamAppTicket+realname+authtype=steam",
    stock_live_capture=("steam 2.2.301 sends {apiVer:'3.6', authtype:'steam', clientVersion:'2.2.301', "
                        "id:'S<steam64>', inLounge:false, lanAddresses:[~8 entries], name, realname, "
                        "steamAppTicket, wanAddress}; the CLIENT supplies its S-id and the server keeps it"),

    official_web_js="{authtype:'web', key:<query>, id:'0', apiVer:'0.0'}",
    official_live="accepted verbatim; assigned web id B100xxxx",
    lobby_monitor="{authtype:'web', key, id:'0', apiVer:'0.0', name?, playerName?, clientVersion}",
    gamewatcher="{authtype:'web', key:'', id:'0', apiVer:'0.0'} then SetPlayerData identity",
    dedicated_server="accepts dict; honors requested id or generates U<uuid12>; admin via key/admin-ids",
    expected_responses=["OnAuthorization", "OnWhitelistUpdated"],
    flags={"stock": True, "official_web": True, "official_live": True,
           "lobby_monitor": True, "gamewatcher": True, "dedicated_server": True},
    confidence="CONFIRMED",
)

rec(
    message="OnAuthorization", direction="server->client",
    content_shape={"type": "dup", "time": "str", "success": "bool", "isAdmin": "bool",
                   "id": "server-assigned userId", "isBB": "bool"},
    required_fields=["success", "id"],
    stock_binary="dispatcher reads time+success; must be first inbound message",
    official_live='{"type","time","success":true,"isAdmin":false,"id":"B1001097","isBB":false}',
    official_web_js="reads data.isAdmin (admin tabs) and data.isBB (test tab)",
    dedicated_server="superset (name/authType/user/etc.); stock ignores extras",
    flags={"stock": True, "official_web": True, "official_live": True, "dedicated_server": True},
    confidence="CONFIRMED",
)

rec(
    message="OnWhitelistUpdated", direction="server->client",
    content_shape={"whitelist": {"list": "[glob]", "major": "int", "minor": "int"}},
    stock_binary="logs 'Whitelist updated to version : %ld.%ld'",
    official_live="immediately after OnAuthorization; major=8 minor=0; 12 entries *.wav..*mod.material",
    official_web_js="reads whitelist.list/.major/.minor",
    dedicated_server="DEFAULT_WHITELIST equals the observed official list",
    flags={"stock": True, "official_web": True, "official_live": True, "dedicated_server": True},
    confidence="CONFIRMED",
)

rec(
    message="DoEnterLounge", direction="client->server",
    content_shape="boolean true (live-stock capture 2026-03-21 pair + all known clients send scalar true; the earlier 'numeric payload' hypothesis is disproven by raw pcap)",
    stock_binary="builder 0x006C6320; live stock pcap shows content:true",
    stock_live_capture="boolean true (game1.pcapng frame 9585 region / Kat_PC.pcapng)",
    official_web_js="content:true",
    official_live="accepted; replies OnLobbyListChanged only - NO OnDoEnterLounge ack exists upstream",
    lobby_monitor="content:true", gamewatcher="content:true",
    dedicated_server="coerces scalar; acks OnDoEnterLounge then pushes list (deliberate superset; smoke test depends on ack)",
    expected_responses=["OnLobbyListChanged"],
    flags={"stock": True, "official_web": True, "official_live": True,
           "lobby_monitor": True, "gamewatcher": True, "dedicated_server": True},
    confidence="CONFIRMED",
)

rec(
    message="DoExitLounge", direction="client->server",
    content_shape="empty/scalar",
    stock_binary="builder 0x006C65B0",
    dedicated_server="acks OnDoExitLounge",
    flags={"stock": True, "dedicated_server": True}, confidence="HIGH",
)

rec(
    message="GetLobbyList", direction="client->server",
    content_shape="true",
    lobby_monitor="sends right after auth",
    dedicated_server="replies OnLobbyList AND OnGetLobbyList (legacy tolerance)",
    flags={"lobby_monitor": True, "dedicated_server": True}, confidence="MEDIUM",
)

rec(
    message="CreateLobby", direction="client->server",
    content_shape={"name?": "~chat~pub~~Name", "memberLimit": "int (20000 typical)",
                   "isPrivate?": "bool", "password?": "string"},
    stock_binary="builder 0x006C3FB0 memberLimit(number)/isPrivate(bool)/password(string) setters",
    official_web_js="{name, memberLimit:20000} only",
    lobby_monitor="{name:'~chat~pub~~X', isPrivate:false, memberLimit:20000, password:''}",
    dedicated_server="normalizes chat prefix; seeds metadata name/clientVersion/GameVersion/gameType/gameSettings/ready",
    flags={"stock": True, "official_web": True, "lobby_monitor": True, "dedicated_server": True},
    confidence="CONFIRMED",
)

rec(
    message="CreateGame", direction="client->server",
    content_shape={"memberLimit": "int", "isPrivate": "bool", "password": "string"},
    stock_binary="builder 0x006C4350 minimal memberLimit/isPrivate/password (no name field in builder)",
    stock_live_capture='{"isPrivate":true,"memberLimit":8,"password":"..."} exactly; friendly name arrives afterwards via SetLobbyData key=name',
    dedicated_server="wraps CreateLobby with ~game~pub~~ name + nextmid/gameType/gameSettings defaults",
    flags={"stock": True, "dedicated_server": True}, confidence="CONFIRMED",
)

rec(
    message="DoJoinLobby", direction="client->server",
    content_shape={"id": "number", "password": "string"},
    stock_binary="builder 0x006C46B0: content object, key DAT_00897b9c='id' via number setter FUN_006dc0f0 + 'password' string setter",
    official_live=("scalar id -> OnFailure 'DoJoinLobby failure bad content data type'; "
                   "object without password -> same failure; "
                   "{'id': 2848, 'password': ''} accepted once identity was set"),
    official_web_js="sends bare numeric id (broken against current official service)",
    lobby_monitor="{'id': int-or-str, 'password': ''}",
    dedicated_server="accepts stock shape + liberal variants; bool content rejected with official wording",
    stock_live_capture="both host and joiner send {'id': <int>, 'password': ''} ; stock ALWAYS joins ~chat~pub~~default right after entering the lounge, exits it, then creates/joins the game lobby",
    expected_responses=["OnLobbyJoined", "OnFailure"],
    flags={"stock": True, "official_live": True, "lobby_monitor": True, "dedicated_server": True},
    confidence="CONFIRMED",
)

rec(
    message="OnLobbyJoined", direction="server->client",
    content_shape={"type": "dup", "time": "str", "success": "bool", "id": "lobbyId",
                   "reason": "str", "reasonCode": "int"},
    stock_binary="dispatcher reads reasonCode (+ lobby object fields tolerated)",
    official_live=("success {success:true,id,reason:'',reasonCode:0}; failure "
                   "{success:false,id,reason:'Join Lobby Failed, Lobby no longer exists',reasonCode:4}"),
    reason_codes={
        "0": "success CONFIRMED",
        "4": "lobby no longer exists CONFIRMED (official-live 2026-08-24)",
        "1,2,3,5,6": "provisional community mapping (missing id / invalid id / bad password / locked / full)",
    },
    nested_payload_note=(
        "RESOLVED: raw paired stock pcaps (2026-03-21) plus live observation show the success "
        "ack is FLAT {type,time,success,id,reason:'',reasonCode:0}; nested lobby/user structures "
        "are NOT sent upstream. They remain tolerated input only."
    ),
    stock_live_capture="flat ack confirmed in both game1.pcapng and Kat_PC.pcapng",
    flags={"stock": True, "official_live": True, "dedicated_server": True},
    confidence="CONFIRMED",
)

rec(
    message="OnFailure", direction="server->client",
    content_shape={"type": "dup", "time": "str", "success": False, "reason": "str"},
    official_live="'DoJoinLobby failure bad content data type'; NO reasonCode field present",
    dedicated_server="emitted for malformed DoJoinLobby content",
    flags={"official_live": True, "dedicated_server": True}, confidence="CONFIRMED",
)

LOBBY_SNAPSHOT = {
    "id": "int", "userCount": "int", "memberLimit": "int", "isPrivate": "bool",
    "isLocked": "bool", "isChat": "bool", "owner": "userId string or -1 when ownerless",
    "metadata": {
        "name": "~type~vis~pw~~friendly", "gameType": "'0'|'1' validity marker",
        "gameSettings": "*-separated 13-field tuple", "GameVersion": "string",
        "userCount": "string echo", "userPack?": "padded string (bridge users)",
    },
    "clientVersion": "string ('' for bridge chat lobbies)",
    "createdTime": "ISO-8601 Z ms",
    "users": {"<userId>": "user snapshot"},
}
USER_SNAPSHOT = {
    "id": "string", "name": "string", "authType": "'steam'|'gog'|'web'",
    "clientVersion": "string", "ipAddress": "string", "wanAddress": "string",
    "lanAddresses": ["string"], "isInLounge": "bool", "isAdmin": "bool",
    "isAuth": "bool", "isBB": "bool", "isTest": "bool",
    "lobby": "int (-1 while in lounge)", "metadata": {},
}

rec(
    message="OnLobbyListChanged", direction="server->client broadcast",
    content_shape={"lobbies": {"<lobbyId string>": "lobby snapshot"},
                   "totalUsers": "int (upstream can wrap negative; we report true count)"},
    official_live="reply to DoEnterLounge; spontaneous on lobby changes while observing",
    official_web_js="handled (full re-populate)",
    lobby_monitor="handled (wholesale replace)", gamewatcher="handled (store replace)",
    dedicated_server="broadcast after create/join/leave/remove + pushed on enter lounge",
    lobby_snapshot=LOBBY_SNAPSHOT, user_snapshot=USER_SNAPSHOT,
    flags={"official_live": True, "official_web": True, "lobby_monitor": True,
           "gamewatcher": True, "dedicated_server": True},
    confidence="CONFIRMED",
)

rec(
    message="OnLobbyChanged", direction="server->client broadcast",
    content_shape={"lobbies": {"<lobbyId>": "lobby snapshot"}, "totalUsers": "int"},
    official_live=("primary join/leave/metadata signal; official emits REDUNDANT multi-broadcasts "
                   "per membership change (2-4 copies observed)"),
    lobby_snapshot=LOBBY_SNAPSHOT, user_snapshot=USER_SNAPSHOT,
    official_web_js="handled (merge single)",
    lobby_monitor="handled (merge)", gamewatcher="handled (add-or-update)",
    flags={"official_live": True, "official_web": True, "lobby_monitor": True,
           "gamewatcher": True, "dedicated_server": True},
    confidence="CONFIRMED",
)

rec(
    message="OnLobbyMemberListChanged", direction="server->client broadcast",
    content_shape={"member": "AFFECTED USER ID (not display name!)", "lobbyId": "int",
                   "removed": "bool"},
    official_live='{"member":"B1000006" (joiner id),"lobbyId":1000,"removed":false} between OnLobbyChanged and OnLobbyJoined',
    stock_live_capture="paired pcaps corroborate member=userId semantics upstream",
    stock_binary="dispatcher reads member/removed/lobbyId/users",
    lobby_monitor="reads member as DISPLAY NAME (divergence; tolerated)",
    dedicated_server="member=userId since 2026-08-24 fix; adds memberName/users supersets",
    flags={"stock": True, "official_live": True, "lobby_monitor": True, "dedicated_server": True},
    confidence="CONFIRMED",
)

rec(
    message="OnLobbyRemoved", direction="server->client broadcast",
    content_shape={"id": "lobbyId"},
    official_web_js="reads data.id and drops the row",
    lobby_monitor="reads id (string-coerced compare)",
    dedicated_server="{'id': lid,'time':...} broadcast when empty lobby removed",
    flags={"official_web": True, "lobby_monitor": True, "dedicated_server": True},
    confidence="HIGH",
)

rec(
    message="OnLobbyCreated", direction="server->client",
    content_shape={"success": "bool", "id": "lobbyId", "reason?": "str"},
    lobby_monitor="flat success/id/reason; triggers metadata seeding",
    dedicated_server="emitted to creator before OnLobbyChanged/full list",
    flags={"lobby_monitor": True, "dedicated_server": True}, confidence="MEDIUM",
)

rec(
    message="DoExitLobby", direction="client->server",
    content_shape="numeric lobby id scalar (admin JS) or object; officially the numeric scalar IS accepted for exit",
    stock_binary="builder 0x006C5760",
    stock_live_capture="stock sends bare int lobby id ('content':1004); reply is OnLobbyRemoved + OnLobbyListChanged, no explicit exit ack observed",
    official_live="numeric scalar accepted; removal broadcast via OnLobbyChanged; no explicit ack observed",
    lobby_monitor="sends current_lobby_id int",
    dedicated_server="acks OnDoExitLobby {success,id} then broadcasts",
    flags={"stock": True, "official_live": True, "lobby_monitor": True, "dedicated_server": True},
    confidence="HIGH",
)

rec(
    message="DoSendChat", direction="client->server",
    content_shape="plain string; whisper convention '/t <name> <text>'",
    stock_binary="builder 0x006C3D50",
    official_web_js="content:msg",
    lobby_monitor="string; /t whisper; [Discord] bridge prefix",
    dedicated_server="/t whisper handling + lounge/lobby fanout",
    expected_responses=["OnChatMessage"],
    flags={"stock": True, "official_web": True, "lobby_monitor": True, "dedicated_server": True},
    confidence="CONFIRMED",
)

rec(
    message="OnChatMessage", direction="server->client broadcast",
    content_shape={"author": "display name", "speakerId": "userId", "text": "string",
                   "time": "string", "color?": "(admin page only)"},
    stock_binary="dispatcher reads text/speakerId/lobbyId/isChat",
    stock_live_capture="{type,time,success,text,lobbyId,speakerId}; 'author' absent in stock-captured chat; text may be engine protocol strings like '~AT~S<sid>~2~2'",
    official_web_js="reads author/text/color/time",
    lobby_monitor="author||speakerId fallback",
    gamewatcher="speakerId/author/text/time",
    dedicated_server="{author,speakerId,text,lobbyId,time[,system,private]}",
    flags={"stock": True, "official_web": True, "lobby_monitor": True,
           "gamewatcher": True, "dedicated_server": True},
    confidence="CONFIRMED",
)

rec(
    message="SetPlayerData", direction="client->server",
    content_shape={"key": "name|playerName|clientVersion|authType|...", "value": "any"},
    stock_binary="builder 0x006C4F70",
    gamewatcher="sets name/playerName/clientVersion/authType after auth",
    lobby_monitor="same four keys",
    official_live="no direct response; reflected later via user snapshots",
    dedicated_server="applies + broadcasts OnUserDataChanged",
    flags={"stock": True, "official_live": True, "lobby_monitor": True,
           "gamewatcher": True, "dedicated_server": True},
    confidence="CONFIRMED",
)

rec(
    message="OnUserDataChanged", direction="server->client broadcast",
    content_shape={"changedLobby": "int (+ optional user snapshot supersets)"},
    stock_binary="dispatcher reads user object fields",
    stock_live_capture="upstream emits {type,time,success,member:<userId>,lobbyId:<int>} minimally; richer snapshots are community supersets",
    lobby_monitor="reads only member (log line)",
    dedicated_server="{id,userId,member,memberName,key,value,changedLobby,user,users}",
    flags={"stock": True, "lobby_monitor": True, "dedicated_server": True},
    confidence="MEDIUM",
)

rec(
    message="SetLobbyData", direction="client->server",
    content_shape={"key": "string", "value": "any"},
    stock_binary="builder 0x006C4C50",
    official_web_js="{key:'name', value:msg}",
    lobby_monitor="seeds clientVersion/GameVersion/gameType/gameSettings/name",
    dedicated_server="owner-only enforced; broadcasts OnLobbyDataChanged + OnLobbyChanged",
    flags={"stock": True, "official_web": True, "lobby_monitor": True, "dedicated_server": True},
    confidence="CONFIRMED",
)

rec(
    message="OnLobbyDataChanged", direction="server->client broadcast",
    content_shape={"changedLobby": "lobbyId (minimal upstream)"},
    stock_binary="dispatcher reads changedLobby + lobby object",
    official_live="{type,time,success,changedLobby} only",
    dedicated_server="adds key/value/deleted/lobby/lobbies supersets",
    flags={"stock": True, "official_live": True, "lobby_monitor": True, "dedicated_server": True},
    confidence="CONFIRMED",
)

rec(
    message="DeleteLobbyData", direction="client->server",
    content_shape={"key": "string"},
    stock_binary="builder 0x006C6040",
    dedicated_server="removes metadata key; broadcasts deleted=true",
    flags={"stock": True, "dedicated_server": True}, confidence="MEDIUM",
)

rec(
    message="LockLobby", direction="client->server",
    content_shape="boolean scalar",
    stock_binary="builder 0x006C5290",
    official_web_js="content:true/false",
    dedicated_server="owner-only; is_locked update + OnLobbyChanged",
    flags={"stock": True, "official_web": True, "dedicated_server": True},
    confidence="HIGH",
)

rec(
    message="SetLobbyMemberLimit", direction="client->server",
    content_shape="numeric scalar",
    stock_binary="builder 0x006C54F0",
    official_web_js="content:parseInt(msg)",
    dedicated_server="owner-only; max(1,int)",
    flags={"stock": True, "official_web": True, "dedicated_server": True},
    confidence="HIGH",
)

rec(
    message="DoSetLobbyOwner", direction="client->server",
    content_shape="target userId scalar",
    stock_binary="builder 0x006C49F0",
    official_web_js="content:id",
    dedicated_server="owner-only; target must be member; OnLobbyChanged",
    flags={"stock": True, "official_web": True, "dedicated_server": True},
    confidence="HIGH",
)

rec(
    message="DoKickUser", direction="client->server",
    content_shape="target userId scalar",
    stock_binary="builder inferred (Inferred evidence level in shim registry)",
    lobby_monitor="manual kick + auto-ban path",
    dedicated_server="owner-only; removes target; exit ack + stdout warn",
    flags={"lobby_monitor": True, "dedicated_server": True}, confidence="MEDIUM",
)

rec(
    message="DoUpdateWAN", direction="client->server",
    content_shape={"wanAddress": "host:port string"} ,
    stock_binary="shares builder 0x006C6E60; emitted after UDP probe reports public endpoint (~30s cadence)",
    dedicated_server="broadcasts OnWANUpdated + refreshes lobby snapshots",
    flags={"stock": True, "dedicated_server": True}, confidence="HIGH",
)

rec(
    message="DoUpdateLAN", direction="client->server",
    content_shape={"lanAddresses": "[host:port]"},
    stock_binary="shares builder 0x006C6E60",
    dedicated_server="broadcasts OnLANUpdated",
    flags={"stock": True, "dedicated_server": True}, confidence="HIGH",
)

rec(
    message="OnWANUpdated", direction="server->client broadcast",
    content_shape={"userId": "affected id", "wanAddress": "string"},
    stock_binary="dispatcher reads userId/wanAddress",
    dedicated_server="{userId,wanAddress,user,users supersets}",
    flags={"stock": True, "dedicated_server": True}, confidence="HIGH",
)

rec(
    message="OnLANUpdated", direction="server->client broadcast",
    content_shape={"userId": "affected id", "lanAddresses": "[string]"},
    stock_binary="dispatcher reads userId/lanAddresses",
    dedicated_server="{userId,lanAddresses,user,users supersets}",
    flags={"stock": True, "dedicated_server": True}, confidence="HIGH",
)

rec(
    message="DoP2PConnect", direction="client->server",
    content_shape={"player": "target userId", "reset": "bool"},
    stock_binary="builder 0x006C59C0; capture-confirmed {'player':'TARGET','reset':false}",
    stock_live_capture="raw paired pcaps show both peers exchanging DoP2PConnect after game-lobby join",
    dedicated_server="relays OnLobbyMemberP2PConnect to both parties (member=origin id for recipient)",
    flags={"stock": True, "dedicated_server": True}, confidence="CONFIRMED",
)

rec(
    message="OnLobbyMemberP2PConnect", direction="server->client",
    content_shape={"member": "originating userId", "lobbyId": "int", "connect": "bool",
                   "reset": "bool"},
    stock_binary="dispatcher reads player/reset; capture shows member/lobbyId/connect/reset envelope",
    dedicated_server="emitted to both peers; adds player/target supersets",
    flags={"stock": True, "dedicated_server": True}, confidence="HIGH",
)

rec(
    message="DoP2PRoute", direction="client->server",
    content_shape={"player": "target userId"},
    stock_binary="builder 0x006C5D20 state-5 relay request",
    dedicated_server="responds OnLobbyMemberP2PConnect + OnP2PRoute{port:1339 placeholder}",
    flags={"stock": True, "dedicated_server": True}, confidence="HIGH",
)

rec(
    message="OnP2PRoute", direction="server->client",
    content_shape={"player": "target userId", "port": "relay port"},
    stock_binary="consumer FUN_0075f370 stores endpoint pair; success -> state 6 RELAY connect; failure -> reset",
    dedicated_server="port currently hardcoded 1339 placeholder; real allocation unimplemented",
    flags={"stock": True, "dedicated_server": True}, confidence="HIGH",
)

rec(
    message="Ping/DoPing", direction="client->server",
    content_shape="true",
    official_live="NO response observed from official for non-admin web sessions",
    lobby_monitor="manual Ping button sends both verbs",
    dedicated_server="replies OnHeartbeat {serverTimeMs} (deliberate superset)",
    flags={"official_live": True, "lobby_monitor": True, "dedicated_server": True},
    confidence="MEDIUM",
)

rec(
    message="OnHeartbeat", direction="server->client",
    content_shape="presence only",
    stock_binary="presence-only handler",
    dedicated_server="reply to Ping/DoPing",
    flags={"stock": True, "dedicated_server": True}, confidence="LOW",
)

rec(
    message="GetUserList/GetStatLog/GetFullLog/GetLogLevel", direction="client->server (admin)",
    content_shape="true",
    official_web_js="GetStatLog sent by admin page after auth",
    official_live="silently ignored for non-admin web users (no response at all)",
    dedicated_server="answers any authorized session (deliberate divergence for community tooling)",
    flags={"official_web": True, "official_live": True, "dedicated_server": True},
    confidence="HIGH",
)

rec(
    message="SetLogLevel/SetRelay/SetTest/AddRegex/RemoveRegex/TraceData",
    direction="client->server (admin)",
    content_shape={"level?": "0-2", "toggle?": "bool", "regex?": "glob string"},
    official_web_js="exact builders recovered from bzadmin.js",
    dedicated_server="implemented incl. admin gating; SetRelay acknowledged but relay transport not implemented",
    flags={"official_web": True, "dedicated_server": True}, confidence="HIGH",
)

rec(
    message="Admin events: OnGetLogLevel/OnGetUserList/OnGetFullLog/OnStdoutLogEntry/OnGetStatLog/OnRelayUpdated/OnEnableRelayChanged",
    direction="server->client (admin)",
    content_shape={
        "OnGetLogLevel": {"level": "[int,int] (admin page reads index 1)"},
        "OnGetUserList": {"users": {"<id>": "user snapshot"}},
        "OnGetFullLog": {"stdout": {"history": ["lines"]}},
        "OnStdoutLogEntry": {"txt": "line", "level": "str", "message": "str"},
        "OnGetStatLog": {"stats": "multiline string (page caps 200 lines)"},
        "OnRelayUpdated/OnEnableRelayChanged": {"enabled": "bool"},
    },
    official_web_js="all handled by bzadmin.js",
    dedicated_server="all implemented; shapes match admin page expectations",
    admin_grid_user_fields=["id","name","authType","clientVersion","ipAddress","lobby",
                            "isAdmin","isInLounge","metadata","wanAddress","lanAddresses"],
    admin_grid_lobby_fields=["id","owner(-1 none)","isLocked","isChat","isPrivate","password",
                             "memberLimit","users(names)","userCount","metadata"],
    flags={"official_web": True, "dedicated_server": True}, confidence="HIGH",
)

rec(
    message="OnServerShutdown", direction="server->client",
    content_shape="-",
    stock_binary="handler present (presence only)",
    dedicated_server="NOT emitted",
    flags={"stock": True}, confidence="LOW",
)

DOC["records"] = R
OUT.write_text(json.dumps(DOC, indent=2) + "\n", encoding="utf-8")
print(f"wrote {OUT} with {len(R)} records")
