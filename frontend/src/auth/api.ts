// Client for sign-in and accounts: trading-service's /auth/* (nginx and the Vite dev proxy map /api/auth/* to it).

import { request } from "../http";

export type Role = "admin" | "user";
// pending: signed up, waiting for an admin · active: can sign in · rejected / disabled: can't
export type UserStatus = "pending" | "active" | "rejected" | "disabled";

export interface User {
  id: number;
  username: string;
  name: string;
  role: Role;
  status: UserStatus;
  created_at: string;
  approved_at: string | null;
  approved_by: string | null;
  last_login_at: string | null;
}

const BASE = "/api/auth";
// A 401 is part of these calls' normal answers (wrong password, not signed in yet), not "your session ended"
const quiet = { signedOutOk: true };

export const authApi = {
  me: () => request<User>("GET", `${BASE}/me`, undefined, quiet),
  login: (username: string, password: string, remember: boolean) =>
    request<User>("POST", `${BASE}/login`, { username, password, remember }, quiet),
  signup: (body: { username: string; name: string; password: string }) => request<User>("POST", `${BASE}/signup`, body, quiet),
  logout: () => request<void>("POST", `${BASE}/logout`, undefined, quiet),
  changePassword: (current_password: string, new_password: string) =>
    request<void>("POST", `${BASE}/password`, { current_password, new_password }),
  // Admins only
  listUsers: () => request<User[]>("GET", `${BASE}/users`),
  updateUser: (id: number, body: { status?: Exclude<UserStatus, "pending">; role?: Role }) =>
    request<User>("PATCH", `${BASE}/users/${id}`, body),
  deleteUser: (id: number) => request<void>("DELETE", `${BASE}/users/${id}`),
};

// The Users page tells the header's "waiting for approval" badge to refresh right after a change
const USERS_CHANGED = "auth:users-changed";
export const notifyUsersChanged = () => window.dispatchEvent(new Event(USERS_CHANGED));
export function onUsersChanged(fn: () => void): () => void {
  window.addEventListener(USERS_CHANGED, fn);
  return () => window.removeEventListener(USERS_CHANGED, fn);
}
