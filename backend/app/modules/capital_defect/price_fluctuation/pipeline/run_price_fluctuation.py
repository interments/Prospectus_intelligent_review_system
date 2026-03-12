from __future__ import annotations

import argparse
import csv
import json
import logging
from pathlib import Path

from dotenv import load_dotenv

from ..extractors.pdf_router import PdfRouter
from ..parsers.document_segmenter import DocumentSegmenter
from ..schemas.models import PriceFluctuationOutput, TableBlock, TextBlock
from ..services.fluctuation_judge_service import FluctuationJudgeService
from ..services.llm_extraction_service import LlmExtractionService
from ..services.price_calc_service import PriceCalcService


def _slice_report_period_chunks(chunks):
    """优先提取“报告期内股本和股东变化情况”章节，避免混入设立/整体变更口径。"""
    if not chunks:
        return chunks

    start_idx = None
    for i, c in enumerate(chunks):
        t = (c.title or "") + "\n" + (c.content[:120] if c.content else "")
        if "报告期内股本" in t or "股东变化情况" in t:
            start_idx = i
            break

    if start_idx is None:
        return chunks

    end_idx = len(chunks)
    stop_markers = ["发行人股权结构", "子公司及参股公司", "重大资产重组", "业务与技术", "董事、监事、高级管理人员"]
    for j in range(start_idx + 1, len(chunks)):
        t = (chunks[j].title or "") + "\n" + (chunks[j].content[:120] if chunks[j].content else "")
        if any(m in t for m in stop_markers):
            end_idx = j
            break

    return chunks[start_idx:end_idx]


def _write_transfer_df_csv(path: Path, events) -> None:
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["时间", "转让方", "受让方", "股数", "金额(元)", "股票单价(元/股)", "币种", "页码"])
        for e in events:
            w.writerow([
                e.time,
                e.transferor,
                e.transferee,
                e.shares,
                e.amount_cny,
                e.unit_price_cny_per_share,
                e.currency,
                ",".join(str(p) for p in e.source_pages),
            ])


def _write_increase_df_csv(path: Path, events) -> None:
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["时间", "增资方", "股数", "金额(元)", "股票单价(元/股)", "币种", "页码"])
        for e in events:
            w.writerow([
                e.time,
                e.investor,
                e.shares,
                e.amount_cny,
                e.unit_price_cny_per_share,
                e.currency,
                ",".join(str(p) for p in e.source_pages),
            ])


def main() -> None:
    base_dir = Path(__file__).resolve().parents[6]  # Prospectus_intelligent_review_system
    backend_dir = base_dir / "backend"
    load_dotenv(base_dir / ".env")
    load_dotenv(backend_dir / ".env")

    parser = argparse.ArgumentParser(description="Run capital defect price fluctuation pipeline")
    parser.add_argument("--pdf", required=True, help="Path to input pdf")
    parser.add_argument("--workdir", required=True, help="Artifact output dir")
    parser.add_argument("--max-chunks", type=int, default=0, help="Debug: limit number of chunks processed (0=all)")
    parser.add_argument("--preprocessed", default="", help="Optional shared preprocessed.json path")
    args = parser.parse_args()

    pdf_path = Path(args.pdf).resolve()
    workdir = Path(args.workdir).resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    log_dir = workdir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("price_fluctuation_pipeline")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    file_handler = logging.FileHandler(log_dir / "run.log", encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(file_handler)

    router = PdfRouter()
    segmenter = DocumentSegmenter()
    extractor = LlmExtractionService()
    calc = PriceCalcService()
    judge = FluctuationJudgeService()

    logger.info("pipeline start: pdf=%s", str(pdf_path))

    preprocessed_path = Path(args.preprocessed).resolve() if args.preprocessed else None
    if preprocessed_path and preprocessed_path.exists():
        payload = json.loads(preprocessed_path.read_text(encoding="utf-8"))
        text_blocks = [TextBlock.model_validate(x) for x in payload.get("text_blocks", [])]
        table_blocks = [TableBlock.model_validate(x) for x in payload.get("table_blocks", [])]
        logger.info("extract reused: %s (text_blocks=%s table_blocks=%s)", preprocessed_path, len(text_blocks), len(table_blocks))
    else:
        text_blocks, table_blocks = router.extract(pdf_path)
        logger.info("extract done: text_blocks=%s table_blocks=%s", len(text_blocks), len(table_blocks))

    pre = {
        "text_blocks": [tb.model_dump() for tb in text_blocks],
        "table_blocks": [tb.model_dump() for tb in table_blocks],
    }
    (workdir / "preprocessed.json").write_text(json.dumps(pre, ensure_ascii=False, indent=2), encoding="utf-8")

    chunks, segment_debug = segmenter.segment(text_blocks)
    logger.info("section locate/segment done: chunks=%s", len(chunks))
    (workdir / "segment_debug.json").write_text(json.dumps(segment_debug, ensure_ascii=False, indent=2), encoding="utf-8")
    (workdir / "chunks.json").write_text(
        json.dumps([c.model_dump() for c in chunks], ensure_ascii=False, indent=2), encoding="utf-8"
    )

    filtered_chunks = _slice_report_period_chunks(chunks)
    logger.info("report-period chunks: %s", len(filtered_chunks))
    (workdir / "chunks_report_period.json").write_text(
        json.dumps([c.model_dump() for c in filtered_chunks], ensure_ascii=False, indent=2), encoding="utf-8"
    )

    transfer_events = []
    increase_events = []
    extraction_debug = []
    base_chunks = filtered_chunks or chunks
    to_process = base_chunks[: args.max_chunks] if args.max_chunks and args.max_chunks > 0 else base_chunks
    logger.info("processing chunks: %s", len(to_process))
    for idx, chunk in enumerate(to_process):
        r = extractor.extract(chunk.content, list(range(chunk.page_start, chunk.page_end + 1)))
        transfer_events.extend(r.transfer_events)
        increase_events.extend(r.increase_events)
        extraction_debug.append(
            {
                "chunk_index": idx,
                "chunk_title": chunk.title,
                "pages": [chunk.page_start, chunk.page_end],
                "transfer_count": len(r.transfer_events),
                "increase_count": len(r.increase_events),
            }
        )

    (workdir / "debug_extraction_counts.json").write_text(
        json.dumps(extraction_debug, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (workdir / "transfer_events_raw.json").write_text(
        json.dumps([e.model_dump(mode="json") for e in transfer_events], ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (workdir / "increase_events_raw.json").write_text(
        json.dumps([e.model_dump(mode="json") for e in increase_events], ensure_ascii=False, indent=2), encoding="utf-8"
    )
    logger.info("event extraction done: transfer=%s increase=%s", len(transfer_events), len(increase_events))
    _write_transfer_df_csv(workdir / "transfer_events_df_raw.csv", transfer_events)
    _write_increase_df_csv(workdir / "increase_events_df_raw.csv", increase_events)

    transfer_events, increase_events, dropped = calc.clean_events(transfer_events, increase_events)
    logger.info("clean done: transfer=%s increase=%s dropped=%s", len(transfer_events), len(increase_events), len(dropped))
    (workdir / "dropped_events.json").write_text(json.dumps(dropped, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_transfer_df_csv(workdir / "transfer_events_df.csv", transfer_events)
    _write_increase_df_csv(workdir / "increase_events_df.csv", increase_events)

    timeline = calc.normalize_events(transfer_events, increase_events)
    timeline = sorted(timeline, key=lambda x: x.time)
    alerts = judge.judge(timeline)
    logger.info("judge done: timeline=%s alerts=%s", len(timeline), len(alerts))

    out = PriceFluctuationOutput(timeline=timeline, alerts=alerts)
    (workdir / "result.json").write_text(out.model_dump_json(indent=2), encoding="utf-8")

    logger.info("pipeline done")
    print(f"Done. timeline={len(timeline)} alerts={len(alerts)}")
    print(f"Artifacts: {workdir}")


if __name__ == "__main__":
    main()
