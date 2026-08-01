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
      testTimeout: 10_000,
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
      chunkSizeWarningLimit: 1000,
      modulePreload: {
        resolveDependencies(_filename, dependencies) {
          return dependencies.filter((dependency) => (
            !dependency.includes("/monaco-")
            && !dependency.includes("/editor.worker-")
          ));
        },
      },
      rollupOptions: {
        output: {
          manualChunks(id) {
            const normalized = id.replace(/\\/g, "/");
            if (!normalized.includes("/node_modules/")) return undefined;
            if (normalized.includes("/@monaco-editor/react/")) return "monaco-react";
            if (normalized.includes("/monaco-editor/esm/vs/basic-languages/")) return "monaco-languages";
            if (normalized.includes("/monaco-editor/esm/vs/editor/contrib/")) {
              const feature = normalized.split("/monaco-editor/esm/vs/editor/contrib/")[1]?.split("/")[0] || "misc";
              return `monaco-contrib-${feature}`;
            }
            if (normalized.includes("/monaco-editor/esm/vs/editor/browser/")) return "monaco-editor-ui";
            if (normalized.includes("/monaco-editor/esm/vs/editor/common/")) return "monaco-editor-core";
            if (normalized.includes("/monaco-editor/esm/vs/platform/")) return "monaco-platform";
            if (normalized.includes("/monaco-editor/esm/vs/base/")) return "monaco-base";
            if (
              normalized.includes("/react-markdown/")
              || normalized.includes("/remark-")
              || normalized.includes("/rehype-")
              || normalized.includes("/unified/")
              || normalized.includes("/micromark")
              || normalized.includes("/mdast-")
              || normalized.includes("/hast-")
              || normalized.includes("/katex/")
            ) {
              return "markdown-vendor";
            }
            if (
              normalized.includes("/@iconify/")
              || normalized.includes("/@iconify-icons/")
              || normalized.includes("/@lobehub/icons/")
            ) {
              return "icons-vendor";
            }
            if (normalized.includes("/@xterm/")) return "terminal-vendor";
            return undefined;
          },
        },
      },
    },
  };
});
