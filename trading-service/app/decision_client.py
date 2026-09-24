"""Calls decision-service for a BUY/SELL/HOLD signal with the bot's own parameters."""

from datetime import date, datetime

import httpx

from .config import settings


class SignalError(Exception):
    """decision-service failed; the bot records a HOLD with this message and trades nothing."""


def get_signal(bot, as_of: date, at: datetime | None = None) -> dict:
    """`at` = the moment of the decision. decision-service uses it as the news cutoff, so a 10:00 decision
    only sees news published before 10:00 and ages headlines from then (default: 15:30 New York)."""
    params = {
        "symbol": bot.symbol,
        "as_of": as_of.isoformat(),
        "mode": bot.mode,
        "account_balance": round(bot.cash + bot.shares * (bot.last_price or 0), 2),
        # Per-bot stop/target so the agent's explanation quotes the same levels the bot will enforce
        "stop_pct": bot.stop_pct,
        "target_pct": bot.target_pct,
    }
    if at is not None:
        params["decided_at"] = at.isoformat()
    try:
        r = httpx.post(f"{settings.decision_service_url}/signal", params=params, timeout=120)  # LLM mode can be slow
    except httpx.HTTPError as e:
        raise SignalError(f"decision-service unreachable: {e}") from e
    if r.status_code != 200:
        raise SignalError(f"decision-service {r.status_code}: {r.text[:300]}")
    return r.json()
