# Changelog

All notable changes to graph-me. Versions follow [semantic versioning](https://semver.org/);
before 1.0, minor versions may break the config or the store.

## 0.1.0 (unreleased)

First public release.

### Added

- **Tier 0 indexing, no AI and no network**: text from files (PDF, Word, Excel, PowerPoint,
  Markdown, text, code), full-text search with French and English rule packs, secrets redacted in
  every result.
- **Mail, chats and contacts** through [msgvault](https://github.com/kenn-io/msgvault)
  (read-only) and `.vcf` files: people recognized by email and phone, birthdays from contact
  cards and from yearly wishes, files linked to the emails they came with.
- **`graph-me sync`**: adds, updates and forgets, with safety nets for unreachable sources and
  mass deletions. Build folders, caches, lockfiles and minified bundles are skipped as noise.
- **Agent interfaces**: a read-only MCP server (`graph-me mcp`), a Claude Code skill
  (`graph-me install-skill`), and `graph-me query --format markdown`. Results are cited, tagged
  by trust level, and flagged when they contain instruction-like text.
- **Tier 1 enrichment** (`graph-me enrich`, `[medium]` extra): labels for file names and
  contacts from your agent, Ollama, the Claude API or an OpenAI-compatible server, validated
  against a closed schema; meaning-based search with a local multilingual model.
- **Web UI** (`graph-me ui`, `[ui]` extra): search, graph explorer, entity pages with the
  evidence behind every fact, status. Local only, token-protected, read-only.
- `REPORT.md` and `graph.json` after every scan and sync; `scripts/benchmark.py`.
