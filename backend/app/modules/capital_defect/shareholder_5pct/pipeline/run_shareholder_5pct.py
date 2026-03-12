from __future__ import annotations

import argparse
import json
import logging
import os
import re
import time
from pathlib import Path

from ...price_fluctuation.extractors.pdf_router import PdfRouter
from ...price_fluctuation.schemas.models import TableBlock, TextBlock
from ...price_fluctuation.services.llm_client import build_ark_client

PCT_RE = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*%")


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


def _header_idx(row: list[str | None], keys: list[str]) -> int | None:
    for i, c in enumerate(row):
        t = str(c or "")
        if all(k in t for k in keys):
            return i
    return None


def _is_shareholder_table(header: list[str]) -> bool:
    h = ''.join(header)
    has_name = ("股东" in h or "姓名" in h or "名称" in h)
    has_ratio = ("持股比例" in h or "比例" in h)
    # 排除明显非股东持股表
    bad = ["客户", "供应商", "关联方", "前五大", "销售", "采购", "董事", "监事", "职务"]
    if any(x in h for x in bad):
        return False
    return has_name and has_ratio


def _extract_from_tables(tables, pages: set[int] | None) -> list[dict]:
    out: dict[str, dict] = {}
    for tb in tables:
        if pages is not None and tb.page not in pages:
            continue
        rows = tb.rows or []
        if not rows:
            continue

        header0 = [str(x or "") for x in rows[0]]
        header1 = [str(x or "") for x in rows[1]] if len(rows) > 1 else []
        width = max(len(header0), len(header1)) if header1 else len(header0)
        header = []
        for i in range(width):
            h0 = header0[i] if i < len(header0) else ""
            h1 = header1[i] if i < len(header1) else ""
            header.append((h0 + h1).replace("\n", ""))

        if not _is_shareholder_table(header):
            continue

        name_idx = None
        pct_idx = None
        for i, h in enumerate(header):
            if ("股东" in h or "姓名" in h or "名称" in h) and name_idx is None:
                name_idx = i
            if "持股比例" in h or "比例" in h:
                pct_idx = i
        if name_idx is None or pct_idx is None:
            continue

        data_start = 1
        if len(rows) > 1 and ("比例" in ''.join(str(x or '') for x in rows[1])):
            data_start = 2

        for r in rows[data_start:]:
            if max(name_idx, pct_idx) >= len(r):
                continue
            name = str(r[name_idx] or "").strip()
            pct_raw = str(r[pct_idx] or "").strip()
            if not name or "合计" in name:
                continue
            # 排除股本结构中的类别项，避免被识别为“股东”
            if any(x in name for x in ["社会公众股", "无限售", "有限售", "国家股", "法人股"]):
                continue
            pct = _pct_to_float(pct_raw)
            if pct is None:
                continue
            key = _norm_name(name)
            # 5%模块关注口径：只保留 >=4.9 的候选，减少噪声
            if pct < 4.9:
                continue
            if key not in out or pct > out[key]["holding_pct"]:
                out[key] = {"name": name, "holding_pct": pct, "page": tb.page, "source": "table"}

    # 兜底：对于“发行前后股本结构”这类复杂多级表头，按行扫描“名称+比例%”
    if not out:
        for tb in tables:
            if pages is not None and tb.page not in pages:
                continue
            rows = tb.rows or []
            if not rows:
                continue
            header_text = ''.join(str(c or '') for r in rows[:6] for c in r)
            if not any(k in header_text for k in ["股东名称", "持股数量", "发行前", "发行后", "万股"]):
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


def _extract_expected_from_text(text_blocks, pages: set[int]) -> list[dict]:
    out: dict[str, dict] = {}
    # 示例：1 张国强 13,264,430 25.09%
    row_re = re.compile(r"^(?:\d+\s+)?([\u4e00-\u9fa5A-Za-z0-9（）\(\)·\-]+)\s+[0-9,]+\s+([0-9]+(?:\.[0-9]+)?\s*%)$")
    for b in text_blocks:
        if b.page not in pages:
            continue
        for ln in (b.text or "").splitlines():
            ln = re.sub(r"\s+", " ", ln.strip())
            m = row_re.match(ln)
            if not m:
                continue
            name = m.group(1).strip()
            pct = _pct_to_float(m.group(2))
            if not name or pct is None or pct < 4.9:
                continue
            if any(x in name for x in ["合计", "社会公众股", "无限售", "有限售", "国家股", "法人股"]):
                continue
            key = _norm_name(name)
            if key not in out or pct > (out[key].get("holding_pct") or 0):
                out[key] = {"name": name, "holding_pct": pct, "page": b.page, "source": "text"}
    return list(out.values())


def _extract_disclosed_from_text(text_blocks, pages: set[int]) -> list[dict]:
    out: dict[str, dict] = {}
    # 5%以上股东章节常见叙述：
    # - 控股股东利元亨投资 / 控股股东、实际控制人为王颖
    # - 其他持有发行人5%以上股份的主要股东为川捷投资
    phrase_res = [
        re.compile(r"控股股东(?:、?实际控制人)?(?:为|系)?([\u4e00-\u9fa5A-Za-z0-9·\-]{2,30})(?:（|\(|。|，|,|；|;|$)"),
        re.compile(r"主要股东为([\u4e00-\u9fa5A-Za-z0-9·\-、，]{2,60})(?:。|；|;|$)"),
        re.compile(r"其他持有(?:发行人|公司)?\s*5\s*[%％]?\s*以上股份的主要股东(?:包括|为)\s*([\u4e00-\u9fa5A-Za-z0-9·\-、，]{2,80})(?:。|；|;|$)"),
    ]

    for b in text_blocks:
        if b.page not in pages:
            continue
        raw_text = b.text or ""

        compact = raw_text.replace("\n", "")
        for pr in phrase_res:
            for m in pr.finditer(compact):
                raw_name = (m.group(1) or "").strip().strip("，。；")
                if not raw_name:
                    continue
                candidates = re.split(r"[、，]", raw_name)
                for name in candidates:
                    name = name.strip()
                    if not name or len(name) < 2 or any(x in name for x in ["股东", "股份"]):
                        continue
                    key = _norm_name(name)
                    out.setdefault(key, {"name": name, "holding_pct": None, "page": b.page, "source": "text_phrase"})

    # 结构化兜底：在“持有公司/发行人5%以上股份的主要股东”小节中抽取“1、张三”式标题
    sorted_blocks = sorted([b for b in text_blocks if b.page in pages], key=lambda x: x.page)
    in_major_5pct = False
    major_start_re = re.compile(r"持有(?:发行人|公司)?\s*5\s*[%％]?\s*以上股份的主要股东")
    major_end_re = re.compile(r"^[一二三四五六七八九十]+、")
    numbered_name_re = re.compile(r"^\s*\d+、\s*([\u4e00-\u9fa5A-Za-z0-9·\-（）()]{2,40})\s*$")

    for b in sorted_blocks:
        for ln in (b.text or "").splitlines():
            line = (ln or "").strip()
            if not line:
                continue
            if major_start_re.search(line):
                in_major_5pct = True
                continue
            if in_major_5pct and major_end_re.search(line) and ("持有" not in line and "主要股东" not in line):
                in_major_5pct = False
            if not in_major_5pct:
                continue
            m = numbered_name_re.match(line)
            if not m:
                continue
            name = m.group(1).strip()
            if any(x in name for x in ["合计", "股东", "实际控制人"]):
                continue
            if name in {"基本情况", "股权结构", "财务数据", "主要财务数据"}:
                continue
            key = _norm_name(name)
            out.setdefault(key, {"name": name, "holding_pct": None, "page": b.page, "source": "text_numbered"})

    return list(out.values())


def _extract_disclosed_with_llm(text_blocks, pages: set[int], logger: logging.Logger) -> list[dict]:
    try:
        client, ark_model = build_ark_client()
    except Exception:
        logger.info("5pct disclosed llm disabled(no ARK_API_KEY), fallback rule")
        return _extract_disclosed_from_text(text_blocks, pages)

    model = os.getenv("SHAREHOLDER_5PCT_MODEL", os.getenv("ARK_MODEL", ark_model))
    max_retries = int(os.getenv("LLM_MAX_RETRIES", "3"))

    blocks = [b for b in text_blocks if b.page in pages]
    blocks = sorted(blocks, key=lambda x: x.page)
    text = "\n\n".join([f"[page={b.page}]\n{(b.text or '')[:4000]}" for b in blocks])[:24000]

    sys_prompt = (
        "你是招股书信息抽取助手。任务：从给定章节中抽取‘需要进行5%以上股东披露的主体名单’。"
        "\n请一步一步在内部判断：先识别小节角色，再筛选只属于‘控股股东’和‘其他持有发行人5%以上股份的主要股东’。"
        "\n不要把‘实际控制人基本情况’里的自然人误当作5%以上股东披露对象（除非文本明确该自然人直接持有发行人>=5%股份）。"
        "\n严禁抽取被披露股东自身的内部出资人/执行事务合伙人/管理人/GP。"
        "\n若文本写‘A及其一致行动人’，默认仅保留A；仅当一致行动人单体明确直接持有发行人>=5%时才保留。"
        "\n输出必须是JSON数组，每项形如 {\"name\":\"股东名称\",\"role\":\"控股股东|其他5%以上股东\",\"holding_pct\":null,\"source\":\"llm\"}。"
        "\n只输出最终JSON，不输出分析过程文字。"
    )

    for attempt in range(1, max_retries + 1):
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": text},
                ],
                temperature=0,
                max_tokens=int(os.getenv("SHAREHOLDER_5PCT_MAX_TOKENS", "4096")),
            )
            out = (resp.choices[0].message.content or "").strip()
            data = json.loads(out)
            if not isinstance(data, list):
                raise ValueError("llm output not list")

            page_hint = min(pages) if pages else None
            cleaned = []
            seen = set()
            for x in data:
                if not isinstance(x, dict):
                    continue
                name = str(x.get("name") or "").strip()
                role = str(x.get("role") or "").strip()
                if not name or len(name) < 2:
                    continue
                if role and role not in ["控股股东", "其他5%以上股东"]:
                    continue
                if any(k in name for k in ["股权结构", "出资人", "执行事务合伙人", "管理人", "GP"]):
                    continue
                key = _norm_name(name)
                if key in seen:
                    continue
                seen.add(key)
                cleaned.append({"name": name, "holding_pct": None, "page": page_hint, "source": "llm"})

            if cleaned:
                logger.info("5pct disclosed llm success: %s", cleaned)
                return cleaned
            logger.info("5pct disclosed llm empty, fallback rule")
            return _extract_disclosed_from_text(text_blocks, pages)
        except Exception as e:
            logger.warning("5pct disclosed llm failed attempt=%s err=%s", attempt, str(e))
            if attempt >= max_retries:
                break
            time.sleep(min(2 ** (attempt - 1), 3))

    return _extract_disclosed_from_text(text_blocks, pages)


def _reconcile_lists_with_llm(disclosed: list[dict], expected: list[dict], logger: logging.Logger) -> tuple[list[dict], list[dict]]:
    try:
        client, ark_model = build_ark_client()
    except Exception:
        logger.info("5pct reconcile llm disabled(no ARK_API_KEY), use raw lists")
        return disclosed, expected

    model = os.getenv("SHAREHOLDER_5PCT_MODEL", os.getenv("ARK_MODEL", ark_model))
    payload = {
        "disclosed_list": disclosed,
        "expected_list": expected,
    }
    sys_prompt = (
        "你是招股书5%以上股东披露校验助手。请对 disclosed_list 和 expected_list 进行名称归一与口径对齐。"
        "规则：1) 仅保留发行人层面‘需要披露5%以上股东’主体（控股股东、其他5%以上股东）；"
        "2) 不把‘实际控制人介绍中的人物’当5%以上股东，除非该自然人明确直接持有发行人>=5%；"
        "3) 不把‘股东自身的出资人/合伙人/执行事务合伙人/管理人’当发行人股东；"
        "4) 将全称公司名归一到简称（如‘XX有限公司’->‘XX’常用简称）；"
        "5) 输出JSON对象：{normalized_disclosed:[{name,page,raw_name}], normalized_expected:[{name,page,raw_name}]}。"
        "仅输出JSON。"
    )
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            temperature=0,
            max_tokens=int(os.getenv("SHAREHOLDER_5PCT_MAX_TOKENS", "4096")),
        )
        out = (resp.choices[0].message.content or "").strip()
        data = json.loads(out)
        nd = data.get("normalized_disclosed") or []
        ne = data.get("normalized_expected") or []
        if not isinstance(nd, list) or not isinstance(ne, list):
            return disclosed, expected

        def _clean(items: list[dict], fallback_source: str):
            ret = []
            seen = set()
            for x in items:
                if not isinstance(x, dict):
                    continue
                name = str(x.get("name") or "").strip()
                if not name:
                    continue
                key = _norm_name(name)
                if key in seen:
                    continue
                seen.add(key)
                ret.append({
                    "name": name,
                    "holding_pct": x.get("holding_pct"),
                    "page": x.get("page"),
                    "source": x.get("source") or fallback_source,
                    "raw_name": x.get("raw_name") or x.get("name"),
                })
            return ret

        nd2 = _clean(nd, "llm_reconcile")
        ne2 = _clean(ne, "llm_reconcile")
        if nd2 and ne2:
            logger.info("5pct reconcile llm success: disclosed=%s expected=%s", nd2, ne2)
            return nd2, ne2
    except Exception as e:
        logger.warning("5pct reconcile llm failed err=%s", str(e))

    return disclosed, expected


def _locate_pages(text_blocks):
    disclosed_pages = set()
    expected_pages = set()

    blocks = [b for b in sorted(text_blocks, key=lambda x: x.page) if b.page >= 30]
    top_heading_re = re.compile(r"[一二三四五六七八九十]{1,3}、")
    parent_heading_re = re.compile(r"^\s*第[一二三四五六七八九十百零〇\d]+节\s*(发行人|公司)基本情况\s*$", re.M)
    section_heading_re = re.compile(r"^\s*第[一二三四五六七八九十百零〇\d]+节\s*", re.M)

    parent_hits = []
    for i, b in enumerate(blocks):
        t = b.text or ""
        if "......" in t:
            continue
        if parent_heading_re.search(t):
            parent_hits.append(i)

    search_start = 0
    search_end = len(blocks)
    if parent_hits:
        # 取首次正文命中，避免后文“详见第五节公司基本情况”引用语句把范围带偏
        search_start = parent_hits[0]
        for j in range(search_start + 1, len(blocks)):
            t = blocks[j].text or ""
            if section_heading_re.search(t):
                search_end = j
                break

    disclosed_start_idx = None
    expected_start_idx = None
    disclosed_heading_re = re.compile(r"持(?:有发行人|有公司|股)?\s*5\s*[%％]?\s*以上.*股东.*实际控制人")

    for i in range(search_start, search_end):
        t = (blocks[i].text or "").replace("\n", "")
        if "......" in t:
            continue

        if disclosed_start_idx is None and (
            any(k in t for k in ["持有发行人5%以上", "持有发行人 5%以上", "持股5%以上股东", "持股 5%以上股东"])
            or bool(disclosed_heading_re.search(t))
        ):
            disclosed_start_idx = i

        if expected_start_idx is None and any(k in t for k in ["发行人股本情况", "公司股本情况", "发行前股本", "发行前股本结构"]):
            expected_start_idx = i

    # 兜底：父章节内没命中时，回退全文
    if disclosed_start_idx is None and expected_start_idx is None:
        for i, b in enumerate(blocks):
            t = (b.text or "").replace("\n", "")
            if "......" in t:
                continue
            if disclosed_start_idx is None and (
                any(k in t for k in ["持有发行人5%以上", "持有发行人 5%以上", "持股5%以上股东", "持股 5%以上股东"])
                or bool(disclosed_heading_re.search(t))
            ):
                disclosed_start_idx = i
            if expected_start_idx is None and any(k in t for k in ["发行人股本情况", "公司股本情况", "发行前股本", "发行前股本结构"]):
                expected_start_idx = i

    def _collect_until_next_peer(start_idx: int | None) -> set[int]:
        out = set()
        if start_idx is None:
            return out
        start_page = blocks[start_idx].page
        for j in range(start_idx, len(blocks)):
            t = (blocks[j].text or "").replace("\n", "")
            if j > start_idx and top_heading_re.search(t):
                # 边界页往往同时包含上一节末尾内容，保留该页以免漏提取
                out.add(blocks[j].page)
                break
            out.add(blocks[j].page)
        # 防御性兜底：至少保留起始页；正常以“下一个同级标题”作为终止边界
        if not out:
            out.add(start_page)
        return out

    disclosed_pages = _collect_until_next_peer(disclosed_start_idx)

    # expected 仍保持保守：取目标标题页及后一页
    if expected_start_idx is not None:
        p = blocks[expected_start_idx].page
        expected_pages.update(range(p, p + 2))

    return disclosed_pages, expected_pages


def main() -> None:
    parser = argparse.ArgumentParser(description="Run 5% shareholder disclosure check")
    parser.add_argument("--pdf", required=True)
    parser.add_argument("--workdir", required=True)
    parser.add_argument("--preprocessed", default="", help="Optional shared preprocessed.json path")
    args = parser.parse_args()

    pdf_path = Path(args.pdf).resolve()
    workdir = Path(args.workdir).resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    log_dir = workdir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("shareholder_5pct_pipeline")
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
        logger.info("extract reused: %s (text_blocks=%s table_blocks=%s)", preprocessed_path, len(text_blocks), len(table_blocks))
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

    # 披露口径优先：默认使用LLM从“5%以上披露段落”抽取主体（无key/失败自动回退规则抽取）。
    # 避免把“其他股东股权结构表”中的穿透层自然人误计为披露股东。
    disclosed = _extract_disclosed_with_llm(text_blocks, disclosed_pages, logger)

    # 过滤发行人自身名称，避免被LLM误当作股东主体
    issuer_name = None
    m_issuer = re.search(r"^[0-9]+_(.+?)_申报稿\.pdf$", pdf_path.name)
    if m_issuer:
        issuer_name = _norm_name(m_issuer.group(1))
    if issuer_name:
        disclosed = [x for x in disclosed if _norm_name(x.get("name", "")) != issuer_name]

    expected = _extract_from_tables(table_blocks, expected_pages)
    expected_text = _extract_expected_from_text(text_blocks, expected_pages)
    by_name_expected = {_norm_name(x["name"]): x for x in expected}
    for x in expected_text:
        k = _norm_name(x["name"])
        if k not in by_name_expected or (x.get("holding_pct") or 0) > (by_name_expected[k].get("holding_pct") or 0):
            by_name_expected[k] = x
    expected = [x for x in by_name_expected.values() if (x.get("holding_pct") or 0) >= 4.9]

    # 二次LLM归一：对齐简称/全称与口径差异，输出可回溯页码
    disclosed_norm, expected_norm = _reconcile_lists_with_llm(disclosed, expected, logger)

    # 可信度增强：expected_list 优先保留表格提取的 holding_pct
    expected_raw_by_norm = {_norm_name(x.get("name", "")): x for x in expected}
    for x in expected_norm:
        if x.get("holding_pct") is not None:
            continue
        n = _norm_name(x.get("name", ""))
        raw_name_norm = _norm_name(x.get("raw_name", ""))
        hit = expected_raw_by_norm.get(n) or expected_raw_by_norm.get(raw_name_norm)
        if hit is None:
            # 简称/全称兜底匹配
            for k, v in expected_raw_by_norm.items():
                if n and (n in k or k in n):
                    hit = v
                    break
        if hit is not None:
            x["holding_pct"] = hit.get("holding_pct")
            x["page"] = x.get("page") or hit.get("page")

    disclosed_set = {_norm_name(x["name"]) for x in disclosed_norm}
    missing = [x for x in expected_norm if _norm_name(x["name"]) not in disclosed_set]

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

    # 对齐价格波动模块的前端消费习惯（message + page）
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

    result = {
        "summary": {
            "disclosed_count": len(disclosed_norm),
            "expected_count": len(expected_norm),
            "missing_count": len(missing),
            "status": "pass" if not missing else "fail",
        },
        "issues": issues,
        "alerts": alerts,
        "disclosed_list": disclosed_norm,
        "expected_list": expected_norm,
        "disclosed_list_raw": disclosed,
        "expected_list_raw": expected,
        "missing_shareholders": missing,
        "evidence_pages": {
            "disclosed_pages": sorted(disclosed_pages),
            "expected_pages": sorted(expected_pages),
        },
    }

    (workdir / "disclosed_list.json").write_text(json.dumps(disclosed_norm, ensure_ascii=False, indent=2), encoding="utf-8")
    (workdir / "expected_list.json").write_text(json.dumps(expected_norm, ensure_ascii=False, indent=2), encoding="utf-8")
    (workdir / "disclosed_list_raw.json").write_text(json.dumps(disclosed, ensure_ascii=False, indent=2), encoding="utf-8")
    (workdir / "expected_list_raw.json").write_text(json.dumps(expected, ensure_ascii=False, indent=2), encoding="utf-8")
    (workdir / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    logger.info("pipeline done: disclosed=%s expected=%s missing=%s", len(disclosed_norm), len(expected_norm), len(missing))
    print(f"Done. status={result['summary']['status']} missing={len(missing)}")
    print(f"Artifacts: {workdir}")


if __name__ == "__main__":
    main()
