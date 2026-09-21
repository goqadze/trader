import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
// In docker the app is built and served by nginx, which proxies the API + WebSocket
// to backtest-service. This proxy block only matters for local `npm run dev`, where
// backtest-service is reachable on host port 8001.
export default defineConfig({
    plugins: [react()],
    server: {
        host: true,
        proxy: {
            "/runs": "http://localhost:8001",
            "/health": "http://localhost:8001",
            "/ws": { target: "ws://localhost:8001", ws: true },
        },
    },
});
