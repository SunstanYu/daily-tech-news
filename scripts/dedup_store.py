"""
dedup_store.py — SQLite-backed dedup + storage.

Identity = item['dedup_key'] (derived in common.make_item from the url).
- An item is "new" if its dedup_key isn't already in the DB within the
  dedup window.
- New items are inserted; we return only the new ones for downstream ranking.
- After the agent layer summarizes an item, it calls `mark_summarized` to
  set `summarized_at`. Re-runs use `filter_summarized_out` to skip items the
  agent already processed — so a rerun never re-spends LLM time on the same
  story, even if dedup_window_days expires or the URL slips back into a feed.

The DB also serves as the historical record (archive + future baseline).
"""
import sqlite3
import json
import argparse
import datetime as dt

from common import load_config, resolve_path


# Note: indexes are created AFTER _migrate so they can reference columns
# (summarized_at) that legacy databases will only have post-migration.
SCHEMA_TABLE = """
CREATE TABLE IF NOT EXISTS items (
    dedup_key      TEXT PRIMARY KEY,
    source         TEXT,
    stream         TEXT,
    title          TEXT,
    url            TEXT,
    published      TEXT,
    summary        TEXT,
    author         TEXT,
    metrics        TEXT,           -- JSON blob
    first_seen     TEXT,           -- ISO timestamp we first stored it
    summarized_at  TEXT            -- ISO timestamp set by the agent layer; NULL = pending
);
"""

INDEX_STATEMENTS = (
    "CREATE INDEX IF NOT EXISTS idx_first_seen ON items(first_seen)",
    "CREATE INDEX IF NOT EXISTS idx_stream ON items(stream)",
    "CREATE INDEX IF NOT EXISTS idx_summarized_at ON items(summarized_at)",
)


def _migrate(conn):
    """Bring a pre-existing DB up to the current schema. Only the cases
    we've actually shipped need handling — keep this minimal."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(items)").fetchall()}
    if "summarized_at" not in cols:
        conn.execute("ALTER TABLE items ADD COLUMN summarized_at TEXT")
        conn.commit()


def connect(db_path):
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA_TABLE)
    _migrate(conn)
    for stmt in INDEX_STATEMENTS:
        conn.execute(stmt)
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


# --- summarization bookkeeping ------------------------------------------------

def summarized_keys(conn, dedup_keys):
    """Of the given dedup_keys, return the subset already marked summarized."""
    if not dedup_keys:
        return set()
    keys = list(dedup_keys)
    out = set()
    # Chunk to stay well under SQLite's default variable limit (999).
    for i in range(0, len(keys), 500):
        chunk = keys[i:i + 500]
        placeholders = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"SELECT dedup_key FROM items "
            f"WHERE summarized_at IS NOT NULL AND dedup_key IN ({placeholders})",
            chunk,
        ).fetchall()
        out.update(r[0] for r in rows)
    return out


def filter_summarized_out(items, conn):
    """Drop items whose dedup_key already has summarized_at set in DB.
    Returns (pending_items, skipped_count). Items not in DB are kept (they
    haven't been summarized yet by definition)."""
    done = summarized_keys(conn, [it["dedup_key"] for it in items])
    pending = [it for it in items if it["dedup_key"] not in done]
    return pending, len(items) - len(pending)


def mark_summarized(conn, dedup_keys, when=None):
    """Set summarized_at on the given dedup_keys. Returns the count updated."""
    if not dedup_keys:
        return 0
    when = when or dt.datetime.now(dt.timezone.utc).isoformat()
    keys = list(dedup_keys)
    total = 0
    for i in range(0, len(keys), 500):
        chunk = keys[i:i + 500]
        placeholders = ",".join("?" * len(chunk))
        cur = conn.execute(
            f"UPDATE items SET summarized_at = ? "
            f"WHERE dedup_key IN ({placeholders})",
            [when, *chunk],
        )
        total += cur.rowcount
    conn.commit()
    return total


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=None)
    sub = ap.add_subparsers(dest="cmd")

    # default behavior (no subcommand) = legacy: dedup new items.
    ap.add_argument("--in", dest="infile", default=None,
                    help="JSON file with {'items': [...]} from a fetch script")
    ap.add_argument("--out", default=None, help="write new items here")

    p_mark = sub.add_parser("mark-summarized",
                            help="mark items as summarized by dedup_key")
    p_mark.add_argument("--keys-file", required=True,
                        help="JSON file: {'keys': ['abc...', ...]} "
                             "OR a digest-shaped file (entries[].dedup_key).")

    p_filter = sub.add_parser("filter-pending",
                              help="filter items file to those not yet summarized")
    p_filter.add_argument("--in", dest="infile", required=True)
    p_filter.add_argument("--out", required=True)

    args = ap.parse_args()
    cfg = load_config(args.config)

    if args.cmd == "mark-summarized":
        with open(args.keys_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        keys = data.get("keys")
        if keys is None:
            # accept a digest file too
            keys = [e["dedup_key"] for e in data.get("entries", [])
                    if e.get("dedup_key")]
        conn = connect(resolve_path(cfg["storage"]["db_path"]))
        try:
            n = mark_summarized(conn, keys)
        finally:
            conn.close()
        print(f"mark-summarized: updated {n} of {len(keys)} keys")
        return

    if args.cmd == "filter-pending":
        with open(args.infile, "r", encoding="utf-8") as f:
            items = json.load(f).get("items", [])
        conn = connect(resolve_path(cfg["storage"]["db_path"]))
        try:
            pending, skipped = filter_summarized_out(items, conn)
        finally:
            conn.close()
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump({"items": pending}, f, ensure_ascii=False, indent=2)
        print(f"filter-pending: kept {len(pending)} pending, "
              f"skipped {skipped} already-summarized")
        return

    # Legacy default: dedup against the historical window.
    if not args.infile:
        ap.error("--in required for the default dedup operation")
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
