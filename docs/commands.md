# Commands

Every command has `--help`. These options go before the command name and work with all of them:

| Option | Meaning |
| --- | --- |
| `--config <path>` | Use this `config.yaml` (or set `GRAPH_ME_CONFIG`) |
| `--out <path>` | Use this folder for the index (or set `GRAPH_ME_OUT`) |
| `--version` | Print the version |

Example: `graph-me --config ~/work/config.yaml scan`.

## Set up

### `init`

`graph-me init` creates `config.yaml` from the template if it doesn't exist, the `graph-out/`
folder (readable only by you) and an empty index. It never overwrites an existing config.

### `where`

`graph-me where` shows which config and index are in use, how many sources, items, entities
(people, projects, documents) and facts they hold, and checks your Python and SQLite.

### `install-skill`

`graph-me install-skill` installs the Claude Code skill in `~/.claude/skills/graph-me/`.

| Option | Meaning |
| --- | --- |
| `--dest <path>` | Install into another skills folder (default `~/.claude/skills`) |
| `--force` | Replace a `SKILL.md` you edited |

## Index

### `scan`

`graph-me scan` reads your sources and adds new or changed items. It never removes anything. Use
`sync` for that.

### `sync`

`graph-me sync` makes the index match your sources. It adds and updates like `scan`. It also
forgets deleted files, messages deleted in msgvault, and sources you removed from
`config.yaml`, along with everything learned only from them. It skips a source it can't reach
(unplugged drive, missing folder) instead of wiping it.

| Option | `scan` | `sync` | Meaning |
| --- | --- | --- | --- |
| `--source <name>` | yes | yes | Only this source from `config.yaml` |
| `--workers <n>` | yes | yes | Number of files read in parallel (default: your CPU count minus one) |
| `--allow-mass-forget` | | yes | Allow forgetting more than half of a source. Without it, sync stops in case a drive is unplugged. |

### `report`

`graph-me report` rewrites `REPORT.md` and `graph.json` in `graph-out/`. `scan` and `sync`
already do it, so you rarely need this.

## Ask

### `query`

`graph-me query "<words>"` searches everything and prints each result with its path or message,
date, source and trust level.

| Option | Meaning |
| --- | --- |
| `--kind <kind>` | Only `file`, `email`, `message` or `contact` |
| `--source <name>` | Only this source |
| `--since <date>`, `--until <date>` | Only items dated in this range (`YYYY-MM-DD`) |
| `--limit <n>` | At most this many results (default 10) |
| `--format text\|json\|markdown` | Output for people (`text`), programs (`json`) or AI assistants (`markdown`) |
| `--json` | Same as `--format json` |
| `--reveal` | Show secrets that are normally hidden (see [Privacy and safety](privacy.md)) |

```bash
graph-me query "invoice" --kind email --since 2025-01-01
graph-me query "passport" --format markdown
```

### `who`

`graph-me who "<person>"` shows what graph-me knows about someone: emails, phone numbers, facts
and closest contacts. The person can be a name, a nickname, an email, a phone number, or `me`.
Add `--json` for JSON.

### `fact`

`graph-me fact <person> <fact>` shows one fact with the items graph-me learned it from, for
example `graph-me fact Sophie birthday`. Facts are `birthday`, `birth_year`, `nickname`,
`organization`, and after Tier 1 `relation_to_user`. Add `--json` for JSON. The output says when
a fact has low confidence.

## AI enrichment

### `enrich`

`graph-me enrich` runs Tier 1 (see [Better results with AI](tier1.md)).

| Option | Meaning |
| --- | --- |
| `--llm agent\|ollama\|anthropic\|openai_compat` | Who labels (overrides `config.yaml`) |
| `--dry-run` | Show what would be labelled and how much, without doing it |
| `--status` | List agent batches not answered and merged yet |
| `--source <name>` | Only items from this source |
| `--path <folder>` | Only files under this folder |
| `--since <date>` | Only items dated on or after this day |
| `--batch-size <n>` | Names per batch (default 50) |
| `--no-embeddings` | Skip meaning-based search |
| `--yes` | Don't ask before sending names to an API |

### `ingest`

`graph-me ingest <files>` merges answers to agent batches (`batch-NNNN.out.json`). Use `--all`
to merge every answered batch in `graph-out/work/`. It checks each answer before keeping it.

## Interfaces

### `ui`

`graph-me ui` starts the [web UI](web-ui.md) (needs `graph-me[ui]`).

| Option | Meaning |
| --- | --- |
| `--port <n>` | Use this port (default: any free port) |
| `--no-open` | Print the address without opening the browser |

### `mcp`

`graph-me mcp` starts the MCP server for AI assistants. You don't run it by hand. Register it
once with `claude mcp add graph-me -- graph-me mcp` (or your app's equivalent) and the app starts
it when needed. See [Use it from your AI assistant](assistants.md).
