"""Broker registry: which brokers exist, which are usable with the current keys, and building one for a bot."""

from ..config import settings
from .alpaca import AlpacaBroker
from .base import Broker, BrokerError, BrokerOrder, Quote
from .paper import PaperBroker

__all__ = ["Broker", "BrokerError", "BrokerOrder", "Quote", "catalog", "get_broker", "SHARED_ACCOUNT_BROKERS"]

# Brokers backed by ONE real account. Two bots on the same symbol there would buy/sell the same
# shares and corrupt each other's position, so the API allows one bot per symbol on these.
SHARED_ACCOUNT_BROKERS = {"alpaca-paper", "alpaca-live"}


def catalog() -> list[dict]:
    """Every broker with whether it can be used right now and why not (shown in the New bot form)."""
    paper_keys = bool(settings.alpaca_paper_key_id and settings.alpaca_paper_secret_key)
    live_keys = bool(settings.alpaca_live_key_id and settings.alpaca_live_secret_key)
    return [
        {"name": "paper", "label": "Paper (built-in simulator)", "live": False, "available": True, "reason": ""},
        {"name": "alpaca-paper", "label": "Alpaca paper account", "live": False, "available": paper_keys,
         "reason": "" if paper_keys else "set ALPACA_PAPER_KEY_ID / ALPACA_PAPER_SECRET_KEY in trading-service/.env"},
        {"name": "alpaca-live", "label": "Alpaca LIVE (real money)", "live": True,
         "available": settings.allow_live_trading and live_keys,
         "reason": "" if settings.allow_live_trading and live_keys
         else "disabled: set ALLOW_LIVE_TRADING=true and the ALPACA_LIVE_* keys in trading-service/.env"},
    ]


def get_broker(bot) -> Broker:
    """Build the broker a bot trades through. Raises BrokerError if it isn't configured/allowed."""
    if bot.broker == "paper":
        return PaperBroker(slippage_pct=bot.slippage_pct, fee_pct=bot.fee_pct)
    if bot.broker == "alpaca-paper":
        if not (settings.alpaca_paper_key_id and settings.alpaca_paper_secret_key):
            raise BrokerError("Alpaca paper keys are not configured")
        return AlpacaBroker(settings.alpaca_paper_key_id, settings.alpaca_paper_secret_key, live=False)
    if bot.broker == "alpaca-live":
        # Checked on EVERY use, not just at bot creation: flipping the flag off stops live orders at once
        if not settings.allow_live_trading:
            raise BrokerError("live trading is disabled (ALLOW_LIVE_TRADING=false)")
        if not (settings.alpaca_live_key_id and settings.alpaca_live_secret_key):
            raise BrokerError("Alpaca live keys are not configured")
        return AlpacaBroker(settings.alpaca_live_key_id, settings.alpaca_live_secret_key, live=True)
    raise BrokerError(f"unknown broker '{bot.broker}'")
