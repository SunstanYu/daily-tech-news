"""
rank.py — Hotness ranking for the keyword stream.

Goal (from the user's own intuition): surface topics that "everyone is talking
about" — i.e. many DISTINCT authors discussing the same thing — not a single
viral mega-tweet. So the score weights UNIQUE AUTHORS above raw engagement.

Pipeline:
  1. Group tweets into topics. v1 grouping signal = hashtags + cashtags +
     a few salient keywords (lightweight, zero-cost, no embeddings).
     A tweet with no hashtag/cashtag falls back to a keyword bucket.
  2. For each topic compute:
       - unique_authors : number of distinct handles (anti-brigade)
       - total_engagement : sum of like+rt+reply+quote across its tweets
  3. score = unique_authors * w_authors
           + (total_engagement / engagement_norm_base) * w_engagement
  4. Sort by score desc, return top_n topics, each with its representative
     (highest-engagement) tweet + the full member list.

Only the keyword stream is ranked. RSS + following streams are NOT ranked
(they're curated sources — passed through untouched by the digest builder).
"""
import re
import json
import argparse
from collections import defaultdict

from common import load_config


HASHTAG_RE = re.compile(r"[#＃](\w{2,})")
CASHTAG_RE = re.compile(r"\$([A-Za-z]{1,6})\b")

# Minimal stopword list for the keyword-bucket fallback.
STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "to", "of", "in", "on", "for",
    "is", "are", "was", "be", "this", "that", "with", "it", "as", "at",
    "by", "from", "you", "your", "we", "they", "i", "im", "its", "via",
    "rt", "amp", "new", "just", "now", "how", "what", "why", "will",
}


def topic_signals(text):
    """Extract grouping signals from one tweet's text."""
    if not text:
        return []
    tags = [t.lower() for t in HASHTAG_RE.findall(text)]
    cash = ["$" + c.lower() for c in CASHTAG_RE.findall(text)]
    sigs = tags + cash
    return sigs


def fallback_keyword(text):
    """Pick one salient keyword as a bucket when a tweet has no tag."""
    words = re.findall(r"[A-Za-z]{3,}", (text or "").lower())
    for w in words:
        if w not in STOPWORDS:
            return f"kw:{w}"
    return "kw:_misc"


def engagement(metrics):
    return (metrics.get("like", 0) + metrics.get("retweet", 0)
            + metrics.get("reply", 0) + metrics.get("quote", 0))


def cluster(items):
    """
    Group tweets into topics keyed by signal. A tweet can belong to multiple
    hashtag topics (it counts toward each). Tweets with no tag go to a single
    fallback keyword bucket so they still get a chance to cluster.
    """
    topics = defaultdict(list)
    for it in items:
        sigs = topic_signals(it["summary"])
        if not sigs:
            sigs = [fallback_keyword(it["summary"])]
        for s in sigs:
            topics[s].append(it)
    return topics


def score_topic(members, cfg):
    ks = cfg["keywords_stream"]
    w_auth = ks.get("unique_authors_weight", 2.0)
    w_eng = ks.get("engagement_weight", 1.0)
    norm = ks.get("engagement_norm_base", 1000) or 1

    authors = {m["author"] for m in members if m.get("author")}
    unique_authors = len(authors)
    total_eng = sum(engagement(m.get("metrics", {})) for m in members)
    score = unique_authors * w_auth + (total_eng / norm) * w_eng
    return {
        "unique_authors": unique_authors,
        "total_engagement": total_eng,
        "tweet_count": len(members),
        "score": round(score, 4),
    }


def rank(items, cfg):
    """Return a list of ranked topics (top_n), richest first."""
    topics = cluster(items)
    ranked = []
    for signal, members in topics.items():
        stats = score_topic(members, cfg)
        # representative tweet = highest single-tweet engagement
        rep = max(members, key=lambda m: engagement(m.get("metrics", {})))
        ranked.append({
            "topic_signal": signal,
            **stats,
            "representative": {
                "title": rep["title"],
                "url": rep["url"],
                "author": rep.get("author"),
                "summary": rep["summary"],
                "metrics": rep.get("metrics", {}),
            },
            "members": [
                {"url": m["url"], "author": m.get("author"),
                 "engagement": engagement(m.get("metrics", {}))}
                for m in members
            ],
        })
    ranked.sort(key=lambda t: t["score"], reverse=True)
    top_n = cfg["keywords_stream"].get("top_n", 20)
    return ranked[:top_n]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=None)
    ap.add_argument("--in", dest="infile", required=True)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    cfg = load_config(args.config)
    with open(args.infile, "r", encoding="utf-8") as f:
        payload = json.load(f)
    items = payload.get("items", payload)
    # Only rank keyword-stream tweets.
    kw_items = [it for it in items if it.get("stream") == "keywords"]

    ranked = rank(kw_items, cfg)
    print(f"ranked {len(ranked)} topics from {len(kw_items)} keyword tweets")
    for i, t in enumerate(ranked[:10]):
        print(f"  #{i} {t['topic_signal']:<20} "
              f"authors={t['unique_authors']:<3} "
              f"eng={t['total_engagement']:<7} score={t['score']}")
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump({"topics": ranked}, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
