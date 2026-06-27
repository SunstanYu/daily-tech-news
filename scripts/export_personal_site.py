"""
export_personal_site.py — 将 digest_final.json 转成 personal-site news 格式。

作为 daily-tech-news 流水线的最后一步(Skip 可选),读取已完成的
digest_final.json,生成个人站点 src/content/news/YYYY-MM-DD.md 文件,
并在 personal-site 仓内 git commit + push。

字段映射:
  date             ← digest["date"]
  title            ← 自动生成 "M 月 D 日 · 今日精选"
  summary          ← 确定性拼接:从所有 entry.category 提取关键词组成一句话
  count            ← digest["entry_count"]
  item.title       ← entry.title
  item.url         ← entry.url
  item.source      ← stream 映射: rss→rss, hn→hn, x/following→x, keywords→x
  item.site        ← entry.source (如 "OpenAI", "Hugging Face")
  item.category    ← entry.angle  (如 "趋势洞察", "工具评测")
  item.note        ← entry.summary (中文摘要)
"""
import os
import sys
import json
import argparse
from datetime import datetime
from collections import Counter

from common import SKILL_DIR


STREAM_MAP = {
    "rss": "rss",
    "hn": "hn",
    "following": "x",
    "x": "x",
    "keywords": "x",
}

NEWS_DIR = os.path.expanduser("~/personal-site/src/content/news")
PERSONAL_SITE_DIR = os.path.expanduser("~/personal-site")


def safe_yaml_scalar(s):
    """Wrap a string in quotes if it contains YAML-special chars."""
    if not s:
        return '""'
    # URLs (http/https) are safe unquoted in YAML despite containing ":"
    if s.startswith("http://") or s.startswith("https://"):
        return s
    # Characters that make YAML interpret a value ambiguously
    needs_quote = any(c in s for c in ["#", "{", "}", "[", "]", ",", "&", "*", "?", "|", ">", "'", "\"", "%", "@", "`"])
    # Colon not at position 0 requires quoting
    if ":" in s and ":" != s[0]:
        needs_quote = True
    # Or starts with a space, or is a YAML boolean keyword
    if s.lower() in ("true", "false", "null", "yes", "no", "on", "off"):
        needs_quote = True
    if s and s[0] == " ":
        needs_quote = True
    if not needs_quote:
        return s
    escaped = s.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def build_summary(entries):
    """Deterministic one-liner summary from entry categories and sources.
    
    Scans all entries, extracts unique categories and key source names,
    and produces a Chinese summary line like:
    "今天的关键词是「趋势洞察」——涵盖 OpenAI 的 Agent 工作模式转变、Google Research 的缓存优化、DeepMind 的 Computer Use 等方向。"
    """
    if not entries:
        return "今日无新条目。"
    
    # Collect categories (angle)
    categories = [e.get("angle") for e in entries if e.get("angle")]
    cat_counter = Counter(categories)
    top_categories = [cat for cat, _ in cat_counter.most_common(3)]
    
    # Collect sources
    sources = [e.get("source") for e in entries if e.get("source")]
    seen = set()
    key_sources = []
    for s in sources:
        if s and s not in seen:
            seen.add(s)
            key_sources.append(s)
    
    # Pick top 3 source names for the summary
    key_sources = key_sources[:3]
    
    # Build the one-liner
    parts = []
    if top_categories:
        parts.append(f"今天的关键词是「{'、'.join(top_categories)}」")
    
    # Create brief mention of sources + titles
    highlights = []
    for e in entries[:3]:
        title = e.get("title", "")
        # Trim very long titles
        if len(title) > 40:
            title = title[:38] + "…"
        source = e.get("source", "")
        if source and source != "Hacker News":
            highlights.append(f"{source}")
        elif source == "Hacker News":
            highlights.append("HN")
    
    if highlights:
        unique_highlights = list(dict.fromkeys(highlights))  # deduplicate preserving order
        highlights_text = "、".join(unique_highlights)
        result = f"今天的关键词是「{'、'.join(top_categories)}」——涵盖{highlights_text}等方向的最新动态。"
    else:
        result = f"今天的关键词是「{'、'.join(top_categories)}」——共 {len(entries)} 条科技新闻。"
    
    return result


def fmt_date_title(date_str):
    """Convert '2026-06-26' → '6 月 26 日 · 今日精选'."""
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    return f"{dt.month} 月 {dt.day} 日 · 今日精选"


def build_md(digest):
    """Generate the complete Markdown frontmatter string."""
    date_str = digest["date"]
    title = fmt_date_title(date_str)
    entries = digest.get("entries", [])
    count = digest.get("entry_count", len(entries))
    summary = build_summary(entries)
    
    lines = []
    lines.append("---")
    lines.append(f"date: {date_str}")
    lines.append(f"title: {safe_yaml_scalar(title)}")
    lines.append(f"summary: {safe_yaml_scalar(summary)}")
    lines.append(f"count: {count}")
    lines.append("items:")
    
    for entry in entries:
        stream_val = entry.get("stream", "rss")
        ps_source = STREAM_MAP.get(stream_val, "rss")
        title_val = entry.get("title", "")
        url_val = entry.get("url", "")
        site_val = entry.get("source", "")
        category_val = entry.get("angle", "")
        note_val = entry.get("summary", "")
        
        lines.append(f"  - title: {safe_yaml_scalar(title_val)}")
        lines.append(f"    url: {safe_yaml_scalar(url_val)}")
        lines.append(f"    source: {ps_source}")
        
        if site_val:
            lines.append(f"    site: {safe_yaml_scalar(site_val)}")
        if category_val:
            lines.append(f"    category: {safe_yaml_scalar(category_val)}")
        if note_val:
            lines.append(f"    note: {safe_yaml_scalar(note_val)}")
    
    lines.append("---")
    return "\n".join(lines) + "\n"


def git_push(date_str):
    """Commit and push the new file in the personal-site repo."""
    if not os.path.isdir(os.path.join(PERSONAL_SITE_DIR, ".git")):
        print("warn: personal-site repo not found, skipping git push")
        return None
    
    # Check if there are any changes to commit
    import subprocess
    try:
        subprocess.run(
            ["git", "add", f"src/content/news/{date_str}.md"],
            cwd=PERSONAL_SITE_DIR, capture_output=True, check=True
        )
        
        # Check if there's actually something to commit
        status = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=PERSONAL_SITE_DIR, capture_output=True
        )
        if status.returncode == 0:
            print("info: no new git changes to commit")
            return None
        
        commit_msg = f"每日新闻: {date_str}"
        subprocess.run(
            ["git", "commit", "-m", commit_msg],
            cwd=PERSONAL_SITE_DIR, capture_output=True, check=True, text=True
        )
        
        push_result = subprocess.run(
            ["git", "push", "origin", "main"],
            cwd=PERSONAL_SITE_DIR, capture_output=True, text=True
        )
        
        if push_result.returncode != 0:
            print(f"warn: git push failed: {push_result.stderr.strip()}")
            return None
        
        return commit_msg
    except subprocess.CalledProcessError as e:
        print(f"error: git operation failed: {e.stderr.strip()}")
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--digest", default=None,
                    help="digest_final.json path (default: config.yaml output dir)")
    ap.add_argument("--skip-git", action="store_true",
                    help="Skip git commit/push after writing the file")
    args = ap.parse_args()

    # Load digest
    if args.digest:
        digest_path = args.digest
    else:
        from common import resolve_path, load_config
        cfg = load_config()
        digest_path = resolve_path(os.path.join(cfg["output"]["digest_dir"], "digest_final.json"))

    if not os.path.isfile(digest_path):
        print(f"error: digest not found at {digest_path}")
        print("Run the full pipeline first (Steps 1-5) to generate digest_final.json")
        sys.exit(1)

    with open(digest_path, "r", encoding="utf-8") as f:
        digest = json.load(f)

    date_str = digest["date"]
    md_content = build_md(digest)

    # Ensure news dir exists
    os.makedirs(NEWS_DIR, exist_ok=True)
    
    output_path = os.path.join(NEWS_DIR, f"{date_str}.md")
    
    # Check if file already exists
    if os.path.isfile(output_path):
        print(f"info: {output_path} already exists, overwriting")
    
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(md_content)
    
    print(f"export: wrote {output_path}")
    print(f"  entries: {digest.get('entry_count', 'N/A')}")
    
    # Git push
    if not args.skip_git:
        commit_msg = git_push(date_str)
        if commit_msg:
            print(f"git: committed & pushed ({commit_msg})")


if __name__ == "__main__":
    main()
