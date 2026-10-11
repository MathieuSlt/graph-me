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

`graph-me init --detect` writes the config for you instead. It looks for your Documents, Desktop
and Downloads folders, Obsidian vaults, a msgvault archive and `.vcf` address books, asks about
each one, and guesses your time zone and phone country code. It reads nothing inside your
files. `--yes` keeps everything it finds.

### `demo`

`graph-me demo` builds a graph from made-up data (a lease, a car insurance renewal, a plumber's
quote, an address book, a suspicious download) in a temporary folder, and prints questions to
try on it. `--dir <folder>` builds it in an empty folder of your choice, `--ui` opens the web
UI on it. Your own config and index are not touched.

### `doctor`

`graph-me doctor` checks the whole setup and says how to fix each problem: the config, every
source (reached the way `scan` would, reading one item at most), your corrections, the index
folder's permissions, the index and how fresh it is, failures in the last run, the optional
parts installed, the Claude Code skill and MCP server, and the schedule. It exits with code 1
if something fails.

### `install-mcp`

`graph-me install-mcp` registers the MCP server with an AI app, using absolute paths so the app
can start it without your shell.

| Option | Meaning |
| --- | --- |
| `--client claude-code\|claude-desktop\|cursor\|codex` | Which app (default `claude-code`, through `claude mcp add --scope user`) |
| `--name <name>` | The server's name in the app (default `graph-me`) |
| `--print` | Show the change without making it |
| `--yes` | Don't ask before editing |

It shows the entry it adds first (never the app's other settings, which can hold tokens),
keeps a `.bak` copy, and leaves the rest of the file's settings as they are.

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

`graph-me scan --reread` reads every item again, even unchanged ones. You need it once after
upgrading to use profiles on an older index (`graph-me doctor` says so).

On a big archive, `graph-me scan --since 90d` reads only what is dated in the last 90 days
(also `12w`, `6m`, `1y` or a date), so you can ask questions within minutes. Run `graph-me scan`
afterwards for the rest.

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

Only one `scan`, `sync`, `enrich` or `ingest` runs on an index at a time. A second one stops
at once with a message instead of waiting.

### `schedule`

`graph-me schedule install --every 6h` runs `graph-me sync` regularly with your system's own
scheduler: a launchd agent on macOS (`~/Library/LaunchAgents/io.github.graph-me.sync.plist`),
a systemd user timer on Linux (`~/.config/systemd/user/graph-me-sync.timer`). There is no
graph-me background service. Each run appends to `graph-out/sync.log`. A source that can't be
reached is skipped, never wiped, as with a manual sync.

| Command | Meaning |
| --- | --- |
| `graph-me schedule install [--every 30m\|6h\|1d]` | Sync on a schedule (default every 6 hours, at least 15 minutes); replaces an existing one |
| `graph-me schedule status` | Whether a schedule is installed and loaded |
| `graph-me schedule remove` | Stop syncing on a schedule |

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
example `graph-me fact Sophie birthday`. Facts are `birthday`, `birth_year`, `anniversary`,
`nickname`, `organization`, `job_title`, `address`, `relation_to_user` (set with `relate`, or by
Tier 1), and `quote_amount` / `invoice_amount` (what someone quoted or billed you). Add `--json` for JSON.
The output says when a fact has low confidence.

### `upcoming`

`graph-me upcoming` lists what is coming up in the next 60 days: birthdays and anniversaries,
and deadlines read in your documents and messages: when something **expires**, **renews**, is
**due** or **ends**. Each line says how sure graph-me is and where it read it.

| Option | Meaning |
| --- | --- |
| `--days <n>` | How far ahead (default 60, up to 366) |
| `--kind dates\|deadlines` | Only birthdays and anniversaries, or only deadlines (or one key, like `renews`) |
| `--json` | Print JSON |

Deadlines and amounts come from rules, without AI: a full date (day, month and year) a few
words after a phrase like "valid until", "renews on", "due by" or "lease ends" (French:
"valable jusqu'au", "échéance", "fin du bail"...), and an amount after "quote", "invoice" or
"total due". They are marked 60% sure: check the document before relying on one. Search
results show them too ("read in it: ends 2026-06-30").

### `export ics`

`graph-me export ics` writes `graph-out/upcoming.ics`: birthdays and anniversaries as yearly
events, deadlines as all-day events with a reminder a week before. Import it in your calendar
app, or subscribe to the file. With `export: {ics: true}` in `config.yaml`, every `scan` and
`sync` rewrites it. `--out <file>` writes it elsewhere. Events hold a name and what happens
("Alex Taylor: birthday", "lease_2025.pdf: ends"), no message text.

### Corrections from `fact`

With `--set <value>` or `--reject <value>`, `fact` saves a correction instead (see below):
`graph-me fact Sophie birthday --reject 07-04`.

`query`, `who` and `fact` warn when the index was last synced more than
`index.stale_after_days` ago (7 by default). MCP answers carry the same information in `index`.

## Correct

graph-me can be wrong: two people merged into one, a birthday taken from a late wish, a sister
it can't recognize. These commands save your corrections in `overrides.yaml`, next to
`config.yaml`. The next `scan` or `sync` applies them. graph-me reads that file like a source:
each correction is cited as "set by you", and removing it undoes it. A person can be named by
email, phone number, or a name that matches exactly one person.

| Command | What it says |
| --- | --- |
| `graph-me link <person> <person>...` | These emails or phone numbers are one person |
| `graph-me unlink <person> <person>...` | These are different people, even if a contact card lists them together |
| `graph-me relate <person> <relation>` | How they relate to you (`sibling`, `parent`, `spouse`, `landlord`...), so "my sister" finds them without Tier 1. `--remove` forgets it. |
| `graph-me fact <person> <fact> --set <value>` | Know this value (confidence 100%) |
| `graph-me fact <person> <fact> --reject <value>` | This value is wrong: drop it, now and after every re-read |
| `graph-me overrides` | Show your corrections and where they are saved |

```bash
graph-me relate sophie@example.com sibling
graph-me unlink sophie@work.example sophie@example.com
graph-me sync
graph-me fact "my sister" birthday
```

`unlink`, and removing a link or a rejection, are applied fully by a `scan` or `sync` without
`--source`: graph-me rebuilds the people involved from their items.

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

### `log`

`graph-me log` shows what was asked, newest first: by your AI assistants (with the app's name,
like "claude-code"), in the terminal and in the web UI, with the files, messages and people each
answer returned. Items deleted since show as forgotten. The web UI has the same list under
**Activity**. AI assistants can't read it: there is no MCP tool for it.

| Option | Meaning |
| --- | --- |
| `--since <when>` | Only since then (`7d`, `2w` or a date) |
| `--interface mcp\|cli\|ui` | Only from there |
| `--client <app>` | Only this app |
| `--profile <name>` | Only queries made under this profile |
| `--limit <n>` | At most this many (default 50) |
| `--json` | Print JSON |
| `--clear` | Delete the log (with `--since`, only what is older) |

Queries are kept `log.keep_days` days (90 by default).

### `ui`

`graph-me ui` starts the [web UI](web-ui.md) (needs `graph-me[ui]`).

| Option | Meaning |
| --- | --- |
| `--port <n>` | Use this port (default: any free port) |
| `--no-open` | Print the address without opening the browser |

### `mcp`

`graph-me mcp` starts the MCP server for AI assistants. With `--profile <name>`, the assistant
only sees that profile's sources (see [Profiles](configuration.md#profiles)) and can't change
it: register one server per profile with `graph-me install-mcp --profile <name>`, which names it
`graph-me-<name>`. `graph-me ui --profile <name>` opens the web UI the same way. You don't run it by hand. Register it
once with `claude mcp add graph-me -- graph-me mcp` (or your app's equivalent) and the app starts
it when needed. See [Use it from your AI assistant](assistants.md).
