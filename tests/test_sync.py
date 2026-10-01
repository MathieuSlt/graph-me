"""M3: sync mirrors the sources and forgets what they no longer have."""

from datetime import UTC, datetime

import pytest
from conftest import assert_consistent, docs_config
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from graph_me.config import Config, PeopleConfig, SourceConfig
from graph_me.connectors import registry
from graph_me.connectors.base import Item, Party, SourceUnavailable
from graph_me.pipeline import run
from graph_me.query import engine, graph
from graph_me.store import db

# --- an in-memory connector the tests can edit ------------------------------------------------

STORE: dict[str, dict[str, Item]] = {}
UNAVAILABLE: set[str] = set()


class MemoryConnector:
    type = "memory"

    def __init__(self, name, source, blacklist):
        self.name = name

    def list_ids(self):
        if self.name in UNAVAILABLE:
            raise SourceUnavailable(f"{self.name} is offline")
        return ((eid, item.version) for eid, item in STORE.get(self.name, {}).items())

    def fetch(self, ids):
        items = STORE.get(self.name, {})
        return (items[i] for i in ids if i in items)


registry.register("memory", MemoryConnector)

PEOPLE = {
    "sophie": Party(name="Sophie Martin", phone="+33612345678"),
    "jean": Party(name="Jean Dupont", email="jean@example.org"),
    "paul": Party(name="Paul Petit", email="paul@example.org"),
}
ME = Party(name="Camille", email="camille@example.com")


def message(n: int, to: str, text: str, year: int = 2024, version: str = "1") -> Item:
    return Item(
        external_id=f"m{n}", version=version, kind="message", text=text,
        author=ME, recipients=[PEOPLE[to]], is_from_me=True, trust="self",
        created_at=datetime(year, 3, 12, 9, tzinfo=UTC),
    )  # fmt: skip


def memory_config(*names: str) -> Config:
    return Config(
        people=PeopleConfig(timezone="UTC"),
        sources={n: SourceConfig(type="memory") for n in names},
    )


@pytest.fixture(autouse=True)
def clean_memory():
    STORE.clear()
    UNAVAILABLE.clear()
    yield
    STORE.clear()
    UNAVAILABLE.clear()


def put(source: str, *items: Item) -> None:
    STORE.setdefault(source, {}).update({i.external_id: i for i in items})


def birthday_of(conn, who: str) -> list[str]:
    found = graph.get_fact(conn, who, "birthday", PeopleConfig())
    return [f["value"] for p in found for f in p["facts"]]


# --- behaviour ---------------------------------------------------------------------------------


def test_sync_forgets_deleted_items_and_what_came_only_from_them(conn):
    put("chat", message(1, "sophie", "Joyeux anniversaire !", 2023),
        message(2, "sophie", "Joyeux anniv !", 2024), message(3, "jean", "Bonjour"))  # fmt: skip
    cfg = memory_config("chat")
    run.sync(conn, cfg, workers=1)
    assert birthday_of(conn, "Sophie") == ["03-12"]

    del STORE["chat"]["m1"]
    st_ = run.sync(conn, cfg, workers=1).sources["chat"]
    assert st_.forgotten == 1
    assert birthday_of(conn, "Sophie") == ["03-12"]  # still backed by m2
    assert_consistent(conn)

    del STORE["chat"]["m2"]
    run.sync(conn, cfg, workers=1)
    assert birthday_of(conn, "Sophie") == []  # no evidence left: the fact is gone
    assert graph.find_people(conn, "Sophie") == []  # and so is Sophie: nothing mentions her
    assert graph.find_people(conn, "Jean")  # Jean is still mentioned by m3
    assert_consistent(conn)


def test_scan_never_forgets(conn):
    put("chat", message(1, "jean", "Bonjour"), message(2, "jean", "Salut"))
    cfg = memory_config("chat")
    run.scan(conn, cfg, workers=1)
    del STORE["chat"]["m1"]
    run.scan(conn, cfg, workers=1)
    assert conn.execute("SELECT count(*) FROM items").fetchone()[0] == 2
    assert run.sync(conn, cfg, workers=1).sources["chat"].forgotten == 1


def test_changed_item_drops_facts_from_its_old_version(conn):
    put("chat", message(1, "sophie", "Joyeux anniversaire !"))
    cfg = memory_config("chat")
    run.sync(conn, cfg, workers=1)
    put("chat", message(1, "sophie", "On se voit demain ?", version="2"))
    st_ = run.sync(conn, cfg, workers=1).sources["chat"]
    assert (st_.updated, st_.forgotten) == (1, 0)
    assert birthday_of(conn, "Sophie") == []
    assert_consistent(conn)


def test_sync_is_idempotent(conn):
    put("chat", *(message(n, "paul", f"message {n}") for n in range(10)))
    cfg = memory_config("chat")
    run.sync(conn, cfg, workers=1)
    before = conn.execute("SELECT count(*) FROM items").fetchone()[0]
    st_ = run.sync(conn, cfg, workers=1).sources["chat"]
    assert (st_.added, st_.updated, st_.forgotten, st_.unchanged) == (0, 0, 0, 10)
    assert conn.execute("SELECT count(*) FROM items").fetchone()[0] == before


def test_unavailable_source_is_skipped_not_wiped(conn):
    put("chat", *(message(n, "paul", f"message {n}") for n in range(5)))
    cfg = memory_config("chat")
    run.sync(conn, cfg, workers=1)
    UNAVAILABLE.add("chat")
    report = run.sync(conn, cfg, workers=1)
    assert not report.ok and "offline" in report.sources["chat"].failure
    assert conn.execute("SELECT count(*) FROM items").fetchone()[0] == 5


def test_mass_forget_needs_permission(conn):
    put("chat", *(message(n, "paul", f"message {n}") for n in range(100)))
    cfg = memory_config("chat")
    run.sync(conn, cfg, workers=1)
    STORE["chat"] = {k: v for k, v in STORE["chat"].items() if k in {"m1", "m2"}}

    report = run.sync(conn, cfg, workers=1)
    assert "would forget 98 of 100" in report.sources["chat"].failure
    assert conn.execute("SELECT count(*) FROM items").fetchone()[0] == 100

    report = run.sync(conn, cfg, workers=1, allow_mass_forget=True)
    assert report.ok and report.sources["chat"].forgotten == 98
    assert_consistent(conn)


def test_small_deletions_need_no_permission(conn):
    put("chat", *(message(n, "paul", f"message {n}") for n in range(40)))
    cfg = memory_config("chat")
    run.sync(conn, cfg, workers=1)
    STORE["chat"].clear()
    assert run.sync(conn, cfg, workers=1).sources["chat"].forgotten == 40  # under MASS_FORGET_MIN


def test_source_removed_from_config_is_forgotten(conn):
    put("chat", message(1, "jean", "Bonjour"))
    put("work", message(2, "paul", "Réunion lundi"))
    run.sync(conn, memory_config("chat", "work"), workers=1)
    report = run.sync(conn, memory_config("chat"), workers=1)
    assert report.removed_sources == {"work": 1}
    assert engine.search(conn, "réunion") == []
    assert conn.execute("SELECT id FROM sources").fetchall()[0][0] == "chat"
    assert_consistent(conn)

    # a single-source sync never touches other sources
    put("work", message(2, "paul", "Réunion lundi"))
    run.sync(conn, memory_config("chat", "work"), workers=1)
    assert (
        run.sync(conn, memory_config("chat"), only_source="chat", workers=1).removed_sources == {}
    )


def test_removing_a_big_source_needs_permission(conn):
    put("work", *(message(n, "paul", f"message {n}") for n in range(60)))
    run.sync(conn, memory_config("work"), workers=1)
    report = run.sync(conn, memory_config(), workers=1)
    assert "work" in report.failures and report.removed_sources == {}
    assert run.sync(conn, memory_config(), workers=1, allow_mass_forget=True).removed_sources == {
        "work": 60
    }


# --- files -------------------------------------------------------------------------------------


def test_deleted_file_is_forgotten(conn, docs):
    cfg = docs_config(docs)
    run.sync(conn, cfg, workers=1)
    (docs / "notes/recette_crepes.md").unlink()
    assert run.sync(conn, cfg, workers=1).sources["docs"].forgotten == 1
    assert engine.search(conn, "farine") == []
    assert_consistent(conn)

    for f in (docs / "Logement").iterdir():  # the whole folder: its project entity goes too
        f.unlink()
    run.sync(conn, cfg, workers=1)
    assert conn.execute("SELECT count(*) FROM entities WHERE name = 'Logement'").fetchone()[0] == 0
    assert_consistent(conn)


def test_missing_folder_is_reported_not_forgotten(conn, docs):
    cfg = docs_config(docs)
    run.sync(conn, cfg, workers=1)
    docs.rename(docs.with_name("docs-unplugged"))
    report = run.sync(conn, cfg, workers=1)
    assert "not found" in report.sources["docs"].failure
    assert conn.execute("SELECT count(*) FROM items").fetchone()[0] == 11


def test_newly_excluded_files_need_no_permission(conn, docs):
    for n in range(80):
        (docs / ".next" / f"chunk{n}.js").parent.mkdir(exist_ok=True)
        (docs / ".next" / f"chunk{n}.js").write_text(f"const BAILOUT_{n} = 1\n")
    run.sync(conn, docs_config(docs, exclude=[]), workers=1)  # nothing excluded: 80 chunks in

    # the defaults now skip .next: forgetting the chunks is a config change, not a lost source
    report = run.sync(conn, docs_config(docs), workers=1)
    assert report.ok and report.sources["docs"].forgotten == 81  # and node_modules/leftpad
    assert engine.search(conn, "BAILOUT_1") == []
    assert_consistent(conn)


def test_big_forget_compacts_the_store(tmp_path, docs):
    db_path = tmp_path / "graph.db"
    conn = db.connect(db_path)
    big = "".join(f"ligne {n} du journal de bord numéro {n * 7}\n" for n in range(20_000))
    for n in range(12):
        (docs / f"journal{n}.txt").write_text(big)
    cfg = docs_config(docs)
    run.sync(conn, cfg, workers=1)
    full = db_path.stat().st_size
    for n in range(12):
        (docs / f"journal{n}.txt").unlink()
    run.sync(conn, cfg, workers=1)
    assert conn.execute("PRAGMA freelist_count").fetchone()[0] == 0
    assert db_path.stat().st_size < full / 2
    assert_consistent(conn)
    conn.close()


def test_deleted_files_still_need_permission(conn, docs):
    for n in range(80):
        (docs / f"note{n}.txt").write_text(f"note {n}\n")
    cfg = docs_config(docs)
    run.sync(conn, cfg, workers=1)
    for n in range(80):
        (docs / f"note{n}.txt").unlink()
    assert "would forget 80 of 91" in run.sync(conn, cfg, workers=1).sources["docs"].failure


# --- property test ------------------------------------------------------------------------------

operation = st.tuples(
    st.sampled_from(["add", "edit", "delete"]),
    st.integers(min_value=0, max_value=12),
    st.sampled_from(sorted(PEOPLE)),
    st.sampled_from(["Joyeux anniversaire !", "Bonjour", "Happy birthday", "Merci"]),
    st.integers(min_value=2020, max_value=2025),
)


@settings(
    max_examples=40, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(st.lists(operation, min_size=1, max_size=25))
def test_random_changes_keep_the_graph_consistent(tmp_path, ops):
    STORE.clear()
    conn = db.connect(tmp_path / f"prop-{abs(hash(tuple(ops)))}.db")
    cfg = memory_config("chat")
    try:
        for n_op, (op, n, who, text, year) in enumerate(ops):
            items = STORE.setdefault("chat", {})
            if op == "add" or (op == "edit" and f"m{n}" in items):
                items[f"m{n}"] = message(n, who, text, year, version=str(n_op))
            elif op == "delete":
                items.pop(f"m{n}", None)
            run.sync(conn, cfg, workers=1)
            assert_consistent(conn)
            stored = {r[0] for r in conn.execute("SELECT external_id FROM items")}
            assert stored == set(items)
            # every birthday fact is backed by a stored greeting to that person
            for value, count in conn.execute(
                "SELECT f.value, count(e.item_id) FROM facts f JOIN evidence e ON e.fact_id = f.id "
                "WHERE f.key = 'birthday' GROUP BY f.id"
            ):
                assert value == "03-12" and count >= 1
    finally:
        conn.close()
