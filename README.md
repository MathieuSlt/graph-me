# graph-me

A local knowledge graph of your personal data (files, mail, WhatsApp) that your AI agent can query. Think [graphify](https://github.com/Graphify-Labs/graphify), for your own life.

> Status: early development (milestone M5). Search over files, mail, chats and contacts, people
> and birthdays, `sync`, use from Claude Code (skill + MCP server), and Tier 1 AI enrichment.
> The web UI and the first release come next.
> See `docs/` for the design.

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
graph-me query "contrat de bail"   # cited results: path, snippet, source, date, trust
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

- file names and folders get a type, a topic and keywords in French and English, so
  "rental contract" finds `Contrat_bail_2025.pdf`;
- contacts get a relation to you ("sibling", "landlord"...), so "when is my sister's birthday?"
  finds Sophie without her name.

Who labels is up to you (`extraction.medium.llm`): your agent (default: no API key, just ask
Claude Code "use graph-me to enrich the db"), a local Ollama model, the Claude API, or any
OpenAI-compatible server. graph-me asks before sending names to an API. Every answer is checked
against a strict schema; a label containing instructions, an unknown value or an unknown item is
rejected, so a malicious file name can at worst get a wrong label.

Meaning-based search runs a small multilingual model on your computer (downloaded once, about
220 MB): "dessert recipe" finds "recette de crêpes". It is combined with word search.

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
so national numbers ("06 12 34 56 78") match international ones.

Tier 0 uses no model at all, so results are less convincing than with AI: it matches words in file
names and contents (French and English, accents ignored). Secrets such as IBANs, card numbers,
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
```

## License

MIT
