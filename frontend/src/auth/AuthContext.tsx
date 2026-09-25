import { Spin } from "antd";
import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { Navigate, useLocation } from "react-router-dom";
import { onUnauthorized } from "../http";
import { authApi, type User } from "./api";

interface Auth {
  user: User | null; // null = signed out
  sessionEnded: boolean; // was signed in, then an API call answered 401 (expired, disabled, password changed elsewhere)
  signIn: (username: string, password: string, remember: boolean) => Promise<void>;
  signOut: () => Promise<void>;
}

const AuthContext = createContext<Auth | null>(null);

/** Knows who is signed in. The session itself is an HttpOnly cookie the browser keeps (and sends) by itself. */
export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [checking, setChecking] = useState(true);
  const [sessionEnded, setSessionEnded] = useState(false);
  const userRef = useRef(user);
  userRef.current = user;

  useEffect(() => {
    // Still signed in from an earlier visit? Asking also renews the session.
    authApi.me().then(setUser, () => setUser(null)).finally(() => setChecking(false));
    return onUnauthorized(() => {
      if (userRef.current) setSessionEnded(true);
      setUser(null);
    });
  }, []);

  const signIn = useCallback(async (username: string, password: string, remember: boolean) => {
    setUser(await authApi.login(username, password, remember));
    setSessionEnded(false);
  }, []);

  const signOut = useCallback(async () => {
    try {
      await authApi.logout();
    } finally {
      setUser(null);
    }
  }, []);

  if (checking) return <Spin fullscreen />;
  return <AuthContext.Provider value={{ user, sessionEnded, signIn, signOut }}>{children}</AuthContext.Provider>;
}

export function useAuth(): Auth {
  const auth = useContext(AuthContext);
  if (!auth) throw new Error("useAuth must be used inside <AuthProvider>");
  return auth;
}

/** Pages behind the sign-in. Signed out, go to /login, and back here afterwards. */
export function RequireAuth({ children, admin = false }: { children: ReactNode; admin?: boolean }) {
  const { user } = useAuth();
  const location = useLocation();
  if (!user) return <Navigate to="/login" replace state={{ from: location }} />;
  if (admin && user.role !== "admin") return <Navigate to="/" replace />;
  return <>{children}</>;
}
