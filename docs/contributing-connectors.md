# Write a connector

A connector teaches graph-me to read a new source: a notes app, a chat export, a service's
archive. It's a small Python class in a package of its own. graph-me finds it through an entry
point, so `uv tool install graph-me --with graph-me-yourthing` makes `type: yourthing` available
in `config.yaml`.

The fastest start is the example plugin in the repository,
[`examples/graph-me-connector-example`](https://github.com/MathieuSlt/graph-me/tree/main/examples/graph-me-connector-example).
It reads notes from a JSON Lines file. Copy it and rename it.

## The two methods

```python
class MyConnector:
    type = "yourthing"

    def __init__(self, name: str, source: SourceConfig, blacklist: BlacklistConfig): ...
    def list_ids(self) -> Iterator[tuple[str, str]]: ...      # (external_id, version)
    def fetch(self, external_ids: Iterable[str]) -> Iterator[Item]: ...
```

- `list_ids` lists **everything that exists now**, as `(external_id, version)` pairs. The
  version is any string that changes when the item changes (a modification time, a hash).
  graph-me reads an item again only when its version changes.
- `fetch` returns the full `Item` for each id it's given, and skips ids that have vanished
  since. Give an item its `text`, or a `path` for graph-me to read like a file. Use
  `author` / `recipients` (`Party` with name, email, phone or handle) for messages, `contact`
  for address-book entries, `kind` for what it is (`file`, `email`, `message`, `contact`,
  `event`).
- Your keys under `sources.<name>` in `config.yaml` arrive in `source.model_extra`.

Optional: `close()` to release what you hold open; `newer_than(id, version, cutoff)` so
`scan --since` can skip old items; `excluded(id)` for ids the config now leaves out (they are
forgotten without counting toward the mass-forget guard).

## The rules

graph-me computes what was added, changed and deleted itself. Your connector never deletes
anything. In exchange, it must keep four promises:

1. **Stable versions.** Listing twice with nothing changed gives the same pairs.
2. **`fetch` matches `list_ids`.** Same versions, and unknown ids are skipped.
3. **An unreachable source raises `SourceUnavailable`.** Never list nothing when a folder is
   missing, a database can't be opened or a file is cut off. `sync` would forget everything.
4. **Valid items.** A text or a path, a known kind, timezone-aware dates.

And graph-me's own: read-only (never write to the source), and treat everything you read as
data, not instructions.

## Test it

`graph_me.testing.check_connector` checks the four rules:

```python
from graph_me.testing import check_connector

def test_contract(tmp_path):
    write_some_data(tmp_path)
    check_connector(
        lambda: MyConnector("s", SourceConfig(type="yourthing", path=str(tmp_path)), BlacklistConfig()),
        unreachable=lambda: MyConnector("s", SourceConfig(type="yourthing", path="/nope"), BlacklistConfig()),
    )
```

graph-me runs the same checks on its own connectors.

## Register it

```toml
[project.entry-points."graph_me.connectors"]
yourthing = "your_package:MyConnector"
```

Name the package `graph-me-<source>` so people can find it. Built-in connectors live in
`src/graph_me/connectors/`. A connector that many people need can be proposed there too; open
an issue first.
