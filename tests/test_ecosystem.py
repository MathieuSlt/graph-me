"""M12: the connector contract (built-ins and the example plugin), rule packs and their
examples, user packs, and document bundles."""

import importlib.util
import json
import os
import stat
import sys
from pathlib import Path

import pytest
from conftest import assert_consistent
from factory import TEST_IBAN, make_vcf
from typer.testing import CliRunner

from graph_me import config, rules
from graph_me import demo as demo_mod
from graph_me import overrides as corrections
from graph_me.cli import app
from graph_me.config import BlacklistConfig, Config, SourceConfig
from graph_me.connectors import registry
from graph_me.connectors.base import Item
from graph_me.export import bundle
from graph_me.pipeline import run
from graph_me.pipeline.tier0 import facts
from graph_me.service import Service
from graph_me.testing import check_connector

EXAMPLE = Path(__file__).parents[1] / "examples" / "graph-me-connector-example"
runner = CliRunner()


def connector(type_, **extra):
    return registry.create("s", SourceConfig(type=type_, **extra), BlacklistConfig())


def test_builtin_connectors_keep_the_contract(tmp_path, docs):
    contacts = make_vcf(tmp_path).parent
    (tmp_path / "cal.ics").write_text(
        "BEGIN:VCALENDAR\nVERSION:2.0\nBEGIN:VEVENT\nUID:a\nDTSTART:20260310T090000Z\n"
        "SUMMARY:Kickoff\nEND:VEVENT\nEND:VCALENDAR\n"
    )
    fixes = corrections.Overrides()
    corrections.relate(fixes, "a@example.com", "sibling")
    corrections.save(tmp_path / "overrides.yaml", fixes)
    gone = tmp_path / "missing"
    for type_, here, there in (
        ("filesystem", {"paths": [str(docs)]}, {"paths": [str(gone)]}),
        ("vcard", {"paths": [str(contacts)]}, {"paths": [str(gone)]}),
        ("ics", {"paths": [str(tmp_path / "cal.ics")]}, {"paths": [str(gone)]}),
        ("overrides", {"path": str(tmp_path / "overrides.yaml")}, None),
    ):
        check_connector(lambda t=type_, e=here: connector(t, **e),
                        unreachable=(lambda t=type_, e=there: connector(t, **e)) if there else None)  # fmt: skip


def test_the_contract_catches_mistakes():
    class Unstable:
        type = "unstable"
        calls = 0

        def list_ids(self):
            Unstable.calls += 1
            yield "a", str(Unstable.calls)  # a new version on every listing

        def fetch(self, ids):
            yield Item(external_id="a", version=str(Unstable.calls), kind="file", text="x")

    with pytest.raises(AssertionError, match="listing twice"):
        check_connector(Unstable)

    class LooksEmpty(Unstable):
        def list_ids(self):
            return iter([("a", "1")])

        def fetch(self, ids):
            return iter([Item(external_id="a", version="1", kind="file", text="x")
                         for i in ids if i == "a"])  # fmt: skip

    class Unplugged(LooksEmpty):
        def list_ids(self):
            return iter([])

    with pytest.raises(AssertionError, match="SourceUnavailable"):
        check_connector(LooksEmpty, unreachable=Unplugged)


@pytest.fixture
def example(monkeypatch):
    monkeypatch.syspath_prepend(str(EXAMPLE / "src"))
    sys.modules.pop("graph_me_notes", None)
    import graph_me_notes

    return graph_me_notes


def test_example_plugin(example, tmp_path, conn):
    spec = importlib.util.spec_from_file_location("example_contract", EXAMPLE / "tests" /
                                                  "test_contract.py")  # fmt: skip
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.test_contract(tmp_path)  # the plugin's own test, as its author runs it

    # Registered the way an installed plugin is (entry point), it works in a scan.
    registry.register("notes-jsonl", example.NotesConnector)
    path = tmp_path / "notes.jsonl"
    path.write_text(json.dumps({"id": "n1", "title": "Boiler", "text": "Serviced in May",
                                "author": "sam@example.org"}) + "\n")  # fmt: skip
    cfg = Config(sources={"notes": SourceConfig(type="notes-jsonl", path=str(path))})
    run.scan(conn, cfg, workers=1)
    assert_consistent(conn)
    assert [r[0] for r in conn.execute("SELECT title FROM items")] == ["Boiler"]
    pyproject = (EXAMPLE / "pyproject.toml").read_text()
    assert '[project.entry-points."graph_me.connectors"]' in pyproject


# -- rule packs --------------------------------------------------------------------------------


@pytest.mark.parametrize("lang", sorted(rules.load_all()))
def test_builtin_pack_examples(lang):
    path = Path(rules.__file__).parent / f"{lang}.yaml"
    pack = rules.read_pack(path)
    assert pack.examples.birthday.match, "every pack has examples"
    assert rules.check_examples(pack) == []


def test_template_is_a_valid_pack_and_is_not_loaded():
    template = rules.read_pack(Path(rules.__file__).parent / "_template.yaml")
    assert template.language == "xx" and "xx" not in rules.load_all()


@pytest.fixture
def user_rules(tmp_path, monkeypatch):
    folder = tmp_path / "rules"
    folder.mkdir()
    monkeypatch.setenv(rules.ENV_RULES, str(folder))
    rules.reload()
    yield folder
    monkeypatch.delenv(rules.ENV_RULES)
    rules.reload()


def test_user_packs_add_a_language_and_words(user_rules):
    (user_rules / "de.yaml").write_text(
        "language: de\nstopwords: [der, die, und]\n"
        "birthday: {greetings: [alles gute zum geburtstag]}\n"
        "relations: {sibling: [schwester]}\n"
    )
    (user_rules / "en.yaml").write_text("language: en\nrelations: {friend: [bestie]}\n")
    (user_rules / "_draft.yaml").write_text("language: zz\n")  # skipped
    rules.reload()
    packs = rules.load_all()
    assert set(packs) == {"en", "fr", "de"}
    assert facts.find_greeting("Alles Gute zum Geburtstag, Anna!") == "de"
    words = rules.relation_words()
    assert words["schwester"] == "sibling" and words["bestie"] == "friend"
    assert words["sister"] == "sibling"  # the built-in English words are still there


def test_a_broken_user_pack_is_reported(user_rules):
    (user_rules / "es.yaml").write_text("language: es\ndeadlines: {soon: [pronto]}\n")
    rules.reload()
    with pytest.raises(ValueError, match="es.yaml"):
        rules.load_all()


def test_cli_rules_check(tmp_path, user_rules):
    good = tmp_path / "de.yaml"
    good.write_text(
        "language: de\nbirthday: {greetings: [alles gute zum geburtstag]}\n"
        "examples:\n  birthday: {match: ['Alles Gute zum Geburtstag!'], no_match: ['Hallo']}\n"
    )
    result = runner.invoke(app, ["rules", "check", str(good)])
    assert result.exit_code == 0, result.output
    assert "2 examples passed" in result.output
    assert "de" not in rules.load_all()  # checking doesn't install it

    bad = tmp_path / "bad.yaml"
    bad.write_text(good.read_text().replace("'Hallo'", "'Alles Gute zum Geburtstag'"))
    result = runner.invoke(app, ["rules", "check", str(bad)])
    assert result.exit_code == 1 and "a greeting was found" in result.output
    invalid = tmp_path / "invalid.yaml"
    invalid.write_text("language: German\n")
    assert runner.invoke(app, ["rules", "check", str(invalid)]).exit_code == 1
    assert "language: xx" in runner.invoke(app, ["rules", "template"]).output


def test_user_rules_reach_the_worker_processes(tmp_path, monkeypatch):
    """The CLI points GRAPH_ME_RULES at rules/ next to config.yaml (workers inherit it)."""
    base = tmp_path / "base"
    (base / "rules").mkdir(parents=True)
    (base / "config.yaml").write_text("sources: {}\n")
    monkeypatch.delenv(rules.ENV_RULES, raising=False)
    monkeypatch.setattr("graph_me.cli._rules_set", None)
    runner.invoke(app, ["--config", str(base / "config.yaml"), "rules", "list"])
    assert os.environ[rules.ENV_RULES] == str(base / "rules")
    monkeypatch.delenv(rules.ENV_RULES)
    rules.reload()


# -- bundles -----------------------------------------------------------------------------------


@pytest.fixture
def demo(tmp_path):
    cfg_path = demo_mod.build(tmp_path / "demo")
    (tmp_path / "demo" / "bank.vcf").write_text(
        f"BEGIN:VCARD\nVERSION:3.0\nUID:bank\nFN:Lakeside Bank\nNOTE:IBAN {TEST_IBAN}\n"
        "EMAIL:help@bank.example\nEND:VCARD\n"
    )
    text = cfg_path.read_text().replace("contacts.vcf", "")  # read every .vcf in the folder
    cfg_path.write_text(text)
    cfg = config.load_config(cfg_path)
    out = config.ensure_output(config.resolve_output(None, cfg))
    conn = db_connect(out)
    run.scan(conn, cfg, workers=1)
    conn.close()
    return cfg, cfg_path, out


def db_connect(out):
    from graph_me.store import db

    return db.connect(out / config.DB_NAME)


def test_bundle(demo, tmp_path):
    cfg, _, out = demo
    service = Service(cfg, out / config.DB_NAME)
    items = bundle.plan(service, "lease landlord bank", limit=20)
    folder = tmp_path / "apartment"
    result = bundle.write(service, items, folder)
    assert stat.S_IMODE(folder.stat().st_mode) == 0o700
    names = sorted(p.name for p in folder.rglob("*") if p.is_file())
    assert "INDEX.md" in names and any(n.endswith("lease_2025.pdf") for n in names)
    assert (folder / "files").is_dir() and (folder / "contacts").is_dir()
    assert not any(" " in n for n in names)
    bank = next(p for p in result.messages if "Lakeside" in p.name)
    assert "[REDACTED:iban]" in bank.read_text() and TEST_IBAN not in bank.read_text()
    index = (folder / "INDEX.md").read_text()
    assert "lease_2025.pdf" in index and "files/" in index
    lease = next(p for p in result.files if p.name.endswith("lease_2025.pdf"))
    original = Path(next(i["path"] for i in items if i["title"] == "lease_2025.pdf"))
    assert lease.read_bytes() == original.read_bytes() and original.exists()  # copied, not moved
    with pytest.raises(FileExistsError):
        bundle.write(service, items, folder)

    revealed = bundle.write(service, items, tmp_path / "revealed", reveal=True)
    bank = next(p for p in revealed.messages if "Lakeside" in p.name)
    assert TEST_IBAN in bank.read_text()


def test_cli_bundle(demo, tmp_path):
    _, cfg_path, _ = demo
    target = tmp_path / "move"
    result = runner.invoke(app, ["--config", str(cfg_path), "export", "bundle", "lease",
                                 "--out", str(target)], input="y\n")  # fmt: skip
    assert result.exit_code == 0, result.output
    assert "lease_2025.pdf" in result.output and (target / "INDEX.md").exists()
    result = runner.invoke(app, ["--config", str(cfg_path), "export", "bundle", "lease",
                                 "--out", str(target), "--yes"])  # fmt: skip
    assert result.exit_code == 1 and "not empty" in result.output
    nothing = runner.invoke(app, ["--config", str(cfg_path), "export", "bundle", "zzzqqq",
                                  "--out", str(tmp_path / "x"), "--yes"])  # fmt: skip
    assert nothing.exit_code == 1


def test_bundle_is_not_an_agent_tool(demo):
    """Writing files is a terminal action: no MCP tool exports anything."""
    import anyio
    from mcp.client import Client

    from graph_me.mcp_server import build_server

    cfg, _, out = demo
    server = build_server(Service(cfg, out / config.DB_NAME, "mcp"))

    async def names():
        async with Client(server) as client:
            return {t.name for t in (await client.list_tools()).tools}

    found = anyio.run(names)
    assert found and not any(w in n for n in found for w in ("export", "bundle", "write"))
