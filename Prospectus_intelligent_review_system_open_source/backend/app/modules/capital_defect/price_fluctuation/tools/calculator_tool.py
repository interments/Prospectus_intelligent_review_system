from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP, getcontext

getcontext().prec = 28


def safe_divide(a: Decimal, b: Decimal, quant: str = "0.0001") -> Decimal | None:
    if b == 0:
        return None
    return (a / b).quantize(Decimal(quant), rounding=ROUND_HALF_UP)


def pct_change_abs(prev_price: Decimal, curr_price: Decimal, quant: str = "0.0001") -> Decimal | None:
    if prev_price == 0:
        return None
    return (abs(curr_price - prev_price) / prev_price).quantize(Decimal(quant), rounding=ROUND_HALF_UP)
