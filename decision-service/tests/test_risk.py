"""Tests for the position-sizing math in app/risk.py — the core of risk management."""

import pytest

from app.risk import PositionPlan, position_size


def test_canonical_example():
    """The worked example from the docs: $500 account, 2% risk, buy $50, stop $48, target $54."""
    plan = position_size(account_balance=500, entry=50, stop_loss=48, target=54, risk_pct=0.02)
    assert isinstance(plan, PositionPlan)
    assert plan.shares == 5  # $10 risk budget / $2 risk-per-share
    assert plan.risk_amount == 10.0  # 5 shares * $2
    assert plan.reward_amount == 20.0  # 5 shares * ($54 - $50)
    assert plan.risk_reward_ratio == 2.0  # $20 reward / $10 risk


def test_shares_round_down_never_exceed_budget():
    """Shares must round DOWN so actual risk never exceeds the budget."""
    # budget = 100 * 0.02 = $2, risk-per-share = $3 -> 0.66 shares -> floors to 0
    plan = position_size(100, entry=50, stop_loss=47, risk_pct=0.02)
    assert plan.shares == 0
    assert plan.risk_amount == 0.0
    # A budget of exactly 2 shares' worth gives exactly 2, not 3
    plan2 = position_size(100, entry=50, stop_loss=49, risk_pct=0.02)  # budget $2 / $1 = 2
    assert plan2.shares == 2
    assert plan2.risk_amount == 2.0


def test_zero_shares_when_entry_too_expensive():
    """A pricey stock relative to the account yields 0 shares, not a negative or a crash."""
    plan = position_size(100, entry=1000, stop_loss=960, risk_pct=0.02)  # rps $40, budget $2
    assert plan.shares == 0
    assert plan.reward_amount == 0.0
    assert plan.risk_reward_ratio == 0.0  # no risk taken -> ratio defined as 0


def test_no_target_means_no_reward_or_ratio():
    plan = position_size(500, entry=50, stop_loss=48)  # target defaults to None
    assert plan.shares == 5
    assert plan.reward_amount == 0.0
    assert plan.risk_reward_ratio == 0.0


def test_stop_at_or_above_entry_raises():
    """A long trade needs the stop BELOW entry, else risk-per-share is <= 0."""
    with pytest.raises(ValueError):
        position_size(500, entry=50, stop_loss=50)  # equal
    with pytest.raises(ValueError):
        position_size(500, entry=50, stop_loss=52)  # above


def test_risk_reward_ratio_three_to_one():
    plan = position_size(1000, entry=100, stop_loss=98, target=106, risk_pct=0.02)
    # budget $20 / rps $2 = 10 shares; reward 10*$6=$60; risk 10*$2=$20 -> 3.0
    assert plan.shares == 10
    assert plan.reward_amount == 60.0
    assert plan.risk_reward_ratio == 3.0


def test_larger_risk_pct_buys_more():
    small = position_size(1000, 100, 98, risk_pct=0.01).shares
    big = position_size(1000, 100, 98, risk_pct=0.05).shares
    assert big > small
