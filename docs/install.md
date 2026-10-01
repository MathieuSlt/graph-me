# Install

graph-me runs on a Python that [uv](https://docs.astral.sh/uv/) downloads and manages for it, so
you don't need Python installed and nothing touches your system Python.

```bash
# 1. Install uv, if you don't have it yet
curl -LsSf https://astral.sh/uv/install.sh | sh

# 2. Install graph-me
uv tool install --managed-python graph-me
```

## Optional parts

The base install has search, people, birthdays, sync and the AI assistant interfaces. You add
the optional parts by name:

| Install command | Adds |
| --- | --- |
| `uv tool install --managed-python graph-me` | Everything above, with no AI and no network |
| `uv tool install --managed-python "graph-me[ui]"` | The [web UI](web-ui.md) |
| `uv tool install --managed-python "graph-me[medium]"` | [AI labels and meaning-based search](tier1.md) (Tier 1) |
| `uv tool install --managed-python "graph-me[medium,ui]"` | Both |

## Mail and chats

For mail and chats, install [msgvault](https://github.com/kenn-io/msgvault) too and import your
accounts with it first. graph-me reads msgvault's archive and never connects to your accounts
itself.

## Upgrade and uninstall

```bash
uv tool upgrade graph-me
uv tool uninstall graph-me
```

Uninstalling leaves `~/graph-me/` (your config and index) in place. Delete that folder to remove
them too.
