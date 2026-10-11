"""M9: deadlines and amounts (Tier 0 rules), vCard dates, `upcoming`, REPORT.md and .ics export."""

from datetime import UTC, date, datetime

import pytest
from conftest import assert_consistent
from hypothesis import given, settings
from hypothesis import strategies as st
from typer.testing import CliRunner

from graph_me import config, report
from graph_me import demo as demo_mod
from graph_me.cli import app
from graph_me.connectors.base import Item
from graph_me.connectors.vcard import card_to_contact, parse_vcards
from graph_me.export import ics
from graph_me.pipeline import run
from graph_me.pipeline.tier0 import entities, extract
from graph_me.query import graph
from graph_me.service import Service
from graph_me.store import db

TODAY = date(2026, 12, 20)  # the renewal and the birthday fall in January: across the year end


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("The lease ends on June 30, 2026.", [("ends", "2026-06-30")]),
        ("Policy renews on 15 January 2027", [("renews", "2027-01-15")]),
        ("Passport valid until 03/04/2031", [("expires", "2031-03-04")]),  # MDY in English
        ("Carte valable jusqu'au 03/04/2031", [("expires", "2031-04-03")]),  # DMY in French
        ("Date d'échéance : 1er février 2027", [("due", "2027-02-01")]),
        ("Fin du bail : 30 juin 2028.", [("ends", "2028-06-30")]),
        ("Payment due 2027-01-31, late fees apply", [("due", "2027-01-31")]),
        ("Devis chaudière : 1 250,50 € TTC", [("quote_amount", "1250.5 EUR")]),
        ("Invoice 4471. Total due: $1,450.00", [("invoice_amount", "1450 USD")]),
        ("Your quote for the roof repair is £980", [("quote_amount", "980 GBP")]),
        ("Facture n° 12 : montant 89 euros", [("invoice_amount", "89 EUR")]),
        # No date or amount next to the keyword, or not a full date: nothing.
        ("The deadline is next week, I promise", []),
        ("Expires soon. Renews in March.", []),
        ("Invoice #8812, please ignore", []),
        ("We met on June 30, 2026 to talk.", []),  # a date, but no keyword
    ],
)
def test_extract(text, expected):
    assert [(k, v) for k, v, _ in extract.extract(text)] == expected


def test_extract_skips_the_items_own_date():
    text = "Sent today. Payment due by 03/04/2026."
    assert extract.extract(text, date(2026, 3, 4)) == []
    [(key, value, method)] = extract.extract(text, date(2026, 1, 1))
    assert (key, value, method) == ("due", "2026-03-04", "rule:en.deadline")


@settings(max_examples=200, deadline=None)
@given(st.text(alphabet=st.characters(codec="utf-8"), max_size=300))
def test_extract_never_crashes(text):
    for key, value, method in extract.extract("due date " + text + " invoice " + text):
        assert method.startswith("rule:")
        if key.endswith("_amount"):
            number, currency = value.split(" ")
            assert float(number) > 0 and currency.isupper()
        else:
            assert date.fromisoformat(value)


def test_vcard_dates_and_addresses():
    [card] = parse_vcards(
        "BEGIN:VCARD\nVERSION:4.0\nFN:Ana\nTITLE:Engineer\n"
        "ADR;TYPE=home:;;18 Maple Street;Springfield;;12345;USA\nANNIVERSARY:2015-06-20\n"
        "END:VCARD\n"
    )
    contact = card_to_contact(card)
    assert contact.title == "Engineer"
    assert contact.addresses == ["18 Maple Street, Springfield, 12345, USA"]
    assert contact.anniversary == "2015-06-20"


# -- the pipeline, on the demo --------------------------------------------------------------------


@pytest.fixture
def demo(tmp_path):
    cfg_path = demo_mod.build(tmp_path / "demo", today=TODAY)
    cfg = config.load_config(cfg_path)
    out = config.ensure_output(config.resolve_output(None, cfg))
    conn = db.connect(out / config.DB_NAME)
    run.scan(conn, cfg, workers=1)
    yield conn, cfg, cfg_path, out
    conn.close()


def test_deadlines_are_cited_facts_and_listed(demo):
    conn, cfg, _, out = demo
    assert_consistent(conn)
    soon = graph.upcoming(conn, TODAY, days=60)
    found = [(u["date"], u["key"], u["entity"]["name"]) for u in soon]
    assert found == [
        ("2027-01-09", "birthday", "Alex Taylor"),
        ("2027-01-29", "renews", "vehicle_policy_renewal.docx"),
    ]
    renewal = soon[1]
    assert renewal["confidence"] == 0.6 and renewal["in_days"] == 40
    [ev] = renewal["evidence"]
    assert ev["method"] == "rule:en.deadline" and ev["uri"].endswith(".docx")
    # The lease ended in June 2026: in the past, so not upcoming, but still a cited fact.
    assert all(u["key"] != "ends" for u in graph.upcoming(conn, TODAY, days=366))
    assert conn.execute("SELECT count(*) FROM facts WHERE key = 'ends'").fetchone()[0] == 1

    conn.commit()  # assert_consistent's integrity checks hold a write lock
    service = Service(cfg, out / config.DB_NAME)
    assert [u["key"] for u in service.upcoming(days=60, kind="deadlines", today=TODAY)
            ["results"]] == ["renews"]  # fmt: skip
    assert [u["key"] for u in service.upcoming(days=60, kind="dates", today=TODAY)
            ["results"]] == ["birthday"]  # fmt: skip
    [hit] = [i for i in service.search("plumber quote")["answer_items"]
             if i["title"] == "plumber_quote.eml"]  # fmt: skip
    assert hit["values"] == [{"key": "quote_amount", "value": "480 USD"}]


def test_deleting_the_document_forgets_its_deadline(demo):
    conn, cfg, cfg_path, _ = demo
    (cfg_path.parent / "Documents/Car/vehicle_policy_renewal.docx").unlink()
    run.sync(conn, cfg, workers=1)
    assert_consistent(conn)
    assert conn.execute("SELECT count(*) FROM facts WHERE key = 'renews'").fetchone()[0] == 0


def test_new_rules_reach_stored_items(demo):
    conn, cfg, _, _ = demo
    conn.execute("DELETE FROM evidence WHERE method LIKE 'rule:%.deadline'")
    run.finish(conn)
    db.set_meta(conn, "tier0_rules", "older-rules")
    conn.commit()
    assert conn.execute("SELECT count(*) FROM facts WHERE key = 'renews'").fetchone()[0] == 0
    report_ = run.scan(conn, cfg, workers=1)
    assert report_.backfilled > 0
    assert report_.sources["contacts"].updated == 4  # cards are read again for their new fields
    assert_consistent(conn)
    assert conn.execute("SELECT count(*) FROM facts WHERE key = 'renews'").fetchone()[0] == 1
    assert run.scan(conn, cfg, workers=1).backfilled == 0  # once only


def test_a_deadline_in_a_message_makes_it_a_document(conn):
    conn.execute("INSERT INTO sources(id, type) VALUES ('m', 'msgvault')")
    conn.execute(
        "INSERT INTO items(id, source_id, external_id, kind, title) "
        "VALUES ('i1', 'm', 'x', 'email', 'Your renewal')"
    )
    item = Item(external_id="x", version="1", kind="email", title="Your renewal")
    entities.value_facts(conn, "i1", item, None, [("renews", "2027-02-01", "rule:en.deadline")])
    run.finish(conn)
    assert_consistent(conn)
    [u] = graph.upcoming(conn, date(2027, 1, 1), days=60)
    assert (u["entity"]["kind"], u["entity"]["name"]) == ("document", "Your renewal")


def test_report_lists_what_is_coming_up(demo):
    conn, _, _, out = demo
    path, _ = report.write(conn, out, today=TODAY)
    text = path.read_text()
    assert "## Coming up in the next 45 days" in text
    assert "- 09 January: Alex Taylor (confidence 0.95)" in text
    assert "- 29 January: vehicle_policy_renewal.docx renews (confidence 0.60)" in text
    assert "What expires or is due" in text


def test_ics_export(demo):
    conn, _, _, _ = demo
    text = ics.build(conn, today=TODAY, now=datetime(2026, 12, 20, 8, tzinfo=UTC))
    assert text.startswith("BEGIN:VCALENDAR\r\n") and text.endswith("END:VCALENDAR\r\n")
    assert "\n" not in text.replace("\r\n", "")
    assert all(len(line.encode()) <= 75 for line in text.split("\r\n"))
    events = text.split("BEGIN:VEVENT")[1:]
    birthday = next(e for e in events if "Alex Taylor: birthday" in e)
    assert "DTSTART;VALUE=DATE:20270109" in birthday and "RRULE:FREQ=YEARLY" in birthday
    renewal = next(e for e in events if "renews" in e)
    assert "DTSTART;VALUE=DATE:20270129" in renewal and "TRIGGER:-P7D" in renewal
    assert "DTSTAMP:20261220T080000Z" in renewal
    assert "ends" not in text  # the lease end is in the past
    assert ics._escape("a, b; c\\d\ne") == "a\\, b\\; c\\\\d\\ne"


def test_cli_upcoming_and_export(demo, tmp_path):
    _, _, cfg_path, out = demo
    runner = CliRunner()
    result = runner.invoke(app, ["--config", str(cfg_path), "upcoming", "--days", "366"])
    assert result.exit_code == 0, result.output
    assert "vehicle_policy_renewal.docx: renews" in result.output
    result = runner.invoke(app, ["--config", str(cfg_path), "export", "ics"])
    assert result.exit_code == 0 and (out / "upcoming.ics").exists()

    # export.ics in config.yaml rewrites it after every scan.
    (out / "upcoming.ics").unlink()
    cfg_path.write_text(cfg_path.read_text() + "export: {ics: true}\n")
    assert runner.invoke(app, ["--config", str(cfg_path), "scan"]).exit_code == 0
    assert "BEGIN:VEVENT" in (out / "upcoming.ics").read_text()
