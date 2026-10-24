#!/usr/bin/env python3
"""Discord server-admin tools for Hermes, as a tiny MCP server.

Standard library only: no third-party packages. Speaks MCP over stdio
(newline-delimited JSON-RPC) and calls Discord's official REST API (v10).

Run it in one of two tiers, as two separate MCP servers in Hermes:
  --tier safe     reading, creating and editing (Hermes runs these freely)
  --tier danger   deleting and moderating people (Hermes asks you first)

The bot token is read from DISCORD_BOT_TOKEN, or from Hermes's .env file.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

API = "https://discord.com/api/v10"
USER_AGENT = "DiscordBot (https://localhost/hermes-discord-admin, 0.1)"
SERVER_NAME = "discord-admin"
SERVER_VERSION = "0.1.0"

PERMISSIONS = {
    "CREATE_INSTANT_INVITE": 0, "KICK_MEMBERS": 1, "BAN_MEMBERS": 2,
    "ADMINISTRATOR": 3, "MANAGE_CHANNELS": 4, "MANAGE_GUILD": 5,
    "ADD_REACTIONS": 6, "VIEW_AUDIT_LOG": 7, "PRIORITY_SPEAKER": 8,
    "STREAM": 9, "VIEW_CHANNEL": 10, "SEND_MESSAGES": 11,
    "SEND_TTS_MESSAGES": 12, "MANAGE_MESSAGES": 13, "EMBED_LINKS": 14,
    "ATTACH_FILES": 15, "READ_MESSAGE_HISTORY": 16, "MENTION_EVERYONE": 17,
    "USE_EXTERNAL_EMOJIS": 18, "VIEW_GUILD_INSIGHTS": 19, "CONNECT": 20,
    "SPEAK": 21, "MUTE_MEMBERS": 22, "DEAFEN_MEMBERS": 23,
    "MOVE_MEMBERS": 24, "USE_VAD": 25, "CHANGE_NICKNAME": 26,
    "MANAGE_NICKNAMES": 27, "MANAGE_ROLES": 28, "MANAGE_WEBHOOKS": 29,
    "MANAGE_GUILD_EXPRESSIONS": 30, "USE_APPLICATION_COMMANDS": 31,
    "REQUEST_TO_SPEAK": 32, "MANAGE_EVENTS": 33, "MANAGE_THREADS": 34,
    "CREATE_PUBLIC_THREADS": 35, "CREATE_PRIVATE_THREADS": 36,
    "USE_EXTERNAL_STICKERS": 37, "SEND_MESSAGES_IN_THREADS": 38,
    "USE_EMBEDDED_ACTIVITIES": 39, "MODERATE_MEMBERS": 40,
}

CHANNEL_TYPES = {"text": 0, "voice": 2, "category": 4, "announcement": 5,
                 "stage": 13, "forum": 15}
CHANNEL_TYPE_NAMES = {v: k for k, v in CHANNEL_TYPES.items()}
CHANNEL_TYPE_NAMES.update({11: "thread", 12: "private_thread"})


class ToolError(Exception):
    pass


# ---------------------------------------------------------------- Discord API

_token = None
_guilds = None


def log(msg):
    print(f"[{SERVER_NAME}] {msg}", file=sys.stderr, flush=True)


def load_token():
    global _token
    if _token:
        return _token
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        home = os.environ.get("HERMES_HOME", "/opt/data")
        env_file = os.path.join(home, ".env")
        try:
            with open(env_file) as f:
                for line in f:
                    key, sep, value = line.strip().partition("=")
                    if sep and key.strip() == "DISCORD_BOT_TOKEN":
                        token = value.strip().strip("'\"")
        except OSError:
            pass
    if not token:
        raise ToolError("No DISCORD_BOT_TOKEN found in the environment or Hermes .env file.")
    _token = token
    return token


def discord(method, path, body=None, reason=None, query=None):
    url = API + path
    if query:
        url += "?" + urllib.parse.urlencode(query)
    headers = {"Authorization": f"Bot {load_token()}", "User-Agent": USER_AGENT}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if reason:
        headers["X-Audit-Log-Reason"] = urllib.parse.quote(reason[:512])

    for _ in range(5):
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            raw = e.read().decode(errors="replace")
            if e.code == 429:
                try:
                    wait = float(json.loads(raw).get("retry_after", 1))
                except ValueError:
                    wait = 1.0
                time.sleep(min(wait, 10))
                continue
            raise ToolError(f"Discord API {e.code} on {method} {path}: {raw[:500]}")
        except urllib.error.URLError as e:
            raise ToolError(f"Could not reach Discord: {e.reason}")
    raise ToolError("Discord kept rate-limiting the request; try again shortly.")


def guild_id(args):
    """Use the given server ID, or the only server the bot is in."""
    global _guilds
    if args.get("server_id"):
        return str(args["server_id"])
    if os.environ.get("DISCORD_GUILD_ID"):
        return os.environ["DISCORD_GUILD_ID"]
    if _guilds is None:
        _guilds = discord("GET", "/users/@me/guilds")
    if len(_guilds) == 1:
        return _guilds[0]["id"]
    if not _guilds:
        raise ToolError("The bot is not in any server yet.")
    names = ", ".join(f"{g['name']} ({g['id']})" for g in _guilds)
    raise ToolError(f"The bot is in several servers; pass server_id. Servers: {names}")


def permission_bits(names, allow_admin=False):
    bits = 0
    for name in names or []:
        key = str(name).strip().upper().replace(" ", "_")
        if key not in PERMISSIONS:
            raise ToolError(f"Unknown permission '{name}'. Known: {', '.join(PERMISSIONS)}")
        if key == "ADMINISTRATOR" and not allow_admin:
            raise ToolError("Granting ADMINISTRATOR is not allowed through this tool.")
        bits |= 1 << PERMISSIONS[key]
    return str(bits)


def permission_names(bits):
    bits = int(bits)
    return [n for n, b in PERMISSIONS.items() if bits & (1 << b)]


def color_value(color):
    if color is None:
        return None
    return int(str(color).lstrip("#"), 16)


def pick(args, mapping):
    """Copy the arguments that were given, renamed to Discord's field names."""
    return {api_key: args[arg] for arg, api_key in mapping.items() if arg in args}


# ---------------------------------------------------------------- safe tools

def list_servers(args):
    return [{"id": g["id"], "name": g["name"]} for g in discord("GET", "/users/@me/guilds")]


def server_overview(args):
    gid = guild_id(args)
    guild = discord("GET", f"/guilds/{gid}")
    channels = discord("GET", f"/guilds/{gid}/channels")
    roles = discord("GET", f"/guilds/{gid}/roles")

    categories = {c["id"]: {"id": c["id"], "name": c["name"], "position": c["position"], "channels": []}
                  for c in channels if c["type"] == 4}
    uncategorized = []
    for c in sorted(channels, key=lambda c: c["position"]):
        if c["type"] == 4:
            continue
        entry = {"id": c["id"], "name": c["name"],
                 "type": CHANNEL_TYPE_NAMES.get(c["type"], c["type"])}
        if c.get("topic"):
            entry["topic"] = c["topic"]
        parent = categories.get(c.get("parent_id"))
        (parent["channels"] if parent else uncategorized).append(entry)

    return {
        "server": {"id": guild["id"], "name": guild["name"], "owner_id": guild["owner_id"]},
        "uncategorized_channels": uncategorized,
        "categories": sorted(categories.values(), key=lambda c: c["position"]),
        "roles": [{"id": r["id"], "name": r["name"], "position": r["position"],
                   "color": f"#{r['color']:06x}", "permissions": permission_names(r["permissions"])}
                  for r in sorted(roles, key=lambda r: -r["position"])],
    }


def list_members(args):
    gid = guild_id(args)
    members = discord("GET", f"/guilds/{gid}/members", query={"limit": 1000})
    return [{"user_id": m["user"]["id"], "username": m["user"]["username"],
             "nickname": m.get("nick"), "bot": m["user"].get("bot", False),
             "role_ids": m["roles"]} for m in members]


def read_messages(args):
    limit = max(1, min(int(args.get("limit", 20)), 100))
    msgs = discord("GET", f"/channels/{args['channel_id']}/messages", query={"limit": limit})
    return [{"id": m["id"], "author": m["author"]["username"], "content": m["content"],
             "time": m["timestamp"]} for m in reversed(msgs)]


def create_channel(args):
    gid = guild_id(args)
    kind = args.get("type", "text")
    if kind not in CHANNEL_TYPES:
        raise ToolError(f"Unknown channel type '{kind}'. Use one of: {', '.join(CHANNEL_TYPES)}")
    body = {"name": args["name"], "type": CHANNEL_TYPES[kind]}
    body.update(pick(args, {"category_id": "parent_id", "topic": "topic",
                            "position": "position", "slowmode_seconds": "rate_limit_per_user"}))
    if args.get("private"):
        # The @everyone role shares the server's ID; hide the channel from it.
        body["permission_overwrites"] = [{"id": gid, "type": 0,
                                          "deny": permission_bits(["VIEW_CHANNEL"])}]
    c = discord("POST", f"/guilds/{gid}/channels", body)
    return {"created": CHANNEL_TYPE_NAMES.get(c["type"], c["type"]), "id": c["id"], "name": c["name"]}


def edit_channel(args):
    body = pick(args, {"name": "name", "topic": "topic", "category_id": "parent_id",
                       "position": "position", "slowmode_seconds": "rate_limit_per_user",
                       "nsfw": "nsfw"})
    if not body:
        raise ToolError("Nothing to change: pass at least one field.")
    c = discord("PATCH", f"/channels/{args['channel_id']}", body)
    return {"updated": c["id"], "name": c["name"]}


def set_channel_permissions(args):
    body = {"type": 0 if args.get("target_type", "role") == "role" else 1,
            "allow": permission_bits(args.get("allow")),
            "deny": permission_bits(args.get("deny"))}
    discord("PUT", f"/channels/{args['channel_id']}/permissions/{args['target_id']}", body)
    return {"ok": True, "channel_id": args["channel_id"], "target_id": args["target_id"]}


def clear_channel_permissions(args):
    discord("DELETE", f"/channels/{args['channel_id']}/permissions/{args['target_id']}")
    return {"ok": True}


def create_role(args):
    gid = guild_id(args)
    body = {"name": args["name"]}
    if "permissions" in args:
        body["permissions"] = permission_bits(args["permissions"])
    if "color" in args:
        body["color"] = color_value(args["color"])
    body.update(pick(args, {"show_separately": "hoist", "mentionable": "mentionable"}))
    r = discord("POST", f"/guilds/{gid}/roles", body)
    return {"created_role": r["id"], "name": r["name"]}


def edit_role(args):
    gid = guild_id(args)
    body = pick(args, {"name": "name", "show_separately": "hoist", "mentionable": "mentionable"})
    if "permissions" in args:
        body["permissions"] = permission_bits(args["permissions"])
    if "color" in args:
        body["color"] = color_value(args["color"])
    if not body:
        raise ToolError("Nothing to change: pass at least one field.")
    r = discord("PATCH", f"/guilds/{gid}/roles/{args['role_id']}", body)
    return {"updated_role": r["id"], "name": r["name"]}


def add_role_to_member(args):
    gid = guild_id(args)
    discord("PUT", f"/guilds/{gid}/members/{args['user_id']}/roles/{args['role_id']}")
    return {"ok": True}


def remove_role_from_member(args):
    gid = guild_id(args)
    discord("DELETE", f"/guilds/{gid}/members/{args['user_id']}/roles/{args['role_id']}")
    return {"ok": True}


def set_nickname(args):
    gid = guild_id(args)
    discord("PATCH", f"/guilds/{gid}/members/{args['user_id']}", {"nick": args.get("nickname") or None})
    return {"ok": True}


def send_message(args):
    body = {"content": args["content"],
            # Never ping @everyone, @here or whole roles.
            "allowed_mentions": {"parse": ["users"]}}
    m = discord("POST", f"/channels/{args['channel_id']}/messages", body)
    return {"sent": m["id"], "channel_id": m["channel_id"]}


def _pin(method, args):
    cid, mid = args["channel_id"], args["message_id"]
    try:
        discord(method, f"/channels/{cid}/messages/pins/{mid}")
    except ToolError as e:
        if " 404 " not in str(e) and " 405 " not in str(e):
            raise
        discord(method, f"/channels/{cid}/pins/{mid}")  # older endpoint
    return {"ok": True}


def pin_message(args):
    return _pin("PUT", args)


def unpin_message(args):
    return _pin("DELETE", args)


def create_thread(args):
    cid = args["channel_id"]
    if args.get("message_id"):
        t = discord("POST", f"/channels/{cid}/messages/{args['message_id']}/threads",
                    {"name": args["name"]})
    elif args.get("first_message"):  # forum channels need a first post
        t = discord("POST", f"/channels/{cid}/threads",
                    {"name": args["name"], "message": {"content": args["first_message"],
                                                       "allowed_mentions": {"parse": ["users"]}}})
    else:
        t = discord("POST", f"/channels/{cid}/threads", {"name": args["name"], "type": 11})
    return {"created_thread": t["id"], "name": t["name"]}


def rename_server(args):
    gid = guild_id(args)
    g = discord("PATCH", f"/guilds/{gid}", {"name": args["name"]})
    return {"ok": True, "name": g["name"]}


# ---------------------------------------------------------------- danger tools

def delete_channel(args):
    c = discord("DELETE", f"/channels/{args['channel_id']}", reason=args.get("reason"))
    return {"deleted": c["id"], "name": c.get("name")}


def delete_role(args):
    gid = guild_id(args)
    discord("DELETE", f"/guilds/{gid}/roles/{args['role_id']}", reason=args.get("reason"))
    return {"deleted_role": args["role_id"]}


def delete_messages(args):
    cid, ids = args["channel_id"], [str(i) for i in args["message_ids"]]
    if len(ids) == 1:
        discord("DELETE", f"/channels/{cid}/messages/{ids[0]}", reason=args.get("reason"))
    else:
        # Bulk delete: 2-100 messages, each under 14 days old.
        for i in range(0, len(ids), 100):
            chunk = ids[i:i + 100]
            if len(chunk) == 1:
                discord("DELETE", f"/channels/{cid}/messages/{chunk[0]}", reason=args.get("reason"))
            else:
                discord("POST", f"/channels/{cid}/messages/bulk-delete", {"messages": chunk},
                        reason=args.get("reason"))
    return {"deleted_messages": len(ids)}


def timeout_member(args):
    gid = guild_id(args)
    minutes = int(args["minutes"])
    until = None
    if minutes > 0:
        until = (datetime.now(timezone.utc) + timedelta(minutes=min(minutes, 28 * 24 * 60))).isoformat()
    discord("PATCH", f"/guilds/{gid}/members/{args['user_id']}",
            {"communication_disabled_until": until}, reason=args.get("reason"))
    return {"ok": True, "timed_out_until": until}


def kick_member(args):
    gid = guild_id(args)
    discord("DELETE", f"/guilds/{gid}/members/{args['user_id']}", reason=args.get("reason"))
    return {"kicked": args["user_id"]}


def ban_member(args):
    gid = guild_id(args)
    seconds = max(0, min(int(args.get("delete_message_days", 0)), 7)) * 86400
    discord("PUT", f"/guilds/{gid}/bans/{args['user_id']}",
            {"delete_message_seconds": seconds}, reason=args.get("reason"))
    return {"banned": args["user_id"]}


def unban_member(args):
    gid = guild_id(args)
    discord("DELETE", f"/guilds/{gid}/bans/{args['user_id']}", reason=args.get("reason"))
    return {"unbanned": args["user_id"]}


# ---------------------------------------------------------------- tool table

def s(desc, **extra):
    return dict(type="string", description=desc, **extra)


SERVER = s("Server ID. Optional when the bot is in only one server.")
CHANNEL = s("Channel ID (use server_overview to find IDs).")
USER = s("Discord user ID (use list_members to find IDs).")
ROLE = s("Role ID (use server_overview to find IDs).")
REASON = s("Short reason, saved in the server's audit log.")
PERM_LIST = dict(type="array", items={"type": "string"},
                 description="Permission names, e.g. VIEW_CHANNEL, SEND_MESSAGES, MANAGE_MESSAGES.")


def tool(fn, desc, props, required=(), read_only=False, destructive=False):
    return {
        "fn": fn,
        "spec": {
            "name": fn.__name__,
            "description": desc,
            "inputSchema": {"type": "object", "properties": props, "required": list(required)},
            "annotations": {"readOnlyHint": read_only, "destructiveHint": destructive,
                            "openWorldHint": True},
        },
    }


SAFE_TOOLS = [
    tool(list_servers, "List the Discord servers the bot is in.", {}, read_only=True),
    tool(server_overview, "Show the server's categories, channels (with IDs and topics) and roles. "
         "Call this first to find IDs.", {"server_id": SERVER}, read_only=True),
    tool(list_members, "List server members with their user IDs and role IDs.",
         {"server_id": SERVER}, read_only=True),
    tool(read_messages, "Read recent messages in a channel or thread.",
         {"channel_id": CHANNEL, "limit": {"type": "integer", "description": "1-100, default 20"}},
         ["channel_id"], read_only=True),
    tool(create_channel, "Create a channel or category.",
         {"server_id": SERVER, "name": s("Channel name."),
          "type": s("Channel type.", enum=list(CHANNEL_TYPES)),
          "category_id": s("Category ID to put the channel in."),
          "topic": s("Channel topic."), "position": {"type": "integer"},
          "slowmode_seconds": {"type": "integer"},
          "private": {"type": "boolean", "description": "Hide from @everyone."}},
         ["name"]),
    tool(edit_channel, "Rename a channel, change its topic, move it to a category, reorder it, "
         "or set slowmode.",
         {"channel_id": CHANNEL, "name": s("New name."), "topic": s("New topic."),
          "category_id": s("Category ID to move it into."), "position": {"type": "integer"},
          "slowmode_seconds": {"type": "integer"}, "nsfw": {"type": "boolean"}},
         ["channel_id"]),
    tool(set_channel_permissions, "Set what a role or member can do in one channel or category "
         "(replaces that target's existing override).",
         {"channel_id": CHANNEL, "target_id": s("Role ID or user ID. The @everyone role ID equals "
                                                "the server ID."),
          "target_type": s("Whether target_id is a role or a member.", enum=["role", "member"]),
          "allow": PERM_LIST, "deny": PERM_LIST},
         ["channel_id", "target_id"]),
    tool(clear_channel_permissions, "Remove a role's or member's permission override from a channel.",
         {"channel_id": CHANNEL, "target_id": s("Role ID or user ID.")},
         ["channel_id", "target_id"]),
    tool(create_role, "Create a role. ADMINISTRATOR cannot be granted.",
         {"server_id": SERVER, "name": s("Role name."), "color": s("Hex color like #3498db."),
          "permissions": PERM_LIST,
          "show_separately": {"type": "boolean", "description": "Show members separately in the list."},
          "mentionable": {"type": "boolean"}},
         ["name"]),
    tool(edit_role, "Change a role's name, color or permissions. ADMINISTRATOR cannot be granted.",
         {"server_id": SERVER, "role_id": ROLE, "name": s("New name."), "color": s("Hex color."),
          "permissions": PERM_LIST, "show_separately": {"type": "boolean"},
          "mentionable": {"type": "boolean"}},
         ["role_id"]),
    tool(add_role_to_member, "Give a member a role.",
         {"server_id": SERVER, "user_id": USER, "role_id": ROLE}, ["user_id", "role_id"]),
    tool(remove_role_from_member, "Take a role away from a member.",
         {"server_id": SERVER, "user_id": USER, "role_id": ROLE}, ["user_id", "role_id"]),
    tool(set_nickname, "Set or clear a member's nickname.",
         {"server_id": SERVER, "user_id": USER, "nickname": s("New nickname; empty to clear.")},
         ["user_id"]),
    tool(send_message, "Post a message in a channel or thread. Never pings @everyone or roles.",
         {"channel_id": CHANNEL, "content": s("Message text (max 2000 characters).")},
         ["channel_id", "content"]),
    tool(pin_message, "Pin a message.", {"channel_id": CHANNEL, "message_id": s("Message ID.")},
         ["channel_id", "message_id"]),
    tool(unpin_message, "Unpin a message.", {"channel_id": CHANNEL, "message_id": s("Message ID.")},
         ["channel_id", "message_id"]),
    tool(create_thread, "Start a thread in a channel, optionally from a message. For forum channels, "
         "pass first_message.",
         {"channel_id": CHANNEL, "name": s("Thread name."),
          "message_id": s("Start the thread from this message."),
          "first_message": s("First post (needed for forum channels).")},
         ["channel_id", "name"]),
    tool(rename_server, "Rename the server.", {"server_id": SERVER, "name": s("New server name.")},
         ["name"]),
]

DANGER_TOOLS = [
    tool(delete_channel, "Permanently delete a channel or category (and its messages).",
         {"channel_id": CHANNEL, "reason": REASON}, ["channel_id"], destructive=True),
    tool(delete_role, "Permanently delete a role.",
         {"server_id": SERVER, "role_id": ROLE, "reason": REASON}, ["role_id"], destructive=True),
    tool(delete_messages, "Delete one or more messages from a channel (bulk delete only works on "
         "messages under 14 days old).",
         {"channel_id": CHANNEL, "message_ids": {"type": "array", "items": {"type": "string"}},
          "reason": REASON}, ["channel_id", "message_ids"], destructive=True),
    tool(timeout_member, "Time a member out (they can't talk) for some minutes; 0 removes a timeout.",
         {"server_id": SERVER, "user_id": USER, "minutes": {"type": "integer"}, "reason": REASON},
         ["user_id", "minutes"], destructive=True),
    tool(kick_member, "Kick a member from the server.",
         {"server_id": SERVER, "user_id": USER, "reason": REASON}, ["user_id"], destructive=True),
    tool(ban_member, "Ban a user from the server.",
         {"server_id": SERVER, "user_id": USER, "reason": REASON,
          "delete_message_days": {"type": "integer", "description": "0-7 days of their messages to delete."}},
         ["user_id"], destructive=True),
    tool(unban_member, "Lift a user's ban.",
         {"server_id": SERVER, "user_id": USER, "reason": REASON}, ["user_id"], destructive=True),
]


# ---------------------------------------------------------------- MCP stdio

def respond(msg_id, result=None, error=None):
    msg = {"jsonrpc": "2.0", "id": msg_id}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def handle(msg, tools):
    method, msg_id, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if msg_id is None:
        return  # a notification, e.g. notifications/initialized
    if method == "initialize":
        respond(msg_id, {
            "protocolVersion": params.get("protocolVersion", "2025-06-18"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        })
    elif method == "ping":
        respond(msg_id, {})
    elif method == "tools/list":
        respond(msg_id, {"tools": [t["spec"] for t in tools.values()]})
    elif method == "tools/call":
        t = tools.get(params.get("name"))
        if not t:
            respond(msg_id, error={"code": -32602, "message": f"Unknown tool: {params.get('name')}"})
            return
        try:
            result = t["fn"](params.get("arguments") or {})
            text, is_error = json.dumps(result, ensure_ascii=False, indent=1), False
        except ToolError as e:
            text, is_error = str(e), True
        except (KeyError, ValueError, TypeError) as e:
            text, is_error = f"Bad or missing argument: {e}", True
        respond(msg_id, {"content": [{"type": "text", "text": text}], "isError": is_error})
    else:
        respond(msg_id, error={"code": -32601, "message": f"Method not found: {method}"})


def main():
    tier = "safe"
    if "--tier" in sys.argv:
        tier = sys.argv[sys.argv.index("--tier") + 1]
    if tier not in ("safe", "danger"):
        sys.exit("--tier must be 'safe' or 'danger'")
    tools = {t["spec"]["name"]: t for t in (SAFE_TOOLS if tier == "safe" else DANGER_TOOLS)}
    log(f"started, tier={tier}, {len(tools)} tools")

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            respond(None, error={"code": -32700, "message": "Parse error"})
            continue
        try:
            handle(msg, tools)
        except Exception as e:  # keep the server alive whatever happens
            log(f"error handling {msg.get('method')}: {e!r}")
            if msg.get("id") is not None:
                respond(msg["id"], error={"code": -32603, "message": str(e)})


if __name__ == "__main__":
    main()
