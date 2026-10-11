# graph-me-connector-example

A minimal [graph-me](https://github.com/MathieuSlt/graph-me) connector, to copy when you write
your own. It reads notes from a JSON Lines file.

```bash
uv pip install -e .            # in the environment graph-me runs in
uv run pytest                  # runs graph-me's connector contract on it
```

Then in `config.yaml`:

```yaml
sources:
  notes:
    type: notes-jsonl
    path: ~/notes.jsonl
```

See [Write a connector](https://mathieuslt.github.io/graph-me/contributing-connectors/).
