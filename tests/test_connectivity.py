"""
test_connectivity.py — REAL network tests. Run on YOUR machine.

These hit live endpoints. RSS + HN are free. Apify costs a little money and
is SKIPPED unless you pass --live-apify.

Usage:
    # free: test all RSS sources + HN
    python3 test_connectivity.py

    # also test Apify (costs ~cents, needs APIFY_TOKEN)
    export APIFY_TOKEN=xxx
    python3 test_connectivity.py --live-apify

This is a plain script (not pytest) so the output is easy to read: each source
prints OK/FAIL + count + a sample title, so you can see exactly which feed URLs
are valid and fix the dead ones in config.yaml.
"""
import os
import sys
import argparse

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, SCRIPTS)

import common  # noqa: E402
import fetch_rss  # noqa: E402
import fetch_x  # noqa: E402


GREEN = "\033[92m"
RED = "\033[91m"
DIM = "\033[2m"
RESET = "\033[0m"


def line(ok, label, detail=""):
    tag = f"{GREEN}OK  {RESET}" if ok else f"{RED}FAIL{RESET}"
    print(f"[{tag}] {label:<20} {detail}")


def test_rss(cfg):
    print(f"\n{'='*60}\nRSS / HN sources (free)\n{'='*60}")
    items, report = fetch_rss.fetch_all(cfg)
    ok_count = 0
    for r in report:
        if r["ok"]:
            ok_count += 1
            detail = f"count={r['count']:<3} {DIM}{r.get('sample_title') or ''}{RESET}"
        else:
            detail = f"{r.get('error', '')}"
        line(r["ok"], r["source"], detail)
    print(f"\n{ok_count}/{len(report)} sources reachable, "
          f"{len(items)} items total")
    if ok_count < len(report):
        print(f"{DIM}提示: FAIL 的源去 config.yaml 换 URL,或启用注释里的备用源"
              f"(Hugging Face / DeepMind / The Decoder){RESET}")
    return ok_count > 0


def test_apify(cfg, live):
    print(f"\n{'='*60}\nX / Apify\n{'='*60}")
    # always show what query WOULD be sent (free)
    kq = fetch_x.build_keyword_query(cfg)
    fq = fetch_x.build_following_query(cfg)
    print(f"{DIM}keyword query  : {kq}{RESET}")
    print(f"{DIM}following query : {fq}{RESET}")

    if not live:
        print(f"\n{DIM}跳过真实 Apify 调用(加 --live-apify 启用,会花少量钱){RESET}")
        return True

    token = os.environ.get(cfg["apify"]["token_env"], "")
    if not token:
        line(False, "APIFY_TOKEN", "环境变量未设置")
        return False
    line(True, "APIFY_TOKEN", f"{DIM}已设置 (***{token[-4:]}){RESET}")

    # do a tiny real fetch to verify token + field shape
    cfg2 = dict(cfg)
    cfg2["keywords_stream"] = dict(cfg["keywords_stream"])
    cfg2["keywords_stream"]["max_items"] = 50  # actor minimum is 50
    cfg2["keywords_stream"]["enabled"] = True
    items, rep = fetch_x.fetch_stream(cfg2, "keywords_stream")
    if rep["ok"]:
        sample = items[0] if items else None
        detail = f"count={rep['count']}"
        if sample:
            detail += f" {DIM}@{sample['author']}: {sample['title'][:40]}{RESET}"
        line(True, "keyword fetch", detail)
        # verify expected fields exist
        if sample:
            for fld in ("author", "url", "metrics"):
                ok = bool(sample.get(fld))
                line(ok, f"field:{fld}", "" if ok else "缺失!")
    else:
        line(False, "keyword fetch", rep.get("error", ""))
    return rep["ok"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=None)
    ap.add_argument("--live-apify", action="store_true",
                    help="actually call Apify (costs money, needs APIFY_TOKEN)")
    args = ap.parse_args()

    cfg = common.load_config(args.config)
    rss_ok = test_rss(cfg)
    apify_ok = test_apify(cfg, args.live_apify)

    print(f"\n{'='*60}")
    print(f"RSS: {'OK' if rss_ok else 'FAIL'}  |  "
          f"Apify: {'OK' if apify_ok else 'SKIPPED/FAIL'}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
