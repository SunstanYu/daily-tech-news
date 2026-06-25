"""
test_connectivity.py — REAL network tests. Run on YOUR machine.

These hit live endpoints. RSS + HN are free. TwitterAPI.io bills per returned tweet and
is SKIPPED unless you pass --live.

Usage:
    # free: test all RSS sources + HN
    python3 test_connectivity.py

    # also test TwitterAPI.io (per-tweet billing, needs token)
    export TWITTERAPI_KEY=xxx
    python3 test_connectivity.py --live

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


def test_twitterapi(cfg, live):
    print(f"\n{'='*60}\nX / TwitterAPI.io (following stream)\n{'='*60}")
    # always show what query WOULD be sent (free)
    fq, since_dt = fetch_x.build_following_query(cfg)
    if fq is None:
        print(f"{DIM}following_stream.handles 为空 —— 填入账号后才会抓取{RESET}")
        return True
    print(f"{DIM}following query : {fq}{RESET}")
    print(f"{DIM}since (UTC)     : {since_dt}{RESET}")

    if not live:
        print(f"\n{DIM}跳过真实 TwitterAPI.io 调用"
              f"(加 --live 启用,按返回推文条数计费,小用量极便宜){RESET}")
        return True

    token = os.environ.get(cfg["twitterapi"]["token_env"], "")
    if not token:
        line(False, cfg["twitterapi"]["token_env"], "环境变量未设置")
        return False
    line(True, cfg["twitterapi"]["token_env"], f"{DIM}已设置 (***{token[-4:]}){RESET}")

    if not cfg["following_stream"].get("enabled"):
        print(f"{DIM}following_stream.enabled=false,临时打开以测试{RESET}")
        cfg["following_stream"]["enabled"] = True

    items, rep = fetch_x.fetch_following(cfg)
    if rep["ok"]:
        detail = f"count={rep['count']} (fetched {rep.get('fetched', '?')})"
        if items:
            s = items[0]
            detail += f" {DIM}@{s['author']}: {s['title'][:40]}{RESET}"
        line(True, "following fetch", detail)
        if items:
            for fld in ("author", "url", "metrics", "published"):
                ok = bool(items[0].get(fld))
                line(ok, f"field:{fld}", "" if ok else "缺失!")
        else:
            print(f"{DIM}注:时间窗内这些账号没有推文,属正常(不代表失败){RESET}")
    else:
        line(False, "following fetch", rep.get("error", ""))
    return rep["ok"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=None)
    ap.add_argument("--live", action="store_true",
                    help="actually call TwitterAPI.io (needs token; per-tweet billing)")
    ap.add_argument("--window-hours", type=int, default=None,
                    help="override following_stream.window_hours for this test")
    args = ap.parse_args()

    cfg = common.load_config(args.config)
    if args.window_hours is not None:
        cfg["following_stream"]["window_hours"] = args.window_hours
    rss_ok = test_rss(cfg)
    x_ok = test_twitterapi(cfg, args.live)

    print(f"\n{'='*60}")
    print(f"RSS: {'OK' if rss_ok else 'FAIL'}  |  "
          f"TwitterAPI.io: {'OK' if x_ok else 'SKIPPED/FAIL'}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
