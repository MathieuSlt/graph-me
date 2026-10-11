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

## Blacklist

| Key | Meaning |
| --- | --- |
| `paths` | Folders and files never to index |
| `contacts` | Emails or phone numbers. graph-me drops every item involving that person. |
| `patterns` | Regular expressions. graph-me drops items whose text matches. |

When you add something to the blacklist, the next `scan` or `sync` also removes what it already
indexed.
