#!/usr/bin/env python3
"""Split digest into 3 batches for parallel delegate_task and write task files."""
import json, os

RAW = '/tmp/dtn_digest_raw.json'
OUT_DIR = '/tmp/dtn_batches'

entries = json.load(open(RAW))['entries']
n = len(entries)
BATCHES = 3
batch_size = (n + BATCHES - 1) // BATCHES

os.makedirs(OUT_DIR, exist_ok=True)

for b in range(BATCHES):
    start = b * batch_size
    end = min(start + batch_size, n)
    batch = entries[start:end]
    ids = [e['id'] for e in batch]
    count = len(batch)
    min_id = min(ids)
    max_id = max(ids)
    print(f"Batch {b}: ids {min_id}-{max_id}, count={count}")
    
    entries_block = ""
    for e in batch:
        eid = e["id"]
        title = e.get("title", "")
        source = e.get("source", "")
        stream = e.get("stream", "")
        url = e.get("url", "")
        raw = e.get("raw_summary", "") or ""
        if len(raw) > 400:
            raw = raw[:400] + "..."
        entries_block += f"--- entry [{eid}] ---\n"
        entries_block += f"  source: {source}\n"
        entries_block += f"  stream: {stream}\n"
        entries_block += f"  url: {url}\n"
        entries_block += f"  title: {title}\n"
        entries_block += f"  raw_summary: {raw}\n"
    
    # Build example code as a string without f-string braces issues
    example_code = '''       import json
       result = {"id": ID_PLACEHOLDER, "summary": "...", "angle": "..."}
       with open(SUMMARY_PATH_PLACEHOLDER, "w", encoding="utf-8") as f:
           json.dump(result, f, ensure_ascii=False, indent=2)'''
    
    context = (
        "读取文件 /tmp/dtn_digest_raw.json，包含 " + str(count) + 
        " 条新闻条目（这是批次 " + str(b+1) + "/" + str(BATCHES) + "）。\n"
        "每条条目字段：id、title、url、source、stream、raw_summary。\n"
        "raw_summary 是上游抓取脚本已获取的内容（RSS description 或推文原文），你只需要基于它工作。\n\n"
        "所有条目列表：\n\n" + entries_block + "\n"
        "--- 以下是对每一条条目要执行的操作 ---\n\n"
        "1. **内容可用性检查**：如果 raw_summary 为空、仅含无关符号、或内容太短/残缺（如只有标题或截断的一句话）导致无法写出有意义的摘要，将该条目标记为 SKIPPED。\n\n"
        "2. **内容相关性判断**：\n"
        "   - 保留：投资/融资/赚钱、AI/大模型/技术讨论、科技公司/行业动态、带转发评论的quote tweet（作者写了推荐/转发原因）\n"
        "   - 剔除：生活分享、心情吐槽、日常闲聊、搞笑段子\n"
        "   - 宁多勿漏：混合信号（生活+科技）保留，不确定时保留\n"
        "   - 将判定为不相关的条目标记为 FILTERED。\n\n"
        "3. **写摘要**：对保留的条目，写一段 2-4 句的中文摘要，客观说明这条新闻/讨论是什么、为什么值得注意。摘要必须平台中立。\n\n"
        "4. **给角度**：为每条保留的条目从这些里选最合适的一个：教程 | 观点解读 | 工具评测 | 事件分析 | 趋势洞察\n\n"
        "5. **写结果文件**：对每一条条目（无论保留/跳过/过滤），用 Python 的 json.dump 写入 /tmp/dtn_summaries/{id}.json：\n"
        "   - 正常条目：id=N, summary=你的摘要, angle=你选的角度\n"
        "   - 跳过的条目：id=N, summary=SKIPPED: 已抓内容不可用, angle=无\n"
        "   - 过滤的条目：id=N, summary=FILTERED: 内容不相关, angle=无\n"
        "   使用 Python json.dump 写入，确保转义正确。\n\n"
        "**重要：逐条处理，对所有 " + str(count) + " 条都输出对应的 summary 文件。不要遗漏任何一条。**"
    )

    task = {
        "goal": "为 " + str(count) + " 条科技新闻条目逐条生成摘要和内容角度建议，输出到 /tmp/dtn_summaries/{id}.json 文件",
        "context": context,
        "toolsets": ["file"],
        "role": "leaf",
    }
    
    with open(os.path.join(OUT_DIR, 'task_' + str(b).zfill(3) + '.json'), 'w') as f:
        json.dump(task, f, ensure_ascii=False, indent=2)

print("Wrote " + str(BATCHES) + " task files to " + OUT_DIR)
