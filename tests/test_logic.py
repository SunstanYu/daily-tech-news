"""
test_logic.py — Offline tests for the deterministic layer (mock data only).
No network, no Apify, no cost. Run: pytest test_logic.py -v -s

Covers: config load, common normalization/dedup-key, RSS parsing (mock file),
tweet normalization, SQLite dedup, ranking (incl. the anti-brigade claim),
and digest assembly.
"""
import os
import sys
import json

# make scripts importable
SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, SCRIPTS)

import common  # noqa: E402
import fetch_rss  # noqa: E402
import fetch_x  # noqa: E402
import dedup_store  # noqa: E402
import rank  # noqa: E402
import build_digest  # noqa: E402

MOCKS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mocks")


def load_cfg():
    return common.load_config()


# --- config -------------------------------------------------------------------

def test_config_loads_and_has_required_sections():
    cfg = load_cfg()
    for key in ("rss_sources", "keywords_stream", "following_stream",
                "twitterapi", "storage", "output", "summarize"):
        assert key in cfg, f"missing config section: {key}"
    assert cfg["keywords_stream"]["keywords"] == ["ai", "agent"]
    print("\n[config] sections present; keywords =", cfg["keywords_stream"]["keywords"])


# --- common -------------------------------------------------------------------

def test_dedup_key_stable_and_url_based():
    a = common.make_item(source="S", stream="rss", title="T", url="https://x.com/1")
    b = common.make_item(source="S", stream="rss", title="DIFFERENT", url="https://x.com/1")
    c = common.make_item(source="S", stream="rss", title="T", url="https://x.com/2")
    assert a["dedup_key"] == b["dedup_key"], "same url -> same key"
    assert a["dedup_key"] != c["dedup_key"], "different url -> different key"
    print("\n[common] dedup key is url-based and stable")


def test_strip_html():
    assert common.strip_html("<p>Hello <b>world</b></p>") == "Hello world"


# --- RSS handler (offline, mock file) -----------------------------------------

def test_standard_rss_parsing_offline(monkeypatch):
    with open(os.path.join(MOCKS, "feed.xml"), "rb") as f:
        raw = f.read()

    class FakeResp:
        content = raw
        def raise_for_status(self): pass

    monkeypatch.setattr(fetch_rss.requests, "get", lambda *a, **k: FakeResp())
    items = fetch_rss.handle_standard_rss({"name": "Mock", "url": "http://x"})
    assert len(items) == 2
    assert items[0]["title"] == "Model X released with new reasoning"
    # HTML stripped from description
    assert "<" not in items[0]["summary"]
    assert items[0]["stream"] == "rss"
    print(f"\n[rss] parsed {len(items)} items; first = {items[0]['title']!r}")


def test_hn_algolia_offline(monkeypatch):
    fake = {"hits": [
        {"objectID": "1", "title": "HN story", "url": "https://h.com/a",
         "points": 120, "num_comments": 45, "created_at": "2026-06-23T00:00:00Z"}
    ]}

    class FakeResp:
        def raise_for_status(self): pass
        def json(self): return fake

    monkeypatch.setattr(fetch_rss.requests, "get", lambda *a, **k: FakeResp())
    items = fetch_rss.handle_hn_algolia({"name": "HN", "query": "ai",
                                         "points_threshold": 50, "max_items": 30})
    assert len(items) == 1
    assert items[0]["metrics"]["points"] == 120
    print(f"\n[hn] parsed story with points={items[0]['metrics']['points']}")


# --- X following stream (offline, injected TwitterAPI.io call) -----------------

def test_following_query_none_when_empty():
    cfg = load_cfg()
    cfg["following_stream"]["handles"] = []   # don't rely on the config default
    q, since_dt = fetch_x.build_following_query(cfg)
    assert q is None
    print("\n[x] empty handles -> query None (stream will skip)")


def test_following_query_construction():
    cfg = load_cfg()
    cfg["following_stream"]["handles"] = ["sama", "@ylecun", "AndrewYNg"]
    cfg["following_stream"]["window_hours"] = 24
    q, since_dt = fetch_x.build_following_query(cfg)
    assert "from:sama" in q and "from:ylecun" in q and "from:AndrewYNg" in q
    assert "@" not in q  # leading @ stripped
    assert "since:" in q
    assert since_dt is not None
    print(f"\n[x] following query = {q!r}")


def test_window_hours_affects_since():
    cfg = load_cfg()
    cfg["following_stream"]["handles"] = ["sama"]
    cfg["following_stream"]["window_hours"] = 6
    _, since_6 = fetch_x.build_following_query(cfg)
    cfg["following_stream"]["window_hours"] = 48
    _, since_48 = fetch_x.build_following_query(cfg)
    # 48h window reaches further back than 6h window
    assert since_48 < since_6
    print(f"\n[x] window_hours respected: 6h since={since_6:%H:%M} "
          f"< 48h since={since_48:%H:%M}")


def test_following_fetch_with_pagination_and_normalization():
    cfg = load_cfg()
    cfg["following_stream"]["enabled"] = True
    cfg["following_stream"]["handles"] = ["alice", "bob"]
    cfg["following_stream"]["window_hours"] = 24
    os.environ["TWITTERAPI_KEY"] = "test-token"

    # two pages, then stop
    pages = [
        {"tweets": [
            {"id": "1", "text": "post from alice", "createdAt": _recent(),
             "likeCount": 10, "retweetCount": 1, "replyCount": 0,
             "author": {"userName": "alice", "followers": 100}},
         ], "next_cursor": "C1", "has_next_page": True},
        {"tweets": [
            {"id": "2", "text": "post from bob", "createdAt": _recent(),
             "likeCount": 5, "author": {"userName": "bob"}},
         ], "next_cursor": "", "has_next_page": False},
    ]
    calls = {"n": 0}
    def fake_search(token, query, cursor=None, timeout=30):
        assert token == "test-token"
        i = calls["n"]; calls["n"] += 1
        return pages[i]

    items, rep = fetch_x.fetch_following(cfg, search_fn=fake_search)
    assert rep["ok"], rep
    assert rep["count"] == 2, f"expected 2 tweets across 2 pages, got {rep}"
    assert calls["n"] == 2, "should have paginated exactly twice"
    assert items[0]["author"] == "alice"
    assert items[0]["stream"] == "following"
    assert items[0]["metrics"]["like"] == 10
    assert items[0]["url"].startswith("https://x.com/alice/status/")
    print(f"\n[x] paginated 2 pages -> {rep['count']} tweets, "
          f"normalized with author+metrics+url")


def test_following_fetch_filters_old_tweets():
    """Tweets older than the window are dropped client-side."""
    cfg = load_cfg()
    cfg["following_stream"]["enabled"] = True
    cfg["following_stream"]["handles"] = ["alice"]
    cfg["following_stream"]["window_hours"] = 24
    os.environ["TWITTERAPI_KEY"] = "test-token"

    page = {"tweets": [
        {"id": "1", "text": "recent", "createdAt": _recent(),
         "author": {"userName": "alice"}},
        {"id": "2", "text": "too old", "createdAt": _old(hours=72),
         "author": {"userName": "alice"}},
    ], "next_cursor": "", "has_next_page": False}

    def fake_search(token, query, cursor=None, timeout=30):
        return page

    items, rep = fetch_x.fetch_following(cfg, search_fn=fake_search)
    assert rep["fetched"] == 2 and rep["count"] == 1, rep
    assert items[0]["title"] == "recent"
    print(f"\n[x] window filter: fetched 2, kept 1 (dropped 72h-old tweet)")


def test_following_skips_when_disabled():
    cfg = load_cfg()
    cfg["following_stream"]["enabled"] = False
    items, rep = fetch_x.fetch_following(cfg)
    assert rep.get("skipped") and items == []
    print("\n[x] disabled stream skipped cleanly")


def _recent():
    import datetime as _dt
    return (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=2)) \
        .strftime("%a %b %d %H:%M:%S %z %Y")


def _old(hours):
    import datetime as _dt
    return (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=hours)) \
        .strftime("%a %b %d %H:%M:%S %z %Y")


# --- dedup / sqlite -----------------------------------------------------------

def test_sqlite_dedup_roundtrip():
    cfg = load_cfg()
    conn = dedup_store.connect(":memory:")
    batch1 = [
        common.make_item(source="S", stream="rss", title="A", url="https://x/1"),
        common.make_item(source="S", stream="rss", title="B", url="https://x/2"),
    ]
    new1, stats1 = dedup_store.process(cfg, batch1, conn=conn)
    assert stats1["new"] == 2 and stats1["duplicates"] == 0

    # second batch: one repeat (url /2), one fresh (/3), plus in-batch dup
    batch2 = [
        common.make_item(source="S", stream="rss", title="B2", url="https://x/2"),
        common.make_item(source="S", stream="rss", title="C", url="https://x/3"),
        common.make_item(source="S", stream="rss", title="C-again", url="https://x/3"),
    ]
    new2, stats2 = dedup_store.process(cfg, batch2, conn=conn)
    assert stats2["new"] == 1, f"expected 1 new, got {stats2}"
    assert new2[0]["url"] == "https://x/3"
    print(f"\n[sqlite] batch1 new=2; batch2 new=1 (caught 1 cross-dup + 1 in-batch dup)")


# --- ranking: THE CORE CLAIM --------------------------------------------------

def test_ranking_unique_authors_beats_single_spammer():
    """
    The whole point: #agi (6 distinct authors) must outrank #spamcoin
    (1 author x6 with higher raw engagement). This proves the author-weighted
    score resists single-account brigading.
    """
    cfg = load_cfg()
    with open(os.path.join(MOCKS, "tweets.json"), "r", encoding="utf-8") as f:
        raw = json.load(f)["items"]
    items = [common.make_item(source="X", stream="keywords",
                              title=t["summary"][:50], url=f"https://x/{i}",
                              summary=t["summary"], author=t["author"],
                              metrics=t["metrics"])
             for i, t in enumerate(raw)]

    ranked = rank.rank(items, cfg)
    by_signal = {t["topic_signal"]: t for t in ranked}

    assert "agi" in by_signal, "#agi topic should exist"
    assert "spamcoin" in by_signal, "#spamcoin topic should exist"

    agi = by_signal["agi"]
    spam = by_signal["spamcoin"]

    print(f"\n[rank] #agi      authors={agi['unique_authors']} "
          f"eng={agi['total_engagement']} score={agi['score']}")
    print(f"[rank] #spamcoin authors={spam['unique_authors']} "
          f"eng={spam['total_engagement']} score={spam['score']}")

    assert agi["unique_authors"] == 6
    assert spam["unique_authors"] == 1
    assert agi["score"] > spam["score"], (
        "FAIL: single-account spam outranked multi-author topic — "
        "author weighting not working")
    # #agi should be the top-ranked topic overall
    assert ranked[0]["topic_signal"] == "agi"
    print("[rank] PASS: multi-author topic correctly ranked #1 over spam")


def test_ranking_representative_is_highest_engagement():
    cfg = load_cfg()
    items = [
        common.make_item(source="X", stream="keywords", title="t1",
                         url="https://x/1", summary="#ai low",
                         author="a", metrics={"like": 5}),
        common.make_item(source="X", stream="keywords", title="t2",
                         url="https://x/2", summary="#ai high",
                         author="b", metrics={"like": 999}),
    ]
    ranked = rank.rank(items, cfg)
    ai = [t for t in ranked if t["topic_signal"] == "ai"][0]
    assert ai["representative"]["url"] == "https://x/2"
    print("\n[rank] representative tweet = highest-engagement member (correct)")


# --- digest assembly ----------------------------------------------------------

def test_build_digest_numbering_and_order():
    cfg = load_cfg()
    curated = [
        common.make_item(source="OpenAI", stream="rss", title="Article 1",
                         url="https://o/1", summary="s1"),
    ]
    topics = [{
        "topic_signal": "agi", "unique_authors": 6, "total_engagement": 1200,
        "tweet_count": 6, "score": 13.2,
        "representative": {"title": "agi tweet", "url": "https://x/agi",
                           "author": "carol", "summary": "#agi big",
                           "metrics": {"like": 300}},
        "members": [],
    }]
    digest = build_digest.build(cfg, curated, topics)
    entries = digest["entries"]
    # hot topic first, curated second
    assert entries[0]["stream"] == "keywords"
    assert entries[1]["stream"] == "rss"
    # stable unique numbering
    ids = [e["id"] for e in entries]
    assert ids == list(range(len(entries)))
    # neutral fields present + summary/angle left for agent layer
    assert entries[0]["summary"] is None and entries[0]["angle"] is None
    assert "hotness" in entries[0]
    print(f"\n[digest] {len(entries)} entries, hot-first, ids={ids}, "
          f"summary/angle left empty for agent")


# --- RSS volume control: window + per-source cap ------------------------------

def test_parse_published_recognizes_common_formats():
    import datetime as _dt
    # RFC822 (most RSS), ISO with Z (HN algolia), ISO with offset
    rfc = "Tue, 24 Jun 2026 12:00:00 +0000"
    iso_z = "2026-06-24T12:00:00Z"
    iso_off = "2026-06-24T12:00:00+00:00"
    for s in (rfc, iso_z, iso_off):
        d = fetch_rss._parse_published(s)
        assert d is not None and d.tzinfo is not None, s
        assert d.year == 2026 and d.hour == 12
    assert fetch_rss._parse_published("not a date") is None
    assert fetch_rss._parse_published(None) is None
    print("\n[fetch_rss] _parse_published handles RFC822, ISO Z, ISO offset")


def test_apply_window_drops_old_keeps_undated():
    import datetime as _dt
    now = _dt.datetime.now(_dt.timezone.utc)
    items = [
        {"title": "recent",  "published": (now - _dt.timedelta(hours=6)).strftime("%a, %d %b %Y %H:%M:%S +0000")},
        {"title": "too_old", "published": (now - _dt.timedelta(hours=100)).strftime("%a, %d %b %Y %H:%M:%S +0000")},
        {"title": "undated", "published": None},
    ]
    kept, stats = fetch_rss._apply_window_and_cap(items, window_hours=48, max_items=999)
    titles = [i["title"] for i in kept]
    assert "recent" in titles and "undated" in titles
    assert "too_old" not in titles
    assert stats["dropped_old"] == 1 and stats["undated_kept"] == 1
    print(f"\n[fetch_rss] window=48h dropped 1 old, kept undated; stats={stats}")


def test_apply_cap_truncates_to_max_items():
    import datetime as _dt
    now = _dt.datetime.now(_dt.timezone.utc)
    items = [
        {"title": f"a{i}",
         "published": (now - _dt.timedelta(hours=i)).strftime("%a, %d %b %Y %H:%M:%S +0000")}
        for i in range(50)
    ]
    kept, stats = fetch_rss._apply_window_and_cap(items, window_hours=999, max_items=10)
    assert len(kept) == 10
    assert kept[0]["title"] == "a0", "newest first"
    assert stats["capped"] == 40
    print(f"\n[fetch_rss] capped 50 -> 10 newest-first; capped={stats['capped']}")


def test_fetch_all_uses_defaults_and_per_source_override(monkeypatch):
    """Per-source window_hours/max_items wins over rss_defaults."""
    cfg = {
        "rss_defaults": {"window_hours": 48, "max_items": 30},
        "rss_sources": [
            {"name": "Override", "type": "rss", "url": "http://x", "enabled": True,
             "window_hours": 1, "max_items": 2},
        ],
    }
    import datetime as _dt
    now = _dt.datetime.now(_dt.timezone.utc)

    def fake_handler(src):
        return [
            {"title": "h0", "published": (now - _dt.timedelta(minutes=10)).strftime("%a, %d %b %Y %H:%M:%S +0000")},
            {"title": "h1", "published": (now - _dt.timedelta(minutes=20)).strftime("%a, %d %b %Y %H:%M:%S +0000")},
            {"title": "h2", "published": (now - _dt.timedelta(minutes=30)).strftime("%a, %d %b %Y %H:%M:%S +0000")},
            {"title": "old", "published": (now - _dt.timedelta(hours=5)).strftime("%a, %d %b %Y %H:%M:%S +0000")},
        ]
    monkeypatch.setitem(fetch_rss.HANDLERS, "rss", fake_handler)
    items, report = fetch_rss.fetch_all(cfg)
    # 1h window kills "old"; max_items=2 caps to 2 newest
    assert [i["title"] for i in items] == ["h0", "h1"]
    f = report[0]["filter"]
    assert f["dropped_old"] == 1 and f["capped"] == 1
    print(f"\n[fetch_rss] per-source override beat defaults; filter={f}")


# --- dedup_store: summarized_at -----------------------------------------------

def test_summarized_at_migration_on_legacy_db(tmp_path):
    """Old DBs missing summarized_at must be auto-migrated by connect()."""
    import sqlite3
    db = tmp_path / "legacy.db"
    legacy = sqlite3.connect(str(db))
    legacy.execute("""
        CREATE TABLE items (
          dedup_key TEXT PRIMARY KEY, source TEXT, stream TEXT, title TEXT,
          url TEXT, published TEXT, summary TEXT, author TEXT, metrics TEXT,
          first_seen TEXT
        )
    """)
    legacy.execute("INSERT INTO items (dedup_key, first_seen) VALUES ('k1', '2026-01-01')")
    legacy.commit()
    legacy.close()

    conn = dedup_store.connect(str(db))
    cols = {row[1] for row in conn.execute("PRAGMA table_info(items)").fetchall()}
    assert "summarized_at" in cols, "migration should have added the column"
    # legacy row should still be there with NULL summarized_at
    row = conn.execute("SELECT dedup_key, summarized_at FROM items WHERE dedup_key='k1'").fetchone()
    assert row == ("k1", None)
    conn.close()
    print("\n[dedup_store] legacy DB migrated: summarized_at added, data preserved")


def test_mark_summarized_and_filter_pending_roundtrip():
    cfg = load_cfg()
    conn = dedup_store.connect(":memory:")
    batch = [
        common.make_item(source="S", stream="rss", title="A", url="https://x/a"),
        common.make_item(source="S", stream="rss", title="B", url="https://x/b"),
        common.make_item(source="S", stream="rss", title="C", url="https://x/c"),
    ]
    dedup_store.process(cfg, batch, conn=conn)

    # Before marking, all three are still pending.
    pending, skipped = dedup_store.filter_summarized_out(batch, conn)
    assert len(pending) == 3 and skipped == 0

    # Mark A + B as summarized; only C should remain pending next time.
    n = dedup_store.mark_summarized(conn, [batch[0]["dedup_key"], batch[1]["dedup_key"]])
    assert n == 2
    pending, skipped = dedup_store.filter_summarized_out(batch, conn)
    assert [p["title"] for p in pending] == ["C"]
    assert skipped == 2
    # Items not yet in DB are treated as pending (haven't been summarized).
    fresh = common.make_item(source="S", stream="rss", title="D", url="https://x/d")
    pending2, _ = dedup_store.filter_summarized_out([fresh], conn)
    assert pending2 == [fresh]
    print("\n[dedup_store] mark_summarized + filter_summarized_out round-trip OK")


# --- digest: carries dedup_key for the mark-summarized hand-off ---------------

def test_digest_curated_entries_carry_dedup_key():
    cfg = load_cfg()
    curated = [common.make_item(source="OpenAI", stream="rss", title="t",
                                url="https://o/1", summary="s")]
    digest = build_digest.build(cfg, curated, [])
    e = digest["entries"][0]
    assert e["dedup_key"] == curated[0]["dedup_key"]
    print(f"\n[digest] curated entry carries dedup_key={e['dedup_key']}")


if __name__ == "__main__":
    sys.exit(__import__("pytest").main([__file__, "-v", "-s"]))
