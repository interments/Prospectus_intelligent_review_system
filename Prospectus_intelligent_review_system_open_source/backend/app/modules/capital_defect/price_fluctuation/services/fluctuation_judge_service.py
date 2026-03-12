from __future__ import annotations

from datetime import date
from decimal import Decimal

from dateutil.relativedelta import relativedelta

from ..schemas.models import FluctuationAlert, TimelineRow
from ..tools.calculator_tool import pct_change_abs


class FluctuationJudgeService:
    short_window_months = 6
    short_threshold = Decimal("0.05")
    long_threshold = Decimal("0.15")

    def judge(self, timeline: list[TimelineRow]) -> list[FluctuationAlert]:
        if not timeline:
            return []

        ordered = sorted(enumerate(timeline), key=lambda x: (x[1].time, x[0]))
        rows = [x[1] for x in ordered]

        alerts: list[FluctuationAlert] = []
        for i in range(len(rows) - 1):
            prev_row = rows[i]
            curr_row = rows[i + 1]
            ratio = pct_change_abs(prev_row.unit_price_cny_per_share, curr_row.unit_price_cny_per_share)
            if ratio is None:
                continue

            is_short = self._lt_6_natural_months(prev_row.time, curr_row.time)
            threshold = self.short_threshold if is_short else self.long_threshold
            if ratio > threshold:
                prev_desc = self._event_desc(prev_row)
                curr_desc = self._event_desc(curr_row)
                prev_text = self._event_text(prev_row)
                curr_text = self._event_text(curr_row)
                msg = (
                    f"{prev_row.time}，{prev_desc} 与 {curr_row.time}，{curr_desc} 存在明显股价波动，"
                    f"对应股价为{prev_row.unit_price_cny_per_share}元/股与{curr_row.unit_price_cny_per_share}元/股，"
                    f"变动幅度{(ratio*Decimal('100')).quantize(Decimal('0.01'))}% ，需披露。"
                )
                alerts.append(
                    FluctuationAlert(
                        message=msg,
                        page=curr_row.page,
                        previous_price=prev_row.unit_price_cny_per_share,
                        current_price=curr_row.unit_price_cny_per_share,
                        change_ratio=ratio,
                        previous_event_id=prev_row.source_event_id,
                        current_event_id=curr_row.source_event_id,
                        previous_event_page=prev_row.page,
                        current_event_page=curr_row.page,
                        previous_event_text=prev_text,
                        current_event_text=curr_text,
                    )
                )

        return alerts

    @staticmethod
    def _event_desc(row: TimelineRow) -> str:
        if row.event == "股权转让":
            return f"{row.transferor}与{row.transferee}的股权转让事件"
        return f"{row.transferee}的增资事件"

    @staticmethod
    def _event_text(row: TimelineRow) -> str:
        if row.event == "股权转让":
            return f"{row.time} {row.transferor}向{row.transferee}股权转让，价格{row.unit_price_cny_per_share}元/股"
        return f"{row.time} {row.transferee}增资，价格{row.unit_price_cny_per_share}元/股"

    def _lt_6_natural_months(self, d1: date, d2: date) -> bool:
        return d2 < (d1 + relativedelta(months=self.short_window_months))
