from __future__ import annotations

from datetime import date
from decimal import Decimal

from ..schemas.models import CapitalIncreaseEvent, TimelineRow, TransferEvent
from ..tools.calculator_tool import safe_divide
from ..tools.exchange_rate_tool import ExchangeRateTool


class PriceCalcService:
    def __init__(self) -> None:
        self.fx = ExchangeRateTool()
        self.fallback_usd_cny = Decimal("7.2000")

    def clean_events(
        self,
        transfer_events: list[TransferEvent],
        increase_events: list[CapitalIncreaseEvent],
    ) -> tuple[list[TransferEvent], list[CapitalIncreaseEvent], list[dict]]:
        cleaned_t: list[TransferEvent] = []
        cleaned_i: list[CapitalIncreaseEvent] = []
        dropped: list[dict] = []

        for i, e in enumerate(transfer_events):
            self._normalize_currency_and_price(e)
            if not e.transferor or not e.transferee:
                dropped.append({"event": f"transfer_{i}", "reason": "missing_entity"})
                continue
            if e.unit_price_cny_per_share is None and (e.amount_cny is None or e.shares is None):
                dropped.append({"event": f"transfer_{i}", "reason": "missing_price_and_uncomputable"})
                continue
            if e.unit_price_cny_per_share is None:
                dropped.append({"event": f"transfer_{i}", "reason": "missing_price_after_compute"})
                continue
            if self._is_implausible_price(e.unit_price_cny_per_share):
                dropped.append({"event": f"transfer_{i}", "reason": "implausible_unit_price"})
                continue
            cleaned_t.append(e)

        for i, e in enumerate(increase_events):
            self._normalize_currency_and_price(e)
            if not e.investor:
                dropped.append({"event": f"increase_{i}", "reason": "missing_entity"})
                continue
            if e.unit_price_cny_per_share is None and (e.amount_cny is None or e.shares is None):
                dropped.append({"event": f"increase_{i}", "reason": "missing_price_and_uncomputable"})
                continue
            if e.unit_price_cny_per_share is None:
                dropped.append({"event": f"increase_{i}", "reason": "missing_price_after_compute"})
                continue
            if self._is_implausible_price(e.unit_price_cny_per_share):
                dropped.append({"event": f"increase_{i}", "reason": "implausible_unit_price"})
                continue
            cleaned_i.append(e)

        return cleaned_t, cleaned_i, dropped

    def normalize_events(
        self,
        transfer_events: list[TransferEvent],
        increase_events: list[CapitalIncreaseEvent],
    ) -> list[TimelineRow]:
        rows: list[TimelineRow] = []

        for i, e in enumerate(transfer_events):
            if e.time and e.unit_price_cny_per_share is not None:
                rows.append(
                    TimelineRow(
                        time=e.time,
                        event="股权转让",
                        transferor=e.transferor or "无",
                        transferee=e.transferee or "无",
                        unit_price_cny_per_share=e.unit_price_cny_per_share,
                        page=e.source_pages[0] if e.source_pages else -1,
                        source_event_id=f"transfer_{i}",
                    )
                )

        for i, e in enumerate(increase_events):
            if e.time and e.unit_price_cny_per_share is not None:
                rows.append(
                    TimelineRow(
                        time=e.time,
                        event="增资",
                        transferor="无",
                        transferee=e.investor or "无",
                        unit_price_cny_per_share=e.unit_price_cny_per_share,
                        page=e.source_pages[0] if e.source_pages else -1,
                        source_event_id=f"increase_{i}",
                    )
                )

        return rows

    def _normalize_currency_and_price(self, event: TransferEvent | CapitalIncreaseEvent) -> None:
        if event.currency == "USD" and (event.amount_cny is not None or event.unit_price_cny_per_share is not None):
            d = event.time or date.today()
            try:
                if event.amount_cny is not None:
                    event.amount_cny = self.fx.convert_usd_to_cny(event.amount_cny, d)
                if event.unit_price_cny_per_share is not None:
                    event.unit_price_cny_per_share = self.fx.convert_usd_to_cny(event.unit_price_cny_per_share, d)
            except Exception:
                # 实验阶段兜底：汇率服务失败时使用固定汇率，保证主流程不中断
                if event.amount_cny is not None:
                    event.amount_cny = event.amount_cny * self.fallback_usd_cny
                if event.unit_price_cny_per_share is not None:
                    event.unit_price_cny_per_share = event.unit_price_cny_per_share * self.fallback_usd_cny

        if event.unit_price_cny_per_share is None and event.amount_cny is not None and event.shares is not None:
            event.unit_price_cny_per_share = safe_divide(Decimal(event.amount_cny), Decimal(event.shares))

    @staticmethod
    def _is_implausible_price(price: Decimal | None) -> bool:
        if price is None:
            return True
        # 极小值常见于“交易总额/非股本单位”误抽成单价
        if price < Decimal("0.01"):
            return True
        # 极大值在本模块样本中通常为口径错误（如总额误入）
        if price > Decimal("10000"):
            return True
        return False
