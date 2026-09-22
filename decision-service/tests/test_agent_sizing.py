"""Tests for the size_position node in app/agent.py — turns a BUY into a concrete plan."""

from app.agent import size_position


def _pin_env(monkeypatch, stop="0.04", target="0.08", risk="0.02"):
    monkeypatch.setenv("STOP_PCT", stop)
    monkeypatch.setenv("TARGET_PCT", target)
    monkeypatch.setenv("RISK_PCT", risk)


def _buy_state(last_close=50.0, balance=500.0):
    return {"action": "BUY", "indicators": {"last_close": last_close}, "account_balance": balance, "steps": ["prev"]}


def test_hold_is_not_sized():
    assert size_position({"action": "HOLD", "steps": []}) == {}


def test_sell_is_not_sized():
    assert size_position({"action": "SELL", "steps": []}) == {}


def test_buy_produces_full_plan(monkeypatch):
    _pin_env(monkeypatch)
    out = size_position(_buy_state(50.0, 500.0))
    pos = out["position"]
    assert pos["entry"] == 50.0
    assert pos["stop_loss"] == 48.0  # 50 * (1 - 0.04)
    assert pos["target"] == 54.0  # 50 * (1 + 0.08)
    assert pos["shares"] == 5
    assert pos["risk_amount"] == 10.0
    assert pos["reward_amount"] == 20.0
    assert pos["risk_reward_ratio"] == 2.0
    assert any("Sized BUY" in s for s in out["steps"])


def test_zero_shares_when_entry_too_large(monkeypatch):
    _pin_env(monkeypatch)
    out = size_position(_buy_state(1000.0, 100.0))  # entry huge vs tiny account
    assert out["position"]["shares"] == 0
    assert "0 shares" in out["steps"][-1]


def test_env_overrides_change_stop_and_target(monkeypatch):
    _pin_env(monkeypatch, stop="0.10", target="0.20")
    pos = size_position(_buy_state(100.0, 1000.0))["position"]
    assert pos["stop_loss"] == 90.0
    assert pos["target"] == 120.0


def test_default_balance_used_when_missing(monkeypatch):
    _pin_env(monkeypatch)
    state = {"action": "BUY", "indicators": {"last_close": 50.0}, "steps": []}  # no account_balance
    out = size_position(state)
    # default balance 500, risk 2% = $10 budget, rps $2 -> 5 shares
    assert out["position"]["shares"] == 5
