# Configuration

graph-me keeps its files in `~/graph-me/`:

| Path | What it is |
| --- | --- |
| `~/graph-me/config.yaml` | Your sources and settings |
| `~/graph-me/graph-out/graph.db` | The index (a single SQLite file) |
| `~/graph-me/graph-out/REPORT.md` | A summary written after each scan and sync |
| `~/graph-me/graph-out/graph.json` | The graph, for other tools |

To start over, delete `graph-out/` and run `graph-me scan` again. This never touches your
sources.

[`config-template.yaml`](https://github.com/MathieuSlt/graph-me/blob/main/config-template.yaml)
is the file `graph-me init` copies, with a comment on every option.

## Sources

Each entry under `sources:` has a name you choose and a `type`.

### `filesystem`

Reads folders.

| Key | Meaning |
| --- | --- |
| `paths` | Folders (or single files) to read |
| `trust` | `self` for your own files (default), `known` for files from people you know, `untrusted` for files from anyone, like Downloads |
| `exclude` | Patterns to skip, like `["**/old-backups/**"]`. Setting it replaces the built-in list below. |
| `ocr` | `true` reads scanned PDF pages (pages with no text in them), up to 30 per PDF. Needs `graph-me[ocr]`. Turning it on reads your PDFs again. |
| `images` | With `ocr: true`, also reads `.png` and `.jpg` files as photographed documents (receipts, letters). Off by default: most photos aren't documents. |

OCR runs on your computer with [RapidOCR](https://github.com/RapidAI/RapidOCR). Its models come
with the install, so nothing is downloaded and nothing leaves your computer. Expect about a
second per page. CSV and TSV files are split so that every chunk starts with the header row,
so a bank statement line found by search keeps its column names. Evernote exports (`.enex`)
are read note by note.

By default graph-me skips folders that are noise, not personal data: `.git`, `node_modules`,
virtual environments, caches, build output of code projects (`dist`, `build`, `target` next to a
`package.json`, `pyproject.toml` and the like), lockfiles and minified files. To keep a folder out
for another reason, put an empty file named `.graph-me-ignore` in it, or use the
[blacklist](#blacklist).

### `msgvault`

Reads mail and chats archived by [msgvault](https://github.com/kenn-io/msgvault).

| Key | Meaning |
| --- | --- |
| `db` | msgvault's folder (default `~/.msgvault`) or its `msgvault.db` file |
| `accounts` | Only these accounts (emails or phone numbers). Default: all. |
| `contacts` | Also read the address book synced into msgvault (default `true`) |

### `vcard`

Reads `.vcf` address books (Google Contacts, iCloud and phone exports).

| Key | Meaning |
| --- | --- |
| `paths` | `.vcf` files or folders containing them |

### `ics`

Reads calendar files: exports from any calendar app (Calendar, Outlook, Google Calendar's
"Export"), or subscribed calendars saved to disk.

| Key | Meaning |
| --- | --- |
| `paths` | `.ics` files or folders containing them |
| `trust` | `known` by default: invitations come from other people |

Each event becomes an item. The organizer and attendees become people, linked by `met`, so
`graph-me who Sam` says when you last met. A recurring event is one item that says how it
repeats. A file that looks cut off (still being written) is skipped for that run, never taken
as a calendar whose events were deleted.

### `macos-calendar`

Reads the macOS Calendar app directly, with every account it shows (iCloud, Google, Exchange...),
through Apple's EventKit. Needs `graph-me[macos]`, on macOS only.

| Key | Meaning |
| --- | --- |
| `calendars` | Only these calendars, by name (default: all) |
| `past_days` | How far back to read (default 730). Older events are forgotten like deleted ones. |
| `future_days` | How far ahead (default 365) |
| `trust` | `known` by default |

The first `scan` asks macOS for access to your calendars. macOS calls it "full access", which
would also allow changes; graph-me only reads. If you refuse, or a scheduled sync can't ask,
the source is skipped and nothing is forgotten. You can change your answer in System Settings >
Privacy & Security > Calendars.

## People

| Key | Meaning |
| --- | --- |
| `phone_country_code` | Your country's calling code (`"44"`, `"1"`, `"33"`...). Without it, a number written `07700 900123` in your contacts won't match the same person on WhatsApp. |
| `me` | Your own emails and phone numbers, if a source can't tell which messages are yours |
| `timezone` | Decides which day a message was sent (a birthday wish at 00:30 counts for that day). Default: your computer's. |

graph-me treats two contacts as the same person only if they share an email or a phone number.
Two "Sophie" stay two people.

## Corrections

`overrides.yaml`, next to `config.yaml`, holds your corrections. The `link`, `unlink`, `relate`
and `fact --set/--reject` commands edit it (see [Commands](commands.md#correct)), and you can
edit it by hand:

```yaml
people:
  - who: sophie@example.com          # an email or phone number
    same_as: ["+44 7700 900123"]     # the same person
    relation: sibling                # how they relate to you
    name: Sophie Martin
    facts: {birthday: "03-12"}
not_same:
  - [sophie@work.example, sophie@example.com]
reject:
  - {who: sophie@example.com, fact: birthday, value: "07-04"}
```

| Key in `config.yaml` | Meaning |
| --- | --- |
| `overrides` | Where your corrections are saved. Default: `overrides.yaml` next to `config.yaml`. `null` turns corrections off. |

If the file has an error, `scan` and `sync` say so and keep your previous corrections.

## Index

| Key | Meaning |
| --- | --- |
| `index.stale_after_days` | Answers say the index may be out of date when a source was last synced longer ago than this (default 7) |

## Profiles

A profile limits what one AI assistant can see to some of your sources. Give your coding
assistant your work documents and mail, and keep your family chats out of its reach:

```yaml
profiles:
  work:
    sources: [work-docs, work-mail]   # names under `sources:`
    kinds: [file, email]              # optional: only these kinds of items
```

Then register it with `graph-me install-mcp --profile work`, or start it with
`graph-me mcp --profile work`. Under a profile, every answer comes only from those sources:
search, documents, people and their facts, the graph, what's coming up, the status page. A person
who appears in both worlds is shown with only what the profile's sources say about them: a
colleague's work email, not the personal phone number from a family chat. Your corrections
(`overrides.yaml`) stay out unless you add `_overrides` to the profile's sources.

The profile is set when the server starts, by the app's config: the assistant can't switch it.

## Activity log

| Key | Meaning |
| --- | --- |
| `log.keep_days` | How long `graph-me log` keeps what was asked (default 90; 0 keeps nothing) |

## Export

| Key | Meaning |
| --- | --- |
| `export.ics` | `true` rewrites `graph-out/upcoming.ics` (birthdays, anniversaries, deadlines) after every scan and sync, for your calendar app (default `false`) |

## Blacklist

| Key | Meaning |
| --- | --- |
| `paths` | Folders and files never to index |
| `contacts` | Emails or phone numbers. graph-me drops every item involving that person. |
| `patterns` | Regular expressions. graph-me drops items whose text matches. |

When you add something to the blacklist, the next `scan` or `sync` also removes what it already
indexed.
