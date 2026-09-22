import { CopyOutlined } from "@ant-design/icons";
import { Button, Card, Table, Tag, message } from "antd";
import type { ColumnsType } from "antd/es/table";
import type { Action, LogRow } from "../types";

// News sentiment -> tag color. "unavailable"/"" means the news layer wasn't active for that decision.
const SENTIMENT_COLOR: Record<string, string> = { bullish: "green", bearish: "red", neutral: "blue" };

const columns: ColumnsType<LogRow> = [
  { title: "Date", dataIndex: "date", width: 110 },
  {
    title: "Action",
    dataIndex: "action",
    width: 90,
    render: (a: Action) => <Tag color={a === "BUY" ? "green" : a === "SELL" ? "red" : "default"}>{a}</Tag>,
  },
  { title: "Conf.", dataIndex: "confidence", width: 70, render: (c: number) => (c != null ? c.toFixed(2) : "") },
  {
    title: "Sentiment",
    dataIndex: "sentiment",
    width: 110,
    render: (s: string) => (s ? <Tag color={SENTIMENT_COLOR[s] ?? "default"}>{s}</Tag> : ""),
  },
  { title: "Price", dataIndex: "price", width: 90, render: (p: number) => `$${p.toFixed(2)}` },
  { title: "Equity", dataIndex: "equity", width: 100, render: (e: number) => `$${Math.round(e).toLocaleString()}` },
  {
    title: "Explanation",
    dataIndex: "reasoning",
    ellipsis: { showTitle: true }, // truncate; full text shows on hover
    render: (r: string) => r || <span style={{ color: "#8b98b5" }}>—</span>,
  },
];

// Copy the whole table as JSON so it can be pasted elsewhere (e.g. for analysis).
// Drops the internal `key` field and keeps the meaningful columns.
async function copyRowsAsJson(rows: LogRow[]) {
  const data = rows.map(({ key: _key, ...rest }) => rest);
  const json = JSON.stringify(data, null, 2);
  try {
    await navigator.clipboard.writeText(json);
    message.success(`Copied ${data.length} rows as JSON`);
  } catch {
    message.error("Couldn't copy — clipboard blocked by the browser");
  }
}

// Scrollable table of decision-day rows (newest first).
export default function ActivityLog({ rows }: { rows: LogRow[] }) {
  return (
    <Card
      title="Activity log"
      size="small"
      style={{ marginTop: 16 }}
      extra={
        <Button
          size="small"
          icon={<CopyOutlined />}
          disabled={rows.length === 0}
          onClick={() => copyRowsAsJson(rows)}
        >
          Copy JSON
        </Button>
      }
    >
      <Table<LogRow> columns={columns} dataSource={rows} size="small" pagination={false} scroll={{ y: 280 }} />
    </Card>
  );
}
