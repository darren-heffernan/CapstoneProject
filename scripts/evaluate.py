"""Retrieval evaluation.

Measures how good the pgvector similarity search is at bringing back *relevant*
historical maintenance cases (see ``docs/report.md`` §9 and
``docs/evaluation.md``).
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
from pathlib import Path
from statistics import mean

import psycopg
from dotenv import load_dotenv
from pgvector.psycopg import register_vector
from psycopg.rows import dict_row

# Make ``app`` importable when run directly (same trick as scripts/ingest.py).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.embeddings import embed_texts  # noqa: E402

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LABELLED_PATH = REPO_ROOT / "docs" / "eval" / "labelled_queries.json"
DEFAULT_KS = [1, 3, 5, 10]


# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #
def _connect() -> psycopg.Connection:
    conn = psycopg.connect(
        host=os.getenv("POSTGRES_HOST", "localhost"),
        port=os.getenv("POSTGRES_PORT", "5432"),
        user=os.getenv("POSTGRES_USER", "kbox"),
        password=os.getenv("POSTGRES_PASSWORD", "kbox"),
        dbname=os.getenv("POSTGRES_DB", "knowledgebox"),
    )
    register_vector(conn)
    return conn


def _corpus_size(conn: psycopg.Connection) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM maintenance_records")
        return cur.fetchone()[0]


def _retrieve(
    conn: psycopg.Connection,
    query_vector: list[float],
    k: int,
    exclude_hash: str | None = None,
) -> list[dict]:
    """Top-k nearest cases by cosine distance (mirrors app/main.py's query)."""
    where = "WHERE row_hash <> %s" if exclude_hash is not None else ""
    sql = f"""
        SELECT row_hash, category, fault_description, remedial_action,
               embedding <=> %s::vector AS distance
        FROM maintenance_records
        {where}
        ORDER BY distance
        LIMIT %s
    """
    params: list = [query_vector]
    if exclude_hash is not None:
        params.append(exclude_hash)
    params.append(k)
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def _count_relevant_in_corpus(conn: psycopg.Connection, spec: dict) -> int:
    """How many rows in the whole corpus satisfy this query's relevance spec.
    """
    clauses: list[str] = []
    params: list = []

    category = spec.get("category")
    if category:
        clauses.append("lower(category) = lower(%s)")
        params.append(category)

    keywords = spec.get("relevant_any_keywords") or []
    if keywords:
        kw_clauses = []
        for kw in keywords:
            kw_clauses.append("(fault_description ILIKE %s OR remedial_action ILIKE %s)")
            params.extend([f"%{kw}%", f"%{kw}%"])
        clauses.append("(" + " OR ".join(kw_clauses) + ")")

    if not clauses:
        return 0

    sql = "SELECT count(*) FROM maintenance_records WHERE " + " AND ".join(clauses)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()[0]


# --------------------------------------------------------------------------- #
# Relevance + metrics
# --------------------------------------------------------------------------- #
def _is_relevant(row: dict, spec: dict) -> bool:
    """A retrieved row is relevant if it matches the category (when given) AND
    contains at least one of the keywords (when given)."""
    category = spec.get("category")
    if category and (row.get("category") or "").strip().lower() != category.strip().lower():
        return False

    keywords = spec.get("relevant_any_keywords") or []
    if keywords:
        haystack = f"{row.get('fault_description') or ''} {row.get('remedial_action') or ''}".lower()
        if not any(kw.lower() in haystack for kw in keywords):
            return False

    return bool(category or keywords)


def _metrics_for_query(flags: list[bool], ks: list[int], total_relevant: int) -> dict:
    """Per-query precision/recall/success at each k, plus reciprocal rank."""
    first_rel_rank = next((i for i, rel in enumerate(flags, start=1) if rel), None)

    out: dict = {}
    for k in ks:
        hits = sum(flags[:k])
        out[k] = {
            "precision": hits / k,
            "recall": (hits / total_relevant) if total_relevant > 0 else math.nan,
            "success": 1.0 if hits > 0 else 0.0,
        }
    out["mrr"] = (1.0 / first_rel_rank) if first_rel_rank else 0.0
    return out


def _aggregate(per_query: list[dict], ks: list[int]) -> dict:
    agg: dict = {"n_queries": len(per_query)}
    for k in ks:
        precisions = [q["metrics"][k]["precision"] for q in per_query]
        recalls = [
            q["metrics"][k]["recall"]
            for q in per_query
            if not math.isnan(q["metrics"][k]["recall"])
        ]
        successes = [q["metrics"][k]["success"] for q in per_query]
        agg[k] = {
            "precision": mean(precisions) if precisions else math.nan,
            "recall": mean(recalls) if recalls else math.nan,
            "success": mean(successes) if successes else math.nan,
            "recall_n": len(recalls),
        }
    mrrs = [q["metrics"]["mrr"] for q in per_query]
    agg["mrr"] = mean(mrrs) if mrrs else math.nan
    return agg


# --------------------------------------------------------------------------- #
# Modes
# --------------------------------------------------------------------------- #
def _retrieved_distances(ranked: list[dict], flags: list[bool]) -> list[dict]:
    """Pair each retrieved row's cosine distance with whether it was relevant."""
    return [
        {"distance": float(row["distance"]), "relevant": bool(flag)}
        for row, flag in zip(ranked, flags)
    ]


def run_labelled(
    conn: psycopg.Connection, path: Path, ks: list[int], collect_distances: bool = False
) -> list[dict]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    queries = raw["queries"] if isinstance(raw, dict) else raw

    specs = []
    for item in queries:
        if not item.get("category") and not item.get("relevant_any_keywords"):
            logger.warning("Skipping query with no relevance criteria: %r", item.get("query"))
            continue
        specs.append(item)

    if not specs:
        raise SystemExit("No usable labelled queries found.")

    vectors = embed_texts([s["query"] for s in specs])
    maxk = max(ks)

    per_query = []
    for spec, vector in zip(specs, vectors):
        total_relevant = _count_relevant_in_corpus(conn, spec)
        if total_relevant == 0:
            logger.warning(
                "Query %r matches 0 rows in the indexed corpus.",
                spec["query"],
            )
        ranked = _retrieve(conn, vector, maxk)
        flags = [_is_relevant(r, spec) for r in ranked]
        entry = {
            "query": spec["query"],
            "total_relevant": total_relevant,
            "metrics": _metrics_for_query(flags, ks, total_relevant),
        }
        if collect_distances:
            entry["retrieved"] = _retrieved_distances(ranked, flags)
        per_query.append(entry)
    return per_query


def run_auto(
    conn: psycopg.Connection,
    n: int,
    ks: list[int],
    seed: float | None,
    collect_distances: bool = False,
) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT category, count(*) FROM maintenance_records "
            "WHERE category IS NOT NULL GROUP BY category"
        )
        category_counts = {row[0]: row[1] for row in cur.fetchall()}

    with conn.cursor(row_factory=dict_row) as cur:
        if seed is not None:
            cur.execute("SELECT setseed(%s)", [seed])  # reproducible sample, seed in [-1, 1]
        cur.execute(
            """
            SELECT row_hash, category, fault_description
            FROM maintenance_records
            WHERE category IS NOT NULL AND fault_description <> ''
            ORDER BY random()
            LIMIT %s
            """,
            [n],
        )
        sample = cur.fetchall()

    if not sample:
        raise SystemExit("No rows with a category to sample; nothing to evaluate.")

    vectors = embed_texts([r["fault_description"] for r in sample])
    maxk = max(ks)

    per_query = []
    for row, vector in zip(sample, vectors):
        ranked = _retrieve(conn, vector, maxk, exclude_hash=row["row_hash"])
        flags = [(r["category"] == row["category"]) for r in ranked]
        total_relevant = category_counts.get(row["category"], 1) - 1  # exclude the query row itself
        entry = {
            "query": row["fault_description"],
            "category": row["category"],
            "total_relevant": total_relevant,
            "metrics": _metrics_for_query(flags, ks, total_relevant),
        }
        if collect_distances:
            entry["retrieved"] = _retrieved_distances(ranked, flags)
        per_query.append(entry)
    return per_query


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
def _print_report(mode: str, corpus_size: int, agg: dict, ks: list[int]) -> None:
    print()
    print(f"Knowledge Box — retrieval evaluation ({mode} mode)")
    print(f"Corpus: {corpus_size:,} indexed records   Queries: {agg['n_queries']}")
    print("-" * 58)
    print(f"{'k':>3}  {'Precision@k':>12}  {'Recall@k':>10}  {'Success@k':>10}")
    print("-" * 58)
    for k in ks:
        row = agg[k]
        recall = "   n/a" if math.isnan(row["recall"]) else f"{row['recall']:.3f}"
        print(f"{k:>3}  {row['precision']:>12.3f}  {recall:>10}  {row['success']:>10.3f}")
    print("-" * 58)
    print(f"MRR: {agg['mrr']:.3f}")
    if any(math.isnan(agg[k]['recall']) for k in ks):
        print("(Recall@k is n/a for queries whose relevant set could not be counted.)")
    print()


def _percentile(sorted_xs: list[float], p: float) -> float:
    """Linear-interpolated percentile (p in [0, 100]) of a pre-sorted list."""
    if not sorted_xs:
        return math.nan
    if len(sorted_xs) == 1:
        return sorted_xs[0]
    pos = (p / 100) * (len(sorted_xs) - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return sorted_xs[lo]
    return sorted_xs[lo] + (sorted_xs[hi] - sorted_xs[lo]) * (pos - lo)


def _dump_distances(per_query: list[dict], out_path: str | None) -> None:
    """Summarise the cosine-distance distribution of relevant vs non-relevant
    retrieved rows, to inform a weak-match threshold. Optionally write a CSV of
    every (distance, relevant) pair for plotting."""
    pairs = [item for q in per_query for item in q.get("retrieved", [])]
    relevant = sorted(p["distance"] for p in pairs if p["relevant"])
    non_relevant = sorted(p["distance"] for p in pairs if not p["relevant"])

    print("Distance distribution (cosine; lower = more similar)")
    print("-" * 58)
    print(f"{'group':>14}  {'n':>6}  {'p10':>6}  {'p25':>6}  {'p50':>6}  {'p75':>6}  {'p90':>6}")
    for label, xs in (("relevant", relevant), ("non-relevant", non_relevant)):
        if xs:
            print(
                f"{label:>14}  {len(xs):>6}  "
                f"{_percentile(xs, 10):>6.3f}  {_percentile(xs, 25):>6.3f}  "
                f"{_percentile(xs, 50):>6.3f}  {_percentile(xs, 75):>6.3f}  "
                f"{_percentile(xs, 90):>6.3f}"
            )
        else:
            print(f"{label:>14}  {0:>6}  {'—':>6}  {'—':>6}  {'—':>6}  {'—':>6}  {'—':>6}")
    print("-" * 58)
    print(
        "A weak-match cutoff sits where relevant distances stay below it while "
        "most non-relevant ones fall above (e.g. near the relevant p75-p90)."
    )
    print()

    if out_path and out_path != "-":
        import csv

        with open(out_path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(["distance", "relevant"])
            for p in pairs:
                writer.writerow([f"{p['distance']:.6f}", int(p["relevant"])])
        logger.info("Wrote %d (distance, relevant) pairs to %s", len(pairs), out_path)


def main() -> None:
    parser = argparse.ArgumentParser(description="Retrieval evaluation for Knowledge Box.")
    parser.add_argument(
        "--auto",
        type=int,
        metavar="N",
        help="Auto leave-one-out mode over N sampled rows (category-based relevance).",
    )
    parser.add_argument(
        "--labelled",
        type=Path,
        default=DEFAULT_LABELLED_PATH,
        help=f"Labelled query set (default: {DEFAULT_LABELLED_PATH}).",
    )
    parser.add_argument(
        "--k",
        type=int,
        nargs="+",
        default=DEFAULT_KS,
        help=f"Cut-off values for @k metrics (default: {DEFAULT_KS}).",
    )
    parser.add_argument(
        "--seed",
        type=float,
        default=None,
        help="Seed in [-1, 1] for a reproducible --auto sample.",
    )
    parser.add_argument(
        "--json-out",
        type=Path,
        default=None,
        help="Also write aggregate + per-query results to this JSON file.",
    )
    parser.add_argument(
        "--dump-distances",
        nargs="?",
        const="-",
        default=None,
        metavar="CSV",
        help="Print the relevant vs non-relevant cosine-distance distribution "
        "(to choose a weak-match threshold); optionally write every pair to CSV.",
    )
    args = parser.parse_args()

    collect_distances = args.dump_distances is not None

    ks = sorted(set(args.k))

    conn = _connect()
    try:
        corpus_size = _corpus_size(conn)
        if corpus_size == 0:
            raise SystemExit("maintenance_records is empty; run scripts/ingest.py first.")

        if args.auto is not None:
            mode = "auto leave-one-out"
            per_query = run_auto(conn, args.auto, ks, args.seed, collect_distances)
        else:
            mode = "labelled"
            if not args.labelled.exists():
                raise SystemExit(f"Labelled query file not found: {args.labelled}")
            per_query = run_labelled(conn, args.labelled, ks, collect_distances)
    except psycopg.errors.UndefinedTable as exc:
        raise SystemExit(
            "maintenance_records does not exist; run scripts/ingest.py first."
        ) from exc
    finally:
        conn.close()

    agg = _aggregate(per_query, ks)
    _print_report(mode, corpus_size, agg, ks)

    if collect_distances:
        _dump_distances(per_query, args.dump_distances)

    if args.json_out:
        payload = {
            "mode": mode,
            "corpus_size": corpus_size,
            "k": ks,
            "aggregate": {str(key): value for key, value in agg.items()},
            "per_query": per_query,
        }
        args.json_out.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        logger.info("Wrote detailed results to %s", args.json_out)


if __name__ == "__main__":
    main()
