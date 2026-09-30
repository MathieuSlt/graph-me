"""M2 gate: Sophie's birthday is 03-12, cited by her contact card and the WhatsApp greetings."""

import pytest
from conftest import full_config, needs_msgvault

from graph_me.pipeline import run
from graph_me.query import graph

pytestmark = needs_msgvault


@pytest.fixture
def graph_db(conn, docs, mv):
    home, contacts = mv
    cfg = full_config(docs, home, contacts)
    run.scan(conn, cfg, workers=1)
    return conn, cfg


def test_gate_sophie_birthday(graph_db):
    conn, cfg = graph_db
    [sophie] = graph.get_fact(conn, "Sophie", "birthday", cfg.people)
    assert sophie["entity"]["name"] == "Sophie Martin"
    [birthday] = sophie["facts"]
    assert (birthday["value"], birthday["confidence"]) == ("03-12", 0.95)
    methods = [e["method"] for e in birthday["evidence"]]
    assert methods[0] == "contact:bday"  # strongest evidence first
    greetings = [e for e in birthday["evidence"] if e["method"].startswith("rule:")]
    # 2022 email + 2023, 2024 (00:30 local time!), 2025 WhatsApp
    assert len(greetings) == 4
    assert {e["kind"] for e in greetings} == {"email", "message"}


def test_no_fact_from_late_or_group_wishes(graph_db):
    conn, _ = graph_db
    values = {r[0] for r in conn.execute("SELECT value FROM facts WHERE key = 'birthday'")}
    assert values == {"03-12"}  # not 06-13 ("en retard"), not 08-20 (group chat)


def test_two_sophies_stay_apart(graph_db):
    conn, cfg = graph_db
    people = graph.who_is(conn, "Sophie", cfg.people)
    names = {p["name"] for p in people}
    assert {"Sophie Martin", "Sophie Bernard"} <= names
    bernard = next(p for p in people if p["name"] == "Sophie Bernard")
    assert not any(f["key"] == "birthday" for f in bernard["facts"])


def test_email_phone_and_card_merge_into_one_person(graph_db):
    conn, cfg = graph_db
    [sophie] = [
        p for p in graph.who_is(conn, "Soeurette", cfg.people) if p["name"] == "Sophie Martin"
    ]
    aliases = {(a["kind"], a["value"]) for a in sophie["aliases"]}
    assert aliases == {("email", "sophie.martin@example.com"), ("phone", "+33612345678")}


def test_me_is_one_entity_with_all_my_identifiers(graph_db):
    conn, cfg = graph_db
    [me] = graph.who_is(conn, "me", cfg.people, limit=1)
    assert me["is_me"] and me["name"] == "Camille Martin"
    values = {a["value"] for a in me["aliases"]}
    assert {"camille@example.com", "+33600000000"} <= values
    to_sophie = next(r for r in me["related"] if r["entity"]["name"] == "Sophie Martin")
    assert to_sophie["direction"] == "out" and to_sophie["weight"] >= 5


def test_file_origin_and_saved_copy(graph_db, docs):
    conn, _ = graph_db
    pdf = conn.execute("SELECT id FROM items WHERE title = 'Contrat_bail_2025.pdf'").fetchone()[0]
    [origin] = graph.origins(conn, pdf)
    assert (origin["title"], origin["from"]) == ("Votre contrat de bail", "Jean Dupont")
    extras = graph.enrich_hits(conn, [origin["id"]])[origin["id"]]
    assert extras["saved_as"][0]["path"] == str(docs / "Logement/Contrat_bail_2025.pdf")
    assert (extras["from"], extras["to"]) == ("Jean Dupont", ["Camille Martin"])


def test_query_includes_facts_about_named_people(graph_db):
    conn, cfg = graph_db
    found = graph.facts_for_query(conn, "anniversaire Soeurette", cfg.people)
    assert found[0]["entity"]["name"] == "Sophie Martin"


def test_greetings_alone_give_lower_confidence(conn, docs, mv):
    home, contacts = mv
    cfg = full_config(docs, home, contacts)
    del cfg.sources["contacts"]  # no address book: only the greetings remain
    run.scan(conn, cfg, workers=1)
    facts = graph.get_fact(conn, "+33612345678", "birthday", cfg.people)
    [birthday] = facts[0]["facts"]
    assert birthday["value"] == "03-12"
    # 2023-2025 on WhatsApp: 0.5 + 0.15 per extra year. Without the card, nothing links
    # Sophie's email to her phone, so the 2022 email greeting counts for a separate person.
    assert birthday["confidence"] == 0.8
    by_email = graph.get_fact(conn, "sophie.martin@example.com", "birthday", cfg.people)
    assert by_email[0]["entity"]["id"] != facts[0]["entity"]["id"]
    assert by_email[0]["facts"][0]["confidence"] == 0.5


def test_blacklisted_contact_is_forgotten_everywhere(graph_db):
    conn, cfg = graph_db
    cfg.blacklist.contacts.append("06 12 34 56 78")
    report = run.scan(conn, cfg, workers=1)
    assert report.forgotten_blacklisted >= 6  # card, email greeting, WhatsApp chat, group message
    assert graph.get_fact(conn, "Soeurette", "birthday", cfg.people) == []
    assert not conn.execute("SELECT count(*) FROM aliases WHERE value = '+33612345678'").fetchone()[
        0
    ]
    # blacklisted items are not re-added by later scans
    run.scan(conn, cfg, workers=1)
    assert graph.get_fact(conn, "Soeurette", "birthday", cfg.people) == []


def test_rescan_is_stable(graph_db):
    conn, cfg = graph_db
    before = conn.execute("SELECT count(*), sum(confidence) FROM facts").fetchone()
    report = run.scan(conn, cfg, workers=1)
    assert all(st.added == st.updated == 0 for st in report.sources.values())
    assert conn.execute("SELECT count(*), sum(confidence) FROM facts").fetchone() == before


def test_cli_who_fact_and_query(tmp_path, docs, mv, monkeypatch):
    import json

    from typer.testing import CliRunner

    from graph_me.cli import app

    monkeypatch.delenv("GRAPH_ME_OUT", raising=False)
    home, contacts = mv
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "people: {phone_country_code: '33'}\n"
        "sources:\n"
        f"  docs: {{type: filesystem, paths: ['{docs}']}}\n"
        f"  messages: {{type: msgvault, db: '{home}'}}\n"
        f"  contacts: {{type: vcard, paths: ['{contacts}']}}\n"
    )
    base = ["--config", str(cfg), "--out", str(tmp_path / "out")]
    runner = CliRunner()
    assert runner.invoke(app, [*base, "init"]).exit_code == 0
    assert runner.invoke(app, [*base, "scan", "--workers", "2"]).exit_code == 0

    result = runner.invoke(app, [*base, "fact", "Soeurette", "birthday"])
    assert result.exit_code == 0 and "birthday: 03-12" in result.output

    result = runner.invoke(app, [*base, "fact", "Sophie Bernard", "birthday"])
    assert result.exit_code == 1

    data = json.loads(runner.invoke(app, [*base, "who", "me", "--json"]).output)
    assert data[0]["is_me"]

    data = json.loads(runner.invoke(app, [*base, "query", "contrat de bail", "--json"]).output)
    email = next(i for i in data["answer_items"] if i["kind"] == "email")
    assert email["from"] == "Jean Dupont" and email["saved_as"]
    pdf = next(i for i in data["answer_items"] if i["kind"] == "file" and i.get("origin"))
    assert pdf["origin"][0]["title"] == "Votre contrat de bail"
