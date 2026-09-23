"""Calls decision-service for a BUY/SELL/HOLD signal with the bot's own parameters."""

from datetime import date

import httpx

from .config import settings


class SignalError(Exception):
    """decision-service failed; the bot records a HOLD with this message and trades nothing."""


def get_signal(bot, as_of: date) -> dict:
    params = {
        "symbol": bot.symbol,
        "as_of": as_of.isoformat(),
        "mode": bot.mode,
        "account_balance": round(bot.cash + bot.shares * (bot.last_price or 0), 2),
        # Per-bot stop/target so the agent's explanation quotes the same levels the bot will enforce
        "stop_pct": bot.stop_pct,
        "target_pct": bot.target_pct,
    }
    try:
        r = httpx.post(f"{settings.decision_service_url}/signal", params=params, timeout=120)  # LLM mode can be slow
    except httpx.HTTPError as e:
        raise SignalError(f"decision-service unreachable: {e}") from e
    if r.status_code != 200:
        raise SignalError(f"decision-service {r.status_code}: {r.text[:300]}")
    return r.json()
