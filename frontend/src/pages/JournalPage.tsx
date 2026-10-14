import { BookOutlined, DownloadOutlined, DownOutlined, MessageOutlined, PlusOutlined, RobotOutlined } from "@ant-design/icons";
import { Button, Dropdown, Grid, Space, Tabs, Typography } from "antd";
import { useEffect, useMemo, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { journalApi } from "../journal/api";
import { MUTED } from "../journal/catalog";
import DaysView from "../journal/DaysView";
import FilterBar from "../journal/FilterBar";
import ImportModal from "../journal/ImportModal";
import StatsView from "../journal/StatsView";
import { CheckInModal } from "../journal/StepModal";
import TradeForm from "../journal/TradeForm";
import TradeList from "../journal/TradeList";
import type { JournalFilters, JournalTrade } from "../journal/types";

const TABS = ["trades", "days", "stats"] as const;
type Tab = (typeof TABS)[number];
const FILTERS_KEY = "trading.journalFilters"; // the filters survive a reload (this browser only)

function savedFilters(): JournalFilters {
  try {
    return JSON.parse(sessionStorage.getItem(FILTERS_KEY) ?? "{}") as JournalFilters;
  } catch {
    return {};
  }
}

/** The trading journal: your trades with the plan, the feelings at each step and the review; a page per day with
 *  check-ins; and what your own record says. It's yours alone: nobody else who signs in sees it. */
export default function JournalPage() {
  const navigate = useNavigate();
  const mobile = Grid.useBreakpoint().md === false;
  const [params, setParams] = useSearchParams();
  const tab: Tab = (TABS as readonly string[]).includes(params.get("tab") ?? "") ? (params.get("tab") as Tab) : "trades";
  const [filters, setFilters] = useState<JournalFilters>(savedFilters);
  const [reloadKey, setReloadKey] = useState(0);
  const [creating, setCreating] = useState(false);
  const [checkIn, setCheckIn] = useState(false);
  const [importing, setImporting] = useState(false);
  const [openTrades, setOpenTrades] = useState<JournalTrade[]>([]);

  useEffect(() => {
    try {
      sessionStorage.setItem(FILTERS_KEY, JSON.stringify(filters));
    } catch {
      // private mode: the filters just don't survive a reload
    }
  }, [filters]);
  // The open trades, so a check-in can be about one of them
  useEffect(() => {
    journalApi.trades({ status: "open" }, { offset: 0, limit: 100 }).then((p) => setOpenTrades(p.items), () => undefined);
  }, [reloadKey]);

  // The statistics are about closed trades: the list's status filter doesn't apply there
  const statsFilters = useMemo(() => ({ ...filters, status: undefined }), [filters]);
  const reload = () => setReloadKey((k) => k + 1);
  return (
    <>
      <Space align="center" wrap style={{ width: "100%", justifyContent: "space-between", marginBottom: 8 }}>
        <div>
          <Typography.Title level={4} style={{ margin: 0 }}><BookOutlined /> Trading journal</Typography.Title>
          {!mobile && <span style={{ color: MUTED, fontSize: 12 }}>Plan each trade, write how you feel at every step, review it. Only you can see your journal.</span>}
        </div>
        <Space wrap>
          <Button icon={<MessageOutlined />} onClick={() => setCheckIn(true)}>Check in</Button>
          <Button type="primary" icon={<PlusOutlined />} onClick={() => setCreating(true)}>New trade</Button>
          <Dropdown
            trigger={["click"]}
            placement="bottomRight"
            menu={{ items: [
              { key: "import", icon: <RobotOutlined />, label: "Import the bots' trades", onClick: () => setImporting(true) },
              { key: "csv", icon: <DownloadOutlined />, label: <a href={journalApi.csvUrl(filters)} download>Export to CSV (filtered)</a> },
            ] }}
          >
            <Button>More <DownOutlined style={{ fontSize: 10 }} /></Button>
          </Dropdown>
        </Space>
      </Space>

      <Tabs
        activeKey={tab}
        onChange={(t) => setParams(t === "trades" ? {} : { tab: t }, { replace: true })}
        items={[
          { key: "trades", label: "Trades", children: <><FilterBar value={filters} onChange={setFilters} /><TradeList filters={filters} reloadKey={reloadKey} /></> },
          { key: "days", label: "Days", children: <DaysView reloadKey={reloadKey} /> },
          { key: "stats", label: "Statistics", children: <><FilterBar value={filters} onChange={setFilters} showStatus={false} /><StatsView filters={statsFilters} reloadKey={reloadKey} /></> },
        ]}
        destroyOnHidden
      />

      <TradeForm open={creating} onClose={() => setCreating(false)} onSaved={(t) => { setCreating(false); navigate(`/journal/trades/${t.id}`); }} />
      <CheckInModal open={checkIn} openTrades={openTrades} onClose={() => setCheckIn(false)} onSaved={() => { setCheckIn(false); reload(); }} />
      <ImportModal open={importing} onClose={() => setImporting(false)} onImported={() => { setImporting(false); reload(); }} />
    </>
  );
}
