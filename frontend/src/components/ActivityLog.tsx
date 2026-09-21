import { Card, Table, Tag } from "antd";
import type { ColumnsType } from "antd/es/table";
import type { Action, LogRow } from "../types";

const columns: ColumnsType<LogRow> = [
  { title: "Date", dataIndex: "date", width: 110 },
  {
    title: "Action",
    dataIndex: "action",
    width: 90,
    render: (a: Action) => <Tag color={a === "BUY" ? "green" : a === "SELL" ? "red" : "default"}>{a}</Tag>,
  },
  { title: "Conf.", dataIndex: "confidence", width: 70, render: (c: number) => (c != null ? c.toFixed(2) : "") },
  { title: "Price", dataIndex: "price", render: (p: number) => `$${p.toFixed(2)}` },
  { title: "Equity", dataIndex: "equity", render: (e: number) => `$${Math.round(e).toLocaleString()}` },
];

// Scrollable table of decision-day rows (newest first).
export default function ActivityLog({ rows }: { rows: LogRow[] }) {
  return (
    <Card title="Activity log" size="small" style={{ marginTop: 16 }}>
      <Table<LogRow> columns={columns} dataSource={rows} size="small" pagination={false} scroll={{ y: 280 }} />
    </Card>
  );
}
