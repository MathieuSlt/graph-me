# Development

```bash
git clone https://github.com/MathieuSlt/graph-me && cd graph-me
uv sync --extra medium --extra ui
uv run pytest                 # mail and WhatsApp tests need msgvault: scripts/install_msgvault.sh
uv run ruff check && uv run ruff format --check
uv run graph-me where         # inside the clone, config.yaml and graph-out/ live at its root
```

The tests use only synthetic data from `tests/factory.py`.
`scripts/benchmark.py --config <config.yaml>` measures scan time, index size and query speed on
your own data and prints numbers only, so you can share the result.

## Design notes

- [Discovery](graph-me-discovery.md): vision, scope, architecture, security model and the
  decisions log.
- [Implementation plan](graph-me-implementation-plan.md): stack, repo layout, SQLite schema,
  connector interface, CLI and MCP tools, tests and milestones.

## This documentation

The site is built with [Zensical](https://zensical.org) from `docs/` and `zensical.toml`, and
published to GitHub Pages on every push to `main`. To preview it while you edit:

```bash
uv run --group docs zensical serve -a 127.0.0.1:8000   # 127.0.0.1, not localhost: avoids "Request Header Fields Too Large" from localhost cookies
```
