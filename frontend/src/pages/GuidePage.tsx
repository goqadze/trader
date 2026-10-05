import { ExportOutlined } from "@ant-design/icons";
import { useLocation } from "react-router-dom";

// Each guide is a self-contained static page (frontend/public/guides/), so it keeps its own styles
// and can also be opened on its own. Shown in an iframe to keep its CSS away from antd's.
export const GUIDES = {
  lifecycle: { url: "/guides/trading-bot-lifecycle.html", title: "Trading Bot Lifecycle" },
  strategies: { url: "/guides/trading-strategies.html", title: "Trading Strategies" },
  architecture: { url: "/guides/architecture.html", title: "Architecture & Stack" },
} as const;

/** In-app view of one guide. A #hash in the app URL (e.g. /guide/strategies#breakout) jumps to that section. */
export default function GuidePage({ guide }: { guide: keyof typeof GUIDES }) {
  const { url, title } = GUIDES[guide];
  const { hash } = useLocation();
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8, height: "calc(100vh - 96px)" }}>
      <a href={url + hash} target="_blank" rel="noreferrer" style={{ alignSelf: "flex-end", color: "#8b98b5", fontSize: 13 }}>
        Open in its own tab <ExportOutlined />
      </a>
      <iframe
        key={url}
        src={url + hash}
        title={title}
        style={{ flex: 1, width: "100%", border: "1px solid #2a3550", borderRadius: 8, background: "#0d1220" }}
      />
    </div>
  );
}
