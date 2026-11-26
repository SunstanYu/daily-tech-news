"""
fetch_cac_filings.py — 监控网信办「生成式人工智能服务已备案信息」公告,解析新一期名单。

为什么是单个 URL:网信办不是每期发一个新网页,而是**在同一个公告页上不断追加附件**
(附件1 = 2024年4月,一直到现在,约每 2 个月一期)。所以判新 = 看附件号,
而不是比对文章列表。

判新用**水位线**,不是「这个附件见过没」:状态里只存处理过的最大附件号。
新一期 = 附件号比水位线更大。差别在状态丢了的时候 —— 按「见过没」判,空状态
会把 14 期历史全算成新的、挨个下 PDF;按水位线判,没有水位线就说明没有历史要补,
check 自动把水位线建在当前最新一期上,报 0 新增。所以不需要人工 seed。
另外只盯最新一期的标题有没有被换(网信办偶尔把刚发的附件换成修订版),
旧附件被改不再报 —— 发现了也不会去重发两年前那期。

本脚本只做确定性的事:抓页面、比对、下载、把 PDF 解析成结构化表格。
「这批公司都是做什么的」那种判断交给上层 agent,脚本里不碰 LLM。

子命令:
  check    抓公告页 → 解析附件列表 → 与状态文件比对。日报每天跑这个,成本 1 个请求。
  extract  下载新附件(或 --attachment N 指定一期)的 PDF 并解析成结构化 JSON。
  mark-seen 把水位线推到本次处理过的最大附件号,下次不再重复触发。

用法:
  python3 scripts/fetch_cac_filings.py check --out /tmp/cac_check.json
  python3 scripts/fetch_cac_filings.py extract --new --out /tmp/cac_latest.json
  python3 scripts/fetch_cac_filings.py mark-seen --from /tmp/cac_latest.json
"""
import os
import re
import sys
import json
import html
import hashlib
import argparse

import requests

from common import load_config, resolve_path

BASE = "https://www.cac.gov.cn"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# 附件链接长这样:/cms/pub/interact/downloadfile.jsp?filepath=<加密串>&fText=<文件名>
ATTACH_RE = re.compile(
    r'<a\s[^>]*href="([^"]*downloadfile\.jsp[^"]*)"[^>]*>(.*?)</a>', re.S)
# 「附件N」可能在 <a> 里,也可能在前面的文字里,两处都找
ATTACH_NO_RE = re.compile(r"附件\s*(\d+)")
PERIOD_RE = re.compile(r"（\s*(\d{4})\s*年\s*(\d{1,2})\s*月(?:\s*至\s*(\d{1,2})\s*月)?\s*）")

# PDF 里的表头 → 归一化的表名。一个附件里有好几张表,备案和登记的门槛不一样,
# 必须分开统计,不能混成一个数。
SECTION_PATTERNS = [
    (re.compile(r"提供手机端侧.*已备案信息"), "端侧备案"),
    (re.compile(r"拟人化互动服务场景.*已备案信息"), "拟人化备案"),
    (re.compile(r"生成式人工智能服务已备案变更信息"), "变更"),
    (re.compile(r"生成式人工智能服务已登记信息"), "登记"),
    (re.compile(r"生成式人工智能服务已备案信息"), "备案"),
]

# 行首序号。「新疆维吾尔自治区」这种长属地会被 PDF 折成三行,序号因此可能独占一行,
# 所以也接受裸数字 —— 但只在它正好等于「上一条 + 1」时才算,免得把页码当成记录。
ROW_START_RE = re.compile(r"^\s*(\d{1,5})(?:\s+(.*))?$")
# 备案编号:Prefix-Name-12位数字(登记号形如 ...-20260909S0011)。PDF 常把它折行,
# 所以匹配前先把空白压掉。
FILING_NO_RE = re.compile(r"([A-Za-z][A-Za-z0-9]*(?:-[A-Za-z0-9]+)*-\d{8}[A-Za-z]?\d{0,4})")
DATE_RE = re.compile(r"(\d{4}/\d{1,2}/\d{1,2})")
PROVINCE_RE = re.compile(r"^([一-鿿]{2,10}?(?:省|市|自治区|自治州|兵团))")
# 备案单位的结尾词。不能只列「有限公司」—— 名单里有图书馆、政务服务和数据管理局、
# 电视台、医院这些,漏一个就静默丢一家公司。
UNIT_SUFFIX = (r"(?:公司|工作室|事务所|研究院|研究所|设计院|出版社|日报社|"
               r"传媒|集团|控股|大学|学院|学校|医院|银行|中心|"
               r"图书馆|博物馆|美术馆|档案馆|科学技术馆|馆|"
               r"电视台|广播电台|台|管理局|数据局|局|委员会|协会|学会|基金会|"
               r"总站|电厂|工厂|厂|社|队|园|站)")
UNIT_TAIL_RE = re.compile(r"(?:集团|股份|有限|责任)*" + UNIT_SUFFIX + r"(?:（[^）]*）|\([^)]*\))?$")


# ---------------------------------------------------------------- 公告页

def fetch_announcement(url, timeout):
    r = requests.get(url, headers={"User-Agent": UA}, timeout=timeout)
    r.raise_for_status()
    r.encoding = r.apparent_encoding or "utf-8"
    return r.text


def parse_attachments(page_html):
    """从公告页 HTML 解析出附件列表(按附件号排序)。"""
    out = []
    for m in ATTACH_RE.finditer(page_html):
        href = html.unescape(m.group(1))
        label = re.sub(r"\s+", "", re.sub(r"<[^>]+>", "", m.group(2)))
        # 附件号:先看 <a> 自己的文字,没有就往前找最近的「附件N」
        no_match = ATTACH_NO_RE.search(label)
        if not no_match:
            before = re.sub(r"<[^>]+>", "", page_html[max(0, m.start() - 300):m.start()])
            tags = ATTACH_NO_RE.findall(before)
            no = int(tags[-1]) if tags else None
        else:
            no = int(no_match.group(1))
        period = None
        p = PERIOD_RE.search(label)
        if p:
            period = (f"{p.group(1)}年{int(p.group(2))}月"
                      if not p.group(3) else
                      f"{p.group(1)}年{int(p.group(2))}月至{int(p.group(3))}月")
        out.append({
            "no": no,
            "title": ATTACH_NO_RE.sub("", label).lstrip("：:").strip(),
            "period": period,
            "url": href if href.startswith("http") else BASE + href,
        })
    out.sort(key=lambda a: (a["no"] is None, a["no"] or 0))
    return out


def load_state(path):
    if not os.path.isfile(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_state(path, state):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def _set_watermark(path, att):
    """状态文件的全部内容就是这三个字段。标题留着是为了认出「刚发的附件被换了」。"""
    save_state(path, {"last_no": att["no"],
                      "last_title": att.get("title"),
                      "last_period": att.get("period")})


def diff_attachments(attachments, state):
    """返回 (新增的, 最新一期被换过的)。判据是水位线 last_no,见模块文档。

    没有水位线时返回空 —— 这不是「没有新的」,是「还不知道从哪算起」,
    由 cmd_check 负责建基线。在这里顺手建会让纯比对函数带上写状态的副作用。
    """
    last_no = state.get("last_no")
    if last_no is None:
        return [], []
    added = [a for a in attachments if (a["no"] or 0) > last_no]
    changed = []
    cur = next((a for a in attachments if a["no"] == last_no), None)
    if cur and state.get("last_title") and cur["title"] != state["last_title"]:
        changed = [{**cur, "prev_title": state["last_title"]}]
    return added, changed


# ---------------------------------------------------------------- PDF

def download_pdf(att, pdf_dir, timeout, announcement_url):
    os.makedirs(pdf_dir, exist_ok=True)
    # 下载接口要 Referer,否则返回的不是 PDF
    r = requests.get(att["url"],
                     headers={"User-Agent": UA, "Referer": announcement_url},
                     timeout=timeout)
    r.raise_for_status()
    data = r.content
    if not data.startswith(b"%PDF"):
        raise RuntimeError(f"附件{att['no']} 下载到的不是 PDF(前 8 字节 {data[:8]!r})")
    path = os.path.join(pdf_dir, f"attachment-{att['no']:02d}.pdf")
    with open(path, "wb") as f:
        f.write(data)
    return path, hashlib.sha256(data).hexdigest()


def pdf_text(path):
    try:
        from pypdf import PdfReader
    except ImportError:
        sys.exit("缺 pypdf,先装:pip install pypdf")
    reader = PdfReader(path)
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def _section_of(line):
    for pat, name in SECTION_PATTERNS:
        if pat.search(line):
            return name
    return None


def parse_pdf_rows(text):
    """把 PDF 文本切成「表 → 行」。

    PDF 里一条记录经常被折成好几行(单位名、备案编号都会断行),所以先按
    「行首是序号」把文本切成块,再在块内整体提字段 —— 不要指望一行一条。
    """
    sections = {}
    dropped = []
    current = "备案"          # 第一张表在表头出现前就可能有行
    pending_no, buf = None, []
    last_no = {}

    def flush():
        nonlocal current
        if pending_no is None:
            return
        blob = "".join(buf)   # 折行是行尾断开,空串拼接才能把被切开的词接回去
        row = _row_fields(pending_no, blob)
        if row:
            # 早几期的 PDF 不写表名,两张表直接接在一起。同一张表里序号只增不减,
            # 所以序号往回跳 = 换表了 —— 不靠表名也能切开,否则两张表的序号混在
            # 一起,缺号自检会报出一堆假阳性,真漏数据反而看不出来。
            if pending_no <= last_no.get(current, 0):
                base = current.split("#")[0]
                k = 2
                while f"{base}#{k}" in sections:
                    k += 1
                current = f"{base}#{k}"
            sections.setdefault(current, []).append(row)
            last_no[current] = pending_no
        else:
            # 解析不出字段的行必须报出来,不能当没看见 —— 静默丢一行就是丢一家公司
            dropped.append({"section": current, "no": pending_no, "raw": blob[:300]})

    for raw in text.split("\n"):
        line = raw.rstrip()
        if not line.strip():
            continue
        # 表头是独占一行的短标题。加长度/日期限制,免得正文里带「…已备案信息」
        # 字样的某一行被当成换表,把后面的记录全记到错的表里。
        sec = _section_of(line) if len(line.strip()) <= 40 and not DATE_RE.search(line) else None
        if sec and "序号" not in line:
            flush()
            pending_no, buf = None, []
            current = sec
            continue
        if "序号" in line and "属地" in line:      # 表头,跳过
            continue
        m = ROW_START_RE.match(line)
        rest = (m.group(2) or "").strip() if m else ""
        is_row_start = False
        if m:
            if rest and re.match(r"[一-鿿]", rest):
                is_row_start = True
            elif not rest:
                # 裸数字:只有接上序列才算记录开头,否则多半是页码。
                # 基准要用「正在攒的那条」的序号 —— last_no 要等 flush 才更新,
                # 用它会一直差一个,长属地折行的那几条就全漏了。
                prev = pending_no if pending_no is not None else last_no.get(current, 0)
                is_row_start = int(m.group(1)) == prev + 1
        if is_row_start:
            flush()
            pending_no = int(m.group(1))
            buf = [rest] if rest else []
        elif pending_no is not None:
            buf.append(line.strip())
    flush()
    return sections, dropped


def _row_fields(no, blob):
    """从一条记录的文本块里提字段。

    表的列序是「序号 属地 大模型名称 备案单位 备案编号 备案时间」。列与列之间是空格,
    折行只发生在行尾 —— 所以 buf 按空串拼接(修好折行),列分隔的空格保留下来,
    再按空格切列。不能把空白全压掉:压掉之后「新言 新疆希言人工智能有限公司」
    会粘成一个词,模型名和单位名就分不开了。

    备案单位紧挨在备案编号左边,所以从右往左攒 token,攒到以机构词结尾为止;
    「派欧云计算 （上海）有限公司」这种名字中间被 PDF 塞了空格的,靠「候选不能以
    左括号开头」继续往左吃一个 token。
    """
    text = re.sub(r"[ \t]+", " ", blob).strip()
    if not text:
        return None
    nospace = text.replace(" ", "")
    province = PROVINCE_RE.match(nospace)
    # 备案编号可能被折行切开(`ZheJiang-AnQiDaMoXing -202604130085`),所以容忍内部空格
    filing_m = re.search(r"[A-Za-z][A-Za-z0-9]*(?:\s*-\s*[A-Za-z0-9]+)*\s*-\s*\d{8}[A-Za-z]?\d{0,4}", text)
    date_m = DATE_RE.search(text)
    if not (filing_m or date_m):
        return None

    # 属地在 text 里可能带折行空格,用 nospace 的长度在 text 上走一遍定位
    start, seen = 0, 0
    if province:
        for i, ch in enumerate(text):
            if not ch.isspace():
                seen += 1
            if seen == len(province.group(1)):
                start = i + 1
                break
    end = filing_m.start() if filing_m else (date_m.start() if date_m else len(text))
    middle = text[start:end]
    # 备案编号的前缀(Beijing-SongYanDongLi 这截)经常被折行切下来,不同 pypdf 版本
    # 对它和单位名之间的空格处理还不一样 —— 7.x 会粘成一个词。备案单位是中文法人名,
    # 不可能以 ASCII 结尾,所以把尾巴上的 ASCII 串削掉,结果就与 pypdf 版本无关了。
    middle = re.sub(r"[A-Za-z0-9\-·\s]+$", "", middle)
    tokens = [t for t in middle.split(" ") if t]

    unit, model, parsed = None, None, False
    for take in range(1, len(tokens) + 1):
        cand = "".join(tokens[-take:])
        # 削掉机构后缀后还得剩下名字 ——「有限公司」削完是空的,它不是一家公司;
        # 也不能以左括号开头(「（上海）有限公司」是被 PDF 切开的后半截)
        if not UNIT_TAIL_RE.search(cand) or cand.startswith(("（", "(")):
            continue
        if len(UNIT_TAIL_RE.sub("", cand)) >= 2:
            unit = cand
            model = "".join(tokens[:-take]).strip() or None
            parsed = True
            break
    if unit is None and tokens:
        # 认不出机构词,但确实是一条记录。保留整段并标出来,让上层知道这条没切净,
        # 而不是把它丢掉 —— 丢一行就是丢一家公司。
        unit = "".join(tokens).strip() or None
    return {
        "no": no,
        "province": province.group(1) if province else None,
        "model": model,
        "unit": unit,
        "unit_parsed": parsed,
        "filing_no": re.sub(r"\s+", "", filing_m.group(0)) if filing_m else None,
        "filed_at": date_m.group(1) if date_m else None,
    }


def summarize(sections, dropped):
    """确定性统计 + 自检。行业归类需要理解语义,不在这里做 —— 交给上层 agent。

    自检这件事不能省:序号是连续的,所以「范围内缺了哪些号」直接暴露漏解析。
    网信办哪天换个 PDF 排版,这里会立刻报出来,而不是悄悄少几十家公司。
    """
    from collections import Counter
    out = {}
    for name, rows in sections.items():
        units = [r["unit"] for r in rows if r["unit"]]
        nos = sorted(r["no"] for r in rows)
        gaps = [n for n in range(nos[0], nos[-1] + 1) if n not in set(nos)] if nos else []
        out[name] = {
            "rows": len(rows),
            "unique_units": len(set(units)),
            "no_range": [nos[0], nos[-1]] if nos else None,
            "missing_nos": gaps,
            "unit_unparsed": sum(1 for r in rows if not r.get("unit_parsed")),
            "by_province": Counter(r["province"] for r in rows if r["province"]).most_common(),
        }
    out["_dropped"] = dropped
    return out


# ---------------------------------------------------------------- 命令

def cmd_check(cfg, args):
    url = cfg["url"]
    page = fetch_announcement(url, cfg["timeout_seconds"])
    atts = parse_attachments(page)
    state_path = resolve_path(cfg["state_path"])
    state = load_state(state_path)
    if state.get("last_no") is None and atts:
        # 状态丢了/第一次跑。历史名单不需要补发,所以把水位线建在当前最新一期上,
        # 本次报 0 新增。少了这一步,要么挨个下 14 个 PDF,要么永远静默不触发。
        _set_watermark(state_path, atts[-1])
        print(f"没有水位线,已按当前最新({atts[-1]['title']})建基线,本次不触发")
        state = load_state(state_path)
    added, changed = diff_attachments(atts, state)
    out = {
        "source_url": url,
        "attachment_count": len(atts),
        "latest": atts[-1] if atts else None,
        "has_new": bool(added or changed),
        "new_attachments": added,
        "changed_attachments": changed,
        "all_attachments": atts,
    }
    _emit(out, args.out)
    print(f"附件 {len(atts)} 个,最新:{atts[-1]['title'] if atts else '-'}"
          f" | 新增 {len(added)} 个,内容变更 {len(changed)} 个")
    return out


def cmd_extract(cfg, args):
    url = cfg["url"]
    page = fetch_announcement(url, cfg["timeout_seconds"])
    atts = parse_attachments(page)
    if args.attachment:
        targets = [a for a in atts if a["no"] == args.attachment]
        if not targets:
            sys.exit(f"没有附件{args.attachment}(当前共 {len(atts)} 个)")
    elif args.new:
        state = load_state(resolve_path(cfg["state_path"]))
        added, changed = diff_attachments(atts, state)
        targets = added + changed
        if not targets:
            _emit({"has_new": False, "batches": []}, args.out)
            print("没有新附件,无需解读")
            return
    else:
        targets = atts[-1:]

    pdf_dir = resolve_path(cfg["pdf_dir"])
    batches = []
    for att in targets:
        path, digest = download_pdf(att, pdf_dir, cfg["timeout_seconds"], url)
        sections, dropped = parse_pdf_rows(pdf_text(path))
        batches.append({
            **att,
            "pdf_path": path,
            "sha256": digest,
            "stats": summarize(sections, dropped),
            "sections": sections,
        })
        s = batches[-1]["stats"]
        print(f"附件{att['no']} {att['title'][:40]}")
        for name, st in s.items():
            if name == "_dropped":
                continue
            warn = ""
            if st["missing_nos"]:
                warn += f"  ⚠ 缺号 {len(st['missing_nos'])} 个: {st['missing_nos'][:8]}"
            if st["unit_unparsed"]:
                warn += f"  ⚠ 单位名未切净 {st['unit_unparsed']} 条"
            print(f"    {name:<8} {st['rows']:>4} 条 / {st['unique_units']:>4} 家单位"
                  f"  序号 {st['no_range']}{warn}")
        if dropped:
            print(f"    ⚠ 整行解析失败 {len(dropped)} 条: "
                  f"{[(d['section'], d['no']) for d in dropped[:8]]}")
    _emit({"has_new": True, "batches": batches}, args.out)
    return {"batches": batches}


def cmd_mark_seen(cfg, args):
    path = resolve_path(cfg["state_path"])
    with open(args.from_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    batches = data.get("batches") or data.get("new_attachments") or []
    if not batches:
        print("输入里没有 batches,水位线不动")
        return
    # 只往前推,不回退 —— 重跑一期旧附件(extract --attachment N)不该把水位线拉回去
    top = max(batches, key=lambda b: b.get("no") or 0)
    state = load_state(path)
    if (top.get("no") or 0) <= (state.get("last_no") or 0):
        print(f"水位线已在附件{state['last_no']},不回退")
        return
    _set_watermark(path, top)
    print(f"水位线推到附件{top['no']}({top.get('title')}),状态文件:{path}")


def _emit(obj, out_path):
    if out_path:
        os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("check", help="只判新,不下载")
    p.add_argument("--out", default=None)

    p = sub.add_parser("extract", help="下载 PDF 并解析成结构化 JSON")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--new", action="store_true", help="只处理未见过的附件")
    g.add_argument("--attachment", type=int, help="指定附件号")
    p.add_argument("--out", default=None)

    p = sub.add_parser("mark-seen", help="把已处理的附件写进状态")
    p.add_argument("--from", dest="from_file", required=True,
                   help="check 或 extract 的输出 JSON")


    args = ap.parse_args()
    cfg = (load_config(args.config) or {}).get("cac_filings") or {}
    if not cfg.get("url"):
        sys.exit("config.yaml 里缺 cac_filings.url")
    cfg.setdefault("timeout_seconds", 60)
    cfg.setdefault("state_path", "./data/cac_seen.json")
    cfg.setdefault("pdf_dir", "./data/cac_pdf")

    {"check": cmd_check, "extract": cmd_extract,
     "mark-seen": cmd_mark_seen}[args.cmd](cfg, args)


if __name__ == "__main__":
    main()
