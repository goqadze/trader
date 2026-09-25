import React from "react";
import ReactDOM from "react-dom/client";
import "antd/dist/reset.css";
import App from "./App";

// The app never belongs inside a frame. It lands in one when a guide's iframe outlives the session: nginx
// sends the guide's request to /login, which would show a second copy of the app inside the guide. Load
// that page in the whole window instead.
const framed = window.top !== window.self;
if (framed && window.top) {
  window.top.location.href = window.location.href;
} else {
  ReactDOM.createRoot(document.getElementById("root") as HTMLElement).render(
    <React.StrictMode>
      <App />
    </React.StrictMode>
  );
}
