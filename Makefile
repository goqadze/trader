# Convenience commands. Run `make test` to run every test suite.
# Tests run inside the service containers, so no local Python setup is needed.

.PHONY: test test-decision test-backtest

# Run all backend tests (both services).
test: test-decision test-backtest

# decision-service tests (risk sizing, decision rules, indicators).
test-decision:
	docker compose run --rm --no-deps -v "$(CURDIR)/decision-service:/code" -w /code \
		decision-service sh -c "pip install -q -r requirements-dev.txt && pytest"

# backtest-service tests (engine, fees/slippage, risk exits, config, client).
test-backtest:
	docker compose run --rm --no-deps -v "$(CURDIR)/backtest-service:/code" -w /code \
		backtest-service sh -c "pip install -q -r requirements-dev.txt && pytest"
