from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import date
from decimal import Decimal
from pathlib import Path

from dateutil import parser as dt_parser
from openai import OpenAI

from ..schemas.models import CapitalIncreaseEvent, ExtractionResult, TransferEvent


class LlmExtractionService:
    def __init__(self) -> None:
        self.logger = logging.getLogger("price_fluctuation_pipeline")
        self.api_key = os.getenv("ARK_API_KEY") or os.getenv("OPENAI_API_KEY")
        self.model = os.getenv("EXTRACTION_MODEL", "doubao-1-5-pro-32k-250115")
        self.base_url = os.getenv("ARK_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3")
        self.client = OpenAI(api_key=self.api_key, base_url=self.base_url) if self.api_key else None
        self.max_retries = int(os.getenv("LLM_MAX_RETRIES", "3"))
        self.prompt = (Path(__file__).resolve().parent.parent / "prompts" / "extraction_prompt_zh.txt").read_text(encoding="utf-8")

    def extract(self, text: str, pages: list[int]) -> ExtractionResult:
        if self.client is None:
            self.logger.info("llm disabled(no api key), fallback heuristic pages=%s", pages)
            base = self._heuristic_extract(text, pages)
            p_t, p_i = self._pattern_extract(text, pages)
            base.transfer_events.extend(p_t)
            base.increase_events.extend(p_i)
            return base

        raw = self._extract_with_llm(text)
        if raw is None:
            self.logger.info("llm failed after retries, fallback heuristic pages=%s", pages)
            base = self._heuristic_extract(text, pages)
            p_t, p_i = self._pattern_extract(text, pages)
            base.transfer_events.extend(p_t)
            base.increase_events.extend(p_i)
            return base

        try:
            data = json.loads(raw)
        except Exception:
            return self._heuristic_extract(text, pages)

        transfer_list = data.get("transfer_events") or []
        increase_list = data.get("increase_events") or []
        if not isinstance(transfer_list, list):
            transfer_list = []
        if not isinstance(increase_list, list):
            increase_list = []

        default_currency = "USD" if ("美元" in text and "人民币" not in text) else "CNY"
        transfers = [self._to_transfer(e, pages, default_currency=default_currency) for e in transfer_list if isinstance(e, dict)]
        increases = [self._to_increase(e, pages, default_currency=default_currency) for e in increase_list if isinstance(e, dict)]
        p_transfers, p_increases = self._pattern_extract(text, pages)
        transfers.extend(p_transfers)
        increases.extend(p_increases)
        return ExtractionResult(transfer_events=transfers, increase_events=increases)

    def _extract_with_llm(self, text: str) -> str | None:
        self.logger.info("llm call start: model=%s chars=%s", self.model, len(text))
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = self.client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": self.prompt},
                        {"role": "user", "content": text[:24000]},
                    ],
                    temperature=0,
                )
                out = (resp.choices[0].message.content or "").strip()
                self.logger.info("llm call success: attempt=%s out_chars=%s preview=%s", attempt, len(out), out[:240].replace("\n", " "))
                return out
            except Exception as e:
                self.logger.warning("llm call failed: attempt=%s err=%s", attempt, str(e))
                if attempt >= self.max_retries:
                    return None
                time.sleep(min(2 ** (attempt - 1), 3))
        return None

    def _heuristic_extract(self, text: str, pages: list[int]) -> ExtractionResult:
        transfers: list[TransferEvent] = []
        increases: list[CapitalIncreaseEvent] = []

        if "转让" in text or "受让" in text:
            transfers.append(
                TransferEvent(
                    time=self._extract_date(text),
                    transferor=self._extract_party(text, ["转让方", "出让方"]),
                    transferee=self._extract_party(text, ["受让方"]),
                    shares=self._extract_number(text, ["股", "万股"]),
                    amount_cny=self._extract_number(text, ["元", "万元", "美元"]),
                    unit_price_cny_per_share=self._extract_unit_price(text),
                    currency="USD" if "美元" in text else "CNY",
                    source_pages=pages,
                )
            )

        if "增资" in text or "认购" in text:
            increases.append(
                CapitalIncreaseEvent(
                    time=self._extract_date(text),
                    investor=self._extract_party(text, ["增资方", "投资方", "认购方"]),
                    shares=self._extract_number(text, ["股", "万股"]),
                    amount_cny=self._extract_number(text, ["元", "万元", "美元"]),
                    unit_price_cny_per_share=self._extract_unit_price(text),
                    currency="USD" if "美元" in text else "CNY",
                    source_pages=pages,
                )
            )

        return ExtractionResult(transfer_events=transfers, increase_events=increases)

    def _pattern_extract(self, text: str, pages: list[int]) -> tuple[list[TransferEvent], list[CapitalIncreaseEvent]]:
        transfers: list[TransferEvent] = []
        increases: list[CapitalIncreaseEvent] = []

        lines = [x.strip() for x in text.splitlines() if x.strip()]
        date_re = re.compile(r"(20\d{2})\s*年\s*(\d{1,2})\s*月")

        # 跨行规则：从“对应出资X万元，以Y万元价格转让”计算单价
        block_transfer_re = re.compile(
            r"(20\d{2}\s*年\s*\d{1,2}\s*月)[\s\S]{0,180}?对应出资\s*([0-9]+(?:\.[0-9]+)?)\s*万?元[\s\S]{0,120}?以\s*([0-9]+(?:\.[0-9]+)?)\s*万?元(?:的)?价格转让"
        )
        for m in block_transfer_re.finditer(text):
            d = self._parse_date(m.group(1))
            cap = Decimal(m.group(2))
            amt = Decimal(m.group(3))
            shares = cap * Decimal("10000")
            amount = amt * Decimal("10000")
            unit = amount / shares if shares else None
            transfers.append(
                TransferEvent(
                    time=d,
                    shares=shares,
                    amount_cny=amount,
                    unit_price_cny_per_share=unit,
                    currency="CNY",
                    source_pages=pages,
                )
            )

        # 跨行规则：增资价格为X元/股，前文就近取年月
        block_inc_re = re.compile(r"(20\d{2}\s*年\s*\d{1,2}\s*月)[\s\S]{0,120}?增资价格为\s*([0-9]+(?:\.[0-9]+)?)\s*元\s*/\s*股")
        for m in block_inc_re.finditer(text):
            d = self._parse_date(m.group(1))
            increases.append(
                CapitalIncreaseEvent(
                    time=d,
                    unit_price_cny_per_share=Decimal(m.group(2)),
                    currency="CNY",
                    source_pages=pages,
                )
            )

        for ln in lines:
            # 增资/定向发行价格直接给出
            m_inc = re.search(r"(?:增资价格为|以)\s*([0-9]+(?:\.[0-9]+)?)\s*元\s*/\s*股", ln)
            if ("增资" in ln or "定向发行" in ln or "发行" in ln) and m_inc:
                d = self._extract_date(ln)
                if d is None:
                    md = date_re.search(ln)
                    if md:
                        d = date(int(md.group(1)), int(md.group(2)), 1)
                increases.append(
                    CapitalIncreaseEvent(
                        time=d,
                        investor=None,
                        shares=None,
                        amount_cny=None,
                        unit_price_cny_per_share=Decimal(m_inc.group(1)),
                        currency="CNY",
                        source_pages=pages,
                    )
                )

            # 股权转让：若有“对应出资X万元，以Y万元的价格转让”可计算单价
            if "转让" in ln:
                m_cap = re.search(r"对应出资\s*([0-9]+(?:\.[0-9]+)?)\s*万?元", ln)
                m_amt = re.search(r"以\s*([0-9]+(?:\.[0-9]+)?)\s*万?元(?:的)?价格转让", ln)
                d = self._extract_date(ln)
                unit_price = None
                shares = None
                amount = None
                if m_cap and m_amt:
                    cap = Decimal(m_cap.group(1))
                    amt = Decimal(m_amt.group(1))
                    # 若单位带“万元”，按同量纲相除即可得 元/股（注册资本1元/股口径）
                    shares = cap * Decimal("10000")
                    amount = amt * Decimal("10000")
                    if shares != 0:
                        unit_price = amount / shares
                m_up = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*元\s*/\s*股", ln)
                if m_up:
                    unit_price = Decimal(m_up.group(1))
                if unit_price is not None or m_cap or m_amt:
                    transfers.append(
                        TransferEvent(
                            time=d,
                            transferor=None,
                            transferee=None,
                            shares=shares,
                            amount_cny=amount,
                            unit_price_cny_per_share=unit_price,
                            currency="CNY",
                            source_pages=pages,
                        )
                    )

        return transfers, increases

    @staticmethod
    def _extract_date(text: str) -> date | None:
        m = re.search(r"(20\d{2}[年\-/\.\s]\d{1,2}[月\-/\.\s]\d{1,2}日?)", text)
        if not m:
            m = re.search(r"(20\d{2}[年\-/\.\s]\d{1,2}月)", text)
        if not m:
            return None
        s = m.group(1).replace("年", "-").replace("月", "-").replace("日", "")
        s = re.sub(r"\s+", "", s)
        try:
            d = dt_parser.parse(s, default=dt_parser.parse("2000-01-28"))
            return d.date()
        except Exception:
            return None

    @staticmethod
    def _extract_party(text: str, keys: list[str]) -> str | None:
        for k in keys:
            m = re.search(rf"{k}[：:]?\s*([\u4e00-\u9fa5A-Za-z0-9（）()·\.\-]+)", text)
            if m:
                return m.group(1)
        return None

    @staticmethod
    def _extract_number(text: str, units: list[str]) -> Decimal | None:
        for u in units:
            m = re.search(rf"([0-9]+(?:\.[0-9]+)?)\s*{u}", text)
            if m:
                val = Decimal(m.group(1))
                if u == "万元":
                    return val * Decimal("10000")
                return val
        return None

    @staticmethod
    def _extract_unit_price(text: str) -> Decimal | None:
        m = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*元\s*/\s*股", text)
        return Decimal(m.group(1)) if m else None

    @staticmethod
    def _to_transfer(d: dict, pages: list[int], default_currency: str = "CNY") -> TransferEvent:
        return TransferEvent(
            time=LlmExtractionService._parse_date(d.get("time")),
            transferor=LlmExtractionService._to_text(d.get("transferor")),
            transferee=LlmExtractionService._to_text(d.get("transferee")),
            shares=LlmExtractionService._to_shares(d.get("shares")),
            amount_cny=LlmExtractionService._to_amount_yuan(d.get("amount")),
            unit_price_cny_per_share=LlmExtractionService._to_price(d.get("unit_price")),
            currency=(d.get("currency") or default_currency).upper(),
            source_pages=pages,
        )

    @staticmethod
    def _to_increase(d: dict, pages: list[int], default_currency: str = "CNY") -> CapitalIncreaseEvent:
        return CapitalIncreaseEvent(
            time=LlmExtractionService._parse_date(d.get("time")),
            investor=LlmExtractionService._to_text(d.get("investor")),
            shares=LlmExtractionService._to_shares(d.get("shares")),
            amount_cny=LlmExtractionService._to_amount_yuan(d.get("amount")),
            unit_price_cny_per_share=LlmExtractionService._to_price(d.get("unit_price")),
            currency=(d.get("currency") or default_currency).upper(),
            source_pages=pages,
        )

    @staticmethod
    def _to_text(v) -> str | None:
        if v is None:
            return None
        if isinstance(v, list):
            return "、".join([str(x) for x in v if x is not None]) or None
        return str(v)

    @staticmethod
    def _safe_eval_numeric_expr(s: str) -> Decimal | None:
        if not re.fullmatch(r"[0-9\.\s\+\-\*/\(\)]+", s):
            return None
        try:
            return Decimal(str(eval(s, {"__builtins__": {}}, {})))
        except Exception:
            return None

    @staticmethod
    def _to_amount_yuan(v) -> Decimal | None:
        if v is None:
            return None
        if isinstance(v, (int, float, Decimal)):
            return Decimal(str(v))
        s = str(v).replace(",", "").strip()
        if not s:
            return None
        if "万元" in s:
            m = re.search(r"([0-9]+(?:\.[0-9]+)?)", s)
            return (Decimal(m.group(1)) * Decimal("10000")) if m else None
        d = LlmExtractionService._safe_eval_numeric_expr(s)
        if d is not None:
            return d
        m = re.search(r"([0-9]+(?:\.[0-9]+)?)", s)
        return Decimal(m.group(1)) if m else None

    @staticmethod
    def _to_shares(v) -> Decimal | None:
        if v is None:
            return None
        if isinstance(v, (int, float, Decimal)):
            return Decimal(str(v))
        s = str(v).replace(",", "").strip()
        if not s:
            return None
        # 百分比无法直接转股数
        if "%" in s:
            return None
        if "万股" in s:
            m = re.search(r"([0-9]+(?:\.[0-9]+)?)", s)
            return (Decimal(m.group(1)) * Decimal("10000")) if m else None
        if "万" in s and "股" not in s:
            m = re.search(r"([0-9]+(?:\.[0-9]+)?)", s)
            return (Decimal(m.group(1)) * Decimal("10000")) if m else None
        d = LlmExtractionService._safe_eval_numeric_expr(s)
        if d is not None:
            return d
        m = re.search(r"([0-9]+(?:\.[0-9]+)?)", s)
        return Decimal(m.group(1)) if m else None

    @staticmethod
    def _to_price(v) -> Decimal | None:
        if v is None:
            return None
        if isinstance(v, (int, float, Decimal)):
            return Decimal(str(v))
        s = str(v).replace(",", "").strip()
        d = LlmExtractionService._safe_eval_numeric_expr(s)
        if d is not None:
            return d
        m = re.search(r"([0-9]+(?:\.[0-9]+)?)", s)
        return Decimal(m.group(1)) if m else None

    @staticmethod
    def _parse_date(v) -> date | None:
        if not v:
            return None
        s = str(v).strip()
        m = re.search(r"(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})?\s*日?", s)
        if m:
            y = int(m.group(1))
            mm = int(m.group(2))
            dd = int(m.group(3) or 1)
            try:
                return date(y, mm, dd)
            except Exception:
                pass
        try:
            return dt_parser.parse(s).date()
        except Exception:
            return None
