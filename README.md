# graph-me

A local knowledge graph of your personal data (files, mail, WhatsApp) that your AI agent can query. Think [graphify](https://github.com/Graphify-Labs/graphify), for your own life.

> Status: early development (milestone M1). Tier 0 search over your files works; mail, WhatsApp,
> the MCP server and the Claude Code skill come next. See `docs/` for the design.

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
graph-me query "contrat de bail"   # cited results: path, snippet, source, date, trust
graph-me query "wifi" --json       # the context pack an agent receives
```

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
