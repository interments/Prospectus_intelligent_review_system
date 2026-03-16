from __future__ import annotations

import argparse
import json
import logging
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
import os
from functools import lru_cache

from dateutil import parser as dt_parser
from dateutil.relativedelta import relativedelta

from pydantic import BaseModel, Field
from langchain_core.output_parsers import PydanticOutputParser
from langchain_core.prompts import PromptTemplate
from langchain_core.runnables import RunnableLambda
from langchain_classic.output_parsers import OutputFixingParser

from app.core.llm import build_chat_llm
from app.shared.pdf.extractors import PdfRouter
from app.shared.pdf.schemas import TextBlock, TableBlock


class EventItem(BaseModel):
    event_type: str = Field(description="transfer/increase")
    event_date: str | None = None
    transferor: str | None = None
    transferee: str | None = None
    investor: str | None = None
    holder_name: str | None = None
    shares: str | None = None
    amount: str | None = None
    unit_price: str | None = None
    pct: str | None = None
    page_hint: int | None = None
    raw_text: str | None = None


class EventOutput(BaseModel):
    events: list[EventItem] = Field(default_factory=list)


def _to_decimal(raw: str | None) -> Decimal | None:
    if not raw:
        return None
    s = str(raw).replace(',', '').replace('，', '').strip()
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    if not m:
        return None
    try:
        return Decimal(m.group(0))
    except InvalidOperation:
        return None


def _parse_date(raw: str | None) -> date | None:
    if not raw:
        return None
    s = str(raw).strip()

    m = re.search(r"(20\d{2})\s*[年\-/]\s*(\d{1,2})\s*[月\-/]\s*(\d{1,2})\s*日?", s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            return date(y, mo, d)
        except Exception:
            return None

    m2 = re.search(r"(20\d{2})\s*[年\-/]\s*(\d{1,2})\s*月?", s)
    if m2:
        y, mo = int(m2.group(1)), int(m2.group(2))
        try:
            return date(y, mo, 1)
        except Exception:
            return None

    try:
        d = dt_parser.parse(s, fuzzy=True).date()
        if d.year < 1990 or d.year > 2100:
            return None
        return d
    except Exception:
        return None


def _unit_price_from_event(e: dict) -> Decimal | None:
    up = _to_decimal(e.get("unit_price"))
    if up is not None:
        return up
    amount = _to_decimal(e.get("amount"))
    shares = _to_decimal(e.get("shares"))
    if amount is not None and shares and shares != 0:
        return amount / shares
    return None


def _is_usd_text(s: str | None) -> bool:
    t = str(s or "").upper()
    return ("美元" in t) or ("USD" in t) or ("US$" in t)


def _detect_currency_from_event(e: dict) -> str:
    if _is_usd_text(e.get("unit_price")):
        return "USD"
    if _is_usd_text(e.get("amount")):
        return "USD"
    if _is_usd_text(e.get("raw_text")) and ("元/股" not in str(e.get("raw_text") or "")):
        return "USD"
    return "CNY"


def _get_usd_cny_fallback_rate() -> Decimal:
    raw = (os.getenv("USD_CNY_FALLBACK") or "7.20").strip()
    try:
        v = Decimal(raw)
        if v > 0:
            return v
    except Exception:
        pass
    return Decimal("7.20")


@lru_cache(maxsize=1)
def _load_usd_cny_df():
    """按日期加载USD/CNY历史汇率（可选依赖）。失败时返回None并走fallback。"""
    try:
        import akshare as ak  # type: ignore
        import pandas as pd  # type: ignore

        df = ak.macro_china_fx_usdcny()
        if df is None or df.empty:
            return None

        value_col = None
        for c in ["美元/人民币", "今值", "value", "收盘"]:
            if c in df.columns:
                value_col = c
                break
        if value_col is None:
            numerics = [c for c in df.columns if pd.api.types.is_numeric_dtype(df[c])]
            if not numerics:
                return None
            value_col = numerics[0]

        d2 = df[["日期", value_col]].rename(columns={value_col: "rate"}).copy()
        d2["日期"] = pd.to_datetime(d2["日期"]).dt.date
        d2["rate"] = pd.to_numeric(d2["rate"], errors="coerce")
        d2 = d2.dropna(subset=["rate"]).sort_values("日期")
        return d2
    except Exception:
        return None


def _usd_to_cny_rate_by_date(d: date | None) -> Decimal:
    fallback = _get_usd_cny_fallback_rate()
    if d is None:
        return fallback

    df = _load_usd_cny_df()
    if df is None:
        return fallback

    try:
        exact = df[df["日期"] == d]
        if not exact.empty:
            return Decimal(str(exact.iloc[-1]["rate"]))

        prev = df[df["日期"] <= d]
        if not prev.empty:
            return Decimal(str(prev.iloc[-1]["rate"]))

        return Decimal(str(df.iloc[0]["rate"]))
    except Exception:
        return fallback


def _fmt_price(v: Decimal | None) -> str:
    if v is None:
        return "-"
    return str(v.quantize(Decimal('0.001')))


def _event_desc(x: dict) -> str:
    if x.get("event") == "股权转让":
        return f"{x.get('transferor') or '未知'}与{x.get('transferee') or '未知'}的股权转让事件"
    return f"{x.get('transferee') or x.get('investor') or x.get('holder_name') or '未知'}的增资事件"


def _event_text(x: dict) -> str:
    if x.get("event") == "股权转让":
        return f"{x['time']} {x.get('transferor') or '未知'}向{x.get('transferee') or '未知'}股权转让，价格{_fmt_price(x['unit_price'])}元/股"
    return f"{x['time']} {x.get('transferee') or x.get('investor') or x.get('holder_name') or '未知'}增资，价格{_fmt_price(x['unit_price'])}元/股"


def _judge_alerts(timeline: list[dict]) -> list[dict]:
    if not timeline:
        return []
    rows = sorted(timeline, key=lambda x: x["time"])
    alerts: list[dict] = []
    for i in range(len(rows) - 1):
        a, b = rows[i], rows[i + 1]
        if a["unit_price"] is None or b["unit_price"] is None:
            continue
        if a["time"] == b["time"]:
            continue
        base = a["unit_price"]
        if base == 0:
            continue
        ratio = abs((b["unit_price"] - base) / base)
        short = b["time"] < (a["time"] + relativedelta(months=6))
        threshold = Decimal("0.05") if short else Decimal("0.15")
        if ratio > threshold:
            prev_desc = _event_desc(a)
            curr_desc = _event_desc(b)
            msg = (
                f"{a['time']}，{prev_desc} 与 {b['time']}，{curr_desc} 存在明显股价波动，"
                f"对应股价为{_fmt_price(a['unit_price'])}元/股与{_fmt_price(b['unit_price'])}元/股，"
                f"变动幅度{(ratio*Decimal('100')).quantize(Decimal('0.01'))}% ，需披露。"
            )
            alerts.append(
                {
                    "message": msg,
                    "page": b["page"],
                    "previous_price": _fmt_price(a["unit_price"]),
                    "current_price": _fmt_price(b["unit_price"]),
                    "change_ratio": str(ratio.quantize(Decimal('0.0001'))),
                    "previous_event_id": a.get("source_event_id") or f"event_{i}",
                    "current_event_id": b.get("source_event_id") or f"event_{i+1}",
                    "previous_event_page": a["page"],
                    "current_event_page": b["page"],
                    "previous_event_text": _event_text(a),
                    "current_event_text": _event_text(b),
                }
            )
    return alerts


def _norm(s: str) -> str:
    return (s or "").replace(" ", "").replace("\n", "")


@dataclass
class _Line:
    page: int
    text: str


def _to_lines(text_blocks: list[TextBlock]) -> list[_Line]:
    out: list[_Line] = []
    for b in text_blocks:
        for ln in (b.text or "").splitlines():
            t = ln.strip()
            if t:
                out.append(_Line(page=b.page, text=t))
    return out


def _is_tocish_line(t: str) -> bool:
    return bool(re.search(r"\.{6,}\s*\d+\s*$", t))


def _find_line(lines: list[_Line], pat: re.Pattern) -> int | None:
    for i, ln in enumerate(lines):
        if _is_tocish_line(ln.text):
            continue
        if pat.search(ln.text):
            return i
    return None


def _find_next_chapter(lines: list[_Line], start: int, chapter_re: re.Pattern) -> int | None:
    for i in range(start, len(lines)):
        if _is_tocish_line(lines[i].text):
            continue
        if chapter_re.search(lines[i].text):
            return i
    return None


_CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def _cn_to_int(s: str | None) -> int | None:
    if not s:
        return None
    s = s.strip()
    if s in _CN_NUM:
        return _CN_NUM[s]
    if s.startswith("十"):
        return 10 + (_CN_NUM.get(s[1:], 0) if len(s) > 1 else 0)
    if "十" in s:
        a, b = s.split("十", 1)
        return _CN_NUM.get(a, 0) * 10 + (_CN_NUM.get(b, 0) if b else 0)
    return None


def _heading_meta(line: str) -> tuple[int, str, int | None] | None:
    t = line.strip()
    m = re.match(r"^([一二三四五六七八九十]+)[、.．]\s*", t)
    if m:
        return (1, "cn_top", _cn_to_int(m.group(1)))
    m = re.match(r"^[（(]([一二三四五六七八九十]+)[）)]\s*", t)
    if m:
        return (2, "cn_paren", _cn_to_int(m.group(1)))
    m = re.match(r"^(\d+)[、.．]\s*", t)
    if m:
        return (3, "num_top", int(m.group(1)))
    m = re.match(r"^[（(](\d+)[）)]\s*", t)
    if m:
        return (4, "num_paren", int(m.group(1)))
    return None


def _find_next_peer_heading(lines: list[_Line], start: int, upper: int, style: str, level: int) -> int | None:
    for i in range(start, upper):
        if _is_tocish_line(lines[i].text):
            continue
        meta = _heading_meta(lines[i].text)
        if not meta:
            continue
        lv, st, _ = meta
        if lv == level and st == style:
            return i
    return None


def _split_chunks(text_blocks: list[TextBlock]) -> tuple[list[tuple[int, str]], list[int]]:
    """
    分层定位：
    1) 父章节：发行人/公司基本情况
    2) 命中“股本/股权...变化/变动”标题
    3) 若其内部存在更细且同主题标题，继续下钻
    4) 每层均按“当前标题到下一个同级标题”截取
    5) 在最终窗口内按子标题分段抽取
    """
    lines = _to_lines(sorted(text_blocks, key=lambda x: x.page))
    if not lines:
        return [], []

    main_re = re.compile(r"第[一二三四五六七八九十0-9]+[章节]\s*(?:发行人|公司)(?:的)?基本情况")
    chapter_re = re.compile(r"第[一二三四五六七八九十0-9]+[章节]")
    stock_re = re.compile(r"(股本|股权|股票).{0,14}(形成及其变化|形成和变化|变化情况|变动情况|变化|变动)")

    main_start = _find_line(lines, main_re)
    if main_start is None:
        return [], []
    main_end = _find_next_chapter(lines, main_start + 1, chapter_re) or len(lines)

    def find_stock_heading(start: int, end: int, min_level: int = 0) -> int | None:
        for i in range(start, end):
            if _is_tocish_line(lines[i].text):
                continue
            meta = _heading_meta(lines[i].text)
            if not meta:
                continue
            lv, _, _ = meta
            if lv <= min_level:
                continue
            if stock_re.search(lines[i].text):
                return i
        return None

    start_idx = find_stock_heading(main_start, main_end, min_level=0)
    if start_idx is None:
        return [], []

    # 逐层下钻到更细的“股本/股权变化”标题
    while True:
        start_meta = _heading_meta(lines[start_idx].text)
        if not start_meta:
            break
        lv, style, _ = start_meta
        end_idx = _find_next_peer_heading(lines, start_idx + 1, main_end, style=style, level=lv) or main_end
        child_idx = find_stock_heading(start_idx + 1, end_idx, min_level=lv)
        if child_idx is None:
            break
        start_idx = child_idx

    start_meta = _heading_meta(lines[start_idx].text)
    if not start_meta:
        return [], []
    lv, style, _ = start_meta
    end_idx = _find_next_peer_heading(lines, start_idx + 1, main_end, style=style, level=lv) or main_end

    target = lines[start_idx:end_idx]
    if not target:
        return [], []
    evidence_pages = sorted({x.page for x in target})

    # 在最终窗口内，按更下一级子标题切段（每段对应一个事件单元）
    split_indices = [0]
    for i in range(1, len(target)):
        meta = _heading_meta(target[i].text)
        if not meta:
            continue
        child_lv, _, _ = meta
        if child_lv > lv:
            split_indices.append(i)

    split_indices = sorted(set(split_indices))
    chunks: list[tuple[int, str]] = []
    for i, st in enumerate(split_indices):
        ed = split_indices[i + 1] if i + 1 < len(split_indices) else len(target)
        seg = target[st:ed]
        txt = "\n".join([x.text for x in seg]).strip()
        if txt:
            chunks.append((seg[0].page, txt))

    return chunks, evidence_pages


def _extract_events_with_langchain(chunks: list[tuple[int, str]], logger: logging.Logger) -> list[dict]:
    if not chunks:
        return []

    llm = build_chat_llm(temperature=0)
    parser = PydanticOutputParser(pydantic_object=EventOutput)
    fixing_parser = OutputFixingParser.from_llm(parser=parser, llm=llm)

    prompt_tmpl = PromptTemplate.from_template(
        "你是招股书股权变动抽取助手。请从文本中提取股份转让/增资扩股相关事件。\n"
        "输出要求：\n{format_instructions}\n\n"
        "补充规则：\n"
        "1) 只抽取与股权/股本变动相关事件；\n"
        "2) event_type 仅允许 transfer 或 increase；\n"
        "3) transfer 事件尽量填 transferor/transferee；increase 事件尽量填 investor；\n"
        "4) 尽量填充 event_date/holder_name/shares/amount/unit_price/pct；\n"
        "5) page_hint 用当前文本页码。\n\n"
        "page={page}\n文本：\n{text}"
    )

    chain = (
        RunnableLambda(lambda x: prompt_tmpl.format(**x))
        | llm
        | RunnableLambda(lambda m: m.content if isinstance(m.content, str) else str(m.content))
        | RunnableLambda(lambda raw: fixing_parser.parse(raw))
    )

    events: list[dict] = []
    for page, chunk in chunks:
        try:
            logger.info("llm call start: stage=price_event_extract page=%s", page)
            data: EventOutput = chain.invoke(
                {
                    "page": page,
                    "text": chunk,
                    "format_instructions": parser.get_format_instructions(),
                }
            )
            for e in data.events:
                item = e.model_dump()
                item["page_hint"] = item.get("page_hint") or page
                events.append(item)
            logger.info("llm call done: stage=price_event_extract page=%s events=%s", page, len(data.events))
        except Exception as e:
            logger.warning("llm call failed: stage=price_event_extract page=%s err=%s", page, str(e))

    # 简单去重
    seen = set()
    out = []
    for e in events:
        k = (e.get("event_type"), e.get("event_date"), e.get("holder_name"), e.get("amount"), e.get("pct"), e.get("page_hint"))
        if k in seen:
            continue
        seen.add(k)
        out.append(e)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Run price fluctuation extraction (LangChain, WIP)")
    parser.add_argument("--pdf", required=True)
    parser.add_argument("--workdir", required=True)
    parser.add_argument("--preprocessed", default="")
    args = parser.parse_args()

    pdf_path = Path(args.pdf).resolve()
    workdir = Path(args.workdir).resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / "logs").mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("price_fluctuation_langchain")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    fh = logging.FileHandler(workdir / "logs" / "run.log", encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(fh)

    logger.info("pipeline start: pdf=%s", pdf_path)

    preprocessed_path = Path(args.preprocessed).resolve() if args.preprocessed else None
    if preprocessed_path and preprocessed_path.exists():
        payload = json.loads(preprocessed_path.read_text(encoding="utf-8"))
        text_blocks = [TextBlock.model_validate(x) for x in payload.get("text_blocks", [])]
        table_blocks = [TableBlock.model_validate(x) for x in payload.get("table_blocks", [])]
        logger.info("extract reused: %s", preprocessed_path)
    else:
        router = PdfRouter()
        text_blocks, table_blocks = router.extract(pdf_path)

    (workdir / "preprocessed.json").write_text(
        json.dumps(
            {
                "text_blocks": [x.model_dump() for x in text_blocks],
                "table_blocks": [x.model_dump() for x in table_blocks],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    chunks, pages = _split_chunks(text_blocks)
    logger.info("section locate/segment done: pages=%s chunks=%s", len(pages), len(chunks))

    events = _extract_events_with_langchain(chunks, logger)

    fx_used: set[str] = set()
    timeline = []
    for idx, e in enumerate(events):
        d = _parse_date(e.get("event_date"))
        up = _unit_price_from_event(e)
        if d is None or up is None:
            continue

        currency = _detect_currency_from_event(e)
        if currency == "USD":
            fx = _usd_to_cny_rate_by_date(d)
            fx_used.add(str(fx))
            up = up * fx

        if up < Decimal('0.01') or up > Decimal('10000'):
            continue

        evt = "股权转让" if (e.get("event_type") or "").lower().startswith("transfer") else "增资"
        timeline.append(
            {
                "time": d,
                "event": evt,
                "event_type": e.get("event_type"),
                "holder_name": e.get("holder_name"),
                "transferor": e.get("transferor"),
                "transferee": e.get("transferee"),
                "investor": e.get("investor"),
                "amount": e.get("amount"),
                "shares": e.get("shares"),
                "unit_price": up,
                "currency": currency,
                "pct": e.get("pct"),
                "page": e.get("page_hint"),
                "raw_text": e.get("raw_text"),
                "source_event_id": f"{(e.get('event_type') or 'event').lower()}_{idx}",
            }
        )

    timeline = sorted(timeline, key=lambda x: x["time"])
    alerts = _judge_alerts(timeline)

    is_negative = (len(alerts) == 0)

    result = {
        "summary": {
            "runtime": "langchain",
            "status": "pass" if not alerts else "fail",
            "timeline_count": len(timeline),
            "alerts_count": len(alerts),
            "note": (
                "阈值判定已接入LangChain版（<6个月:5%, >=6个月:15%）；"
                + (f"USD按事件日期历史汇率折算（样本汇率: {', '.join(sorted(fx_used))}）" if fx_used else "未涉及USD折算")
            ),
            "negative_sample": is_negative,
            "negative_note": "未检测到明显价格波动告警" if is_negative else "检测到价格波动告警",
        },
        "timeline": [
            {
                "time": str(x["time"]),
                "event_type": x["event_type"],
                "holder_name": x["holder_name"],
                "amount": x["amount"],
                "shares": x["shares"],
                "unit_price": _fmt_price(x["unit_price"]),
                "currency": x.get("currency", "CNY"),
                "pct": x["pct"],
                "page": x["page"],
                "raw_text": x["raw_text"],
            }
            for x in timeline
        ],
        "alerts": alerts,
        "negative_output": {
            "is_negative": is_negative,
            "message": "阴性样本：未检测到明显价格波动告警" if is_negative else "非阴性样本：检测到明显价格波动告警",
        },
        "evidence_pages": pages,
    }

    (workdir / "timeline_raw.json").write_text(json.dumps(events, ensure_ascii=False, indent=2), encoding="utf-8")
    (workdir / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    logger.info("pipeline done: timeline=%s alerts=%s", len(timeline), len(alerts))
    print(f"Done. status={result['summary']['status']} timeline={len(timeline)} alerts={len(alerts)}")
    print(f"Artifacts: {workdir}")


if __name__ == "__main__":
    main()
