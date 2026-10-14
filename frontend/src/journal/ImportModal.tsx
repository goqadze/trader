// Bring the bots' trades into the journal: one journal trade per sale, never twice. The live bots are ticked first.

import { App as AntApp, Modal, Table, Tag } from "antd";
import { useEffect, useState } from "react";
import { journalApi } from "./api";
import { MUTED } from "./catalog";
import type { JournalImportBot } from "./types";

const ACCOUNT = (b: JournalImportBot) => (b.live ? <Tag color="red">LIVE</Tag> : <Tag color="blue">{b.broker === "paper" ? "PAPER" : b.broker}</Tag>);

export default function ImportModal({ open, onClose, onImported }: { open: boolean; onClose: () => void; onImported: () => void }) {
  const { message } = AntApp.useApp();
  const [bots, setBots] = useState<JournalImportBot[] | null>(null);
  const [chosen, setChosen] = useState<number[]>([]);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!open) return;
    setBots(null);
    journalApi.importChoices().then(
      (b) => {
        setBots(b);
        setChosen(b.filter((x) => x.live && x.imported < x.closed_trades).map((x) => x.id));
      },
      (e) => message.error(e instanceof Error ? e.message : String(e)),
    );
  }, [open, message]);

  const run = async () => {
    setBusy(true);
    try {
      const r = await journalApi.importBots(chosen);
      message.success(r.imported ? `${r.imported} trade(s) imported${r.already ? ` (${r.already} were already in)` : ""}` : "Nothing new: those trades are in your journal already");
      onImported();
    } catch (e) {
      message.error(e instanceof Error ? e.message : String(e), 8);
    } finally {
      setBusy(false);
    }
  };

  const fresh = (bots ?? []).filter((b) => chosen.includes(b.id)).reduce((n, b) => n + b.closed_trades - b.imported, 0);
  return (
    <Modal open={open} title="Import the bots' trades" onCancel={onClose} onOk={run} okText={fresh ? `Import ${fresh} trade(s)` : "Import"}
           okButtonProps={{ disabled: !chosen.length }} confirmLoading={busy} width={720} style={{ top: 24 }}>
      <p style={{ color: MUTED, marginTop: 0 }}>
        Each sale a bot made becomes a closed journal trade (tagged "bot"), with its P&L to the cent. Importing again only adds the new ones.
        Then journal them like your own: how you felt watching the bot, and your review of its call.
      </p>
      <Table<JournalImportBot>
        size="small"
        rowKey="id"
        loading={!bots}
        dataSource={bots ?? []}
        pagination={false}
        scroll={{ y: 360, x: 520 }}
        rowSelection={{ selectedRowKeys: chosen, onChange: (keys) => setChosen(keys as number[]) }}
        locale={{ emptyText: "No bot has sold anything yet" }}
        columns={[
          { title: "Bot", key: "bot", render: (_, b) => <><b>{b.name}</b> <span style={{ color: MUTED }}>#{b.id}</span></> },
          { title: "Account", key: "account", render: (_, b) => ACCOUNT(b), width: 120 },
          { title: "Status", dataIndex: "status", width: 90 },
          { title: "Sales", key: "n", align: "right", width: 130,
            render: (_, b) => <>{b.closed_trades} {b.imported > 0 && <span style={{ color: MUTED }}>({b.imported} in)</span>}</> },
        ]}
      />
    </Modal>
  );
}
