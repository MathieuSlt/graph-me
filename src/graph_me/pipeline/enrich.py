"""Tier 1 enrichment: labels for short strings (files, contacts) and meaning-based vectors.

Two ways to label:

- agent mode (``llm.provider: agent``, no API key): ``enrich`` writes batch files to
  ``graph-out/work/``; the calling agent answers each in ``<batch>.out.json``; ``ingest``
  validates and merges them. Ingested batches move to ``work/done/``.
- a model (``ollama``, ``anthropic``, ``openai_compat``): ``enrich`` calls it batch by batch.

Either way only validated labels are kept (tier1/labels.py). Work is resumable: labelled items
are marked tier 1, embedded chunks have a vector, pending batch files are never duplicated.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from graph_me import llm as llm_mod
from graph_me.config import Config, LLMConfig
from graph_me.pipeline.tier1 import embed, labels
from graph_me.store import forget

WORK = "work"
DEFAULT_BATCH = 50
TOKENS_PER_WORD = 1.5  # rough, for estimates only
PROMPT_TOKENS = 450  # instructions + schema per batch
OUTPUT_TOKENS_PER_LABEL = {"files": 45, "contacts": 15}
TASKS = ("files", "contacts")


@dataclass
class Scope:
    source: str | None = None
    since: str | None = None  # item date >= (YYYY-MM-DD)
    path: str | None = None  # file path prefix

    def sql(self) -> tuple[str, tuple]:
        where, params = [], []
        if self.source:
            where.append("i.source_id = ?")
            params.append(self.source)
        if self.since:
            where.append("COALESCE(i.created_at, i.modified_at) >= ?")
            params.append(self.since)
        if self.path:
            where.append("i.uri LIKE ? ESCAPE '\\'")
            params.append(forget._like_prefix(str(Path(self.path).expanduser().resolve())))
        return "".join(f" AND {w}" for w in where), tuple(params)


@dataclass
class EnrichReport:
    provider: str
    candidates: dict[str, int] = field(default_factory=dict)
    labelled: int = 0
    rejected: list[str] = field(default_factory=list)
    stale: int = 0
    batches_written: list[Path] = field(default_factory=list)
    embedded: int = 0
    embeddings: str | None = None  # model name, or why it was skipped
    estimate: dict[str, int] = field(default_factory=dict)


def work_dir(out: Path) -> Path:
    path = out / WORK
    (path / "done").mkdir(parents=True, exist_ok=True)
    return path


def pending_batches(out: Path) -> list[Path]:
    """Batch files not ingested yet (answered or not). Never creates anything."""
    work = out / WORK
    if not work.is_dir():
        return []
    return sorted(p for p in work.glob("batch-*.json") if not p.name.endswith(".out.json"))


def _in_pending(out: Path) -> set[str]:
    ids: set[str] = set()
    for path in pending_batches(out):
        ids.update(item["id"] for item in json.loads(path.read_text())["items"])
    return ids


def _candidates(conn: sqlite3.Connection, scope: Scope, skip: set[str]) -> dict[str, list]:
    where, params = scope.sql()
    found = {
        "files": labels.file_candidates(conn, where, params),
        "contacts": labels.contact_candidates(conn, where, params),
    }
    return {task: [c for c in cs if c.item_id not in skip] for task, cs in found.items()}


def _estimate(cands: dict[str, list], batch_size: int, chunks: int) -> dict[str, int]:
    batches = sum(-(-len(cs) // batch_size) for cs in cands.values())
    words = sum(len(f"{c.text} {c.context}".split()) for cs in cands.values() for c in cs)
    return {
        "strings": sum(len(cs) for cs in cands.values()),
        "batches": batches,
        "input_tokens": int(words * TOKENS_PER_WORD + batches * PROMPT_TOKENS),
        "output_tokens": sum(OUTPUT_TOKENS_PER_LABEL[t] * len(cs) for t, cs in cands.items()),
        "chunks_to_embed": chunks,
    }


def _prompt(task: str, batch_id: str, items: list, me: str | None) -> tuple[str, str]:
    system = labels.INSTRUCTIONS[task] + " Answer only with JSON matching the schema."
    payload = {
        "batch_id": batch_id,
        "user_name": me,
        "items": [
            {"id": c.item_id, "text": c.text, "context": c.context, "trust": c.trust} for c in items
        ],
    }
    return system, json.dumps(payload, ensure_ascii=False)


def enrich(
    conn: sqlite3.Connection,
    cfg: Config,
    out: Path,
    *,
    model: llm_mod.LLM | None = None,
    provider: str | None = None,
    scope: Scope | None = None,
    batch_size: int = DEFAULT_BATCH,
    dry_run: bool = False,
    embeddings: bool = True,
    progress=None,
) -> EnrichReport:
    """Run Tier 1. ``model`` overrides the configured provider (tests, embedding)."""
    scope = scope or Scope()
    llm_cfg = cfg.extraction.medium.llm or LLMConfig()
    if provider:
        llm_cfg = llm_cfg.model_copy(update={"provider": provider})
    agent = model is None and llm_cfg.provider == "agent"
    report = EnrichReport(provider=model.name if model else llm_cfg.provider)

    cands = _candidates(conn, scope, _in_pending(out) if agent else set())
    report.candidates = {t: len(cs) for t, cs in cands.items()}
    where, params = scope.sql()
    emb_name = cfg.extraction.medium.embeddings or "local"
    to_embed = (
        embed.pending_chunks(conn, where, params) if embeddings and emb_name != "none" else []
    )
    report.estimate = _estimate(cands, batch_size, len(to_embed))
    if dry_run:
        return report

    me = labels.user_name(conn)
    if agent:
        report.batches_written = _write_batches(out, cands, batch_size, me)
    else:
        model = model or llm_mod.create(llm_cfg)
        for task in TASKS:
            items = cands[task]
            for start in range(0, len(items), batch_size):
                batch = items[start : start + batch_size]
                batch_id = f"{task}-{start // batch_size + 1:04d}"
                system, user = _prompt(task, batch_id, batch, me)
                answer = model.complete_json(system, user, labels.output_schema(task))
                good, rejected = labels.validate(task, answer, {c.item_id for c in batch})
                result = labels.apply(
                    conn, task, good, {c.item_id: c.version for c in batch}, f"llm:{model.name}"
                )
                report.labelled += result.applied
                report.stale += result.stale
                report.rejected += rejected
                if progress:
                    progress(task, min(start + batch_size, len(items)), len(items))

    if embeddings and emb_name != "none":
        report.embeddings, report.embedded = _embed(conn, emb_name, where, params, progress)
    forget.cleanup_orphans(conn)
    conn.commit()
    return report


def _embed(conn, emb_name, where, params, progress) -> tuple[str, int]:
    if not embed.vec_loaded(conn):
        return "skipped: sqlite-vec is not available in this Python (search stays on words)", 0
    try:
        embedder = embed.get(emb_name)
    except ImportError:
        return "skipped: install graph-me[medium] for embeddings", 0
    except Exception as exc:  # download or model errors must not lose the labels
        return f"skipped: could not load the embedding model ({type(exc).__name__}: {exc})", 0
    embed.ensure_table(conn, embedder)
    rows = embed.pending_chunks(conn, where, params)  # after ensure_table (may have rebuilt)
    done = embed.embed_chunks(
        conn, embedder, rows,
        progress=(lambda d, t: progress("embeddings", d, t)) if progress else None,
    )  # fmt: skip
    return embedder.name, done


def _write_batches(out: Path, cands: dict[str, list], batch_size: int, me: str | None) -> list:
    work = work_dir(out)
    existing = [
        int(p.name.split("-")[1].split(".")[0])
        for p in [*work.glob("batch-*.json"), *(work / "done").glob("batch-*.json")]
        if p.name.split("-")[1].split(".")[0].isdigit()
    ]
    n = max(existing, default=0)
    written = []
    for task in TASKS:
        items = cands[task]
        for start in range(0, len(items), batch_size):
            n += 1
            batch = items[start : start + batch_size]
            batch_id = f"{n:04d}"
            system, user = _prompt(task, batch_id, batch, me)
            path = work / f"batch-{batch_id}.json"
            path.write_text(
                json.dumps(
                    {
                        "batch_id": batch_id,
                        "task": task,
                        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
                        "instructions": system,
                        "answer_file": f"batch-{batch_id}.out.json",
                        "answer_schema": labels.output_schema(task),
                        "user_name": me,
                        "items": [
                            {
                                "id": c.item_id,
                                "version": c.version,
                                "text": c.text,
                                "context": c.context,
                                "trust": c.trust,
                            }
                            for c in batch
                        ],  # fmt: skip
                    },
                    ensure_ascii=False,
                    indent=1,
                ),
                encoding="utf-8",
            )
            written.append(path)
    return written


@dataclass
class IngestResult:
    batch: str
    applied: int = 0
    rejected: list[str] = field(default_factory=list)
    stale: int = 0


def ingest(conn: sqlite3.Connection, out: Path, answer_path: Path) -> IngestResult:
    """Validate an agent's answer file against its batch, merge it, archive both files."""
    answer_path = answer_path.resolve()
    work = work_dir(out).resolve()
    if answer_path.parent != work or not answer_path.name.endswith(".out.json"):
        raise ValueError(f"expected a batch answer in {work} (batch-NNNN.out.json)")
    batch_path = answer_path.with_name(answer_path.name.replace(".out.json", ".json"))
    if not batch_path.exists():
        raise ValueError(f"no batch file {batch_path.name} for this answer")
    batch = json.loads(batch_path.read_text())
    try:
        answer = json.loads(answer_path.read_text())
    except json.JSONDecodeError as exc:
        raise ValueError(f"{answer_path.name} is not valid JSON: {exc}") from exc
    result = IngestResult(batch=batch["batch_id"])
    if isinstance(answer, dict) and answer.get("batch_id") not in (None, batch["batch_id"]):
        raise ValueError(f"answer is for batch {answer.get('batch_id')}, not {batch['batch_id']}")
    versions = {item["id"]: item["version"] for item in batch["items"]}
    good, result.rejected = labels.validate(batch["task"], answer, set(versions))
    applied = labels.apply(conn, batch["task"], good, versions, "llm:agent")
    result.applied, result.stale = applied.applied, applied.stale
    for path in (batch_path, answer_path):
        path.rename(work / "done" / path.name)
    return result
