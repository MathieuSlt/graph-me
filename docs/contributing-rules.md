# Add a language

graph-me finds birthdays, relations ("my sister"), deadlines and amounts with rules written per
language, in rule packs: `rules/<code>.yaml`. English and French come built in. A pack is plain
YAML, no code.

## Start from the template

```bash
mkdir -p ~/graph-me/rules
graph-me rules template > ~/graph-me/rules/de.yaml
```

Fill in what you know. Every section is optional:

| Section | What it does |
| --- | --- |
| `stopwords` | Frequent short words: detect the language of a text and are ignored in searches |
| `injection` | Phrases that flag a text as a possible prompt injection ("ignore previous instructions") |
| `birthday.greetings` / `exclude` | A wish sent to one person suggests their birthday; `exclude` words (late, tomorrow) cancel it |
| `relations` | Words for "sister", "landlord"... mapped to a label (`sibling`, `landlord`...) |
| `date_order` | `DMY`, `MDY` or `YMD`: how to read 03/04/2026 |
| `deadlines` | Phrases before a date: `expires`, `renews`, `due`, `ends` |
| `amounts` | Words before an amount: `quote`, `invoice` |
| `examples` | Sentences that must match or not: they test the pack |

## Check it

```bash
graph-me rules check ~/graph-me/rules/de.yaml
```

It validates the file and runs its examples, with the other packs loaded too. Add an example for
every phrase: the test suite runs every pack's examples, so a change that breaks one is caught.

```yaml
examples:
  birthday: {match: ["Alles Gute zum Geburtstag!"], no_match: ["Alles Gute zum Umzug"]}
  values:
    - {text: "gültig bis 31.03.2027", expect: {expires: "2027-03-31"}}
  relations: {meine Schwester: sibling}
```

## Use it, share it

Packs in `rules/` next to your `config.yaml` load on every run. A pack for a language that
already exists (say `en.yaml` with one more relation word) adds its words to the built-in ones.
The next `scan` applies new deadline and amount rules to what is already indexed, from the text
it already has. Files starting with `_` are skipped.

To share a language, copy the pack to `src/graph_me/rules/` in the repository and open a pull
request. `uv run pytest` runs its examples.
