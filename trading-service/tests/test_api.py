"""HTTP API: validation, lifecycle controls, guard rails, and history endpoints."""

from datetime import date

import pytest
from conftest import FakeBroker, at
from fastapi.testclient import TestClient

from app import main
from app.brokers import catalog as real_catalog

CLOSED = at(23, 0)  # Monday 19:00 New York
OPEN = at(15, 0)  # Monday 11:00 New York


@pytest.fixture
def clock(monkeypatch):
    """Freeze the API's clock; returns a setter."""
    now = {"t": CLOSED}
    monkeypatch.setattr(main, "utcnow", lambda: now["t"])
    return now


@pytest.fixture
def broker(monkeypatch, clock):
    b = FakeBroker(price=100.0, now=CLOSED)
    monkeypatch.setattr(main, "get_broker", lambda bot: b)
    return b


@pytest.fixture
def client(broker):
    with TestClient(main.app) as c:
        yield c


def _create(client, **kw):
    body = {"symbol": "aapl", "allocated_cash": 5000, "min_confidence": 0.65, "rebalance_days": 3, **kw}
    r = client.post("/bots", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def test_create_bot_uses_the_quote_as_buy_and_hold_baseline(client):
    bot = _create(client)
    assert bot["symbol"] == "AAPL" and bot["name"] == "AAPL sma_rsi"  # the original simple strategy is the default
    assert bot["status"] == "active" and bot["broker"] == "paper" and bot["live"] is False
    assert bot["cash"] == 5000 and bot["equity"] == 5000 and bot["return_pct"] == 0
    assert bot["benchmark_price"] == 100.0
    events = client.get(f"/bots/{bot['id']}/events").json()
    assert events[0]["kind"] == "created"


@pytest.mark.parametrize("bad", [
    {"stop_pct": 0.9},  # 90% stop-loss is almost certainly a typo
    {"position_pct": 0},
    {"min_confidence": 1.5},
    {"symbol": "not a symbol"},
    {"allocated_cash": 10},
    {"strategy": "llm"},  # removed: the LLM no longer decides trades
    {"strategy": "martingale"},
])
def test_invalid_parameters_are_rejected(client, bad):
    r = client.post("/bots", json={"symbol": "AAPL", **bad})
    assert r.status_code == 422


def test_crypto_is_for_backtests_only(client):
    r = client.post("/bots", json={"symbol": "BTC-USD"})
    assert r.status_code == 422 and "can't trade crypto yet" in r.text


def test_unavailable_broker_is_rejected_with_the_reason(client):
    r = client.post("/bots", json={"symbol": "AAPL", "broker": "alpaca-paper"})
    assert r.status_code == 422 and "ALPACA_PAPER_KEY_ID" in r.text


def test_real_money_bot_needs_explicit_confirmation(client, monkeypatch):
    monkeypatch.setattr(main, "catalog", lambda: [
        {**b, "available": True} if b["name"] == "alpaca-live" else b for b in real_catalog()
    ])
    r = client.post("/bots", json={"symbol": "AAPL", "broker": "alpaca-live"})
    assert r.status_code == 422 and "confirm_live" in r.text
    r = client.post("/bots", json={"symbol": "AAPL", "broker": "alpaca-live", "confirm_live": True})
    assert r.status_code == 201 and r.json()["live"] is True


def test_one_bot_per_symbol_on_a_real_account_but_many_on_the_simulator(client, monkeypatch):
    monkeypatch.setattr(main, "catalog", lambda: [{**b, "available": True} for b in real_catalog()])
    _create(client, broker="alpaca-paper")
    assert client.post("/bots", json={"symbol": "AAPL", "broker": "alpaca-paper"}).status_code == 409
    _create(client, broker="paper", name="AAPL aggressive", min_confidence=0.5)
    _create(client, broker="paper", name="AAPL careful", min_confidence=0.8)
    assert len(client.get("/bots").json()) == 3


def test_pause_resume_and_status_guards(client):
    bot = _create(client)
    assert client.post(f"/bots/{bot['id']}/pause").json()["status"] == "paused"
    assert client.post(f"/bots/{bot['id']}/pause").status_code == 409  # already paused
    assert client.post(f"/bots/{bot['id']}/resume").json()["status"] == "active"


def test_halt_pauses_every_active_bot(client):
    _create(client)
    _create(client, symbol="MSFT")
    assert client.post("/halt").json() == {"paused": 2}
    assert {x["status"] for x in client.get("/bots").json()} == {"paused"}
    assert client.get("/events").json()[0]["kind"] == "halt"


def test_events_of_one_broker_s_bots(client, monkeypatch):
    monkeypatch.setattr(main, "catalog", lambda: [{**b, "available": True} for b in real_catalog()])
    sim = _create(client, broker="paper")
    alpaca = _create(client, broker="alpaca-paper", symbol="MSFT")
    client.post("/halt")  # a system event, no bot's
    assert {e["bot_id"] for e in client.get("/events?broker=alpaca-paper").json()} == {alpaca["id"]}
    assert {e["bot_id"] for e in client.get("/events?broker=paper").json()} == {sim["id"]}
    assert client.get("/events?broker=alpaca-live").json() == []
    assert None in {e["bot_id"] for e in client.get("/events").json()}


def test_param_changes_are_audited(client):
    bot = _create(client)
    r = client.patch(f"/bots/{bot['id']}", json={"min_confidence": 0.75, "stop_pct": 0.05})
    assert r.json()["min_confidence"] == 0.75
    msg = client.get(f"/bots/{bot['id']}/events").json()[0]["message"]
    assert "min_confidence 0.65 → 0.75" in msg and "stop_pct 0.04 → 0.05" in msg


def test_switching_strategy_is_audited_and_reaches_decision_service(client, monkeypatch):
    bot = _create(client, strategy="breakout")
    assert bot["strategy"] == "breakout"
    r = client.patch(f"/bots/{bot['id']}", json={"strategy": "mean_reversion"})
    assert r.json()["strategy"] == "mean_reversion"
    assert "strategy breakout → mean_reversion" in client.get(f"/bots/{bot['id']}/events").json()[0]["message"]

    from app import decision_client
    from app.db import SessionLocal
    sent = {}

    def fake_post(url, params, timeout):
        sent.update(params)
        return type("R", (), {"status_code": 200, "json": lambda self: {"action": "HOLD"}})()

    monkeypatch.setattr(decision_client.httpx, "post", fake_post)
    with SessionLocal() as s:
        decision_client.get_signal(s.get(main.Bot, bot["id"]), date(2025, 6, 2))
    assert sent["strategy"] == "mean_reversion" and "mode" not in sent


def test_run_now_while_closed_is_a_preview(client, monkeypatch):
    monkeypatch.setattr(main, "evaluate", _evaluate_with(lambda bot, d, at=None: {"action": "BUY", "confidence": 0.9}))
    bot = _create(client)
    d = client.post(f"/bots/{bot['id']}/run").json()
    assert d["kind"] == "preview" and d["action"] == "BUY"
    assert client.get(f"/bots/{bot['id']}/orders").json() == []


def test_run_now_while_open_trades_and_shows_in_history(client, clock, broker, monkeypatch):
    monkeypatch.setattr(main, "evaluate", _evaluate_with(lambda bot, d, at=None: {"action": "BUY", "confidence": 0.9}))
    bot = _create(client, slippage_pct=0)  # no sizing buffer, so $5000 at $100 is exactly 50 shares
    clock["t"] = broker.now = OPEN
    d = client.post(f"/bots/{bot['id']}/run").json()
    assert d["kind"] == "manual" and d["outcome"].startswith("BUY 50 sh filled")
    detail = client.get(f"/bots/{bot['id']}").json()
    assert detail["shares"] == 50 and detail["stop_price"] == 96.0
    orders = client.get(f"/bots/{bot['id']}/orders").json()
    assert orders[0]["side"] == "BUY" and orders[0]["decision_id"] == d["id"]
    assert client.get(f"/bots/{bot['id']}/equity").json()[0]["equity"] == 5000

    # Can't archive while holding; can close manually, then archive
    assert client.post(f"/bots/{bot['id']}/archive").status_code == 409
    sell = client.post(f"/bots/{bot['id']}/close").json()
    assert sell["reason"] == "manual" and sell["status"] == "filled"
    assert client.post(f"/bots/{bot['id']}/archive").json()["status"] == "archived"
    assert client.get("/bots").json() == []  # hidden by default...
    assert len(client.get("/bots?include_archived=true").json()) == 1  # ...but never deleted


def test_broker_held_stop_shows_in_the_api_and_close_cancels_it_first(client, clock, broker, monkeypatch):
    monkeypatch.setattr(main, "evaluate", _evaluate_with(lambda bot, d, at=None: {"action": "BUY", "confidence": 0.9}))
    broker.supports_stop_orders, broker.held = True, 0  # behave like Alpaca
    bot = _create(client, slippage_pct=0)
    clock["t"] = broker.now = OPEN
    client.post(f"/bots/{bot['id']}/run")

    detail = client.get(f"/bots/{bot['id']}").json()
    assert detail["stop_at_broker"] is True and detail["pending_order"] is False
    stop = client.get(f"/bots/{bot['id']}/orders").json()[0]  # newest first
    assert (stop["order_type"], stop["stop_price"], stop["status"]) == ("stop", 96.0, "submitted")
    assert client.get("/summary").json()["pending_orders"] == 0  # a resting stop isn't "pending"

    broker.cancel_mode = "pending"  # cancel not confirmed: refuse, don't risk selling twice
    r = client.post(f"/bots/{bot['id']}/close")
    assert r.status_code == 409 and "stop-loss" in r.json()["detail"]
    assert broker.submitted == [("BUY", 50)]

    broker.cancel_mode = "cancel"
    assert client.post(f"/bots/{bot['id']}/close").json()["status"] == "filled"
    assert client.get(f"/bots/{bot['id']}").json()["stop_at_broker"] is False
    assert client.post(f"/bots/{bot['id']}/archive").json()["status"] == "archived"


def test_decide_at_is_saved_audited_and_drives_the_next_decision_time(client, clock):
    clock["t"] = at(12, 0)  # Monday 08:00 New York
    bot = _create(client, decide_at="both", rebalance_days=1)
    assert bot["decide_at"] == "both" and bot["next_decision_at"] == "2025-06-02T14:00:00Z"  # 10:00 New York
    assert client.get("/status").json()["next_decision_at"] == "2025-06-02T14:00:00Z"

    client.patch(f"/bots/{bot['id']}", json={"decide_at": "close"})
    assert client.get(f"/bots/{bot['id']}").json()["next_decision_at"] == "2025-06-02T19:30:00Z"  # 15:30
    assert "decide_at both → close" in client.get(f"/bots/{bot['id']}/events").json()[0]["message"]
    assert client.post("/bots", json={"symbol": "AAPL", "decide_at": "noon"}).status_code == 422


def test_close_position_refused_when_market_closed(client):
    bot = _create(client)
    r = client.post(f"/bots/{bot['id']}/close")
    assert r.status_code == 409


def test_status_reports_market_clock_and_brokers(client):
    s = client.get("/status").json()
    assert s["market_open"] is False
    assert s["next_decision_at"].startswith("2025-06-03T19:30")  # Tuesday 15:30 New York
    assert {b["name"] for b in s["brokers"]} == {"paper", "alpaca-paper", "alpaca-live"}
    assert s["live_trading_allowed"] is False


def test_health_says_when_the_scheduler_is_stuck_or_stopped(client, clock, monkeypatch):
    assert client.get("/health").json() == {"status": "ok", "problems": [], "last_tick": None}  # scheduler off in tests
    monkeypatch.setattr(main, "settings", main.settings.__class__(scheduler_enabled=True, tick_seconds=30))
    state = {"running": True, "started_at": at(22, 50), "last_tick": at(22, 58), "last_error": None}
    monkeypatch.setattr(main.scheduler, "state", state)
    assert client.get("/health").status_code == 200  # 2 minutes ago: fine
    state["last_tick"], state["last_error"] = at(22, 40), "OperationalError: connection refused"
    r = client.get("/health")
    assert r.status_code == 503 and r.json()["status"] == "down"
    assert r.json()["problems"] == ["the scheduler hasn't finished a round for 20 minutes: the bots don't check or trade "
                                    "(last error: OperationalError: connection refused)"]
    state["running"] = False
    assert client.get("/health").json()["problems"] == ["the scheduler isn't running: the bots don't check or trade"]


def test_unknown_bot_is_404(client):
    assert client.get("/bots/999").status_code == 404
    assert client.get("/bots/999/decisions").status_code == 404


def _evaluate_with(signal_fn):
    """evaluate() with a canned signal instead of calling decision-service."""
    from app.trader import evaluate

    return lambda session, bot, broker, now, kind: evaluate(session, bot, broker, now, kind, signal_fn)
