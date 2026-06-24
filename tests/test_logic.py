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
                "apify", "storage", "output", "summarize"):
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


# --- X fetch (offline, injected apify call) -----------------------------------

def test_keyword_query_construction():
    cfg = load_cfg()
    q = fetch_x.build_keyword_query(cfg)
    assert "(ai OR agent)" in q
    assert "min_faves:300" in q
    assert "lang:en" in q
    print(f"\n[x] keyword query = {q!r}")


def test_following_query_none_when_empty():
    cfg = load_cfg()
    assert fetch_x.build_following_query(cfg) is None
    cfg["following_stream"]["handles"] = ["sama", "@ylecun"]
    q = fetch_x.build_following_query(cfg)
    assert "from:sama" in q and "from:ylecun" in q
    print(f"\n[x] following query = {q!r}")


def test_tweet_normalization_and_injected_fetch():
    cfg = load_cfg()
    cfg["keywords_stream"]["enabled"] = True
    raw_tweets = [
        {"type": "tweet", "text": "hello #ai", "url": "https://x.com/t/1",
         "likeCount": 10, "retweetCount": 2, "replyCount": 1, "quoteCount": 0,
         "createdAt": "2026-06-23T00:00:00Z",
         "author": {"userName": "alice", "followers": 999}},
    ]
    def fake_call(actor_id, token, run_input, timeout=300):
        return raw_tweets
    items, rep = fetch_x.fetch_stream(cfg, "keywords_stream", apify_call=fake_call)
    assert rep["ok"] and len(items) == 1
    assert items[0]["author"] == "alice"
    assert items[0]["metrics"]["like"] == 10
    assert items[0]["stream"] == "keywords"
    print(f"\n[x] normalized tweet by {items[0]['author']}, "
          f"engagement fields present")


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


if __name__ == "__main__":
    sys.exit(__import__("pytest").main([__file__, "-v", "-s"]))
