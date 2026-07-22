"""
dispatch_summaries.py — 为 daily-tech-news 生成唯一的子 agent 任务。

读取 digest_raw.json，将所有条目打包成单个 sub-agent 任务。
子 agent 只需要**总结归类**（raw_summary 已由上游抓取脚本提供，不需要再抓网页），
一次性对所有条目做可用性检查、相关性过滤、摘要、归类，结果写入 /tmp/dtn_summaries/。

主 agent 不需要"理解"任何逻辑——只需要：
1. 读取 batches/001.json（包含精确的 delegate_task(tasks=[...]) 参数）
2. 调用 delegate_task(tasks=...)
3. 完成后运行 verify_summaries.py 验证输出
"""
import json
import os
import sys

BATCH_DIR = "/tmp/dtn_batches"
SUMMARY_DIR = "/tmp/dtn_summaries"

ANGELS = ["教程", "观点解读", "工具评测", "事件分析", "趋势洞察"]


def load_digest(path):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("entries", [])


def generate_single_task(digest_path):
    entries = load_digest(digest_path)

    if not entries:
        print("ERROR: No entries found in digest.")
        print("This means dedup/filter-pending found zero new items to summarize.")
        print("Skip Step 4 — tell user '今日无新条目' and go straight to Step 6.")
        sys.exit(1)

    # 构建条目列表文本（截断过长的 raw_summary）
    entries_text_lines = []
    for e in entries:
        eid = e["id"]
        title = e.get("title", "")
        source = e.get("source", "")
        stream = e.get("stream", "")
        url = e.get("url", "")
        raw = e.get("raw_summary", "") or ""
        if len(raw) > 400:
            raw = raw[:400] + "..."
        entries_text_lines.append(f"--- entry [{eid}] ---")
        entries_text_lines.append(f"  source: {source}")
        entries_text_lines.append(f"  stream: {stream}")
        entries_text_lines.append(f"  url: {url}")
        entries_text_lines.append(f"  title: {title}")
        entries_text_lines.append(f"  raw_summary: {raw}")

    entries_block = "\n".join(entries_text_lines)

    context = f"""读取文件 /tmp/dtn_digest_raw.json，包含 {len(entries)} 条新闻条目。
每条条目字段：id、title、url、source、stream、raw_summary。
raw_summary 是上游抓取脚本已获取的内容（RSS description 或推文原文），你只需要基于它工作。

所有条目列表：

{entries_block}

--- 以下是对每一条条目要执行的操作 ---

1. **内容可用性检查**：如果 raw_summary 为空、仅含无关符号、或内容太短/残缺（如只有标题或截断的一句话）导致无法写出有意义的摘要，将该条目标记为 \"SKIPPED\"。

2. **内容相关性判断**：
   - 保留：投资/融资/赚钱、AI/大模型/技术讨论、科技公司/行业动态、带转发评论的quote tweet（作者写了推荐/转发原因）
   - 剔除：生活分享、心情吐槽、日常闲聊、搞笑段子
   - 宁多勿漏：混合信号（生活+科技）保留，不确定时保留
   - 将判定为不相关的条目标记为 \"FILTERED\"。

3. **写摘要**：对保留的条目，写一段 2-4 句的中文摘要，客观说明这条新闻/讨论是什么、为什么值得注意。摘要必须平台中立（不要写成小红书/公众号风格，只是事实性总结）。

4. **给角度**：为每条保留的条目从这些里选最合适的一个：教程 | 观点解读 | 工具评测 | 事件分析 | 趋势洞察

5. **写结果文件**：对每一条条目（无论保留/跳过/过滤），用 Python 的 json.dump 写入 /tmp/dtn_summaries/{{id}}.json：
   - 正常条目：id={id}, summary=\"你的摘要\", angle=\"你选的角度\"
   - 跳过的条目：id={id}, summary=\"SKIPPED: 已抓内容不可用\", angle=\"无\"
   - 过滤的条目：id={id}, summary=\"FILTERED: 内容不相关\", angle=\"无\"
   不要手写 JSON 字符串，确保转义正确。示例：
       import json
       result = {{"id": {id}, "summary": "...", "angle": "..."}}
       with open(f"/tmp/dtn_summaries/{{id}}.json", "w", encoding="utf-8") as f:
           json.dump(result, f, ensure_ascii=False, indent=2)

**重要：逐条处理，对所有 {len(entries)} 条都输出对应的 summary 文件。不要遗漏任何一条。**"""

    task = {
        "goal": f"读取 /tmp/dtn_digest_raw.json 全部 {len(entries)} 条条目，逐条进行内容检查、相关性过滤、中文摘要和角度归类，将每条结果写入 /tmp/dtn_summaries/{{id}}.json",
        "context": context,
        "toolsets": ["file"],
        "role": "leaf",
        "model": {"provider": "openrouter", "model": "minimax/minimax-m2.5"},
    }

    os.makedirs(BATCH_DIR, exist_ok=True)
    os.makedirs(SUMMARY_DIR, exist_ok=True)

    batch_info = {
        "batch_number": 1,
        "total_batches": 1,
        "tasks_in_this_batch": 1,
        "entry_ids": [e["id"] for e in entries],
        "instruction": (
            f"这是唯一的 1/1 批。"
            f"请调用 delegate_task(tasks=tasks)，其中 tasks 是下面的数组（只有 1 个任务）。"
            f"调用完成后，读取 /tmp/dtn_summaries/{{id}}.json 验证每个文件存在且含非空 summary + angle。"
            f"若有遗漏，为该条单独重新委派一次；仍失败则标记'摘要生成失败'。"
        ),
        "tasks": [task],
    }

    batch_path = os.path.join(BATCH_DIR, "001.json")
    with open(batch_path, "w", encoding="utf-8") as f:
        json.dump(batch_info, f, ensure_ascii=False, indent=2)

    prompt_path = os.path.join(BATCH_DIR, "PROMPT.md")
    with open(prompt_path, "w", encoding="utf-8") as f:
        f.write(f"# 摘要子 agent 派发指令\n\n")
        f.write(f"digest 共有 {len(entries)} 条待摘要条目，已合并为 1 个 sub-agent 任务。\n\n")
        f.write(f"## 操作步骤\n\n")
        f.write(f"读取 {BATCH_DIR}/001.json\n")
        f.write(f"调用: delegate_task(tasks=<该文件中的 tasks 数组>)\n")
        f.write(f"完成后，对以下 {len(entries)} 个 entry_ids 逐一验证：\n")
        f.write(f"  entry_ids={batch_info['entry_ids']}\n")
        f.write(f"对每个 id：\n")
        f.write(f"  read_file /tmp/dtn_summaries/{{id}}.json\n")
        f.write(f"  验证：文件存在，含非空 summary 和 angle\n")
        f.write(f"  若遗漏：重新单独委派该条一次；仍失败则标记'摘要生成失败'\n\n")
        f.write(f"所有条目验证完成后：\n")
        f.write(f"进入 Step 4b（过滤不相关条目）→ Step 5（整合 digest）→ Step 7（呈现给用户）\n")

    print(f"dispatch: {len(entries)} entries → 1 sub-agent task")
    print(f"批处理文件: {batch_path}")
    print(f"派发提示:  {prompt_path}")
    print(f"摘要输出:  {SUMMARY_DIR}/")
    print(f"\n主 agent 操作指南:")
    print(f"1. 读取 {batch_path}")
    print(f"2. 调用 delegate_task(tasks=<该文件的 tasks 数组>)")
    print(f"3. 完成后验证 /tmp/dtn_summaries/*.json")


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--digest", default="/tmp/dtn_digest_raw.json", help="digest_raw.json 路径"
    )
    args = ap.parse_args()

    generate_single_task(args.digest)
