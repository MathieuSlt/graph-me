"""M11: calendars (.ics and macOS Calendar), last contact, threads and topic timelines, OCR,
Evernote exports and CSV chunks."""

import importlib.util
from datetime import UTC, datetime, timedelta

import pytest
from conftest import assert_consistent

from graph_me.config import BlacklistConfig, Config, PeopleConfig, SourceConfig
from graph_me.connectors import macos_calendar
from graph_me.connectors.base import SourceUnavailable
from graph_me.connectors.filesystem import FilesystemConnector
from graph_me.pipeline import chunk, parse, run
from graph_me.query import graph
from graph_me.service import Service

ME = "jordan@example.com"
CALENDAR = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//graph-me tests//EN
BEGIN:VEVENT
UID:kickoff-1
DTSTAMP:20260101T090000Z
DTSTART:20260310T090000Z
DTEND:20260310T100000Z
SUMMARY:Project kickoff
LOCATION:Room 4
ORGANIZER;CN=Jordan Taylor:mailto:jordan@example.com
ATTENDEE;CN=Sam Rivera:mailto:sam.rivera@example.org
ATTENDEE;CN=Priya Shah:mailto:p.shah@lakeside.example
DESCRIPTION:Agenda: budget\\, timeline
END:VEVENT
BEGIN:VEVENT
UID:weekly-sync
DTSTAMP:20260101T090000Z
DTSTART:20260105T150000Z
DTEND:20260105T153000Z
RRULE:FREQ=WEEKLY;BYDAY=MO
SUMMARY:Weekly sync
ORGANIZER;CN=Sam Rivera:mailto:sam.rivera@example.org
ATTENDEE;CN=Jordan Taylor:mailto:jordan@example.com
END:VEVENT
BEGIN:VEVENT
UID:weekly-sync
RECURRENCE-ID:20260112T150000Z
DTSTAMP:20260101T090000Z
DTSTART:20260113T150000Z
SUMMARY:Weekly sync (moved to Tuesday)
ORGANIZER;CN=Sam Rivera:mailto:sam.rivera@example.org
ATTENDEE;CN=Jordan Taylor:mailto:jordan@example.com
END:VEVENT
BEGIN:VEVENT
UID:dentist
DTSTAMP:20260101T090000Z
DTSTART;VALUE=DATE:20991201
SUMMARY:Dentist
END:VEVENT
END:VCALENDAR
"""


@pytest.fixture
def calendar(tmp_path):
    path = tmp_path / "cal" / "work.ics"
    path.parent.mkdir()
    path.write_text(CALENDAR)
    cfg = Config(
        people=PeopleConfig(me=[ME]),
        sources={"calendar": SourceConfig(type="ics", paths=[str(path.parent)])},
    )
    return cfg, path


def test_ics_events_link_people(conn, calendar):
    cfg, path = calendar
    report = run.scan(conn, cfg, workers=1)
    assert report.sources["calendar"].added == 4
    assert_consistent(conn)
    kinds = {r[0] for r in conn.execute("SELECT DISTINCT kind FROM items")}
    assert kinds == {"event"}
    [sam] = graph.who_is(conn, "Sam Rivera")
    assert {(r["type"], r["entity"]["name"]) for r in sam["related"]} >= {
        ("met", "Jordan Taylor"), ("met", "Priya Shah")
    }  # fmt: skip
    # Last contact is the latest past meeting; the dentist in 2099 is the next one (no people).
    assert sam["last_contact"]["title"] == "Project kickoff"
    assert sam["next_meeting"] is None
    [kickoff] = [i for i in graph.timeline(conn, sam["id"]) if i["title"] == "Project kickoff"]
    text = " ".join(r[0] for r in conn.execute(
        "SELECT text FROM chunks WHERE item_id = ?", (kickoff["id"],)))  # fmt: skip
    assert "Where: Room 4" in text and "Agenda: budget, timeline" in text

    # The moved occurrence and the series are one thread.
    weekly = conn.execute("SELECT id FROM items WHERE title = 'Weekly sync'").fetchone()[0]
    thread = graph.thread(conn, weekly)
    assert [t["title"] for t in thread] == ["Weekly sync", "Weekly sync (moved to Tuesday)"]

    # An event removed from the file is forgotten on sync; a broken file keeps everything.
    path.write_text(CALENDAR.replace("BEGIN:VEVENT\nUID:dentist", "BEGIN:VEVENT\nUID:x", 1)
                    .split("BEGIN:VEVENT\nUID:x")[0] + "END:VCALENDAR\n")  # fmt: skip
    assert run.sync(conn, cfg, workers=1).sources["calendar"].forgotten == 1
    assert_consistent(conn)
    for broken in (b"BEGIN:VCALENDAR\nBEGIN:VEVENT\nDTSTART:nope\n", b"garbage"):
        path.write_bytes(broken)
        report = run.sync(conn, cfg, workers=1)
        assert report.sources["calendar"].failure
        assert conn.execute("SELECT count(*) FROM items").fetchone()[0] == 3
    # A malformed date doesn't stop the event from being read.
    path.write_text(CALENDAR.replace("DTSTART:20260310T090000Z", "DTSTART:nope"))
    assert not run.sync(conn, cfg, workers=1).sources["calendar"].failure
    assert_consistent(conn)


def test_next_meeting_and_topic_timeline(conn, calendar, tmp_path):
    cfg, path = calendar
    soon = (datetime.now(UTC) + timedelta(days=3)).strftime("%Y%m%dT%H%M%SZ")
    path.write_text(CALENDAR.replace("20991201", soon[:8]).replace(
        "SUMMARY:Dentist", "SUMMARY:Budget review\nATTENDEE;CN=Sam Rivera:mailto:sam.rivera@example.org"))  # fmt: skip
    run.scan(conn, cfg, workers=1)
    conn.commit()
    [sam] = graph.who_is(conn, "Sam Rivera")
    assert sam["next_meeting"]["title"] == "Budget review"
    service = Service(cfg, tmp_path / "graph.db")
    found = service.timeline(query="budget")["results"][0]["items"]
    assert [i["title"] for i in found] == ["Budget review", "Project kickoff"]  # newest first
    weekly = conn.execute("SELECT id FROM items WHERE title = 'Weekly sync'").fetchone()[0]
    assert len(service.thread(weekly)["items"]) == 2
    assert "error" in service.thread("nope")


# -- macOS Calendar, with a fake EventKit -----------------------------------------------------


class FakeDate:
    def __init__(self, when):
        self.when = when

    def timeIntervalSince1970(self):  # noqa: N802 (EventKit names)
        return self.when.timestamp()


class FakeURL:
    def __init__(self, text):
        self.text = text

    def absoluteString(self):  # noqa: N802
        return self.text


class FakeParticipant:
    def __init__(self, name, email, me=False):
        self._name, self._email, self._me = name, email, me

    def name(self):
        return self._name

    def URL(self):  # noqa: N802
        return FakeURL(f"mailto:{self._email}")

    def isCurrentUser(self):  # noqa: N802
        return self._me


class FakeCalendar:
    def __init__(self, title):
        self._title = title

    def title(self):
        return self._title


class FakeEvent:
    def __init__(self, ident, title, start, calendar="Work", attendees=(), organizer=None):
        self.ident, self._title, self.start = ident, title, start
        self._calendar, self._attendees, self._organizer = calendar, attendees, organizer

    def calendarItemExternalIdentifier(self):  # noqa: N802
        return self.ident

    def eventIdentifier(self):  # noqa: N802
        return self.ident

    def title(self):
        return self._title

    def startDate(self):  # noqa: N802
        return FakeDate(self.start)

    def endDate(self):  # noqa: N802
        return FakeDate(self.start + timedelta(hours=1))

    def lastModifiedDate(self):  # noqa: N802
        return FakeDate(self.start - timedelta(days=1))

    def calendar(self):
        return FakeCalendar(self._calendar)

    def organizer(self):
        return self._organizer

    def attendees(self):
        return list(self._attendees)

    def location(self):
        return "Room 4"

    def notes(self):
        return None


def fake_store(events, status=3, grant=True):
    class Store:
        @classmethod
        def authorizationStatusForEntityType_(cls, kind):  # noqa: N802
            return status

        def requestFullAccessToEventsWithCompletion_(self, handler):  # noqa: N802
            handler(grant, None)

        def predicateForEventsWithStartDate_endDate_calendars_(self, start, end, cals):  # noqa: N802
            return start.when, end.when

        def eventsMatchingPredicate_(self, predicate):  # noqa: N802
            start, end = predicate
            return [e for e in events if start <= e.start < end]

    return Store()


@pytest.fixture
def macos(monkeypatch):
    now = datetime.now(UTC)
    me = FakeParticipant("Jordan Taylor", ME, me=True)
    sam = FakeParticipant("Sam Rivera", "sam.rivera@example.org")
    events = [
        FakeEvent("e1", "Planning", now - timedelta(days=10), attendees=[sam], organizer=me),
        FakeEvent("e2", "Dinner", now + timedelta(days=5), calendar="Family", attendees=[sam]),
        FakeEvent("e3", "Old offsite", now - timedelta(days=900)),  # outside past_days
    ]
    monkeypatch.setattr(macos_calendar, "_nsdate", FakeDate)

    def use(**kwargs):
        monkeypatch.setattr(macos_calendar, "store_factory", lambda: fake_store(events, **kwargs))

    use()
    return use


def mac_source(**extra):
    return macos_calendar.MacosCalendarConnector(
        "cal", SourceConfig(type="macos-calendar", **extra), BlacklistConfig()
    )


def test_macos_calendar_reads_events(macos, conn):
    ids = dict(mac_source().list_ids())
    assert len(ids) == 2  # the 900-day-old offsite is outside the window
    only_work = mac_source(calendars=["Work"])
    assert [i for i, _ in only_work.list_ids()] == [next(i for i in ids if i.startswith("e1"))]
    [item] = only_work.fetch([next(iter(dict(only_work.list_ids())))])
    assert (item.kind, item.title, item.is_from_me) == ("event", "Planning", True)
    assert [p.email for p in item.recipients] == ["sam.rivera@example.org"]
    assert "Calendar: Work" in item.text

    cfg = Config(sources={"cal": SourceConfig(type="macos-calendar")}, people=PeopleConfig(me=[ME]))
    run.scan(conn, cfg, workers=1)
    assert_consistent(conn)
    [sam] = graph.who_is(conn, "Sam Rivera")
    assert sam["last_contact"]["title"] == "Planning" and sam["next_meeting"]["title"] == "Dinner"
    old = (datetime.now(UTC) - timedelta(days=800)).isoformat()
    assert mac_source().excluded(f"x@{old}") and not mac_source().excluded("x@nonsense")


def test_macos_calendar_permission(macos):
    macos(status=2)  # denied
    with pytest.raises(SourceUnavailable, match="Privacy & Security"):
        list(mac_source().list_ids())
    macos(status=0, grant=True)  # asked now, and allowed
    assert len(dict(mac_source().list_ids())) == 2
    macos(status=0, grant=False)
    with pytest.raises(SourceUnavailable):
        list(mac_source().list_ids())


def test_macos_calendar_elsewhere(monkeypatch):
    monkeypatch.setattr(macos_calendar.sys, "platform", "linux")
    with pytest.raises(SourceUnavailable, match="macOS only"):
        macos_calendar.default_store()


# -- OCR, Evernote, CSV -----------------------------------------------------------------------


def blank_pdf(path):
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument.new()
    pdf.new_page(300, 200)
    pdf.save(path)
    pdf.close()


def test_ocr_reads_pages_without_text(tmp_path, monkeypatch):
    path = tmp_path / "scan.pdf"
    blank_pdf(path)
    assert parse.parse_file(path) == ""  # no text layer, no OCR

    class Engine:
        calls = 0

        def __call__(self, image):
            Engine.calls += 1
            return type("Out", (), {"txts": ("INVOICE 42", "Total due: 120 EUR")})()

    monkeypatch.setattr(parse, "_engine", Engine())
    assert parse.parse_file(path, ocr=True) == "INVOICE 42\nTotal due: 120 EUR"
    assert Engine.calls == 1


@pytest.mark.skipif(importlib.util.find_spec("rapidocr") is None, reason="needs graph-me[ocr]")
def test_ocr_for_real(tmp_path, monkeypatch):
    import pypdfium2 as pdfium
    from PIL import Image, ImageDraw, ImageFont

    image = Image.new("RGB", (1200, 400), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=56)
    draw.text((60, 80), "RENTAL AGREEMENT", fill="black", font=font)
    draw.text((60, 200), "Rent 1450 dollars", fill="black", font=font)
    pdf = pdfium.PdfDocument.new()
    page = pdf.new_page(600, 200)
    obj = pdfium.PdfImage.new(pdf)
    obj.set_bitmap(pdfium.PdfBitmap.from_pil(image))
    obj.set_matrix(pdfium.PdfMatrix().scale(600, 200))
    page.insert_obj(obj)
    page.gen_content()
    path = tmp_path / "scan.pdf"
    pdf.save(path)
    pdf.close()
    monkeypatch.setattr(parse, "_engine", None)
    text = parse.parse_file(path, ocr=True).upper()
    assert "RENTAL" in text and "1450" in text


def test_filesystem_ocr_and_images(tmp_path):
    (tmp_path / "a.pdf").write_bytes(b"%PDF-1.4")
    (tmp_path / "photo.jpg").write_bytes(b"\xff\xd8")

    def ids(**extra):
        conn = FilesystemConnector(
            "d", SourceConfig(type="filesystem", paths=[str(tmp_path)], **extra), BlacklistConfig()
        )
        return {k.rsplit("/", 1)[1]: v for k, v in conn.list_ids()}

    assert set(ids()) == {"a.pdf"} and not ids()["a.pdf"].endswith(":ocr")
    assert ids(ocr=True)["a.pdf"].endswith(":ocr")  # turning OCR on reads PDFs again
    assert set(ids(ocr=True, images=True)) == {"a.pdf", "photo.jpg"}
    assert set(ids(images=True)) == {"a.pdf"}  # images need OCR


def test_evernote_export(tmp_path):
    path = tmp_path / "notes.enex"
    path.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n<en-export>'
        '<note><title>Boiler</title><content><![CDATA[<?xml version="1.0"?>'
        '<!DOCTYPE en-note SYSTEM "http://xml.evernote.com/pub/enml2.dtd">'
        "<en-note><div>Serviced in <b>May</b>.</div></en-note>]]></content></note>"
        "<note><title>Car</title><content><![CDATA[<en-note>Plates AB-123</en-note>]]>"
        "</content></note></en-export>"
    )
    text = parse.parse_file(path)
    assert "Boiler" in text and "Serviced in May." in " ".join(text.split())
    assert "Plates AB-123" in text


def test_csv_chunks_keep_the_header():
    rows = "\n".join(f"2026-01-{d:02d},Bright Plumbing,{d * 10}" for d in range(1, 29))
    chunks = chunk.chunk_table("Date,Payee,Amount\n" + rows, max_tokens=10)
    assert len(chunks) > 1
    assert all(text.startswith("Date,Payee,Amount\n") for text, _ in chunks)
    assert "2026-01-28,Bright Plumbing,280" in chunks[-1][0]
