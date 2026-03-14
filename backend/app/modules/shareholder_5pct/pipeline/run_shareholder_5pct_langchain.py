from __future__ import annotations

import argparse
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
from app.modules.price_fluctuation.extractors.pdf_router import PdfRouter
from app.modules.price_fluctuation.schemas.models import TableBlock, TextBlock


PCT_RE = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*[%％]")


class DisclosedItem(BaseModel):
    name: str = Field(..., description="应披露主体名称")


class DisclosedOutput(BaseModel):
    items: list[DisclosedItem] = Field(default_factory=list)


def _norm_name(s: str) -> str:
    return re.sub(r"\s+", "", (s or "").strip())


def _pct_to_float(s: str) -> float | None:
    m = PCT_RE.search(str(s or ""))
    if not m:
        return None
    try:
        return float(m.group(1))
    except Exception:
        return None


def _locate_pages(text_blocks: list[TextBlock]) -> tuple[set[int], set[int]]:
    disclosed_pages: set[int] = set()
    expected_pages: set[int] = set()

    blocks = [b for b in sorted(text_blocks, key=lambda x: x.page) if b.page >= 30]
    parent_heading_re = re.compile(r"^\s*第[一二三四五六七八九十百零〇\d]+节\s*(发行人|公司)基本情况\s*$", re.M)
    section_heading_re = re.compile(r"^\s*第[一二三四五六七八九十百零〇\d]+节\s*", re.M)

    parent_hits = []
    for i, b in enumerate(blocks):
        t = b.text or ""
        if "......" in t:
            continue
        if parent_heading_re.search(t):
            parent_hits.append(i)

    search_start, search_end = 0, len(blocks)
    if parent_hits:
        search_start = parent_hits[0]
        for j in range(search_start + 1, len(blocks)):
            if section_heading_re.search(blocks[j].text or ""):
                search_end = j
                break

    disclosed_heading_re = re.compile(r"持(?:有发行人|有公司|股)?\s*5\s*[%％]?\s*以上.*股东.*实际控制人")
    disclosed_start_idx = None
    expected_start_idx = None

    for i in range(search_start, search_end):
        t = (blocks[i].text or "").replace("\n", "")
        if "......" in t:
            continue
        if disclosed_start_idx is None and (
            disclosed_heading_re.search(t)
            or any(k in t for k in ["持有发行人5%以上", "持有发行人 5%以上", "持股5%以上股东", "持股 5%以上股东"])
        ):
            disclosed_start_idx = i
        if expected_start_idx is None and any(k in t for k in ["发行人股本情况", "公司股本情况", "发行前股本", "发行前股本结构"]):
            expected_start_idx = i

    top_heading_re = re.compile(r"[一二三四五六七八九十]{1,3}、")
    if disclosed_start_idx is not None:
        for j in range(disclosed_start_idx, len(blocks)):
            t = (blocks[j].text or "").replace("\n", "")
            disclosed_pages.add(blocks[j].page)
            if j > disclosed_start_idx and top_heading_re.search(t):
                break

    if expected_start_idx is not None:
        p = blocks[expected_start_idx].page
        expected_pages.update(range(p, p + 2))

    return disclosed_pages, expected_pages


def _extract_expected_from_tables(table_blocks: list[TableBlock], expected_pages: set[int]) -> list[dict]:
    out: dict[str, dict] = {}
    for tb in table_blocks:
        if tb.page not in expected_pages:
            continue
        rows = tb.rows or []
        if not rows:
            continue

        header_text = ''.join(str(c or '') for r in rows[:6] for c in r)
        if not any(k in header_text for k in ["股东名称", "持股数量", "发行前", "发行后", "万股", "持股比例"]):
            continue

        for r in rows:
            cells = [str(c or '').strip() for c in r]
            if not any('%' in c or '％' in c for c in cells):
                continue

            name = None
            for c in cells:
                if not c or c in {'-', '--'}:
                    continue
                if re.search(r"^[0-9,\.]+$", c):
                    continue
                if '%' in c or '％' in c:
                    continue
                if c in {'序号', '股东名称', '持股数量', '持股比例', '发行前', '发行后'}:
                    continue
                if re.search(r"^[\u4e00-\u9fa5A-Za-z0-9·\-（）()]{2,40}$", c):
                    name = c
                    break

            if not name or '合计' in name or any(x in name for x in ["社会公众股", "无限售", "有限售", "国家股", "法人股"]):
                continue

            pcts = [(_pct_to_float(c) or 0) for c in cells if ('%' in c or '％' in c)]
            pct = max(pcts) if pcts else None
            if pct is None or pct < 4.9:
                continue

            key = _norm_name(name)
            if key not in out or pct > (out[key].get("holding_pct") or 0):
                out[key] = {"name": name, "holding_pct": pct, "page": tb.page, "source": "table_fallback"}

    return list(out.values())


def _extract_disclosed_with_langchain(text_blocks: list[TextBlock], disclosed_pages: set[int], logger: logging.Logger) -> list[dict]:
    if not disclosed_pages:
        return []

    blocks = [b for b in text_blocks if b.page in disclosed_pages]
    blocks = sorted(blocks, key=lambda x: x.page)
    text = "\n\n".join([f"[page={b.page}]\n{(b.text or '')[:4000]}" for b in blocks])[:30000]

    llm = build_chat_llm(temperature=0)
    parser = PydanticOutputParser(pydantic_object=DisclosedOutput)
    fixing_parser = OutputFixingParser.from_llm(parser=parser, llm=llm)

    prompt_tmpl = PromptTemplate.from_template(
        "你是招股书信息抽取助手。请从下面文本中提取‘发行人层面需要披露的5%以上股东主体名单’。\n"
        "规则：\n"
        "1) 保留控股股东、其他持有发行人/公司5%以上股份的主要股东；\n"
        "2) 不要抽取这些股东自身的出资人/执行事务合伙人/管理人；\n"
        "3) 不要输出‘基本情况/股权结构/财务数据’等标题词；\n"
        "4) 严格按输出格式返回。\n\n"
        "输出格式要求：\n{format_instructions}\n\n"
        "文本：\n{text}"
    )

    # RunnableSequence: prompt -> llm -> parse(fix)
    chain = (
        RunnableLambda(lambda x: prompt_tmpl.format(**x))
        | llm
        | RunnableLambda(lambda m: m.content if isinstance(m.content, str) else str(m.content))
        | RunnableLambda(lambda raw: fixing_parser.parse(raw))
    )

    names: list[str] = []

    try:
        logger.info("llm call start: stage=disclosed_extract parser=output_fixing model=doubao")
        data: DisclosedOutput = chain.invoke(
            {
                "text": text,
                "format_instructions": parser.get_format_instructions(),
            }
        )
        names = [x.name for x in data.items if x.name]
        logger.info("llm call done: stage=disclosed_extract path=chain_parse names=%s", len(names))
    except Exception as e:
        logger.warning("llm call parse failed: stage=disclosed_extract err=%s", str(e))
        # 最终兜底：兼容模型返回 markdown/table/半结构化文本
        logger.info("llm call retry: stage=disclosed_extract path=raw_json_fallback")
        raw = llm.invoke(
            "请仅输出 JSON，格式为：{\"items\":[{\"name\":\"...\"}]}。\n\n文本：\n" + text
        ).content
        raw = raw if isinstance(raw, str) else str(raw)
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict) and isinstance(parsed.get("items"), list):
                names = [str(i.get("name") or "") for i in parsed.get("items", []) if isinstance(i, dict)]
            elif isinstance(parsed, list):
                for i in parsed:
                    if isinstance(i, dict) and i.get("name"):
                        names.append(str(i.get("name")))
                    elif isinstance(i, str):
                        names.append(i)
            logger.info("llm call done: stage=disclosed_extract path=json_fallback names=%s", len(names))
        except Exception:
            logger.info("llm call fallback: stage=disclosed_extract path=markdown_line_parse")
            for ln in raw.splitlines():
                s = ln.strip().strip("|")
                if not s:
                    continue
                if any(x in s for x in ["股东", "类型", "---", "JSON", "items", "name"]):
                    continue
                col = s.split("|")[0].strip() if "|" in s else s
                col = col.strip("-• ")
                if re.search(r"^[\u4e00-\u9fa5A-Za-z0-9·\-（）()]{2,40}$", col):
                    names.append(col)

    out = []
    seen = set()
    page_hint = min(disclosed_pages)
    for name in names:
        name = (name or "").strip()
        if not name:
            continue
        if name in {"基本情况", "股权结构", "财务数据", "主要财务数据"}:
            continue
        if any(k in name for k in ["出资人", "执行事务合伙人", "管理人", "GP", "股东类型"]):
            continue
        key = _norm_name(name)
        if key in seen:
            continue
        seen.add(key)
        out.append({"name": name, "holding_pct": None, "page": page_hint, "source": "langchain"})

    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Run 5% shareholder disclosure check (LangChain)")
    parser.add_argument("--pdf", required=True)
    parser.add_argument("--workdir", required=True)
    parser.add_argument("--preprocessed", default="", help="Optional shared preprocessed.json path")
    args = parser.parse_args()

    pdf_path = Path(args.pdf).resolve()
    workdir = Path(args.workdir).resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    log_dir = workdir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("shareholder_5pct_langchain")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    fh = logging.FileHandler(log_dir / "run.log", encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(fh)

    logger.info("pipeline start: pdf=%s", pdf_path)
    router = PdfRouter()

    preprocessed_path = Path(args.preprocessed).resolve() if args.preprocessed else None
    if preprocessed_path and preprocessed_path.exists():
        payload = json.loads(preprocessed_path.read_text(encoding="utf-8"))
        text_blocks = [TextBlock.model_validate(x) for x in payload.get("text_blocks", [])]
        table_blocks = [TableBlock.model_validate(x) for x in payload.get("table_blocks", [])]
        logger.info("extract reused: %s", preprocessed_path)
    else:
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

    disclosed_pages, expected_pages = _locate_pages(text_blocks)
    logger.info("pages located: disclosed=%s expected=%s", sorted(disclosed_pages), sorted(expected_pages))

    disclosed = _extract_disclosed_with_langchain(text_blocks, disclosed_pages, logger)
    expected = _extract_expected_from_tables(table_blocks, expected_pages)

    disclosed_set = {_norm_name(x["name"]) for x in disclosed}
    missing = [x for x in expected if _norm_name(x["name"]) not in disclosed_set]

    issues = []
    for m in missing:
        issues.append(
            {
                "type": "missing_5pct_shareholder_disclosure",
                "severity": "high",
                "message": f"{m['name']} 持股比例 {m['holding_pct']}% 存在股东未披露问题",
                "shareholder": m["name"],
                "holding_pct": m["holding_pct"],
                "page": m["page"],
                "source_section": "发行人股本情况/发行前股本结构",
            }
        )

    alerts = [
        {
            "message": x["message"],
            "page": x["page"],
            "shareholder": x["shareholder"],
            "holding_pct": x["holding_pct"],
            "source_section": x["source_section"],
        }
        for x in issues
    ]

    is_negative = (len(missing) == 0)

    result = {
        "summary": {
            "disclosed_count": len(disclosed),
            "expected_count": len(expected),
            "missing_count": len(missing),
            "status": "pass" if not missing else "fail",
            "runtime": "langchain",
            "negative_sample": is_negative,
            "negative_note": "未发现5%以上股东漏披露" if is_negative else "存在5%以上股东漏披露",
        },
        "issues": issues,
        "alerts": alerts,
        "disclosed_list": disclosed,
        "expected_list": expected,
        "disclosed_list_raw": disclosed,
        "expected_list_raw": expected,
        "missing_shareholders": missing,
        "negative_output": {
            "is_negative": is_negative,
            "message": "阴性样本：未发现5%以上股东漏披露" if is_negative else "非阴性样本：检测到漏披露",
        },
        "evidence_pages": {
            "disclosed_pages": sorted(disclosed_pages),
            "expected_pages": sorted(expected_pages),
        },
    }

    (workdir / "disclosed_list.json").write_text(json.dumps(disclosed, ensure_ascii=False, indent=2), encoding="utf-8")
    (workdir / "expected_list.json").write_text(json.dumps(expected, ensure_ascii=False, indent=2), encoding="utf-8")
    (workdir / "disclosed_list_raw.json").write_text(json.dumps(disclosed, ensure_ascii=False, indent=2), encoding="utf-8")
    (workdir / "expected_list_raw.json").write_text(json.dumps(expected, ensure_ascii=False, indent=2), encoding="utf-8")
    (workdir / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    logger.info("pipeline done: disclosed=%s expected=%s missing=%s", len(disclosed), len(expected), len(missing))
    print(f"Done. status={result['summary']['status']} missing={len(missing)}")
    print(f"Artifacts: {workdir}")


if __name__ == "__main__":
    main()
