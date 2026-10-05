import { Alert, Card, Col, Row, Tag } from "antd";
import ActivityLog from "../components/ActivityLog";
import ConfigForm from "../components/ConfigForm";
import EquityChart from "../components/EquityChart";
import StatTiles from "../components/StatTiles";
import TradeThisButton from "../components/TradeThisButton";
import { VerdictAlert } from "../components/VerdictView";
import { useLocation } from "react-router-dom";
import { useBacktest } from "../hooks/useBacktest";
import type { Result, RunConfig, RunStatus } from "../types";

const STATUS_COLOR: Record<RunStatus, string> = {
  idle: "default",
  starting: "processing",
  running: "processing",
  done: "success",
  error: "error",
};

const usd0 = (v: number) => v.toLocaleString("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 });

const MICRO: Record<string, string> = { NQ: "MNQ", ES: "MES", YM: "MYM" }; // each a tenth of its E-mini

/** Intraday strategies: the setups that weren't traded, and why. Too big for the account is easy to miss in the log
 *  (a whole year of setups skipped looks like a strategy that never found anything), so it gets a warning with sizes:
 *  what one contract or share ties up and risks, against what the account and one trade allow. */
function SkippedSetups({ result: r, config }: { result: Result; config: RunConfig }) {
  const small = r.too_small;
  const unfilled = r.skips?.unfilled ?? 0;
  if (!small && !unfilled) return null;
  const future = r.contract;
  const name = future ? `${future.root} contract` : "share";
  const riskPct = ((config.risk_pct ?? 0.01) * 100).toFixed(1);
  const parts = small ? [
    ...(small.by_margin ? [`ties up about ${usd0(small.margin_per_unit)} of ${future ? "margin" : "cash"}, more than the ${usd0(config.initial_cash)} account`] : []),
    ...(small.risk_per_unit > small.allowed_risk ? [`risks about ${usd0(small.risk_per_unit)} to the stop, more than the ${usd0(small.allowed_risk)} (${riskPct}%) one trade may lose`] : []),
  ] : [];
  const micro = future && MICRO[future.root];
  const microRisk = small ? small.risk_per_unit / 10 : 0;
  return (
    <>
      {small && (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 16 }}
          message={`${small.count} of ${r.setups ?? small.count} setups weren't traded: too big for the account`}
          description={
            <>
              {`In a typical setup one ${name} ${parts.join(", and ")}. `}
              {micro
                ? microRisk > small.allowed_risk
                  ? `Even ${micro} (a tenth of the size) would risk about ${usd0(microRisk)}: it needs a bigger account, a higher risk per trade (bigger losses), or the limit entry, closer to the stop.`
                  : `${micro} (a tenth of the size) would risk about ${usd0(microRisk)}: that fits.`
                : "It needs a bigger account, a higher risk per trade (bigger losses), or a closer stop (the limit entry)."}
            </>
          }
        />
      )}
      {unfilled > 0 && (
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 16 }}
          message={`${unfilled} limit order${unfilled === 1 ? "" : "s"} never filled: the price didn't come back to the entry in time`}
          description={config.entry !== "market" && (config.strategy === "ict_sweep_fvg" || config.strategy === "ict_amd")
            ? "Entry “At market as soon as the setup completes” trades every setup, at a worse price."
            : undefined}
        />
      )}
    </>
  );
}

/** Configure a backtest on the left, watch it run live on the right. */
export default function BacktestPage() {
  const { state, start } = useBacktest();
  const running = state.status === "starting" || state.status === "running";
  const r = state.result;
  const prefill = (useLocation().state as { prefill?: RunConfig } | null)?.prefill; // e.g. "Re-check with news" from a scan

  return (
    <>
      {state.error && <Alert type="error" message={state.error} showIcon style={{ marginBottom: 16 }} />}
      {r?.verdict && state.config && (
        <VerdictAlert
          verdict={r.verdict}
          extra={
            <span style={{ fontWeight: 400 }}>
              {" "}
              · return {r.total_return_pct}% vs buy &amp; hold {r.buy_hold_return_pct}% · max drawdown {r.max_drawdown_pct}% ·
              win rate {r.win_rate_pct}% over {r.num_trades} trades
            </span>
          }
          action={<TradeThisButton config={state.config} />}
        />
      )}
      {r?.contract && (
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 16 }}
          message={`${r.contract.root} (${r.contract.name}), priced from ${r.contract.etf}'s 5-minute bars`}
          description={`$${r.contract.multiplier} a point, whole contracts, $${r.contract.fee_per_side.toFixed(2)} per contract per fill and a
            ${r.contract.tick}-point tick of slippage on market fills; at most one contract per ${r.contract.margin_pct * 100}% of its
            value in margin. ${r.contract.etf} moves like the future during New York hours, but there is no overnight session here.`}
        />
      )}
      {r?.crypto && (
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 16 }}
          message={`${state.config?.symbol ?? "Crypto"}: daily prices, 7 days a week`}
          description={`Decided on the daily close (00:00 UTC), bought in fractions of a coin, with a ${+(r.crypto.fee_pct * 100).toFixed(3)}% fee on
            every buy and sell (Alpaca's crypto fee for a market order) plus the slippage. Long only: Alpaca can't short crypto.`}
        />
      )}
      {r && state.config && <SkippedSetups result={r} config={state.config} />}
      <Row gutter={[16, 16]}>
        <Col xs={24} md={7} lg={6}>
          <ConfigForm onRun={start} running={running} prefill={prefill} />
        </Col>
        <Col xs={24} md={17} lg={18}>
          {!r && state.status !== "idle" && (
            <Card size="small" style={{ marginBottom: 16 }}>
              <Tag color={STATUS_COLOR[state.status]}>{state.status}</Tag> {state.symbol}
            </Card>
          )}
          <StatTiles state={state} />
          <EquityChart data={state.chart} />
          <ActivityLog rows={state.log} />
        </Col>
      </Row>
    </>
  );
}
