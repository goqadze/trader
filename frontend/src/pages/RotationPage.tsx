import { RocketOutlined } from "@ant-design/icons";
import { Alert, Button, Card, Col, Empty, Row, Space } from "antd";
import { useNavigate } from "react-router-dom";
import RotationForm from "../rotation/RotationForm";
import RotationResult from "../rotation/RotationResult";
import { useRotation } from "../rotation/useRotation";
import { isListedEtf } from "../symbols";
import type { RotationBotInitial } from "../trading/RotationBotForm";

const MUTED = "#8b98b5";

/** Momentum rotation across a universe, on a practice window and an unseen exam window. */
export default function RotationPage() {
  const { runs, error, starting, start } = useRotation();
  const navigate = useNavigate();
  const cfg = runs.exam?.config ?? runs.practice?.config;
  const universe = cfg?.symbols ?? [];
  const stocks = universe.filter((s) => !isListedEtf(s));
  const done = [runs.practice, runs.exam].some((r) => r?.result);
  /** Opens the new rotation bot form on the Live trading page, with exactly the rules that were tested. */
  const paperTrade = () => {
    if (!cfg) return;
    const prefill: RotationBotInitial = {
      universe: cfg.symbols, top_n: cfg.top_n, lookback_months: cfg.lookback_months, skip_months: cfg.skip_months, abs_filter: cfg.abs_filter,
      ...(cfg.slippage_pct != null && { slippage_pct: cfg.slippage_pct }),
      ...(cfg.initial_cash != null && { allocated_cash: cfg.initial_cash }),
      ...(cfg.fractional != null && { fractional: cfg.fractional }),
    };
    navigate("/trading", { state: { rotationPrefill: prefill } });
  };
  return (
    <Row gutter={[16, 16]}>
      <Col xs={24} md={7} lg={6}>
        <RotationForm onRun={start} starting={starting} />
      </Col>
      <Col xs={24} md={17} lg={18}>
        {error && <Alert type="error" showIcon closable message={error} style={{ marginBottom: 16 }} />}
        {stocks.length > 0 && (
          <Alert
            type="warning"
            showIcon
            style={{ marginBottom: 16 }}
            message="Hindsight warning: this universe has individual stocks"
            description={`${stocks.slice(0, 8).join(", ")}${stocks.length > 8 ? "…" : ""} are companies picked today (today's biggest, or an article's picks):
              testing them on past years selects yesterday's winners in hindsight (survivorship bias), which flatters both the rotation and
              holding them. Sector ETFs don't have that problem.`}
          />
        )}
        {runs.practice || runs.exam ? (
          <>
            {done && (
              <Space wrap style={{ marginBottom: 16 }}>
                <Button type="primary" icon={<RocketOutlined />} onClick={paperTrade}>Paper trade this rotation</Button>
                <span style={{ color: MUTED, fontSize: 12 }}>
                  A bot with these rules on a paper account: it rebalances at each month's last close, so give it months, not days.
                </span>
              </Space>
            )}
            <RotationResult title="Practice" run={runs.practice} />
            <RotationResult title="Exam" run={runs.exam} />
          </>
        ) : (
          <Card>
            <Empty
              description={
                <span style={{ color: MUTED }}>
                  Momentum is one of the best-documented effects in markets: what rose over the past year tends to keep rising for a
                  while. Test it on the 11 sector ETFs first: the practice years, then the exam years it never saw.
                </span>
              }
            />
          </Card>
        )}
      </Col>
    </Row>
  );
}
