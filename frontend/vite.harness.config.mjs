// UI harness: the dashboard in a Vite dev server with a fake signed-in admin, for checking a change in a browser
// against real data without a password (Claude's ui-check skill, .claude/skills/ui-check/SKILL.md). Every write to
// trading-service (anything but GET) is refused, so no click can pause, edit or trade a bot. Not part of the build.
//
//   vite --config vite.harness.config.mjs                → :5179, the Mac's trading-service (localhost:8002) + backtests
//   vite --config vite.harness.config.mjs --mode server  → :5178, the server's, through an ssh tunnel on localhost:8098
//
// HARNESS_TARGET=http://localhost:8099 HARNESS_WRITES=1 (Mac mode only) lets writes through, for a throwaway DB copy.
import { fileURLToPath } from "node:url";
import react from "@vitejs/plugin-react";

const ADMIN = { id: 1, username: "harness", role: "admin", status: "active", created_at: "2026-01-01T00:00:00Z" };

export default ({ mode }) => {
  const server = mode === "server";
  const target = process.env.HARNESS_TARGET ?? (server ? "http://localhost:8098" : "http://localhost:8002");
  const writes = !server && process.env.HARNESS_WRITES === "1";
  const json = (res, status, body) => {
    res.statusCode = status;
    res.setHeader("Content-Type", "application/json");
    res.end(JSON.stringify(body));
  };
  const fakeAuth = {
    name: "harness-auth",
    configureServer(dev) {
      dev.middlewares.use((req, res, next) => {
        if (req.url.startsWith("/api/auth/me")) return json(res, 200, ADMIN);
        if (req.url.startsWith("/api/auth/users")) return json(res, 200, []);
        if (req.url.startsWith("/api/trading") && req.method !== "GET" && !writes) {
          return json(res, 403, { detail: "The UI harness is read-only (HARNESS_WRITES=1 for a throwaway DB copy)" });
        }
        next();
      });
    },
  };
  const backtests = server ? {} : {
    "/runs": "http://localhost:8001",
    "/scans": "http://localhost:8001",
    "/ws": { target: "ws://localhost:8001", ws: true },
  };
  return {
    root: fileURLToPath(new URL(".", import.meta.url)),
    plugins: [react(), fakeAuth],
    server: {
      host: "127.0.0.1",
      port: server ? 5178 : 5179,
      strictPort: true,
      proxy: {
        ...backtests,
        "/api/trading": { target, rewrite: (path) => path.replace(/^\/api\/trading/, "") },
      },
    },
  };
};
