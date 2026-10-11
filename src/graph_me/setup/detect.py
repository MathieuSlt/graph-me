"""``graph-me init --detect``: find likely sources and settings on this computer.

Looks only at well-known places (Documents, Desktop, Downloads, Obsidian vaults, msgvault, .vcf
files next to them) and reads nothing inside files. The user confirms each source before it is
written to ``config.yaml``.
"""

from __future__ import annotations

import contextlib
import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

# Calling codes for common locale territories (en_GB -> GB -> 44).
CALLING_CODES = {
    "AE": "971", "AR": "54", "AT": "43", "AU": "61", "BE": "32", "BG": "359", "BR": "55",
    "CA": "1", "CH": "41", "CL": "56", "CN": "86", "CO": "57", "CZ": "420", "DE": "49",
    "DK": "45", "DZ": "213", "EE": "372", "EG": "20", "ES": "34", "FI": "358", "FR": "33",
    "GB": "44", "GR": "30", "HK": "852", "HR": "385", "HU": "36", "ID": "62", "IE": "353",
    "IL": "972", "IN": "91", "IS": "354", "IT": "39", "JP": "81", "KE": "254", "KR": "82",
    "LT": "370", "LU": "352", "LV": "371", "MA": "212", "MX": "52", "MY": "60", "NG": "234",
    "NL": "31", "NO": "47", "NZ": "64", "PE": "51", "PH": "63", "PK": "92", "PL": "48",
    "PT": "351", "RO": "40", "RS": "381", "RU": "7", "SA": "966", "SE": "46", "SG": "65",
    "SI": "386", "SK": "421", "TH": "66", "TN": "216", "TR": "90", "TW": "886", "UA": "380",
    "US": "1", "VN": "84", "ZA": "27",
}  # fmt: skip
FOLDERS = (("documents", "Documents", "self"), ("desktop", "Desktop", "self"),
           ("downloads", "Downloads", "untrusted"))  # fmt: skip
OBSIDIAN_ICLOUD = "Library/Mobile Documents/iCloud~md~obsidian/Documents"
VAULT_DEPTH = 3


@dataclass
class Candidate:
    name: str
    source: dict
    why: str  # shown to the user before they confirm


@dataclass
class Detected:
    candidates: list[Candidate] = field(default_factory=list)
    timezone: str | None = None
    phone_country_code: str | None = None


def detect(home: Path | None = None, env: dict[str, str] | None = None) -> Detected:
    home = (home or Path.home()).expanduser()
    env = dict(os.environ if env is None else env)
    found = Detected(timezone=_timezone(env), phone_country_code=_calling_code(env))
    folders: list[Path] = []
    for name, folder, trust in FOLDERS:
        path = home / folder
        if path.is_dir():
            folders.append(path)
            kind = "files from anyone" if trust == "untrusted" else "your files"
            found.candidates.append(
                Candidate(name, {"type": "filesystem", "paths": [str(path)], "trust": trust},
                          f"{path} ({kind})")
            )  # fmt: skip
    vaults = [v for v in _vaults(home) if not any(v.is_relative_to(f) for f in folders)]
    if vaults:
        found.candidates.append(
            Candidate("notes", {"type": "filesystem", "paths": [str(v) for v in vaults]},
                      f"Obsidian {'vault' if len(vaults) == 1 else 'vaults'}: "
                      + ", ".join(str(v) for v in vaults))
        )  # fmt: skip
    msgvault = home / ".msgvault"  # msgvault's default home
    if (msgvault / "msgvault.db").is_file():
        found.candidates.append(
            Candidate("messages", {"type": "msgvault", "db": str(msgvault)},
                      f"msgvault archive in {msgvault} (mail and chats)")
        )  # fmt: skip
    cards = sorted(p for f in folders for p in f.glob("*.vcf") if p.is_file())
    if cards:
        found.candidates.append(
            Candidate("contacts", {"type": "vcard", "paths": [str(p) for p in cards]},
                      "address book: " + ", ".join(str(p) for p in cards))
        )  # fmt: skip
    return found


def _vaults(home: Path) -> list[Path]:
    """Folders holding ``.obsidian/``, a few levels below home (dot folders and Library skipped)."""
    out: list[Path] = []
    icloud = home / OBSIDIAN_ICLOUD
    if icloud.is_dir():
        out += sorted(p for p in icloud.iterdir() if (p / ".obsidian").is_dir())

    def walk(folder: Path, depth: int) -> None:
        if (folder / ".obsidian").is_dir():
            out.append(folder)
            return
        if depth == 0:
            return
        try:
            children = sorted(folder.iterdir())
        except OSError:
            return
        for child in children:
            if child.is_dir() and not child.name.startswith(".") and child.name != "Library":
                walk(child, depth - 1)

    walk(home, VAULT_DEPTH)
    return list(dict.fromkeys(out))


def _timezone(env: dict[str, str]) -> str | None:
    names = [env.get("TZ", "").lstrip(":")]
    with contextlib.suppress(IndexError, OSError):
        names.append(str(Path("/etc/localtime").resolve()).split("zoneinfo/", 1)[1])
    for name in names:
        try:
            if name and ZoneInfo(name):
                return name
        except (ZoneInfoNotFoundError, ValueError):
            continue
    return None


def _calling_code(env: dict[str, str]) -> str | None:
    for var in ("LC_ALL", "LC_TELEPHONE", "LC_CTYPE", "LANG"):
        value = env.get(var, "")
        territory = value.split(".", 1)[0].split("_")[1:2]  # "en_GB.UTF-8" -> ["GB"]
        if territory and territory[0].upper() in CALLING_CODES:
            return CALLING_CODES[territory[0].upper()]
    return None


def render(detected: Detected, chosen: list[Candidate]) -> str:
    """config.yaml text for the chosen sources and detected settings."""
    data = {
        "sources": {c.name: c.source for c in chosen},
        "people": {
            "phone_country_code": detected.phone_country_code,
            "me": [],
            "timezone": detected.timezone,
        },
        "blacklist": {"paths": [], "contacts": [], "patterns": []},
    }
    return (
        "# graph-me configuration, written by `graph-me init --detect`.\n"
        "# Every option is explained in config-template.yaml:\n"
        "# https://github.com/MathieuSlt/graph-me/blob/main/config-template.yaml\n\n"
        + yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
    )
