#!/usr/bin/env python3
"""
Dealflow engine — starter version.

What it does:
1. Pulls a YC batch (or batches) from the free yc-oss/api mirror.
2. Diffs against the previous snapshot (if one exists) to find NEW companies only.
3. Scores each company against your thesis library using keyword overlap
   (fast, free, zero API calls) as a first-pass filter.
4. Optionally re-scores the top matches with the Claude API for a nuanced
   1-5 fit score + one-line rationale per thesis (needs ANTHROPIC_API_KEY).
5. Writes results to a CSV you can import into Airtable/Google Sheets, or
   posts a digest straight to Slack.

Run:
    python3 dealflow_engine.py --batch summer-2026
    python3 dealflow_engine.py --batch summer-2026 --llm-rescore --top-k 20
    python3 dealflow_engine.py --batch summer-2026 --slack-webhook https://hooks.slack.com/...

Schedule it daily via GitHub Actions (see README.md) and it becomes a live
dealflow feed with zero manual work.
"""

import argparse
import json
import os
import re
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)

# Two mirrors of the same daily-synced dataset:
#   - GitHub Pages (yc-oss.github.io) is the documented endpoint, but some
#     network setups block GitHub Pages while allowing raw.githubusercontent.com.
#   - raw.githubusercontent.com serves the same JSON files directly from the repo.
YC_OSS_BASE = "https://raw.githubusercontent.com/yc-oss/api/main"


def fetch_json(url: str) -> list | dict:
    req = urllib.request.Request(url, headers={"User-Agent": "dealflow-engine/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_batch(batch_slug: str) -> list:
    """batch_slug like 'summer-2026' -> list of company dicts."""
    url = f"{YC_OSS_BASE}/batches/{batch_slug}.json"
    return fetch_json(url)


def load_theses(path: Path) -> list:
    return json.loads(path.read_text())["theses"]


def keyword_score(company: dict, thesis: dict) -> tuple[int, list[str]]:
    """Cheap first-pass score: count keyword hits in name/one_liner/description/tags."""
    haystack = " ".join([
        company.get("name", ""),
        company.get("one_liner", ""),
        company.get("long_description", "") or "",
        " ".join(company.get("tags", []) or []),
        " ".join(company.get("industries", []) or []),
    ]).lower()

    hits = []
    for kw in thesis["keywords"]:
        if kw.lower() in haystack:
            hits.append(kw)
    return len(hits), hits


def score_company(company: dict, theses: list) -> list[dict]:
    """Return a list of {thesis_id, thesis_name, score, matched_keywords} for every thesis with >0 hits."""
    matches = []
    for thesis in theses:
        score, hits = keyword_score(company, thesis)
        if score > 0:
            matches.append({
                "thesis_id": thesis["id"],
                "thesis_name": thesis["name"],
                "keyword_score": score,
                "matched_keywords": hits,
            })
    matches.sort(key=lambda m: -m["keyword_score"])
    return matches


def diff_against_snapshot(batch_slug: str, companies: list) -> list:
    """Compare against the last saved snapshot for this batch; return only new companies."""
    snapshot_path = DATA_DIR / f"{batch_slug}.snapshot.json"
    seen_ids = set()
    if snapshot_path.exists():
        prev = json.loads(snapshot_path.read_text())
        seen_ids = {c["id"] for c in prev}

    new_companies = [c for c in companies if c["id"] not in seen_ids]

    # save current snapshot for next run's diff
    snapshot_path.write_text(json.dumps(companies))
    return new_companies


def llm_rescore(company: dict, theses: list, top_thesis_ids: list[str]) -> dict | None:
    """
    Optional: ask Claude to score fit 1-5 with a one-line rationale for the
    top keyword-matched theses. Requires ANTHROPIC_API_KEY env var.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None

    relevant = [t for t in theses if t["id"] in top_thesis_ids]
    thesis_block = "\n".join(f"- {t['id']}: {t['name']} — {t['description']}" for t in relevant)

    prompt = f"""Company: {company.get('name')}
One-liner: {company.get('one_liner')}
Description: {(company.get('long_description') or '')[:600]}

Score this company's fit (1-5, 5=strong fit) against each thesis below.
Respond ONLY with JSON: a list of objects with keys thesis_id, score, rationale (one sentence).

Theses:
{thesis_block}
"""

    body = json.dumps({
        "model": "claude-sonnet-4-6",
        "max_tokens": 500,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        text = data["content"][0]["text"]
        text = re.sub(r"^```json|```$", "", text.strip(), flags=re.MULTILINE).strip()
        return json.loads(text)
    except Exception as e:
        print(f"  [llm_rescore failed for {company.get('name')}: {e}]")
        return None


def write_html_feed(all_ranked: dict, theses: list, out_path: Path):
    """
    all_ranked: {batch_slug: [ranked company dicts]} across one or more batches.
    Writes a single static HTML page, grouped by thesis, newest matches first.
    This is what gets committed to docs/ and served via GitHub Pages.
    """
    thesis_names = {t["id"]: t["name"] for t in theses}
    by_thesis: dict[str, list] = {tid: [] for tid in thesis_names}

    for batch_slug, ranked in all_ranked.items():
        for r in ranked:
            if not r["matches"]:
                continue
            top = r["matches"][0]
            entry = {**r, "batch": batch_slug, "top": top}
            by_thesis.setdefault(top["thesis_id"], []).append(entry)

    for tid in by_thesis:
        by_thesis[tid].sort(key=lambda e: -e["top"]["keyword_score"])

    updated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    total = sum(len(v) for v in by_thesis.values())

    sections = []
    for tid, entries in by_thesis.items():
        if not entries:
            continue
        name = thesis_names.get(tid, tid)
        rows = []
        for e in entries[:50]:
            llm = None
            if "llm_scores" in e and e["llm_scores"]:
                llm = next((x for x in e["llm_scores"] if x["thesis_id"] == tid), None)
            llm_html = f'<div class="rationale">{llm["rationale"]} <span class="llmscore">LLM fit: {llm["score"]}/5</span></div>' if llm else ""
            rows.append(f"""
              <div class="card">
                <div class="card-head">
                  <a href="{e['url']}" target="_blank">{e['name']}</a>
                  <span class="batch">{e['batch']}</span>
                </div>
                <div class="oneliner">{e['one_liner']}</div>
                <div class="keywords">matched: {', '.join(e['top']['matched_keywords'])}</div>
                {llm_html}
              </div>""")
        sections.append(f"""
        <section>
          <h2>{name} <span class="count">{len(entries)}</span></h2>
          {''.join(rows)}
        </section>""")

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Dealflow feed</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  body {{ font-family: -apple-system, sans-serif; max-width: 760px; margin: 0 auto; padding: 24px 16px 80px;
          background: #fff; color: #222; }}
  h1 {{ font-size: 22px; margin-bottom: 4px; }}
  .meta {{ color: #777; font-size: 13px; margin-bottom: 28px; }}
  h2 {{ font-size: 16px; margin-top: 36px; border-bottom: 1px solid #eee; padding-bottom: 8px; }}
  .count {{ color: #999; font-weight: 400; font-size: 13px; }}
  .card {{ padding: 12px 0; border-bottom: 1px solid #f2f2f2; }}
  .card-head a {{ font-weight: 600; text-decoration: none; color: #1a1a1a; font-size: 15px; }}
  .card-head a:hover {{ text-decoration: underline; }}
  .batch {{ float: right; color: #aaa; font-size: 12px; }}
  .oneliner {{ color: #444; font-size: 14px; margin-top: 2px; }}
  .keywords {{ color: #999; font-size: 12px; margin-top: 4px; }}
  .rationale {{ color: #555; font-size: 13px; margin-top: 6px; font-style: italic; }}
  .llmscore {{ color: #0a7; font-style: normal; font-weight: 600; margin-left: 6px; }}
</style>
</head>
<body>
  <h1>Dealflow feed</h1>
  <div class="meta">{total} matches &middot; updated {updated} &middot; auto-refreshes daily</div>
  {''.join(sections)}
</body>
</html>"""
    out_path.write_text(html)


def post_slack_digest(webhook_url: str, ranked: list, batch_slug: str):
    lines = [f"*Dealflow digest — {batch_slug}* ({datetime.now(timezone.utc):%Y-%m-%d})"]
    if not ranked:
        lines.append("No new thesis-matching companies today.")
    for row in ranked[:15]:
        top = row["matches"][0] if row["matches"] else None
        thesis_str = f"{top['thesis_name']} (score {top['keyword_score']})" if top else "no match"
        lines.append(f"• *{row['name']}* — {row['one_liner']}  _[{thesis_str}]_  {row['url']}")

    payload = json.dumps({"text": "\n".join(lines)}).encode("utf-8")
    req = urllib.request.Request(webhook_url, data=payload, headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=15)


def process_batch(batch_slug: str, theses: list, include_existing: bool,
                   llm_rescore_flag: bool, top_k: int) -> list:
    print(f"Fetching batch {batch_slug}...")
    companies = fetch_batch(batch_slug)
    print(f"  {len(companies)} companies in batch")

    target = companies if include_existing else diff_against_snapshot(batch_slug, companies)
    if not include_existing:
        print(f"  {len(target)} new since last run")

    ranked = []
    for c in target:
        matches = score_company(c, theses)
        ranked.append({
            "id": c["id"],
            "name": c["name"],
            "one_liner": c.get("one_liner", ""),
            "url": c.get("url", ""),
            "batch": c.get("batch", ""),
            "matches": matches,
        })

    ranked.sort(key=lambda r: -(r["matches"][0]["keyword_score"] if r["matches"] else 0))
    ranked = [r for r in ranked if r["matches"]]

    if llm_rescore_flag:
        for r in ranked[:top_k]:
            top_ids = [m["thesis_id"] for m in r["matches"][:3]]
            llm_result = llm_rescore(r, theses, top_ids)
            if llm_result:
                r["llm_scores"] = llm_result

    return ranked


def write_csv(ranked: list, out_path: Path):
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("name,one_liner,top_thesis,keyword_score,matched_keywords,llm_score,llm_rationale,url\n")
        for r in ranked:
            top = r["matches"][0] if r["matches"] else {}
            llm = None
            if "llm_scores" in r and r["llm_scores"]:
                llm = next((x for x in r["llm_scores"] if x["thesis_id"] == top.get("thesis_id")), None)
            row = [
                r["name"],
                r["one_liner"].replace(",", ";"),
                top.get("thesis_name", ""),
                str(top.get("keyword_score", "")),
                "|".join(top.get("matched_keywords", [])),
                str(llm["score"]) if llm else "",
                llm["rationale"].replace(",", ";") if llm else "",
                r["url"],
            ]
            f.write(",".join(row) + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", required=True,
                     help="Comma-separated batch slugs, e.g. summer-2026,fall-2026")
    ap.add_argument("--theses", default=str(Path(__file__).parent / "theses.json"))
    ap.add_argument("--out", default=None, help="CSV output path (single-batch mode only)")
    ap.add_argument("--html-out", default=None, help="Write a static HTML feed page to this path")
    ap.add_argument("--llm-rescore", action="store_true", help="Re-score top matches with Claude API")
    ap.add_argument("--top-k", type=int, default=20, help="How many top matches to LLM-rescore, per batch")
    ap.add_argument("--slack-webhook", default=None)
    ap.add_argument("--include-existing", action="store_true",
                     help="Score the full batch(es), not just new-since-last-run companies")
    args = ap.parse_args()

    theses = load_theses(Path(args.theses))
    batch_slugs = [b.strip() for b in args.batch.split(",") if b.strip()]

    all_ranked = {}
    for slug in batch_slugs:
        all_ranked[slug] = process_batch(
            slug, theses, args.include_existing, args.llm_rescore, args.top_k
        )

    # CSV: combined across all requested batches
    combined = [r for ranked in all_ranked.values() for r in ranked]
    out_path = Path(args.out) if args.out else Path(__file__).parent / "dealflow_latest.csv"
    write_csv(combined, out_path)
    print(f"Wrote {len(combined)} scored companies to {out_path}")

    if args.html_out:
        write_html_feed(all_ranked, theses, Path(args.html_out))
        print(f"Wrote HTML feed to {args.html_out}")

    if args.slack_webhook:
        for slug, ranked in all_ranked.items():
            post_slack_digest(args.slack_webhook, ranked, slug)
        print("Posted Slack digest.")


if __name__ == "__main__":
    main()
