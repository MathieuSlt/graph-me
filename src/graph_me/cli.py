"""graph-me command-line interface."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
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


@app.command()
def scan() -> None:
    """Build the graph from all configured sources."""
    _not_yet("M1")


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
def query() -> None:
    """Ask the graph from the terminal."""
    _not_yet("M1")


@app.command()
def ui() -> None:
    """Start the local, read-only web UI."""
    _not_yet("M6")


@app.command()
def mcp() -> None:
    """Start the read-only MCP server (stdio)."""
    _not_yet("M4")
