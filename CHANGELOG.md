# Changelog

All notable changes to graph-me. Versions follow [semantic versioning](https://semver.org/);
before 1.0, minor versions may break the config or the store.

## Unreleased

### Added

- **Corrections** in `overrides.yaml`: `graph-me link` and `unlink` fix who is who, `relate`
  says how someone relates to you (so "my sister's birthday" works without Tier 1), and
  `fact --set/--reject` fixes a fact. Corrections are cited as "set by you" and removing one
  undoes it. `graph-me overrides` shows them.
- **Calendars**: an `ics` source for calendar files, and a `macos-calendar` source for the
  macOS Calendar app with all its accounts (`graph-me[macos]`). Meetings link the people in
  them; `who` says when you were last in touch and when you meet next.
- **Scanned documents**: `ocr: true` on a folder reads scanned PDF pages on your computer
  (`graph-me[ocr]`, no download); `images: true` also reads photographed documents.
- **Timelines and threads**: `graph-me timeline <person>` or `--topic`, a Timeline page, the
  `get_thread` MCP tool and whole conversations on an item's page.
- Evernote exports (`.enex`) are read; CSV chunks keep their header row.
- **Profiles**: `profiles:` in config.yaml and `graph-me mcp --profile work` (or
  `install-mcp --profile work`) limit an assistant to some sources, including what it learns
  about people. `graph-me ui --profile` too. Indexes made before need `graph-me scan --reread`
  once for people's identifiers to show under a profile.
- **Activity log**: `graph-me log` and the UI's Activity page show what your assistants asked
  (with the app's name) and what came back. Kept 90 days (`log.keep_days`).
- **What's coming up**: `graph-me upcoming`, the `upcoming` MCP tool and a "Coming up" UI page
  list birthdays, anniversaries and deadlines read in your documents and messages (when
  something expires, renews, is due or ends). REPORT.md lists them too. `graph-me export ics`
  (or `export: {ics: true}`) writes them as a calendar file.
- **Deadlines and amounts** are read by English and French rules, without AI, and shown with
  search results. Contact cards also give addresses, anniversaries and job titles. An existing
  index gets the new rules on the next scan, without reading your files again.
- **Easier first run**: `graph-me init --detect` finds your folders, Obsidian vaults,
  msgvault and address books and asks which to index; `graph-me scan --since 90d` reads recent
  items first; `graph-me demo` tries graph-me on made-up data; `graph-me doctor` checks the
  setup and says how to fix it.
- **`graph-me install-mcp`** registers the MCP server with Claude Code, Claude Desktop, Cursor or
  Codex.
- **`graph-me schedule install`** syncs regularly through launchd (macOS) or a systemd user timer
  (Linux). Only one scan, sync or enrich runs on an index at a time.
- **Index freshness**: every answer (CLI, MCP, Markdown) says when the index was last synced,
  and warns after `index.stale_after_days` (7 by default).

## 0.1.0 (2026-10-01)

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
