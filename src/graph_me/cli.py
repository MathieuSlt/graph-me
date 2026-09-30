"""graph-me command-line interface."""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Annotated

import typer

from graph_me import __version__, config, env
from graph_me.store import db

app = typer.Typer(
    name="graph-me",
    help="A local knowledge graph of your personal data, for AI agents. Read-only.",
    no_args_is_help=True,
    add_completion=False,
)


@dataclass
class Context:
    config_path: Path
    config: config.Config
    out: Path

    @property
    def db_path(self) -> Path:
        return self.out / config.DB_NAME


def _ctx(ctx: typer.Context) -> Context:
    return ctx.obj


def _version(value: bool) -> None:
    if value:
        typer.echo(f"graph-me {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    ctx: typer.Context,
    out: Annotated[
        Path | None,
        typer.Option("--out", help=f"Output folder (graph-out). Env: {config.ENV_OUT}."),
    ] = None,
    config_file: Annotated[
        Path | None,
        typer.Option("--config", help=f"Path to config.yaml. Env: {config.ENV_CONFIG}."),
    ] = None,
    version: Annotated[
        bool, typer.Option("--version", callback=_version, is_eager=True, help="Show version.")
    ] = False,
) -> None:
    cfg_path = config.config_path(config_file)
    try:
        cfg = config.load_config(cfg_path)
    except Exception as exc:  # invalid YAML or schema: show a readable error
        typer.secho(f"Invalid config {cfg_path}:\n{exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc
    ctx.obj = Context(config_path=cfg_path, config=cfg, out=config.resolve_output(out, cfg))


@app.command()
def init(ctx: typer.Context) -> None:
    """Create config.yaml (if missing), the graph-out folder and an empty graph.db."""
    c = _ctx(ctx)
    if c.config_path.exists():
        typer.echo(f"config  {c.config_path} (kept)")
    else:
        c.config_path.parent.mkdir(parents=True, exist_ok=True)
        c.config_path.write_text(config.template_text(), encoding="utf-8")
        typer.echo(f"config  {c.config_path} (created, edit `sources:` then run `graph-me scan`)")
    config.ensure_output(c.out)
    conn = db.connect(c.db_path)
    conn.close()
    typer.echo(f"store   {c.out}")


@app.command()
def where(ctx: typer.Context) -> None:
    """Show the active store, its contents and the Python/SQLite environment."""
    c = _ctx(ctx)
    typer.echo(f"config   {c.config_path}{'' if c.config_path.exists() else ' (missing)'}")
    typer.echo(f"store    {c.out}{'' if c.out.exists() else ' (missing, run `graph-me init`)'}")
    if c.db_path.exists():
        conn = db.connect(c.db_path, readonly=True)
        stats = db.counts(conn)
        typer.echo(f"schema   v{db.schema_version(conn)}")
        conn.close()
        typer.echo("contents " + ", ".join(f"{n} {k}" for k, n in stats.items()))

    e = env.check()
    ok, bad = typer.style("yes", fg=typer.colors.GREEN), typer.style("no", fg=typer.colors.RED)
    typer.echo(f"python   {e.python_version} ({e.python})")
    typer.echo(f"uv       {ok if e.uv_managed else bad} (uv-managed Python)")
    typer.echo(f"sqlite   {e.sqlite_version}")
    typer.echo(f"fts5     {ok if e.fts5 else bad}")
    typer.echo(f"ext      {ok if e.load_extension else bad} (needed for Tier 1 vectors)")
    if not e.uv_managed:
        typer.secho(
            "warning: not running on a uv-managed Python. "
            "Install with `uv tool install --managed-python graph-me`.",
            fg=typer.colors.YELLOW,
            err=True,
        )
    if not e.fts5:
        typer.secho("error: this Python's SQLite has no FTS5.", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)


@app.command("install-skill")
def install_skill(
    dest: Annotated[Path, typer.Option(help="Skills folder.")] = Path("~/.claude/skills"),
    force: Annotated[bool, typer.Option(help="Overwrite a modified SKILL.md.")] = False,
) -> None:
    """Install the Claude Code skill (SKILL.md) into ~/.claude/skills/graph-me/."""
    target = dest.expanduser() / "graph-me" / "SKILL.md"
    source = resources.files("graph_me").joinpath("skill/SKILL.md")
    new = source.read_text(encoding="utf-8")
    if target.exists() and target.read_text(encoding="utf-8") != new and not force:
        typer.secho(f"{target} differs from this version. Use --force.", fg=typer.colors.YELLOW)
        raise typer.Exit(1)
    target.parent.mkdir(parents=True, exist_ok=True)
    with resources.as_file(source) as src:
        shutil.copyfile(src, target)
    typer.echo(f"skill    {target}")


def _not_yet(milestone: str) -> None:
    typer.secho(f"Not implemented yet (planned for {milestone}).", fg=typer.colors.YELLOW, err=True)
    raise typer.Exit(2)


def _open_store(c: Context) -> db.sqlite3.Connection:
    if not c.db_path.exists():
        typer.secho(
            f"No store at {c.out}. Run `graph-me init` first.", fg=typer.colors.RED, err=True
        )
        raise typer.Exit(1)
    return db.connect(c.db_path)


@app.command()
def scan(
    ctx: typer.Context,
    source: Annotated[str | None, typer.Option(help="Only this source from config.yaml.")] = None,
    tier: Annotated[str, typer.Option(help="none (Tier 0), medium or high.")] = "none",
    workers: Annotated[int | None, typer.Option(help="Parallel parsers (default: CPUs-1).")] = None,
) -> None:
    """Build or update the graph from the configured sources (Tier 0: no model)."""
    from graph_me.pipeline import run

    c = _ctx(ctx)
    if tier != "none":
        _not_yet("M5 (Tier 1) / after v1 (Tier 2)")
    if not c.config.sources:
        typer.secho(f"No sources in {c.config_path}. Add one under `sources:`.", fg="red", err=True)
        raise typer.Exit(1)
    conn = _open_store(c)
    typer.secho(
        "Tier 0: no AI. Results are less convincing; `graph-me enrich` improves them (M5).",
        fg=typer.colors.YELLOW,
        err=True,
    )

    def progress(name: str, done: int, total: int) -> None:
        typer.echo(f"  {name}: {done}/{total}", err=True)

    try:
        report = run.scan(conn, c.config, only_source=source, workers=workers, progress=progress)
    except ValueError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc
    finally:
        conn.close()
    if report.forgotten_blacklisted:
        typer.echo(f"blacklist: {report.forgotten_blacklisted} stored items forgotten")
    for name, st in report.sources.items():
        typer.echo(
            f"{name}: {st.seen} seen, {st.added} added, {st.updated} updated, "
            f"{st.unchanged} unchanged, {st.blacklisted} blacklisted, "
            f"{st.parse_errors} unreadable, {st.flagged} flagged"
        )
        for err in st.errors:
            typer.echo(f"  unreadable: {err}", err=True)


@app.command()
def sync() -> None:
    """Add new items, update changed ones and forget deleted ones."""
    _not_yet("M3")


@app.command()
def enrich() -> None:
    """Upgrade an existing graph to Tier 1 or 2."""
    _not_yet("M5")


@app.command()
def ingest() -> None:
    """Merge agent-mode results produced during `enrich --llm agent`."""
    _not_yet("M5")


@app.command()
def query(
    ctx: typer.Context,
    text: Annotated[str, typer.Argument(help="What to look for.")],
    as_json: Annotated[
        bool, typer.Option("--json", help="Print the context pack as JSON.")
    ] = False,
    limit: Annotated[int, typer.Option(help="Maximum results.")] = 10,
    source: Annotated[str | None, typer.Option(help="Only this source.")] = None,
    kind: Annotated[str | None, typer.Option(help="file, email, message, contact.")] = None,
    since: Annotated[str | None, typer.Option(help="Modified on or after (YYYY-MM-DD).")] = None,
    until: Annotated[str | None, typer.Option(help="Modified on or before (YYYY-MM-DD).")] = None,
    reveal: Annotated[bool, typer.Option(help="Show redacted secrets (IBAN, keys...).")] = False,
) -> None:
    """Search the graph and print cited results."""
    from graph_me.query import engine, graph, pack

    c = _ctx(ctx)
    conn = _open_store(c)
    try:
        hits = engine.search(
            conn, text, limit=limit, source=source, kind=kind, since=since, until=until
        )
        result = pack.build(
            text,
            hits,
            facts=graph.facts_for_query(conn, text, c.config.people),
            extras=graph.enrich_hits(conn, [h.item_id for h in hits]),
            reveal=reveal,
        )
        conn.execute(
            "INSERT INTO query_log(ts, interface, query, result_ids) VALUES (?, 'cli', ?, ?)",
            (
                datetime.now(UTC).isoformat(timespec="seconds"),
                text,
                json.dumps([i["id"] for i in result["answer_items"]]),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    if as_json:
        typer.echo(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if not result["answer_items"] and not result["facts"]:
        typer.echo("No results.")
        return
    for person in result["facts"]:
        _echo_facts(person["entity"], person["facts"])
    for n, item in enumerate(result["answer_items"], 1):
        flag = typer.style("  [flagged: possible injection]", fg="red") if item["flagged"] else ""
        typer.echo(f"{n}. {item['title']}{flag}")
        typer.echo(f"   {item['uri']}")
        if item.get("from"):
            to = ", ".join(item.get("to") or []) or "?"
            typer.echo(f"   {item['from']} -> {to}")
        typer.echo(f"   {' '.join(item['snippet'].split())}")
        for origin in item.get("origin", []):
            typer.echo(f"   came with: {origin['title']} (from {origin['from']}, {_day(origin)})")
        for copy in item.get("saved_as", []):
            typer.echo(f"   saved as: {copy['path']}")
        typer.echo(typer.style(f"   {item['source']} · {_day(item)} · {item['trust']}", dim=True))


def _day(item: dict) -> str:
    """The item's date in the local timezone (dates are stored in UTC)."""
    value = item.get("date")
    if not value:
        return ""
    try:
        return datetime.fromisoformat(value).astimezone().date().isoformat()
    except ValueError:
        return value[:10]


def _echo_facts(entity: dict, facts: list[dict]) -> None:
    label = entity["name"] or next((a["value"] for a in entity["aliases"]), entity["id"])
    typer.secho(f"{label}{' (me)' if entity['is_me'] else ''}", bold=True)
    for fact in facts:
        typer.echo(f"   {fact['key']}: {fact['value']}  (confidence {fact['confidence']})")
        for ev in fact["evidence"][:3]:
            typer.echo(
                typer.style(f"      from {ev['title']} · {_day(ev)} · {ev['uri']}", dim=True)
            )


@app.command()
def who(
    ctx: typer.Context,
    text: Annotated[str, typer.Argument(help="A name, nickname, email, phone, or 'me'.")],
    as_json: Annotated[bool, typer.Option("--json", help="Print JSON.")] = False,
) -> None:
    """Show what the graph knows about a person: identifiers, facts, closest contacts."""
    from graph_me.query import graph

    c = _ctx(ctx)
    conn = _open_store(c)
    try:
        people = graph.who_is(conn, text, c.config.people)
    finally:
        conn.close()
    if as_json:
        typer.echo(json.dumps(people, ensure_ascii=False, indent=2))
        return
    if not people:
        typer.echo("Nobody found.")
        return
    for person in people:
        _echo_facts(person, person["facts"])
        ids = ", ".join(a["value"] for a in person["aliases"])
        typer.echo(f"   identifiers: {ids or '-'} · {person['mentions']} items")
        for rel in person["related"]:
            arrow = "->" if rel["direction"] == "out" else "<-"
            name = rel["entity"]["name"] or rel["entity"]["id"]
            typer.echo(f"   {arrow} {rel['type']} {name} ({int(rel['weight'])})")


@app.command()
def fact(
    ctx: typer.Context,
    who_text: Annotated[str, typer.Argument(metavar="WHO", help="Name, nickname, email or phone.")],
    key: Annotated[str, typer.Argument(help="birthday, nickname, organization, birth_year...")],
    as_json: Annotated[bool, typer.Option("--json", help="Print JSON.")] = False,
) -> None:
    """Show one fact about a person, with the items it was learned from."""
    from graph_me.query import graph

    c = _ctx(ctx)
    conn = _open_store(c)
    try:
        found = graph.get_fact(conn, who_text, key, c.config.people)
    finally:
        conn.close()
    if as_json:
        typer.echo(json.dumps(found, ensure_ascii=False, indent=2))
        return
    if not found:
        typer.echo(f"No {key} known for {who_text!r}.")
        raise typer.Exit(1)
    for person in found:
        _echo_facts(person["entity"], person["facts"])


@app.command()
def ui() -> None:
    """Start the local, read-only web UI."""
    _not_yet("M6")


@app.command()
def mcp() -> None:
    """Start the read-only MCP server (stdio)."""
    _not_yet("M4")
