import { SearchOutlined } from "@ant-design/icons";
import { Alert, Button, Descriptions, Drawer, Space, Spin, Tooltip, Typography } from "antd";
import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { getRun } from "../api";
import { METRICS } from "../compare/metrics";
import TradeThisButton from "../components/TradeThisButton";
import { VerdictAlert } from "../components/VerdictView";
import { strategyName } from "../strategies";
import type { Result, RunConfig } from "../types";
import type { ScanCell } from "./types";

const MUTED = "#8b98b5";
const SHOWN = ["total_return_pct", "alpha_vs_buy_hold_pct", "max_drawdown_pct", "sharpe", "win_rate_pct", "num_trades",
  "profit_factor", "avg_trade_pct", "avg_hold_days", "exposure_pct"];

type Loaded = { config: RunConfig; result: Result | null; status: string };

/** A run's full detail, fetched again when its status changes (a scan moves on while the drawer is open). */
function useRun(id: string | undefined, status: string | undefined) {
  const [run, setRun] = useState<Loaded>();
  const [error, setError] = useState<string>();
  useEffect(() => {
    setRun(undefined);
    setError(undefined);
    if (id) getRun(id).then(setRun, (e) => setError(e instanceof Error ? e.message : String(e)));
  }, [id, status]);
  return { run, error };
}

function Period({ title, id, note, run, error }: { title: string; id?: string; note?: string | null; run?: Loaded; error?: string }) {
  if (!id) return <Alert type="info" showIcon message={`${title}: ${note ?? "waits for the practice result"}`} style={{ marginBottom: 16 }} />;
  if (error) return <Alert type="error" showIcon message={`${title}: ${error}`} style={{ marginBottom: 16 }} />;
  if (!run) return <Spin style={{ display: "block", margin: 24 }} />;
  const r = run.result;
  return (
    <div style={{ marginBottom: 24 }}>
      <Typography.Title level={5} style={{ marginTop: 0 }}>
        {title} <span style={{ color: MUTED, fontWeight: 400, fontSize: 13 }}>{run.config.start} → {run.config.end}</span>
      </Typography.Title>
      {!r ? (
        <Alert type="info" showIcon message={`Status: ${run.status}`} />
      ) : (
        <>
          {r.verdict && <VerdictAlert verdict={r.verdict} />}
          <Descriptions size="small" column={2} colon={false}>
            {METRICS.filter((m) => SHOWN.includes(m.key)).map((m) => {
              const bh = m.buyHold?.(r);
              return (
                <Descriptions.Item
                  key={m.key}
                  label={
                    <Tooltip title={m.help}>
                      <span style={{ borderBottom: `1px dotted ${MUTED}` }}>{m.title}</span>
                    </Tooltip>
                  }
                >
                  {m.format(m.value(r), r)}
                  {m.buyHold && <span style={{ color: MUTED, marginLeft: 6, fontSize: 12 }}>B&amp;H {m.format(bh, r)}</span>}
                </Descriptions.Item>
              );
            })}
          </Descriptions>
        </>
      )}
    </div>
  );
}

/** One combination opened: its practice and exam checklists and numbers, and what to do next with it. */
export default function CellDetail({ cell, onClose }: { cell?: ScanCell; onClose: () => void }) {
  const navigate = useNavigate();
  const exam = useRun(cell?.exam?.run_id, cell?.exam?.status);
  const practice = useRun(cell?.practice?.run_id, cell?.practice?.status);
  const latest = exam.run?.result ? exam.run : practice.run?.result ? practice.run : undefined; // the newest finished window
  const finalist = cell?.practice?.verdict?.grade === "paper" && cell?.exam?.verdict?.grade === "paper";

  return (
    <Drawer
      open={!!cell}
      onClose={onClose}
      width={720}
      title={cell && `${cell.symbol} · ${strategyName(cell.strategy)}`}
      extra={
        latest && (
          <Space>
            {latest.config.news === false && (
              <Button
                icon={<SearchOutlined />}
                onClick={() => navigate("/", { state: { prefill: { ...latest.config, news: true } } })}
                title="Open the Backtest page with these settings and news on"
              >
                Re-check with news
              </Button>
            )}
            {finalist && <TradeThisButton config={latest.config} />}
          </Space>
        )
      }
    >
      {cell && (
        <>
          {finalist && (
            <Alert
              type="success"
              showIcon
              style={{ marginBottom: 16 }}
              message="Passed practice and the exam"
              description="Next: re-check it with news, see whether the same strategy passes on similar symbols, then paper-trade it before real money."
            />
          )}
          <Period title="Practice" id={cell.practice?.run_id} {...practice} />
          <Period title="Exam" id={cell.exam?.run_id} note={cell.exam_note} {...exam} />
        </>
      )}
    </Drawer>
  );
}
