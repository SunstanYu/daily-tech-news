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
│   ├── fetch_x.py           # Apify 抓 X(关键词流 + 关注流)
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

### 2b. X 抓取(需要 Apify token,会花少量钱)

先设置 token(不会写入任何文件):
```bash
export APIFY_TOKEN=your_apify_token_here
```

先只看会发出什么 query(不调 API、不花钱):
```bash
python3 fetch_x.py --print-queries
```

真实抓取(会调 Apify,按 $0.40/1000 条计费):
```bash
python3 fetch_x.py --out /tmp/x_items.json
```

注意:关注流默认关闭(following_stream.enabled: false)。
要启用就在 config.yaml 填 handles 并改 enabled: true。

### 2c. 完整链路(真实数据)

```bash
cd scripts
# 1. 抓 RSS + HN
python3 fetch_rss.py --out /tmp/rss.json
# 2. 抓 X(需 APIFY_TOKEN)
python3 fetch_x.py --out /tmp/x.json
# 3. 合并两份的 items 后去重(下面用 python 合并)
python3 -c "
import json
a=json.load(open('/tmp/rss.json'))['items']
b=json.load(open('/tmp/x.json'))['items']
json.dump({'items':a+b}, open('/tmp/all.json','w'), ensure_ascii=False)
"
python3 dedup_store.py --in /tmp/all.json --out /tmp/new.json
# 4. 排序关键词流
python3 rank.py --in /tmp/new.json --out /tmp/topics.json
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
- `keywords_stream.keywords`: 关键词(默认 ["ai","agent"])
- `keywords_stream.min_faves`: X 互动阈值(默认 300)
- `keywords_stream.unique_authors_weight / engagement_weight`: 热度权重
- `following_stream.handles`: 你关注的账号(默认空)

## 已知限制

- 聚类是 v1 轻量版(hashtag/cashtag/关键词),不做语义聚类
- velocity(跨天增长率)未实现,当前是绝对热度
- 摘要步骤属于阶段二(SKILL.md + delegate_task)
