"""Validation tests for RunConfig — the config the frontend sends."""

from datetime import date

import pytest
from pydantic import ValidationError

from app.models import RunConfig


def _cfg(**kw):
    base = dict(start=date(2025, 1, 1), end=date(2025, 2, 1))
    base.update(kw)
    return RunConfig(**base)


def test_sensible_defaults():
    c = _cfg()
    assert c.symbol == "AAPL"
    assert c.initial_cash == 10_000.0
    assert c.fee_pct == 0.0
    assert c.slippage_pct == 0.0005
    assert c.engine == "simple"
    assert c.strategy == "sma_rsi"


def test_unknown_strategy_rejected():
    """A typo must fail at once, not run a whole backtest of decision-service 422s (= all HOLD)."""
    with pytest.raises(ValidationError):
        _cfg(strategy="llm")
    assert _cfg(strategy="breakout").strategy == "breakout"


def test_negative_fee_rejected():
    with pytest.raises(ValidationError):
        _cfg(fee_pct=-0.1)


def test_negative_slippage_rejected():
    with pytest.raises(ValidationError):
        _cfg(slippage_pct=-0.001)


def test_position_pct_must_be_in_0_1():
    with pytest.raises(ValidationError):
        _cfg(position_pct=0)  # gt=0
    with pytest.raises(ValidationError):
        _cfg(position_pct=1.5)  # le=1


def test_min_confidence_must_be_in_0_1():
    with pytest.raises(ValidationError):
        _cfg(min_confidence=-0.1)
    with pytest.raises(ValidationError):
        _cfg(min_confidence=1.5)


def test_rebalance_days_must_be_at_least_1():
    with pytest.raises(ValidationError):
        _cfg(rebalance_days=0)
