# Daily Tech News Skill — 阶段一(确定性脚本层)

每日科技新闻采集 skill 的确定性部分:抓取 → 去重 → 热度排序 → 组装 digest。
摘要那一步(由 Hermes 的 delegate_task 子 agent 完成)属于阶段二,暂未包含。

## 目录结构

```
daily-tech-news/
├── config.yaml              # 所有可调参数集中在此,改这里即可
├── scripts/
│   ├── common.py            # 配置加载 / 统一数据结构 / 去重key / HTML清洗
│   ├── fetch_rss.py         # RSS + Hacker News 抓取(type分发处理器)
│   ├── fetch_x.py           # TwitterAPI.io 抓 X 关注用户(时间窗内推文)
│   ├── dedup_store.py       # SQLite 去重 + 存储 + 历史
│   ├── rank.py              # 热度排序(独立作者数 × 互动量)
│   └── build_digest.py      # 组装 digest_raw.json
├── tests/
│   ├── test_logic.py        # 离线逻辑测试(mock,不联网不花钱)
│   └── mocks/               # 假数据
├── data/                    # SQLite 运行时生成
└── digests/                 # digest 输出
```

## 安装依赖

```bash
pip install feedparser requests pyyaml pytest
```

## 1. 先跑离线逻辑测试(不联网、不花钱)

```bash
cd tests
python3 -m pytest test_logic.py -v -s
```

应全部 12 个通过。重点看 `test_ranking_unique_authors_beats_single_spammer`——
它证明多作者话题能压过单账号刷屏。

## 2. 测试真实联网数据

### 2a. RSS 源连通性(免费)

逐个源打印 通/不通 + 拉到几条 + 示例标题。这是你确认 feed URL 有效性的工具:

```bash
cd scripts
python3 fetch_rss.py --report-only
```

预期:OpenAI / Google Research / Hacker News 大概率 OK;
Meta AI / TLDR AI / The Rundown 可能 FAIL(feed URL 不确定)。
FAIL 的源去 config.yaml 换 URL,或启用注释里的备用源
(Hugging Face / DeepMind / The Decoder)。

单独测一个源:
```bash
python3 fetch_rss.py --only "OpenAI" --report-only
```

拉取真实数据存文件:
```bash
python3 fetch_rss.py --out /tmp/rss_items.json
```

### 2b. X 关注用户抓取(需要 TwitterAPI.io token)

先在 config.yaml 填入要抓的账号(following_stream.handles,不带@),然后设置 token:
```bash
export TWITTERAPI_KEY=your_token_here
```

先只看会发出什么 query(不调 API、不花钱):
```bash
python3 fetch_x.py --print-query
```

真实抓取(TwitterAPI.io 按返回推文条数计费 $0.15/1000 条,关注用户的小用量极便宜):
```bash
python3 fetch_x.py --out /tmp/x_items.json
# 改时间窗(默认24h),例如最近48小时:
python3 fetch_x.py --window-hours 48 --out /tmp/x_items.json
```

说明:
- handles 为空 → 自动跳过(不报错)。
- 时间窗内没有推文 → 正常,不算失败。
- 关键词热点流本版本未启用(热度排序后续再做)。

### 2c. 完整链路(真实数据)

```bash
cd scripts
# 1. 抓 RSS + HN
python3 fetch_rss.py --out /tmp/rss.json
# 2. 抓 X 关注用户(需 TWITTERAPI_KEY + config 里填了 handles)
python3 fetch_x.py --out /tmp/x.json
# 3. 合并两份的 items 后去重(下面用 python 合并)
python3 -c "
import json
a=json.load(open('/tmp/rss.json'))['items']
b=json.load(open('/tmp/x.json'))['items']
json.dump({'items':a+b}, open('/tmp/all.json','w'), ensure_ascii=False)
"
python3 dedup_store.py --in /tmp/all.json --out /tmp/new.json
# 4. 本版本未启用关键词热点流,topics 传空
echo '{"topics": []}' > /tmp/topics.json
# 5. 组装 digest(curated = 去重后的 RSS/following)
python3 -c "
import json
items=json.load(open('/tmp/new.json'))['items']
curated=[i for i in items if i['stream'] in ('rss','following')]
json.dump({'items':curated}, open('/tmp/curated.json','w'), ensure_ascii=False)
"
python3 build_digest.py --curated /tmp/curated.json --topics /tmp/topics.json
# -> 输出到 digests/digest_raw.json
```

## 配置说明

全部参数在 `config.yaml`,常改的:
- `rss_sources`: 增删源(标准 RSS 只需加一行 type: rss + url)
- `following_stream.handles`: 你关注的账号用户名(不带@,默认空)
- `following_stream.window_hours`: 抓取时间窗(默认 24 小时)
- `following_stream.exclude_retweets / exclude_replies`: 是否过滤转推/回复
- `twitterapi.token_env`: token 的环境变量名(默认 TWITTERAPI_KEY)
- `keywords_stream.*`: 关键词热点流(本版本关闭,后续做热度排序时再启用)

## 已知限制

- 聚类是 v1 轻量版(hashtag/cashtag/关键词),不做语义聚类
- velocity(跨天增长率)未实现,当前是绝对热度
- 摘要步骤属于阶段二(SKILL.md + delegate_task)

## 当前问题

### 离线逻辑测试: test_following_query_none_when_empty 失败

```
FAILED tests/test_logic.py::test_following_query_none_when_empty
```

**原因:** 该测试调用 `load_cfg()` 加载真实 config.yaml,预期 `handles` 为空列表(应返回 `None`)。但 config.yaml 中 `following_stream.handles` 已被用户设置为 `["sama", "ylecun"]`,导致返回了构造好的查询字符串而非 `None`。

**修复方案:** 测试中应在调用 `build_following_query` 前显式设置 `cfg["following_stream"]["handles"] = []`,不依赖 config 默认值。

### 在线真实数据测试

| 模块 | 状态 | 说明 |
|---|---|---|
| RSS 抓取 | ✅ 4/6 源可用 | OpenAI(1020), Google Research(100), TLDR AI(20), Hacker News(30) 正常; Meta AI 和 The Rundown AI 均返回 404 |
| X following 抓取 | ✅ 正常 | 从 ylecun/sama 抓取到 4 条推文,含完整 metrics |
| SQLite 去重 | ✅ 正常 | 首次 1170 new/0 dup,二次 0 new/1170 dup,去重逻辑正确 |
| Meta AI RSS | ❌ 404 | `ai.meta.com/blog/rss/` 无公开 feed |
| The Rundown RSS | ❌ 404 | `www.therundown.ai/feed` 无公开 feed |
