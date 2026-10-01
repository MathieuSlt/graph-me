# graph-me

A local knowledge graph of your personal data (files, mail, WhatsApp) that your AI agent can query. Think [graphify](https://github.com/Graphify-Labs/graphify), for your own life.

> Status: alpha (0.1.0). Search over files, mail, chats and contacts, people and birthdays,
> `sync`, use from Claude Code (skill + MCP server), Tier 1 AI enrichment and a local web UI.
> See [CHANGELOG.md](CHANGELOG.md) for what changed and `docs/` for the design.

## Install

graph-me runs on a Python managed by [uv](https://docs.astral.sh/uv/), so you don't need Python installed.

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh     # once, if uv is missing
uv tool install --managed-python graph-me
graph-me init          # writes ~/graph-me/config.yaml from config-template.yaml
graph-me where
```

Edit `config.yaml` to list your sources. [`config-template.yaml`](config-template.yaml) documents every option.

```bash
graph-me scan                      # index your files (Tier 0: no AI, offline)
graph-me sync                      # later: add new items, update changed ones, forget deleted ones
graph-me query "lease agreement"   # cited results: path, snippet, source, date, trust
graph-me query "wifi" --json       # the context pack an agent receives
graph-me who "Sophie"              # a person: identifiers, facts, closest contacts
graph-me fact Sophie birthday      # one fact, with the messages and cards it came from
```

### Tier 1: AI enrichment

```bash
uv tool install --managed-python "graph-me[medium]"   # adds the local model and sqlite-vec
graph-me enrich --dry-run    # how much there is to do
graph-me enrich              # agent mode: writes batch files for your agent, then embeds
graph-me ingest --all        # after your agent answered the batches
```

Tier 1 labels only short strings, so it stays cheap on large archives:

- file names and folders get a type, a topic and keywords in English and French (the two
  languages of v1), so "rental contract" finds `apartment_2025.pdf`;
- contacts get a relation to you ("sibling", "landlord"...), so "when is my sister's birthday?"
  finds Sophie without her name.

Who labels is up to you (`extraction.medium.llm`): your agent (default: no API key, just ask
Claude Code "use graph-me to enrich the db"), a local Ollama model, the Claude API, or any
OpenAI-compatible server. graph-me asks before sending names to an API. Every answer is checked
against a strict schema; a label containing instructions, an unknown value or an unknown item is
rejected, so a malicious file name can at worst get a wrong label.

Meaning-based search runs a small multilingual model on your computer (downloaded once, about
220 MB): "car insurance" finds "vehicle policy renewal". It is combined with word search.

### Use it from Claude Code (or any MCP client)

```bash
graph-me install-skill                        # ~/.claude/skills/graph-me/SKILL.md
claude mcp add graph-me -- graph-me mcp       # read-only MCP server over stdio
```

Then ask: *"use graph-me: where is the lease PDF my landlord emailed me, and when is my
sister's birthday?"*. The MCP tools are `search`, `find_document`, `who_is`, `get_fact`,
`timeline`, `related`, `get_item` and `status`. All are read-only, and none can reveal redacted
secrets: an agent that just read a malicious email must not be able to ask for them.
`graph-me query "..." --format markdown` gives the same results to agents without MCP.

After every scan and sync, `graph-out/REPORT.md` summarizes what was indexed, the people you
deal with most, upcoming birthdays, communities and questions worth asking; `graph.json` holds
the graph for other tools.

### Web UI

```bash
uv tool install --managed-python "graph-me[ui]"
graph-me ui                  # opens http://127.0.0.1:<port>/?token=...
```

Four pages: **Search** (files, mails, chats and contacts, with filters by kind, source and date),
**Graph** (people, projects and documents; coloured by kind or group; click a node to see its
neighbours, double-click to open it), **entity pages** (identifiers, facts with their confidence,
relations, latest items) and **Status** (sources, last runs, blacklist, flagged items). Every fact
has a "Why do I know this?" link to the messages and files it came from.

The UI is read-only and runs on your machine only (127.0.0.1). The URL carries a random token,
new at each start: anyone with it can read your graph while `graph-me ui` runs. Message HTML is
never rendered, only its text, and redacted secrets stay redacted (`graph-me query --reveal` in
a terminal is the only way to see them). The front-end libraries are bundled, so it works
offline.

### Keeping up to date, and forgetting

`graph-me sync` mirrors your sources. A file you delete, a message deleted in msgvault (for
example spam deleted in Gmail and synced by msgvault), or a whole source removed from
`config.yaml` is forgotten, along with everything learned only from it. A fact backed by
something else stays: Sophie's birthday survives deleting one message if her contact card
still says it. Sync is manual: run it when you want.

Two safety nets protect the index. A source that can't be reached (unplugged drive, missing
folder, missing msgvault database) is skipped, never wiped. And a sync that would forget more
than half of a source (above 50 items) stops and asks for `--allow-mass-forget`. Files that
are still on disk but now skipped by your config (build folders, caches) don't count.

### Mail, chats and contacts

We recommend [msgvault](https://github.com/kenn-io/msgvault) for mail and chats: it syncs or
imports Gmail, IMAP, Microsoft 365, MBOX, PST, WhatsApp, iMessage, Slack, Discord and more into a
local archive. Add it as a `msgvault` source; graph-me reads its database read-only. msgvault is
not required: graph-me builds its own people and facts from any connector's messages. For
contacts without msgvault, point a `vcard` source at `.vcf` exports.

msgvault's Gmail login asks for the `gmail.modify` scope, used by msgvault's own deletion flow.
graph-me itself never writes to msgvault or to your accounts.

People are recognized by email and phone number (never by name alone), so two "Sophie" stay two
people. Birthdays come from contact cards, and from birthday wishes you sent to one person,
with a confidence that grows each year the same date comes back. Set `people.phone_country_code`
so national numbers ("07700 900123") match international ones ("+44 7700 900123").

Tier 0 uses no model at all, so results are less convincing than with AI: it matches words in file
names and contents (English and French in v1, accents ignored). Secrets such as IBANs, card numbers,
API keys and passwords are redacted in results unless you pass `--reveal`.

## Principles

- **Local-first**: your data stays on your machine.
- **Read-only**: graph-me never writes, sends or deletes anything in your sources.
- **Free by default**: Tier 0 uses no model and no network. Tier 1 and 2 add AI for better results.
- **Every fact is cited**: answers point back to the file or message they came from.

## Development

```bash
uv sync --all-extras
uv run pytest
uv run ruff check
uv run graph-me where      # inside the clone, data goes to ./graph-out
uv run python scripts/benchmark.py --config ~/graph-me/config.yaml   # scan time, DB size, query latency
```

The benchmark scans into a throwaway folder and prints numbers only (no names, paths or text),
so its table is safe to share.

## License

MIT
