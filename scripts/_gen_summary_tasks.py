#!/usr/bin/env python3
"""Generate batch task files for delegate_task. Each sub-agent writes ONE consolidated JSON file."""
import json, os, glob

RAW = '/tmp/dtn_digest_raw.json'
OUT_DIR = '/tmp/dtn_batches'
existing = set()
for f in glob.glob('/tmp/dtn_summaries/*.json'):
    try:
        existing.add(int(os.path.basename(f).replace('.json','')))
    except:
        pass

entries = json.load(open(RAW))['entries']
total = len(entries)
missing = [e for e in entries if e['id'] not in existing]
print("Total: %d, Existing: %d, Missing: %d" % (total, len(existing), len(missing)))

NUM_BATCHES = 3
batch_size = (len(missing) + NUM_BATCHES - 1) // NUM_BATCHES

os.makedirs(OUT_DIR, exist_ok=True)

tasks = []
for b in range(NUM_BATCHES):
    start = b * batch_size
    end = min(start + batch_size, len(missing))
    batch = missing[start:end]
    ids = [e['id'] for e in batch]
    count = len(batch)
    min_id = min(ids)
    max_id = max(ids)
    print("Batch %d: ids %d-%d, count=%d" % (b, min_id, max_id, count))
    
    batch_entries = []
    for e in batch:
        raw = (e.get('raw_summary', '') or '')[:500]
        batch_entries.append({
            "id": e['id'],
            "title": e.get('title', ''),
            "source": e.get('source', ''),
            "stream": e.get('stream', ''),
            "url": e.get('url', ''),
            "raw_summary": raw
        })
    
    entries_json = json.dumps(batch_entries[:10], ensure_ascii=False, indent=2)
    entries_detail_lines = []
    for e in batch:
        eid = e['id']
        title = e.get('title', '')
        source = e.get('source', '')
        stream = e.get('stream', '')
        url = e.get('url', '')
        raw = (e.get('raw_summary', '') or '')[:400]
        entries_detail_lines.append("--- entry [%d] ---" % eid)
        entries_detail_lines.append("  source: %s" % source)
        entries_detail_lines.append("  stream: %s" % stream)
        entries_detail_lines.append("  url: %s" % url)
        entries_detail_lines.append("  title: %s" % title)
        entries_detail_lines.append("  raw_summary: %s" % raw)
    
    entries_block = "\n".join(entries_detail_lines)
    
    context_parts = [
        "你有 %d 条科技新闻条目需要处理（批次 %d/%d，id范围 %d-%d）。" % (count, b+1, NUM_BATCHES, min_id, max_id),
        "",
        "条目数据：",
        "",
        entries_block,
        "",
        "--- 对每一条条目执行以下操作 ---",
        "",
        "1. **可用性检查**：如果 raw_summary 为空、仅含无关符号、或内容太短/残缺导致无法写出有意义的摘要，将该条目标记为 SKIPPED。",
        "",
        "2. **相关性判断**：",
        "   - 保留：投资/融资/赚钱、AI/大模型/技术讨论、科技公司/行业动态、带转发评论的quote tweet",
        "   - 剔除：生活分享、心情吐槽、日常闲聊、搞笑段子",
        "   - 混合内容（生活+科技）保留，不确定时保留",
        "",
        "3. **写摘要**：正常条目写 2-4 句中文摘要，客观说明新闻内容",
        "",
        "4. **给角度**：从这些选一个：教程 | 观点解读 | 工具评测 | 事件分析 | 趋势洞察",
        "",
        "5. **输出**：生成一个 JSON 文件 /tmp/dtn_summaries/batch_%d_result.json，内容为字典 {\"id\": {\"summary\": \"...\", \"angle\": \"...\"}, ...}" % b,
        "   - 正常条目: {summary: 中文摘要, angle: 角度}",
        "   - 过滤的不相关条目: {summary: 'FILTERED: 内容不相关', angle: '无'}",
        "   - 不可用条目: {summary: 'SKIPPED: 已抓内容不可用', angle: '无'}",
        "   对所有 %d 条都输出。用 Python json.dump 写入。" % count,
        "",
        "**重要：对所有 %d 条都输出。不要逐条写文件，而是汇总成一个 JSON 字典写入**" % count,
    ]
    context = "\n".join(context_parts)
    
    task = {
        "goal": "为 %d 条科技新闻条目生成摘要和角度建议，输出到 /tmp/dtn_summaries/batch_%d_result.json" % (count, b),
        "context": context,
        "toolsets": ["file"],
        "role": "leaf",
    }
    tasks.append(task)

with open(os.path.join(OUT_DIR, 'summary_tasks.json'), 'w') as f:
    json.dump(tasks, f, ensure_ascii=False, indent=2)

print("Wrote %d tasks to %s/summary_tasks.json" % (NUM_BATCHES, OUT_DIR))
