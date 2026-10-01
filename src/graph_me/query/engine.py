"""Search: SQLite FTS5 (BM25) over chunk text plus item titles and paths (Tier 0), merged with
meaning-based vector search when Tier 1 embeddings exist (reciprocal rank fusion)."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

from graph_me import rules

_TOKEN = re.compile(r"\w+")
_CANDIDATES = 200
TITLE_WEIGHT = 2.0  # a match in the file name counts double
RRF_K = 60  # reciprocal rank fusion constant
PREFIX_MIN_LEN = 4  # "lease" also matches "leases"; shorter words match exactly


@dataclass
class Hit:
    item_id: str
    kind: str
    title: str | None
    uri: str | None
    source: str
    modified_at: str | None
    trust: str
    risk_score: float
    tier: int
    lang: str | None
    snippet: str
    score: float


def fts_query(text: str) -> str | None:
    """Turn free text into an FTS5 OR-query, dropping stopwords ("where is my lease")."""
    tokens = [t.casefold() for t in _TOKEN.findall(text)]
    stop = rules.all_stopwords()
    kept = [t for t in tokens if t not in stop and len(t) > 1] or [t for t in tokens if t]
    if not kept:
        return None
    unique = list(dict.fromkeys(kept))
    return " OR ".join(f'"{t}"*' if len(t) >= PREFIX_MIN_LEN else f'"{t}"' for t in unique)


def search(
    conn: sqlite3.Connection,
    text: str,
    *,
    limit: int = 10,
    source: str | None = None,
    kind: str | None = None,
    since: str | None = None,
    until: str | None = None,
    embedder=None,
) -> list[Hit]:
    """``embedder``: a Tier 1 embedder (pipeline/tier1/embed.py) to add meaning-based hits."""
    match = fts_query(text)
    scores: dict[str, float] = {}
    snippets: dict[str, str] = {}
    if match:
        _word_scores(conn, match, scores, snippets)
    if embedder is not None:
        _merge_vectors(conn, embedder, text, scores, snippets)
    return _hits(conn, scores, snippets, limit, source, kind, since, until)


def _word_scores(conn, match: str, scores: dict, snippets: dict) -> None:
    for row in conn.execute(
        """SELECT c.item_id, bm25(chunks_fts) AS rank,
                  snippet(chunks_fts, 0, '[', ']', ' … ', 24) AS snip
           FROM chunks_fts JOIN chunks c ON c.id = chunks_fts.rowid
           WHERE chunks_fts MATCH ? ORDER BY rank LIMIT ?""",
        (match, _CANDIDATES),
    ):
        score = -row["rank"]
        if score > scores.get(row["item_id"], 0.0):
            scores[row["item_id"]] = score
            snippets[row["item_id"]] = row["snip"]
    for row in conn.execute(
        """SELECT i.id, bm25(items_fts, 5.0, 1.0) AS rank
           FROM items_fts JOIN items i ON i.rowid = items_fts.rowid
           WHERE items_fts MATCH ? ORDER BY rank LIMIT ?""",
        (match, _CANDIDATES),
    ):
        scores[row["id"]] = scores.get(row["id"], 0.0) + TITLE_WEIGHT * -row["rank"]


def _merge_vectors(conn, embedder, text: str, scores: dict, snippets: dict) -> None:
    """Fuse word ranking and meaning ranking: score = sum of 1 / (RRF_K + rank)."""
    from graph_me.pipeline.tier1 import embed

    near = embed.nearest(conn, embedder, text, k=_CANDIDATES)
    if not near:
        return
    word_rank = {iid: r for r, iid in enumerate(sorted(scores, key=scores.get, reverse=True))}
    vec_rank: dict[str, int] = {}
    for item_id, chunk_id, _distance in near:
        if item_id not in vec_rank:
            vec_rank[item_id] = len(vec_rank)
            if item_id not in snippets:
                row = conn.execute("SELECT text FROM chunks WHERE id = ?", (chunk_id,)).fetchone()
                snippets[item_id] = " ".join(row[0].split())[:240] if row else ""
    fused = {}
    for iid in set(word_rank) | set(vec_rank):
        fused[iid] = sum(1.0 / (RRF_K + ranks[iid]) for ranks in (word_rank, vec_rank)
                         if iid in ranks)  # fmt: skip
    scores.clear()
    scores.update(fused)


def _hits(conn, scores, snippets, limit, source, kind, since, until) -> list[Hit]:
    if not scores:
        return []

    where, params = [f"i.id IN ({','.join('?' * len(scores))})"], list(scores)
    for column, value, op in (
        ("i.source_id", source, "="),
        ("i.kind", kind, "="),
        ("i.modified_at", since, ">="),
        ("i.modified_at", until, "<="),
    ):
        if value:
            where.append(f"{column} {op} ?")
            params.append(value)
    rows = conn.execute(
        f"""SELECT i.*, (SELECT text FROM chunks WHERE item_id = i.id ORDER BY ord LIMIT 1) AS head
            FROM items i WHERE {" AND ".join(where)}""",
        params,
    ).fetchall()

    hits = [
        Hit(
            item_id=r["id"],
            kind=r["kind"],
            title=r["title"],
            uri=r["uri"],
            source=r["source_id"],
            modified_at=r["modified_at"],
            trust=r["trust"],
            risk_score=r["risk_score"],
            tier=r["tier"],
            lang=r["lang"],
            snippet=snippets.get(r["id"]) or (r["head"] or "")[:200],
            score=scores[r["id"]],
        )
        for r in rows
    ]
    hits.sort(key=lambda h: h.score, reverse=True)
    return hits[:limit]
