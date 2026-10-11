"""``graph-me schedule``: run ``graph-me sync`` regularly, with the system's own scheduler.

macOS: a launchd agent (``~/Library/LaunchAgents/io.github.graph-me.sync.plist``).
Linux: a systemd user timer (``~/.config/systemd/user/graph-me-sync.{service,timer}``).

There is no graph-me daemon: the scheduler starts ``graph-me sync --scheduled``, which appends
to ``graph-out/sync.log``. The mass-forget guard still applies, so an unplugged drive is skipped,
never wiped, and the run lock keeps a scheduled sync from overlapping a manual one.
"""

from __future__ import annotations

import os
import plistlib
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

LABEL = "io.github.graph-me.sync"
UNIT = "graph-me-sync"
LOG_NAME = "sync.log"
LOG_MAX_BYTES = 1024 * 1024
MIN_EVERY = 15 * 60
_EVERY = re.compile(r"^(\d+)\s*([mhd])$")
_UNITS = {"m": 60, "h": 3600, "d": 86400}


class ScheduleError(RuntimeError):
    pass


@dataclass
class Status:
    installed: bool
    active: bool
    files: list[Path]
    every: int | None = None  # seconds


def parse_every(text: str) -> int:
    """``30m``, ``6h``, ``1d`` -> seconds (at least 15 minutes)."""
    m = _EVERY.match(text.strip().lower())
    if not m:
        raise ValueError(f"{text!r}: use minutes, hours or days, e.g. 30m, 6h, 1d")
    seconds = int(m[1]) * _UNITS[m[2]]
    if seconds < MIN_EVERY:
        raise ValueError("sync at most every 15 minutes (15m)")
    return seconds


def sync_command(exe: str, config_file: Path) -> list[str]:
    return [exe, "--config", str(config_file.expanduser().resolve()), "sync", "--scheduled"]


def platform_kind(platform: str = sys.platform) -> str:
    if platform == "darwin":
        return "launchd"
    if platform.startswith("linux"):
        return "systemd"
    raise ScheduleError(f"scheduling is supported on macOS and Linux, not {platform}")


def unit_files(home: Path, platform: str = sys.platform) -> list[Path]:
    if platform_kind(platform) == "launchd":
        return [home / "Library/LaunchAgents" / f"{LABEL}.plist"]
    folder = home / ".config/systemd/user"
    return [folder / f"{UNIT}.service", folder / f"{UNIT}.timer"]


def render_launchd(command: list[str], log: Path, every: int) -> bytes:
    return plistlib.dumps(
        {
            "Label": LABEL,
            "ProgramArguments": command,
            "StartInterval": every,
            "RunAtLoad": False,
            "ProcessType": "Background",
            "LowPriorityIO": True,
            "Nice": 10,
            "StandardOutPath": str(log),
            "StandardErrorPath": str(log),
        }
    )


def _sd_quote(arg: str) -> str:
    return '"' + arg.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_systemd(command: list[str], log: Path, every: int) -> tuple[str, str]:
    service = (
        "[Unit]\nDescription=graph-me sync (read-only index of your files, mail and chats)\n\n"
        "[Service]\nType=oneshot\n"
        f"ExecStart={' '.join(_sd_quote(a) for a in command)}\n"
        f"StandardOutput=append:{log}\nStandardError=append:{log}\n"
        "Nice=10\nIOSchedulingClass=idle\n"
    )
    timer = (
        "[Unit]\nDescription=Run graph-me sync regularly\n\n"
        f"[Timer]\nOnBootSec=10min\nOnUnitActiveSec={every}s\nPersistent=true\n\n"
        "[Install]\nWantedBy=timers.target\n"
    )
    return service, timer


def install(
    command: list[str],
    log: Path,
    every: int,
    home: Path | None = None,
    platform: str = sys.platform,
    runner=subprocess.run,
) -> list[Path]:
    """Write the unit files and load them. Installing again replaces the schedule."""
    home = (home or Path.home()).expanduser()
    files = unit_files(home, platform)
    files[0].parent.mkdir(parents=True, exist_ok=True)
    if platform_kind(platform) == "launchd":
        target = f"gui/{os.getuid()}"
        runner(["launchctl", "bootout", f"{target}/{LABEL}"], capture_output=True)
        files[0].write_bytes(render_launchd(command, log, every))
        _run(runner, ["launchctl", "bootstrap", target, str(files[0])])
    else:
        service, timer = render_systemd(command, log, every)
        files[0].write_text(service, encoding="utf-8")
        files[1].write_text(timer, encoding="utf-8")
        _run(runner, ["systemctl", "--user", "daemon-reload"])
        _run(runner, ["systemctl", "--user", "enable", "--now", f"{UNIT}.timer"])
    return files


def remove(
    home: Path | None = None, platform: str = sys.platform, runner=subprocess.run
) -> list[Path]:
    home = (home or Path.home()).expanduser()
    files = [f for f in unit_files(home, platform) if f.exists()]
    if platform_kind(platform) == "launchd":
        runner(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], capture_output=True)
    else:
        runner(["systemctl", "--user", "disable", "--now", f"{UNIT}.timer"], capture_output=True)
    for f in files:
        f.unlink()
    if platform_kind(platform) == "systemd" and files:
        runner(["systemctl", "--user", "daemon-reload"], capture_output=True)
    return files


def status(home: Path | None = None, platform: str = sys.platform, runner=subprocess.run) -> Status:
    home = (home or Path.home()).expanduser()
    files = unit_files(home, platform)
    if not files[0].exists():
        return Status(installed=False, active=False, files=files)
    if platform_kind(platform) == "launchd":
        every = plistlib.loads(files[0].read_bytes()).get("StartInterval")
        check = ["launchctl", "print", f"gui/{os.getuid()}/{LABEL}"]
    else:
        m = re.search(r"OnUnitActiveSec=(\d+)s", files[1].read_text(encoding="utf-8")) \
            if files[1].exists() else None  # fmt: skip
        every = int(m[1]) if m else None
        check = ["systemctl", "--user", "is-active", "--quiet", f"{UNIT}.timer"]
    try:
        active = runner(check, capture_output=True).returncode == 0
    except FileNotFoundError:
        active = False
    return Status(installed=True, active=active, files=files, every=every)


def rotate_log(out: Path) -> None:
    """Keep sync.log under 1 MB: the previous one becomes sync.log.1."""
    log = out / LOG_NAME
    if log.exists() and log.stat().st_size > LOG_MAX_BYTES:
        log.replace(out / f"{LOG_NAME}.1")


def _run(runner, args: list[str]) -> None:
    try:
        result = runner(args, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise ScheduleError(f"`{args[0]}` is not available on this computer") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise ScheduleError(f"`{' '.join(args[:3])}` failed: {detail}")
