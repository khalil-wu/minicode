import React from "react";
import ReactDOM from "react-dom/client";
import { App } from "./App";
import { ChunkErrorBoundary } from "./shell/ChunkErrorBoundary";
import { startUpdateActivityMirror } from "./desktop/updateActivityMirror";
import "./styles/fonts.css";
import "./styles/tokens.css";
import "./reset.css";
import "./styles/components.css";     // 🎨 Reusable components
import "./styles/animations.css";
import "./styles/utilities.css";
import "./styles/z-index.css";
import "./components/Tooltip.css";
import "./styles/breakpoints.css";  // 🆕 Responsive breakpoints
import "./agent-loop/styles/agent-loop.css";  // Agent-loop owner
import "./composer/composer.css";               // Composer owner
import "./shell/shell.css";                     // Shell/header/sidebar/layout owner
import "./styles/ui-polish.css";                // Canonical final typography and surface layer

startUpdateActivityMirror();

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <ChunkErrorBoundary>
      <App />
    </ChunkErrorBoundary>
  </React.StrictMode>,
);
