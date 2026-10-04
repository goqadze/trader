import { StopOutlined } from "@ant-design/icons";
import { Alert, Button, Card, Popconfirm, Progress, Select, Space, Switch, Table, Tag, Tooltip, Typography } from "antd";
import type { ColumnsType } from "antd/es/table";
import dayjs from "dayjs";
import { useState } from "react";
import { VerdictTag } from "../components/VerdictView";
import { strategyShortName } from "../strategies";
import { GRADE_LOOK } from "../verdict";
import CellDetail from "./CellDetail";
import PooledTable from "./PooledTable";
import { consistency } from "./consistency";
import type { ScanCell, ScanRunView, ScanSummary, ScanView } from "./types";

const MUTED = "#8b98b5";
const signed = (v: number) => `${v > 0 ? "+" : ""}${v.toFixed(1)}%`;

const rank = (r: ScanRunView | null) => (r?.verdict ? GRADE_LOOK[r.verdict.grade].rank : 0);
const passed = (r: ScanRunView | null) => r?.verdict?.grade === "paper";
const isFinalist = (c: ScanCell) => passed(c.practice) && passed(c.exam);
/** Finalists first, then by how far each got: exam grade, practice grade, practice return. */
const order = (c: ScanCell) => rank(c.exam) * 1000 + rank(c.practice) * 100 + (c.practice?.summary?.alpha_vs_buy_hold_pct ?? -99) / 1000;

/** A run's state in one cell: its grade once done, else its progress or failure. */
function RunCell({ run, note }: { run: ScanRunView | null; note?: string | null }) {
  if (!run)
    return note?.startsWith("skipped") ? (
      <Tooltip title="Only combinations that passed practice sit the exam">
        <span style={{ color: MUTED, fontSize: 12, whiteSpace: "nowrap" }}>not examined</span>
      </Tooltip>
    ) : (
      <span style={{ color: MUTED, fontSize: 12, whiteSpace: "nowrap" }}>{note ?? "…"}</span>
    );
  if (run.verdict) return <VerdictTag verdict={run.verdict} />;
  if (run.status === "running") return <Progress percent={Math.round(run.progress * 100)} size="small" style={{ width: 110, margin: 0 }} />;
  if (run.status === "error")
    return (
      <Tooltip title={run.error}>
        <Tag color="error">error</Tag>
      </Tooltip>
    );
  return <Tag>{run.status === "pending" ? "queued" : run.status}</Tag>;
}

function Numbers({ run }: { run: ScanRunView | null }) {
  const s = run?.summary;
  if (!s) return null;
  return (
    <span style={{ fontSize: 12 }}>
      <span style={{ color: s.total_return_pct >= 0 ? "#33c088" : "#ef5b6b" }}>{signed(s.total_return_pct)}</span>
      <span style={{ color: MUTED }}>
        {" "}
        vs {signed(s.buy_hold_return_pct)} · {s.num_trades} trades
      </span>
    </span>
  );
}

function Summary({ scan }: { scan: ScanView }) {
  const hasExam = !!scan.config.exam;
  const winners = scan.cells.filter((c) => (hasExam ? isFinalist(c) : passed(c.practice)));
  const done = scan.status !== "running";
  const held = consistency(scan.cells, scan.config.symbols, hasExam);
  const what = hasExam ? "practice and the exam" : "practice";
  return (
    <Alert
      type={winners.length ? "success" : done ? "warning" : "info"}
      showIcon
      style={{ marginBottom: 16 }}
      message={
        winners.length ? (
          <span>
            {done ? "" : "So far, "}passed {what}:{" "}
            <b>{winners.map((c) => `${c.symbol} · ${strategyShortName(c.strategy)}`).join(", ")}</b>
          </span>
        ) : done ? (
          `No combination passed ${what}. That's an answer too: none of these strategies earned its keep here.`
        ) : (
          `Nothing has passed ${what} yet.`
        )
      }
      description={
        <>
          {held.length > 0 && <div>Across symbols: {held.join(" · ")}. A strategy that passes on several similar symbols is more believable than one lucky pair.</div>}
          <div>
            {winners.length
              ? "Next: open a finalist, re-check it with news, then paper-trade it before risking real money."
              : "The table shows what each combination missed; click a row for its checklist."}{" "}
            Trying many combinations finds some lucky winners: that's why the exam uses years the practice never saw.
          </div>
        </>
      }
    />
  );
}

interface Props {
  scan: ScanView;
  scans: ScanSummary[];
  onPick: (id: string) => void;
  onStop: () => void;
}

/** A scan's progress, summary and every combination's practice and exam result. */
export default function ScanResults({ scan, scans, onPick, onStop }: Props) {
  const [openKey, setOpenKey] = useState<string>(); // the open combination, looked up in every poll's fresh cells
  const key = (c: ScanCell) => `${c.symbol}/${c.strategy}`;
  const open = scan.cells.find((c) => key(c) === openKey);
  const [onlyPassed, setOnlyPassed] = useState(false);
  const cfg = scan.config;
  const news = cfg.runs.every((r) => r.news !== false);
  // While it runs, rows stay put (results arriving would move them under the mouse); once it ends, the best go first
  const shown = scan.cells.filter((c) => !onlyPassed || passed(c.practice));
  const rows = scan.status === "running" ? shown : [...shown].sort((a, b) => order(b) - order(a));

  const columns: ColumnsType<ScanCell> = [
    {
      title: "Symbol",
      dataIndex: "symbol",
      width: 80,
      filters: cfg.symbols.map((s) => ({ text: s, value: s })),
      onFilter: (v, c) => c.symbol === v,
    },
    { title: "Strategy", key: "strategy", width: 170, render: (_, c) => strategyShortName(c.strategy) },
    {
      title: `Practice ${cfg.practice.start.slice(0, 4)}–${cfg.practice.end.slice(0, 4)}`,
      key: "practice",
      width: 170,
      sorter: (a, b) => rank(a.practice) - rank(b.practice),
      render: (_, c) => <RunCell run={c.practice} />,
    },
    { title: "Return vs B&H · trades", key: "pnum", render: (_, c) => <Numbers run={c.practice} /> },
    ...(cfg.exam
      ? ([
          {
            title: `Exam ${cfg.exam.start.slice(0, 4)}–${cfg.exam.end.slice(0, 4)}`,
            key: "exam",
            width: 170,
            sorter: (a, b) => rank(a.exam) - rank(b.exam),
            render: (_, c) => <RunCell run={c.exam} note={c.exam_note} />,
          },
          { title: "Return vs B&H · trades", key: "enum", render: (_, c) => <Numbers run={c.exam} /> },
        ] as ColumnsType<ScanCell>)
      : []),
  ];

  return (
    <>
      <Card
        size="small"
        style={{ marginBottom: 16 }}
        title={
          <Space wrap>
            <span>
              {cfg.symbols.length} symbol{cfg.symbols.length === 1 ? "" : "s"} × {cfg.runs.length} strateg{cfg.runs.length === 1 ? "y" : "ies"}
            </span>
            <Tag color={news ? "blue" : "default"}>{news ? "with news" : "technical only"}</Tag>
            <Tag color={scan.status === "running" ? "processing" : scan.status === "done" ? "success" : "default"}>{scan.status}</Tag>
          </Space>
        }
        extra={
          <Space>
            {scans.length > 1 && (
              <Select
                size="small"
                value={scan.scan_id}
                onChange={onPick}
                style={{ width: 260 }}
                options={scans.map((s) => ({
                  value: s.scan_id,
                  label: `${dayjs(s.created_at).format("MMM D HH:mm")} · ${s.symbols.length}×${s.strategies.length} · ${s.status}`,
                }))}
              />
            )}
            {scan.status === "running" && (
              <Popconfirm title="Stop this scan?" description="Finished results stay; queued runs won't start." onConfirm={onStop}>
                <Button size="small" danger icon={<StopOutlined />}>
                  Stop
                </Button>
              </Popconfirm>
            )}
          </Space>
        }
      >
        <Progress percent={Math.round((scan.finished / scan.total) * 100)} status={scan.status === "running" ? "active" : undefined} />
        <Typography.Text style={{ fontSize: 12, color: MUTED }}>
          {scan.finished} of {scan.total} combinations finished · started {dayjs(scan.created_at).format("MMM D, HH:mm")} · practice{" "}
          {cfg.practice.start} → {cfg.practice.end}
          {cfg.exam ? ` · exam ${cfg.exam.start} → ${cfg.exam.end}${cfg.exam_all ? " (every combination)" : " (practice passers only)"}` : ""}
        </Typography.Text>
      </Card>
      <Summary scan={scan} />
      <PooledTable scan={scan} />
      <Card
        size="small"
        title="Combinations"
        extra={
          <Space size={6} style={{ fontSize: 12, color: MUTED }}>
            Only practice passers <Switch size="small" checked={onlyPassed} onChange={setOnlyPassed} />
          </Space>
        }
      >
        <Table<ScanCell>
          columns={columns}
          dataSource={rows}
          rowKey={key}
          size="small"
          pagination={{ pageSize: 50, hideOnSinglePage: true }}
          scroll={{ x: "max-content" }}
          rowClassName={(c) => (isFinalist(c) ? "ant-table-row-selected" : "")}
          onRow={(c) => ({ onClick: () => setOpenKey(key(c)), style: { cursor: "pointer" } })}
        />
      </Card>
      <CellDetail cell={open} onClose={() => setOpenKey(undefined)} />
    </>
  );
}
