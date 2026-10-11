"""Calendar connector: ``.ics`` files (calendar exports, subscribed calendars saved to disk).

Config keys (under ``sources.<name>``):

- ``paths``: ``.ics`` files or folders containing them
- ``trust``: ``known`` (default: invitations come from other people), ``self`` or ``untrusted``

Each event (each changed occurrence of a recurring one too) becomes an ``event`` item: the
organizer is its author and the attendees its recipients, so meetings link people like mail
does. A recurring event is one item that says how it repeats. A file that can't be parsed
makes the source unavailable for this run, so its events are never forgotten by mistake.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Iterator
from datetime import UTC, date, datetime, time
from pathlib import Path

from graph_me.config import BlacklistConfig, SourceConfig
from graph_me.connectors.base import Item, Party, SourceUnavailable


def _when(value) -> datetime | None:
    """A DTSTART value as an aware datetime (all-day events at noon UTC: the day stays put)."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, date):
        return datetime.combine(value, time(12), tzinfo=UTC)
    return None


def _decoded(event, key: str):
    """A property's value, or None when it is missing or malformed (a bad date is not fatal)."""
    if event.get(key) is None:
        return None
    try:
        return event.decoded(key)
    except (ValueError, TypeError, KeyError):
        return None


def _party(address) -> Party | None:
    if address is None:
        return None
    text = str(address)
    email = text[7:] if text.lower().startswith("mailto:") else (text if "@" in text else None)
    params = getattr(address, "params", {}) or {}
    name = params.get("CN")
    if not (email or name):
        return None
    return Party(name=str(name).strip('"') if name else None, email=email)


def _many(value) -> list:
    if value is None:
        return []
    return list(value) if isinstance(value, list) else [value]


def _text(component, key: str) -> str:
    value = component.get(key)
    return str(value).strip() if value is not None else ""


def event_item(event, external_id: str, uri: str, trust: str, version: str) -> Item:
    start = _when(_decoded(event, "DTSTART"))
    end = _when(_decoded(event, "DTEND"))
    organizer = _party(event.get("ORGANIZER"))
    attendees = [p for p in (_party(a) for a in _many(event.get("ATTENDEE"))) if p]
    lines = [_text(event, "SUMMARY") or "(no title)"]
    if start:
        span = f"{start:%Y-%m-%d %H:%M}" + (f" to {end:%Y-%m-%d %H:%M}" if end else "")
        lines.append(f"When: {span}")
    if location := _text(event, "LOCATION"):
        lines.append(f"Where: {location}")
    if rrule := event.get("RRULE"):
        lines.append("Repeats: " + rrule.to_ical().decode())
    names = [p.name or p.email for p in attendees]
    if organizer:
        lines.append(f"Organizer: {organizer.name or organizer.email}")
    if names:
        lines.append("Attendees: " + ", ".join(n for n in names if n))
    if description := _text(event, "DESCRIPTION"):
        lines.append(description)
    return Item(
        external_id=external_id,
        version=version,
        kind="event",
        title=_text(event, "SUMMARY") or "(no title)",
        uri=uri,
        thread_id=_text(event, "UID") or None,  # occurrences of one recurring event
        created_at=start,
        modified_at=_when(_decoded(event, "LAST-MODIFIED")),
        author=organizer,
        recipients=[a for a in attendees if not organizer or a.email != organizer.email],
        text="\n".join(lines),
        trust=trust,
    )


class IcsConnector:
    type = "ics"

    def __init__(self, name: str, source: SourceConfig, blacklist: BlacklistConfig) -> None:
        extra = source.model_extra or {}
        self.name = name
        self.roots = [Path(p).expanduser().resolve() for p in extra.get("paths", [])]
        self.blocked = [Path(p).expanduser().resolve() for p in blacklist.paths]
        self.trust = extra.get("trust", "known")
        if self.trust not in ("self", "known", "untrusted"):
            raise ValueError(f"source {name!r}: trust must be self, known or untrusted")
        self._events: dict[str, tuple[object, str, str]] = {}  # id -> (event, uri, version)

    def _files(self) -> Iterator[Path]:
        missing = [str(r) for r in self.roots if not r.exists()]
        if missing:
            raise SourceUnavailable(
                f"source {self.name!r}: {', '.join(missing)} not found. Reconnect the drive, "
                "or remove the path from config.yaml if you deleted it on purpose."
            )
        for root in self.roots:
            files = [root] if root.is_file() else sorted(root.rglob("*.ics"))
            for f in files:
                if not any(f == b or f.is_relative_to(b) for b in self.blocked):
                    yield f

    def list_ids(self) -> Iterator[tuple[str, str]]:
        from icalendar import Calendar

        self._events.clear()
        for path in self._files():
            try:
                raw = path.read_bytes()
                calendars = Calendar.from_ical(raw, multiple=True)
            except (OSError, ValueError) as exc:
                raise SourceUnavailable(
                    f"source {self.name!r}: {path} could not be read ({exc}); its events "
                    "were left as they were"
                ) from exc
            # A cut-off file parses without error but loses events: never take it for a calendar
            # whose events were deleted.
            found = sum(1 for c in calendars for _ in c.walk("VEVENT"))
            if found < raw.upper().count(b"BEGIN:VEVENT"):
                raise SourceUnavailable(
                    f"source {self.name!r}: {path} looks incomplete (it may still be being "
                    "written); its events were left as they were"
                )
            for calendar in calendars:
                for n, event in enumerate(calendar.walk("VEVENT")):
                    raw = event.to_ical()
                    digest = hashlib.sha256(raw).hexdigest()
                    uid = _text(event, "UID") or f"event-{n}-{digest[:12]}"
                    recurrence = event.get("RECURRENCE-ID")
                    key = uid + (f"@{recurrence.to_ical().decode()}" if recurrence else "")
                    external_id = f"{path}#{key}"
                    self._events[external_id] = (event, f"{path}#{uid}", digest[:32])
                    yield external_id, digest[:32]

    def fetch(self, external_ids: Iterable[str]) -> Iterator[Item]:
        for external_id in external_ids:
            if external_id in self._events:
                event, uri, version = self._events[external_id]
                yield event_item(event, external_id, uri, self.trust, version)
