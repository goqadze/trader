import { IdcardOutlined, LockOutlined, UserOutlined } from "@ant-design/icons";
import { Alert, Button, Card, Checkbox, Form, Input, Layout, Result, Typography } from "antd";
import { useState, type ReactNode } from "react";
import { Link, Navigate, useLocation, type Location } from "react-router-dom";
import { authApi, type User } from "./api";
import { useAuth } from "./AuthContext";

// The server's messages are lowercase fragments ("wrong username or password"); here each is a sentence
const errorText = (e: unknown) => {
  const msg = e instanceof Error ? e.message : String(e);
  return msg.charAt(0).toUpperCase() + msg.slice(1);
};

/** The centered card both pages sit in. */
function AuthCard({ title, children }: { title?: string; children: ReactNode }) {
  return (
    <Layout style={{ minHeight: "100vh", alignItems: "center", justifyContent: "center", padding: 16 }}>
      <Card style={{ width: "100%", maxWidth: 380 }}>
        <div style={{ fontWeight: 600, fontSize: 18, marginBottom: 12 }}>📈 Trading</div>
        {title && <Typography.Title level={4} style={{ marginTop: 0 }}>{title}</Typography.Title>}
        {children}
      </Card>
    </Layout>
  );
}

export function LoginPage() {
  const { user, sessionEnded, signIn } = useAuth();
  const from = (useLocation().state as { from?: Location } | null)?.from;
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // Signed in (now, or from an earlier visit): back to the page that sent us here
  if (user) return <Navigate to={from ? from.pathname + from.search + from.hash : "/"} replace />;

  const submit = async (v: { username: string; password: string; remember: boolean }) => {
    setBusy(true);
    setError(null);
    try {
      await signIn(v.username, v.password, v.remember);
    } catch (e) {
      setError(errorText(e));
      setBusy(false);
    }
  };

  return (
    <AuthCard title="Sign in">
      {error ? (
        <Alert type="error" showIcon message={error} style={{ marginBottom: 16 }} />
      ) : sessionEnded ? (
        <Alert type="info" showIcon message="Your session ended. Sign in again." style={{ marginBottom: 16 }} />
      ) : null}
      <Form layout="vertical" requiredMark={false} initialValues={{ remember: true }} onFinish={submit}>
        <Form.Item name="username" label="Username" rules={[{ required: true, message: "Enter your username" }]}>
          <Input prefix={<UserOutlined />} autoComplete="username" autoFocus />
        </Form.Item>
        <Form.Item name="password" label="Password" rules={[{ required: true, message: "Enter your password" }]}>
          <Input.Password prefix={<LockOutlined />} autoComplete="current-password" />
        </Form.Item>
        <Form.Item name="remember" valuePropName="checked"
          extra="Stays signed in on this browser; each visit extends it. Untick on a shared computer.">
          <Checkbox>Keep me signed in</Checkbox>
        </Form.Item>
        <Button type="primary" htmlType="submit" block loading={busy}>Sign in</Button>
      </Form>
      <div style={{ marginTop: 16, color: "#8b98b5" }}>
        No account yet? <Link to="/signup">Sign up</Link>
      </div>
    </AuthCard>
  );
}

export function SignupPage() {
  const { user } = useAuth();
  const [created, setCreated] = useState<User | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  if (user) return <Navigate to="/" replace />;

  if (created) {
    return (
      <AuthCard>
        <Result
          status="success"
          title="Thanks for signing up"
          subTitle={`An admin needs to approve "${created.username}" before you can sign in.`}
          extra={<Link to="/login"><Button type="primary">Back to sign in</Button></Link>}
        />
      </AuthCard>
    );
  }

  const submit = async (v: { username: string; name: string; password: string }) => {
    setBusy(true);
    setError(null);
    try {
      setCreated(await authApi.signup({ username: v.username, name: v.name, password: v.password }));
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <AuthCard title="Create an account">
      <Typography.Paragraph type="secondary">An admin approves each new account before it can sign in.</Typography.Paragraph>
      {error && <Alert type="error" showIcon message={error} style={{ marginBottom: 16 }} />}
      <Form layout="vertical" requiredMark={false} onFinish={submit}>
        <Form.Item name="username" label="Username" rules={[
          { required: true, message: "Pick a username" },
          { pattern: /^[A-Za-z0-9._-]{3,32}$/, message: "3–32 letters, digits, dots, dashes or underscores" },
        ]}>
          <Input prefix={<UserOutlined />} autoComplete="username" autoFocus />
        </Form.Item>
        <Form.Item name="name" label="Your name" extra="So the admin knows who is asking."
          rules={[{ required: true, whitespace: true, message: "Enter your name" }, { max: 80 }]}>
          <Input prefix={<IdcardOutlined />} autoComplete="name" />
        </Form.Item>
        <Form.Item name="password" label="Password" rules={[
          { required: true, message: "Choose a password" },
          { min: 8, message: "At least 8 characters" },
          { max: 128 },
        ]}>
          <Input.Password prefix={<LockOutlined />} autoComplete="new-password" />
        </Form.Item>
        <Form.Item name="confirm" label="Password again" dependencies={["password"]} rules={[
          { required: true, message: "Repeat the password" },
          ({ getFieldValue }) => ({
            validator: (_, value) =>
              !value || value === getFieldValue("password") ? Promise.resolve() : Promise.reject(new Error("The passwords don't match")),
          }),
        ]}>
          <Input.Password prefix={<LockOutlined />} autoComplete="new-password" />
        </Form.Item>
        <Button type="primary" htmlType="submit" block loading={busy}>Sign up</Button>
      </Form>
      <div style={{ marginTop: 16, color: "#8b98b5" }}>
        Already have an account? <Link to="/login">Sign in</Link>
      </div>
    </AuthCard>
  );
}
