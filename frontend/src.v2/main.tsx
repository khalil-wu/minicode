import React from "react";
import ReactDOM from "react-dom/client";
import { App } from "./App";
import { ChunkErrorBoundary } from "./shell/ChunkErrorBoundary";
import "./styles/fonts.css";
import "./styles/tokens.css";
import "./reset.css";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <ChunkErrorBoundary>
      <App />
    </ChunkErrorBoundary>
  </React.StrictMode>,
);
