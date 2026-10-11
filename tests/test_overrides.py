"""Your corrections (overrides.yaml): link, unlink, relation, set and reject facts.

Built on the synthetic address book only, so these run without msgvault.
"""

import pytest
from conftest import assert_consistent
from factory import make_vcf

from graph_me import overrides as corrections
from graph_me.config import Config, PeopleConfig, SourceConfig
from graph_me.pipeline import run
from graph_me.query import graph

EXTRA_CARD = """BEGIN:VCARD
VERSION:3.0
UID:s-bernard-mobile
FN:S. Bernard
TEL:+33 7 11 11 11 11
END:VCARD
"""


@pytest.fixture
def setup(tmp_path):
    vcf = make_vcf(tmp_path)
    (vcf.parent / "extra.vcf").write_text(EXTRA_CARD)
    cfg = Config(
        overrides=tmp_path / "overrides.yaml",
        people=PeopleConfig(phone_country_code="33", me=["camille@example.com"]),
        sources={"contacts": SourceConfig(type="vcard", paths=[str(vcf.parent)])},
    )
    return cfg, tmp_path


def edit(cfg, change) -> None:
    fixes = corrections.load(cfg.overrides)
    change(fixes)
    corrections.save(cfg.overrides, fixes)


def people_named(conn, name):
    return [p for p in graph.who_is(conn, name, limit=10) if p["name"] == name]


def facts(person, key):
    return [f for f in person["facts"] if f["key"] == key]


def test_relation_makes_my_sister_work_at_tier_0(conn, setup):
    cfg, _ = setup
    run.scan(conn, cfg, workers=1)
    assert graph.get_fact(conn, "my sister", "birthday", cfg.people) == []

    edit(cfg, lambda f: corrections.relate(f, "sophie.martin@example.com", "sibling"))
    run.scan(conn, cfg, workers=1)
    assert_consistent(conn)
    [sophie] = graph.get_fact(conn, "my sister", "birthday", cfg.people)
    assert sophie["entity"]["name"] == "Sophie Martin"
    [relation] = facts(people_named(conn, "Sophie Martin")[0], "relation_to_user")
    assert (relation["value"], relation["confidence"]) == ("sibling", 1.0)
    [ev] = relation["evidence"]
    assert (ev["method"], ev["uri"], ev["source"]) == (
        "user:relation", str(cfg.overrides), "_overrides"
    )  # fmt: skip
    [me] = graph.who_is(conn, "me", cfg.people, limit=1)
    assert any(r["type"] == "sibling" and r["entity"]["name"] == "Sophie Martin"
               for r in me["related"])  # fmt: skip

    # Removing the correction forgets what it said.
    edit(cfg, lambda f: corrections.relate(f, "sophie.martin@example.com", None))
    run.sync(conn, cfg, workers=1)
    assert_consistent(conn)
    assert graph.get_fact(conn, "my sister", "birthday", cfg.people) == []


def test_set_fact_is_cited_and_sure(conn, setup):
    cfg, _ = setup
    edit(cfg, lambda f: corrections.set_fact(f, "sophie.bernard@atlas.example", "birthday",
                                             "1990-07-04"))  # fmt: skip
    run.scan(conn, cfg, workers=1)
    assert_consistent(conn)
    [bernard] = people_named(conn, "Sophie Bernard")
    [birthday] = facts(bernard, "birthday")
    assert (birthday["value"], birthday["confidence"]) == ("07-04", 1.0)
    assert birthday["evidence"][0]["method"] == "user:fact"


def test_reject_survives_rereading_and_can_be_undone(conn, setup):
    cfg, tmp = setup
    edit(cfg, lambda f: corrections.reject(f, "+33 6 12 34 56 78", "birthday", "03-12"))
    run.scan(conn, cfg, workers=1)
    assert_consistent(conn)
    [sophie] = people_named(conn, "Sophie Martin")
    assert facts(sophie, "birthday") == []
    assert facts(sophie, "birth_year")  # only the rejected value goes

    # The card changes and is read again: the rejected birthday stays out.
    vcf = tmp / "contacts" / "contacts.vcf"
    vcf.write_text(vcf.read_text().replace("NICKNAME:Soeurette", "NICKNAME:Soso"))
    run.sync(conn, cfg, workers=1)
    assert facts(people_named(conn, "Sophie Martin")[0], "birthday") == []

    edit(cfg, lambda f: f.reject.clear())
    report = run.sync(conn, cfg, workers=1)
    assert report.relinked > 0
    assert_consistent(conn)
    [birthday] = facts(people_named(conn, "Sophie Martin")[0], "birthday")
    assert birthday["value"] == "03-12"


def test_link_merges_and_removing_it_splits_back(conn, setup):
    cfg, _ = setup
    run.scan(conn, cfg, workers=1)
    assert len(people_named(conn, "S. Bernard")) == 1

    edit(cfg, lambda f: corrections.link(f, ["sophie.bernard@atlas.example", "07 11 11 11 11"]))
    run.sync(conn, cfg, workers=1)
    assert_consistent(conn)
    [bernard] = [p for p in graph.who_is(conn, "Bernard", limit=10)]
    aliases = {a["value"] for a in bernard["aliases"]}
    assert {"sophie.bernard@atlas.example", "+33711111111"} <= aliases

    edit(cfg, lambda f: f.people.clear())
    report = run.sync(conn, cfg, workers=1)
    assert report.relinked > 0
    assert_consistent(conn)
    assert len(people_named(conn, "S. Bernard")) == 1
    assert len(people_named(conn, "Sophie Bernard")) == 1


def test_unlink_splits_a_card_and_relink_merges_it_back(conn, setup):
    cfg, tmp = setup
    # A second card with her mobile only: once unlinked, the number has a person of its own.
    (tmp / "contacts" / "mobile.vcf").write_text(
        "BEGIN:VCARD\nVERSION:3.0\nUID:sophie-mobile\nFN:Sophie Martin\n"
        "TEL:06 12 34 56 78\nEND:VCARD\n"
    )
    run.scan(conn, cfg, workers=1)
    [sophie] = people_named(conn, "Sophie Martin")
    assert len(sophie["aliases"]) == 2  # her card lists her email and her phone

    edit(cfg, lambda f: corrections.unlink(f, ["sophie.martin@example.com", "06 12 34 56 78"]))
    report = run.scan(conn, cfg, workers=1)
    assert report.relinked == 2  # both cards are read again
    assert_consistent(conn)
    ids = {
        frozenset(a["value"] for a in p["aliases"]) for p in graph.who_is(conn, "Sophie", limit=10)
    }
    assert frozenset({"sophie.martin@example.com"}) in ids
    assert frozenset({"+33612345678"}) in ids

    edit(cfg, lambda f: f.not_same.clear())
    run.scan(conn, cfg, workers=1)
    assert_consistent(conn)
    [sophie] = people_named(conn, "Sophie Martin")
    assert len(sophie["aliases"]) == 2


def test_deleting_the_file_forgets_every_correction(conn, setup):
    cfg, _ = setup
    edit(cfg, lambda f: corrections.relate(f, "sophie.martin@example.com", "sibling"))
    run.scan(conn, cfg, workers=1)
    cfg.overrides.unlink()
    run.sync(conn, cfg, workers=1)
    assert_consistent(conn)
    assert (
        conn.execute("SELECT count(*) FROM items WHERE source_id = '_overrides'").fetchone()[0] == 0
    )
    assert (
        conn.execute("SELECT count(*) FROM facts WHERE key = 'relation_to_user'").fetchone()[0] == 0
    )


def test_a_broken_file_keeps_corrections(conn, setup):
    cfg, _ = setup
    edit(cfg, lambda f: corrections.relate(f, "sophie.martin@example.com", "sibling"))
    run.scan(conn, cfg, workers=1)
    cfg.overrides.write_text("people: [ {who: ")
    report = run.sync(conn, cfg, workers=1)
    assert "has errors" in report.sources["_overrides"].failure
    assert graph.get_fact(conn, "my sister", "birthday", cfg.people)


def test_file_edits_are_validated(tmp_path):
    path = tmp_path / "overrides.yaml"
    fixes = corrections.Overrides()
    with pytest.raises(ValueError, match="unknown relation"):
        corrections.relate(fixes, "a@example.com", "nemesis")
    with pytest.raises(ValueError, match="MM-DD"):
        corrections.set_fact(fixes, "a@example.com", "birthday", "soon")
    corrections.link(fixes, ["a@example.com", "b@example.com"])
    corrections.unlink(fixes, ["a@example.com", "b@example.com"])  # undoes the link
    assert fixes.people[0].same_as == []
    corrections.link(fixes, ["a@example.com", "b@example.com"])  # and back
    assert fixes.not_same == []
    corrections.save(path, fixes)
    assert path.stat().st_mode & 0o777 == 0o600
    assert corrections.load(path) == fixes
    path.write_text(
        "people:\n  - {who: a@x.example}\n  - {who: b@x.example, same_as: [a@x.example]}\n"
    )
    with pytest.raises(ValueError, match="share an identifier"):
        corrections.load(path)


# -- CLI ----------------------------------------------------------------------------------------


@pytest.fixture
def cli(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from graph_me import config
    from graph_me.cli import app

    contacts = make_vcf(tmp_path).parent
    base = tmp_path / "base"
    base.mkdir()
    (base / "config.yaml").write_text(
        "people: {phone_country_code: '33', me: [camille@example.com]}\n"
        f"sources:\n  contacts: {{type: vcard, paths: ['{contacts}']}}\n"
    )
    monkeypatch.setenv(config.ENV_CONFIG, str(base / "config.yaml"))
    monkeypatch.setenv(config.ENV_OUT, str(base / "graph-out"))
    runner = CliRunner()

    def invoke(*args):
        return runner.invoke(app, list(args))

    assert invoke("init").exit_code == 0
    assert invoke("scan").exit_code == 0
    return invoke, base


def test_cli_corrections_by_name(cli):
    invoke, base = cli
    result = invoke("relate", "Sophie", "sibling")
    assert result.exit_code == 1 and "several people" in result.output  # Martin and Bernard
    assert "sophie.martin@example.com" in result.output

    result = invoke("relate", "Sophie Martin", "sibling")
    assert result.exit_code == 0, result.output
    assert "sophie.martin@example.com: your sibling" in result.output
    result = invoke("fact", "Sophie Bernard", "birthday", "--set", "07-04")
    assert result.exit_code == 0, result.output
    assert invoke("relate", "Sophie Martin", "nemesis").exit_code == 1

    result = invoke("sync")
    assert result.exit_code == 0, result.output
    assert "corrections: 1 item" in result.output or "corrections: 2 items" in result.output
    result = invoke("fact", "my sister", "birthday")
    assert result.exit_code == 0 and "03-12" in result.output

    assert invoke("fact", "Sophie Martin", "birthday", "--reject", "03-12").exit_code == 0
    invoke("sync")
    assert invoke("fact", "Sophie Martin", "birthday").exit_code == 1

    result = invoke("overrides")
    assert "relation: sibling" in result.output and "reject:" in result.output
    assert (base / "overrides.yaml.bak").exists()


def test_cli_link_needs_two_identifiers(cli):
    invoke, _ = cli
    assert invoke("link", "sophie.bernard@atlas.example").exit_code == 1
    result = invoke("link", "sophie.bernard@atlas.example", "+33 7 11 11 11 11")
    assert result.exit_code == 0, result.output
    assert invoke("unlink", "Sophie Martin", "06 12 34 56 78").exit_code == 0


def test_stale_index_is_said(cli):
    from graph_me import config
    from graph_me.service import Service
    from graph_me.store import db

    invoke, base = cli
    path = base / "graph-out" / "graph.db"
    store = db.connect(path)
    store.execute("UPDATE sources SET last_sync_at = '2020-01-01T00:00:00+00:00'")
    store.commit()
    store.close()

    result = invoke("query", "Sophie")
    assert "last synced" in result.output
    result = invoke("query", "Sophie", "--format", "markdown")
    assert "last synced" in result.output
    service = Service(config.load_config(base / "config.yaml"), path)
    for answer in (service.search("Sophie"), service.who_is("Sophie"), service.status(),
                   service.get_fact("Sophie", "birthday"), service.timeline("Sophie")):  # fmt: skip
        assert answer["index"]["stale"] is True
        assert answer["index"]["age_days"] > 365
