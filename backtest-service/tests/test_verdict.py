"""The recommendation after a backtest: which checks a result passes and the grade they add up to."""

from datetime import date

from app.models import RunConfig
from app.verdict import judge

YEAR = RunConfig(start=date(2025, 10, 4), end=date(2026, 10, 4))


def _result(**kw):
    base = {"num_trades": 24, "total_return_pct": 18.0, "buy_hold_return_pct": 12.0, "beat_buy_hold": True,
            "avg_trade_pct": 0.8, "profit_factor": 1.7, "win_rate_pct": 55.0, "sharpe": 1.4, "buy_hold_sharpe": 1.0,
            "max_drawdown_pct": -7.0, "decision_errors": 0, "breaker_tripped_on": None}
    return base | kw


def _failed(v):
    return [c["label"] for c in v["checks"] if not c["ok"]]


def test_a_result_that_passes_everything_is_worth_paper_trading():
    v = judge(_result(), YEAR)
    assert v["grade"] == "paper" and v["title"] == "Worth paper trading" and _failed(v) == []
    assert "real money" in v["summary"]  # the best a backtest earns is "paper-trade it next"


def test_less_return_than_holding_passes_with_a_smoother_ride():
    v = judge(_result(total_return_pct=9.0, buy_hold_return_pct=25.0, beat_buy_hold=False, sharpe=1.3), YEAR)
    assert v["grade"] == "paper"
    assert "smoother ride" in next(c["detail"] for c in v["checks"] if c["label"] == "Better than just holding")


def test_lagging_holding_on_both_counts_is_not_yet():
    v = judge(_result(total_return_pct=5.0, buy_hold_return_pct=30.0, beat_buy_hold=False, sharpe=0.6), YEAR)
    assert v["grade"] == "weak" and _failed(v) == ["Better than just holding"]


def test_too_few_trades_is_not_recommended_whatever_the_return():
    v = judge(_result(num_trades=4, total_return_pct=20.0, profit_factor=None, win_rate_pct=100.0), YEAR)
    assert v["grade"] == "no" and v["summary"].startswith("Too few trades")
    assert next(c for c in v["checks"] if c["label"] == "Wins outweigh losses")["detail"] == "No losing trade yet"


def test_losing_money_is_not_recommended():
    v = judge(_result(total_return_pct=-6.0, avg_trade_pct=-0.3, profit_factor=0.8, beat_buy_hold=False, sharpe=-0.5), YEAR)
    assert v["grade"] == "no" and "lost money" in v["summary"]


def test_a_tripped_breaker_and_a_short_window_fail_their_checks():
    short = RunConfig(start=date(2026, 7, 1), end=date(2026, 10, 1))
    v = judge(_result(breaker_tripped_on="2026-08-17", max_drawdown_pct=-21.0), short)
    assert v["grade"] == "weak" and _failed(v) == ["Losses stayed bearable", "Long enough test"]


def test_failed_decisions_spoil_a_clean_run():
    assert _failed(judge(_result(decision_errors=3), YEAR)) == ["Clean run"]
