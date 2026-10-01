# Web UI

```bash
uv tool install --managed-python "graph-me[ui]"
graph-me ui
```

This opens a page in your browser with four parts:

- **Search** across files, emails, chats and contacts, filtered by kind, source and date.
- **Graph** of people, projects and documents. Click a node to see its neighbours, double-click
  to open its page.
- **Person and project pages** with identifiers, facts, relations and recent items. Each fact
  has a "Why do I know this?" list of the files and messages behind it.
- **Status** of your sources, the last runs, the blacklist, and items flagged as suspicious.

!!! warning "Don't share the address"
    The page only works on your computer (it listens on 127.0.0.1), and its address contains a
    random key that changes every time you start it. Anyone with that address can read your
    index while `graph-me ui` runs. Press Ctrl+C to stop.

| Option | Meaning |
| --- | --- |
| `--port <n>` | Use this port (default: any free port) |
| `--no-open` | Print the address without opening the browser |
