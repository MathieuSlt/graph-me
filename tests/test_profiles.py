"""M10: profiles limit what an agent sees; the activity log shows what it asked.

The security property: under a profile, no answer, from any Service method or MCP tool, holds
anything learned only from the profile's excluded sources. Sophie is in both worlds: her work
card and her family card share her work email, so they merge into one person.
"""

import json
from datetime import date

import anyio
import pytest
from conftest import assert_consistent
from mcp.client import Client
from typer.testing import CliRunner

from graph_me import config, doctor
from graph_me import overrides as corrections
from graph_me.cli import app
from graph_me.mcp_server import build_server
from graph_me.pipeline import run
from graph_me.query import graph
from graph_me.service import Service, UnknownProfile
from graph_me.setup import clients
from graph_me.store import db

WORK_CARD = """BEGIN:VCARD
VERSION:3.0
UID:w-sophie
FN:Sophie Martin
EMAIL:sophie@work.example
ORG:Atlas
TITLE:Engineer
END:VCARD
"""
FAMILY_CARD = """BEGIN:VCARD
VERSION:3.0
UID:f-sophie
FN:Sophie Martin
NICKNAME:Soso
EMAIL:sophie@work.example
TEL:06 99 88 77 66
BDAY:1990-07-04
ADR:;;7 Garden Lane;Lyon;;69000;France
END:VCARD
BEGIN:VCARD
VERSION:3.0
UID:f-rose
FN:Grandma Rose
TEL:06 11 22 33 44
BDAY:1940-02-02
END:VCARD
"""
# Things only the family sources know: none may reach the work profile.
SECRETS = ("Grandma", "Rose", "+33699887766", "+33611223344", "Soso", "07-04", "02-02",
           "Garden Lane", "diary", "Secret", "2027-06-30", "family-", "sibling")  # fmt: skip
TODAY = date(2026, 12, 1)


@pytest.fixture
def world(tmp_path):
    for side, card, doc in (
        ("work", WORK_CARD, ("budget.md", "Q3 budget review with Sophie. Payment due by "
                             "2027-01-31.")),
        ("family", FAMILY_CARD, ("diary.md", "Secret family diary. Grandma Rose visits. "
                                 "The lease ends on June 30, 2027.")),
    ):  # fmt: skip
        (tmp_path / side / "docs").mkdir(parents=True)
        (tmp_path / side / "contacts.vcf").write_text(card)
        (tmp_path / side / "docs" / doc[0]).write_text(doc[1])
    raw = {
        "overrides": str(tmp_path / "overrides.yaml"),
        "people": {"phone_country_code": "33"},
        "sources": {
            f"{side}-{kind}": (
                {"type": "filesystem", "paths": [str(tmp_path / side / "docs")]}
                if kind == "docs"
                else {"type": "vcard", "paths": [str(tmp_path / side / "contacts.vcf")]}
            )
            for side in ("work", "family")
            for kind in ("docs", "contacts")
        },
        "profiles": {"work": {"sources": ["work-docs", "work-contacts"]}},
    }
    cfg = config.Config.model_validate(raw)
    fixes = corrections.Overrides()
    corrections.relate(fixes, "sophie@work.example", "sibling")
    corrections.save(cfg.overrides, fixes)
    db_path = tmp_path / "graph.db"
    conn = db.connect(db_path)
    run.scan(conn, cfg, workers=1)
    assert_consistent(conn)
    conn.commit()
    ids = {
        "sophie": graph.find_people(conn, "sophie@work.example")[0],
        "diary": conn.execute("SELECT id FROM items WHERE title = 'diary.md'").fetchone()[0],
        "budget": conn.execute("SELECT id FROM items WHERE title = 'budget.md'").fetchone()[0],
    }
    conn.close()
    return cfg, db_path, ids


def without_queries(value):
    """Answers repeat the question ("query"): only what came back from the graph counts."""
    if isinstance(value, dict):
        return {k: without_queries(v) for k, v in value.items() if k != "query"}
    if isinstance(value, list):
        return [without_queries(v) for v in value]
    return value


def leaks(answer) -> list[str]:
    text = json.dumps(without_queries(answer), ensure_ascii=False)
    return [s for s in SECRETS if s in text]


def answers(service: Service, ids: dict) -> dict:
    """Everything an agent or the UI can ask, by name."""
    return {
        "search sophie": service.search("Sophie"),
        "search diary": service.search("diary"),
        "search grandma": service.search("Grandma Rose"),
        "search lease": service.search("lease ends"),
        "find_document": service.find_document("diary"),
        "who sophie": service.who_is("Sophie"),
        "who rose": service.who_is("Rose"),
        "who phone": service.who_is("+33699887766"),
        "fact birthday": service.get_fact("Sophie", "birthday"),
        "fact sister": service.get_fact("my sister", "birthday"),
        "related": service.related("Sophie"),
        "timeline": service.timeline("Sophie"),
        "timeline topic": service.timeline(query="diary Grandma lease"),
        "thread diary": service.thread(ids["diary"]),
        "thread budget": service.thread(ids["budget"]),
        "upcoming": service.upcoming(days=366, today=TODAY),
        "status": service.status(),
        "graph": service.graph_view(),
        "neighbours": service.neighbourhood(ids["sophie"]),
        "entity": service.entity_page(ids["sophie"]),
        "item diary": service.get_item(ids["diary"]),
        "item page diary": service.item_page(ids["diary"]),
        "item budget": service.get_item(ids["budget"]),
        "activity": service.activity(),
    }


def test_work_profile_sees_nothing_from_family(world):
    cfg, db_path, ids = world
    everything = answers(Service(cfg, db_path), ids)
    full = json.dumps(everything, ensure_ascii=False)
    assert all(secret in full for secret in ("Grandma", "+33699887766", "07-04", "diary"))

    scoped = answers(Service(cfg, db_path, profile="work"), ids)
    for name, answer in scoped.items():
        assert not leaks(answer), f"{name} leaked {leaks(answer)}: {json.dumps(answer)[:400]}"

    # What work may see is still there.
    [sophie] = scoped["who sophie"]["people"]
    assert [a["value"] for a in sophie["aliases"]] == ["sophie@work.example"]
    assert {f["key"]: f["value"] for f in sophie["facts"]} == {
        "organization": "Atlas", "job_title": "Engineer"
    }  # fmt: skip
    assert [i["title"] for i in scoped["search sophie"]["answer_items"]] != []
    assert "error" in scoped["item diary"] and "error" not in scoped["item budget"]
    assert [u["key"] for u in scoped["upcoming"]["results"]] == ["due"]
    assert [s["name"] for s in scoped["status"]["sources"]] == ["work-contacts", "work-docs"]
    assert scoped["status"]["profile"] == "work"


def test_profile_over_mcp(world):
    cfg, db_path, _ = world
    server = build_server(Service(cfg, db_path, "mcp", profile="work"))

    async def go():
        async with Client(server) as client:
            out = []
            for tool, args in (("search", {"query": "Grandma diary"}), ("who_is", {"who": "Sophie"}),
                               ("upcoming", {"days": 366}), ("status", {})):  # fmt: skip
                result = await client.call_tool(tool, args)
                assert not result.is_error
                out.append(result.content[0].text)
            return out

    for text in anyio.run(go):
        assert not leaks(json.loads(text)), text[:300]


def test_unknown_profile_and_bad_config(world):
    cfg, db_path, _ = world
    with pytest.raises(UnknownProfile):
        Service(cfg, db_path, profile="nope")
    raw = cfg.model_dump(mode="json")
    raw["profiles"] = {"x": {"sources": ["missing"]}}
    with pytest.raises(ValueError, match="unknown source missing"):
        config.Config.model_validate(raw)


def test_activity_log(world):
    cfg, db_path, ids = world
    work = Service(cfg, db_path, "mcp", profile="work", client="Claude Code")
    work.search("budget")
    Service(cfg, db_path).search("diary")

    [row] = work.activity()  # a profile sees its own queries only
    assert (row["query"], row["client"], row["profile"], row["interface"]) == (
        "budget", "Claude Code", "work", "mcp"
    )  # fmt: skip
    assert row["results"][0]["title"] == "budget.md"
    everything = Service(cfg, db_path).activity()
    assert [r["query"] for r in everything] == ["diary", "budget"]

    # Forgotten items show as gone; old entries are pruned; the log can be cleared.
    conn = db.connect(db_path)
    conn.execute("DELETE FROM items WHERE id = ?", (ids["diary"],))
    conn.commit()
    assert Service(cfg, db_path).activity()[0]["results"][0]["type"] == "gone"
    conn.execute("UPDATE query_log SET ts = '2020-01-01T00:00:00+00:00' WHERE query = 'diary'")
    assert run.prune_log(conn, 90) == 1
    conn.close()
    assert Service(cfg, db_path).clear_activity() == 1
    assert Service(cfg, db_path).activity() == []


def test_cli_profile_log_and_install(world, tmp_path, monkeypatch):
    cfg, db_path, _ = world
    path = tmp_path / "config.yaml"
    path.write_text(json.dumps(cfg.model_dump(mode="json")))  # JSON is valid YAML
    monkeypatch.setenv(config.ENV_CONFIG, str(path))
    monkeypatch.setenv(config.ENV_OUT, str(db_path.parent))
    runner = CliRunner()
    assert runner.invoke(app, ["query", "budget"]).exit_code == 0
    result = runner.invoke(app, ["log"])
    assert result.exit_code == 0 and "budget" in result.output
    assert runner.invoke(app, ["mcp", "--profile", "nope"]).exit_code == 1

    monkeypatch.setenv("HOME", str(tmp_path))
    result = runner.invoke(app, ["install-mcp", "--client", "cursor", "--profile", "work", "--yes"])
    assert result.exit_code == 0, result.output
    entry = json.loads((tmp_path / ".cursor/mcp.json").read_text())["mcpServers"]["graph-me-work"]
    assert entry["args"][-3:] == ["mcp", "--profile", "work"]
    assert clients.server_command(path)[-1] == "mcp"

    checks = {c.label: c for c in doctor.run(cfg, path, db_path.parent, home=tmp_path)}
    assert checks["profiles"].status == "ok"


def test_ui_activity_and_profile(world):
    pytest.importorskip("starlette")
    from starlette.testclient import TestClient

    from graph_me.ui.app import COOKIE, build_app

    cfg, db_path, ids = world
    client = TestClient(build_app(Service(cfg, db_path, "ui", profile="work"), "t"),
                        base_url="http://127.0.0.1")  # fmt: skip
    client.cookies.set(COOKIE, "t")
    assert "budget.md" in client.get("/search?q=budget").text
    assert "diary.md" not in client.get("/search?q=diary").text
    status = client.get("/status").text
    assert "profile work" in status and "family-" not in status
    for path in ("/", "/graph", "/upcoming?days=366", f"/entity/{ids['sophie']}",
                 "/timeline?q=lease", f"/item/{ids['budget']}"):  # fmt: skip
        assert not [s for s in SECRETS if s in client.get(path).text], path
    graph_data = client.get("/api/graph").text
    assert not [s for s in SECRETS if s in graph_data]
    page = client.get("/activity").text
    assert "<code>budget</code>" in page and "profile work" in page
