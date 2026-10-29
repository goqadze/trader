import { Alert, Button, Grid, Modal, Table, Tooltip } from "antd";
import type { ColumnsType } from "antd/es/table";
import { useState } from "react";
import { Link } from "react-router-dom";
import { tradingApi } from "./api";
import { botTitle, nyTime, pnlColor, usd } from "./format";
import type { ArchivedBot, ArchivedBots } from "./types";

const signed = (v: number) => `${v > 0.005 ? "+" : ""}${usd(v)}`;
const Money = ({ v }: { v: number }) => <span style={{ color: pnlColor(v) }}>{signed(v)}</span>;
const day = (iso: string) => new Date(iso).toLocaleDateString("en-US", { timeZone: "America/New_York", month: "short", day: "numeric" });

/** The line under Total P&L: what the shown broker's archived bots made or lost from start to archive. Total P&L
 * counts only the bots that aren't archived (archiving hands a bot's cash back to the account), so without this a loss
 * in a bot that was closed would show nowhere. History lists them, the earliest archived first, with a running total. */
export function ArchivedLine({ broker, label, count, result, runningPnl }: {
  broker: string; // the broker shown ("all": every broker)
  label: string;
  count: number;
  result: number;
  runningPnl: number; // the running bots' Total P&L, for "all bots together"
}) {
  const phone = Grid.useBreakpoint().sm === false;
  const [open, setOpen] = useState(false);
  const [data, setData] = useState<ArchivedBots | null>(null);
  const [error, setError] = useState<string | null>(null);
  if (count === 0) return null;

  const show = async () => {
    setOpen(true);
    setData(null);
    setError(null);
    try {
      setData(await tradingApi.archivedBots(broker));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const when = (b: ArchivedBot) => b.archived_at
    ? <Tooltip title={nyTime(b.archived_at)}>{day(b.archived_at)}</Tooltip>
    : <Tooltip title={`No archive record (archived by a script): made ${nyTime(b.created_at)}`}>—</Tooltip>;
  const resultCol = { title: "Result", dataIndex: "result", align: "right", render: (v: number) => <Money v={v} /> } as const;
  const runningCol = {
    title: <Tooltip title="This bot's result and those of every bot archived before it, added up">{phone ? "Total" : "Running total"}</Tooltip>,
    dataIndex: "running", align: "right", render: (v: number) => <Money v={v} />,
  } as const;
  const bot = (b: ArchivedBot) => (
    <>
      <Link to={`/trading/${b.id}`}>#{b.id} {botTitle(b)}</Link>
      <div style={{ color: "#8b98b5", fontSize: 12 }}>{broker === "all" ? `${b.broker} · ${b.name}` : b.name}</div>
    </>
  );
  // A phone has room for the result and the running total only: the date and the money in and out go under the bot
  const columns: ColumnsType<ArchivedBot> = phone ? [
    {
      title: "Bot", key: "bot",
      render: (_, b) => (
        <>
          {bot(b)}
          <div style={{ color: "#8b98b5", fontSize: 12 }}>{when(b)} · {usd(b.put_in)} → {usd(b.came_out)}</div>
        </>
      ),
    },
    resultCol,
    runningCol,
  ] : [
    { title: "Archived", key: "at", width: 80, render: (_, b) => when(b) },
    { title: "Bot", key: "bot", render: (_, b) => bot(b) },
    { title: "Put in", dataIndex: "put_in", align: "right", width: 90, render: (v: number) => usd(v) },
    {
      title: <Tooltip title="Its cash at the end, plus any money taken out while it ran">Came out</Tooltip>,
      dataIndex: "came_out", align: "right", width: 90, render: (v: number) => usd(v),
    },
    { ...resultCol, width: 90 },
    { ...runningCol, width: 110 },
  ];

  return (
    <>
      <div style={{ color: "#8b98b5", fontSize: 12 }}>
        <Tooltip title={`What the ${count} archived bot${count === 1 ? "" : "s"} made or lost from start to archive. Not in the Total P&L above: it counts only the bots that aren't archived, because archiving hands a bot's cash back to the account.`}>
          {count} archived:{" "}
        </Tooltip>
        <Money v={result} /> ·{" "}
        <Button type="link" size="small" onClick={show} style={{ padding: 0, height: "auto", fontSize: 12 }}>History</Button>
      </div>
      <Modal
        title={`Archived bots · ${label}`}
        open={open}
        onCancel={() => setOpen(false)}
        footer={null}
        width={phone ? undefined : 820}
        destroyOnHidden
      >
        {error && <Alert type="error" showIcon message={`Can't load the archived bots: ${error}`} />}
        {data && (
          <p style={{ color: "#8b98b5" }}>
            {data.items.length} archived bot{data.items.length === 1 ? "" : "s"}: <Money v={data.result} /> in all. With the running bots'
            Total P&L (<Money v={runningPnl} />), every bot together: <Money v={data.result + runningPnl} />.
          </p>
        )}
        <Table<ArchivedBot>
          rowKey="id"
          size="small"
          loading={!data && !error}
          dataSource={data?.items ?? []}
          columns={columns}
          pagination={{ pageSize: 50, hideOnSinglePage: true, showSizeChanger: false }}
          scroll={phone ? undefined : { x: 640 }}
        />
      </Modal>
    </>
  );
}
