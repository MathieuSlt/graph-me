# Quick start

Want to see it first? `graph-me demo` builds a graph from made-up data and prints questions to
try on it.

## 1. Create the configuration

```bash
graph-me init --detect
```

graph-me looks for your Documents, Desktop and Downloads folders, Obsidian vaults, msgvault and
`.vcf` address books, and asks which ones to index. It writes `~/graph-me/config.yaml` and
creates `~/graph-me/graph-out/`, where the index lives. (`graph-me init` alone writes a
commented template to fill in by hand.)

## 2. Check what graph-me will read

Open `~/graph-me/config.yaml` to adjust your sources:

```yaml
sources:
  docs:
    type: filesystem
    paths: [~/Documents, ~/Desktop]
  downloads:
    type: filesystem
    paths: [~/Downloads]
    trust: untrusted        # files from other people
  messages:
    type: msgvault          # needs msgvault, see Install
    db: ~/.msgvault
  contacts:
    type: vcard
    paths: [~/contacts.vcf]

people:
  phone_country_code: "44"  # your country's calling code, so 07700 900123 matches +44 7700 900123
  timezone: Europe/London

blacklist:
  paths: [~/Documents/medical]   # never indexed
```

Keep only the sources you have. [Configuration](configuration.md) covers every option, and
[`config-template.yaml`](https://github.com/MathieuSlt/graph-me/blob/main/config-template.yaml)
explains them in place.

## 3. Build the index

```bash
graph-me scan --since 90d   # the last 90 days first: answers within minutes
graph-me scan               # then everything else
graph-me doctor             # check the setup
```

The first scan reads everything, so it takes a while on a big archive. Later runs only read
what changed. When it finishes, `~/graph-me/graph-out/REPORT.md` summarizes what it indexed, the
people you deal with most, upcoming birthdays, and questions worth asking.

## 4. Ask

```bash
graph-me query "lease"
graph-me who "Sophie"
graph-me fact Sophie birthday
```

## 5. Keep it up to date

Run `graph-me sync` whenever you want. It adds new items, updates changed ones and forgets
deleted ones. To sync regularly without thinking about it:

```bash
graph-me schedule install --every 6h
```

## Next steps

- [Use it from your AI assistant](assistants.md)
- [Browse it in the web UI](web-ui.md)
- [Get better results with AI (Tier 1)](tier1.md)
