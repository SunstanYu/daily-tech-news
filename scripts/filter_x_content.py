"""
filter_x_content.py — Filter X/Twitter tweets by topic relevance.

Keeps tweets related to: investment, tech, AI, business/money-making
Removes: life sharing, personal complaints, humor/jokes unrelated to tech
Special: preserves quote tweets where the author adds commentary/reason

Run between fetch_x.py and the merge/dedup step:
    python3 scripts/filter_x_content.py --in /tmp/dtn_x.json --out /tmp/dtn_x_filtered.json

This script does deterministic keyword/heuristic filtering only.
No LLM is used here.
"""
import sys
import json
import re
import argparse

# --- Relevance keywords (keep) ---
# Investment
KEEP_INVEST = re.compile(
    r"(invest|投资|融资|IPO|上市|估值|估值|市值|股票|"
    r"fund|VC|PE|venture|capital|"
    r"赚钱|营收|盈利|亏损|财报|revenue|profit|"
    r"A轮|B轮|C轮|种子轮|天使轮|"
    r"portfolio|deal|并购|acquisition)"
    , re.IGNORECASE)

# Tech / AI
KEEP_TECH = re.compile(
    r"(AI\b|ai\b|人工智能|大模型|LLM|llm|GPT|gpt|Claude|claude|"
    r"agent|智能体|模型|model|训练|train|微调|fine-tune|"
    r"推理|inference|transformer|扩散|diffusion|"
    r"GPU|TPU|算力|GPU|芯片|semiconductor|NVIDIA|GPU|"
    r"开源|open ?source|github|GitHub|API|SDK|框架|framework|"
    r"编程|code|开发|deploy|devops|cloud|云计算|"
    r"机器人|robot|自动驾驶|self.driving|\bAGI|agi\b|"
    r"算法|algorithm|数据\s*库|database|SaaS|PaaS|IaaS|"
    r"RAG|向量|vector|embedding|多模态|multimodal|"
    r"benchmark|benchmark|评测|paper|论文|arxiv|arXiv|"
    r"token|参数|parameter|算力|compute|训练|training)"
    , re.IGNORECASE)

# Tech industry / market
KEEP_TECH_INDUSTRY = re.compile(
    r"(Google\b|Anthropic|OpenAI|Meta\b|Apple\b|Microsoft|字节|ByteDance|腾讯|"
    r"阿里|Alibaba|百度\b|华为|Huawei|英伟达|NVIDIA|AMD|Intel|TSMC|"
    r"科技|technology|tech\b|硅谷|Silicon Valley|创业|startup|创始人|founder|"
    r"产品发布|launch|更新|update\b|version\b|新功能|feature\b)"
    , re.IGNORECASE)

# Workplace / career / podcast — keep these too
KEEP_WORKPLACE = re.compile(
    r"(职场|播客|podcast|职业|程序员|被裁|裁员|中年|技术人|技术博主|"
    r"面试|offer|跳槽|升职|薪资|年薪|月入|副业|搞钱|自由职业|solo|"
    r"成长|方法论|经验分享|心得|复盘|审美|预训练|AI思维|行业观察|"
    r"播客节目|音频节目|对话|访谈|对谈|圆桌|圆桌派|聊聊|聊聊AI)"
    , re.IGNORECASE)

# --- Irrelevance keywords (remove) ---
REMOVE_LIFE = re.compile(
    r"(^(今天|周末|假期|好开心|好难过|心情|生活|吃饭|吃饭了|买菜|逛街|逛街|睡觉))|"
    r"(天气|weather\s|逛街|吃饭\s|今天吃|晚餐|午餐|早餐|奶茶|咖啡\s|"
    r"生日|happy\s*birthday|旅行\s|旅游\s|回家|回\s*家|堵车|堵车|地铁\s|"
    r"感冒|生病|医院|医生|好看\s|好美\s|好看\s|可爱\s|哈哈哈|haha|lol|"
    r"吐槽\s|无语|服了|离谱|笑死|太好|太好了|终于|\.\.\.\s*$)"
    , re.IGNORECASE)

REMOVE_HUMOR = re.compile(
    r"^(段子|梗|笑死|搞笑|好搞笑|meme|太搞|太好笑)"
    , re.IGNORECASE)

# --- Quote tweet pattern ---
# TwitterAPI.io returns quote tweets with a quotedStatus or quoted_tweet field
# and the author's own text is separate from the quoted content.


def is_quote_tweet(raw_text_full, item):
    """Detect if this is a quote tweet with author's own commentary."""
    # Check for quoted tweet URL pattern (x.com/xxx/status/xxx in the text)
    quoted_url_pattern = r'https?://(?:twitter\.com|x\.com)/\w+/status/\d+'
    has_quoted_url = re.search(quoted_url_pattern, raw_text_full or '')
    if not has_quoted_url:
        return False

    # If the item has quoted text from another tweet, check if there's
    # substantial text beyond the quoted URL
    summary = item.get('raw_summary', '')
    # Remove the quoted tweet URL to see author's commentary
    own_text = re.sub(quoted_url_pattern, '', summary).strip()
    # Remove common prefixes/tokens
    own_text = re.sub(r'^(RT\s+)?[@#]', '', own_text).strip()

    # If there's substantial text (> 20 chars) beyond just the quoted URL,
    # the author added commentary
    return len(own_text) > 20


def score_relevance(item):
    """Score a tweet's relevance. Returns (keep: bool, reason: str)."""
    title = (item.get('title') or '')
    summary = (item.get('raw_summary') or '')
    author = (item.get('author') or '')
    text = f"{title} {' ' * 5} {summary}"  # Combine for matching

    # Check if this is a quote tweet with commentary -> always keep
    if is_quote_tweet(text, item):
        return True, "quote_tweet_with_commentary"

    # Check keep signals
    keep_signals = []
    if KEEP_INVEST.search(text):
        keep_signals.append("investment")
    if KEEP_TECH.search(text):
        keep_signals.append("tech_ai")
    if KEEP_TECH_INDUSTRY.search(text):
        keep_signals.append("tech_industry")
    if KEEP_WORKPLACE.search(text):
        keep_signals.append("workplace_career")

    # Check remove signals
    remove_signals = []
    if REMOVE_LIFE.search(text):
        remove_signals.append("life_content")
    if REMOVE_HUMOR.search(text):
        remove_signals.append("humor")

    # No remove signals and has keep signals -> keep
    if keep_signals and not remove_signals:
        return True, f"matched: {','.join(keep_signals)}"

    # Has remove signals and no keep signals -> remove
    if remove_signals and not keep_signals:
        return False, f"filtered: {','.join(remove_signals)}"

    # Both have signals -> keep (err on the side of inclusion)
    if keep_signals and remove_signals:
        return True, f"mixed(keep): {','.join(keep_signals)} over {','.join(remove_signals)}"

    # No strong signals either way -> keep short ones (likely just announcements)
    # Remove very short text that looks like pure social chatter
    text_stripped = re.sub(r'https?://\S+', '', text).strip()
    # Very short text with no keep signals and no informative content
    if len(text_stripped) < 15:
        return False, "too_short_no_signal"

    # Default: keep if neutral (no strong signals)
    return True, "default_keep"


def filter_tweets(items):
    """Filter items by relevance. Returns (kept, dropped, stats)."""
    kept = []
    dropped = []
    stats = {"total": len(items), "kept": 0, "dropped": 0, "reasons": {}}

    for item in items:
        keep, reason = score_relevance(item)
        if keep:
            kept.append(item)
            stats["kept"] += 1
            reason_key = reason.split(":")[0].strip()
            stats["reasons"][reason_key] = stats["reasons"].get(reason_key, 0) + 1
        else:
            dropped.append(item)
            stats["dropped"] += 1
            reason_key = reason.split(":")[0].strip()
            stats["reasons"][reason_key + "_dropped"] = (
                stats["reasons"].get(reason_key + "_dropped", 0) + 1
            )

    return kept, dropped, stats


def main():
    ap = argparse.ArgumentParser(
        description="Filter X tweets by topic relevance (investment/tech/AI)")
    ap.add_argument("--in", dest="infile", required=True,
                    help="Input JSON file from fetch_x.py")
    ap.add_argument("--out", default=None,
                    help="Output filtered JSON file")
    ap.add_argument("--verbose", "-v", action="store_true",
                    help="Print drop reasons for each removed tweet")
    args = ap.parse_args()

    with open(args.infile, "r", encoding="utf-8") as f:
        data = json.load(f)

    items = data.get("items", [])
    if not items:
        print("[filter] No tweets to filter.")
        if args.out:
            with open(args.out, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        return

    kept, dropped, stats = filter_tweets(items)

    print(f"[filter] X content filter: {stats['total']} -> {stats['kept']} kept, "
          f"{stats['dropped']} dropped")
    print(f"[filter] Reasons: {json.dumps(stats['reasons'], ensure_ascii=False)}")

    if args.verbose and dropped:
        print("\n[filter] Dropped tweets:")
        for item in dropped:
            keep, reason = score_relevance(item)
            title = item.get('title', '')[:60]
            author = item.get('author', '')
            print(f"  - @{author}: {title} -> {reason}")

    # Replace items in output with filtered set
    data["items"] = kept
    # Add filter report
    original_reports = data.get("report", [])
    filter_report = {
        "stream": "x_content_filter",
        "ok": True,
        "input_count": stats["total"],
        "output_count": stats["kept"],
        "dropped_count": stats["dropped"],
        "reasons": stats["reasons"],
    }

    # Preserve original report entries and add filter report
    if isinstance(original_reports, list):
        original_reports.append(filter_report)
        data["report"] = original_reports

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"[filter] wrote {len(kept)} filtered tweets to {args.out}")
    else:
        json.dump(data, sys.stdout, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
