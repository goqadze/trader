import { ExportOutlined } from "@ant-design/icons";

// The guide is a self-contained static page (frontend/public/guides/), so it keeps its own styles
// and can also be opened on its own. Shown in an iframe to keep its CSS away from antd's.
export const GUIDE_URL = "/guides/trading-bot-lifecycle.html";

/** In-app view of the Trading Bot Lifecycle cheat sheet. */
export default function GuidePage() {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8, height: "calc(100vh - 96px)" }}>
      <a href={GUIDE_URL} target="_blank" rel="noreferrer" style={{ alignSelf: "flex-end", color: "#8b98b5", fontSize: 13 }}>
        Open in its own tab <ExportOutlined />
      </a>
      <iframe
        src={GUIDE_URL}
        title="Trading Bot Lifecycle"
        style={{ flex: 1, width: "100%", border: "1px solid #2a3550", borderRadius: 8, background: "#0d1220" }}
      />
    </div>
  );
}
