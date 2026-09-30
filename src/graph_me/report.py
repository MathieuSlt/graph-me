"""REPORT.md and graph.json, written to graph-out after every scan and sync.

- Communities: Louvain over people, projects and documents, weighted by relation strength.
- REPORT.md: what was indexed, who matters most, upcoming birthdays, communities, questions
  worth asking, and the Tier 0 limits. Names only: no message text, no secrets.
- graph.json: nodes (entities) and edges (relations) for tools and the web UI.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import networkx as nx

TOP = 10
MIN_COMMUNITY = 3
BIRTHDAY_WINDOW_DAYS = 45
COMMUNITY_SEED = 42  # communities are stable from one run to the next


def detect_communities(conn: sqlite3.Connection) -> int:
    """Recompute communities (tier 0). Returns how many were found (size >= MIN_COMMUNITY).

    The user ("me") is left out: linked to everyone, it would pull everything into one group.
    """
    graph = nx.Graph()
    me = {r[0] for r in conn.execute("SELECT entity_id FROM aliases WHERE kind = 'role'")}
    for src, dst, weight in conn.execute("SELECT src, dst, weight FROM relations"):
        if src in me or dst in me:
            continue
        if graph.has_edge(src, dst):
            graph[src][dst]["weight"] += weight
        else:
            graph.add_edge(src, dst, weight=weight)
    conn.execute("DELETE FROM communities WHERE tier = 0")
    if graph.number_of_edges() == 0:
        return 0
    groups = nx.community.louvain_communities(graph, weight="weight", seed=COMMUNITY_SEED)
    groups = sorted((g for g in groups if len(g) >= MIN_COMMUNITY), key=len, reverse=True)
    for members in groups:
        label = _label(conn, members)
        cid = conn.execute(
            "INSERT INTO communities(label, tier) VALUES (?, 0) RETURNING id", (label,)
        ).fetchone()[0]
        conn.executemany(
            "INSERT OR IGNORE INTO community_members(community_id, entity_id) VALUES (?, ?)",
            [(cid, eid) for eid in members],
        )
    conn.commit()
    return len(groups)


def _label(conn: sqlite3.Connection, members: set[str]) -> str:
    """Tier 0 label: its best-connected project, else its two most mentioned people."""
    marks = ",".join("?" * len(members))
    rows = conn.execute(
        f"""SELECT e.kind, e.name, count(m.item_id) AS n FROM entities e
            LEFT JOIN mentions m ON m.entity_id = e.id
            WHERE e.id IN ({marks}) AND e.name IS NOT NULL
              AND e.id NOT IN (SELECT entity_id FROM aliases WHERE kind = 'role')
            GROUP BY e.id ORDER BY n DESC""",
        list(members),
    ).fetchall()
    projects = [r["name"] for r in rows if r["kind"] == "project"]
    if projects:
        return projects[0]
    people = [r["name"] for r in rows if r["kind"] == "person"][:2]
    return " & ".join(people) or "unnamed"


def _top_people(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """People you exchange with: items involving them, counting double those you wrote to them.

    One-off senders (newsletters, notifications) are left out.
    """
    return conn.execute(
        """SELECT e.id, e.name, count(DISTINCT m.item_id) AS items
           FROM entities e JOIN mentions m ON m.entity_id = e.id
           WHERE e.kind = 'person' AND e.name IS NOT NULL
             AND e.id NOT IN (SELECT entity_id FROM aliases WHERE kind = 'role')
           GROUP BY e.id HAVING items >= 2
           ORDER BY items + 2 * (SELECT coalesce(sum(r.weight), 0) FROM relations r
                                 WHERE r.dst = e.id AND r.type = 'wrote_to'
                                   AND r.src IN (SELECT entity_id FROM aliases
                                                 WHERE kind = 'role')) DESC, e.name
           LIMIT ?""",
        (TOP,),
    ).fetchall()


def _upcoming_birthdays(conn: sqlite3.Connection, today: date) -> list[tuple[date, str, float]]:
    out = []
    for name, value, confidence in conn.execute(
        """SELECT e.name, f.value, f.confidence FROM facts f JOIN entities e ON e.id = f.entity_id
           WHERE f.key = 'birthday' AND e.name IS NOT NULL"""
    ):
        month, day = (int(x) for x in value.split("-"))
        for year in (today.year, today.year + 1):
            try:
                when = date(year, month, day)
            except ValueError:  # 29 February in a non-leap year
                continue
            if today <= when <= today + timedelta(days=BIRTHDAY_WINDOW_DAYS):
                out.append((when, name, confidence))
                break
    return sorted(out)


def build_report(conn: sqlite3.Connection, today: date | None = None) -> str:
    today = today or date.today()
    sources = conn.execute(
        """SELECT s.id, s.type, s.last_sync_at, count(i.id) AS items
           FROM sources s LEFT JOIN items i ON i.source_id = s.id GROUP BY s.id ORDER BY s.id"""
    ).fetchall()
    kinds = dict(conn.execute("SELECT kind, count(*) FROM items GROUP BY kind").fetchall())
    people = _top_people(conn)
    birthdays = _upcoming_birthdays(conn, today)
    communities = conn.execute(
        """SELECT c.label, count(cm.entity_id) AS size FROM communities c
           JOIN community_members cm ON cm.community_id = c.id
           GROUP BY c.id ORDER BY size DESC LIMIT ?""",
        (TOP,),
    ).fetchall()
    flagged = conn.execute("SELECT count(*) FROM items WHERE risk_score >= 0.5").fetchone()[0]
    folders = conn.execute(
        """SELECT e.name, count(m.item_id) AS n
           FROM entities e JOIN mentions m ON m.entity_id = e.id
           WHERE e.kind = 'project' GROUP BY e.id ORDER BY n DESC LIMIT 3"""
    ).fetchall()

    lines = [
        "# graph-me report",
        "",
        f"Generated {today.isoformat()}. Tier 0: no AI, so answers match words, not meaning. "
        "`graph-me enrich` (M5) will improve them.",
        "",
        "## Sources",
        "",
        "| Source | Type | Items | Last sync |",
        "| --- | --- | --- | --- |",
    ]
    lines += [
        f"| {s['id']} | {s['type']} | {s['items']} | {(s['last_sync_at'] or '-')[:16]} |"
        for s in sources
    ]
    lines += ["", "Items: " + (", ".join(f"{n} {k}s" for k, n in sorted(kinds.items())) or "none")]
    if flagged:
        lines.append(
            f"{flagged} items contain instruction-like text (possible prompt injection); "
            "results mark them as flagged."
        )

    if people:
        lines += ["", "## People you deal with most", ""]
        lines += [f"- {p['name']} ({p['items']} items)" for p in people]
    if birthdays:
        lines += ["", f"## Birthdays in the next {BIRTHDAY_WINDOW_DAYS} days", ""]
        lines += [
            f"- {when:%d %B}: {name} (confidence {confidence:.2f})"
            for when, name, confidence in birthdays
        ]
    if communities:
        lines += ["", "## Communities", ""]
        lines += [f"- {c['label']} ({c['size']} members)" for c in communities]

    questions = []
    if birthdays or people:
        someone = birthdays[0][1] if birthdays else people[0]["name"]
        questions.append(f"When is {someone}'s birthday?")
    if people:
        questions.append(f"What did {people[0]['name']} send me last?")
    if folders:
        questions.append(f"Which documents are in {folders[0]['name']}?")
    questions.append("Where is my lease (contrat de bail)?")
    lines += ["", "## Try asking your agent", ""]
    lines += [f'- "use graph-me: {q}"' for q in questions]
    return "\n".join(lines) + "\n"


def build_graph_json(conn: sqlite3.Connection) -> dict:
    community_of = dict(conn.execute("SELECT entity_id, community_id FROM community_members"))
    nodes = [
        {
            "id": r["id"],
            "kind": r["kind"],
            "name": r["name"],
            "mentions": r["mentions"],
            "community": community_of.get(r["id"]),
        }
        for r in conn.execute(
            """SELECT e.id, e.kind, e.name, count(m.item_id) AS mentions FROM entities e
               LEFT JOIN mentions m ON m.entity_id = e.id GROUP BY e.id"""
        )
    ]
    edges = [
        {"source": r["src"], "target": r["dst"], "type": r["type"], "weight": r["weight"]}
        for r in conn.execute("SELECT src, dst, type, weight FROM relations")
    ]
    communities = [
        {"id": r["id"], "label": r["label"]}
        for r in conn.execute("SELECT id, label FROM communities ORDER BY id")
    ]
    return {"nodes": nodes, "edges": edges, "communities": communities}


def write(conn: sqlite3.Connection, out: Path, today: date | None = None) -> tuple[Path, Path]:
    detect_communities(conn)
    report, graph_json = out / "REPORT.md", out / "graph.json"
    report.write_text(build_report(conn, today), encoding="utf-8")
    graph_json.write_text(
        json.dumps(build_graph_json(conn), ensure_ascii=False, indent=1), encoding="utf-8"
    )
    return report, graph_json
