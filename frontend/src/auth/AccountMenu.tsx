import { DownOutlined, KeyOutlined, LogoutOutlined, MailOutlined, UserOutlined } from "@ant-design/icons";
import { Alert, App as AntApp, Badge, Button, Dropdown, Form, Input, Modal, Tag } from "antd";
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { usePolling } from "../trading/usePolling";
import { authApi, onUsersChanged } from "./api";
import { useAuth } from "./AuthContext";

/** The header's "Users" link, with how many sign-ups wait for approval. Rendered for admins only. */
export function UsersNavLabel() {
  const pending = usePolling(authApi.listUsers, 60_000);
  useEffect(() => onUsersChanged(pending.reload), [pending.reload]);
  const count = (pending.data ?? []).filter((u) => u.status === "pending").length;
  return (
    <Badge count={count} size="small" offset={[8, -2]} title={`${count} waiting for approval`}>
      <span style={{ color: "inherit" }}>Users</span>
    </Badge>
  );
}

/** Top-right: who is signed in, change password, sign out. */
export function AccountMenu() {
  const { user, signOut } = useAuth();
  const [changing, setChanging] = useState(false);
  if (!user) return null;
  return (
    <>
      <Dropdown
        trigger={["click"]}
        placement="bottomRight"
        menu={{
          items: [
            {
              key: "who",
              disabled: true,
              label: (
                <span>
                  {user.name} <span style={{ opacity: 0.7 }}>@{user.username}</span>
                  {user.role === "admin" && <Tag color="gold" style={{ marginLeft: 8 }}>admin</Tag>}
                </span>
              ),
            },
            { type: "divider" },
            { key: "alerts", icon: <MailOutlined />, label: <Link to="/alerts">Email alerts</Link> },
            { key: "password", icon: <KeyOutlined />, label: "Change password", onClick: () => setChanging(true) },
            { key: "signout", icon: <LogoutOutlined />, label: "Sign out", onClick: () => void signOut() },
          ],
        }}
      >
        <Button type="text" icon={<UserOutlined />} style={{ color: "#8b98b5" }}>
          {user.username} <DownOutlined style={{ fontSize: 10 }} />
        </Button>
      </Dropdown>
      <ChangePasswordModal open={changing} onClose={() => setChanging(false)} />
    </>
  );
}

function ChangePasswordModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { message } = AntApp.useApp();
  const [form] = Form.useForm();
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (v: { current: string; next: string }) => {
    setBusy(true);
    setError(null);
    try {
      await authApi.changePassword(v.current, v.next);
      message.success("Password changed. Any other browser signed in as you was signed out.");
      onClose();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal title="Change password" open={open} onCancel={onClose} okText="Change password" confirmLoading={busy}
      onOk={() => form.submit()} afterClose={() => { form.resetFields(); setError(null); }}>
      {error && <Alert type="error" showIcon message={error} style={{ marginBottom: 16 }} />}
      <Form form={form} layout="vertical" requiredMark={false} onFinish={submit}>
        <Form.Item name="current" label="Current password" rules={[{ required: true, message: "Enter your current password" }]}>
          <Input.Password autoComplete="current-password" autoFocus />
        </Form.Item>
        <Form.Item name="next" label="New password" rules={[
          { required: true, message: "Choose a new password" },
          { min: 8, message: "At least 8 characters" },
          { max: 128 },
        ]}>
          <Input.Password autoComplete="new-password" />
        </Form.Item>
        <Form.Item name="confirm" label="New password again" dependencies={["next"]} rules={[
          { required: true, message: "Repeat the new password" },
          ({ getFieldValue }) => ({
            validator: (_, value) =>
              !value || value === getFieldValue("next") ? Promise.resolve() : Promise.reject(new Error("The passwords don't match")),
          }),
        ]}>
          <Input.Password autoComplete="new-password" />
        </Form.Item>
      </Form>
    </Modal>
  );
}
