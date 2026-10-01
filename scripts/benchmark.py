"""Local benchmark: Tier 0 scan time, graph.db size and query latency on your own data.

    uv run python scripts/benchmark.py --config ~/graph-me/config.yaml [--out /tmp/gm-bench]

It scans into a fresh, throwaway graph-out (your real one is not touched) and prints a markdown
table of numbers only: no paths, names or text from your data, so the table is safe to share.
Targets from the implementation plan: scan of 2 GB under 15 minutes, query under 300 ms,
graph.db under 20% of the source size.
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import sqlite3
import statistics
import tempfile
import time
from pathlib import Path

from graph_me import __version__, config
from graph_me.connectors.filesystem import FilesystemConnector
from graph_me.pipeline import run
from graph_me.service import Service
from graph_me.store import db

QUERIES = [
    "bail", "lease", "facture", "invoice", "anniversaire", "birthday", "contrat", "rendez-vous",
    "meeting", "passeport", "assurance", "banque", "billet", "train", "devis", "médecin",
]  # fmt: skip
TARGET_SCAN_S_PER_GB = 15 * 60 / 2
TARGET_QUERY_MS = 300
TARGET_DB_SHARE = 0.20


def source_bytes(cfg: config.Config) -> int:
    """Bytes graph-me reads: indexed files, plus the msgvault database (and its WAL)."""
    total = 0
    for name, source in cfg.sources.items():
        extra = source.model_extra or {}
        if source.type == "filesystem":
            connector = FilesystemConnector(name, source, cfg.blacklist)
            # a file's version is "mtime_ns:size"
            total += sum(int(version.rsplit(":", 1)[1]) for _, version in connector.list_ids())
        elif source.type == "msgvault":
            home = Path(extra.get("db", "~/.msgvault")).expanduser()
            for f in home.glob("msgvault.db*"):
                total += f.stat().st_size
        elif source.type == "vcard":
            for p in extra.get("paths", []):
                for f in Path(p).expanduser().rglob("*.vcf"):
                    total += f.stat().st_size
    return total


def fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def fmt_duration(seconds: float) -> str:
    return f"{seconds:.1f} s" if seconds < 60 else f"{seconds / 60:.1f} min"


def check(ok: bool) -> str:
    return "✅" if ok else "❌"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--config", type=Path, required=True)
    ap.add_argument("--out", type=Path, help="throwaway graph-out (default: a temp folder)")
    ap.add_argument("--workers", type=int, default=None)
    ap.add_argument("--repeat", type=int, default=5, help="runs per query")
    args = ap.parse_args()

    cfg = config.load_config(args.config.expanduser())
    out = args.out or Path(tempfile.mkdtemp(prefix="graph-me-bench-"))
    if (out / config.DB_NAME).exists():
        raise SystemExit(f"{out} already has a graph: pick an empty --out")
    out.mkdir(parents=True, exist_ok=True, mode=0o700)
    db_path = out / config.DB_NAME

    size = source_bytes(cfg)
    conn = db.connect(db_path)
    start = time.perf_counter()
    report = run.scan(conn, cfg, workers=args.workers)
    scan_s = time.perf_counter() - start
    counts = db.counts(conn)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    db_size = sum(f.stat().st_size for f in out.glob(config.DB_NAME + "*"))

    service = Service(cfg, db_path, "benchmark")
    timings = []
    for q in QUERIES:
        for _ in range(args.repeat):
            t = time.perf_counter()
            service.search(q)
            timings.append((time.perf_counter() - t) * 1000)
    timings.sort()
    p50 = statistics.median(timings)
    p95 = timings[int(len(timings) * 0.95) - 1]

    gb = size / 1024**3
    target_scan = TARGET_SCAN_S_PER_GB * max(gb, 0.001)
    share = db_size / size if size else 0
    sqlite_version = sqlite3.sqlite_version
    print(f"## graph-me {__version__} benchmark ({time.strftime('%Y-%m-%d')})\n")
    print(f"{platform.system()} {platform.machine()}, {os.cpu_count()} CPUs, "
          f"Python {platform.python_version()}, SQLite {sqlite_version}\n")  # fmt: skip
    print("| Measure | Value | Target | |")
    print("| --- | --- | --- | --- |")
    print(f"| Source size | {fmt_bytes(size)} | | |")
    print(f"| Items | {counts['items']} ({counts['chunks']} chunks) | | |")
    print(f"| Entities / relations / facts | {counts['entities']} / {counts['relations']} / "
          f"{counts['facts']} | | |")  # fmt: skip
    print(f"| Tier 0 scan | {fmt_duration(scan_s)} | < {fmt_duration(target_scan)} "
          f"(15 min per 2 GB) | {check(scan_s <= target_scan)} |")  # fmt: skip
    print(f"| Throughput | {size / 1024**2 / scan_s:.1f} MB/s | | |")
    print(f"| graph.db | {fmt_bytes(db_size)} ({share:.0%} of source) | < 20% | "
          f"{check(share <= TARGET_DB_SHARE)} |")  # fmt: skip
    print(f"| Query p50 | {p50:.0f} ms | < {TARGET_QUERY_MS} ms | {check(p50 < TARGET_QUERY_MS)} |")
    print(f"| Query p95 | {p95:.0f} ms | < {TARGET_QUERY_MS} ms | {check(p95 < TARGET_QUERY_MS)} |")
    skipped = [n for n, s in report.sources.items() if s.failure]
    unreadable = sum(s.parse_errors for s in report.sources.values())
    print(f"\n{len(QUERIES)} queries × {args.repeat} runs. Unreadable items: {unreadable}."
          + (f" Skipped sources: {len(skipped)}." if skipped else ""))  # fmt: skip
    if args.out is None:
        shutil.rmtree(out)


if __name__ == "__main__":
    main()
