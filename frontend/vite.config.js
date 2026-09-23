import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
// In docker the app is built and served by nginx, which proxies the APIs (see nginx.conf).
// This proxy block only matters for local `npm run dev`, where backtest-service is on host
// port 8001 and trading-service on 8002.
export default defineConfig({
    plugins: [react()],
    server: {
        host: true,
        proxy: {
            "/runs": "http://localhost:8001",
            "/health": "http://localhost:8001",
            "/ws": { target: "ws://localhost:8001", ws: true },
            "/api/trading": {
                target: "http://localhost:8002",
                rewrite: function (path) { return path.replace(/^\/api\/trading/, ""); },
            },
        },
    },
});
