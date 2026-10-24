# hermes-discord-admin

Lets a [Hermes Agent](https://hermes-agent.nousresearch.com/) run your Discord server: ask for something in Discord, and Hermes does it.

The plugin is one small Python file, [`discord_admin.py`](discord_admin.py), that runs as an MCP server. It uses only Python's standard library and Discord's official REST API (v10), with no third-party packages.

## Two tiers

The same file runs as two MCP servers, so you can give them different trust levels:

| Tier | Tools | Hermes setting |
|---|---|---|
| `safe` | `list_servers`, `server_overview`, `list_members`, `read_messages`, `create_channel`, `edit_channel`, `set_channel_permissions`, `clear_channel_permissions`, `create_role`, `edit_role`, `add_role_to_member`, `remove_role_from_member`, `set_nickname`, `send_message`, `pin_message`, `unpin_message`, `create_thread`, `rename_server` | `trust: full`: runs without asking |
| `danger` | `delete_channel`, `delete_role`, `delete_messages`, `timeout_member`, `kick_member`, `ban_member`, `unban_member` | `trust: untrusted`: Hermes asks you (Allow / Deny) first |

Built-in limits:
- No tool can grant the `ADMINISTRATOR` permission.
- `send_message` never pings `@everyone`, `@here` or roles.
- Danger-tier actions accept a `reason`, which is saved in the server's audit log.

## Requirements

- Hermes Agent with the Discord gateway already set up (`hermes gateway setup`).
- Python 3.9 or newer. The official Hermes Docker image already has it.
- A Discord bot in your server whose role has the permissions it needs, for example Manage Channels, Manage Roles, Manage Messages, Moderate/Kick/Ban Members. Put that role **above** the roles it should manage.

## Install (Hermes in Docker)

These steps assume Hermes data lives in `~/.hermes` on the host, mounted at `/opt/data` in the container.

1. Copy the plugin to the server so that **root** owns it. That way the agent can run the file but can't edit it:

   ```bash
   sudo mkdir -p ~/.hermes/mcp
   sudo cp discord_admin.py ~/.hermes/mcp/
   sudo chown -R root:root ~/.hermes/mcp
   sudo chmod 755 ~/.hermes/mcp
   sudo chmod 644 ~/.hermes/mcp/discord_admin.py
   ```

2. Add the two servers to `~/.hermes/config.yaml`. See [`config.example.yaml`](config.example.yaml). If the file already has an `mcp_servers:` section, add the entries under it instead of creating a second one.

3. Restart Hermes and check that both servers connect:

   ```bash
   sudo docker restart hermes
   sudo docker exec -it -u hermes hermes hermes mcp test discord
   sudo docker exec -it -u hermes hermes hermes mcp test discord_danger
   ```

The plugin reads the bot token from `DISCORD_BOT_TOKEN`. If that variable isn't set, it reads the token from Hermes's `.env` file (`$HERMES_HOME/.env`, default `/opt/data/.env`), so you don't have to store the token twice.

If the bot is in more than one server, set `DISCORD_GUILD_ID` or pass `server_id` to the tools.

## Test without Hermes

```bash
printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"list_servers","arguments":{}}}' \
  | DISCORD_BOT_TOKEN=... python3 discord_admin.py --tier safe
```

## Security notes

- Never commit your bot token or Hermes's `.env` file.
- When Hermes asks for approval, prefer **Allow Once**. **Always Allow** switches off the approval prompt for that tool for good.
- Keep the Hermes Discord allowlist (`DISCORD_ALLOWED_USERS`) limited to people you trust. Anyone on it can tell the agent to change the server.

## Adding a tool

1. Write a function that takes `args` and returns something JSON-serializable. Raise `ToolError` for errors the user should see.
2. Add it to `SAFE_TOOLS` or `DANGER_TOOLS` with `tool(...)`.
3. Restart Hermes.
