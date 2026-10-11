"""``graph-me export bundle``: copy what a search finds into one folder, with an index.

For a tax return, a visa file or a move: the files themselves (copied, never moved), each mail,
chat message or meeting as a Markdown file with who, when and where it came from, the files
their attachments were saved as, and ``INDEX.md`` listing everything with its original place.
Text is redacted unless you ask otherwise, like ``query --reveal``. The folder is private to you.
It is a terminal command only: AI assistants and the web UI can't write files.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from graph_me.service import Service

_UNSAFE = re.compile(r"[^\w.\-]+")  # no spaces either: INDEX.md links stay plain
FOLDERS = {"email": "messages", "message": "messages", "event": "events", "contact": "contacts"}


@dataclass
class Bundle:
    folder: Path
    files: list[Path] = field(default_factory=list)
    messages: list[Path] = field(default_factory=list)  # messages, events, contacts as .md
    missing: list[str] = field(default_factory=list)  # files no longer on disk


def _name(text: str, limit: int = 60) -> str:
    clean = _UNSAFE.sub("_", text).strip(" ._") or "untitled"
    return clean[:limit].rstrip(" ._")


def plan(service: Service, query: str, **search) -> list[dict]:
    """What a bundle for ``query`` would hold: the search results, best first."""
    return service.search(query, **search)["answer_items"]


def write(service: Service, items: list[dict], folder: Path, *, reveal: bool = False) -> Bundle:
    if folder.exists() and any(folder.iterdir()):
        raise FileExistsError(f"{folder} is not empty: pick a new or empty folder")
    folder.mkdir(parents=True, exist_ok=True)
    folder.chmod(0o700)
    out = Bundle(folder)
    rows = []
    for n, item in enumerate(items, 1):
        day = (item.get("date") or "")[:10] or "undated"
        prefix = f"{n:03d}_{day}"
        if item["kind"] == "file":
            source = Path(item.get("path") or item["uri"])
            target = folder / "files" / f"{prefix}_{_name(source.stem)}{source.suffix.lower()}"
            if source.is_file():
                target.parent.mkdir(exist_ok=True)
                shutil.copy2(source, target)
                out.files.append(target)
                rows.append((item, target))
            else:
                out.missing.append(str(source))
                rows.append((item, None))
            continue
        full = service.get_item(item["id"], reveal=reveal, max_words=None)
        sub = FOLDERS.get(item["kind"], "items")
        target = folder / sub / f"{prefix}_{_name(item.get('title') or item['kind'])}.md"
        target.parent.mkdir(exist_ok=True)
        head = [f"# {full.get('title') or '(untitled)'}", ""]
        for label, value in (("Kind", item["kind"]), ("Date", item.get("date")),
                             ("From", item.get("from")), ("To", ", ".join(item.get("to") or [])),
                             ("Source", item["source"]), ("Where", item.get("uri"))):  # fmt: skip
            if value:
                head.append(f"- **{label}:** {value}")
        if full.get("redacted"):
            head.append(f"- **Hidden:** {', '.join(full['redacted'])} (export with --reveal)")
        target.write_text("\n".join([*head, "", full.get("text") or ""]) + "\n", encoding="utf-8")
        out.messages.append(target)
        rows.append((item, target))
        for copy in item.get("saved_as", []):  # the attachment, where you saved it
            source = Path(copy["path"])
            if source.is_file():
                saved = folder / "files" / f"{prefix}_attachment_{_name(source.stem)}"
                saved = saved.with_suffix(source.suffix.lower())
                saved.parent.mkdir(exist_ok=True)
                shutil.copy2(source, saved)
                out.files.append(saved)
    lines = ["# Bundle", "", f"{len(rows)} items, best match first. Copies: the originals are "
             "where they were.", "", "| # | Date | Kind | Title | From | In this folder | "
             "Original |", "| --- | --- | --- | --- | --- | --- | --- |"]  # fmt: skip
    for n, (item, target) in enumerate(rows, 1):
        local = f"[{target.relative_to(folder)}]({target.relative_to(folder)})" if target \
            else "missing"  # fmt: skip
        cells = [str(n), (item.get("date") or "")[:10], item["kind"],
                 (item.get("title") or "").replace("|", "/"), item.get("from") or "",
                 local, f"`{item.get('path') or item.get('uri') or ''}`"]  # fmt: skip
        lines.append("| " + " | ".join(cells) + " |")
    (folder / "INDEX.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out
