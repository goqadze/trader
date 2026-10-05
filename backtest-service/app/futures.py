"""Index futures contracts for the intraday backtests: what a point is worth, the tick, and what a trade costs.

Their 5-minute prices come from decision-service, rebuilt from the ETF that tracks the same index (QQQ for NQ / MNQ,
SPY for ES / MES, DIA for YM / MYM; see decision-service/app/futures.py and keep the roots in sync). A future trades
in whole contracts on margin: P&L is points x the multiplier, the cost is a fee per contract, not a share of the
value."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Contract:
    root: str
    name: str
    etf: str  # the ETF its prices are rebuilt from
    multiplier: float  # dollars per point
    tick: float  # the smallest price step
    fee_per_side: float  # dollars per contract per fill: IBKR's tiered commission plus exchange and regulatory fees, about


CONTRACTS = {c.root: c for c in (
    Contract("NQ", "E-mini Nasdaq-100", "QQQ", 20, 0.25, 2.25),
    Contract("MNQ", "Micro E-mini Nasdaq-100", "QQQ", 2, 0.25, 0.62),
    Contract("ES", "E-mini S&P 500", "SPY", 50, 0.25, 2.25),
    Contract("MES", "Micro E-mini S&P 500", "SPY", 5, 0.25, 0.62),
    Contract("YM", "E-mini Dow", "DIA", 5, 1.0, 2.25),
    Contract("MYM", "Micro E-mini Dow", "DIA", 0.5, 1.0, 0.62),
)}

# The margin held per contract as a share of its value. CME's initial margin on the index futures is roughly 5-10% of
# the contract; 10% caps the contract count cautiously (a $10,000 account: 2 MNQ at 25,000, i.e. $100,000 of Nasdaq).
MARGIN_PCT = 0.10
SLIPPAGE_TICKS = 1  # a market fill (a market entry, a stop, the 15:55 exit) costs a tick; limit fills none
