from __future__ import annotations

import argparse
import csv
import json
import logging
import re
from pathlib import Path

from pydantic import BaseModel, Field
from langchain_core.output_parsers import PydanticOutputParser
from langchain_core.prompts import PromptTemplate
from langchain_core.runnables import RunnableLambda
from langchain_classic.output_parsers import OutputFixingParser

from app.core.llm import build_chat_llm
from app.shared.pdf.extractors import PdfRouter
from app.shared.pdf.schemas import TextBlock, TableBlock


class YesNoOutput(BaseModel):
    disclosed: bool = Field(description="是否检测到质押/冻结相关披露（包含无事项声明）")


class EventItem(BaseModel):
    name: str
    person_type: str
    event_type: str
    event_status: str | None = None
    event_desc: str | None = None


class EventOutput(BaseModel):
    events: list[EventItem] = Field(default_factory=list)


def _norm(s: str) -> str:
    return (s or "").replace(" ", "").replace("\n", "")


def _split_text(content: str, chunk_size: int = 1800, overlap: int = 200) -> list[str]:
    if len(content) <= chunk_size:
        return [content] if content else []
    out = []
    i = 0
    while i < len(content):
        out.append(content[i : i + chunk_size])
        i += max(1, chunk_size - overlap)
    return out


def _locate_sections(text_blocks: list[TextBlock]) -> dict:
    blocks = sorted(text_blocks, key=lambda x: x.page)

    parent_heading_line = re.compile(r"^\s*第[一二三四五六七八九十百零〇\d]+节\s*(?:发行人|公司)基本情况\s*$", re.M)
    section_heading_line = re.compile(r"^\s*第[一二三四五六七八九十百零〇\d]+节\s*", re.M)

    # 标题在不同招股书里常见变体：
    # - “控制人的情况”/“控制人情况”
    # - “持有发行人5%以上股份的主要股东…” / “持股5%以上主要股东…”
    # - “董事、监事、高级管理人员及核心技术人员” 后缀可有可无（情况/简介/基本情况）
    s5_heading_line = re.compile(
        r"^\s*[（(]?[五六七567][）)]?、\s*"
        r"(?:持有发行人\s*5%\s*以上股份的主要股东|持股\s*5%\s*以上(?:股份的)?(?:主要)?股东)"
        r"\s*及\s*实际控制人(?:的)?(?:基本)?(?:情况)?\s*$",
        re.M,
    )
    mg_heading_line = re.compile(
        r"^\s*[（(]?[七八九789][）)]?、\s*"
        r"董事、监事、高级管理人员(?:与|及)核心技术人员(?:的)?(?:简要)?(?:基本)?(?:情况|简介)?\s*$",
        re.M,
    )
    peer_heading = re.compile(r"^\s*[一二三四五六七八九十]+、", re.M)

    parent_hits = [i for i, b in enumerate(blocks) if parent_heading_line.search(b.text or "")]
    search_start, search_end = 0, len(blocks)
    if parent_hits:
        search_start = parent_hits[-1]
        for j in range(search_start + 1, len(blocks)):
            if section_heading_line.search(blocks[j].text or ""):
                search_end = j
                break

    t5_hits, tmg_hits = [], []
    for i in range(search_start, search_end):
        raw = blocks[i].text or ""
        if s5_heading_line.search(raw):
            t5_hits.append(i)
        if mg_heading_line.search(raw):
            tmg_hits.append(i)

    if not t5_hits and not tmg_hits:
        for i, b in enumerate(blocks):
            raw = b.text or ""
            if s5_heading_line.search(raw):
                t5_hits.append(i)
            if mg_heading_line.search(raw):
                tmg_hits.append(i)

    t5_idx = t5_hits[-1] if t5_hits else None
    tmg_idx = tmg_hits[-1] if tmg_hits else None

    def collect(start_idx: int | None, max_pages: int, start_heading_re: re.Pattern) -> tuple[str, list[int]]:
        if start_idx is None:
            return "", []
        start_page = blocks[start_idx].page
        pages: list[int] = []
        parts: list[str] = []
        for j in range(start_idx, len(blocks)):
            b = blocks[j]
            raw = b.text or ""
            if b.page - start_page >= max_pages:
                break
            if j > start_idx and peer_heading.search(raw):
                break
            if j == start_idx:
                m = start_heading_re.search(raw)
                if m:
                    raw = raw[m.start() :]
            pages.append(b.page)
            parts.append(raw)
        return "\n".join(parts).strip(), sorted(set(pages))

    s5_text, s5_pages = collect(t5_idx, 12, s5_heading_line)
    mg_text, mg_pages = collect(tmg_idx, 20, mg_heading_line)

    # 兜底：部分招股书标题不标准/OCR断裂时，按语义关键词定位短窗口，避免 page=1 的假告警
    def fallback_collect(keyword_fn, max_pages: int) -> tuple[str, list[int]]:
        hit_idx = None
        for i in range(search_start, search_end):
            raw = blocks[i].text or ""
            if keyword_fn(raw):
                hit_idx = i
                break
        if hit_idx is None:
            for i, b in enumerate(blocks):
                if keyword_fn(b.text or ""):
                    hit_idx = i
                    break
        if hit_idx is None:
            return "", []

        start_page = blocks[hit_idx].page
        pages: list[int] = []
        parts: list[str] = []
        for j in range(hit_idx, len(blocks)):
            b = blocks[j]
            raw = b.text or ""
            if b.page - start_page >= max_pages:
                break
            if section_heading_line.search(raw) and j > hit_idx:
                break
            pages.append(b.page)
            parts.append(raw)
        return "\n".join(parts).strip(), sorted(set(pages))

    if not s5_pages:
        s5_text, s5_pages = fallback_collect(
            lambda raw: (
                ("5%" in _norm(raw) or "５%" in _norm(raw))
                and ("股东" in raw)
                and ("实际控制人" in raw or "控制人" in raw)
            ),
            max_pages=5,
        )

    if not mg_pages:
        mg_text, mg_pages = fallback_collect(
            lambda raw: (
                ("董事、监事、高级管理人员" in raw or "董事、监事和高级管理人员" in raw)
                and ("核心技术人员" in raw)
            ) or ("所持股份不存在质押或冻结" in raw),
            max_pages=6,
        )

    # 若正文中出现明确“所持股份不存在质押或冻结”语句，但未被纳入mg窗口，则补充该页短窗口
    neg_hit_idx = None
    for i in range(search_start, search_end):
        raw = blocks[i].text or ""
        if "所持股份不存在质押或冻结" in raw or "不存在质押或冻结情况" in raw:
            neg_hit_idx = i
            break
    if neg_hit_idx is not None:
        p = blocks[neg_hit_idx].page
        if p not in set(mg_pages):
            add_text, add_pages = fallback_collect(
                lambda raw: ("所持股份不存在质押或冻结" in raw) or ("不存在质押或冻结情况" in raw),
                max_pages=2,
            )
            if add_text:
                mg_text = (mg_text + "\n" + add_text).strip() if mg_text else add_text
                mg_pages = sorted(set(mg_pages + add_pages))

    return {"s5_text": s5_text, "s5_pages": s5_pages, "mg_text": mg_text, "mg_pages": mg_pages}


def _ask_yes_no(llm, text: str, logger: logging.Logger) -> bool:
    parser = PydanticOutputParser(pydantic_object=YesNoOutput)
    fixing = OutputFixingParser.from_llm(parser=parser, llm=llm)
    prompt = PromptTemplate.from_template(
        "判断下列文本是否存在‘股份质押/股份冻结’相关披露（包含明确声明无相关事项）。\n"
        "输出格式：\n{format_instructions}\n\n"
        "文本：\n{text}"
    )
    chain = (
        RunnableLambda(lambda x: prompt.format(**x))
        | llm
        | RunnableLambda(lambda m: m.content if isinstance(m.content, str) else str(m.content))
        | RunnableLambda(lambda raw: fixing.parse(raw))
    )
    try:
        logger.info("llm call start: stage=pledge_yesno")
        out: YesNoOutput = chain.invoke({"text": text[:12000], "format_instructions": parser.get_format_instructions()})
        logger.info("llm call done: stage=pledge_yesno disclosed=%s", out.disclosed)
        return bool(out.disclosed)
    except Exception as e:
        logger.warning("llm call failed: stage=pledge_yesno err=%s", str(e))
        return False


def _ask_extract_events(llm, text: str, logger: logging.Logger) -> list[dict]:
    parser = PydanticOutputParser(pydantic_object=EventOutput)
    fixing = OutputFixingParser.from_llm(parser=parser, llm=llm)
    prompt = PromptTemplate.from_template(
        "从文本中抽取股份质押/股份冻结事件。\n"
        "规则：只保留人员类型为 董事/监事/高级管理人员/实际控制人。\n"
        "event_type 仅可为 股份质押 或 股份冻结。\n"
        "输出格式：\n{format_instructions}\n\n文本：\n{text}"
    )
    chain = (
        RunnableLambda(lambda x: prompt.format(**x))
        | llm
        | RunnableLambda(lambda m: m.content if isinstance(m.content, str) else str(m.content))
        | RunnableLambda(lambda raw: fixing.parse(raw))
    )
    try:
        logger.info("llm call start: stage=pledge_extract")
        out: EventOutput = chain.invoke({"text": text[:14000], "format_instructions": parser.get_format_instructions()})
        logger.info("llm call done: stage=pledge_extract events=%s", len(out.events))
        events = []
        for e in out.events:
            d = e.model_dump()
            if d.get("person_type") not in {"董事", "监事", "高级管理人员", "实际控制人"}:
                continue
            if d.get("event_type") not in {"股份质押", "股份冻结"}:
                continue
            events.append(d)
        return events
    except Exception as e:
        logger.warning("llm call failed: stage=pledge_extract err=%s", str(e))
        return []


def _dedup_events(events: list[dict]) -> list[dict]:
    seen = set()
    out = []
    for e in events:
        k = (_norm(e.get("name", "")), e.get("person_type"), e.get("event_type"), _norm(e.get("event_status", "")))
        if k in seen:
            continue
        seen.add(k)
        out.append(e)
    return out


def _is_negative_event(e: dict) -> bool:
    txt = _norm((e.get("event_status") or "") + " " + (e.get("event_desc") or ""))
    if not txt:
        return False
    neg_markers = ["不存在", "无质押", "无冻结", "未质押", "未冻结", "未发生", "已解除", "解除质押", "解除冻结"]
    return any(k in txt for k in neg_markers)


def _is_positive_risk_event(e: dict) -> bool:
    txt = _norm((e.get("event_status") or "") + " " + (e.get("event_desc") or ""))
    if not txt:
        return False
    pos_markers = ["存在", "已质押", "质押中", "被冻结", "冻结中", "司法冻结", "轮候冻结"]
    return any(k in txt for k in pos_markers) and (not _is_negative_event(e))


def _split_event_polarity(events: list[dict]) -> tuple[list[dict], list[dict]]:
    negative, risk = [], []
    for e in events:
        if _is_negative_event(e):
            negative.append(e)
        elif _is_positive_risk_event(e):
            risk.append(e)
        # 其余“无法判定极性”的事件先不计入风险，避免误报
    return negative, risk


def main() -> None:
    parser = argparse.ArgumentParser(description="Run pledge_freeze check (LangChain)")
    parser.add_argument("--pdf", required=True)
    parser.add_argument("--workdir", required=True)
    parser.add_argument("--preprocessed", default="")
    parser.add_argument("--chunk-size", type=int, default=1800)
    parser.add_argument("--chunk-overlap", type=int, default=200)
    args = parser.parse_args()

    pdf_path = Path(args.pdf).resolve()
    workdir = Path(args.workdir).resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / "logs").mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("pledge_freeze_langchain")
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

    loc = _locate_sections(text_blocks)
    (workdir / "located_sections.json").write_text(json.dumps(loc, ensure_ascii=False, indent=2), encoding="utf-8")

    llm = build_chat_llm(temperature=0)

    s5_chunks = _split_text(loc["s5_text"], args.chunk_size, args.chunk_overlap) if loc["s5_text"].strip() else []
    mg_chunks = _split_text(loc["mg_text"], args.chunk_size, args.chunk_overlap) if loc["mg_text"].strip() else []

    s5_flags = [_ask_yes_no(llm, c, logger) for c in s5_chunks]
    mg_flags = [_ask_yes_no(llm, c, logger) for c in mg_chunks]

    s5_disclosed = any(s5_flags)
    mg_disclosed = any(mg_flags)

    events: list[dict] = []
    for i, c in enumerate(s5_chunks):
        if i < len(s5_flags) and s5_flags[i]:
            events.extend(_ask_extract_events(llm, c, logger))
    for i, c in enumerate(mg_chunks):
        if i < len(mg_flags) and mg_flags[i]:
            events.extend(_ask_extract_events(llm, c, logger))

    events_all = _dedup_events(events)
    negative_events, risk_events = _split_event_polarity(events_all)

    if not s5_disclosed and not mg_disclosed:
        result = {
            "summary": {
                "runtime": "langchain",
                "status": "fail",
                "disclosed_detected": False,
                "event_count": 0,
                "reason": "未在目标章节检测到质押冻结相关披露",
                "s5_disclosed": False,
                "mg_disclosed": False,
                "s5_chunk_count": len(s5_chunks),
                "s5_hit_chunks": sum(1 for x in s5_flags if x),
                "mg_chunk_count": len(mg_chunks),
                "mg_hit_chunks": sum(1 for x in mg_flags if x),
                "negative_sample": False,
                "negative_note": "披露缺失，不属于阴性样本",
            },
            "alerts": [
                {
                    "message": "发行人持股5%以上股东及董监高核心技术人员部分未检测到股份质押冻结相关披露",
                    "page": (loc["s5_pages"][0] if loc["s5_pages"] else (loc["mg_pages"][0] if loc["mg_pages"] else 1)),
                }
            ],
            "events": [],
            "negative_output": {
                "is_negative": False,
                "message": "非阴性样本：存在披露缺失风险",
            },
        }
    else:
        alerts = [
            {
                "message": f"{e['name']}存在{e['event_type']}且未检测到解除说明",
                "page": (loc["s5_pages"][0] if loc["s5_pages"] else (loc["mg_pages"][0] if loc["mg_pages"] else 1)),
                "name": e.get("name"),
                "person_type": e.get("person_type"),
                "event_type": e.get("event_type"),
            }
            for e in risk_events
        ]
        is_negative = (len(risk_events) == 0 and bool(s5_disclosed or mg_disclosed))
        result = {
            "summary": {
                "runtime": "langchain",
                "status": "pass" if len(risk_events) == 0 else "fail",
                "disclosed_detected": bool(s5_disclosed or mg_disclosed),
                "event_count": len(risk_events),
                "negative_event_count": len(negative_events),
                "s5_disclosed": s5_disclosed,
                "mg_disclosed": mg_disclosed,
                "s5_chunk_count": len(s5_chunks),
                "s5_hit_chunks": sum(1 for x in s5_flags if x),
                "mg_chunk_count": len(mg_chunks),
                "mg_hit_chunks": sum(1 for x in mg_flags if x),
                "negative_sample": is_negative,
                "negative_note": "已检测到阴性披露（存在无质押/冻结声明）" if is_negative else "检测到质押/冻结事件或风险",
            },
            "alerts": alerts,
            "events": risk_events,
            "negative_events": negative_events,
            "events_all": events_all,
            "negative_output": {
                "is_negative": is_negative,
                "message": "阴性样本：检测到无质押/冻结声明，且未提取到风险事件" if is_negative else "非阴性样本：存在质押/冻结事件或风险",
            },
        }

    with (workdir / "events.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["姓名", "人员类型", "事件类型", "事件状态", "事件描述", "是否阴性声明"])
        for e in events_all:
            w.writerow([e.get("name"), e.get("person_type"), e.get("event_type"), e.get("event_status"), e.get("event_desc"), "是" if _is_negative_event(e) else "否"])

    (workdir / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    final_events = result.get("events", [])
    logger.info("pipeline done: status=%s events=%s", result["summary"]["status"], len(final_events))
    print(f"Done. status={result['summary']['status']} events={len(final_events)}")
    print(f"Artifacts: {workdir}")


if __name__ == "__main__":
    main()
