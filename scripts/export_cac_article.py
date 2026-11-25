"""
export_cac_article.py — 把 CAC 备案解读文章发到 personal-site 的 writing 集合。

和 export_personal_site.py 的区别:那个写 news 集合(只有 frontmatter 的链接清单),
这篇是正文长文,所以走 writing 集合。writing 的 schema(personal-site/src/content/config.ts):
    title / date / lang / description / tags / draft / readingTime?

正文由上层 agent 写好存成 Markdown,本脚本只做确定性的事:拼 frontmatter、落盘、
git commit + push。统计数字一律从 extract 的产物里取,不让 agent 手敲 —— 手敲会错。

用法:
  python3 scripts/export_cac_article.py --batch /tmp/cac_new.json --body /tmp/cac_article.md
  # 只生成不推送:
  python3 scripts/export_cac_article.py --batch ... --body ... --no-push
"""
import os
import re
import sys
import json
import argparse
import subprocess

from common import SKILL_DIR
from export_personal_site import safe_yaml_scalar

PERSONAL_SITE_DIR = os.path.expanduser("~/personal-site")
WRITING_DIR = os.path.join(PERSONAL_SITE_DIR, "src", "content", "writing")

# 这些表不计入「这批公司」—— 变更是改已有备案,不是新批
COUNTED_SECTIONS = ("备案", "端侧备案", "拟人化备案", "登记")


def period_parts(batch):
    """从「…（2026年10月）」里取年月,用来拼 slug 和日期。"""
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


def build_description(batch, per_section, units):
    bits = "、".join(f"{k} {v} 条" for k, v in per_section.items())
    year, month = period_parts(batch)
    return (f"网信办 {year} 年 {month} 月这一期生成式 AI 服务备案名单:{bits},"
            f"涉及 {len(units)} 家单位。按赛道和属地拆开看都是谁在备案。")


def build_md(batch, body):
    year, month = period_parts(batch)
    per_section, units = collect(batch)
    date_str = f"{year}-{month:02d}-01"
    title = f"网信办生成式 AI 备案名单解读 · {year} 年 {month} 月"
    lines = [
        "---",
        f"title: {safe_yaml_scalar(title)}",
        f"date: {date_str}",
        "lang: zh",
        f"description: {safe_yaml_scalar(build_description(batch, per_section, units))}",
        'tags: ["AI 备案", "生成式人工智能", "政策"]',
        "---",
        "",
        body.strip(),
        "",
    ]
    return "\n".join(lines), f"cac-genai-filings-{year}-{month:02d}"


def git_push(rel_path, message):
    if not os.path.isdir(os.path.join(PERSONAL_SITE_DIR, ".git")):
        print("warn: 没找到 personal-site 仓库,跳过 push")
        return False
    try:
        subprocess.run(["git", "add", rel_path], cwd=PERSONAL_SITE_DIR,
                       check=True, capture_output=True)
        if subprocess.run(["git", "diff", "--cached", "--quiet"],
                          cwd=PERSONAL_SITE_DIR, capture_output=True).returncode == 0:
            print("info: 内容没变化,无需提交")
            return False
        subprocess.run(["git", "commit", "-m", message], cwd=PERSONAL_SITE_DIR,
                       check=True, capture_output=True, text=True)
        subprocess.run(["git", "push"], cwd=PERSONAL_SITE_DIR,
                       check=True, capture_output=True, text=True)
        print(f"已 push:{message}")
        return True
    except subprocess.CalledProcessError as ex:
        # 推送失败不中止:文章已经落盘,手动补推即可
        print(f"warn: git 操作失败,文章已落盘但未推送\n{ex.stderr or ex}")
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", required=True, help="fetch_cac_filings.py extract 的输出 JSON")
    ap.add_argument("--body", required=True, help="agent 写好的文章正文 Markdown")
    ap.add_argument("--no-push", action="store_true")
    args = ap.parse_args()

    with open(args.batch, "r", encoding="utf-8") as f:
        data = json.load(f)
    batches = data.get("batches") or []
    if not batches:
        sys.exit("batch JSON 里没有 batches —— 没有新一期就不该走到这步")
    batch = batches[0]
    with open(args.body, "r", encoding="utf-8") as f:
        body = f.read()
    if len(body.strip()) < 200:
        sys.exit(f"正文只有 {len(body.strip())} 字,疑似没写成,拒绝发布")

    md, slug = build_md(batch, body)

    # 本仓库留一份,方便回看和复盘
    local_dir = os.path.join(SKILL_DIR, "digests", "cac")
    os.makedirs(local_dir, exist_ok=True)
    local_path = os.path.join(local_dir, f"{slug}.md")
    with open(local_path, "w", encoding="utf-8") as f:
        f.write(md)
    print(f"已写入 {local_path}")

    if not os.path.isdir(WRITING_DIR):
        print(f"warn: 没找到 {WRITING_DIR},只留了本地副本")
        return
    site_path = os.path.join(WRITING_DIR, f"{slug}.md")
    with open(site_path, "w", encoding="utf-8") as f:
        f.write(md)
    print(f"已写入 {site_path}")
    if not args.no_push:
        year, month = period_parts(batch)
        git_push(f"src/content/writing/{slug}.md",
                 f"CAC 备案解读: {year} 年 {month} 月")


if __name__ == "__main__":
    main()
