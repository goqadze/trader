import { RocketOutlined } from "@ant-design/icons";
import { Button } from "antd";
import { useNavigate } from "react-router-dom";
import { strategyName } from "../strategies";
import type { BotFormInitial } from "../trading/BotForm";
import type { RunConfig } from "../types";

/** Backtest config -> trading bot form values. Same parameter names, so nothing is lost in between. */
function toBotParams(cfg: RunConfig): BotFormInitial {
  return {
    symbol: cfg.symbol,
    name: `${cfg.symbol} ${strategyName(cfg.strategy)} (backtest ${cfg.start}..${cfg.end})`.slice(0, 80),
    allocated_cash: cfg.initial_cash,
    strategy: cfg.strategy,
    min_confidence: cfg.min_confidence,
    rebalance_days: cfg.rebalance_days,
    position_pct: cfg.position_pct,
    decide_at: cfg.decide_at ?? "close", // the bot checks when the backtest did
    ...(cfg.stop_pct != null && { stop_pct: cfg.stop_pct }),
    ...(cfg.target_pct != null && { target_pct: cfg.target_pct }),
  };
}

/** Opens the new-bot form on the Live trading page, prefilled with exactly what was backtested. */
export default function TradeThisButton({ config, type = "primary" }: { config: RunConfig; type?: "primary" | "default" }) {
  const navigate = useNavigate();
  return (
    <Button type={type} icon={<RocketOutlined />} onClick={() => navigate("/trading", { state: { prefill: toBotParams(config) } })}>
      Trade this strategy
    </Button>
  );
}
