"""
build_digest.py — Assemble the raw digest (pre-summary).

Combines:
  - Curated entries: RSS + following items (NOT ranked; passed through)
  - Hot topics: ranked output from rank.py (keyword stream)

Produces digest_raw.json with platform-NEUTRAL fields and a stable numeric id
per entry. The agent layer (SKILL.md + delegate_task) then fills `summary` and
`angle` for each entry to produce digest_final.{json,md}.

The schema here is the contract every downstream platform skill reads.
"""
import os
import json
import argparse
import datetime as dt

from common import load_config, resolve_path


def curated_entries(items):
    """RSS + following items -> neutral digest entries (no ranking)."""
    out = []
    for it in items:
        if it.get("stream") not in ("rss", "following"):
            continue
        out.append({
            "dedup_key": it["dedup_key"],   # so agent can mark-summarized later
            "title": it["title"],
            "url": it["url"],
            "source": it["source"],
            "stream": it["stream"],
            "published": it.get("published"),
            "raw_summary": it.get("summary", ""),
            "author": it.get("author"),
            # filled by agent layer:
            "summary": None,
            "angle": None,
        })
    return out


def hot_entries(topics):
    """Ranked topics -> neutral digest entries with hotness metadata."""
    out = []
    for t in topics:
        rep = t["representative"]
        out.append({
            "dedup_key": rep.get("dedup_key"),  # agent uses this to mark-summarized
            "title": rep["title"],
            "url": rep["url"],
            "source": "X (hot topic)",
            "stream": "keywords",
            "topic_signal": t["topic_signal"],
            "raw_summary": rep["summary"],
            "author": rep.get("author"),
            "hotness": {
                "unique_authors": t["unique_authors"],
                "total_engagement": t["total_engagement"],
                "tweet_count": t["tweet_count"],
                "score": t["score"],
            },
            # filled by agent layer:
            "summary": None,
            "angle": None,
        })
    return out


def build(cfg, curated_items, ranked_topics, reports=None):
    """Build digest. reports can be [{'stream': str, 'ok': bool, 'error': str, ...}]."""
    entries = []
    entries.extend(hot_entries(ranked_topics))      # hot topics first
    entries.extend(curated_entries(curated_items))  # then curated sources

    # Stable numbering so the user can say "do #7".
    for i, e in enumerate(entries):
        e["id"] = i

    # Collect failure notes from reports for Step 6 display
    notes = []
    if reports:
        for r in reports:
            stream = r.get("stream") or r.get("source", "?")
            if not r.get("ok", True):
                notes.append(f"注: {stream} 本次未抓取成功 ({r.get('error', 'unknown')})")
            elif r.get("item_count", r.get("count", 0)) == 0 and r.get("ok"):
                if "following" in stream.lower():
                    notes.append("注: X following 流在时间窗内无推文")
                # keyword stream with 0 items is normal when disabled

    return {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "date": dt.date.today().isoformat(),
        "entry_count": len(entries),
        "notes": notes,
        "entries": entries,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=None)
    ap.add_argument("--curated", required=True,
                    help="JSON {'items':[...]} of RSS+following (post-dedup)")
    ap.add_argument("--topics", required=True,
                    help="JSON {'topics':[...]} from rank.py")
    ap.add_argument("--reports", default=None,
                    help="Comma-separated JSON files with fetch reports")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    cfg = load_config(args.config)
    with open(args.curated, "r", encoding="utf-8") as f:
        curated = json.load(f).get("items", [])
    with open(args.topics, "r", encoding="utf-8") as f:
        topics = json.load(f).get("topics", [])

    reports = []
    if args.reports:
        for path in args.reports.split(","):
            path = path.strip()
            if path and os.path.exists(path):
                data = json.load(open(path, "r", encoding="utf-8"))
                reports.extend(data.get("report", []))

    digest = build(cfg, curated, topics, reports=reports)

    out = args.out or os.path.join(
        resolve_path(cfg["output"]["digest_dir"]),
        cfg["output"]["raw_filename"],
    )
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(digest, f, ensure_ascii=False, indent=2)
    print(f"digest: {digest['entry_count']} entries -> {out}")
    if notes := digest.get("notes"):
        for n in notes:
            print(f"  {n}")


if __name__ == "__main__":
    main()
