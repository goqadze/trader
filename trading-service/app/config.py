"""All runtime settings, read once from the environment (see .env.example for what each one means)."""

import os
from dataclasses import dataclass


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    decision_service_url: str = os.getenv("DECISION_SERVICE_URL", "http://decision-service:8000")
    database_url: str = os.getenv("DATABASE_URL", "postgresql+psycopg://trading:trading@trading-db:5432/trading")

    # Scheduler timing
    scheduler_enabled: bool = _bool("SCHEDULER_ENABLED", True)
    tick_seconds: int = int(os.getenv("TICK_SECONDS", "30"))  # how often the scheduler wakes up
    decision_minutes_before_close: int = int(os.getenv("DECISION_MINUTES_BEFORE_CLOSE", "30"))
    # For bots that also (or only) decide in the morning: N minutes after the open (30 = 10:00 New York)
    decision_minutes_after_open: int = int(os.getenv("DECISION_MINUTES_AFTER_OPEN", "30"))
    risk_check_minutes: int = int(os.getenv("RISK_CHECK_MINUTES", "5"))
    # Refuse to trade on a quote older than this during market hours (a frozen feed is worse than no trade)
    max_quote_age_minutes: int = int(os.getenv("MAX_QUOTE_AGE_MINUTES", "20"))

    # Brokers. Live trading needs BOTH the flag and the live keys: two deliberate steps, never a default.
    alpaca_paper_key_id: str = os.getenv("ALPACA_PAPER_KEY_ID", "")
    alpaca_paper_secret_key: str = os.getenv("ALPACA_PAPER_SECRET_KEY", "")
    allow_live_trading: bool = _bool("ALLOW_LIVE_TRADING", False)
    alpaca_live_key_id: str = os.getenv("ALPACA_LIVE_KEY_ID", "")
    alpaca_live_secret_key: str = os.getenv("ALPACA_LIVE_SECRET_KEY", "")


settings = Settings()
