"""graph-me command-line interface."""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from datetime import date, datetime
from importlib import resources
from pathlib import Path
from typing import Annotated

import typer

from graph_me import __version__, config, env
from graph_me.store import db

app = typer.Typer(
    name="graph-me",
    help=(
        "Search your own files, emails, chats and contacts, from the terminal or your AI "
        "assistant. Everything stays on this computer, and graph-me never changes your data.\n\n"
        "Getting started: run `graph-me init`, list your folders in config.yaml, then "
        '`graph-me scan` and `graph-me query "lease"`.'
    ),
    no_args_is_help=True,
    add_completion=False,
)

SETUP, INDEX, ASK, AI, INTERFACES = "Set up", "Index", "Ask", "AI enrichment", "Interfaces"
KINDS = ("file", "email", "message", "contact")
FORMATS = ("text", "json", "markdown")
PROVIDERS = ("agent", "ollama", "anthropic", "openai_compat")


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


def _plural(n: int, word: str, many: str | None = None) -> str:
    return f"{n} {word if n == 1 else many or word + 's'}"


def _error(message: str, code: int = 1) -> typer.Exit:
    typer.secho(message, fg=typer.colors.RED, err=True)
    return typer.Exit(code)


# -- option validation --------------------------------------------------------------------------


def _one_of(*allowed: str):
    def check(value: str | None) -> str | None:
        if value is not None and value not in allowed:
            raise typer.BadParameter(f"{value!r} is not one of: {', '.join(allowed)}.")
        return value

    return check


def _day(value: str | None) -> str | None:
    if value is not None:
        try:
            date.fromisoformat(value)
        except ValueError:
            raise typer.BadParameter(f"{value!r} is not a date. Use YYYY-MM-DD.") from None
    return value


def _config_error(path: Path, exc: Exception) -> str:
    """A readable message for invalid YAML or unknown/invalid options in config.yaml."""
    from pydantic import ValidationError

    if not isinstance(exc, ValidationError):
        return f"{path} is not valid YAML:\n{exc}"
    lines = [f"{path} has errors:"]
    for err in exc.errors():
        where = ".".join(str(p) for p in err["loc"])
        if err["type"] == "extra_forbidden":
            lines.append(f"  {where}: unknown option (check the spelling)")
        else:
            lines.append(f"  {where}: {err['msg'].removeprefix('Value error, ')}")
    lines.append("See config-template.yaml for every option.")
    return "\n".join(lines)


@app.callback()
def main(
    ctx: typer.Context,
    out: Annotated[
        Path | None,
        typer.Option(
            "--out",
            metavar="FOLDER",
            help=f"Folder holding the index. Default: graph-out next to config.yaml. "
            f"Env: {config.ENV_OUT}.",
        ),
    ] = None,  # fmt: skip
    config_file: Annotated[
        Path | None,
        typer.Option(
            "--config",
            metavar="FILE",
            help=f"config.yaml to use. Default: ~/graph-me/config.yaml. Env: {config.ENV_CONFIG}.",
        ),
    ] = None,  # fmt: skip
    version: Annotated[
        bool, typer.Option("--version", callback=_version, is_eager=True, help="Show the version.")
    ] = False,
) -> None:
    cfg_path = config.config_path(config_file)
    try:
        cfg = config.load_config(cfg_path)
    except Exception as exc:  # invalid YAML or schema: show a readable error
        raise _error(_config_error(cfg_path, exc)) from exc
    ctx.obj = Context(config_path=cfg_path, config=cfg, out=config.resolve_output(out, cfg))


def _require_index(c: Context) -> None:
    if not c.db_path.exists():
        raise _error(f"No index in {c.out}. Run `graph-me init`, then `graph-me scan`.")


def _open_store(c: Context) -> db.sqlite3.Connection:
    _require_index(c)
    return db.connect(c.db_path)


def _service(c: Context, interface: str = "cli"):
    from graph_me.service import Service

    return Service(c.config, c.db_path, interface)


# -- set up -------------------------------------------------------------------------------------


@app.command(rich_help_panel=SETUP)
def init(ctx: typer.Context) -> None:
    """Create config.yaml (if missing) and an empty index. Start here."""
    c = _ctx(ctx)
    created = not c.config_path.exists()
    if created:
        c.config_path.parent.mkdir(parents=True, exist_ok=True)
        c.config_path.write_text(config.template_text(), encoding="utf-8")
    config.ensure_output(c.out)
    db.connect(c.db_path).close()
    typer.echo(f"config  {c.config_path} ({'created' if created else 'already there, kept'})")
    typer.echo(f"index   {c.out}")
    typer.echo("\nNext: list your folders under `sources:` in config.yaml, then `graph-me scan`.")


@app.command(rich_help_panel=SETUP)
def where(ctx: typer.Context) -> None:
    """Show which config and index are in use, what the index holds, and check Python/SQLite."""
    c = _ctx(ctx)
    typer.echo(f"config   {c.config_path}{'' if c.config_path.exists() else ' (missing)'}")
    typer.echo(f"index    {c.out}{'' if c.out.exists() else ' (missing, run `graph-me init`)'}")
    if c.db_path.exists():
        conn = db.connect(c.db_path, readonly=True)
        stats = db.counts(conn)
        schema = db.schema_version(conn)
        conn.close()
        typer.echo(
            "contents "
            + ", ".join(
                _plural(stats[k], w)
                for k, w in (("sources", "source"), ("items", "item"), ("relations", "relation"),
                             ("facts", "fact"))
            )
            + f", {_plural(stats['entities'], 'entity', 'entities')} (schema v{schema})"
        )  # fmt: skip

    e = env.check()
    ok, bad = typer.style("yes", fg=typer.colors.GREEN), typer.style("no", fg=typer.colors.RED)
    typer.echo(f"python   {e.python_version} ({e.python})")
    typer.echo(f"uv       {ok if e.uv_managed else bad} (Python managed by uv)")
    typer.echo(f"sqlite   {e.sqlite_version}, full-text search {ok if e.fts5 else bad}, "
               f"extensions {ok if e.load_extension else bad} (needed for Tier 1)")  # fmt: skip
    if not e.uv_managed:
        typer.secho(
            "warning: not running on a uv-managed Python. "
            "Install with `uv tool install --managed-python graph-me`.",
            fg=typer.colors.YELLOW,
            err=True,
        )
    if not e.fts5:
        raise _error("error: this Python's SQLite has no full-text search (FTS5).")


@app.command("install-skill", rich_help_panel=SETUP)
def install_skill(
    dest: Annotated[
        Path, typer.Option(metavar="FOLDER", help="Skills folder to install into.")
    ] = Path("~/.claude/skills"),
    force: Annotated[bool, typer.Option("--force", help="Replace a SKILL.md you edited.")] = False,
) -> None:
    """Install the Claude Code skill, so "use graph-me" works in Claude Code."""
    target = dest.expanduser() / "graph-me" / "SKILL.md"
    source = resources.files("graph_me").joinpath("skill/SKILL.md")
    new = source.read_text(encoding="utf-8")
    if target.exists() and target.read_text(encoding="utf-8") != new and not force:
        raise _error(f"{target} was edited. Run again with --force to replace it.")
    target.parent.mkdir(parents=True, exist_ok=True)
    with resources.as_file(source) as src:
        shutil.copyfile(src, target)
    typer.echo(f"skill   {target}")
    typer.echo("\nAlso register the MCP server: claude mcp add graph-me -- graph-me mcp")


# -- index --------------------------------------------------------------------------------------

_SOURCE = typer.Option("--source", metavar="NAME", help="Only this source from config.yaml.")
_WORKERS = typer.Option(
    "--workers", metavar="N", help="Files read in parallel. Default: number of CPUs minus one."
)


@app.command(rich_help_panel=INDEX)
def scan(
    ctx: typer.Context,
    source: Annotated[str | None, _SOURCE] = None,
    workers: Annotated[int | None, _WORKERS] = None,
    tier: Annotated[str, typer.Option(hidden=True)] = "none",
) -> None:
    """Read your sources and add new or changed items. Never removes anything (see sync)."""
    if tier != "none":
        raise _error("scan builds Tier 0 only. Run `graph-me enrich` afterwards for Tier 1.", 2)
    _run_pipeline(ctx, source=source, workers=workers, forget=False)


@app.command(rich_help_panel=INDEX)
def sync(
    ctx: typer.Context,
    source: Annotated[str | None, _SOURCE] = None,
    workers: Annotated[int | None, _WORKERS] = None,
    allow_mass_forget: Annotated[
        bool,
        typer.Option(
            "--allow-mass-forget",
            help="Allow forgetting more than half of a source. Without it, sync stops instead "
            "(in case a drive is unplugged).",
        ),
    ] = False,
) -> None:
    """Make the index match your sources: add, update, and forget what was deleted.

    Forgets deleted files, messages deleted in msgvault and sources removed from config.yaml,
    with everything learned only from them. A source that can't be reached (unplugged drive,
    missing folder) is skipped, never wiped.
    """
    _run_pipeline(
        ctx, source=source, workers=workers, forget=True, allow_mass_forget=allow_mass_forget
    )


def _source_summary(st, forget: bool) -> str:
    parts = [_plural(st.seen, "item")]
    for n, label in ((st.added, "new"), (st.updated, "updated"),
                     (st.forgotten if forget else 0, "forgotten"),
                     (st.blacklisted, "blacklisted"), (st.parse_errors, "unreadable"),
                     (st.flagged, "flagged as possible prompt injection")):  # fmt: skip
        if n:
            parts.append(f"{n} {label}")
    if not (st.added or st.updated or (forget and st.forgotten)):
        parts.append("no changes")
    return ", ".join(parts)


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
        raise _error(f"No sources in {c.config_path}. List your folders under `sources:`.")
    conn = _open_store(c)
    typer.secho(
        "Tier 0 (no AI): search matches words, not meaning. `graph-me enrich` adds AI labels.",
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
        conn.close()
        raise _error(str(exc)) from exc
    from graph_me import report as report_mod

    try:
        report_path, _ = report_mod.write(conn, c.out)
    finally:
        conn.close()

    if report.forgotten_blacklisted:
        forgot = _plural(report.forgotten_blacklisted, "item")
        typer.echo(f"blacklist: forgot {forgot} it now covers")
    for name, count in report.removed_sources.items():
        typer.echo(f"{name}: removed from config.yaml, forgot {_plural(count, 'item')}")
    for name, message in report.failures.items():
        typer.secho(f"{name}: kept. {message}", fg=typer.colors.RED, err=True)
    for name, st in report.sources.items():
        if st.failure:
            message = st.failure.removeprefix(f"source {name!r}: ")
            typer.secho(f"{name}: skipped. {message}", fg=typer.colors.RED, err=True)
            continue
        typer.echo(f"{name}: {_source_summary(st, forget)}")
        for err in st.errors:
            typer.echo(f"  unreadable: {err}", err=True)
    typer.echo(f"\nSummary: {report_path}")
    if not report.ok:
        raise typer.Exit(1)


@app.command(rich_help_panel=INDEX)
def report(ctx: typer.Context) -> None:
    """Rewrite REPORT.md and graph.json (scan and sync already do it)."""
    from graph_me import report as report_mod

    c = _ctx(ctx)
    conn = _open_store(c)
    try:
        paths = report_mod.write(conn, c.out)
    finally:
        conn.close()
    for path in paths:
        typer.echo(f"wrote   {path}")


# -- ask ----------------------------------------------------------------------------------------


@app.command(rich_help_panel=ASK)
def query(
    ctx: typer.Context,
    text: Annotated[str, typer.Argument(metavar="WORDS", help="What to look for.")],
    kind: Annotated[
        str | None,
        typer.Option(
            "--kind",
            metavar="KIND",
            callback=_one_of(*KINDS),
            help="Only file, email, message or contact.",
        ),
    ] = None,  # fmt: skip
    source: Annotated[
        str | None, typer.Option(metavar="NAME", help="Only this source from config.yaml.")
    ] = None,
    since: Annotated[
        str | None,
        typer.Option(metavar="YYYY-MM-DD", callback=_day, help="Only items dated on or after."),
    ] = None,
    until: Annotated[
        str | None,
        typer.Option(metavar="YYYY-MM-DD", callback=_day, help="Only items dated on or before."),
    ] = None,
    limit: Annotated[int, typer.Option(metavar="N", help="At most N results.")] = 10,
    fmt: Annotated[
        str,
        typer.Option(
            "--format",
            metavar="FORMAT",
            callback=_one_of(*FORMATS),
            help="text (for you), json (for programs) or markdown (for AI assistants).",
        ),
    ] = "text",  # fmt: skip
    as_json: Annotated[bool, typer.Option("--json", help="Same as --format json.")] = False,
    reveal: Annotated[
        bool,
        typer.Option("--reveal", help="Show secrets that are hidden by default (IBANs, keys...)."),
    ] = False,
) -> None:
    """Search everything. Each result shows where it is, its date and its source."""
    from graph_me.query import pack

    fmt = "json" if as_json else fmt
    c = _ctx(ctx)
    _require_index(c)
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
        typer.echo(
            "No results. Try other words, another language, a person's name, or fewer filters."
        )
        return
    for person in result["facts"]:
        _echo_facts(person["entity"], person["facts"])
        typer.echo()
    for n, item in enumerate(result["answer_items"], 1):
        flag = typer.style("  [possible prompt injection]", fg="red") if item["flagged"] else ""
        typer.secho(f"{n}. {item['title'] or '(untitled)'}", bold=True, nl=False)
        typer.echo(flag)
        typer.echo(f"   {item.get('path') or item['uri']}")
        if item.get("from"):
            to = ", ".join(item.get("to") or []) or "?"
            typer.echo(f"   from {item['from']} to {to}")
        typer.echo(f"   {' '.join(item['snippet'].split())}")
        for origin in item.get("origin", []):
            sender = f" from {origin['from']}" if origin.get("from") else ""
            typer.echo(f'   came with the email "{origin["title"]}"{sender}, {_date(origin)}')
        for copy in item.get("saved_as", []):
            typer.echo(f"   saved as {copy['path']}")
        meta = " · ".join(x for x in (item["kind"], item["source"], _date(item),
                                      f"trust: {item['trust']}") if x)  # fmt: skip
        typer.echo(typer.style(f"   {meta}", dim=True))
    if result.get("truncated"):
        typer.echo(typer.style("More results were cut. Narrow the search or raise --limit.",
                               dim=True))  # fmt: skip


def _date(item: dict) -> str:
    """The item's date in the local time zone (dates are stored in UTC)."""
    value = item.get("date")
    if not value:
        return ""
    try:
        return datetime.fromisoformat(value).astimezone().date().isoformat()
    except ValueError:
        return value[:10]


def _echo_facts(entity: dict, facts: list[dict]) -> None:
    label = entity["name"] or next((a["value"] for a in entity["aliases"]), entity["id"])
    typer.secho(f"{label}{' (you)' if entity['is_me'] else ''}", bold=True)
    for fact in facts:
        low = typer.style(" low", fg=typer.colors.YELLOW) if fact["confidence"] < 0.8 else ""
        typer.echo(
            f"   {fact['key'].replace('_', ' ')}: {fact['value']}"
            f"  ({fact['confidence']:.0%} sure{low})"
        )
        for ev in fact["evidence"][:3]:
            where = " · ".join(x for x in (ev["title"], _date(ev), ev["uri"]) if x)
            typer.echo(typer.style(f"      from {where}", dim=True))
        if len(fact["evidence"]) > 3:
            typer.echo(typer.style(f"      and {len(fact['evidence']) - 3} more", dim=True))


_RELATION_LABELS = {  # (type, direction) -> (text, what the weight counts)
    ("wrote_to", "out"): ("wrote to {name}", "message"),
    ("wrote_to", "in"): ("received from {name}", "message"),
    ("in_folder", "out"): ("in the folder {name}", "file"),
    ("in_folder", "in"): ("contains {name}", "file"),
}


@app.command(rich_help_panel=ASK)
def who(
    ctx: typer.Context,
    text: Annotated[
        str,
        typer.Argument(metavar="PERSON", help="A name, nickname, email, phone number, or 'me'."),
    ],
    as_json: Annotated[bool, typer.Option("--json", help="Print JSON.")] = False,
) -> None:
    """Show what graph-me knows about someone: identifiers, facts, closest contacts."""
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
        raise _error(f"Nobody matches {text!r}. Try an email, a phone number or another name.")
    for n, person in enumerate(people):
        if n:
            typer.echo()
        _echo_facts(person, person["facts"])
        ids = ", ".join(a["value"] for a in person["aliases"])
        typer.echo(f"   identifiers: {ids or 'none'}")
        typer.echo(f"   seen in {_plural(person['mentions'], 'item')}")
        for rel in person["related"]:
            name = rel["entity"]["name"] or rel["entity"]["id"]
            text, unit = _RELATION_LABELS.get(
                (rel["type"], rel["direction"]),
                (f"{rel['type'].replace('_', ' ')} {{name}}", "item"),
            )
            typer.echo(f"   {text.format(name=name)} ({_plural(int(rel['weight']), unit)})")


@app.command(rich_help_panel=ASK)
def fact(
    ctx: typer.Context,
    who_text: Annotated[
        str, typer.Argument(metavar="PERSON", help="A name, nickname, email or phone number.")
    ],
    key: Annotated[
        str,
        typer.Argument(
            metavar="FACT", help="birthday, birth_year, nickname, organization or relation_to_user."
        ),
    ],  # fmt: skip
    as_json: Annotated[bool, typer.Option("--json", help="Print JSON.")] = False,
) -> None:
    """Show one fact about someone, with the files and messages it came from."""
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
        raise _error(f"No {key.replace('_', ' ')} known for {who_text!r}.")
    for person in found:
        _echo_facts(person["entity"], person["facts"])


# -- AI enrichment ------------------------------------------------------------------------------


@app.command(rich_help_panel=AI)
def enrich(
    ctx: typer.Context,
    llm: Annotated[
        str | None,
        typer.Option(
            metavar="PROVIDER",
            callback=_one_of(*PROVIDERS),
            help="Who labels: agent (your AI assistant), ollama, anthropic or "
            "openai_compat. Default: extraction.medium.llm.provider in config.yaml.",
        ),
    ] = None,  # fmt: skip
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Show what would be labelled, without doing it.")
    ] = False,
    status: Annotated[
        bool, typer.Option("--status", help="List agent batches not answered and merged yet.")
    ] = False,
    source: Annotated[
        str | None, typer.Option(metavar="NAME", help="Only items from this source.")
    ] = None,
    path: Annotated[
        str | None, typer.Option(metavar="FOLDER", help="Only files under this folder.")
    ] = None,
    since: Annotated[
        str | None,
        typer.Option(metavar="YYYY-MM-DD", callback=_day, help="Only items dated on or after."),
    ] = None,
    batch_size: Annotated[int, typer.Option(metavar="N", help="Names per batch.")] = 50,
    no_embeddings: Annotated[
        bool, typer.Option("--no-embeddings", help="Skip meaning-based search.")
    ] = False,
    yes: Annotated[
        bool, typer.Option("--yes", help="Don't ask before sending names to an online API.")
    ] = False,
    tier: Annotated[str, typer.Option(hidden=True)] = "medium",
) -> None:
    """Tier 1: label file names and contacts with AI, and add meaning-based search.

    Only names are sent (files, folders, contacts), never the content of your documents or
    messages. Every answer is checked before graph-me keeps it. Needs the medium extra.
    """
    from graph_me import llm as llm_mod
    from graph_me import report as report_mod
    from graph_me.pipeline import enrich as enrich_mod

    c = _ctx(ctx)
    if tier != "medium":
        raise _error("Only Tier 1 (--tier medium) exists for now.", 2)
    if status:
        pending = enrich_mod.pending_batches(c.out)
        if not pending:
            typer.echo("No pending batches.")
        for batch in pending:
            answered = batch.with_name(batch.name.replace(".json", ".out.json")).exists()
            state = "answered, run `graph-me ingest --all`" if answered else "waiting for an answer"
            typer.echo(f"{batch}  {state}")
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
            f"to label  {_plural(plan.candidates.get('files', 0), 'file name')}, "
            f"{_plural(plan.candidates.get('contacts', 0), 'contact')} "
            f"in {_plural(est['batches'], 'batch', 'batches')}"
            f" (about {est['input_tokens']} tokens in, {est['output_tokens']} out) by {provider}"
        )
        if not no_embeddings:
            model = c.config.extraction.medium.embeddings or "local"
            typer.echo(f"to embed  {_plural(est['chunks_to_embed'], 'text chunk')} "
                       f"with the {model} model, on this computer")  # fmt: skip
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
        raise _error(f"model error: {exc}") from exc
    finally:
        conn.close()

    if result.batches_written:
        count = _plural(len(result.batches_written), "batch file")
        typer.echo(f"\nWrote {count} to {c.out / 'work'}.")
        typer.echo(
            "Your AI assistant answers each batch-NNNN.json in batch-NNNN.out.json, then run "
            "`graph-me ingest --all`. In Claude Code, just ask: use graph-me to enrich the db."
        )
    elif provider != "agent":
        extras = []
        if result.stale:
            extras.append(f"{result.stale} stale")
        if result.rejected:
            extras.append(f"{_plural(len(result.rejected), 'answer')} rejected")
        suffix = f" ({', '.join(extras)})" if extras else ""
        typer.echo(f"labelled {_plural(result.labelled, 'item')} with {result.provider}{suffix}")
    if result.embeddings:
        typer.echo(f"embedded {_plural(result.embedded, 'text chunk')} ({result.embeddings})")


@app.command(rich_help_panel=AI)
def ingest(
    ctx: typer.Context,
    answers: Annotated[
        list[Path] | None,
        typer.Argument(metavar="[FILES]...", help="batch-NNNN.out.json files to merge."),
    ] = None,
    all_answers: Annotated[
        bool, typer.Option("--all", help="Merge every answered batch in graph-out/work.")
    ] = False,
) -> None:
    """Merge your AI assistant's answers to `graph-me enrich` batches (each one is checked)."""
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
        raise _error("Nothing to merge. Answer a batch first (see `graph-me enrich --status`).")
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
                f"batch {r.batch}: {_plural(r.applied, 'label')} applied"
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


# -- interfaces ---------------------------------------------------------------------------------


@app.command(rich_help_panel=INTERFACES)
def ui(
    ctx: typer.Context,
    port: Annotated[
        int, typer.Option(metavar="N", help="Port to use. Default: any free port.")
    ] = 0,
    open_browser: Annotated[
        bool, typer.Option("--open/--no-open", help="Open the page in your browser.")
    ] = True,
) -> None:
    """Open the web UI in your browser (search, graph, people). Needs the ui extra."""
    try:
        import uvicorn

        from graph_me.ui.app import build_app, new_token
    except ImportError:
        raise _error(
            'The web UI needs the [ui] extra: uv tool install --managed-python "graph-me[ui]"'
        ) from None
    import socket
    import webbrowser

    c = _ctx(ctx)
    _require_index(c)
    token = new_token()
    # Bind first so the printed URL has the real port, then hand the socket to uvicorn.
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("127.0.0.1", port))
    except OSError as exc:
        raise _error(f"Cannot use port {port}: {exc.strerror}. Try another --port.") from None
    url = f"http://127.0.0.1:{sock.getsockname()[1]}/?token={token}"
    typer.echo(f"graph-me UI: {url}")
    typer.echo("Only this computer can open it. Anyone with this address can read your index")
    typer.echo("while it runs, so don't share it. Press Ctrl+C to stop.")
    if open_browser:
        webbrowser.open(url)
    server = uvicorn.Server(
        uvicorn.Config(
            build_app(_service(c, "ui"), token), log_level="warning", access_log=False
        )  # fmt: skip
    )
    server.run(sockets=[sock])


@app.command(rich_help_panel=INTERFACES)
def mcp(ctx: typer.Context) -> None:
    """Run the MCP server for AI assistants. Your app starts it; you don't run it by hand.

    Register it once with: claude mcp add graph-me -- graph-me mcp
    """
    from graph_me.mcp_server import build_server

    c = _ctx(ctx)
    _require_index(c)
    build_server(_service(c, "mcp")).run()
