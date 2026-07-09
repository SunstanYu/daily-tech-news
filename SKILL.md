---
name: daily-tech-news
description: >
  每日科技新闻采集与摘要。抓取 RSS 科技源(OpenAI/Google/Meta/HN 等)+ X 关注用户
  + X 关键词热点,去重、按热度排序,然后为每条生成摘要和"内容角度建议",产出一份
  带编号的 digest 供用户挑选。当用户说"抓今天的新闻""今日科技热点""跑一下新闻摘要"
  等类似请求时使用本 skill。
version: 2.1.0
---

# Daily Tech News Skill

本 skill 分两层:**确定性脚本层**(抓取/去重/排序,已写好,直接调用)和
**agent 摘要层**(用 delegate_task 委派子 agent 逐条摘要)。脚本不做任何 LLM 判断;
所有需要理解/总结/判断角度的工作,都交给摘要子 agent。

## 前置条件

- 环境变量 `TWITTERAPI_KEY` 已设置(用于 X 抓取)。若未设置,X 流会跳过但 RSS 流仍可用。
- 依赖:`pip install feedparser requests pyyaml`
- 所有可调参数在 `config.yaml`。不要在本文件或脚本里硬编码参数。

## 执行步骤

### Step 0: 环境预检

```bash
# Python 版本
/opt/bitnami/python/bin/python3 --version
# 依赖检查
/opt/bitnami/python/bin/python3 -c "import feedparser, requests, yaml; print('ok')"
# X API（可选，缺失时 X 流降级）
echo $([ -n "$TWITTERAPI_KEY" ] && echo "TWITTERAPI_KEY=SET" || echo "X fetch=SKIPPED")
```

### Step 1: 抓取 RSS + Hacker News（确定性脚本）

**以下命令在主 session shell 中执行，不在 delegate_task 子 agent 内。**

```bash
cd /home/bitnami/daily-tech-news
/opt/bitnami/python/bin/python3 scripts/fetch_rss.py --out /tmp/dtn_rss.json
```

输出包含 `items`(归一化条目)和 `report`(每个源的成功/失败状态)。
读取 report,若有源 FAIL,在最终 digest 的开头用一行注明哪些源没抓到
(例如:"注:Meta AI / TLDR 本次未抓取成功"),不要因为个别源失败就中止。

### Step 2: 抓取 X 关注用户推文（确定性脚本）

```bash
/opt/bitnami/python/bin/python3 fetch_x.py --out /tmp/dtn_x.json
# 临时改时间窗(覆盖配置,例如抓最近 48 小时):
# /opt/bitnami/python/bin/python3 fetch_x.py --window-hours 48 --out /tmp/dtn_x.json
```

- 关键词热点流本版本未启用(热度排序后续再做)。
- 若 `TWITTERAPI_KEY` 缺失、handles 为空、或抓取失败,report 会标明;
  此时继续用 RSS 数据,并在 digest 开头注明 X 流未成功/已跳过。
- 时间窗内没有推文是正常情况(不算失败)。

### Step 2b: X 推文内容筛选（确定性脚本）

过滤抓取到的 X 推文，只保留与投资/科技/AI/商业赚钱相关的内容，剔除生活分享/搞笑推文等无关内容。**带转发评论的 quote tweet 保留。**

```bash
cd /home/bitnami/daily-tech-news
/opt/bitnami/python/bin/python3 scripts/filter_x_content.py --in /tmp/dtn_x.json --out /tmp/dtn_x_filtered.json
# 若 X 流为空或跳过，直接复制原文件：
cp /tmp/dtn_x.json /tmp/dtn_x_filtered.json
```

脚本说明：
- 保留：投资融资、AI/大模型/技术讨论、科技公司动态、商业赚钱相关内容
- 保留：带评论的转发推文（quote tweet，作者写了转发/推荐原因）
- 剔除：生活分享、心情吐槽、日常闲聊、搞笑段子
- 混合内容（既有生活又有科技）**保留**（宁多勿漏）
- 过滤报告会写入 output JSON 的 report 字段

### Step 3: 合并、去重、排序（确定性脚本）

```bash
# 合并两份 items（使用过滤后的 X 数据）
/opt/bitnami/python/bin/python3 -c "
import json
a=json.load(open('/tmp/dtn_rss.json'))['items']
b=json.load(open('/tmp/dtn_x_filtered.json'))['items']
json.dump({'items':a+b}, open('/tmp/dtn_all.json','w'), ensure_ascii=False)
"
# 去重 + 过滤已摘要的条目
# 注意：dedup=0 不代表没有待处理条目！已入库但未摘要的条目会出现在 filter-pending/db-pending 中。
# 即使 dedup=0，也必须继续走 filter-pending → build_digest 流程。
/opt/bitnami/python/bin/python3 dedup_store.py --in /tmp/dtn_all.json --out /tmp/dtn_new.json
# 方法1：从文件去重流提取 pending（适用于新抓取的条目）
/opt/bitnami/python/bin/python3 dedup_store.py filter-pending --in /tmp/dtn_new.json --out /tmp/dtn_pending.json
# 方法2（推荐）：直接从 DB 提取所有未摘要条目，不依赖 dedup 结果
# 这确保即使 dedup=0，之前抓了但没做摘要的条目也不会被遗漏
/opt/bitnami/python/bin/python3 dedup_store.py db-pending --out /tmp/dtn_pending.json --window-days 7

# 对关键词流做热度排序（当前为空，不影响流程）
/opt/bitnami/python/bin/python3 rank.py --in /tmp/dtn_pending.json --out /tmp/dtn_topics.json
# 拆出 curated 流(RSS + following,不排热度)
/opt/bitnami/python/bin/python3 -c "
import json
items=json.load(open('/tmp/dtn_pending.json'))['items']
curated=[i for i in items if i['stream'] in ('rss','following')]
json.dump({'items':curated}, open('/tmp/dtn_curated.json','w'), ensure_ascii=False)
"
# 组装 digest_raw.json
/opt/bitnami/python/bin/python3 build_digest.py --curated /tmp/dtn_curated.json --topics /tmp/dtn_topics.json --reports /tmp/dtn_rss.json,/tmp/dtn_x.json --out /tmp/dtn_digest_raw.json
```

此时 `/tmp/dtn_digest_raw.json` 含所有待摘要条目,每条有:
`id`(编号)、`title`、`url`、`source`、`stream`、`raw_summary`、`dedup_key`。
`summary` 和 `angle` 字段为 null —— 这是下一步要填的。

> **重要**: 判断是否需要继续流程的标准是 **pending 条目数**，不是 dedup 结果。
> - `dedup_store.py --in` 返回 `new=0` 是正常的（说明今天抓到的 URL 7 天内已入库过）。
> - 关键看 `db-pending` 或 `filter-pending` 的输出：如果 pending 条目数 > 0，继续走 Step 4-6。
> - 只有当 db-pending/filter-pending 也返回 0 条时，才跳到 Step 6 说"今日无新条目"。

### Step 4: 逐条摘要（delegate_task 子 agent，单任务汇总）

**无论今日抓到多少条新内容（哪怕只有 1 条），都必须走子 agent 做总结归类。不要自己跳过摘要或者直接手动写摘要。**

不要手动构造 delegate_task 参数。使用 `dispatch_summaries.py` 生成精确 JSON。

```bash
cd /home/bitnami/daily-tech-news
/opt/bitnami/python/bin/python3 scripts/dispatch_summaries.py --digest /tmp/dtn_digest_raw.json
```

该脚本将所有条目打包成 **1 个 sub-agent 任务**，因为子 agent 不需要抓网页（只做总结归类），单任务即可完成。

脚本输出:
- `/tmp/dtn_batches/001.json` — 包含唯一的 delegate_task tasks 数组
- `/tmp/dtn_batches/PROMPT.md` — 主 agent 的操作指令

**主 agent 操作流程（零理解成本）:**

1. 读取 `/tmp/dtn_batches/001.json`
2. 调用 `delegate_task(tasks=<001.json 的 tasks 数组>)`（只有 1 个任务）
3. 调用完成后，对所有 entry_ids 逐一读取 `/tmp/dtn_summaries/{id}.json` 验证文件存在且含非空 summary + angle
4. 若有遗漏，为该条单独重新委派一次；仍失败则标记"摘要生成失败"

**验证脚本:**
```bash
/opt/bitnami/python/bin/python3 scripts/verify_summaries.py
```
输出 `ALL OK` 表示全部通过；否则列出缺失/无效的条目 ID。

> **为什么不用多 batch 并行？** 子 agent 不抓网页，只做总结归类，单任务即可一次性处理所有条目。并行多 batch 只会浪费 token 和增加调度复杂度。

### Step 4b: 过滤子 Agent 标记的不相关/不可用条目

子 Agent 在摘要时会将不相关条目标记为 `"FILTERED: 内容不相关"`,内容残缺无法写摘要的标记为 `"SKIPPED: 已抓内容不可用"`。这些条目都不应进入最终 digest:

```bash
cd /home/bitnami/daily-tech-news
/opt/bitnami/python/bin/python3 -c "
import json, os
# 加载 digest_raw 的全部条目
raw = json.load(open('/tmp/dtn_digest_raw.json'))
entries = raw.get('entries', [])
kept = []
filtered = 0

for entry in entries:
    eid = entry['id']
    summary_file = f'/tmp/dtn_summaries/{eid}.json'
    if os.path.exists(summary_file):
        with open(summary_file) as f:
            summary_data = json.load(f)
        # 过滤掉标记为不相关或不可用的条目
        summary = summary_data.get('summary', '')
        if summary.startswith('FILTERED:') or summary.startswith('SKIPPED:'):
            filtered += 1
            os.remove(summary_file)
            continue
    kept.append(entry)

# 重写 digest_raw，只保留通过过滤的条目
for i, entry in enumerate(kept):
    entry['id'] = i
raw['entries'] = kept
raw['entry_count'] = len(kept)
with open('/tmp/dtn_digest_raw.json', 'w') as f:
    json.dump(raw, f, ensure_ascii=False, indent=2)
print(f'filtered out {filtered} entries, kept {len(kept)}')
"
```

注意过滤后需要重新给保留的条目分配连续的 id（从 0 开始），以保证 Step 5 整合时引用正确。

### Step 5: 整合最终 digest

把所有 `/tmp/dtn_summaries/N.json` 的 summary + angle 填回对应的 entry,生成两份最终文件:

1. **digest_final.json**(机器可读):完整 entries,含 id/title/url/source/stream/
   summary/angle/dedup_key。写到 `/home/bitnami/daily-tech-news/digests/digest_final.json`。

2. **digest_final.md**(用户可读):
   - 先检查 digest_final.json 中的 `notes` 字段，若有则在文件开头逐行写入
   - 然后每条一段,格式:
   ```
   #N [角度] 标题
      摘要文字
      来源: source | 链接: url
      (热点条目附:🔥 N 位作者讨论 · 互动 X)
   ```
   写到 `/home/bitnami/daily-tech-news/digests/digest_final.md`。

3. **标记已摘要**(避免下次重复消费):
   ```bash
   cd /home/bitnami/daily-tech-news
   /opt/bitnami/python/bin/python3 scripts/dedup_store.py mark-summarized --keys-file digests/digest_final.json
   ```

### Step 6: 输出到个人站点（确定性脚本）

将所有 `/tmp/dtn_summaries/N.json` 的 summary + angle 整合后，将 digest_final.json 转为
personal-site 站点的 news 格式并 git push：

```bash
cd /home/bitnami/daily-tech-news
/opt/bitnami/python/bin/python3 scripts/export_personal_site.py
```

该脚本读取 `digests/digest_final.json`，生成 `personal-site/src/content/news/YYYY-MM-DD.md`，
并在 personal-site 仓内自动 git commit + push 到 main。

**如果个人站点仓库不存在或 git push 失败，脚本会打印警告但不中止。**

### Step 7: 呈现给用户

把 digest_final.md 的内容发给用户，并提示：
"看完后告诉我做哪条（例如'做 #7 的小红书版'），我会交给对应的内容生成 skill。"

如果 Step 3 发现条目数为 0，直接告诉用户："今日无新条目，所有最近内容都已在之前的摘要中。"

## 重要原则

1. **脚本只做确定性的事**,摘要/判断全部交给子 agent。不要试图在脚本里加 LLM 调用。
2. **子 agent 的 goal 必须自包含**——它不知道本对话说过什么,所有信息走 context。
3. **始终回读文件验证**子 agent 的产出,不要相信它返回的 summary 字符串。
4. **个别源/流失败不中止**,降级继续并在 digest 开头注明。
5. digest 的 `summary` 必须平台中立,平台风格化是下游 skill 的事。
6. **Step 1-3（含 Step 2b）的命令在主 session shell 执行**,不用 delegate_task 子 agent 跑。
7. **摘要子 agent 只有 1 个任务，不并行分批**——所有条目打包成单个 task，一次性完成总结归类。
