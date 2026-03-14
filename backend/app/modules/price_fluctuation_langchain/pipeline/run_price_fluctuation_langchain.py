from __future__ import annotations

import argparse
import json
import logging
import re
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from dateutil import parser as dt_parser
from dateutil.relativedelta import relativedelta

from pydantic import BaseModel, Field
from langchain_core.output_parsers import PydanticOutputParser
from langchain_core.prompts import PromptTemplate
from langchain_core.runnables import RunnableLambda
from langchain_classic.output_parsers import OutputFixingParser

from app.core.llm import build_chat_llm
from app.modules.price_fluctuation.extractors.pdf_router import PdfRouter
from app.modules.price_fluctuation.schemas.models import TextBlock, TableBlock


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


def _locate_pages(text_blocks: list[TextBlock]) -> list[int]:
    # 初版：父章节 + 关键词页粗定位
    blocks = sorted(text_blocks, key=lambda x: x.page)
    parent_re = re.compile(r"第[一二三四五六七八九十百零〇\d]+[节章]\s*(发行人|公司)基本情况")
    section_re = re.compile(r"第[一二三四五六七八九十百零〇\d]+[节章]")
    kw = ["股本", "股权", "变动", "增资", "转让", "历次"]

    parent_idx = None
    for i, b in enumerate(blocks):
        t = b.text or ""
        if "......" in t:
            continue
        if parent_re.search(t):
            parent_idx = i
            break

    search_start = parent_idx or 0
    search_end = len(blocks)
    if parent_idx is not None:
        for j in range(parent_idx + 1, len(blocks)):
            if section_re.search(blocks[j].text or ""):
                search_end = j
                break

    pages = []
    for b in blocks[search_start:search_end]:
        t = _norm(b.text or "")
        if any(k in t for k in kw):
            pages.append(b.page)

    return sorted(set(pages))


def _split_chunks(text_blocks: list[TextBlock], pages: list[int], max_chars: int = 3000) -> list[tuple[int, str]]:
    chunks: list[tuple[int, str]] = []
    for b in text_blocks:
        if b.page not in pages:
            continue
        txt = (b.text or "").strip()
        if not txt:
            continue
        if len(txt) <= max_chars:
            chunks.append((b.page, txt))
            continue
        i = 0
        while i < len(txt):
            chunks.append((b.page, txt[i : i + max_chars]))
            i += 2600
    return chunks


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
                    "text": chunk[:6000],
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

    pages = _locate_pages(text_blocks)
    chunks = _split_chunks(text_blocks, pages)
    logger.info("section locate/segment done: pages=%s chunks=%s", len(pages), len(chunks))

    events = _extract_events_with_langchain(chunks, logger)

    timeline = []
    for idx, e in enumerate(events):
        d = _parse_date(e.get("event_date"))
        up = _unit_price_from_event(e)
        if d is None or up is None:
            continue
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
            "note": "阈值判定已接入LangChain版（<6个月:5%, >=6个月:15%）",
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
