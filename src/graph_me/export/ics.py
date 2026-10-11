"""``graph-me export ics``: birthdays, anniversaries and deadlines as a calendar file (RFC 5545).

Subscribe to it, or import it, in any calendar app. Birthdays and anniversaries repeat every
year; a deadline is one all-day event with a reminder a week before. Events carry a short summary
only ("Alex Taylor: birthday", "lease_2025.pdf: ends"), redacted like every other output: no
message text. graph-me writes this file in graph-out/; it never touches your calendars.
"""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, date, datetime

from graph_me.query import graph
from graph_me.query.pack import redact

FILE_NAME = "upcoming.ics"
REMINDER_DAYS = 7
_LABELS = {"birthday": "birthday", "anniversary": "anniversary", "expires": "expires",
           "renews": "renews", "due": "due", "ends": "ends"}  # fmt: skip


def _escape(text: str) -> str:
    return (text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
            .replace("\r\n", "\\n").replace("\n", "\\n"))  # fmt: skip


def _fold(line: str) -> list[str]:
    """Lines longer than 75 octets continue on the next line after a space."""
    out, current = [], b""
    for ch in line:
        encoded = ch.encode("utf-8")
        if len(current) + len(encoded) > (75 if not out else 74):
            out.append(current.decode("utf-8"))
            current = b""
        current += encoded
    out.append(current.decode("utf-8"))
    return [out[0], *(" " + part for part in out[1:])]


def build(conn: sqlite3.Connection, today: date | None = None, now: datetime | None = None) -> str:
    """The calendar text for every dated fact (past deadlines are left out)."""
    today = today or date.today()
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//graph-me//upcoming//EN",
             "CALSCALE:GREGORIAN", "METHOD:PUBLISH", "X-WR-CALNAME:graph-me"]  # fmt: skip
    events = []
    for fact in graph.dated_facts(conn):
        when = graph.next_date(fact["key"], fact["value"], today)
        if when is None or not fact["name"]:
            continue
        events.append((when, fact))
    seen = set()
    for when, fact in sorted(events, key=lambda e: (e[0], e[1]["key"], e[1]["name"])):
        annual = fact["key"] in graph.ANNUAL_KEYS
        uid_key = f"{fact['eid']}|{fact['key']}|{fact['value']}"
        if uid_key in seen:
            continue
        seen.add(uid_key)
        name = redact(fact["name"])[0]
        summary = f"{name}: {_LABELS.get(fact['key'], fact['key'])}"
        start = when.strftime("%Y%m%d")
        lines += [
            "BEGIN:VEVENT",
            f"UID:{hashlib.sha256(uid_key.encode()).hexdigest()[:24]}@graph-me",
            f"DTSTAMP:{stamp}",
            f"DTSTART;VALUE=DATE:{start}",
            f"SUMMARY:{_escape(summary)}",
            "TRANSP:TRANSPARENT",
        ]
        if annual:
            lines.append("RRULE:FREQ=YEARLY")
        else:
            lines += ["BEGIN:VALARM", "ACTION:DISPLAY", f"DESCRIPTION:{_escape(summary)}",
                      f"TRIGGER:-P{REMINDER_DAYS}D", "END:VALARM"]  # fmt: skip
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "".join(part + "\r\n" for line in lines for part in _fold(line))
