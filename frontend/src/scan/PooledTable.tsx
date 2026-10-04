import { InfoCircleOutlined } from "@ant-design/icons";
import { Card, Table, Tooltip, Typography } from "antd";
import { strategyShortName } from "../strategies";
import { pooled, type Pool, type PooledRow } from "./pooled";
import type { ScanView } from "./types";

const MUTED = "#8b98b5";
const GOOD = "#33c088";
const BAD = "#ef5b6b";

/** A profit factor in words: about 1 is no edge, whatever the single best symbol did. */
function PF({ v, trades }: { v: number | null; trades: number }) {
  if (v == null) return <span style={{ color: MUTED }}>—</span>;
  const color = v >= 1.2 && trades >= 100 ? GOOD : v < 1 ? BAD : undefined;
  return <span style={{ color, fontWeight: 600 }}>{v === Infinity ? "∞" : v.toFixed(2)}</span>;
}

const pct = (v: number | null, d = 1) => (v == null ? "—" : `${v.toFixed(d)}%`);
const signed = (v: number | null) => (v == null ? "—" : `${v > 0 ? "+" : ""}${v.toFixed(3)}%`);

function cols(period: "practice" | "exam", title: string) {
  const p = (r: PooledRow) => r[period] as Pool;
  return {
    title,
    children: [
      { title: "Trades", key: `${period}-t`, align: "right" as const, render: (_: unknown, r: PooledRow) => p(r).trades.toLocaleString() },
      { title: "Won", key: `${period}-w`, align: "right" as const, render: (_: unknown, r: PooledRow) => pct(p(r).winRate, 0) },
      {
        title: (
          <Tooltip title="$ won on winning trades ÷ $ lost on losing ones, all symbols together. About 1.0 = no edge; look for 1.2+ over 100+ trades.">
            Profit factor <InfoCircleOutlined style={{ fontSize: 11, color: MUTED }} />
          </Tooltip>
        ),
        key: `${period}-pf`,
        align: "right" as const,
        render: (_: unknown, r: PooledRow) => <PF v={p(r).profitFactor} trades={p(r).trades} />,
      },
      { title: "Avg trade", key: `${period}-a`, align: "right" as const, render: (_: unknown, r: PooledRow) => signed(p(r).avgTrade) },
      {
        title: "Made money on",
        key: `${period}-m`,
        align: "right" as const,
        render: (_: unknown, r: PooledRow) => (
          <span>
            {p(r).profitable} of {p(r).symbols}
            {p(r).breakers > 0 && <span style={{ color: MUTED, fontSize: 12 }}> · {p(r).breakers} hit the breaker</span>}
          </span>
        ),
      },
    ],
  };
}

/** Every strategy's trades on all the scan's symbols together: the honest answer to "does this rule have an edge?". */
export default function PooledTable({ scan }: { scan: ScanView }) {
  const rows = pooled(scan)
    .filter((r) => r.practice.symbols > 0)
    .sort((a, b) => (b.practice.profitFactor ?? -1) - (a.practice.profitFactor ?? -1));
  if (!rows.length) return null;
  const exam = rows.some((r) => r.exam);
  const twins = rows[0].twinsSkipped;
  return (
    <Card size="small" title="Pooled across symbols" style={{ marginBottom: 16 }}>
      <Typography.Paragraph style={{ fontSize: 12, color: MUTED, marginBottom: 8 }}>
        One symbol can be lucky; all of a strategy's trades together show whether the rule itself has an edge.
        {twins > 0 && ` Twin ETFs are counted once (${twins} left out per strategy).`}
        {!exam && scan.config.exam && " The exam isn't pooled: only practice's winners sat it (tick “Examine every combination” to pool it too)."}
      </Typography.Paragraph>
      <Table<PooledRow>
        size="small"
        pagination={false}
        rowKey="strategy"
        dataSource={rows}
        scroll={{ x: "max-content" }}
        columns={[
          { title: "Strategy", key: "s", fixed: "left", render: (_, r) => strategyShortName(r.strategy) },
          cols("practice", `Practice ${scan.config.practice.start.slice(0, 4)}–${scan.config.practice.end.slice(0, 4)}`),
          ...(exam && scan.config.exam ? [cols("exam", `Exam ${scan.config.exam.start.slice(0, 4)}–${scan.config.exam.end.slice(0, 4)}`)] : []),
        ]}
      />
    </Card>
  );
}
