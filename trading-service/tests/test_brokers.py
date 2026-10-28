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


def _yahoo(monkeypatch, bars: dict):
    """A fake Yahoo answering each period with its bars ({period: [(time, close)]}, missing = empty)."""
    import pandas as pd

    from app.brokers import paper

    asked = []

    class Ticker:
        def __init__(self, symbol):
            pass

        def history(self, period, **kw):
            asked.append(period)
            rows = bars.get(period, [])
            return pd.DataFrame({"Close": [c for _, c in rows]}, index=pd.to_datetime([t for t, _ in rows], utc=True))

    monkeypatch.setattr(paper.yf, "Ticker", Ticker)
    return paper, asked


def test_yahoo_quote_at_the_open_falls_back_to_the_last_bar_of_recent_days(monkeypatch):
    """At 09:30:29 Yahoo had no bar for today yet: that is not an unknown symbol. The last bar keeps its own time,
    so a trade still refuses it as stale."""
    paper, asked = _yahoo(monkeypatch, {"5d": [("2026-10-27 19:58", 68.1), ("2026-10-27 19:59", 68.25)]})
    q = paper.yahoo_quote("XLE")
    assert q.price == 68.25 and q.at == datetime(2026, 10, 27, 19, 59, tzinfo=timezone.utc)
    assert asked == ["1d", "5d"]


def test_yahoo_quote_uses_today_without_a_second_request(monkeypatch):
    paper, asked = _yahoo(monkeypatch, {"1d": [("2026-10-28 13:34", 68.33)], "5d": [("2026-10-27 19:59", 68.25)]})
    assert paper.yahoo_quote("XLE").price == 68.33
    assert asked == ["1d"]


def test_yahoo_quote_with_no_bars_at_all_is_an_error(monkeypatch):
    paper, asked = _yahoo(monkeypatch, {"1d": [("2026-10-28 13:30", float("nan"))]})
    with pytest.raises(BrokerError, match="no price data for NOPE"):
        paper.yahoo_quote("NOPE")
    assert asked == ["1d", "5d"]
