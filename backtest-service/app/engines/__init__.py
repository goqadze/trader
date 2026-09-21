from .base import BacktestEngine
from .simple import SimplePortfolioEngine

# Registry of available engines. The frontend picks one by name via RunConfig.engine.
ENGINES: dict[str, type[BacktestEngine]] = {
    "simple": SimplePortfolioEngine,
}

# The Nautilus engine is optional because it pulls in the heavy nautilus_trader library.
# We import it lazily so the service still runs without that dependency installed.
try:
    from .nautilus import NautilusEngine  # noqa: F401

    ENGINES["nautilus"] = NautilusEngine
except Exception:
    pass
