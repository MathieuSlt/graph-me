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
        "Getting started: `graph-me init --detect` finds your folders, then `graph-me scan` and "
        '`graph-me query "lease"`. To try it on made-up data first: `graph-me demo`.'
    ),
    no_args_is_help=True,
    add_completion=False,
)

SETUP, INDEX, ASK, CORRECT, AI, INTERFACES = (
    "Set up", "Index", "Ask", "Correct", "AI enrichment", "Interfaces"
)  # fmt: skip
KINDS = ("file", "email", "message", "contact", "event")
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


def _since(value: str | None) -> str | None:
    """``90d``, ``12w``, ``6m`` or ``YYYY-MM-DD`` -> an aware ISO timestamp (UTC)."""
    if value is None:
        return None
    from datetime import UTC, timedelta

    value = value.strip()
    units = {"d": 1, "w": 7, "m": 30, "y": 365}
    if value[:-1].isdigit() and value[-1:].lower() in units:
        days = int(value[:-1]) * units[value[-1].lower()]
        return (datetime.now(UTC) - timedelta(days=days)).isoformat()
    try:
        day = date.fromisoformat(value)
    except ValueError:
        raise typer.BadParameter(f"{value!r}: use 90d, 12w, 6m, 1y or YYYY-MM-DD.") from None
    return datetime(day.year, day.month, day.day, tzinfo=UTC).isoformat()


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
    _use_user_rules(cfg_path.expanduser().parent / "rules")
    try:
        cfg = config.load_config(cfg_path)
    except Exception as exc:  # invalid YAML or schema: show a readable error
        raise _error(_config_error(cfg_path, exc)) from exc
    ctx.obj = Context(config_path=cfg_path, config=cfg, out=config.resolve_output(out, cfg))


def _use_user_rules(folder: Path) -> None:
    """Your own rule packs: rules/ next to config.yaml (an explicit GRAPH_ME_RULES wins).

    Set in the environment so the worker processes of a scan read them too.
    """
    import os

    from graph_me import rules

    global _rules_set
    current = os.environ.get(rules.ENV_RULES)
    if current is not None and current != _rules_set:  # set by you: keep it
        return
    if current != str(folder):
        os.environ[rules.ENV_RULES] = _rules_set = str(folder)
        rules.reload()


_rules_set: str | None = None  # the GRAPH_ME_RULES value this process set itself


def _require_index(c: Context) -> None:
    if not c.db_path.exists():
        raise _error(f"No index in {c.out}. Run `graph-me init`, then `graph-me scan`.")


def _open_store(c: Context) -> db.sqlite3.Connection:
    _require_index(c)
    return db.connect(c.db_path)


def _service(c: Context, interface: str = "cli", profile: str | None = None):
    from graph_me.service import Service, UnknownProfile

    try:
        return Service(c.config, c.db_path, interface, profile=profile)
    except UnknownProfile as exc:
        raise _error(f"{exc}. Profiles are listed under `profiles:` in config.yaml.") from exc


_PROFILE = typer.Option(
    "--profile",
    metavar="NAME",
    help="Only the sources of this profile (`profiles:` in config.yaml).",
)


def _warn_if_stale(index: dict) -> None:
    if index.get("stale"):
        typer.secho(
            f"The index was last synced {index['age_days']} days ago: recent items may be "
            "missing. Run `graph-me sync`.",
            fg=typer.colors.YELLOW,
            err=True,
        )


def _index_age(c: Context, conn: db.sqlite3.Connection) -> dict:
    from graph_me.service import freshness

    return freshness(conn, c.config.index.stale_after_days)


# -- set up -------------------------------------------------------------------------------------


@app.command(rich_help_panel=SETUP)
def init(
    ctx: typer.Context,
    detect: Annotated[
        bool,
        typer.Option(
            "--detect",
            help="Look for your Documents, Desktop, Downloads, Obsidian vaults, msgvault and .vcf "
            "files, and ask which ones to index.",
        ),
    ] = False,  # fmt: skip
    yes: Annotated[
        bool, typer.Option("--yes", help="With --detect: keep everything found.")
    ] = False,
) -> None:
    """Create config.yaml (if missing) and an empty index. Start here."""
    c = _ctx(ctx)
    created = not c.config_path.exists()
    if detect and not created:
        raise _error(f"{c.config_path} already exists. --detect only writes a new config: move "
                     "it away first, or edit it by hand.")  # fmt: skip
    if created:
        c.config_path.parent.mkdir(parents=True, exist_ok=True)
        text = _detected_config(yes) if detect else config.template_text()
        c.config_path.write_text(text, encoding="utf-8")
    config.ensure_output(c.out)
    db.connect(c.db_path).close()
    typer.echo(f"config  {c.config_path} ({'created' if created else 'already there, kept'})")
    typer.echo(f"index   {c.out}")
    if detect:
        typer.echo(
            "\nNext: `graph-me scan --since 90d` gives answers in minutes; then run "
            "`graph-me scan` for the rest, and `graph-me doctor` to check the setup."
        )
    else:
        typer.echo("\nNext: list your folders under `sources:` in config.yaml, then "
                   "`graph-me scan`. Or start over with `graph-me init --detect`.")  # fmt: skip


def _detected_config(yes: bool) -> str:
    from graph_me.setup import detect

    found = detect.detect()
    if not found.candidates:
        typer.echo("Found no usual folders. The config lists none: add yours under `sources:`.")
    chosen = []
    for cand in found.candidates:
        if yes or typer.confirm(f"Index {cand.name}: {cand.why}?", default=True):
            chosen.append(cand)
    timezone = found.timezone or "unknown (this computer's is used)"
    code = found.phone_country_code or "unknown, set it in config.yaml"
    typer.echo(f"time zone           {timezone}")
    typer.echo(f"phone country code  {code}")
    return detect.render(found, chosen)


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
    typer.echo("\nFor a full check of sources, skill, MCP and schedule: graph-me doctor")


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


@app.command(rich_help_panel=SETUP)
def doctor(ctx: typer.Context) -> None:
    """Check the setup (sources, index, skill, MCP, schedule) and say how to fix problems."""
    from graph_me import doctor as doctor_mod

    c = _ctx(ctx)
    colors = {"ok": typer.colors.GREEN, "warn": typer.colors.YELLOW, "fail": typer.colors.RED,
              "info": None}  # fmt: skip
    checks = doctor_mod.run(c.config, c.config_path, c.out)
    for check in checks:
        mark = typer.style(f"{check.status:<4}", fg=colors[check.status], bold=True)
        typer.echo(f"{mark}  {check.label}: {check.message}")
        if check.hint and check.status != "ok":
            typer.echo(typer.style(f"      {check.hint}", dim=True))
    if any(ch.status == "fail" for ch in checks):
        raise typer.Exit(1)


@app.command("install-mcp", rich_help_panel=SETUP)
def install_mcp(
    ctx: typer.Context,
    client: Annotated[
        str,
        typer.Option(
            "--client",
            metavar="APP",
            callback=_one_of("claude-code", "claude-desktop", "cursor", "codex"),
            help="claude-code, claude-desktop, cursor or codex.",
        ),
    ] = "claude-code",  # fmt: skip
    name: Annotated[
        str | None,
        typer.Option(metavar="NAME", help="Server name in the app. Default: graph-me[-PROFILE]."),
    ] = None,
    profile: Annotated[str | None, _PROFILE] = None,
    print_only: Annotated[
        bool, typer.Option("--print", help="Show what would change, without changing it.")
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", help="Don't ask before editing.")] = False,
) -> None:
    """Register graph-me's MCP server with an AI app: Claude Code, Claude Desktop, Cursor, Codex."""
    from graph_me.setup import clients

    c = _ctx(ctx)
    if profile is not None and profile not in c.config.profiles:
        raise _error(f"No profile {profile!r} under `profiles:` in {c.config_path}.")
    name = name or (f"graph-me-{profile}" if profile else "graph-me")
    try:
        p = clients.plan(client, name, c.config_path, profile=profile)
    except ValueError as exc:
        raise _error(str(exc)) from exc
    if p.run:
        typer.echo("command  " + " ".join(p.run))
    elif p.already:
        typer.echo(f"{p.path} already has the {name!r} server. Nothing to do.")
        return
    else:
        # Only graph-me's own entry: the app's other servers may hold tokens, never print them.
        typer.echo(f"file     {p.path} (other settings kept, a .bak copy is made)")
        typer.secho(clients.snippet(p), fg="green")
    if print_only:
        return
    if not yes and not typer.confirm("Apply this change?", default=True):
        raise typer.Exit(1)
    try:
        clients.apply(p)
    except (OSError, FileNotFoundError) as exc:
        raise _error(f"{exc}. Run the command above yourself.") from exc
    except Exception as exc:  # subprocess.CalledProcessError
        raise _error(f"failed: {exc}") from exc
    typer.echo(f"Done. Restart {client} so it starts graph-me's server.")


schedule_app = typer.Typer(
    help="Run `graph-me sync` regularly with your system's scheduler (launchd or systemd).",
    no_args_is_help=True,
)
app.add_typer(schedule_app, name="schedule", rich_help_panel=INDEX)


@schedule_app.command("install")
def schedule_install(
    ctx: typer.Context,
    every: Annotated[str, typer.Option(metavar="EVERY", help="30m, 6h, 1d... (min 15m).")] = "6h",
) -> None:
    """Sync every EVERY (replaces an existing schedule)."""
    from graph_me.setup import clients
    from graph_me.setup import schedule as schedule_mod

    c = _ctx(ctx)
    _require_index(c)
    try:
        seconds = schedule_mod.parse_every(every)
        command = schedule_mod.sync_command(clients.executable(), c.config_path)
        files = schedule_mod.install(command, c.out / schedule_mod.LOG_NAME, seconds)
    except (ValueError, schedule_mod.ScheduleError) as exc:
        raise _error(str(exc)) from exc
    for f in files:
        typer.echo(f"wrote   {f}")
    typer.echo(f"graph-me sync now runs every {every}. Its output goes to "
               f"{c.out / schedule_mod.LOG_NAME}.")  # fmt: skip


@schedule_app.command("remove")
def schedule_remove() -> None:
    """Stop syncing on a schedule."""
    from graph_me.setup import schedule as schedule_mod

    try:
        files = schedule_mod.remove()
    except schedule_mod.ScheduleError as exc:
        raise _error(str(exc)) from exc
    typer.echo("\n".join(f"removed {f}" for f in files) or "No schedule was installed.")


@schedule_app.command("status")
def schedule_status() -> None:
    """Show whether a schedule is installed and loaded."""
    from graph_me.setup import schedule as schedule_mod

    try:
        st = schedule_mod.status()
    except schedule_mod.ScheduleError as exc:
        raise _error(str(exc)) from exc
    if not st.installed:
        typer.echo("No schedule. `graph-me schedule install` syncs regularly.")
        return
    every = f"every {st.every // 60} minutes" if st.every else "installed"
    typer.echo(f"{every}, {'loaded' if st.active else 'NOT loaded'}: {st.files[0]}")


@app.command(rich_help_panel=SETUP)
def demo(
    ctx: typer.Context,
    folder: Annotated[
        Path | None,
        typer.Option("--dir", metavar="FOLDER", help="Where to build it. Default: a temp folder."),
    ] = None,
    open_ui: Annotated[bool, typer.Option("--ui", help="Open the web UI on it.")] = False,
) -> None:
    """Try graph-me on made-up data (documents, contacts, a correction), not yours."""
    import tempfile

    from graph_me import demo as demo_mod
    from graph_me import report as report_mod
    from graph_me.pipeline import run

    if folder:
        root = folder.expanduser().resolve()
    else:
        root = Path(tempfile.mkdtemp(prefix="graph-me-demo-"))
    if folder and root.exists() and any(root.iterdir()):
        raise _error(f"{root} is not empty. Pick an empty or new folder.")
    cfg_path = demo_mod.build(root)
    cfg = config.load_config(cfg_path)
    out = config.ensure_output(config.resolve_output(None, cfg))
    conn = db.connect(out / config.DB_NAME)
    try:
        run.scan(conn, cfg, workers=1)
        report_mod.write(conn, out)
    finally:
        conn.close()
    typer.echo(f"Built a demo graph from made-up data in {root}\n\nTry:")
    for question in demo_mod.QUESTIONS:
        words = " ".join(f'"{w}"' if " " in w else w for w in question[1:])
        typer.echo(f"  graph-me --config {cfg_path} {question[0]} {words}")
    typer.echo(f"  graph-me --config {cfg_path} ui")
    typer.echo(f"\nDelete {root} when you are done.")
    if open_ui:
        ctx.obj = Context(config_path=cfg_path, config=cfg, out=out)
        ui(ctx, port=0, open_browser=True, profile=None)


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
    since: Annotated[
        str | None,
        typer.Option(
            metavar="WHEN",
            callback=_since,
            help="Only items dated since then: 90d, 12w, 6m or YYYY-MM-DD. Gives answers fast "
            "on a big archive; run a full scan afterwards for the rest.",
        ),
    ] = None,  # fmt: skip
    reread: Annotated[
        bool,
        typer.Option("--reread", help="Read every item again, even unchanged ones (slow)."),
    ] = False,
    tier: Annotated[str, typer.Option(hidden=True)] = "none",
) -> None:
    """Read your sources and add new or changed items. Never removes anything (see sync)."""
    if tier != "none":
        raise _error("scan builds Tier 0 only. Run `graph-me enrich` afterwards for Tier 1.", 2)
    _run_pipeline(ctx, source=source, workers=workers, forget=False, since=since, reread=reread)


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
    scheduled: Annotated[bool, typer.Option("--scheduled", hidden=True)] = False,
) -> None:
    """Make the index match your sources: add, update, and forget what was deleted.

    Forgets deleted files, messages deleted in msgvault and sources removed from config.yaml,
    with everything learned only from them. A source that can't be reached (unplugged drive,
    missing folder) is skipped, never wiped.
    """
    if scheduled:  # started by `graph-me schedule`: output goes to graph-out/sync.log
        from graph_me.setup import schedule as schedule_mod

        schedule_mod.rotate_log(_ctx(ctx).out)
        typer.echo(f"--- {datetime.now().astimezone().isoformat(timespec='seconds')}")
    _run_pipeline(
        ctx, source=source, workers=workers, forget=True, allow_mass_forget=allow_mass_forget
    )


def _source_summary(st, forget: bool) -> str:
    parts = [_plural(st.seen, "item")]
    for n, label in ((st.added, "new"), (st.updated, "updated"),
                     (st.older, "older, left for a full scan"),
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
    since: str | None = None,
    reread: bool = False,
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
                conn, c.config, only_source=source, workers=workers, progress=progress,
                since=datetime.fromisoformat(since) if since else None, reread=reread,
            )  # fmt: skip
    except (ValueError, db.RunLocked) as exc:
        conn.close()
        raise _error(str(exc)) from exc
    from graph_me import report as report_mod

    try:
        report_path, _ = report_mod.write(conn, c.out)
        if c.config.export.ics:
            _write_ics(conn, c.out / "upcoming.ics")
    finally:
        conn.close()

    if report.forgotten_blacklisted:
        forgot = _plural(report.forgotten_blacklisted, "item")
        typer.echo(f"blacklist: forgot {forgot} it now covers")
    for name, count in report.removed_sources.items():
        typer.echo(f"{name}: removed from config.yaml, forgot {_plural(count, 'item')}")
    for name, message in report.failures.items():
        typer.secho(f"{name}: kept. {message}", fg=typer.colors.RED, err=True)
    if report.backfilled:
        typer.echo(f"new rules: read {_plural(report.backfilled, 'stored item')} again")
    if report.relinked:
        typer.echo(f"corrections: rebuilt the people in {_plural(report.relinked, 'item')}")
    for note in report.notes:
        typer.secho(note, fg=typer.colors.YELLOW, err=True)
    for name, st in report.sources.items():
        if name == "_overrides":
            name = "corrections"
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
            help="Only file, email, message, contact or event.",
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
    _warn_if_stale(result["index"])
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
        if item.get("values"):
            found = ", ".join(f"{v['key'].replace('_', ' ')} {v['value']}" for v in item["values"])
            typer.echo(f"   read in it: {found}")
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
    ("met", "out"): ("met {name}", "meeting"),
    ("met", "in"): ("met {name}", "meeting"),
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
        index = _index_age(c, conn)
    finally:
        conn.close()
    if as_json:
        typer.echo(json.dumps(people, ensure_ascii=False, indent=2))
        return
    _warn_if_stale(index)
    if not people:
        raise _error(f"Nobody matches {text!r}. Try an email, a phone number or another name.")
    for n, person in enumerate(people):
        if n:
            typer.echo()
        _echo_facts(person, person["facts"])
        ids = ", ".join(a["value"] for a in person["aliases"])
        typer.echo(f"   identifiers: {ids or 'none'}")
        typer.echo(f"   seen in {_plural(person['mentions'], 'item')}")
        if last := person.get("last_contact"):
            typer.echo(f"   last in touch: {_date(last)} ({last['kind']}: {last['title']})")
        if meeting := person.get("next_meeting"):
            typer.echo(f"   next meeting: {_date(meeting)} ({meeting['title']})")
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
    set_value: Annotated[
        str | None,
        typer.Option("--set", metavar="VALUE", help="Correct it: graph-me will know VALUE."),
    ] = None,
    reject_value: Annotated[
        str | None,
        typer.Option("--reject", metavar="VALUE", help="Correct it: VALUE is wrong, drop it."),
    ] = None,
) -> None:
    """Show one fact about someone, with the files and messages it came from.

    With --set or --reject, save a correction to overrides.yaml instead (applied on the next
    sync). Example: graph-me fact Sophie birthday --reject 07-04
    """
    from graph_me import overrides as corrections
    from graph_me.query import graph

    c = _ctx(ctx)
    if set_value is not None and reject_value is not None:
        raise _error("Use --set or --reject, not both.")
    if set_value is not None or reject_value is not None:
        who_id = _identifier(c, who_text)

        def change(fixes: corrections.Overrides) -> str:
            if set_value is not None:
                value = corrections.set_fact(fixes, who_id, key, set_value)
                return f"{who_id}: {key.replace('_', ' ')} is {value}"
            value = corrections.reject(fixes, who_id, key, reject_value)
            return f"{who_id}: {key.replace('_', ' ')} is not {value}"

        _correct(c, change)
        return
    conn = _open_store(c)
    try:
        found = graph.get_fact(conn, who_text, key, c.config.people)
        index = _index_age(c, conn)
    finally:
        conn.close()
    if as_json:
        typer.echo(json.dumps(found, ensure_ascii=False, indent=2))
        return
    _warn_if_stale(index)
    if not found:
        raise _error(f"No {key.replace('_', ' ')} known for {who_text!r}.")
    for person in found:
        _echo_facts(person["entity"], person["facts"])


@app.command(rich_help_panel=ASK)
def timeline(
    ctx: typer.Context,
    text: Annotated[
        str, typer.Argument(metavar="PERSON_OR_TOPIC", help="A person, or a topic with --topic.")
    ],
    topic: Annotated[
        bool, typer.Option("--topic", help="TEXT is a topic (apartment move), not a person.")
    ] = False,
    since: Annotated[
        str | None,
        typer.Option(metavar="YYYY-MM-DD", callback=_day, help="Only items dated on or after."),
    ] = None,
    until: Annotated[
        str | None,
        typer.Option(metavar="YYYY-MM-DD", callback=_day, help="Only items dated on or before."),
    ] = None,
    limit: Annotated[int, typer.Option(metavar="N", help="At most N items.")] = 20,
    as_json: Annotated[bool, typer.Option("--json", help="Print JSON.")] = False,
) -> None:
    """Items involving a person, or about a topic, newest first (mails, chats, meetings, files)."""
    c = _ctx(ctx)
    _require_index(c)
    service = _service(c)
    if topic:
        result = service.timeline(query=text, since=since, until=until, limit=limit)
    else:
        result = service.timeline(text, since=since, until=until, limit=limit)
    if as_json:
        typer.echo(json.dumps(result, ensure_ascii=False, indent=2))
        return
    _warn_if_stale(result["index"])
    if not result["results"] or not any(r["items"] for r in result["results"]):
        raise _error(f"Nothing found for {text!r}.")
    for r in result["results"]:
        if r["entity"]:
            typer.secho(r["entity"]["name"] or r["entity"]["id"], bold=True)
        for item in r["items"]:
            typer.echo(f"   {_date(item):<10}  {item['kind']:<8} {item['title'] or '(untitled)'}")


@app.command(rich_help_panel=ASK)
def upcoming(
    ctx: typer.Context,
    days: Annotated[int, typer.Option(metavar="N", min=1, max=366, help="How far ahead.")] = 60,
    kind: Annotated[
        str | None,
        typer.Option(
            metavar="KIND",
            help="dates (birthdays, anniversaries), deadlines (expires, renews, due, ends), "
            "or one of those keys.",
        ),
    ] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Print JSON.")] = False,
) -> None:
    """What is coming up: birthdays, anniversaries, and deadlines read in your documents."""
    c = _ctx(ctx)
    _require_index(c)
    result = _service(c).upcoming(days=days, kind=kind)
    if as_json:
        typer.echo(json.dumps(result, ensure_ascii=False, indent=2))
        return
    _warn_if_stale(result["index"])
    if not result["results"]:
        typer.echo(f"Nothing in the next {_plural(days, 'day')}.")
        return
    for u in result["results"]:
        when = date.fromisoformat(u["date"])
        soon = "today" if u["in_days"] == 0 else f"in {_plural(u['in_days'], 'day')}"
        name = u["entity"]["name"] or "(unnamed)"
        low = typer.style(" low", fg=typer.colors.YELLOW) if u["confidence"] < 0.8 else ""
        typer.secho(f"{when:%a %d %b %Y}  {name}: {u['key']}", bold=True, nl=False)
        typer.echo(f"  ({soon}, {u['confidence']:.0%} sure{low})")
        for ev in u["evidence"][:2]:
            where = " · ".join(x for x in (ev["title"], _date(ev), ev["uri"]) if x)
            typer.echo(typer.style(f"      from {where}", dim=True))


def _write_ics(conn: db.sqlite3.Connection, path: Path) -> Path:
    from graph_me.export import ics

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(ics.build(conn), encoding="utf-8", newline="")
    return path


export_app = typer.Typer(help="Write files for other tools (a calendar of what is coming up).",
                         no_args_is_help=True)  # fmt: skip
app.add_typer(export_app, name="export", rich_help_panel=ASK)


@export_app.command("ics")
def export_ics(
    ctx: typer.Context,
    out: Annotated[
        Path | None,
        typer.Option("--out", metavar="FILE", help="Default: graph-out/upcoming.ics."),
    ] = None,
) -> None:
    """Write birthdays, anniversaries and deadlines as a calendar (.ics) file.

    Set `export: {ics: true}` in config.yaml to rewrite it after every scan and sync.
    """
    c = _ctx(ctx)
    conn = _open_store(c)
    try:
        path = _write_ics(conn, (out or c.out / "upcoming.ics").expanduser())
    finally:
        conn.close()
    typer.echo(f"wrote   {path}")
    typer.echo("Import it in your calendar app, or subscribe to the file to keep it updated.")


@app.command("log", rich_help_panel=INTERFACES)
def activity_log(
    ctx: typer.Context,
    since: Annotated[
        str | None,
        typer.Option(metavar="WHEN", callback=_since, help="Only since then: 7d, 2w or a date."),
    ] = None,
    interface: Annotated[
        str | None,
        typer.Option(metavar="FROM", callback=_one_of("cli", "mcp", "ui"), help="cli, mcp or ui."),
    ] = None,
    client: Annotated[
        str | None, typer.Option(metavar="APP", help="Only this app (its MCP client name).")
    ] = None,
    profile: Annotated[str | None, _PROFILE] = None,
    limit: Annotated[int, typer.Option(metavar="N", help="At most N queries.")] = 50,
    as_json: Annotated[bool, typer.Option("--json", help="Print JSON.")] = False,
    clear: Annotated[
        bool, typer.Option("--clear", help="Delete the log (only before --since, if given).")
    ] = False,
) -> None:
    """What your AI assistants (and you) asked graph-me, and what it returned."""
    c = _ctx(ctx)
    _require_index(c)
    service = _service(c, profile=profile)
    if clear:
        removed = service.clear_activity(before=since)
        typer.echo(f"Deleted {_plural(removed, 'logged query', 'logged queries')}.")
        return
    rows = service.activity(since=since, interface=interface, client=client, limit=limit)
    if as_json:
        typer.echo(json.dumps(rows, ensure_ascii=False, indent=2))
        return
    if not rows:
        typer.echo("Nothing logged yet." if not (since or interface or client) else "No match.")
        return
    for r in rows:
        when = datetime.fromisoformat(r["ts"]).astimezone().strftime("%Y-%m-%d %H:%M")
        who = " · ".join(x for x in (r["interface"], r["client"],
                                     r["profile"] and f"profile {r['profile']}") if x)  # fmt: skip
        typer.secho(f"{when}  {r['query']}", bold=True, nl=False)
        typer.echo(typer.style(f"  ({who}, {_plural(r['result_count'], 'result')})", dim=True))
        for res in r["results"][:5]:
            label = res["title"] or ("(forgotten since)" if res["type"] == "gone" else "(untitled)")
            typer.echo(typer.style(f"      {res['kind'] or '-'}: {label}", dim=True))
    typer.echo(typer.style(f"\nKept {c.config.log.keep_days} days (log.keep_days). "
                           "`graph-me log --clear` deletes it.", dim=True))  # fmt: skip


rules_app = typer.Typer(
    help="Language rule packs: the words graph-me looks for (birthdays, deadlines, relations).",
    no_args_is_help=True,
)
app.add_typer(rules_app, name="rules", rich_help_panel=SETUP)


@rules_app.command("list")
def rules_list() -> None:
    """Show the loaded rule packs and where your own go."""
    from graph_me import rules

    for lang, pack in sorted(rules.load_all().items()):
        counts = (f"{len(pack.stopwords)} stopwords, {len(pack.birthday_greetings)} greetings, "
                  f"{sum(map(len, pack.relations.values()))} relation words, "
                  f"{sum(map(len, pack.deadlines.values()))} deadline phrases")  # fmt: skip
        typer.echo(f"{lang}  {counts}")
    folder = rules.user_dir()
    state = "found" if folder and folder.is_dir() else "none yet"
    typer.echo(f"\nYour own packs: {folder} ({state})")
    typer.echo("Start from: graph-me rules template > rules/<code>.yaml")


@rules_app.command("template")
def rules_template() -> None:
    """Print a commented, empty rule pack to start a new language from."""
    from importlib import resources as res

    typer.echo(res.files("graph_me.rules").joinpath("_template.yaml").read_text("utf-8"), nl=False)


@rules_app.command("check")
def rules_check(
    file: Annotated[Path, typer.Argument(metavar="FILE", help="A rules/<code>.yaml file.")],
) -> None:
    """Validate a rule pack and run its examples (with the other packs loaded too)."""
    import os
    import tempfile

    from pydantic import ValidationError

    from graph_me import rules

    try:
        pack = rules.read_pack(file)
    except (OSError, ValueError, ValidationError) as exc:
        detail = "\n".join(f"  {'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()) \
            if isinstance(exc, ValidationError) else f"  {exc}"  # fmt: skip
        raise _error(f"{file} is not a valid rule pack:\n{detail}") from exc
    saved = os.environ.get(rules.ENV_RULES)
    with tempfile.TemporaryDirectory() as tmp:
        # Load the pack under check next to the others, as a scan would.
        user = Path(saved).expanduser() if saved else None
        for existing in user.glob("*.yaml") if user and user.is_dir() else ():
            if existing.resolve() != file.resolve():
                (Path(tmp) / existing.name).write_bytes(existing.read_bytes())
        (Path(tmp) / "checked.yaml").write_bytes(file.read_bytes())
        os.environ[rules.ENV_RULES] = tmp
        rules.reload()
        try:
            failures = rules.check_examples(pack)
        finally:
            if saved is None:
                os.environ.pop(rules.ENV_RULES, None)
            else:
                os.environ[rules.ENV_RULES] = saved
            rules.reload()
    ex = pack.examples
    total = (len(ex.birthday.match) + len(ex.birthday.no_match) + len(ex.injection.match)
             + len(ex.injection.no_match) + len(ex.values) + len(ex.relations))  # fmt: skip
    for failure in failures:
        typer.secho(f"fail  {failure}", fg=typer.colors.RED)
    if failures:
        raise typer.Exit(1)
    typer.echo(f"{file}: valid {pack.language} pack, {_plural(total, 'example')} passed.")
    if not total:
        typer.secho("Add examples (see `graph-me rules template`) so changes stay tested.",
                    fg=typer.colors.YELLOW)  # fmt: skip


@export_app.command("bundle")
def export_bundle(
    ctx: typer.Context,
    text: Annotated[str, typer.Argument(metavar="WORDS", help="What to gather.")],
    out: Annotated[
        Path, typer.Option("--out", metavar="FOLDER", help="A new or empty folder to fill.")
    ],
    kind: Annotated[
        str | None,
        typer.Option(metavar="KIND", callback=_one_of(*KINDS), help="Only this kind of item."),
    ] = None,
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
    limit: Annotated[int, typer.Option(metavar="N", min=1, max=500, help="At most N items.")] = 100,
    reveal: Annotated[
        bool, typer.Option("--reveal", help="Keep secrets (IBANs, passwords...) in the text.")
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", help="Don't ask before copying.")] = False,
) -> None:
    """Copy what a search finds into one folder: files, messages as Markdown, and an index.

    For a tax return, a visa file or a move. The originals stay where they are.
    Example: graph-me export bundle "lease landlord deposit" --out ~/Desktop/apartment
    """
    from graph_me.export import bundle

    c = _ctx(ctx)
    _require_index(c)
    service = _service(c)
    folder = out.expanduser().resolve()
    if folder.exists() and any(folder.iterdir()):
        raise _error(f"{folder} is not empty. Pick a new or empty folder.")
    items = bundle.plan(service, text, limit=limit, kind=kind, source=source, since=since,
                        until=until)  # fmt: skip
    if not items:
        raise _error(f"Nothing found for {text!r}. Try other words or fewer filters.")
    for n, item in enumerate(items, 1):
        typer.echo(f"{n:3d}. {_date(item):<10} {item['kind']:<8} {item['title'] or '(untitled)'}")
    if not yes and not typer.confirm(f"Copy these {len(items)} items to {folder}?", default=True):
        raise typer.Exit(1)
    result = bundle.write(service, items, folder, reveal=reveal)
    typer.echo(f"\n{folder}: {_plural(len(result.files), 'file')}, "
               f"{_plural(len(result.messages), 'note')}, INDEX.md")  # fmt: skip
    for path in result.missing:
        typer.secho(f"  no longer on disk: {path}", fg=typer.colors.YELLOW, err=True)


# -- correct ------------------------------------------------------------------------------------

_PEOPLE = typer.Argument(
    metavar="PERSON...", help="Emails, phone numbers, or names that match exactly one person."
)


def _identifier(c: Context, text: str) -> str:
    """An email or phone number for ``text``: itself, or that of the one person it names."""
    from graph_me.connectors.base import Party
    from graph_me.pipeline.tier0.people import PeopleContext
    from graph_me.query import graph

    people = PeopleContext.from_config(c.config.people)
    if "@" in text or sum(ch.isdigit() for ch in text) >= 6:
        if not people.keys(Party(email=text, phone=text)):
            raise _error(f"{text!r} is not an email or phone number graph-me can read.")
        return text.strip()
    if not c.db_path.exists():
        raise _error(f"No index to look {text!r} up in: give an email or phone number.")
    conn = db.connect(c.db_path, readonly=True)
    try:
        found = [graph.entity(conn, e) for e in graph.find_people(conn, text, c.config.people)]
    finally:
        conn.close()
    reachable = [
        (person, [a["value"] for a in person["aliases"] if a["kind"] in ("email", "phone")])
        for person in found
    ]
    reachable = [(person, ids) for person, ids in reachable if ids]
    if not reachable:
        raise _error(f"Nobody named {text!r} has an email or phone number. Give one instead.")
    if len(reachable) > 1:
        lines = [f"{text!r} matches several people. Give an email or phone number instead:"]
        lines += [f"  {person['name'] or '?'}: {', '.join(ids)}" for person, ids in reachable]
        raise _error("\n".join(lines))
    _, ids = reachable[0]
    return next((i for i in ids if "@" in i), ids[0])


def _correct(c: Context, change) -> None:
    """Apply ``change`` to overrides.yaml and save it."""
    from pydantic import ValidationError

    from graph_me import overrides as corrections

    path = c.config.overrides
    if path is None:
        raise _error("Corrections are turned off (`overrides: null` in config.yaml).")
    try:
        fixes = corrections.load(path)
        summary = change(fixes)
        corrections.save(path, fixes)
    except (ValueError, ValidationError) as exc:
        message = exc.errors()[0]["msg"] if isinstance(exc, ValidationError) else str(exc)
        raise _error(message.removeprefix("Value error, ")) from exc
    typer.echo(summary)
    typer.echo(f"Saved in {path}. Run `graph-me sync` to apply it.")


@app.command(rich_help_panel=CORRECT)
def link(ctx: typer.Context, people: Annotated[list[str], _PEOPLE]) -> None:
    """Say these emails or phone numbers belong to one person.

    Example: graph-me link sophie@work.example "+44 7700 900123"
    """
    from graph_me import overrides as corrections

    c = _ctx(ctx)
    if len(people) < 2:
        raise _error("Give at least two people or identifiers to link.")
    ids = list(dict.fromkeys(_identifier(c, p) for p in people))
    if len(ids) < 2:
        raise _error("These all name the same identifier: nothing to link.")

    def change(fixes: corrections.Overrides) -> str:
        corrections.link(fixes, ids)
        return f"{' = '.join(ids)}: one person"

    _correct(c, change)


@app.command(rich_help_panel=CORRECT)
def unlink(ctx: typer.Context, people: Annotated[list[str], _PEOPLE]) -> None:
    """Say these emails or phone numbers belong to different people, even if a contact card
    lists them together.

    Example: graph-me unlink sophie@work.example sophie@home.example
    """
    from graph_me import overrides as corrections

    c = _ctx(ctx)
    ids = list(dict.fromkeys(_identifier(c, p) for p in people))
    if len(ids) < 2:
        raise _error("Give at least two different emails or phone numbers.")

    def change(fixes: corrections.Overrides) -> str:
        corrections.unlink(fixes, ids)
        return f"{' / '.join(ids)}: different people"

    _correct(c, change)


@app.command(rich_help_panel=CORRECT)
def relate(
    ctx: typer.Context,
    person: Annotated[str, typer.Argument(metavar="PERSON", help="An email, phone or name.")],
    relation: Annotated[
        str | None,
        typer.Argument(metavar="RELATION", help="sibling, parent, spouse, landlord, ..."),
    ] = None,
    remove: Annotated[
        bool, typer.Option("--remove", help="Forget the relation you set for them.")
    ] = False,
) -> None:
    """Say how someone relates to you, so "my sister" or "my landlord" finds them.

    Example: graph-me relate sophie@example.com sibling
    """
    from graph_me import overrides as corrections

    c = _ctx(ctx)
    if not remove and relation is None:
        raise _error(f"Give a relation: {', '.join(corrections.relations())}.")
    if relation is not None and relation not in corrections.relations():
        raise _error(
            f"Unknown relation {relation!r}. Use one of: {', '.join(corrections.relations())}."
        )
    who_id = _identifier(c, person)

    def change(fixes: corrections.Overrides) -> str:
        corrections.relate(fixes, who_id, None if remove else relation)
        return f"{who_id}: no relation set" if remove else f"{who_id}: your {relation}"

    _correct(c, change)


@app.command("overrides", rich_help_panel=CORRECT)
def show_overrides(ctx: typer.Context) -> None:
    """Show your corrections (overrides.yaml) and where they are saved."""
    c = _ctx(ctx)
    path = c.config.overrides
    if path is None:
        typer.echo("Corrections are turned off (`overrides: null` in config.yaml).")
        return
    if not path.exists():
        typer.echo(f"No corrections yet. They will be saved in {path}.")
        return
    typer.echo(f"# {path}")
    typer.echo(path.read_text(encoding="utf-8"), nl=False)


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
    except db.RunLocked as exc:
        raise _error(str(exc)) from exc
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
    except db.RunLocked as exc:
        raise _error(str(exc)) from exc
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
    profile: Annotated[str | None, _PROFILE] = None,
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
            build_app(_service(c, "ui", profile), token), log_level="warning", access_log=False
        )  # fmt: skip
    )
    server.run(sockets=[sock])


@app.command(rich_help_panel=INTERFACES)
def mcp(ctx: typer.Context, profile: Annotated[str | None, _PROFILE] = None) -> None:
    """Run the MCP server for AI assistants. Your app starts it; you don't run it by hand.

    Register it once with: graph-me install-mcp. With --profile, the assistant only sees that
    profile's sources, and cannot change it.
    """
    from graph_me.mcp_server import build_server

    c = _ctx(ctx)
    _require_index(c)
    build_server(_service(c, "mcp", profile)).run()
