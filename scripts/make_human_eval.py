"""Generate a human relevance-rating form for Knowledge Box retrieval.

This is the human-judged relevance study described in docs/report.md §9.5 and
docs/eval/human_eval_protocol.md.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import sys
from pathlib import Path

import psycopg
from dotenv import load_dotenv
from pgvector.psycopg import register_vector
from psycopg.rows import dict_row

# Make ``app`` importable when run directly (same trick as scripts/ingest.py).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.embeddings import embed_texts  # noqa: E402

load_dotenv()

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = REPO_ROOT / "docs" / "eval" / "human_eval_form.html"


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


def _sample_queries(conn: psycopg.Connection, n: int, seed: float | None) -> list[dict]:
    with conn.cursor(row_factory=dict_row) as cur:
        if seed is not None:
            cur.execute("SELECT setseed(%s)", [seed])
        cur.execute(
            """
            SELECT row_hash, fault_description
            FROM maintenance_records
            WHERE fault_description IS NOT NULL AND fault_description <> ''
            ORDER BY random()
            LIMIT %s
            """,
            [n],
        )
        return cur.fetchall()


def _retrieve(conn: psycopg.Connection, vector: list[float], k: int, exclude_hash: str) -> list[dict]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT category, fault_description, remedial_action,
                   embedding <=> %s::vector AS distance
            FROM maintenance_records
            WHERE row_hash <> %s
            ORDER BY distance
            LIMIT %s
            """,
            [vector, exclude_hash, k],
        )
        return cur.fetchall()


def build_form_html(items: list[dict], k: int) -> str:
    """Pure HTML builder — ``items`` is a list of {query, cases:[{...}]} dicts.

    Kept free of DB/model dependencies so it can be unit-tested directly.
    """
    data_json = json.dumps(items).replace("<", "\\u003c")

    blocks = []
    for qi, item in enumerate(items):
        rows = []
        for ci, case in enumerate(item["cases"]):
            pct = max(0, round((1 - case["distance"]) * 100))
            rows.append(
                f"""
        <tr>
          <td class="rank">{ci + 1}</td>
          <td>
            <div class="fault">{html.escape(case['fault_description'] or '')}</div>
            <div class="fix">{html.escape(case['remedial_action'] or '')}</div>
            <div class="meta">{html.escape(case['category'] or '—')} · {pct}% similarity</div>
          </td>
          <td class="rate">
            <label><input type="radio" name="q{qi}c{ci}" value="1"> useful</label>
            <label><input type="radio" name="q{qi}c{ci}" value="0"> not</label>
          </td>
        </tr>"""
            )
        blocks.append(
            f"""
    <section class="query" data-qi="{qi}">
      <h2>Query {qi + 1} of {len(items)}</h2>
      <p class="qtext">{html.escape(item['query'] or '')}</p>
      <table>
        <thead><tr><th>#</th><th>Retrieved historical case</th><th>Useful?</th></tr></thead>
        <tbody>{''.join(rows)}</tbody>
      </table>
      <label class="notes">Notes (optional): <input type="text" data-notes="{qi}"></label>
    </section>"""
        )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Knowledge Box — retrieval relevance rating</title>
<style>
  body {{ font-family: system-ui, sans-serif; max-width: 900px; margin: 1.5rem auto; padding: 0 1rem; line-height: 1.4; }}
  h1 {{ font-size: 1.4rem; }}
  .intro {{ background: #f4f6f8; border: 1px solid #d9dee3; padding: 1rem; border-radius: 6px; }}
  section.query {{ border-top: 2px solid #d9dee3; margin-top: 1.5rem; padding-top: 1rem; }}
  .qtext {{ font-weight: 600; font-size: 1.05rem; background: #eef3ff; padding: .5rem .75rem; border-radius: 4px; }}
  table {{ width: 100%; border-collapse: collapse; margin: .5rem 0; }}
  th, td {{ text-align: left; padding: .4rem .5rem; border-bottom: 1px solid #eceff2; vertical-align: top; }}
  td.rank, th:first-child {{ width: 2rem; color: #667; }}
  td.rate {{ white-space: nowrap; width: 8rem; }}
  .fault {{ font-weight: 600; }}
  .fix {{ color: #334; }}
  .meta {{ color: #778; font-size: .85rem; margin-top: .15rem; }}
  .notes {{ display: block; margin-top: .5rem; color: #556; }}
  .notes input {{ width: 60%; }}
  .bar {{ position: sticky; bottom: 0; background: #fff; border-top: 2px solid #d9dee3; padding: .75rem 0; margin-top: 1.5rem; display: flex; gap: 1rem; align-items: center; }}
  button {{ font-size: 1rem; padding: .5rem 1rem; cursor: pointer; }}
  #tally {{ color: #445; }}
</style>
</head>
<body>
<h1>Knowledge Box — retrieval relevance rating</h1>
<div class="intro">
  <p>For each fault (the <strong>query</strong>), mark whether each retrieved
  historical case is a <strong>useful</strong> match — i.e. something you'd
  reasonably look at when diagnosing that fault. Judge relevance of the case to
  the query, not whether the remedial action is perfect.</p>
  <p>When done, click <strong>Download ratings (CSV)</strong> and send the file back.
  Nothing is uploaded anywhere; it all stays in this page until you download.</p>
</div>
{''.join(blocks)}
<div class="bar">
  <button id="download">Download ratings (CSV)</button>
  <span id="tally"></span>
</div>
<script>
const DATA = JSON.parse({json.dumps(data_json)});
function collect() {{
  const rows = [["query_id","query","rank","case_fault","distance","useful","notes"]];
  let rated = 0, usefulCases = 0, queriesWithHit = 0, total = 0;
  DATA.forEach((item, qi) => {{
    const notes = (document.querySelector(`[data-notes="${{qi}}"]`)||{{}}).value || "";
    let hit = false;
    item.cases.forEach((c, ci) => {{
      total++;
      const sel = document.querySelector(`input[name="q${{qi}}c${{ci}}"]:checked`);
      const useful = sel ? sel.value : "";
      if (useful !== "") rated++;
      if (useful === "1") {{ usefulCases++; hit = true; }}
      rows.push([qi+1, item.query, ci+1, c.fault_description, c.distance, useful, ci===0?notes:""]);
    }});
    if (hit) queriesWithHit++;
  }});
  return {{rows, rated, usefulCases, queriesWithHit, total}};
}}
function refresh() {{
  const {{rated, usefulCases, queriesWithHit, total}} = collect();
  const nq = DATA.length;
  const rate = total ? (usefulCases/total*100).toFixed(0) : 0;
  const succ = nq ? (queriesWithHit/nq*100).toFixed(0) : 0;
  document.getElementById("tally").textContent =
    `${{rated}}/${{total}} cases rated · usefulness ${{rate}}% · success@k ${{succ}}% (${{queriesWithHit}}/${{nq}} queries with a useful hit)`;
}}
document.addEventListener("change", refresh);
document.getElementById("download").addEventListener("click", () => {{
  const {{rows}} = collect();
  const csv = rows.map(r => r.map(v => `"${{String(v).replace(/"/g,'""')}}"`).join(",")).join("\\n");
  const blob = new Blob([csv], {{type: "text/csv"}});
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "human_eval_ratings.csv";
  a.click();
}});
refresh();
</script>
</body>
</html>
"""


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a human retrieval-rating form.")
    parser.add_argument("--n", type=int, default=20, help="Number of query faults to sample (default 20).")
    parser.add_argument("--k", type=int, default=5, help="Retrieved cases per query (default 5).")
    parser.add_argument("--seed", type=float, default=None, help="Seed in [-1, 1] for a reproducible sample.")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help=f"Output HTML path (default {DEFAULT_OUT}).")
    args = parser.parse_args()

    conn = _connect()
    try:
        sample = _sample_queries(conn, args.n, args.seed)
        if not sample:
            raise SystemExit("No faults to sample; run scripts/ingest.py first.")
        vectors = embed_texts([r["fault_description"] for r in sample])
        items = []
        for row, vector in zip(sample, vectors):
            cases = _retrieve(conn, vector, args.k, row["row_hash"])
            items.append(
                {
                    "query": row["fault_description"],
                    "cases": [
                        {
                            "category": c["category"],
                            "fault_description": c["fault_description"],
                            "remedial_action": c["remedial_action"],
                            "distance": float(c["distance"]),
                        }
                        for c in cases
                    ],
                }
            )
    finally:
        conn.close()

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(build_form_html(items, args.k), encoding="utf-8")
    print(f"Wrote {len(items)} queries × {args.k} cases to {args.out}")
    print("Open it in a browser, rate each case, then Download ratings (CSV).")


if __name__ == "__main__":
    main()
