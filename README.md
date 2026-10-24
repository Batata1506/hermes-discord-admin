# hermes-agents

Plugins that add agents to a [Hermes Agent](https://hermes-agent.nousresearch.com/) running in Docker on a server and talking to you in Discord. Each plugin is a small MCP server in its own folder, using only Python's standard library. Some also come with a Hermes skill.

| Plugin | What it does |
|---|---|
| [`discord-admin`](discord-admin/) | Run your Discord server by asking: channels, roles, messages and moderation. Destructive actions ask for your approval first. |
| [`fashion`](fashion/) | A personal stylist in `#fits`: outfit ideas with a store photo of each piece for Vinted's photo search, a style quiz, and a fortnightly drop of new looks. |

## Shared setup

These steps assume Hermes data lives in `~/.hermes` on the server, mounted at `/opt/data` in the container.

1. Get the repo onto the server:

   ```bash
   git clone https://github.com/Batata1506/hermes-agents.git
   ```

2. Install the plugins you want by following each plugin's README. Plugins are copied into `~/.hermes/mcp/` (and skills into `~/.hermes/skills/`) and owned by root, so the agent can run them but can't edit them.

3. [`config.example.yaml`](config.example.yaml) has the `~/.hermes/config.yaml` entries for every plugin. Copy only the ones you install. If your config already has an `mcp_servers:` or `discord:` section, add the entries under it instead of creating a second one.

To update later, run `git pull` in the repo folder, copy the changed plugin files into `~/.hermes` again, and restart Hermes:

```bash
sudo docker restart hermes
```

## Adding a plugin

1. Make a folder with the MCP server file and a README. The MCP stdio code at the bottom of an existing plugin can be copied as-is.
2. Add its entry to `config.example.yaml` and a row to the table above.

## Security notes

- Never commit tokens, API keys or Hermes's `.env` file. `.gitignore` already excludes `.env` files.
- Plugins read their secrets from Hermes's `.env`, so each secret is stored in one place.
