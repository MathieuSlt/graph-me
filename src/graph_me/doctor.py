"""``graph-me doctor``: check the setup and say how to fix what is wrong.

Read-only like everything else: it opens each source the way ``scan`` would (listing one item at
most) and reads the index, but changes nothing.
"""

from __future__ import annotations

import importlib.util
import itertools
import json
import shutil
import stat
import subprocess
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from graph_me import config, env
from graph_me import overrides as corrections
from graph_me.connectors import registry
from graph_me.connectors.base import SourceUnavailable
from graph_me.setup import schedule
from graph_me.store import db

OK, WARN, FAIL, INFO = "ok", "warn", "fail", "info"


@dataclass
class Check:
    status: str  # ok | warn | fail | info
    label: str
    message: str
    hint: str | None = None


def run(
    cfg: config.Config,
    config_file: Path,
    out: Path,
    *,
    home: Path | None = None,
    runner=subprocess.run,
) -> list[Check]:
    home = (home or Path.home()).expanduser()
    checks: list[Check] = []
    add = checks.append

    if config_file.exists():
        add(Check(OK, "config", str(config_file)))
    else:
        add(Check(FAIL, "config", f"{config_file} is missing",
                  "Run `graph-me init --detect` (or `graph-me init`)."))  # fmt: skip
    if not cfg.sources:
        hint = "List your folders under `sources:`, or run `graph-me init --detect`."
        add(Check(FAIL, "sources", "none listed in config.yaml", hint))
    for name, source in cfg.sources.items():
        add(_source(name, source, cfg))
    if cfg.overrides is not None and cfg.overrides.exists():
        try:
            fixes = corrections.load(cfg.overrides)
            count = len(fixes.people) + len(fixes.not_same) + len(fixes.reject)
            add(Check(OK, "corrections", f"{count} in {cfg.overrides}"))
        except Exception as exc:  # YAML or schema error
            add(Check(FAIL, "corrections", f"{cfg.overrides} has errors: {exc}",
                      "Fix the file, or restore overrides.yaml.bak next to it."))  # fmt: skip

    add(_output(out))
    checks += _index(out / config.DB_NAME, cfg)

    e = env.check()
    if not e.fts5:
        add(Check(FAIL, "sqlite", "this Python's SQLite has no full-text search (FTS5)",
                  "Install with `uv tool install --managed-python graph-me`."))  # fmt: skip
    elif not e.uv_managed:
        add(Check(WARN, "python", f"{e.python_version} is not managed by uv",
                  "Install with `uv tool install --managed-python graph-me`."))  # fmt: skip
    else:
        add(Check(OK, "python", f"{e.python_version}, SQLite {e.sqlite_version}"))
    extras = [name for name, module in (("medium", "fastembed"), ("ui", "uvicorn"),
                                        ("ocr", "rapidocr")) if _has(module)]  # fmt: skip
    add(Check(INFO, "extras", ", ".join(extras) if extras else "none (base install, Tier 0)"))

    add(_skill(home))
    add(_mcp(runner))
    add(_schedule(home, runner))
    return checks


def _has(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def _source(name: str, source: config.SourceConfig, cfg: config.Config) -> Check:
    label = f"source {name}"
    try:
        connector = registry.create(name, source, cfg.blacklist)
        first = list(itertools.islice(connector.list_ids(), 1))
    except SourceUnavailable as exc:
        return Check(FAIL, label, str(exc).removeprefix(f"source {name!r}: "))
    except Exception as exc:  # bad config keys, unreadable database...
        return Check(FAIL, label, f"{type(exc).__name__}: {exc}",
                     "Check this source in config.yaml (see config-template.yaml).")  # fmt: skip
    if not first:
        return Check(WARN, label, f"{source.type}: reachable, but nothing to read in it",
                     "Check its paths, exclude patterns and blacklist.")  # fmt: skip
    return Check(OK, label, f"{source.type}: reachable")


def _output(out: Path) -> Check:
    if not out.exists():
        return Check(FAIL, "index folder", f"{out} is missing", "Run `graph-me init`.")
    mode = stat.S_IMODE(out.stat().st_mode)
    if mode & 0o077:
        return Check(FAIL, "index folder", f"{out} can be read by others (mode {mode:o})",
                     f"Run `chmod 700 {out}`.")  # fmt: skip
    return Check(OK, "index folder", f"{out} (private to you)")


def _index(path: Path, cfg: config.Config) -> list[Check]:
    from graph_me.service import freshness

    if not path.exists():
        return [Check(FAIL, "index", "not built yet", "Run `graph-me scan`.")]
    conn = db.connect(path, readonly=True)
    try:
        version = db.schema_version(conn)
        if version > db.SCHEMA_VERSION:
            return [Check(FAIL, "index", f"schema v{version} is newer than this graph-me",
                          "Upgrade graph-me.")]  # fmt: skip
        items = conn.execute("SELECT count(*) FROM items").fetchone()[0]
        index = freshness(conn, cfg.index.stale_after_days)
        last_run = json.loads(db.get_meta(conn, "last_run") or "null") or {}
        unseen = 0
        if cfg.profiles and version >= 5:
            unseen = conn.execute(
                """SELECT count(*) FROM aliases a WHERE a.kind IN ('email', 'phone', 'handle')
                   AND NOT EXISTS (SELECT 1 FROM item_identifiers s
                                   WHERE s.kind = a.kind AND s.value = a.value)"""
            ).fetchone()[0]
    finally:
        conn.close()
    checks = []
    if not items:
        checks.append(Check(WARN, "index", "empty", "Run `graph-me scan`."))
    elif version < db.SCHEMA_VERSION:
        checks.append(Check(INFO, "index", f"{items} items, schema v{version} "
                            f"(upgraded to v{db.SCHEMA_VERSION} on the next scan)"))  # fmt: skip
    else:
        checks.append(Check(OK, "index", f"{items} items"))
    if index["stale"]:
        checks.append(Check(WARN, "freshness", f"last synced {index['age_days']} days ago",
                            "Run `graph-me sync`, or `graph-me schedule install`."))  # fmt: skip
    elif index["last_sync_at"]:
        checks.append(Check(OK, "freshness", f"last synced {index['last_sync_at']}"))
    if cfg.profiles:
        if unseen:
            checks.append(Check(WARN, "profiles", f"{unseen} emails or phone numbers were indexed "
                                "before profiles could tell where they came from, so profiles "
                                "hide them", "Run `graph-me scan --reread` once."))  # fmt: skip
        else:
            checks.append(Check(OK, "profiles", ", ".join(sorted(cfg.profiles))))
    for name, stats in (last_run.get("sources") or {}).items():
        if stats.get("failure"):
            checks.append(Check(WARN, f"last run: {name}", stats["failure"].splitlines()[0]))
    return checks


def _skill(home: Path) -> Check:
    installed = home / ".claude/skills/graph-me/SKILL.md"
    if not installed.exists():
        return Check(INFO, "skill", "not installed (Claude Code)",
                     "Run `graph-me install-skill` if you use Claude Code.")  # fmt: skip
    packaged = resources.files("graph_me").joinpath("skill/SKILL.md").read_text(encoding="utf-8")
    if installed.read_text(encoding="utf-8") != packaged:
        return Check(WARN, "skill", f"{installed} differs from this version",
                     "Run `graph-me install-skill --force` to update it.")  # fmt: skip
    return Check(OK, "skill", str(installed))


def _mcp(runner) -> Check:
    if shutil.which("claude") is None:
        return Check(INFO, "mcp", "Claude Code not found; for other apps see `install-mcp`")
    try:
        found = runner(["claude", "mcp", "get", "graph-me"], capture_output=True, text=True,
                       timeout=20).returncode == 0  # fmt: skip
    except (OSError, subprocess.TimeoutExpired):
        return Check(INFO, "mcp", "could not ask Claude Code")
    if found:
        return Check(OK, "mcp", "registered in Claude Code")
    return Check(WARN, "mcp", "not registered in Claude Code",
                 "Run `graph-me install-mcp --client claude-code`.")  # fmt: skip


def _schedule(home: Path, runner) -> Check:
    try:
        st = schedule.status(home, runner=runner)
    except schedule.ScheduleError as exc:
        return Check(INFO, "schedule", str(exc))
    if not st.installed:
        return Check(INFO, "schedule", "none: sync runs when you run it",
                     "Run `graph-me schedule install` to sync regularly.")  # fmt: skip
    every = f"every {st.every // 3600}h" if st.every and st.every % 3600 == 0 else (
        f"every {st.every // 60}m" if st.every else "installed")  # fmt: skip
    if not st.active:
        return Check(WARN, "schedule", f"{every}, but not loaded",
                     "Run `graph-me schedule install` again.")  # fmt: skip
    return Check(OK, "schedule", every)
