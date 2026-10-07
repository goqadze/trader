import { MailOutlined, SendOutlined } from "@ant-design/icons";
import { Alert, App as AntApp, Button, Card, Checkbox, Col, Form, Row, Select, Space, Switch, Table, Tag, Tooltip, Typography } from "antd";
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { tradingApi } from "../trading/api";
import { localTime } from "../trading/format";
import type { EmailAlert, EmailAlerts, EmailAlertsSave } from "../trading/types";
import { usePolling } from "../trading/usePolling";

const MUTED = "#8b98b5";
const CATEGORY_COLOR: Record<string, string> = { trades: "blue", risk: "red", problems: "gold", signals: "cyan", test: "green" };
const EMAIL = /^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$/; // as trading-service checks it
const STATUS_COLOR: Record<EmailAlert["status"], string> = { sent: "green", pending: "blue", failed: "red", skipped: "default" };

/** What to put in trading-service/.env when no mail server is set up yet (Gmail as the example). */
function SmtpSetup() {
  return (
    <Alert
      type="warning"
      showIcon
      style={{ marginBottom: 16 }}
      message="No mail server set up yet: nothing can be sent"
      description={
        <>
          <div>Add these to <code>trading-service/.env</code>, then restart it:</div>
          <pre style={{ margin: "8px 0", fontSize: 12, whiteSpace: "pre-wrap" }}>
            {"SMTP_HOST=smtp.gmail.com\nSMTP_PORT=587\nSMTP_USERNAME=you@gmail.com\nSMTP_PASSWORD=your 16-letter App Password"}
          </pre>
          <pre style={{ margin: "0 0 8px", fontSize: 12 }}>docker compose up -d --force-recreate trading-service</pre>
          <div style={{ fontSize: 12 }}>
            Gmail doesn't take your normal password here: turn on 2-Step Verification, then create an App Password at{" "}
            <a href="https://myaccount.google.com/apppasswords" target="_blank" rel="noreferrer">myaccount.google.com/apppasswords</a>.
            Any other SMTP server works too (SMTP_SECURITY=ssl for port 465).
          </div>
        </>
      }
    />
  );
}

/** Email alerts: buys and sells, stop-losses and blacklists, the drawdown breaker, errors, and optionally every dip
 *  signal, to the addresses set here. The service queues each alert with its event and sends what piled up as one
 *  email every few seconds, retrying a failed send. */
export default function EmailAlertsPage() {
  const { message } = AntApp.useApp();
  const [form] = Form.useForm<EmailAlertsSave>();
  const [cfg, setCfg] = useState<EmailAlerts | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const history = usePolling(() => tradingApi.emailHistory(50), 15_000);
  const enabled = Form.useWatch("enabled", form);

  useEffect(() => {
    tradingApi.emailAlerts().then(
      (c) => {
        setCfg(c);
        form.setFieldsValue({ enabled: c.enabled, recipients: c.recipients, categories: c.categories });
      },
      (e) => setLoadError(e instanceof Error ? e.message : String(e)),
    );
  }, [form]);

  const save = async (quiet = false) => {
    const v = await form.validateFields();
    setSaving(true);
    try {
      const c = await tradingApi.saveEmailAlerts(v);
      setCfg(c);
      if (!quiet) message.success(c.enabled ? "Saved: email alerts are on" : "Saved: email alerts are off");
      return true;
    } catch (e) {
      message.error(e instanceof Error ? e.message : String(e));
      return false;
    } finally {
      setSaving(false);
    }
  };

  // The test goes to the saved addresses, so save what's on the form first
  const test = async () => {
    if (!(await save(true))) return;
    setTesting(true);
    try {
      await tradingApi.testEmail();
      message.success(`Test email sent to ${form.getFieldValue("recipients").join(", ")}: check your inbox (and spam)`);
    } catch (e) {
      message.error(e instanceof Error ? e.message : String(e), 8);
    } finally {
      setTesting(false);
      void history.reload();
    }
  };

  return (
    <>
      <Typography.Title level={4} style={{ marginTop: 0 }}>
        <MailOutlined /> Email alerts
      </Typography.Title>
      {loadError && <Alert type="error" showIcon message={`Can't reach trading-service: ${loadError}`} style={{ marginBottom: 16 }} />}
      <Row gutter={[16, 16]}>
        <Col xs={24} lg={10}>
          <Card size="small" title="What to email, and to whom">
            {cfg && !cfg.smtp_configured && <SmtpSetup />}
            {cfg?.smtp_configured && (
              <Typography.Paragraph style={{ fontSize: 12, color: MUTED }}>
                Sent through {cfg.smtp_server} as {cfg.sender}. Links in the emails open {cfg.dashboard_url} (DASHBOARD_URL).
              </Typography.Paragraph>
            )}
            <Form<EmailAlertsSave> form={form} layout="vertical" requiredMark={false} disabled={!cfg}>
              <Form.Item name="enabled" valuePropName="checked" style={{ marginBottom: 12 }}>
                <Switch checkedChildren="Alerts on" unCheckedChildren="Alerts off" />
              </Form.Item>
              <Form.Item
                name="recipients"
                label="Send to"
                tooltip="Up to 10 addresses. Type one and press Enter."
                rules={[
                  { required: !!enabled, message: "Add an address to send the alerts to" },
                  {
                    validator: (_, v?: string[]) => {
                      const bad = (v ?? []).filter((a) => !EMAIL.test(a));
                      return bad.length ? Promise.reject(new Error(`Not an email address: ${bad.join(", ")}`)) : Promise.resolve();
                    },
                  },
                ]}
              >
                <Select mode="tags" tokenSeparators={[",", " ", ";"]} placeholder="you@gmail.com" open={false} suffixIcon={null} />
              </Form.Item>
              <Form.Item name="categories" label="About">
                <Checkbox.Group style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                  {(cfg?.available ?? []).map((c) => (
                    <Checkbox key={c.key} value={c.key}>
                      <Tag color={CATEGORY_COLOR[c.key]} style={{ marginRight: 6 }}>{c.label}</Tag>
                      <span style={{ color: MUTED, fontSize: 12 }}>{c.description}</span>
                    </Checkbox>
                  ))}
                </Checkbox.Group>
              </Form.Item>
              <Space wrap>
                <Button type="primary" onClick={() => void save()} loading={saving}>Save</Button>
                <Tooltip title={cfg?.smtp_configured ? "Saves, then sends a test email to these addresses right away" : "Set up the mail server first"}>
                  <Button icon={<SendOutlined />} onClick={test} loading={testing} disabled={!cfg?.smtp_configured}>Send a test email</Button>
                </Tooltip>
              </Space>
            </Form>
            <Typography.Paragraph style={{ fontSize: 12, color: MUTED, marginTop: 16, marginBottom: 0 }}>
              Alerts that happen together (a stop-loss sale and its blacklisting) arrive as one email, within about 15 seconds.
              A failed send is tried again after 1, 5 and 30 minutes and 2 hours. For dip signals in the browser (sound, read
              aloud), use the bell at the top or the <Link to="/dip">Dip buyer</Link> page.
            </Typography.Paragraph>
          </Card>
        </Col>
        <Col xs={24} lg={14}>
          <Card size="small" title="Recent alerts" extra={<span style={{ color: MUTED, fontSize: 12 }}>newest first</span>}>
            {history.error && <Alert type="error" showIcon message={history.error} style={{ marginBottom: 12 }} />}
            <Table<EmailAlert>
              size="small"
              rowKey="id"
              dataSource={history.data ?? []}
              loading={history.loading && !history.data}
              pagination={{ pageSize: 15, hideOnSinglePage: true }}
              locale={{ emptyText: "No alerts yet. Turn them on, and the next buy, sale or stop-loss shows up here." }}
              expandable={{
                expandedRowRender: (n) => <pre style={{ margin: 0, whiteSpace: "pre-wrap", fontSize: 12 }}>{n.body}</pre>,
              }}
              columns={[
                { title: "When", dataIndex: "created_at", width: 150, render: (t: string) => <span style={{ fontSize: 12 }}>{localTime(t)}</span> },
                {
                  title: "Alert",
                  dataIndex: "subject",
                  render: (s: string, n) => (
                    <>
                      <Tag color={CATEGORY_COLOR[n.category]}>{n.category}</Tag>
                      {n.bot_id ? <Link to={`/trading/${n.bot_id}`}>{s}</Link> : s}
                    </>
                  ),
                },
                {
                  title: "Email",
                  dataIndex: "status",
                  width: 110,
                  render: (s: EmailAlert["status"], n) => (
                    <Tooltip
                      title={
                        n.error
                          ? `${n.error}${s === "pending" && n.next_attempt_at ? ` · next try ${localTime(n.next_attempt_at)}` : ""}`
                          : n.sent_at ? `Sent ${localTime(n.sent_at)}` : undefined
                      }
                    >
                      <Tag color={STATUS_COLOR[s]}>{s === "pending" && n.attempts ? `retry ${n.attempts}` : s}</Tag>
                    </Tooltip>
                  ),
                },
              ]}
            />
          </Card>
        </Col>
      </Row>
    </>
  );
}
