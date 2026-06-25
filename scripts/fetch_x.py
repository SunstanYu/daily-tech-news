"""
fetch_x.py — Fetch X/Twitter via TwitterAPI.io (advanced_search endpoint).

Current scope (this version):
  - following_stream: fetch ALL tweets from your subscribed handles within a
    configurable time window (default 24h). Handles are merged into ONE query
    via (from:a OR from:b ...) so it's a single paginated search, not one call
    per user. Billing is per returned tweet, so a small daily volume is cheap.

  - keywords_stream: present but OUT OF SCOPE for now (hot ranking deferred).
    Left wired so enabling it later is trivial; disabled by default in config.

API: GET https://api.twitterapi.io/twitter/tweet/advanced_search
Auth: header  X-API-Key: <token from env>
Query: full X advanced-search expression, e.g.
    (from:sama OR from:ylecun) since:2026-06-24_00:00:00_UTC
Pagination: response has `tweets` + `next_cursor`; loop until cursor empty.

The HTTP call is isolated in `twitterapi_search` so tests can inject mock data.
Token is read from the env var named in config (twitterapi.token_env). Never stored.
"""
import os
import sys
import json
import argparse
import datetime as dt

import requests

from common import load_config, make_item


API_BASE = "https://api.twitterapi.io"
SEARCH_PATH = "/twitter/tweet/advanced_search"


# --- query construction -------------------------------------------------------

def _since_clause(window_hours):
    """
    Build a `since:` clause for the time window. X advanced search accepts
    since:YYYY-MM-DD_HH:MM:SS_UTC for sub-day precision.
    Returns (clause_string, since_datetime_utc).
    """
    now = dt.datetime.now(dt.timezone.utc)
    since_dt = now - dt.timedelta(hours=window_hours)
    stamp = since_dt.strftime("%Y-%m-%d_%H:%M:%S_UTC")
    return f"since:{stamp}", since_dt


def build_following_query(cfg):
    """
    Returns (query_string, since_dt) or (None, None) if no handles.
    """
    s = cfg["following_stream"]
    handles = s.get("handles", [])
    if not handles:
        return None, None
    froms = " OR ".join(f"from:{h.lstrip('@')}" for h in handles)
    q = f"({froms})"
    window = s.get("window_hours", 24)
    since_clause, since_dt = _since_clause(window)
    q += f" {since_clause}"
    lang = s.get("lang")
    if lang:
        q += f" lang:{lang}"
    # exclude retweets/replies if configured (optional, default keep all)
    if s.get("exclude_retweets", False):
        q += " -filter:retweets"
    if s.get("exclude_replies", False):
        q += " -filter:replies"
    return q, since_dt


def build_keyword_query(cfg):
    """Kept for later; keyword/hot-ranking stream is out of scope now."""
    s = cfg.get("keywords_stream", {})
    kws = s.get("keywords", [])
    if not kws:
        return None, None
    q = "(" + " OR ".join(kws) + ")"
    if s.get("min_faves"):
        q += f" min_faves:{s['min_faves']}"
    window = s.get("window_hours", 24)
    since_clause, since_dt = _since_clause(window)
    q += f" {since_clause}"
    if s.get("lang"):
        q += f" lang:{s['lang']}"
    return q, since_dt


# --- API call (isolated for tests) --------------------------------------------

def twitterapi_search(token, query, cursor=None, timeout=30):
    """
    One page of advanced_search. Returns the raw JSON dict
    (expects keys: tweets[], next_cursor, has_next_page).
    Isolated so tests can monkeypatch it. Raises on HTTP errors.
    """
    params = {"query": query}
    if cursor:
        params["cursor"] = cursor
    resp = requests.get(
        API_BASE + SEARCH_PATH,
        headers={"X-API-Key": token},
        params=params,
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()


def _search_all_pages(token, query, max_pages, timeout, search_fn):
    """Paginate advanced_search until no cursor or max_pages reached."""
    tweets = []
    cursor = None
    for _ in range(max_pages):
        data = search_fn(token, query, cursor=cursor, timeout=timeout)
        page = data.get("tweets", []) or []
        tweets.extend(page)
        cursor = data.get("next_cursor")
        has_next = data.get("has_next_page", bool(cursor))
        if not cursor or not has_next or not page:
            break
    return tweets


# --- normalization ------------------------------------------------------------

def _parse_created_at(raw):
    """
    TwitterAPI.io createdAt is typically Twitter's format
    'Wed Jun 25 10:00:00 +0000 2026'. Return tz-aware datetime or None.
    """
    val = raw.get("createdAt") or raw.get("created_at")
    if not val:
        return None
    for fmt in ("%a %b %d %H:%M:%S %z %Y", "%Y-%m-%dT%H:%M:%S%z",
                "%Y-%m-%d %H:%M:%S%z"):
        try:
            return dt.datetime.strptime(val, fmt)
        except (ValueError, TypeError):
            continue
    return None


def normalize_tweet(raw, stream, source_label):
    """Map a TwitterAPI.io tweet object into our common item shape."""
    author_obj = raw.get("author") or {}
    author = author_obj.get("userName") or author_obj.get("screen_name")
    metrics = {
        "like": raw.get("likeCount", raw.get("favorite_count", 0)),
        "retweet": raw.get("retweetCount", raw.get("retweet_count", 0)),
        "reply": raw.get("replyCount", raw.get("reply_count", 0)),
        "quote": raw.get("quoteCount", raw.get("quote_count", 0)),
        "view": raw.get("viewCount", 0),
        "author_followers": author_obj.get("followers",
                                           author_obj.get("followers_count", 0)),
    }
    text = raw.get("text") or raw.get("full_text") or ""
    url = raw.get("url") or raw.get("twitterUrl") or ""
    if not url and raw.get("id") and author:
        url = f"https://x.com/{author}/status/{raw['id']}"
    return make_item(
        source=source_label,
        stream=stream,
        title=text[:120],
        url=url,
        published=raw.get("createdAt") or raw.get("created_at"),
        summary=text,
        author=author,
        metrics=metrics,
    )


def _within_window(raw, since_dt):
    """Client-side guard: keep only tweets at/after since_dt when parseable."""
    if since_dt is None:
        return True
    created = _parse_created_at(raw)
    if created is None:
        return True  # can't parse -> don't drop (API already filtered by since:)
    return created >= since_dt


# --- stream orchestration -----------------------------------------------------

def fetch_following(cfg, search_fn=twitterapi_search):
    """
    Fetch all tweets from subscribed handles within the time window.
    Returns (items, report_entry).
    """
    s = cfg["following_stream"]
    if not s.get("enabled", False):
        return [], {"stream": "following_stream", "ok": True,
                    "skipped": True, "count": 0, "note": "disabled"}

    query, since_dt = build_following_query(cfg)
    if query is None:
        return [], {"stream": "following_stream", "ok": True, "skipped": True,
                    "count": 0, "note": "no handles configured"}

    token = os.environ.get(cfg["twitterapi"]["token_env"], "")
    if not token:
        return [], {"stream": "following_stream", "ok": False,
                    "error": "token env not set", "query": query}

    try:
        raw = _search_all_pages(
            token, query,
            max_pages=s.get("max_pages", 20),
            timeout=cfg["twitterapi"].get("timeout_seconds", 30),
            search_fn=search_fn,
        )
        kept = [t for t in raw if _within_window(t, since_dt)]
        items = [normalize_tweet(t, "following", "X-following") for t in kept]
        return items, {"stream": "following_stream", "ok": True,
                       "count": len(items), "fetched": len(raw),
                       "query": query}
    except Exception as ex:  # noqa: BLE001
        return [], {"stream": "following_stream", "ok": False,
                    "error": f"{type(ex).__name__}: {ex}", "query": query}


def fetch_all(cfg, search_fn=twitterapi_search):
    """Currently only the following stream is in scope."""
    items, report = [], []
    got, rep = fetch_following(cfg, search_fn=search_fn)
    items.extend(got)
    report.append(rep)
    return items, report


# --- CLI ----------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--window-hours", type=int, default=None,
                    help="override following_stream.window_hours")
    ap.add_argument("--print-query", action="store_true",
                    help="print the query that would be sent, then exit")
    args = ap.parse_args()

    cfg = load_config(args.config)
    if args.window_hours is not None:
        cfg["following_stream"]["window_hours"] = args.window_hours

    if args.print_query:
        q, since_dt = build_following_query(cfg)
        print("following query:", q)
        print("since (UTC)    :", since_dt)
        return

    items, report = fetch_all(cfg)
    out = {"items": items, "report": report}
    for r in report:
        if r.get("skipped"):
            print(f"[skip] {r['stream']}: {r.get('note')}")
        elif r["ok"]:
            print(f"[ok]   {r['stream']}: {r['count']} tweets "
                  f"(fetched {r.get('fetched', r['count'])})")
        else:
            print(f"[FAIL] {r['stream']}: {r.get('error')}")
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        print(f"wrote {len(items)} tweets to {args.out}")
    else:
        json.dump(out, sys.stdout, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
