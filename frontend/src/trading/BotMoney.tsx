// A bot's money from its start (trading-service ledger.of_bot): the bot page's Money history popup, with the sum from
// what was put in to its equity and every move of money into or out of the bot.

import { Alert, Card, Grid, Modal, Spin, Table, Tooltip } from "antd";
import type { ColumnsType } from "antd/es/table";
import { useEffect, useState } from "react";
import { Line } from "./AccountLedger";
import { tradingApi } from "./api";
import { Money, day } from "./ArchivedHistory";
import { nyTime, usd } from "./format";
import type { Bot, BotLedger, BotMove } from "./types";

const MUTED = "#8b98b5";
const small = { color: MUTED, fontSize: 12 };

const WHAT: Record<BotMove["kind"], string> = {
  start: "Started with",
  add: "Added",
  take_out: "Taken out",
  archived: "Archived: its cash went back to the account",
};

/** The sum: what is still put in, the gain or loss on top of it, and the equity they make. */
function TheSum({ l }: { l: BotLedger }) {
  const moved = l.added > 0 || l.taken_out > 0;
  return (
    <Card size="small" style={{ marginBottom: 12 }}>
      <Line label="Started with" v={l.start} />
      {l.added > 0 && <Line label="Added" v={l.added} signed />}
      {l.taken_out > 0 && <Line label="Taken out" v={-l.taken_out} signed />}
      {moved && <Line label={l.taken_out > 0 ? "Put in, less taken out" : "Put in"} v={l.net} strong />}
      <Line label="Gain or loss" v={l.result} signed />
      <Line label={l.archived ? "Went back to the account" : "Equity"} v={l.equity} strong />
      <div style={{ ...small, marginTop: 8 }}>
        The gain or loss is what its trades made, closed and open{l.fees > 0 ? `, after ${usd(l.fees)} of Alpaca's fees` : ""}.
        Adding or taking out money changes what is put in, not the gain.
      </div>
    </Card>
  );
}

/** The bot page's Money history popup: the sum, then every move, the newest first. Read when it opens, and again
 * when the page's next read moves the equity, so the sum matches the Equity card. */
export function BotMoneyHistory({ bot, open, onClose }: { bot: Bot; open: boolean; onClose: () => void }) {
  const phone = Grid.useBreakpoint().sm === false;
  const [ledger, setLedger] = useState<BotLedger | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!open) return;
    let gone = false;
    tradingApi.botLedger(bot.id).then(
      (l) => { if (!gone) { setLedger(l); setError(""); } },
      (e) => { if (!gone) setError(e instanceof Error ? e.message : String(e)); },
    );
    return () => { gone = true; };
  }, [open, bot.id, bot.equity, bot.status]);

  const l = ledger?.bot_id === bot.id ? ledger : null; // not another bot's, after following a link
  const when = (m: BotMove) => <Tooltip title={nyTime(m.at)}>{day(m.at)}</Tooltip>;
  const what = (m: BotMove) => <>{WHAT[m.kind]}{m.note && <div style={small}>{m.note}</div>}</>;
  const amount = { title: <Tooltip title="How much went into the bot; negative: came out of it">Money</Tooltip>, dataIndex: "amount", align: "right", render: (v: number) => <Money v={v} /> } as const;
  const after = { title: <Tooltip title="What was put in less what was taken out, after this line. Below zero: more came out than went in.">Put in</Tooltip>, dataIndex: "net_after", align: "right", render: (v: number) => usd(v) } as const;
  // A phone has room for the two amounts only: the day goes under what happened
  const columns: ColumnsType<BotMove> = phone ? [
    { title: "Move", key: "what", render: (_, m) => <>{what(m)}<div style={small}>{when(m)}</div></> },
    amount,
    after,
  ] : [
    { title: "Day", key: "at", width: 80, render: (_, m) => when(m) },
    { title: "Move", key: "what", render: (_, m) => what(m) },
    { ...amount, width: 100 },
    { ...after, width: 100 },
  ];

  return (
    <Modal title={phone ? "Money history" : `Money history · ${bot.name}`} open={open} onCancel={onClose} footer={null} width={phone ? undefined : 680} destroyOnHidden>
      {error && <Alert type="error" showIcon style={{ marginBottom: 12 }} message={`Couldn't read the money history: ${error}`} />}
      {l ? (
        <>
          <TheSum l={l} />
          <Table<BotMove>
            rowKey={(m) => `${m.kind}-${m.at}-${m.note}`}
            size="small"
            dataSource={[...l.moves].reverse()}
            columns={columns}
            pagination={{ pageSize: 50, hideOnSinglePage: true, showSizeChanger: false }}
          />
        </>
      ) : !error && <div style={{ textAlign: "center", padding: 24 }}><Spin /></div>}
    </Modal>
  );
}
