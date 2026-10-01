# Quick start

## 1. Create the configuration

```bash
graph-me init
```

This writes `~/graph-me/config.yaml` and creates `~/graph-me/graph-out/`, where the index lives.

## 2. Tell graph-me what to read

Open `~/graph-me/config.yaml` and list your sources:

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
graph-me scan
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
deleted ones. Nothing runs in the background.

## Next steps

- [Use it from your AI assistant](assistants.md)
- [Browse it in the web UI](web-ui.md)
- [Get better results with AI (Tier 1)](tier1.md)
