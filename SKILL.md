---
name: daily-tech-news
description: >
  每日科技新闻采集与摘要。抓取 RSS 科技源(OpenAI/Google/Meta/HN 等)+ X 关注用户
  + X 关键词热点,去重、按热度排序,然后为每条生成摘要和"内容角度建议",产出一份
  带编号的 digest 供用户挑选。当用户说"抓今天的新闻""今日科技热点""跑一下新闻摘要"
  等类似请求时使用本 skill。
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

### Step 1: 抓取 RSS + Hacker News(确定性脚本)

在 skill 目录下运行:

```bash
cd scripts
python3 fetch_rss.py --out /tmp/dtn_rss.json
```

输出包含 `items`(归一化条目)和 `report`(每个源的成功/失败状态)。
读取 report,若有源 FAIL,在最终 digest 的开头用一行注明哪些源没抓到
(例如:"注:Meta AI / TLDR 本次未抓取成功"),不要因为个别源失败就中止。

### Step 2: 抓取 X 关注用户推文(确定性脚本)

抓取 config.yaml 中 `following_stream.handles` 列出的账号,在时间窗内(默认24h)
发布的所有推文。通过 TwitterAPI.io 的 advanced_search,所有 handle 合并为一个
`(from:a OR from:b ...)` 查询,按返回推文条数计费(小用量极便宜)。

```bash
python3 fetch_x.py --out /tmp/dtn_x.json
# 临时改时间窗(覆盖配置,例如抓最近 48 小时):
# python3 fetch_x.py --window-hours 48 --out /tmp/dtn_x.json
```

- 关键词热点流本版本未启用(热度排序后续再做)。
- 若 `TWITTERAPI_KEY` 缺失、handles 为空、或抓取失败,report 会标明;
  此时继续用 RSS 数据,并在 digest 开头注明 X 流未成功/已跳过。
- 时间窗内没有推文是正常情况(不算失败)。

### Step 3: 合并、去重、排序(确定性脚本)

```bash
# 合并两份 items
python3 -c "
import json
a=json.load(open('/tmp/dtn_rss.json'))['items']
b=json.load(open('/tmp/dtn_x.json'))['items']
json.dump({'items':a+b}, open('/tmp/dtn_all.json','w'), ensure_ascii=False)
"
# 去重 + 存入 SQLite(自动跳过近期已见过的条目)
python3 dedup_store.py --in /tmp/dtn_all.json --out /tmp/dtn_new.json
# 对关键词流做热度排序(独立作者数 × 互动量)
python3 rank.py --in /tmp/dtn_new.json --out /tmp/dtn_topics.json
# 拆出 curated 流(RSS + following,不排热度)
python3 -c "
import json
items=json.load(open('/tmp/dtn_new.json'))['items']
curated=[i for i in items if i['stream'] in ('rss','following')]
json.dump({'items':curated}, open('/tmp/dtn_curated.json','w'), ensure_ascii=False)
"
# 组装 digest_raw.json
python3 build_digest.py --curated /tmp/dtn_curated.json --topics /tmp/dtn_topics.json --out /tmp/dtn_digest_raw.json
```

此时 `/tmp/dtn_digest_raw.json` 含所有条目,每条有:
`id`(编号)、`title`、`url`、`source`、`stream`、`raw_summary`、热点条目还有 `hotness`。
`summary` 和 `angle` 字段为 null —— 这是下一步要填的。

### Step 4: 逐条摘要(delegate_task 子 agent)

读取 `/tmp/dtn_digest_raw.json` 的 entries 数量。分批委派子 agent 做摘要,
**每批最多 3 个**(对应 Hermes 并发上限)。对每个条目:

调用 `delegate_task`,`tasks` 数组中每个元素:

- **goal**: "读取 /tmp/dtn_digest_raw.json 第 N 条(0-indexed),为它写一段中文摘要
  和一个内容角度建议,结果写入 /tmp/dtn_summaries/N.json"
- **context**(必须自包含,子 agent 看不到本对话):
  ```
  文件路径: /tmp/dtn_digest_raw.json
  目标条目: 第 N 条(entries[N])
  该条目有 url、title、raw_summary 字段。
  任务:
  1. 若 url 有效且可访问,用 web 工具抓取原文全文;若抓不到,基于 raw_summary。
  2. 写一段 2-4 句的中文摘要,客观说明这条新闻/讨论是什么、为什么值得注意。
     摘要必须平台中立(不要写成小红书/公众号风格,只是事实性总结)。
  3. 给一个角度建议,从这些里选最合适的一个:
     "教程" | "观点解读" | "工具评测" | "事件分析" | "趋势洞察"
  4. 把结果写入 /tmp/dtn_summaries/N.json,格式:
     {"id": N, "summary": "...", "angle": "...", "fetched_fulltext": true/false}
  只输出这个文件,不要做别的。
  ```
- **toolsets**: ["terminal", "file", "web"]
- **role**: "leaf"

委派前先 `mkdir -p /tmp/dtn_summaries`。

### Step 5: 验证 + 整合(主 agent,不信子 agent 的自报告)

子 agent 的 summary 字段是自报告的,**不可信**。必须实际回读文件验证:

1. 对每个 N,`read_file /tmp/dtn_summaries/N.json`,确认文件存在且含 summary + angle。
2. 若某条缺失或为空,重新委派该条一次;仍失败则在该条标注"摘要生成失败"。
3. 把每条的 summary + angle 填回对应 entry,生成两份最终文件:

   - **digest_final.json**(给下游平台 skill 读):完整 entries,含 id/title/url/
     source/stream/summary/angle/hotness。写到 `digests/digest_final.json`。
   - **digest_final.md**(给用户在 Telegram 看):每条一段,格式:
     ```
     #N [角度] 标题
        摘要文字
        来源: source | 链接: url
        (热点条目附:🔥 N 位作者讨论 · 互动 X)
     ```
     写到 `digests/digest_final.md`。

### Step 6: 呈现给用户

把 digest_final.md 的内容发给用户,并提示:
"看完后告诉我做哪条(例如'做 #7 的小红书版'),我会交给对应的内容生成 skill。"

## 重要原则

1. **脚本只做确定性的事**,摘要/判断全部交给子 agent。不要试图在脚本里加 LLM 调用。
2. **子 agent 的 goal 必须自包含**——它不知道本对话说过什么,所有信息走 context。
3. **始终回读文件验证**子 agent 的产出,不要相信它返回的 summary 字符串。
4. **个别源/流失败不中止**,降级继续并在 digest 开头注明。
5. digest 的 `summary` 必须平台中立,平台风格化是下游 skill 的事。
