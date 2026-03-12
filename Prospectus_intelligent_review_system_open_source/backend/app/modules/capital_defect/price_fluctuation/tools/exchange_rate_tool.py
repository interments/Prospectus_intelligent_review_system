from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import akshare as ak
import pandas as pd


class ExchangeRateTool:
    """USD/CNY historical fx with nearest available previous day fallback."""

    def __init__(self) -> None:
        self._cache: pd.DataFrame | None = None

    def _load(self) -> pd.DataFrame:
        if self._cache is not None:
            return self._cache
        # Macro China: often returns columns including 日期 and 美元/人民币
        df = ak.macro_china_fx_usdcny()
        if df.empty:
            raise RuntimeError("akshare returned empty USD/CNY data")
        df = df.copy()
        if "日期" not in df.columns:
            raise RuntimeError(f"unexpected fx dataframe columns: {df.columns.tolist()}")
        # try common value columns
        value_col = None
        for c in ["美元/人民币", "今值", "value", "收盘"]:
            if c in df.columns:
                value_col = c
                break
        if value_col is None:
            # fallback to first numeric column
            numerics = [c for c in df.columns if pd.api.types.is_numeric_dtype(df[c])]
            if not numerics:
                raise RuntimeError("cannot locate fx value column")
            value_col = numerics[0]

        df = df[["日期", value_col]].rename(columns={value_col: "rate"})
        df["日期"] = pd.to_datetime(df["日期"]).dt.date
        df["rate"] = pd.to_numeric(df["rate"], errors="coerce")
        df = df.dropna(subset=["rate"]).sort_values("日期")
        self._cache = df
        return df

    def usd_to_cny_rate(self, d: date) -> Decimal:
        df = self._load()
        exact = df[df["日期"] == d]
        if not exact.empty:
            return Decimal(str(exact.iloc[-1]["rate"]))
        # nearest previous day in data
        prev = df[df["日期"] <= d]
        if not prev.empty:
            return Decimal(str(prev.iloc[-1]["rate"]))
        # if target earlier than dataset begin, use earliest
        return Decimal(str(df.iloc[0]["rate"]))

    def convert_usd_to_cny(self, amount_usd: Decimal, d: date) -> Decimal:
        return amount_usd * self.usd_to_cny_rate(d)
