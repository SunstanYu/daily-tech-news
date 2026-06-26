"""
dispatch_summaries.py — 为 daily-tech-news 生成精确的子 agent 任务批次。

读取 digest_raw.json，为每条 entry 生成完整的 delegate_task task 参数。
按 batch_size 分批输出，每批一个 JSON 文件。

主 agent 不需要"理解"任何逻辑——只需要：
1. 读取 batches/001.json（包含精确的 delegate_task(tasks=[...]) 参数）
2. 调用 delegate_task(tasks=...)
3. 读取 batches/002.json（如果存在）
4. 重复，直到所有批次处理完
5. 运行 verify_summaries.py 验证输出
"""
import json
import os
import sys
from pathlib import Path

BATCH_DIR = "/tmp/dtn_batches"
SUMMARY_DIR = "/tmp/dtn_summaries"

ANGELS = ["教程", "观点解读", "工具评测", "事件分析", "趋势洞察"]

SUBAGENT_CONTEXT_TEMPLATE = """文件路径: /tmp/dtn_digest_raw.json
目标条目: entries[{id}]
该条目字段:
  - url: {url}
  - title: {title}
  - source: {source}
  - stream: {stream}
  - raw_summary: {raw_summary}

任务:
1. 若 url 有效且可访问,用 web 工具抓取原文全文;若抓不到或 url 为空,基于 raw_summary。
2. 写一段 2-4 句的中文摘要,客观说明这条新闻/讨论是什么、为什么值得注意。
   摘要必须平台中立(不要写成小红书/公众号风格,只是事实性总结)。
3. 给一个角度建议,从这些里选最合适的一个:
   {angles}
4. 用 Python 的 json.dump 将结果写入 {summary_file}。**不要手写 JSON 字符串**,用以下方式确保转义正确:
   import json
   result = {{"id": {id}, "summary": "你的摘要", "angle": "你选的角度", "fetched_fulltext": true或false}}
   with open("{summary_file}", "w", encoding="utf-8") as f:
       json.dump(result, f, ensure_ascii=False, indent=2)
只输出这个文件,不要做别的。"""


def load_digest(path):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("entries", [])


def make_subagent_task(entry, batch_size=3):
    """生成单个 sub-agent task 参数（精确 JSON，零歧义）"""
    eid = entry["id"]
    title = entry.get("title", "")
    url = entry.get("url", "")
    source = entry.get("source", "")
    stream = entry.get("stream", "")
    raw_summary = entry.get("raw_summary", "")

    if len(raw_summary) > 300:
        raw_summary = raw_summary[:300] + "..."

    context = SUBAGENT_CONTEXT_TEMPLATE.format(
        id=eid,
        url=url,
        title=title,
        source=source,
        stream=stream,
        raw_summary=raw_summary,
        angles=" | ".join(ANGELS),
        summary_file=f"{SUMMARY_DIR}/{eid}.json",
    )

    return {
        "goal": f"读取 /tmp/dtn_digest_raw.json 第 {eid} 条(entries[{eid}]),为它写一段中文摘要和内容角度建议,结果写入 {SUMMARY_DIR}/{eid}.json",
        "context": context,
        "toolsets": ["terminal", "file", "web"],
        "role": "leaf",
    }


def generate_batches(digest_path, batch_size=3):
    entries = load_digest(digest_path)

    if not entries:
        print("ERROR: No entries found in digest.")
        print("This means dedup/filter-pending found zero new items to summarize.")
        print("Skip Step 4 — tell user '今日无新条目' and go straight to Step 6.")
        sys.exit(1)

    os.makedirs(BATCH_DIR, exist_ok=True)
    os.makedirs(SUMMARY_DIR, exist_ok=True)

    total = len(entries)
    batches = []

    for i in range(0, total, batch_size):
        batch_entries = entries[i : i + batch_size]
        batch_num = i // batch_size + 1
        tasks = [make_subagent_task(e) for e in batch_entries]

        batch_info = {
            "batch_number": batch_num,
            "total_batches": (total + batch_size - 1) // batch_size,
            "tasks_in_this_batch": len(tasks),
            "entry_ids": [e["id"] for e in batch_entries],
            "instruction": f"这是第 {batch_num}/{(total + batch_size - 1) // batch_size} 批。"
            f"请调用 delegate_task(tasks=tasks),其中 tasks 是下面的数组。"
            f"调用完成后,读取 /tmp/dtn_summaries/{{id}}.json 验证每个文件存在且含非空 summary + angle。"
            f"若某条失败,重新委派该条一次;仍失败则标记'摘要生成失败'。",
            "tasks": tasks,
        }

        batch_path = os.path.join(BATCH_DIR, f"{batch_num:03d}.json")
        with open(batch_path, "w", encoding="utf-8") as f:
            json.dump(batch_info, f, ensure_ascii=False, indent=2)

        batches.append(batch_info)

    # 生成 dispatcher prompt
    prompt_parts = []
    prompt_parts.append(f"# 摘要子 agent 派发指令")
    prompt_parts.append(f"")
    prompt_parts.append(f"digest 共有 {total} 条待摘要条目。")
    prompt_parts.append(f"已分批到 {BATCH_DIR}/,每批最多 {batch_size} 个任务。")
    prompt_parts.append(f"请按顺序处理:")
    prompt_parts.append(f"")

    for b in batches:
        prompt_parts.append(f"## 第 {b['batch_number']}/{b['total_batches']} 批 (条目 {b['entry_ids']})")
        prompt_parts.append(f"读取 {BATCH_DIR}/{b['batch_number']:03d}.json")
        prompt_parts.append(f"调用: delegate_task(tasks=<该文件中的 tasks 数组>)")
        prompt_parts.append(f"完成后对 entry_ids={b['entry_ids']} 中的每个 id:")
        prompt_parts.append(f"  read_file /tmp/dtn_summaries/{{id}}.json")
        prompt_parts.append(f"  验证: 文件存在,含非空 summary 和 angle")
        prompt_parts.append(f"  若失败: 重新单独委派该条一次;仍失败则标记'摘要生成失败'")
        prompt_parts.append(f"")

    prompt_parts.append(
        f"所有批次完成后:\n"
        f"1. 运行: python3 scripts/dedup_store.py mark-summarized "
        f"--keys-file digests/digest_final.json\n"
        f"2. 整合 /tmp/dtn_summaries/*.json 到 digest_final.json 和 digest_final.md\n"
        f"3. 把 digest_final.md 内容呈现给用户"
    )

    prompt_path = os.path.join(BATCH_DIR, "PROMPT.md")
    with open(prompt_path, "w", encoding="utf-8") as f:
        f.write("\n".join(prompt_parts))

    print(f"dispatch: {len(entries)} entries → {len(batches)} batches")
    print(f"批处理文件: {BATCH_DIR}/001.json ~ {BATCH_DIR}/{len(batches):03d}.json")
    print(f"派发提示:  {BATCH_DIR}/PROMPT.md")
    print(f"摘要输出:  {SUMMARY_DIR}/")
    print(f"")
    print("主 agent 操作指南:")
    print("1. 读取 PROMPT.md 或 001.json")
    print("2. 按 prompt 指示调用 delegate_task")
    print("3. 每批完成后验证 /tmp/dtn_summaries/*.json")
    print("4. 重复直到所有批次完成")


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--digest", default="/tmp/dtn_digest_raw.json", help="digest_raw.json 路径"
    )
    ap.add_argument(
        "--batch-size", type=int, default=3, help="每批 sub-agent 数量 (默认 3)"
    )
    args = ap.parse_args()

    generate_batches(args.digest, args.batch_size)
