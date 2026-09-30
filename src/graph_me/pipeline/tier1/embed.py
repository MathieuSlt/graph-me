"""Tier 1 embeddings: meaning-based search with a local multilingual model and sqlite-vec.

- ``local`` (default): sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2 through
  fastembed (ONNX, no PyTorch). It runs on this computer; the model (~220 MB) is downloaded once
  on first use into ~/.cache/graph-me/models (GRAPH_ME_MODELS_DIR). French and English
  share one vector space: "rental contract" is close to "contrat de bail".
- any other fastembed model name.
- ``hash``: a deterministic toy embedder for tests only (no meaning, no download).

Vectors live in ``chunks_vec`` (sqlite-vec) inside graph.db. Without the extension, search
simply stays on words (FTS5).
"""

from __future__ import annotations

import hashlib
import math
import os
import sqlite3
import struct
from collections.abc import Iterable
from functools import cache
from pathlib import Path
from typing import Protocol

LOCAL_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
BATCH = 64
# Vectors are normalized, so L2 distance d relates to cosine similarity: cos = 1 - d**2 / 2.
# Short cross-language phrases score low but clearly above noise ("contrat de bail" vs
# "rental agreement": 0.34; vs "recette de crêpes": 0.03), hence cos >= 0.25.
MAX_DISTANCE = 1.22
MODELS_DIR = Path(os.environ.get("GRAPH_ME_MODELS_DIR", "~/.cache/graph-me/models")).expanduser()


class Embedder(Protocol):
    name: str
    dims: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class FastEmbedder:
    def __init__(self, model: str = LOCAL_MODEL) -> None:
        # Hugging Face's newer "xet" transfer can stall; the classic download is reliable.
        os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
        from fastembed import TextEmbedding

        self.name = model
        # A persistent cache: fastembed's default lives in /tmp and is lost on reboot.
        self._model = TextEmbedding(model_name=model, cache_dir=str(MODELS_DIR))
        self.dims = len(next(iter(self._model.embed(["dimension probe"]))))

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [_normalize(list(map(float, v))) for v in self._model.embed(texts, batch_size=BATCH)]


class HashEmbedder:
    """Testing only: words hashed into a fixed-size vector. Shares words, not meaning."""

    name = "hash"
    dims = 64

    def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for text in texts:
            v = [0.0] * self.dims
            for word in text.casefold().split():
                v[int(hashlib.md5(word.encode()).hexdigest(), 16) % self.dims] += 1.0
            out.append(_normalize(v))
        return out


def _normalize(v: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / norm for x in v]


@cache
def get(name: str) -> Embedder | None:
    """The embedder for ``extraction.medium.embeddings`` (None for "none")."""
    if name in (None, "", "none"):
        return None
    if name == "hash":
        return HashEmbedder()
    return FastEmbedder(LOCAL_MODEL if name == "local" else name)


def vec_loaded(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute("SELECT vec_version()")
        return True
    except sqlite3.OperationalError:
        return False


def has_vectors(conn: sqlite3.Connection) -> bool:
    return bool(
        conn.execute("SELECT count(*) FROM sqlite_master WHERE name = 'chunks_vec'").fetchone()[0]
    )


def _pack(v: list[float]) -> bytes:
    return struct.pack(f"{len(v)}f", *v)


def ensure_table(conn: sqlite3.Connection, embedder: Embedder) -> None:
    """Create chunks_vec for this model; rebuild it if the model changed."""
    from graph_me.store import db

    current = db.get_meta(conn, "embeddings_model")
    if has_vectors(conn) and current != f"{embedder.name}:{embedder.dims}":
        conn.execute("DROP TABLE chunks_vec")
    conn.execute(
        f"CREATE VIRTUAL TABLE IF NOT EXISTS chunks_vec "
        f"USING vec0(chunk_id INTEGER PRIMARY KEY, embedding float[{embedder.dims}])"
    )
    db.set_meta(conn, "embeddings_model", f"{embedder.name}:{embedder.dims}")
    conn.commit()


def pending_chunks(conn: sqlite3.Connection, item_filter: str = "", params: tuple = ()) -> list:
    if not has_vectors(conn):
        return conn.execute(
            "SELECT c.id, c.text FROM chunks c JOIN items i ON i.id = c.item_id "
            f"WHERE 1=1{item_filter}",
            params,
        ).fetchall()
    return conn.execute(
        f"""SELECT c.id, c.text FROM chunks c JOIN items i ON i.id = c.item_id
            WHERE c.id NOT IN (SELECT chunk_id FROM chunks_vec){item_filter}""",
        params,
    ).fetchall()


def embed_chunks(
    conn: sqlite3.Connection, embedder: Embedder, rows: Iterable, progress=None
) -> int:
    rows = list(rows)
    done = 0
    for start in range(0, len(rows), BATCH):
        batch = rows[start : start + BATCH]
        vectors = embedder.embed([r[1] for r in batch])
        conn.executemany(
            "INSERT OR REPLACE INTO chunks_vec(chunk_id, embedding) VALUES (?, ?)",
            [(r[0], _pack(v)) for r, v in zip(batch, vectors, strict=True)],
        )
        conn.commit()
        done += len(batch)
        if progress:
            progress(done, len(rows))
    return done


def nearest(conn: sqlite3.Connection, embedder: Embedder, text: str, k: int = 50) -> list:
    """(item_id, chunk_id, distance) of the chunks closest in meaning, best first."""
    if not (has_vectors(conn) and vec_loaded(conn)):
        return []
    [query] = embedder.embed([text])
    return conn.execute(
        """SELECT c.item_id, v.chunk_id, v.distance
           FROM (SELECT chunk_id, distance FROM chunks_vec
                 WHERE embedding MATCH ? AND k = ?) v
           JOIN chunks c ON c.id = v.chunk_id
           WHERE v.distance <= ?
           ORDER BY v.distance""",
        (_pack(query), k, MAX_DISTANCE),
    ).fetchall()
