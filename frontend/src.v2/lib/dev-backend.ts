/**
 * dev-backend.ts — Resolve the best backend origin for Vite dev-server proxy.
 *
 * Used ONLY by vite.config.ts (Node-side), never bundled into the client.
 */

interface ResolveOptions {
  candidates: (string | undefined)[];
}

interface ResolvedOrigins {
  apiBaseUrl: string;
  wsBaseUrl: string;
}

const isTruthy = (v: string | undefined | null): v is string =>
  typeof v === "string" && v.length > 0 && v !== "undefined" && v !== "null";

const toWs = (http: string): string => http.replace(/^http/i, "ws");

export async function resolveMiniCodeDevBackendOrigins(
  opts: ResolveOptions,
): Promise<ResolvedOrigins> {
  const first = opts.candidates.find(isTruthy) ?? "http://127.0.0.1:8000";
  const apiBaseUrl = first.replace(/\/+$/, "");
  const wsBaseUrl = toWs(apiBaseUrl);
  return { apiBaseUrl, wsBaseUrl };
}
