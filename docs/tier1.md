# Better results with AI (Tier 1)

Out of the box, graph-me uses no AI at all (Tier 0). It matches words, so "rental contract"
won't find a file named `apartment_2025.pdf`. Tier 1 fixes most of that:

- An AI model labels your file names, folder names and contacts with a type, a topic and
  keywords in English and French. It also guesses how contacts relate to you ("sibling",
  "landlord"), so "my sister's birthday" finds her without her name.
- A small model running on your computer adds meaning-based search, so "car insurance" finds
  "vehicle policy renewal". It downloads once (about 220 MB).

!!! info "What leaves your computer"
    Tier 1 only sends short strings (names of files, folders and contacts) to the AI, never the
    content of your documents or messages. That keeps it cheap on large archives. With the
    `ollama` provider, nothing leaves your computer at all.

```bash
uv tool install --managed-python "graph-me[medium]"
graph-me enrich --dry-run     # shows how much there is to label
graph-me enrich               # labels, then builds meaning-based search
```

## Providers

You choose who does the labelling, with `--llm` or in `config.yaml`
(`extraction.medium.llm.provider`):

| Provider | How it works |
| --- | --- |
| `agent` (default) | Your AI assistant does it. No API key needed. In Claude Code, ask "use graph-me to enrich the db". |
| `ollama` | A local model through [Ollama](https://ollama.com) (`qwen3:4b` by default). Nothing leaves your computer. |
| `anthropic` | The Claude API. Needs `ANTHROPIC_API_KEY`. |
| `openai_compat` | Any OpenAI-compatible server (set `model`, `base_url`, `api_key_env`). |

graph-me asks before it sends anything to an API. It checks every answer against a strict list
of allowed labels and rejects the rest, so a booby-trapped file name can at worst get a wrong
label.

## The agent workflow

With the `agent` provider, `graph-me enrich` writes batch files to `graph-out/work/`. Your
assistant answers them, then `graph-me ingest --all` merges the answers. The skill does these
steps for you.

See [`enrich` and `ingest`](commands.md#ai-enrichment) for every option.
