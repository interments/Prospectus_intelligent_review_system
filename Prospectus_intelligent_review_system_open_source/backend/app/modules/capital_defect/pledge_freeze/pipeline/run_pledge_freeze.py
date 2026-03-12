from __future__ import annotations

import argparse
import csv
import json
import logging
import re
from pathlib import Path

from dotenv import load_dotenv

from ...price_fluctuation.extractors.pdf_router import PdfRouter
from ...price_fluctuation.schemas.models import TableBlock, TextBlock
from ...price_fluctuation.services.llm_client import build_ark_client


def _split_text(content: str, chunk_size: int = 1800, overlap: int = 200) -> list[str]:
    try:
        from langchain_text_splitters import RecursiveCharacterTextSplitter

        splitter = RecursiveCharacterTextSplitter(chunk_size=chunk_size, chunk_overlap=overlap)
        chunks = splitter.split_text(content)
        return chunks or [content]
    except Exception:
        # fallback splitter
        if len(content) <= chunk_size:
            return [content]
        out = []
        i = 0
        while i < len(content):
            out.append(content[i:i + chunk_size])
            i += max(1, chunk_size - overlap)
        return out


def _norm(s: str) -> str:
    return (s or "").replace(" ", "").replace("\n", "")


def _locate_sections(text_blocks: list[TextBlock]) -> dict:
    blocks = sorted(text_blocks, key=lambda x: x.page)

    # 父章节：发行人基本情况 / 公司基本情况（先定位父章节，再在其内找子标题）
    parent_heading_line = re.compile(
        r"^\s*第[一二三四五六七八九十百零〇\d]+节\s*(?:发行人|公司)基本情况\s*$",
        re.M,
    )
    # 父章节边界（下一节）
    section_heading_line = re.compile(r"^\s*第[一二三四五六七八九十百零〇\d]+节\s*", re.M)

    # 指定目标子标题（避免误命中“持股意向/减持意向”等）
    s5_heading_line = re.compile(
        r"^\s*[（(]?[五六56][）)]?、\s*(?:持有发行人\s*5%\s*以上股份的主要股东及实际控制人(?:基本)?情况|持股\s*5%\s*以上(?:主要)?股东及实际控制人(?:基本)?情况)\s*$",
        re.M,
    )
    mg_heading_line = re.compile(
        r"^\s*[（(]?[七八78][）)]?、\s*董事、监事、高级管理人员(?:与|及)核心技术人员(?:的简要情况)?\s*$",
        re.M,
    )
    # 同级标题边界：仅匹配“六、...”“八、...”，不匹配“（一）”“1、”等下级标题
    peer_heading = re.compile(r"^\s*[一二三四五六七八九十]+、", re.M)

    parent_hits = []
    for i, b in enumerate(blocks):
        raw = b.text or ""
        if parent_heading_line.search(raw):
            parent_hits.append(i)

    # 目录页常先命中，取最后一次更接近正文
    parent_idx = parent_hits[-1] if parent_hits else None

    search_start = 0
    search_end = len(blocks)
    if parent_idx is not None:
        search_start = parent_idx
        for j in range(parent_idx + 1, len(blocks)):
            if section_heading_line.search(blocks[j].text or ""):
                search_end = j
                break

    t5_hits = []
    tmg_hits = []
    for i in range(search_start, search_end):
        raw = blocks[i].text or ""
        if s5_heading_line.search(raw):
            t5_hits.append(i)
        if mg_heading_line.search(raw):
            tmg_hits.append(i)

    # 兜底：父章节范围内没找到时，回退到全文检索
    if not t5_hits and not tmg_hits:
        for i, b in enumerate(blocks):
            raw = b.text or ""
            if s5_heading_line.search(raw):
                t5_hits.append(i)
            if mg_heading_line.search(raw):
                tmg_hits.append(i)

    # 目录页常先命中，取最后一次行级标题命中更接近正文子标题
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
            t = _norm(raw)
            if b.page - start_page >= max_pages:
                break
            if j > start_idx and peer_heading.search(raw):
                break

            if j == start_idx:
                m = start_heading_re.search(raw)
                if m:
                    raw = raw[m.start():]
            pages.append(b.page)
            parts.append(raw)
        return "\n".join(parts), sorted(set(pages))

    s5_text, s5_pages = collect(t5_idx, max_pages=12, start_heading_re=s5_heading_line)
    mg_text, mg_pages = collect(tmg_idx, max_pages=20, start_heading_re=mg_heading_line)
    return {
        "s5_text": s5_text,
        "s5_pages": s5_pages,
        "mg_text": mg_text,
        "mg_pages": mg_pages,
    }


def _ask_yes_no(client, model: str, content: str) -> bool:
    prompt = (
        "你是金融合规分析师。请判断以下内容是否存在股份质押/股份冻结相关披露（包括'不存在质押冻结'声明或存在质押冻结事件披露）。"
        "仅输出 true 或 false。\n\n原文:\n" + content[:12000]
    )
    rsp = client.chat.completions.create(
        model=model,
        temperature=0,
        messages=[{"role": "user", "content": prompt}],
    )
    txt = (rsp.choices[0].message.content or "").strip().lower()
    return "true" in txt and "false" not in txt


def _ask_extract_table(client, model: str, content: str) -> str:
    prompt = (
        "原文：\n"
        + content[:16000]
        + "\n\n你是一个金融分析师，负责提取原文中控股股东、实际控制人或董事、监事、高级管理人员中存在股份质押或冻结的情况，请从以上招股说明书原文中提取出所有发行人的董事、监事、高级管理人员或发行人的控股股东和实际控制人存在股份质押情况的人员，并以每行按“|序号|人员名称|人员类型|事件情况|”的表格格式列出。\n"
        "请注意：\n"
        "1.人员类型为董事/监事/高级管理人员/实际控制人，事件情况从“股份质押”或“股份冻结”中选择。\n"
        "2.如原文中说明了A向B质押了X股份，这时请提取出A及A的人员类型，事件情况为股份质押。其他情况下请不要提取。\n"
        "3.若无股份质押或股份冻结行为请不要列出。\n"
        "4.若原文中出现了表格的形式，请找出并对应好相应人员股份质押/股份冻结情况的表项，相关人员若不存在股份质押或冻结情况，原文表格中会用“-”表示。\n"
        "5.若遇到存在股份质押或冻结情况的人员，但同时说明了目前已不存在相关质押或冻结情况的情况，也不要列出。"
    )
    rsp = client.chat.completions.create(
        model=model,
        temperature=0,
        messages=[{"role": "user", "content": prompt}],
    )
    return (rsp.choices[0].message.content or "").strip()


def _parse_markdown_table(s: str, page: int) -> list[dict]:
    rows = []
    for ln in s.splitlines():
        line = ln.strip()
        if not line.startswith("|"):
            continue
        cols = [c.strip() for c in line.strip("|").split("|")]
        if len(cols) < 4:
            continue
        if any(x in cols[0] for x in ["序号", "---"]):
            continue
        name = cols[1]
        role = cols[2]
        event = cols[3]
        if not name or name == "-":
            continue
        if role not in {"董事", "监事", "高级管理人员", "实际控制人"}:
            continue
        if event not in {"股份质押", "股份冻结"}:
            continue
        rows.append({"name": name, "person_type": role, "event_type": event, "page": page})
    # dedup
    seen = set()
    out = []
    for r in rows:
        k = (r["name"], r["person_type"], r["event_type"])
        if k in seen:
            continue
        seen.add(k)
        out.append(r)
    return out


def main() -> None:
    base_dir = Path(__file__).resolve().parents[6]
    backend_dir = base_dir / "backend"
    load_dotenv(base_dir / ".env")
    load_dotenv(backend_dir / ".env")

    parser = argparse.ArgumentParser(description="Run pledge/freeze declaration check")
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

    logger = logging.getLogger("pledge_freeze_pipeline")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    fh = logging.FileHandler(workdir / "logs" / "run.log", encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(fh)

    logger.info("pipeline start: pdf=%s", pdf_path)

    pre_path = Path(args.preprocessed).resolve() if args.preprocessed else None
    if pre_path and pre_path.exists():
        payload = json.loads(pre_path.read_text(encoding="utf-8"))
        text_blocks = [TextBlock.model_validate(x) for x in payload.get("text_blocks", [])]
        table_blocks = [TableBlock.model_validate(x) for x in payload.get("table_blocks", [])]
        logger.info("extract reused: %s", pre_path)
    else:
        router = PdfRouter()
        text_blocks, table_blocks = router.extract(pdf_path)

    (workdir / "preprocessed.json").write_text(
        json.dumps({
            "text_blocks": [x.model_dump() for x in text_blocks],
            "table_blocks": [x.model_dump() for x in table_blocks],
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    loc = _locate_sections(text_blocks)
    (workdir / "located_sections.json").write_text(json.dumps(loc, ensure_ascii=False, indent=2), encoding="utf-8")

    client, model = build_ark_client()

    s5_chunks = _split_text(loc["s5_text"], args.chunk_size, args.chunk_overlap) if loc["s5_text"].strip() else []
    s5_flags = []
    for c in s5_chunks:
        s5_flags.append(_ask_yes_no(client, model, c))
    s5_disclosed = any(s5_flags)

    mg_chunks = _split_text(loc["mg_text"], args.chunk_size, args.chunk_overlap) if loc["mg_text"].strip() else []
    mg_flags = []
    for c in mg_chunks:
        mg_flags.append(_ask_yes_no(client, model, c))
    mg_disclosed = any(mg_flags)

    if not s5_disclosed and not mg_disclosed:
        result = {
            "summary": {
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
            },
            "alerts": [
                {
                    "message": "发行人持股5%以上股东及董监高核心技术人员部分未检测到股份质押冻结相关披露",
                    "page": (loc["s5_pages"][0] if loc["s5_pages"] else (loc["mg_pages"][0] if loc["mg_pages"] else 1)),
                }
            ],
            "events": [],
        }
        (workdir / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print("Done. status=fail events=0")
        print(f"Artifacts: {workdir}")
        return

    events = []

    if s5_disclosed:
        for idx, c in enumerate(s5_chunks):
            if idx < len(s5_flags) and not s5_flags[idx]:
                continue
            table_text = _ask_extract_table(client, model, c)
            page = loc["s5_pages"][0] if loc["s5_pages"] else 1
            events.extend(_parse_markdown_table(table_text, page))

    if mg_disclosed:
        for idx, c in enumerate(mg_chunks):
            if idx < len(mg_flags) and not mg_flags[idx]:
                continue
            table_text = _ask_extract_table(client, model, c)
            page = loc["mg_pages"][0] if loc["mg_pages"] else 1
            events.extend(_parse_markdown_table(table_text, page))

    # dedup after merge
    uniq = {}
    for e in events:
        k = (e["name"], e["person_type"], e["event_type"])
        if k not in uniq:
            uniq[k] = e
    events = list(uniq.values())

    alerts = [
        {
            "message": f"{e['name']}（{e['person_type']}）存在{e['event_type']}且未检测到解除说明",
            "page": e["page"],
            "person_name": e["name"],
            "person_type": e["person_type"],
            "event_type": e["event_type"],
        }
        for e in events
    ]

    status = "pass" if len(events) == 0 else "fail"
    result = {
        "summary": {
            "status": status,
            "disclosed_detected": bool(s5_disclosed or mg_disclosed),
            "event_count": len(events),
            "s5_disclosed": s5_disclosed,
            "mg_disclosed": mg_disclosed,
            "s5_chunk_count": len(s5_chunks),
            "s5_hit_chunks": sum(1 for x in s5_flags if x),
            "mg_chunk_count": len(mg_chunks),
            "mg_hit_chunks": sum(1 for x in mg_flags if x),
        },
        "alerts": alerts,
        "events": events,
    }

    (workdir / "events.csv").write_text("", encoding="utf-8")
    with (workdir / "events.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["序号", "人员名称", "人员类型", "事件情况", "页码"])
        for i, e in enumerate(events, 1):
            w.writerow([i, e["name"], e["person_type"], e["event_type"], e["page"]])

    (workdir / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    logger.info("pipeline done: status=%s events=%s", status, len(events))
    print(f"Done. status={status} events={len(events)}")
    print(f"Artifacts: {workdir}")


if __name__ == "__main__":
    main()
