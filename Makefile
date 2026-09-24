# Convenience commands. Run `make test` to run every test suite.
# Tests run inside the service containers, so no local Python setup is needed.

.PHONY: test test-decision test-backtest test-trading backup-trading-db

# Run all backend tests (both services).
test: test-decision test-backtest test-trading

# decision-service tests (risk sizing, decision rules, indicators).
test-decision:
	docker compose run --rm --no-deps -v "$(CURDIR)/decision-service:/code" -w /code \
		decision-service sh -c "pip install -q -r requirements-dev.txt && pytest"

# backtest-service tests (engine, fees/slippage, risk exits, config, client).
test-backtest:
	docker compose run --rm --no-deps -v "$(CURDIR)/backtest-service:/code" -w /code \
		backtest-service sh -c "pip install -q -r requirements-dev.txt && pytest"

# trading-service tests (trader rules, scheduler timing, API guard rails, broker adapters).
# Runs against Postgres -- the same database engine production uses -- in a separate
# `trading_test` database, so your real trading history is never touched.
test-trading:
	docker compose up -d --wait trading-db
	docker compose run --rm --no-deps -v "$(CURDIR)/trading-service:/code" -w /code \
		-e TEST_DATABASE_URL=postgresql+psycopg://trading:trading@trading-db:5432/trading_test \
		trading-service sh -c "pip install -q -r requirements-dev.txt && pytest"

# Snapshot the trading history (bots, decisions, orders, equity, audit log) to backups/.
# Restore: docker compose exec -T trading-db psql -U trading trading < backups/<file>.sql
backup-trading-db:
	mkdir -p backups
	docker compose exec -T trading-db pg_dump -U trading --clean --if-exists trading > backups/trading-$$(date +%Y%m%d-%H%M%S).sql
	@ls -lh backups | tail -1
