import React from "react";
import ReactDOM from "react-dom/client";
import { App } from "./App";
import { ChunkErrorBoundary } from "./shell/ChunkErrorBoundary";
import "./styles/fonts.css";
import "./styles/tokens.css";
import "./reset.css";
import "./styles/animations.css";
import "./styles/utilities.css";
import "./styles/z-index.css";
import "./styles/breakpoints.css";  // 🆕 Responsive breakpoints
import "./styles/scroll.css";       // 🆕 Scroll optimizations

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <ChunkErrorBoundary>
      <App />
    </ChunkErrorBoundary>
  </React.StrictMode>,
);
