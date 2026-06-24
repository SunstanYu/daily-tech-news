"""
fetch_x.py — Fetch X/Twitter via the Apify apidojo/tweet-scraper actor.

Two streams:
  - keywords_stream : (ai OR agent) with min_faves -> candidates for hot ranking
  - following_stream: (from:a OR from:b ...) merged into ONE query (beats the
                      50-tweet-per-query minimum and saves cost)

The actual Apify HTTP call is isolated in `run_apify_actor` so tests can
monkeypatch it with mock tweet data (no network, no cost).

Token is read from the env var named in config (apify.token_env). Never stored.
"""
import os
import sys
import json
import time
import argparse

import requests

from common import load_config, make_item


def build_keyword_query(cfg):
    kws = cfg["keywords_stream"]["keywords"]
    expr = " OR ".join(kws)
    q = f"({expr})"
    min_faves = cfg["keywords_stream"].get("min_faves")
    if min_faves:
        q += f" min_faves:{min_faves}"
    lang = cfg["keywords_stream"].get("lang")
    if lang:
        q += f" lang:{lang}"
    return q


def build_following_query(cfg):
    handles = cfg["following_stream"].get("handles", [])
    if not handles:
        return None
    froms = " OR ".join(f"from:{h.lstrip('@')}" for h in handles)
    q = f"({froms})"
    lang = cfg["following_stream"].get("lang")
    if lang:
        q += f" lang:{lang}"
    return q


def run_apify_actor(actor_id, token, run_input, timeout=300):
    """
    Call Apify's run-sync-get-dataset-items endpoint. Returns a list of items.
    Isolated here so tests can replace it. Raises on HTTP / auth errors.
    """
    actor_path = actor_id.replace("/", "~")
    url = f"https://api.apify.com/v2/acts/{actor_path}/run-sync-get-dataset-items"
    resp = requests.post(
        url,
        params={"token": token},
        json=run_input,
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()


def normalize_tweet(raw, stream, source_label):
    """Map a raw apidojo tweet object into our common item shape."""
    author = (raw.get("author") or {}).get("userName")
    metrics = {
        "like": raw.get("likeCount", 0),
        "retweet": raw.get("retweetCount", 0),
        "reply": raw.get("replyCount", 0),
        "quote": raw.get("quoteCount", 0),
        "bookmark": raw.get("bookmarkCount", 0),
        "author_followers": (raw.get("author") or {}).get("followers", 0),
    }
    return make_item(
        source=source_label,
        stream=stream,
        title=(raw.get("text") or "")[:120],  # tweets have no title; use text head
        url=raw.get("url") or raw.get("twitterUrl") or "",
        published=raw.get("createdAt"),
        summary=raw.get("text") or "",
        author=author,
        metrics=metrics,
    )


def fetch_stream(cfg, stream_name, apify_call=run_apify_actor):
    """
    Fetch one stream. `apify_call` is injectable for tests.
    Returns (items, report_entry).
    """
    s = cfg[stream_name]
    if not s.get("enabled", False):
        return [], {"stream": stream_name, "ok": True, "skipped": True, "count": 0}

    if stream_name == "keywords_stream":
        query = build_keyword_query(cfg)
    elif stream_name == "following_stream":
        query = build_following_query(cfg)
        if query is None:
            return [], {"stream": stream_name, "ok": True,
                        "skipped": True, "count": 0, "note": "no handles"}
    else:
        raise ValueError(f"unknown stream {stream_name}")

    token = os.environ.get(cfg["apify"]["token_env"], "")
    run_input = {
        "searchTerms": [query],
        "sort": s.get("sort", "Latest"),
        "maxItems": s.get("max_items", 100),
    }
    if s.get("lang"):
        run_input["tweetLanguage"] = s["lang"]

    try:
        raw_items = apify_call(
            cfg["apify"]["actor_id"], token, run_input,
            timeout=cfg["apify"].get("timeout_seconds", 300),
        )
        label = "X-keywords" if stream_name == "keywords_stream" else "X-following"
        stream_tag = "keywords" if stream_name == "keywords_stream" else "following"
        items = [normalize_tweet(t, stream_tag, label)
                 for t in raw_items if t.get("type", "tweet") == "tweet"]
        return items, {"stream": stream_name, "ok": True, "count": len(items),
                       "query": query}
    except Exception as ex:  # noqa: BLE001
        return [], {"stream": stream_name, "ok": False,
                    "error": f"{type(ex).__name__}: {ex}", "query": query}


def fetch_all(cfg, apify_call=run_apify_actor):
    items, report = [], []
    for stream_name in ("keywords_stream", "following_stream"):
        got, rep = fetch_stream(cfg, stream_name, apify_call=apify_call)
        items.extend(got)
        report.append(rep)
    return items, report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--print-queries", action="store_true",
                    help="just print the queries that would be sent")
    args = ap.parse_args()

    cfg = load_config(args.config)

    if args.print_queries:
        print("keyword query :", build_keyword_query(cfg))
        print("following query:", build_following_query(cfg))
        return

    items, report = fetch_all(cfg)
    out = {"items": items, "report": report}
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        print(f"wrote {len(items)} tweets to {args.out}")
    else:
        json.dump(out, sys.stdout, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
