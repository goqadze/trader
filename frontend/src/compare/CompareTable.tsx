import { InfoCircleOutlined, WarningOutlined } from "@ant-design/icons";
import { Progress, Table, Tag, Tooltip } from "antd";
import type { ColumnsType } from "antd/es/table";
import { strategyShortName, type StrategyId } from "../strategies";
import type { BacktestState, Result } from "../types";
import { METRICS, bestValues } from "./metrics";

const MUTED = "#8b98b5";
const BEST = "#33c088";

interface Row {
  key: StrategyId;
  run: BacktestState;
}

/** Sort key for a metric: runs without a value (still running, no trades) always sink to the bottom. */
const sortValue = (v: number | null | undefined, order: "ascend" | "descend" | null | undefined) =>
  v == null || Number.isNaN(v) ? (order === "ascend" ? Infinity : -Infinity) : v;

function Status({ run }: { run: BacktestState }) {
  if (run.status === "error") {
    return (
      <Tooltip title={run.error}>
        <Tag color="error">error</Tag>
      </Tooltip>
    );
  }
  if (run.status === "done") return <Tag color="success">done</Tag>;
  if (run.status === "running" && run.total > 0) return <Progress percent={Math.round((run.i / run.total) * 100)} size="small" style={{ width: 90, margin: 0 }} />;
  return <Tag>queued</Tag>;
}

/**
 * One row per strategy with every headline measurement, best value per column in green, sortable by any
 * column, and a fixed buy & hold row on top as the bar to clear. Clicking a row opens that strategy below.
 */
export default function CompareTable({
  order,
  runs,
  focus,
  onFocus,
}: {
  order: StrategyId[];
  runs: Partial<Record<StrategyId, BacktestState>>;
  focus: StrategyId | undefined;
  onFocus: (id: StrategyId) => void;
}) {
  const rows: Row[] = order.map((id) => ({ key: id, run: runs[id]! })).filter((r) => r.run);
  const results = rows.map((r) => r.run.result).filter((r): r is Result => !!r);
  const best = bestValues(results);
  const benchmark = results[0]; // every run shares the symbol and window, so any finished one has the buy & hold figures
  const metrics = METRICS.filter((m) => m.table);

  const columns: ColumnsType<Row> = [
    {
      title: "Strategy",
      key: "strategy",
      fixed: "left",
      width: 190,
      render: (_, { key, run }) => (
        <span style={{ fontWeight: key === focus ? 600 : undefined }}>
          {strategyShortName(key)}
          {!!run.result?.decision_errors && (
            <Tooltip title={`${run.result.decision_errors} decision(s) failed and counted as holds: treat this result with care`}>
              <WarningOutlined style={{ color: "#faad14", marginLeft: 6 }} />
            </Tooltip>
          )}
        </span>
      ),
    },
    { title: "Status", key: "status", width: 110, render: (_, { run }) => <Status run={run} /> },
    ...metrics.map(
      (m): ColumnsType<Row>[number] => ({
        key: m.key,
        align: "right",
        width: m.title.length > 10 ? 120 : 100,
        title: (
          <Tooltip title={m.help}>
            {m.title} <InfoCircleOutlined style={{ fontSize: 11, color: MUTED }} />
          </Tooltip>
        ),
        defaultSortOrder: m.key === "total_return_pct" ? "descend" : undefined,
        sorter: (a, b, order) =>
          sortValue(a.run.result && m.value(a.run.result), order) - sortValue(b.run.result && m.value(b.run.result), order) || 0,
        render: (_, { run }) => {
          const r = run.result;
          if (!r) return <span style={{ color: MUTED }}>…</span>;
          const v = m.value(r);
          const top = v != null && best.get(m.key) === v;
          const warning = m.warn?.(r);
          if (warning) {
            return (
              <Tooltip title={warning}>
                <span style={{ color: "#faad14" }}>{m.format(v, r)}</span>
              </Tooltip>
            );
          }
          return <span style={top ? { color: BEST, fontWeight: 600 } : undefined}>{m.format(v, r)}</span>;
        },
      })
    ),
  ];

  return (
    <Table<Row>
      columns={columns}
      dataSource={rows}
      size="small"
      pagination={false}
      scroll={{ x: "max-content" }}
      showSorterTooltip={false}
      rowClassName={(r) => (r.key === focus ? "ant-table-row-selected" : "")}
      onRow={(r) => ({ onClick: () => onFocus(r.key), style: { cursor: "pointer" } })}
      summary={() =>
        benchmark && (
          <Table.Summary fixed="top">
            <Table.Summary.Row>
              <Table.Summary.Cell index={0}>
                <span style={{ color: MUTED }}>Buy &amp; hold (benchmark)</span>
              </Table.Summary.Cell>
              <Table.Summary.Cell index={1} />
              {metrics.map((m, i) => {
                const v = m.buyHold?.(benchmark);
                return (
                  <Table.Summary.Cell key={m.key} index={i + 2} align="right">
                    <span style={{ color: MUTED }}>{m.buyHold ? m.format(v, benchmark) : ""}</span>
                  </Table.Summary.Cell>
                );
              })}
            </Table.Summary.Row>
          </Table.Summary>
        )
      }
    />
  );
}
