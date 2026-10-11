"""``graph-me install-mcp``: register graph-me's MCP server with an AI app.

Each app keeps its MCP servers in its own config file. graph-me adds one entry that starts
``graph-me --config <config.yaml> mcp`` with absolute paths, keeps a ``.bak`` copy of the file,
and never touches the app's other settings. Claude Code is registered through its own CLI.
"""

from __future__ import annotations

import json
import re
import shutil
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

CLIENTS = ("claude-code", "claude-desktop", "cursor", "codex")


@dataclass
class Plan:
    client: str
    name: str
    command: list[str]  # the MCP server command line
    path: Path | None = None  # config file to edit; None for claude-code
    before: str = ""
    after: str = ""
    run: list[str] | None = None  # a command to run instead of editing a file
    already: bool = False  # the same entry is already there


def executable() -> str:
    """The graph-me executable, as an absolute path an app can start without your shell."""
    found = shutil.which("graph-me")
    if found:
        return str(Path(found).resolve())
    return str(Path(sys.argv[0]).resolve())


def server_command(config_file: Path) -> list[str]:
    return [executable(), "--config", str(config_file.expanduser().resolve()), "mcp"]


def config_path(client: str, home: Path, platform: str = sys.platform) -> Path | None:
    if client == "claude-desktop":
        if platform == "darwin":
            return home / "Library/Application Support/Claude/claude_desktop_config.json"
        return home / ".config/Claude/claude_desktop_config.json"
    if client == "cursor":
        return home / ".cursor/mcp.json"
    if client == "codex":
        return home / ".codex/config.toml"
    return None


def plan(client: str, name: str, config_file: Path, home: Path | None = None) -> Plan:
    if client not in CLIENTS:
        raise ValueError(f"unknown app {client!r} (known: {', '.join(CLIENTS)})")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
        raise ValueError(f"--name {name!r}: use letters, digits, - and _ only")
    home = (home or Path.home()).expanduser()
    command = server_command(config_file)
    if client == "claude-code":
        run = ["claude", "mcp", "add", "--scope", "user", name, "--", *command]
        return Plan(client, name, command, run=run)
    path = config_path(client, home)
    before = path.read_text(encoding="utf-8") if path.exists() else ""
    if client == "codex":
        after, already = _codex(before, name, command, path)
    else:
        after, already = _json(before, name, command, path)
    return Plan(client, name, command, path=path, before=before, after=after, already=already)


def apply(p: Plan) -> None:
    """Write the planned config (keeping a .bak copy). claude-code: run its CLI."""
    if p.run:
        import subprocess

        if shutil.which(p.run[0]) is None:
            raise FileNotFoundError("the `claude` command is not on PATH")
        subprocess.run(p.run, check=True)
        return
    if p.already:
        return
    p.path.parent.mkdir(parents=True, exist_ok=True)
    if p.path.exists():
        shutil.copy2(p.path, p.path.with_name(p.path.name + ".bak"))
    p.path.write_text(p.after, encoding="utf-8")


def _json(text: str, name: str, command: list[str], path: Path) -> tuple[str, bool]:
    try:
        data = json.loads(text) if text.strip() else {}
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} is not valid JSON ({exc}); fix it first") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path} is not a JSON object; fix it first")
    servers = data.setdefault("mcpServers", {})
    entry = {"command": command[0], "args": command[1:]}
    if servers.get(name) == entry:
        return text, True
    servers[name] = entry
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n", False


def _codex(text: str, name: str, command: list[str], path: Path) -> tuple[str, bool]:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"{path} is not valid TOML ({exc}); fix it first") from exc
    current = data.get("mcp_servers", {}).get(name)
    entry = {"command": command[0], "args": command[1:]}
    if current is not None:
        if {k: current.get(k) for k in entry} == entry:
            return text, True
        raise ValueError(
            f"{path} already has [mcp_servers.{name}]: edit it by hand, or pick another --name"
        )
    block = (f"\n[mcp_servers.{name}]\ncommand = {json.dumps(command[0])}\n"
             f"args = {json.dumps(command[1:])}\n")  # fmt: skip
    return text.rstrip("\n") + ("\n" if text.strip() else "") + block, False
