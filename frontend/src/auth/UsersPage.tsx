import { Alert, App as AntApp, Button, Card, Popconfirm, Space, Table, Tag, Tooltip, Typography } from "antd";
import type { ColumnsType } from "antd/es/table";
import { localTime, relative } from "../trading/format";
import { usePolling } from "../trading/usePolling";
import { authApi, notifyUsersChanged, type User } from "./api";
import { useAuth } from "./AuthContext";

const STATUS: Record<User["status"], { color: string; label: string }> = {
  pending: { color: "orange", label: "waiting for approval" },
  active: { color: "green", label: "active" },
  rejected: { color: "red", label: "rejected" },
  disabled: { color: "default", label: "disabled" },
};

/** Admins only: approve new sign-ups and manage who can sign in. */
export default function UsersPage() {
  const { user: me } = useAuth();
  const { message } = AntApp.useApp();
  const users = usePolling(authApi.listUsers, 30_000);
  const pending = (users.data ?? []).filter((u) => u.status === "pending").length;

  const act = async (done: string, call: () => Promise<unknown>) => {
    try {
      await call();
      message.success(done);
      users.reload();
      notifyUsersChanged();
    } catch (e) {
      message.error(e instanceof Error ? e.message : String(e));
    }
  };

  const actions = (u: User) => {
    if (u.id === me?.id) return <span style={{ color: "#8b98b5" }}>you</span>;
    const approve = (
      <Button size="small" type="primary"
        onClick={() => act(`${u.username} can sign in now`, () => authApi.updateUser(u.id, { status: "active" }))}>
        Approve
      </Button>
    );
    const remove = (
      <Popconfirm title={`Delete ${u.username}?`} description="The account is removed for good and the username becomes free again."
        okText="Delete" okButtonProps={{ danger: true }} onConfirm={() => act(`Deleted ${u.username}`, () => authApi.deleteUser(u.id))}>
        <Button size="small" type="text" danger>Delete</Button>
      </Popconfirm>
    );
    return (
      <Space size={4} wrap>
        {u.status === "pending" && (
          <>
            {approve}
            <Button size="small" danger
              onClick={() => act(`Rejected ${u.username}`, () => authApi.updateUser(u.id, { status: "rejected" }))}>
              Reject
            </Button>
          </>
        )}
        {u.status === "rejected" && approve}
        {u.status === "active" && (
          <>
            <Popconfirm title={`Disable ${u.username}?`} description="Signs them out everywhere right away. You can enable the account again later."
              okText="Disable" okButtonProps={{ danger: true }}
              onConfirm={() => act(`Disabled ${u.username}`, () => authApi.updateUser(u.id, { status: "disabled" }))}>
              <Button size="small">Disable</Button>
            </Popconfirm>
            <Button size="small"
              onClick={() => act(`${u.username} is ${u.role === "admin" ? "a regular user" : "an admin"} now`,
                () => authApi.updateUser(u.id, { role: u.role === "admin" ? "user" : "admin" }))}>
              {u.role === "admin" ? "Make regular user" : "Make admin"}
            </Button>
          </>
        )}
        {u.status === "disabled" && (
          <Button size="small" onClick={() => act(`Enabled ${u.username}`, () => authApi.updateUser(u.id, { status: "active" }))}>
            Enable
          </Button>
        )}
        {remove}
      </Space>
    );
  };

  const columns: ColumnsType<User> = [
    {
      title: "User",
      key: "user",
      render: (_, u) => (
        <Space direction="vertical" size={0}>
          <span style={{ fontWeight: 600 }}>{u.name}</span>
          <span style={{ color: "#8b98b5", fontSize: 12 }}>@{u.username}</span>
        </Space>
      ),
    },
    { title: "Role", dataIndex: "role", render: (r: User["role"]) => <Tag color={r === "admin" ? "gold" : "blue"}>{r}</Tag> },
    { title: "Status", dataIndex: "status", render: (s: User["status"]) => <Tag color={STATUS[s].color}>{STATUS[s].label}</Tag> },
    {
      title: "Signed up",
      dataIndex: "created_at",
      render: (v: string, u) => (
        <Tooltip title={u.approved_by ? `Approved by ${u.approved_by}, ${localTime(u.approved_at)}` : undefined}>
          {localTime(v)}
        </Tooltip>
      ),
    },
    { title: "Last sign-in", dataIndex: "last_login_at", render: (v: string | null) => (v ? relative(v) : "never") },
    { title: "", key: "actions", align: "right", render: (_, u) => actions(u) },
  ];

  return (
    <Card title={<Typography.Title level={4} style={{ margin: 0 }}>Users</Typography.Title>}
      extra={<span style={{ color: "#8b98b5" }}>New sign-ups can't sign in until you approve them</span>}>
      {users.error && <Alert type="error" showIcon message={users.error} style={{ marginBottom: 16 }} />}
      {pending > 0 && (
        <Alert type="warning" showIcon style={{ marginBottom: 16 }}
          message={`${pending} sign-up${pending === 1 ? " is" : "s are"} waiting for your approval`} />
      )}
      <Table rowKey="id" size="middle" columns={columns} dataSource={users.data ?? []} loading={users.loading}
        pagination={false} scroll={{ x: "max-content" }} />
    </Card>
  );
}
