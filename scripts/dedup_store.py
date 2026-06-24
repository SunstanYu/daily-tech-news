"""
dedup_store.py — SQLite-backed dedup + storage.

Identity = item['dedup_key'] (derived in common.make_item from the url).
- An item is "new" if its dedup_key isn't already in the DB within the
  dedup window.
- New items are inserted; we return only the new ones for downstream ranking.

The DB also serves as the historical record (archive + future baseline).
"""
import sqlite3
import json
import argparse
import datetime as dt

from common import load_config, resolve_path


SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    dedup_key   TEXT PRIMARY KEY,
    source      TEXT,
    stream      TEXT,
    title       TEXT,
    url         TEXT,
    published   TEXT,
    summary     TEXT,
    author      TEXT,
    metrics     TEXT,           -- JSON blob
    first_seen  TEXT            -- ISO timestamp we first stored it
);
CREATE INDEX IF NOT EXISTS idx_first_seen ON items(first_seen);
CREATE INDEX IF NOT EXISTS idx_stream ON items(stream);
"""


def connect(db_path):
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    return conn


def existing_keys(conn, window_days):
    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=window_days)).isoformat()
    rows = conn.execute(
        "SELECT dedup_key FROM items WHERE first_seen >= ?", (cutoff,)
    ).fetchall()
    return {r[0] for r in rows}


def filter_new(items, seen_keys):
    """Return (new_items, dup_count). Also de-dups within the batch itself."""
    new = []
    batch_seen = set()
    dups = 0
    for it in items:
        k = it["dedup_key"]
        if k in seen_keys or k in batch_seen:
            dups += 1
            continue
        batch_seen.add(k)
        new.append(it)
    return new, dups


def insert(conn, items):
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    conn.executemany(
        """INSERT OR IGNORE INTO items
           (dedup_key, source, stream, title, url, published, summary,
            author, metrics, first_seen)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        [
            (it["dedup_key"], it["source"], it["stream"], it["title"],
             it["url"], it.get("published"), it.get("summary", ""),
             it.get("author"), json.dumps(it.get("metrics", {}),
                                          ensure_ascii=False), now)
            for it in items
        ],
    )
    conn.commit()


def process(cfg, items, conn=None):
    """
    Main entry: given freshly fetched items, return only the new ones,
    after storing them. Returns (new_items, stats).
    """
    window = cfg["storage"].get("dedup_window_days", 7)
    own_conn = False
    if conn is None:
        conn = connect(resolve_path(cfg["storage"]["db_path"]))
        own_conn = True
    seen = existing_keys(conn, window)
    new, dups = filter_new(items, seen)
    insert(conn, new)
    stats = {"input": len(items), "new": len(new), "duplicates": dups}
    if own_conn:
        conn.close()
    return new, stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=None)
    ap.add_argument("--in", dest="infile", required=True,
                    help="JSON file with {'items': [...]} from a fetch script")
    ap.add_argument("--out", default=None, help="write new items here")
    args = ap.parse_args()

    cfg = load_config(args.config)
    with open(args.infile, "r", encoding="utf-8") as f:
        payload = json.load(f)
    items = payload.get("items", payload)

    new, stats = process(cfg, items)
    print(f"dedup: input={stats['input']} new={stats['new']} "
          f"duplicates={stats['duplicates']}")
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump({"items": new}, f, ensure_ascii=False, indent=2)
        print(f"wrote {len(new)} new items to {args.out}")


if __name__ == "__main__":
    main()
