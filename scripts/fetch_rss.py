"""
fetch_rss.py — Fetch the curation streams (RSS sources + Hacker News).

Design: handlers are keyed by source TYPE, not by website. Adding a standard
RSS source = one config entry, zero code. Adding a new TYPE = one handler func.

Each handler returns a list of normalized items (see common.make_item).
Network errors are caught per-source so one dead feed never breaks the run;
the failure is reported in the result so connectivity tests can surface it.
"""
import sys
import json
import argparse

import requests

from common import load_config, make_item, strip_html

try:
    import feedparser
except ImportError:
    feedparser = None

USER_AGENT = "daily-tech-news-skill/1.0 (+rss-reader)"


# --- Handlers -----------------------------------------------------------------

def handle_standard_rss(src):
    """Generic handler for any standard RSS/Atom feed."""
    if feedparser is None:
        raise RuntimeError("feedparser not installed")
    # feedparser can fetch URLs itself, but we use requests for a UA + timeout,
    # then hand the bytes to feedparser. More predictable error handling.
    resp = requests.get(src["url"], headers={"User-Agent": USER_AGENT}, timeout=20)
    resp.raise_for_status()
    parsed = feedparser.parse(resp.content)
    items = []
    for e in parsed.entries:
        published = e.get("published") or e.get("updated") or None
        summary = e.get("summary") or e.get("description") or ""
        items.append(make_item(
            source=src["name"],
            stream="rss",
            title=e.get("title", ""),
            url=e.get("link", ""),
            published=published,
            summary=summary,
        ))
    return items


def handle_hn_algolia(src):
    """Hacker News via Algolia Search API. Carries `points` as a real signal."""
    base = "https://hn.algolia.com/api/v1/search_by_date"
    params = {
        "query": src.get("query", ""),
        "tags": "story",
        "numericFilters": f"points>{src.get('points_threshold', 50)}",
        "hitsPerPage": src.get("max_items", 30),
    }
    resp = requests.get(base, params=params, headers={"User-Agent": USER_AGENT}, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    items = []
    for h in data.get("hits", []):
        url = h.get("url") or f"https://news.ycombinator.com/item?id={h.get('objectID')}"
        items.append(make_item(
            source=src["name"],
            stream="rss",
            title=h.get("title", ""),
            url=url,
            published=h.get("created_at"),
            summary=strip_html(h.get("story_text") or ""),
            metrics={
                "points": h.get("points", 0),
                "num_comments": h.get("num_comments", 0),
            },
        ))
    return items


HANDLERS = {
    "rss": handle_standard_rss,
    "algolia": handle_hn_algolia,
}


# --- Orchestration ------------------------------------------------------------

def fetch_all(config, only_source=None):
    """
    Returns (items, report). `report` lists per-source status so callers /
    tests can see exactly which feeds worked.
    """
    items = []
    report = []
    for src in config.get("rss_sources", []):
        if not src.get("enabled", True):
            continue
        if only_source and src["name"] != only_source:
            continue
        handler = HANDLERS.get(src.get("type"))
        if handler is None:
            report.append({"source": src["name"], "ok": False,
                           "error": f"unknown type: {src.get('type')}", "count": 0})
            continue
        try:
            got = handler(src)
            items.extend(got)
            report.append({"source": src["name"], "ok": True, "count": len(got),
                           "sample_title": got[0]["title"] if got else None})
        except Exception as ex:  # noqa: BLE001 - we want to keep going
            report.append({"source": src["name"], "ok": False,
                           "error": f"{type(ex).__name__}: {ex}", "count": 0})
    return items, report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=None)
    ap.add_argument("--out", default=None, help="write items JSON to this path")
    ap.add_argument("--only", default=None, help="fetch only this source name")
    ap.add_argument("--report-only", action="store_true",
                    help="print per-source status, not items")
    args = ap.parse_args()

    config = load_config(args.config)
    items, report = fetch_all(config, only_source=args.only)

    if args.report_only:
        for r in report:
            status = "OK " if r["ok"] else "FAIL"
            extra = r.get("sample_title") or r.get("error") or ""
            print(f"[{status}] {r['source']:<18} count={r['count']:<3} {extra}")
        ok = sum(1 for r in report if r["ok"])
        print(f"\n{ok}/{len(report)} sources reachable")
        return

    out = {"items": items, "report": report}
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        print(f"wrote {len(items)} items to {args.out}")
    else:
        json.dump(out, sys.stdout, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
