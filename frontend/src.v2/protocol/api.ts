import { safeURL } from "../lib/safe-parse";

const RUNTIME = (globalThis as unknown as {
  __MINICODE_RUNTIME__?: { apiBaseUrl?: string; wsBaseUrl?: string; runtimeToken?: string };
}).__MINICODE_RUNTIME__;

const ENV = (import.meta as unknown as {
  env?: {
  VITE_API_BASE_URL?: string;
  VITE_WS_BASE_URL?: string;
  DEV?: boolean | string;
  };
}).env ?? {};

const envValue = (key: "VITE_API_BASE_URL" | "VITE_WS_BASE_URL"): string | undefined =>
  ENV[key] ??
  (globalThis as typeof globalThis & { process?: { env?: Record<string, string | undefined> } })
    .process?.env?.[key];

const trimBase = (value: string | undefined): string | undefined => {
  const trimmed = value?.trim().replace(/\/+$/, "");
  return trimmed || undefined;
};

interface RuntimeLocation {
  protocol?: string;
  host?: string;
}

const currentLocation = (): RuntimeLocation | undefined => {
  if (typeof window !== "undefined") return window.location;
  return (globalThis as typeof globalThis & { location?: RuntimeLocation }).location;
};

const currentHttpOrigin = (): string => {
  const location = currentLocation();
  if (location?.protocol && location.host) return `${location.protocol}//${location.host}`;
  return "http://127.0.0.1:5173";
};

const isViteDevServer = (): boolean => ENV.DEV === true || ENV.DEV === "true";

const currentWsOrigin = (): string => {
  const location = currentLocation();
  const proto = location?.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${location?.host ?? "127.0.0.1:5173"}`;
};

/*
 * VITE_DEV_BACKEND_ORIGIN and related env vars configure Vite's dev proxy.
 * Browser code stays same-origin in dev so API/WS traffic goes through that
 * proxy instead of crossing CORS/runtime-token boundaries.
 */
export const apiBase = (): string =>
  trimBase(RUNTIME?.apiBaseUrl) ??
  (isViteDevServer()
    ? currentHttpOrigin()
    : trimBase(envValue("VITE_API_BASE_URL"))) ??
  currentHttpOrigin();

export const wsBase = (): string => {
  const runtimeWs = trimBase(RUNTIME?.wsBaseUrl);
  if (runtimeWs) return runtimeWs;
  if (isViteDevServer()) return currentWsOrigin();
  const explicit = trimBase(envValue("VITE_WS_BASE_URL"));
  if (explicit) return explicit;
  const api = trimBase(envValue("VITE_API_BASE_URL"));
  if (api) return api.replace(/^http/i, "ws");
  return currentWsOrigin();
};

export const runtimeToken = (): string => RUNTIME?.runtimeToken?.trim() ?? "";

export const authHeaders = (headers?: HeadersInit): HeadersInit => {
  const next = new Headers(headers);
  const token = runtimeToken();
  if (token) next.set("X-MiniCode-Token", token);
  return next;
};

export const withRuntimeToken = (url: string): string => {
  const token = runtimeToken();
  if (!token) return url;
  const parsed = safeURL(url, currentHttpOrigin());
  if (!parsed) return url; // Invalid URL, return as-is
  parsed.searchParams.set("minicode_token", token);
  const isAbsolute = /^[a-z][a-z\d+\-.]*:/i.test(url);
  return isAbsolute ? parsed.toString() : `${parsed.pathname}${parsed.search}${parsed.hash}`;
};

export const wsUrl = (path: string, params?: Record<string, string | undefined>): string => {
  const normalizedPath = path.startsWith("/") ? path : `/${path}`;
  const url = safeURL(`${wsBase()}${normalizedPath}`);
  if (!url) {
    // Fallback to a basic URL if construction fails
    console.error(`Failed to construct WebSocket URL for path: ${path}`);
    return `${wsBase()}${normalizedPath}`;
  }
  Object.entries(params ?? {}).forEach(([key, value]) => {
    if (value != null && value !== "") url.searchParams.set(key, value);
  });
  const token = runtimeToken();
  if (token) url.searchParams.set("minicode_token", token);
  return url.toString();
};

export const v1 = (path: string): string =>
  `${apiBase()}/api/v1${path.startsWith("/") ? path : `/${path}`}`;

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

const errorMessageFromResponseText = (text: string, fallback: string): string => {
  const trimmed = text.trim();
  if (!trimmed) return fallback;
  try {
    const parsed = JSON.parse(trimmed) as unknown;
    if (parsed && typeof parsed === "object") {
      const detail = (parsed as { detail?: unknown; message?: unknown; error?: unknown }).detail
        ?? (parsed as { message?: unknown }).message
        ?? (parsed as { error?: unknown }).error;
      if (Array.isArray(detail)) {
        return detail.map((item) => {
          if (item && typeof item === "object" && "msg" in item) return String((item as { msg?: unknown }).msg);
          return String(item);
        }).join("; ");
      }
      if (detail != null) return String(detail);
    }
  } catch {
    /* not json */
  }
  return trimmed;
};

export const apiFetch = async <T = unknown>(
  path: string,
  init?: RequestInit,
): Promise<T> => {
  const res = await fetch(v1(path), {
    ...init,
    headers: authHeaders({ "content-type": "application/json", ...(init?.headers ?? {}) }),
  });
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new ApiError(res.status, errorMessageFromResponseText(text, res.statusText));
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
};

export interface StatusResponse {
  ok: boolean;
  [k: string]: unknown;
}
export const fetchStatus = (): Promise<StatusResponse> => apiFetch("/status");
export const fetchGuidelines = (): Promise<unknown> => apiFetch("/guidelines");
export const fetchLLMSettings = async (): Promise<unknown> => {
  const res = await fetch(`${apiBase()}/api/llm/settings`, {
    cache: "no-store",
    headers: authHeaders(),
  });
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new ApiError(res.status, errorMessageFromResponseText(text, res.statusText));
  }
  return res.json();
};

export interface UploadResponse {
  file_name: string;
  doc_id: string;
  artifact_id: string;
  indexed_chunks: number;
  attachment: Record<string, unknown>;
}

export const uploadAttachment = async (
  sessionId: string,
  file: File,
): Promise<UploadResponse> => {
  const form = new FormData();
  form.append("file", file);
  const res = await fetch(
    `${apiBase()}/api/uploads?session_id=${encodeURIComponent(sessionId)}`,
    { method: "POST", body: form, headers: authHeaders() },
  );
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new ApiError(res.status, errorMessageFromResponseText(text, res.statusText));
  }
  return (await res.json()) as UploadResponse;
};
