# Use it from your AI assistant

graph-me ships a Claude Code skill and an MCP server. Set both up once:

```bash
graph-me install-skill                     # teaches Claude Code when and how to use graph-me
claude mcp add graph-me -- graph-me mcp    # lets Claude Code call graph-me's tools
```

Then start your request with "use graph-me":

> use graph-me: where is the lease PDF my landlord emailed me, and when is my sister's birthday?

Claude answers with the file path, the email it came with, and the birthday with the messages
graph-me learned it from.

## Other MCP clients

Other MCP clients (Claude Desktop, Cursor and others) work the same way. Point them at the
command `graph-me mcp`.

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
