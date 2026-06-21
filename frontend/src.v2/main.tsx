import React from "react";
import ReactDOM from "react-dom/client";
import { App } from "./App";
import { ChunkErrorBoundary } from "./shell/ChunkErrorBoundary";
import "./styles/design-tokens.css";  // 🎨 Codex design tokens
import "./styles/fonts.css";
import "./styles/tokens.css";
import "./reset.css";
import "./styles/components.css";     // 🎨 Reusable components
import "./styles/animations.css";
import "./styles/utilities.css";
import "./styles/z-index.css";
import "./styles/breakpoints.css";  // 🆕 Responsive breakpoints
import "./styles/scroll.css";       // 🆕 Scroll optimizations
import "./agent-loop/styles/agent-loop.css";
import "./composer/composer.css";   // 🎨 Composer styles
import "./styles/workbench-polish.css";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <ChunkErrorBoundary>
      <App />
    </ChunkErrorBoundary>
  </React.StrictMode>,
);
