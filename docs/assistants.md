# Use it from your AI assistant

graph-me ships a Claude Code skill and an MCP server. Set both up once:

```bash
graph-me install-skill   # teaches Claude Code when and how to use graph-me
graph-me install-mcp     # lets Claude Code call graph-me's tools (runs `claude mcp add`)
```

Then start your request with "use graph-me":

> use graph-me: where is the lease PDF my landlord emailed me, and when is my sister's birthday?

Claude answers with the file path, the email it came with, and the birthday with the messages
graph-me learned it from.

## Other MCP clients

`graph-me install-mcp --client claude-desktop`, `--client cursor` or `--client codex` adds
graph-me to those apps' config files (it shows the change first and keeps a `.bak` copy).
Other MCP clients work the same way: point them at the command `graph-me mcp`, with
`--config <path to config.yaml>` before `mcp` if your config is not in `~/graph-me`.

## MCP tools

| Tool | What it does |
| --- | --- |
| `search` | Searches files, emails, chats and contacts |
| `find_document` | Finds a file from a description, with the email it came with |
| `who_is` | Shows a person's identifiers, facts and closest contacts |
| `get_fact` | Returns one fact (birthday, nickname, organization...) and its sources |
| `timeline` | Lists a person's items in date order |
| `related` | Lists the people and projects linked to someone |
| `get_item` | Returns the full text of one file or message |
| `status` | Shows the sources, item counts and last sync |

All tools are read-only. Secrets stay redacted in every answer, and there is no option to reveal
them.

## Assistants without MCP

They can run `graph-me query "..." --format markdown` and read the result.
