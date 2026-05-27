import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";
import path from "path";
import { resolveMiniCodeDevBackendOrigins } from "./src.v2/lib/dev-backend";

const EXPECTED_PROXY_DISCONNECT_CODES = new Set(["ECONNRESET", "ECONNABORTED", "EPIPE"]);

const isExpectedProxyDisconnect = (error: unknown): boolean => {
  const err = error as { code?: unknown; message?: unknown };
  const code = typeof err.code === "string" ? err.code : "";
  const message = typeof err.message === "string" ? err.message.toLowerCase() : "";
  return (
    EXPECTED_PROXY_DISCONNECT_CODES.has(code)
    || message.includes("socket hang up")
    || message.includes("aborted")
    || message.includes("premature close")
  );
};

const configureQuietProxy = (label: string) => (proxy: { on: (event: "error", handler: (error: unknown) => void) => void }) => {
  proxy.on("error", (error) => {
    if (isExpectedProxyDisconnect(error)) return;
    console.warn(`[MiniCode] Vite proxy error (${label}):`, error);
  });
};

export default defineConfig(async ({ command, mode }) => {
  // Always resolve env files from the frontend project directory.
  const env = loadEnv(mode, __dirname, "");
  const isVitest = process.env.VITEST === "true" || process.env.NODE_ENV === "test";
  const isPreview = process.argv.includes("preview");
  const backendCandidates = [
    process.env.MINICODE_API_BASE_URL,
    process.env.MINICODE_WS_BASE_URL,
    process.env.VITE_DEV_BACKEND_ORIGIN,
    process.env.VITE_API_BASE_URL,
    process.env.VITE_WS_BASE_URL,
    env.MINICODE_API_BASE_URL,
    env.MINICODE_WS_BASE_URL,
    env.VITE_DEV_BACKEND_ORIGIN,
    env.VITE_API_BASE_URL,
    env.VITE_WS_BASE_URL,
  ];
  const resolvedBackend =
    command === "serve" && !isVitest && !isPreview
      ? await resolveMiniCodeDevBackendOrigins({ candidates: backendCandidates })
      : (() => {
          const apiBaseUrl =
            process.env.VITE_API_BASE_URL ||
            env.VITE_API_BASE_URL ||
            process.env.VITE_DEV_BACKEND_ORIGIN ||
            env.VITE_DEV_BACKEND_ORIGIN ||
            "http://127.0.0.1:8000";
          const wsBaseUrl =
            process.env.VITE_WS_BASE_URL ||
            env.VITE_WS_BASE_URL ||
            apiBaseUrl.replace(/^http/i, "ws");
          return { apiBaseUrl, wsBaseUrl };
        })();
  const backendOrigin = resolvedBackend.apiBaseUrl;
  const wsOrigin = resolvedBackend.wsBaseUrl;
  const wsProxyTarget = wsOrigin.replace(/^ws/i, "http");
  if (command === "serve" && !isVitest && !isPreview) {
    console.info(`[MiniCode] Vite dev backend: ${backendOrigin} (${wsOrigin})`);
  }
  // Keep every production build file:// safe for the Electron shell. The env
  // flag stays for compatibility with existing desktop scripts.
  const useRelativeBase =
    process.env.MINICODE_VITE_RELATIVE_BASE === "1" || env.MINICODE_VITE_RELATIVE_BASE === "1";

  return {
    base: useRelativeBase ? "./" : "./",
    plugins: [react()],
    test: {
      exclude: ["tests/**", "node_modules/**"],
    },
    resolve: {
      alias: {
        "@": path.resolve(__dirname, "./src.v2"),
      },
    },
    server: {
      port: 5173,
      proxy: {
        "/ws": {
          target: wsProxyTarget,
          ws: true,
          changeOrigin: true,
          configure: configureQuietProxy("ws"),
        },
        "/api": {
          target: backendOrigin,
          changeOrigin: true,
          configure: configureQuietProxy("api"),
        },
        "/health": {
          target: backendOrigin,
          changeOrigin: true,
          configure: configureQuietProxy("health"),
        },
      },
    },
    build: {
      outDir: "dist",
      emptyOutDir: true,
      chunkSizeWarningLimit: 5000,
      rollupOptions: {
        output: {
          manualChunks(id) {
            if (id.includes("@xterm")) return "terminal";
            if (id.includes("@monaco-editor/react") || id.includes("monaco-editor")) return "editor";
            if (id.includes("react-syntax-highlighter")) return "syntax-highlighter";
            if (id.includes("react-markdown") || id.includes("remark-gfm") || id.includes("unified") || id.includes("remark-") || id.includes("rehype-") || id.includes("mdast") || id.includes("hast") || id.includes("micromark")) return "markdown";
            if (id.includes("@lobehub/icons-static-svg")) return "provider-icons";
            if (id.includes("lucide-react")) return "ui-icons";
            if (id.includes("node_modules/react-dom")) return "react-vendor";
            if (id.includes("node_modules/react/") || id.includes("node_modules/scheduler")) return "react-vendor";
            if (id.includes("node_modules/zustand") || id.includes("node_modules/immer")) return "state-vendor";
            return undefined;
          },
        },
      },
    },
  };
});
