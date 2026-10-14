// The journal's trades: newest first (or by P&L / R), filtered, a page at a time, with a one-line summary on top.

import { CameraOutlined, CheckCircleFilled, CloseCircleFilled, MessageOutlined, RobotOutlined } from "@ant-design/icons";
import { Alert, Empty, Space, Table, Tag, Tooltip } from "antd";
import type { ColumnsType } from "antd/es/table";
import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { pct, pnlColor, usd } from "../trading/format";
import { journalApi, type TradeSort } from "./api";
import { EmotionEmojis, GRADE_LETTER, MUTED, StatusTag, dayLabel, mistakeLabel, rText } from "./catalog";
import type { JournalFilters, JournalPage, JournalStats, JournalTrade } from "./types";

const PAGE = 25;

/** "14 closed · +$340.12 · 57% wins · 2 open · 1 planned" */
export function Summary({ stats }: { stats: JournalStats | null }) {
  if (!stats) return null;
  const parts = [
    stats.open ? `${stats.open} open` : "",
    stats.planned ? `${stats.planned} planned` : "",
    stats.skipped ? `${stats.skipped} skipped` : "",
  ].filter(Boolean);
  return (
    <Space size={12} wrap style={{ marginBottom: 8 }}>
      <span><b>{stats.trades}</b> closed</span>
      <span style={{ color: pnlColor(stats.net_pnl), fontWeight: 600 }}>{usd(stats.net_pnl)}</span>
      {stats.win_rate != null && <span>{stats.win_rate}% wins</span>}
      {stats.avg_r != null && <span>avg {rText(stats.avg_r)}</span>}
      {parts.length > 0 && <span style={{ color: MUTED }}>{parts.join(" · ")}</span>}
    </Space>
  );
}

function Review({ t }: { t: JournalTrade }) {
  if (t.status !== "closed") return <span style={{ color: MUTED }}>—</span>;
  if (t.followed_plan == null && !t.grade && !t.mistakes.length) return <span style={{ color: MUTED, fontSize: 12 }}>not reviewed</span>;
  return (
    <Space size={4}>
      {t.followed_plan != null && (
        <Tooltip title={t.followed_plan ? "Followed the plan" : "Broke the plan"}>
          {t.followed_plan ? <CheckCircleFilled style={{ color: "#33c088" }} /> : <CloseCircleFilled style={{ color: "#ef5b6b" }} />}
        </Tooltip>
      )}
      {t.grade && <Tag style={{ marginInlineEnd: 0 }}>{GRADE_LETTER[t.grade - 1]}</Tag>}
      {t.mistakes.length > 0 && (
        <Tooltip title={t.mistakes.map(mistakeLabel).join(", ")}>
          <Tag color="volcano" style={{ marginInlineEnd: 0 }}>{t.mistakes.length} mistake{t.mistakes.length > 1 ? "s" : ""}</Tag>
        </Tooltip>
      )}
    </Space>
  );
}

export default function TradeList({ filters, reloadKey }: { filters: JournalFilters; reloadKey: number }) {
  const navigate = useNavigate();
  const [page, setPage] = useState(1);
  const [sort, setSort] = useState<{ by: TradeSort; order: "asc" | "desc" }>({ by: "day", order: "desc" });
  const [data, setData] = useState<JournalPage | null>(null);
  const [stats, setStats] = useState<JournalStats | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => setPage(1), [filters]);
  useEffect(() => {
    let live = true;
    setLoading(true);
    Promise.all([journalApi.trades(filters, { offset: (page - 1) * PAGE, limit: PAGE, sort: sort.by, order: sort.order }), journalApi.stats(filters)])
      .then(([d, s]) => { if (live) { setData(d); setStats(s); setError(null); } },
            (e) => live && setError(e instanceof Error ? e.message : String(e)))
      .finally(() => live && setLoading(false));
    return () => { live = false; };
  }, [filters, page, sort, reloadKey]);

  const sortOrder = (by: TradeSort) => (sort.by === by ? (sort.order === "asc" ? "ascend" : "descend") : null);
  const columns: ColumnsType<JournalTrade> = [
    {
      title: "Day",
      key: "day",
      width: 120,
      sorter: true,
      sortOrder: sortOrder("day"),
      sortDirections: ["descend", "ascend"],
      render: (_, t) => (
        <Space direction="vertical" size={2}>
          <span style={{ whiteSpace: "nowrap" }}>{dayLabel(t.day)}</span>
          <StatusTag status={t.status} />
        </Space>
      ),
    },
    {
      title: "Trade",
      key: "trade",
      sorter: true,
      sortOrder: sortOrder("symbol"),
      sortDirections: ["ascend", "descend"],
      render: (_, t) => (
        <Space direction="vertical" size={0}>
          <Space size={6}>
            <b style={{ fontSize: 15 }}>{t.symbol}</b>
            <Tag color={t.side === "long" ? "green" : "volcano"} style={{ marginInlineEnd: 0 }}>{t.side}</Tag>
            {t.source === "bot" && <Tooltip title="Imported from a bot"><RobotOutlined style={{ color: "#b37feb" }} /></Tooltip>}
          </Space>
          <span style={{ color: MUTED, fontSize: 12 }}>{[t.setup, t.account].filter(Boolean).join(" · ") || " "}</span>
        </Space>
      ),
    },
    {
      title: "P&L",
      key: "pnl",
      align: "right",
      sorter: true,
      sortOrder: sortOrder("pnl"),
      sortDirections: ["descend", "ascend"],
      render: (_, t) => t.pnl == null ? <span style={{ color: MUTED }}>—</span> : (
        <Space direction="vertical" size={0} style={{ alignItems: "flex-end" }}>
          <b style={{ color: pnlColor(t.pnl) }}>{usd(t.pnl)}</b>
          <span style={{ color: MUTED, fontSize: 12 }}>{pct(t.return_pct)}</span>
        </Space>
      ),
    },
    {
      title: "R",
      key: "r",
      align: "right",
      responsive: ["sm"],
      sorter: true,
      sortOrder: sortOrder("r"),
      sortDirections: ["descend", "ascend"],
      render: (_, t) => <span style={{ color: pnlColor(t.r_multiple) }}>{rText(t.r_multiple)}</span>,
    },
    { title: "Feelings", key: "feel", responsive: ["md"], render: (_, t) => <EmotionEmojis names={t.emotions} /> },
    { title: "Review", key: "review", responsive: ["lg"], render: (_, t) => <Review t={t} /> },
    {
      title: "",
      key: "extra",
      responsive: ["lg"],
      render: (_, t) => (
        <Space size={8} style={{ color: MUTED, fontSize: 12 }}>
          {t.notes > 0 && <span><MessageOutlined /> {t.notes}</span>}
          {t.images > 0 && <span><CameraOutlined /> {t.images}</span>}
          {t.tags.slice(0, 3).map((x) => <Tag key={x} style={{ marginInlineEnd: 0 }}>{x}</Tag>)}
        </Space>
      ),
    },
  ];

  if (error) return <Alert type="error" showIcon message={`Can't load the journal: ${error}`} />;
  return (
    <>
      <Summary stats={stats} />
      <Table<JournalTrade>
        size="middle"
        rowKey="id"
        loading={loading}
        columns={columns}
        dataSource={data?.items ?? []}
        onRow={(t) => ({ onClick: () => navigate(`/journal/trades/${t.id}`), style: { cursor: "pointer" } })}
        onChange={(_p, _f, sorter) => {
          const s = Array.isArray(sorter) ? sorter[0] : sorter;
          const by: TradeSort = s?.order ? ({ day: "day", trade: "symbol", pnl: "pnl", r: "r" } as Record<string, TradeSort>)[String(s.columnKey)] ?? "day" : "day";
          setSort({ by, order: s?.order === "ascend" ? "asc" : "desc" });
        }}
        pagination={{ current: page, pageSize: PAGE, total: data?.total ?? 0, onChange: setPage, showSizeChanger: false, hideOnSinglePage: true, size: "small" }}
        locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={Object.values(filters).some(Boolean) ? "No trade matches the filters" : "No trades yet: add your first one, or import the bots' trades"} /> }}
      />
    </>
  );
}
