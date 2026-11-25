"""export_cac_article.py — 把 CAC 备案解读交付到精选新闻网站(curated-news-site)。

落点不是 personal-site 的 writing 集合(2026-11-25 改),是「精选科技解读」站。
那边的文章不是 Markdown,是一份 interpretation.json:

    title / hook / category / palette / read_minutes
    sections[]  每节 heading + conclusion + body + diagram_id
    diagrams[]  图示的规格,图本身是 03_diagrams/<id>.svg
    judgment / closing / source

所以上层 agent 写的不是 Markdown 正文,而是这份 JSON 和几张 SVG。正文标记只有
**强调** / [[术语]] / 段首 > 引语 / 段首编号列表 / [文字](https://…) 五种,
没有表格 —— 统计表要画成图示,不要写成 Markdown 表格(会原样显示成一串竖线)。

两台机器的分工(实测,别想当然):
  · 服务器(日报跑的地方)上**没有** curated-news-site,也没有 news-website 那套
    发布脚本。所以在服务器上本脚本只做一件事:把 run 目录落进 digests/cac/<slug>/
    并提交推送,等于把稿子交到 git 上,等 Mac 上取。
  · Mac 上两个仓库都在,本脚本顺带把 run 目录导进网站(import-run.mjs)。

真正上线仍然要人定价,本脚本不碰:
    python ../news-website/approve.py --free <slug> --push
「发布必须定价」是网站那边定好的规矩,不在这里绕开 —— 导入默认落在 pending,
pending 不进生产产物。

统计数字一律从 extract 的产物里核对,不让 agent 手敲 —— 手敲会错。

用法:
  python3 scripts/export_cac_article.py --batch /tmp/cac_new.json \
      --article /tmp/cac_interp.json --diagrams /tmp/cac_diagrams
  # 只落 run 目录,不导入也不提交:
  python3 scripts/export_cac_article.py --batch ... --article ... --no-import --no-push
"""
import os
import re
import sys
import json
import shutil
import argparse
import subprocess

from common import SKILL_DIR, load_config

RUN_ROOT = os.path.join(SKILL_DIR, "digests", "cac")
# 这些表不计入「这批公司」—— 变更是改已有备案,不是新批
COUNTED_SECTIONS = ("备案", "端侧备案", "拟人化备案", "登记")
REQUIRED = ("title", "hook", "category", "palette", "read_minutes",
            "sections", "diagrams", "judgment", "closing", "source")


def period_parts(batch):
    """从「…（2026年10月）」里取年月,用来拼 slug。"""
    text = batch.get("period") or batch.get("title") or ""
    m = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月", text)
    if not m:
        sys.exit(f"从 period/title 里认不出年月:{text!r}")
    return int(m.group(1)), int(m.group(2))


def collect(batch):
    """按表汇总条数和单位数。单位去重要跨表做,同一家可能既备案又登记。"""
    sections = batch.get("sections") or {}
    per_section, units = {}, set()
    for name in COUNTED_SECTIONS:
        rows = sections.get(name) or []
        if not rows:
            continue
        per_section[name] = len(rows)
        units.update(r["unit"] for r in rows if r.get("unit"))
    return per_section, units


def check(article, batch):
    """文章自身的硬检查。过不了就不往下走 —— 一篇数字对不上的名录文章没有价值。"""
    missing = [k for k in REQUIRED if k not in article]
    if missing:
        sys.exit(f"interpretation 缺字段:{', '.join(missing)}")
    secs = article["sections"]
    if len(secs) < 3:
        sys.exit(f"只有 {len(secs)} 节,疑似没写成,拒绝交付")
    for i, s in enumerate(secs, 1):
        for k in ("heading", "conclusion", "body"):
            if not (s.get(k) or "").strip():
                sys.exit(f"第 {i} 节缺 {k}")
        if "\n|" in s["body"] or s["body"].lstrip().startswith("|"):
            sys.exit(f"第 {i} 节正文里有 Markdown 表格 —— 站点不认,改画成图示")

    # 条数和单位数必须和 extract 的产物对得上。agent 手敲这两个数是最常见的错。
    per_section, units = collect(batch)
    total = sum(per_section.values())
    blob = json.dumps(article, ensure_ascii=False)
    for n, what in ((total, "条数"), (len(units), "单位数")):
        if str(n) not in blob:
            sys.exit(f"文章里找不到{what} {n} —— 数字对不上,先核对 extract 的 stats")
    print(f"  自检:{total} 条 / {len(units)} 家单位,"
          f"{len(secs)} 节,{len(article['diagrams'])} 图")
    return total, len(units)


def write_run(article, diagrams_dir, slug):
    """落成 import-run.mjs 认的 run 目录:<slug>/02_interpretation.json + 03_diagrams/。"""
    run = os.path.join(RUN_ROOT, slug)
    figs = os.path.join(run, "03_diagrams")
    os.makedirs(figs, exist_ok=True)
    with open(os.path.join(run, "02_interpretation.json"), "w", encoding="utf-8") as f:
        json.dump(article, f, ensure_ascii=False, indent=2)
    wanted = {f"{d['id']}.{'html' if d.get('type') == 'custom' else 'svg'}"
              for d in article.get("diagrams") or []}
    # 先清空:图数变少或某张换了形式时,旧文件留着就是孤儿
    for f in os.listdir(figs):
        if re.fullmatch(r"d\d+\.(svg|html)", f):
            os.remove(os.path.join(figs, f))
    for name in sorted(wanted):
        src = os.path.join(diagrams_dir, name) if diagrams_dir else None
        if not src or not os.path.exists(src):
            sys.exit(f"缺图 {name} —— diagrams 目录里没有,文章 JSON 里却声明了")
        shutil.copyfile(src, os.path.join(figs, name))
    print(f"已写入 {run}({len(wanted)} 张图)")
    return run


def import_to_site(site_dir, slug):
    """Mac 上顺带导进网站。服务器上没有这个仓库,跳过不是错。"""
    if not site_dir or not os.path.isdir(os.path.join(site_dir, ".git")):
        print(f"info: 没找到 curated-news-site({site_dir or '未配置'}),"
              f"只留了 run 目录;到 Mac 上跑:\n"
              f"      node scripts/import-run.mjs <日报仓库>/digests/cac {slug}")
        return False
    r = subprocess.run(["node", "scripts/import-run.mjs", RUN_ROOT, slug],
                       cwd=site_dir, capture_output=True, text=True)
    sys.stdout.write(r.stdout)
    if r.returncode != 0:
        print(f"warn: 导入失败,run 目录已落盘\n{r.stderr}")
        return False
    print("提示:导入后是 pending,不进生产产物。上线要定价:\n"
          f"      python3 approve.py --free {slug} --push   # 在 news-website 下跑")
    return True


def git_push(rel_path, message):
    try:
        subprocess.run(["git", "add", rel_path], cwd=SKILL_DIR,
                       check=True, capture_output=True)
        if subprocess.run(["git", "diff", "--cached", "--quiet"],
                          cwd=SKILL_DIR, capture_output=True).returncode == 0:
            print("info: 内容没变化,无需提交")
            return False
        subprocess.run(["git", "commit", "-m", message], cwd=SKILL_DIR,
                       check=True, capture_output=True, text=True)
        subprocess.run(["git", "push"], cwd=SKILL_DIR,
                       check=True, capture_output=True, text=True)
        print(f"已 push:{message}")
        return True
    except subprocess.CalledProcessError as ex:
        # 推送失败不中止:run 目录已经落盘,手动补推即可
        print(f"warn: git 操作失败,run 目录已落盘但未推送\n{ex.stderr or ex}")
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", required=True, help="fetch_cac_filings.py extract 的输出 JSON")
    ap.add_argument("--article", required=True, help="agent 写好的 interpretation JSON")
    ap.add_argument("--diagrams", help="图示目录(dN.svg / dN.html)")
    ap.add_argument("--site-dir", help="curated-news-site 路径,默认读 config.yaml")
    ap.add_argument("--no-import", action="store_true")
    ap.add_argument("--no-push", action="store_true")
    args = ap.parse_args()

    with open(args.batch, "r", encoding="utf-8") as f:
        batches = (json.load(f).get("batches") or [])
    if not batches:
        sys.exit("batch JSON 里没有 batches —— 没有新一期就不该走到这步")
    batch = batches[0]
    with open(args.article, "r", encoding="utf-8") as f:
        article = json.load(f)

    check(article, batch)
    year, month = period_parts(batch)
    slug = f"cac-genai-filings-{year}-{month:02d}"
    write_run(article, args.diagrams, slug)

    if not args.no_import:
        cfg = (load_config().get("cac_filings") or {})
        site = args.site_dir or cfg.get("curated_site_dir")
        import_to_site(os.path.expanduser(site) if site else None, slug)
    if not args.no_push:
        git_push(f"digests/cac/{slug}", f"CAC 备案解读 {year} 年 {month} 月:交稿")


if __name__ == "__main__":
    main()
