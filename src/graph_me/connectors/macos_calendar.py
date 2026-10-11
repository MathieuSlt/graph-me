"""macOS Calendar connector: every calendar the Calendar app shows (iCloud, Google, Exchange...)
through Apple's EventKit. Needs the ``[macos]`` extra and your permission. Read-only.

Config keys (under ``sources.<name>``):

- ``calendars``: only these calendars (their names); empty means all
- ``past_days`` (default 730) and ``future_days`` (default 365): the window read. Events that
  leave it (older than ``past_days``) are forgotten like deleted ones.
- ``trust``: ``known`` (default)

The first run asks macOS for access to your calendars ("graph-me" or your terminal app would
like to access your calendars). Reading events needs "full access" in EventKit's terms, which
would also allow changes: graph-me never makes any. Without permission (refused, or a scheduled
run that can't ask) the source is skipped and nothing is forgotten.

Each occurrence of an event in the window becomes an ``event`` item, with the organizer as its
author and the attendees as its recipients.
"""

from __future__ import annotations

import hashlib
import sys
import threading
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime, timedelta

from graph_me.config import BlacklistConfig, SourceConfig
from graph_me.connectors.base import Item, Party, SourceUnavailable

ENTITY_EVENT = 0  # EKEntityTypeEvent
STATUS_NOT_DETERMINED, STATUS_RESTRICTED, STATUS_DENIED, STATUS_FULL = 0, 1, 2, 3
WINDOW_DAYS = 365  # EventKit searches at most 4 years at once: ask year by year
PERMISSION_TIMEOUT = 120  # seconds to answer the macOS prompt
HELP = (
    "Allow it in System Settings > Privacy & Security > Calendars (for graph-me or your "
    "terminal app), then run graph-me again."
)


def default_store():
    """An ``EKEventStore``. Tests replace ``store_factory`` with a fake."""
    if sys.platform != "darwin":
        raise SourceUnavailable("the macos-calendar source works on macOS only")
    try:
        from EventKit import EKEventStore
    except ImportError:
        raise SourceUnavailable(
            "the macos-calendar source needs the [macos] extra: "
            'uv tool install --managed-python "graph-me[macos]"'
        ) from None
    return EKEventStore.alloc().init()


store_factory = default_store


def _status(store) -> int:
    return int(type(store).authorizationStatusForEntityType_(ENTITY_EVENT))


def _request(store) -> bool:
    """Ask macOS for full access to events (it shows a prompt once). True if granted."""
    done, answer = threading.Event(), {"granted": False}

    def handler(granted, error) -> None:
        answer["granted"] = bool(granted)
        done.set()

    if hasattr(store, "requestFullAccessToEventsWithCompletion_"):  # macOS 14 and later
        store.requestFullAccessToEventsWithCompletion_(handler)
    else:
        store.requestAccessToEntityType_completion_(ENTITY_EVENT, handler)
    done.wait(PERMISSION_TIMEOUT)
    return answer["granted"]


def _datetime(nsdate) -> datetime | None:
    if nsdate is None:
        return None
    return datetime.fromtimestamp(nsdate.timeIntervalSince1970(), UTC)


def _nsdate(when: datetime):
    from Foundation import NSDate  # only reached with the real store

    return NSDate.dateWithTimeIntervalSince1970_(when.timestamp())


def _party(participant) -> Party | None:
    if participant is None:
        return None
    url = participant.URL()
    address = str(url.absoluteString()) if url is not None else ""
    email = address[7:] if address.lower().startswith("mailto:") else None
    name = participant.name()
    if not (email or name):
        return None
    return Party(name=str(name) if name else None, email=email)


class MacosCalendarConnector:
    type = "macos-calendar"

    def __init__(self, name: str, source: SourceConfig, blacklist: BlacklistConfig) -> None:
        extra = source.model_extra or {}
        self.name = name
        self.calendars = {str(c) for c in extra.get("calendars", [])}
        self.past_days = int(extra.get("past_days", 730))
        self.future_days = int(extra.get("future_days", 365))
        self.trust = extra.get("trust", "known")
        if self.trust not in ("self", "known", "untrusted"):
            raise ValueError(f"source {name!r}: trust must be self, known or untrusted")
        self._events: dict[str, tuple[object, str]] = {}
        self._store = None

    def _open(self):
        if self._store is not None:
            return self._store
        store = store_factory()
        status = _status(store)
        if status == STATUS_NOT_DETERMINED and _request(store):
            status = STATUS_FULL
        if status != STATUS_FULL:
            raise SourceUnavailable(f"source {self.name!r}: no access to your calendars. {HELP}")
        self._store = store
        return store

    def _wanted(self, event) -> bool:
        if not self.calendars:
            return True
        calendar = event.calendar()
        return calendar is not None and str(calendar.title()) in self.calendars

    def list_ids(self) -> Iterator[tuple[str, str]]:
        store = self._open()
        self._events.clear()
        now = datetime.now(UTC)
        start, end = now - timedelta(days=self.past_days), now + timedelta(days=self.future_days)
        seen: set[str] = set()
        while start < end:
            stop = min(start + timedelta(days=WINDOW_DAYS), end)
            predicate = store.predicateForEventsWithStartDate_endDate_calendars_(
                _nsdate(start), _nsdate(stop), None
            )
            for event in store.eventsMatchingPredicate_(predicate) or ():
                if not self._wanted(event):
                    continue
                begins = _datetime(event.startDate())
                ident = str(event.calendarItemExternalIdentifier() or event.eventIdentifier())
                external_id = f"{ident}@{begins.isoformat() if begins else ''}"
                if external_id in seen:  # an event across two search windows
                    continue
                seen.add(external_id)
                stamp = _datetime(event.lastModifiedDate())
                version = hashlib.sha256(
                    f"{stamp}|{event.title()}|{begins}|{_datetime(event.endDate())}".encode()
                ).hexdigest()[:32]
                self._events[external_id] = (event, version)
                yield external_id, version
            start = stop

    def excluded(self, external_id: str) -> bool:
        """An occurrence before the window still exists in Calendar: forget it quietly."""
        when = external_id.rsplit("@", 1)[-1]
        try:
            begins = datetime.fromisoformat(when)
        except ValueError:
            return False
        return begins < datetime.now(UTC) - timedelta(days=self.past_days)

    def fetch(self, external_ids: Iterable[str]) -> Iterator[Item]:
        for external_id in external_ids:
            if external_id not in self._events:
                continue
            event, version = self._events[external_id]
            yield self._item(external_id, event, version)

    def _item(self, external_id: str, event, version: str) -> Item:
        begins, ends = _datetime(event.startDate()), _datetime(event.endDate())
        organizer = _party(event.organizer())
        attendees = [p for p in (_party(a) for a in (event.attendees() or ())) if p]
        title = str(event.title() or "(no title)")
        calendar = event.calendar()
        lines = [title]
        if begins:
            span = f"{begins:%Y-%m-%d %H:%M}" + (f" to {ends:%Y-%m-%d %H:%M}" if ends else "")
            lines.append(f"When: {span}")
        if event.location():
            lines.append(f"Where: {event.location()}")
        if calendar is not None:
            lines.append(f"Calendar: {calendar.title()}")
        if organizer:
            lines.append(f"Organizer: {organizer.name or organizer.email}")
        if attendees:
            lines.append("Attendees: " + ", ".join(p.name or p.email for p in attendees))
        if event.notes():
            lines.append(str(event.notes()))
        me = event.organizer() is not None and bool(event.organizer().isCurrentUser())
        return Item(
            external_id=external_id,
            version=version,
            kind="event",
            title=title,
            uri=f"calendar:{external_id}",
            thread_id=str(event.calendarItemExternalIdentifier() or "") or None,
            created_at=begins,
            modified_at=_datetime(event.lastModifiedDate()),
            author=organizer,
            recipients=[a for a in attendees if not organizer or a.email != organizer.email],
            text="\n".join(lines),
            trust=self.trust,
            is_from_me=me,
        )
