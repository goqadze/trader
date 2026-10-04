"""Broker registry and the built-in paper simulator."""

from datetime import datetime, timezone

import pytest

from app import brokers
from app.brokers import BrokerError, get_broker
from app.brokers.base import Quote
from app.brokers.paper import PaperBroker
from app.models import Bot


def test_paper_fills_apply_slippage_against_you_and_a_fee():
    b = PaperBroker(slippage_pct=0.01, fee_pct=0.001, price_source=lambda s: Quote(100.0, datetime.now(timezone.utc)))
    buy = b.submit("AAPL", "BUY", 10, "c1")
    sell = b.submit("AAPL", "SELL", 10, "c2")
    assert buy.avg_price == 101.0 and sell.avg_price == 99.0
    assert buy.fee == round(10 * 101 * 0.001, 2)
    assert b.lookup("c1") is buy and b.lookup("missing") is None


def test_live_broker_is_refused_while_the_flag_is_off():
    with pytest.raises(BrokerError, match="live trading is disabled"):
        get_broker(Bot(broker="alpaca-live"))


def test_live_needs_flag_and_keys(monkeypatch):
    monkeypatch.setattr(brokers, "settings", brokers.settings.__class__(allow_live_trading=True))
    with pytest.raises(BrokerError, match="keys"):
        get_broker(Bot(broker="alpaca-live"))
    live = [b for b in brokers.catalog() if b["name"] == "alpaca-live"][0]
    assert live["available"] is False


def test_unknown_broker():
    with pytest.raises(BrokerError):
        get_broker(Bot(broker="robinhood"))


def test_yahoo_quotes_ask_for_share_classes_with_a_dash(monkeypatch):
    """Bots (like Alpaca) write BRK.B; Yahoo only knows BRK-B."""
    import pandas as pd

    from app.brokers import paper

    asked = []

    class Ticker:
        def __init__(self, symbol):
            asked.append(symbol)

        def history(self, **kw):
            return pd.DataFrame({"Close": [500.0]}, index=pd.to_datetime(["2026-09-01 14:00"], utc=True))

    monkeypatch.setattr(paper.yf, "Ticker", Ticker)
    assert paper.yahoo_quote("BRK.B").price == 500.0
    assert asked == ["BRK-B"]
