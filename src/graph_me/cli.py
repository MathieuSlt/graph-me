"""graph-me command-line interface."""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from datetime import datetime
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


def _service(c: Context, interface: str = "cli"):
    from graph_me.service import Service

    return Service(c.config, c.db_path, interface)


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
    """Build or update the graph from the configured sources (Tier 0: no model).

    Adds new items and updates changed ones. Use `sync` to also forget deleted ones.
    """
    if tier != "none":
        _not_yet("M5 (Tier 1) / after v1 (Tier 2)")
    _run_pipeline(ctx, source=source, workers=workers, forget=False)


@app.command()
def sync(
    ctx: typer.Context,
    source: Annotated[str | None, typer.Option(help="Only this source from config.yaml.")] = None,
    workers: Annotated[int | None, typer.Option(help="Parallel parsers (default: CPUs-1).")] = None,
    allow_mass_forget: Annotated[
        bool,
        typer.Option(
            help="Allow forgetting more than half of a source (big deletions, removed sources)."
        ),  # fmt: skip
    ] = False,
) -> None:
    """Mirror your sources: add new items, update changed ones, forget deleted ones.

    Deleted files, messages deleted in msgvault and sources removed from config.yaml are
    forgotten, with everything learned only from them. An unreachable source (unplugged drive,
    missing msgvault database) is skipped, never wiped.
    """
    _run_pipeline(
        ctx, source=source, workers=workers, forget=True, allow_mass_forget=allow_mass_forget
    )


def _run_pipeline(
    ctx: typer.Context,
    *,
    source: str | None,
    workers: int | None,
    forget: bool,
    allow_mass_forget: bool = False,
) -> None:
    from graph_me.pipeline import run

    c = _ctx(ctx)
    if not c.config.sources:
        typer.secho(f"No sources in {c.config_path}. Add one under `sources:`.", fg="red", err=True)
        raise typer.Exit(1)
    conn = _open_store(c)
    typer.secho(
        "Tier 0: no AI. Results are less convincing; `graph-me enrich` improves them.",
        fg=typer.colors.YELLOW,
        err=True,
    )

    def progress(name: str, done: int, total: int) -> None:
        typer.echo(f"  {name}: {done}/{total}", err=True)

    try:
        if forget:
            report = run.sync(
                conn,
                c.config,
                only_source=source,
                workers=workers,
                progress=progress,
                allow_mass_forget=allow_mass_forget,
            )
        else:
            report = run.scan(
                conn, c.config, only_source=source, workers=workers, progress=progress
            )
    except ValueError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc
    else:
        from graph_me import report as report_mod

        report_mod.write(conn, c.out)
    finally:
        conn.close()

    if report.forgotten_blacklisted:
        typer.echo(f"blacklist: {report.forgotten_blacklisted} stored items forgotten")
    for name, count in report.removed_sources.items():
        typer.echo(f"{name}: removed from config.yaml, {count} items forgotten")
    for name, message in report.failures.items():
        typer.secho(f"{name}: kept. {message}", fg=typer.colors.RED, err=True)
    for name, st in report.sources.items():
        if st.failure:
            typer.secho(f"{name}: skipped. {st.failure}", fg=typer.colors.RED, err=True)
            continue
        forgotten = f"{st.forgotten} forgotten, " if forget else ""
        typer.echo(
            f"{name}: {st.seen} seen, {st.added} added, {st.updated} updated, {forgotten}"
            f"{st.unchanged} unchanged, {st.blacklisted} blacklisted, "
            f"{st.parse_errors} unreadable, {st.flagged} flagged"
        )
        for err in st.errors:
            typer.echo(f"  unreadable: {err}", err=True)
    if not report.ok:
        raise typer.Exit(1)


@app.command()
def enrich(
    ctx: typer.Context,
    tier: Annotated[
        str, typer.Option(help="medium (Tier 1). high (Tier 2) comes after v1.")
    ] = "medium",
    llm: Annotated[
        str | None,
        typer.Option(
            help="agent (default: your agent labels batch files), ollama, anthropic, "
            "openai_compat. Overrides extraction.medium.llm.provider."
        ),  # fmt: skip
    ] = None,
    source: Annotated[str | None, typer.Option(help="Only items of this source.")] = None,
    since: Annotated[str | None, typer.Option(help="Only items dated on/after YYYY-MM-DD.")] = None,
    path: Annotated[str | None, typer.Option(help="Only files under this folder.")] = None,
    batch_size: Annotated[int, typer.Option(help="Strings per batch.")] = 50,
    dry_run: Annotated[bool, typer.Option(help="Show what would be done and its size.")] = False,
    status: Annotated[bool, typer.Option(help="List agent-mode batches not ingested yet.")] = False,
    no_embeddings: Annotated[bool, typer.Option(help="Skip meaning-based vectors.")] = False,
    yes: Annotated[bool, typer.Option(help="Don't ask before sending data to an API.")] = False,
) -> None:
    """Tier 1: label file names and contacts with an AI model, add meaning-based search.

    Only short strings are labelled (file and folder names, contact cards): cheap even for
    large archives. Every answer is validated against a strict schema before it is kept.
    """
    from graph_me import llm as llm_mod
    from graph_me import report as report_mod
    from graph_me.pipeline import enrich as enrich_mod

    c = _ctx(ctx)
    if tier != "medium":
        _not_yet("after v1 (Tier 2)")
    if status:
        pending = enrich_mod.pending_batches(c.out)
        if not pending:
            typer.echo("No pending batches.")
        for batch in pending:
            answered = batch.with_name(batch.name.replace(".json", ".out.json")).exists()
            typer.echo(f"{batch}  {'answered, run ingest' if answered else 'waiting for answer'}")
        return
    conn = _open_store(c)
    scope = enrich_mod.Scope(source=source, since=since, path=path)
    provider = llm or (c.config.extraction.medium.llm.provider
                       if c.config.extraction.medium.llm else "agent")  # fmt: skip
    try:
        plan = enrich_mod.enrich(conn, c.config, c.out, provider=provider, scope=scope,
                                 batch_size=batch_size, dry_run=True,
                                 embeddings=not no_embeddings)  # fmt: skip
        est = plan.estimate
        typer.echo(
            f"to label: {plan.candidates.get('files', 0)} file names, "
            f"{plan.candidates.get('contacts', 0)} contacts in {est['batches']} batches "
            f"(~{est['input_tokens']} input / ~{est['output_tokens']} output tokens); "
            f"to embed: {est['chunks_to_embed']} chunks "
            f"({c.config.extraction.medium.embeddings or 'local'} model, on this computer)"
        )
        if dry_run:
            return
        if provider in llm_mod.REMOTE and est["strings"] and not yes:
            typer.confirm(
                f"File names and contact cards will be sent to {provider}. Continue?", abort=True
            )

        def progress(task: str, done: int, total: int) -> None:
            typer.echo(f"  {task}: {done}/{total}", err=True)

        result = enrich_mod.enrich(conn, c.config, c.out, provider=provider, scope=scope,
                                   batch_size=batch_size, embeddings=not no_embeddings,
                                   progress=progress)  # fmt: skip
        report_mod.write(conn, c.out)
    except llm_mod.LLMError as exc:
        typer.secho(f"model error: {exc}", fg="red", err=True)
        raise typer.Exit(1) from exc
    finally:
        conn.close()

    if result.batches_written:
        typer.echo(f"{len(result.batches_written)} batch files written to {c.out / 'work'}.")
        typer.echo(
            "Answer each batch-NNNN.json in batch-NNNN.out.json (see its instructions and "
            "answer_schema), then run: graph-me ingest --all"
        )
    elif provider != "agent":
        extras = []
        if result.stale:
            extras.append(f"{result.stale} stale")
        if result.rejected:
            extras.append(f"{len(result.rejected)} answers rejected")
        suffix = f" ({', '.join(extras)})" if extras else ""
        typer.echo(f"labelled {result.labelled} items with {result.provider}{suffix}")
    if result.embeddings:
        typer.echo(f"embeddings: {result.embedded} chunks ({result.embeddings})")


@app.command()
def ingest(
    ctx: typer.Context,
    answers: Annotated[
        list[Path] | None, typer.Argument(help="batch-NNNN.out.json files to merge.")
    ] = None,
    all_answers: Annotated[
        bool, typer.Option("--all", help="Ingest every answered batch in graph-out/work.")
    ] = False,
) -> None:
    """Merge agent-mode answers written for `graph-me enrich` batches (validated first)."""
    from graph_me import report as report_mod
    from graph_me.pipeline import enrich as enrich_mod

    c = _ctx(ctx)
    paths = list(answers or [])
    if all_answers:
        paths += [
            b.with_name(b.name.replace(".json", ".out.json"))
            for b in enrich_mod.pending_batches(c.out)
            if b.with_name(b.name.replace(".json", ".out.json")).exists()
        ]
    if not paths:
        typer.echo("Nothing to ingest (answer a batch first, see `graph-me enrich --status`).")
        raise typer.Exit(1)
    conn = _open_store(c)
    failed = False
    try:
        for answer in paths:
            try:
                r = enrich_mod.ingest(conn, c.out, answer)
            except ValueError as exc:
                typer.secho(f"{answer.name}: {exc}", fg="red", err=True)
                failed = True
                continue
            typer.echo(
                f"batch {r.batch}: {r.applied} labels applied"
                f"{f', {r.stale} stale' if r.stale else ''}"
                f"{f', {len(r.rejected)} rejected' if r.rejected else ''}"
            )
            for reason in r.rejected[:5]:
                typer.echo(f"  rejected: {reason}", err=True)
        report_mod.write(conn, c.out)
    finally:
        conn.close()
    if failed:
        raise typer.Exit(1)


@app.command()
def query(
    ctx: typer.Context,
    text: Annotated[str, typer.Argument(help="What to look for.")],
    as_json: Annotated[
        bool, typer.Option("--json", help="Print the context pack as JSON (= --format json).")
    ] = False,
    fmt: Annotated[
        str, typer.Option("--format", help="text, json, or markdown (for agents).")
    ] = "text",
    limit: Annotated[int, typer.Option(help="Maximum results.")] = 10,
    source: Annotated[str | None, typer.Option(help="Only this source.")] = None,
    kind: Annotated[str | None, typer.Option(help="file, email, message, contact.")] = None,
    since: Annotated[str | None, typer.Option(help="Modified on or after (YYYY-MM-DD).")] = None,
    until: Annotated[str | None, typer.Option(help="Modified on or before (YYYY-MM-DD).")] = None,
    reveal: Annotated[bool, typer.Option(help="Show redacted secrets (IBAN, keys...).")] = False,
) -> None:
    """Search the graph and print cited results."""
    from graph_me.query import pack

    fmt = "json" if as_json else fmt
    if fmt not in ("text", "json", "markdown"):
        typer.secho("--format must be text, json or markdown.", fg="red", err=True)
        raise typer.Exit(2)
    c = _ctx(ctx)
    _open_store(c).close()
    result = _service(c).search(
        text, limit=limit, source=source, kind=kind, since=since, until=until, reveal=reveal
    )
    if fmt == "json":
        typer.echo(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if fmt == "markdown":
        typer.echo(pack.render_markdown(result), nl=False)
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
def ui(
    ctx: typer.Context,
    port: Annotated[int, typer.Option(help="Port on 127.0.0.1 (0 picks a free one).")] = 0,
    open_browser: Annotated[
        bool, typer.Option("--open/--no-open", help="Open the page in your browser.")
    ] = True,
) -> None:
    """Start the local, read-only web UI on 127.0.0.1 (search, graph, people, provenance)."""
    try:
        import uvicorn

        from graph_me.ui.app import build_app, new_token
    except ImportError:
        typer.secho(
            'The web UI needs the [ui] extra: uv tool install --managed-python "graph-me[ui]"',
            fg="red",
            err=True,
        )
        raise typer.Exit(1) from None
    import socket
    import webbrowser

    c = _ctx(ctx)
    if not c.db_path.exists():
        typer.secho(f"No store at {c.out}. Run `graph-me init` and `graph-me scan` first.",
                    fg="red", err=True)  # fmt: skip
        raise typer.Exit(1)
    token = new_token()
    # Bind first so the printed URL has the real port, then hand the socket to uvicorn.
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("127.0.0.1", port))
    except OSError as exc:
        typer.secho(f"Cannot listen on 127.0.0.1:{port}: {exc.strerror}", fg="red", err=True)
        raise typer.Exit(1) from None
    url = f"http://127.0.0.1:{sock.getsockname()[1]}/?token={token}"
    typer.echo(f"graph-me UI (read-only, this machine only): {url}")
    typer.echo("Anyone with this URL can read your graph while it runs. Ctrl+C to stop.")
    if open_browser:
        webbrowser.open(url)
    server = uvicorn.Server(
        uvicorn.Config(
            build_app(_service(c, "ui"), token), log_level="warning", access_log=False
        )  # fmt: skip
    )
    server.run(sockets=[sock])


@app.command()
def mcp(ctx: typer.Context) -> None:
    """Start the read-only MCP server on stdio (for Claude Code, Claude Desktop, Cursor...).

    Register it with: claude mcp add graph-me -- graph-me mcp
    """
    from graph_me.mcp_server import build_server

    c = _ctx(ctx)
    if not c.db_path.exists():
        typer.secho(f"No store at {c.out}. Run `graph-me init` and `graph-me scan` first.",
                    fg="red", err=True)  # fmt: skip
        raise typer.Exit(1)
    build_server(_service(c, "mcp")).run()


@app.command()
def report(ctx: typer.Context) -> None:
    """Regenerate REPORT.md and graph.json in graph-out (done after every scan and sync)."""
    from graph_me import report as report_mod

    c = _ctx(ctx)
    conn = _open_store(c)
    try:
        paths = report_mod.write(conn, c.out)
    finally:
        conn.close()
    for path in paths:
        typer.echo(f"wrote    {path}")
