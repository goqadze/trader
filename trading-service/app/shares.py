"""Share quantities: whole shares, or fractions of one for a dip bot with `fractional` on.

Alpaca sells fractions of most US stocks and ETFs (market orders, good for the day, $1 or more, up to 9 decimals); a
symbol it can't split is bought in whole shares. The backtest sizes its buys with the same rule
(backtest-service/app/engines/dip.py), so a fractional backtest and a fractional bot buy the same amounts.
"""

import math

QTY_DECIMALS = 6  # a fraction is cut to a millionth of a share: under a cent of the slot is left unspent
MIN_FRACTIONAL_ORDER = 1.0  # Alpaca's smallest fractional order, in dollars


def affordable(budget: float, unit: float, fractional: bool = False) -> float:
    """How many shares `budget` buys at `unit` dollars each (slippage and fee included): whole shares, or with
    `fractional` down to a millionth of one. 0 when that isn't even one share (or, in fractions, under the $1 minimum)."""
    if unit <= 0 or budget <= 0:
        return 0
    if not fractional:
        return math.floor(budget / unit)
    qty = math.floor(budget / unit * 10**QTY_DECIMALS) / 10**QTY_DECIMALS
    return qty if qty * unit >= MIN_FRACTIONAL_ORDER else 0


def tidy(qty: float) -> float:
    """A quantity after adding or subtracting fills, without float noise (0.1 + 0.2 = 0.30000000000000004)."""
    return round(qty, 9)


def same_qty(a: float | None, b: float | None) -> bool:
    """Do two quantities agree (the broker's and ours), allowing for float rounding of fractions?"""
    return abs((a or 0) - (b or 0)) < 1e-6


def fmt_qty(qty: float | None) -> str:
    """Shares as people write them: 12 for whole shares, 0.142857 for a fraction."""
    qty = qty or 0
    return str(int(round(qty))) if same_qty(qty, round(qty)) else f"{qty:.6f}".rstrip("0").rstrip(".")
