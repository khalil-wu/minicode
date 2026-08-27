import { hmac } from "@noble/hashes/hmac.js";
import { sha256 } from "@noble/hashes/sha2.js";
import { safeJsonParse, safeURL } from "../lib/safe-parse";

const RUNTIME = (globalThis as unknown as {
  __MINICODE_RUNTIME__?: { apiBaseUrl?: string; wsBaseUrl?: string; runtimeToken?: string };
}).__MINICODE_RUNTIME__;

const ENV = (import.meta as unknown as {
  env?: {
  VITE_API_BASE_URL?: string;
  VITE_WS_BASE_URL?: string;
  VITE_DEV_BACKEND_ORIGIN?: string;
  DEV?: boolean | string;
  };
}).env ?? {};

const envValue = (key: "VITE_API_BASE_URL" | "VITE_WS_BASE_URL" | "VITE_DEV_BACKEND_ORIGIN"): string | undefined =>
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

const sameOrigin = (left: string | undefined, right: string | undefined): boolean => {
  const leftUrl = safeURL(left || "");
  const rightUrl = safeURL(right || "");
  return Boolean(leftUrl && rightUrl && leftUrl.protocol === rightUrl.protocol && leftUrl.host === rightUrl.host);
};

const devProxyApiOrigin = (): string =>
  trimBase(envValue("VITE_DEV_BACKEND_ORIGIN")) ??
  trimBase(envValue("VITE_API_BASE_URL")) ??
  "http://127.0.0.1:8000";

const devProxyWsOrigin = (): string =>
  trimBase(envValue("VITE_WS_BASE_URL")) ??
  devProxyApiOrigin().replace(/^http/i, "ws");

const shouldUseDevProxyForRuntimeApi = (runtimeApi: string | undefined): boolean =>
  Boolean(runtimeApi && isViteDevServer() && sameOrigin(runtimeApi, devProxyApiOrigin()));

const shouldUseDevProxyForRuntimeWs = (runtimeWs: string | undefined): boolean =>
  Boolean(runtimeWs && isViteDevServer() && sameOrigin(runtimeWs, devProxyWsOrigin()));

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
  (() => {
    const runtimeApi = trimBase(RUNTIME?.apiBaseUrl);
    if (runtimeApi && !shouldUseDevProxyForRuntimeApi(runtimeApi)) return runtimeApi;
    if (isViteDevServer()) return currentHttpOrigin();
    return runtimeApi ?? trimBase(envValue("VITE_API_BASE_URL")) ?? currentHttpOrigin();
  })();

export const wsBase = (): string => {
  const runtimeWs = trimBase(RUNTIME?.wsBaseUrl);
  if (runtimeWs && !shouldUseDevProxyForRuntimeWs(runtimeWs)) return runtimeWs;
  if (isViteDevServer()) return currentWsOrigin();
  const explicit = trimBase(envValue("VITE_WS_BASE_URL"));
  if (explicit) return explicit;
  const api = trimBase(envValue("VITE_API_BASE_URL"));
  if (api) return api.replace(/^http/i, "ws");
  return currentWsOrigin();
};

export const runtimeToken = (): string => RUNTIME?.runtimeToken?.trim() ?? "";

const base64UrlEncode = (value: string): string => {
  if (typeof TextEncoder !== "undefined" && typeof btoa !== "undefined") {
    const bytes = new TextEncoder().encode(value);
    let binary = "";
    bytes.forEach((byte) => {
      binary += String.fromCharCode(byte);
    });
    return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
  }
  if (typeof btoa !== "undefined") {
    return btoa(value).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
  }
  return encodeURIComponent(value).replace(/%/g, ".");
};

export const wsProtocols = (): string[] | undefined => {
  const token = runtimeToken();
  if (!token) return undefined;
  return ["minicode", `minicode-token.${base64UrlEncode(token)}`];
};

export const authHeaders = (headers?: HeadersInit): HeadersInit => {
  const next = new Headers(headers);
  const token = runtimeToken();
  if (token) next.set("X-MiniCode-Token", token);
  return next;
};

const toUtf8Bytes = (value: string): Uint8Array => new TextEncoder().encode(value);

const bytesToBase64Url = (bytes: Uint8Array): string => {
  let binary = "";
  bytes.forEach((byte) => {
    binary += String.fromCharCode(byte);
  });
  if (typeof btoa === "undefined") {
    return encodeURIComponent(binary).replace(/%/g, ".");
  }
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
};

const hmacSha256Bytes = (key: Uint8Array, message: Uint8Array): Uint8Array => {
  return hmac(sha256, key, message);
};

const workspaceRawToken = (path: string, workspaceRoot: string): string => {
  const token = runtimeToken();
  if (!token) return "";
  const expiresAt = Math.floor(Date.now() / 1000) + 300;
  const payload = `workspace_raw:v2:${workspaceRoot}:${path}:${expiresAt}`;
  const signature = bytesToBase64Url(hmacSha256Bytes(toUtf8Bytes(token), toUtf8Bytes(payload)));
  return `${expiresAt}.${signature}`;
};

const skillAssetToken = (skillPath: string, variant: "small" | "large"): string => {
  const token = runtimeToken();
  if (!token) return "";
  const expiresAt = Math.floor(Date.now() / 1000) + 300;
  const payload = `skill_asset:v1:${skillPath}:${variant}:${expiresAt}`;
  const signature = bytesToBase64Url(hmacSha256Bytes(toUtf8Bytes(token), toUtf8Bytes(payload)));
  return `${expiresAt}.${signature}`;
};

const pluginAssetToken = (pluginPath: string, variant: "composer" | "logo" | "logo-dark"): string => {
  const token = runtimeToken();
  if (!token) return "";
  const expiresAt = Math.floor(Date.now() / 1000) + 300;
  const payload = `plugin_asset:v1:${pluginPath}:${variant}:${expiresAt}`;
  const signature = bytesToBase64Url(hmacSha256Bytes(toUtf8Bytes(token), toUtf8Bytes(payload)));
  return `${expiresAt}.${signature}`;
};

const attachmentAssetToken = (
  artifactId: string,
  sessionId: string,
  conversationId: string,
): string => {
  const token = runtimeToken();
  if (!token) return "";
  const expiresAt = Math.floor(Date.now() / 1000) + 300;
  const payload = `attachment_raw:v2:${sessionId}:${conversationId}:${artifactId}:${expiresAt}`;
  const signature = bytesToBase64Url(hmacSha256Bytes(toUtf8Bytes(token), toUtf8Bytes(payload)));
  return `${expiresAt}.${signature}`;
};

const artifactAssetToken = (
  artifactId: string,
  sessionId: string,
  conversationId: string,
): string => {
  const token = runtimeToken();
  if (!token) return "";
  const expiresAt = Math.floor(Date.now() / 1000) + 300;
  const payload = `artifact_raw:v1:${sessionId}:${conversationId}:${artifactId}:${expiresAt}`;
  const signature = bytesToBase64Url(hmacSha256Bytes(toUtf8Bytes(token), toUtf8Bytes(payload)));
  return `${expiresAt}.${signature}`;
};

export const withRuntimeToken = (url: string): string => url;

export const workspaceRawResourceUrlWithToken = (
  path: string,
  workspaceRoot: string,
  base = apiBase(),
): string => {
  const url = safeURL(`${base}/api/workspace/raw`, currentHttpOrigin());
  if (!url) {
    return `${base}/api/workspace/raw?path=${encodeURIComponent(path)}&workspace_root=${encodeURIComponent(workspaceRoot)}`;
  }
  url.searchParams.set("path", path);
  url.searchParams.set("workspace_root", workspaceRoot);
  const rawToken = workspaceRawToken(path, workspaceRoot);
  if (rawToken) url.searchParams.set("raw_token", rawToken);
  return url.toString();
};

export const skillAssetResourceUrlWithToken = (
  skillPath: string,
  variant: "small" | "large" = "small",
  base = apiBase(),
): string => {
  if (!skillPath.trim()) return "";
  const url = safeURL(`${base}/api/skills/asset`, currentHttpOrigin());
  if (!url) return "";
  url.searchParams.set("skill_path", skillPath);
  url.searchParams.set("variant", variant);
  const assetToken = skillAssetToken(skillPath, variant);
  if (assetToken) url.searchParams.set("asset_token", assetToken);
  return url.toString();
};

export const pluginAssetResourceUrlWithToken = (
  pluginPath: string,
  variant: "composer" | "logo" | "logo-dark" = "logo",
  base = apiBase(),
): string => {
  if (!pluginPath.trim()) return "";
  const url = safeURL(`${base}/api/plugins/asset`, currentHttpOrigin());
  if (!url) return "";
  url.searchParams.set("plugin_path", pluginPath);
  url.searchParams.set("variant", variant);
  const assetToken = pluginAssetToken(pluginPath, variant);
  if (assetToken) url.searchParams.set("asset_token", assetToken);
  return url.toString();
};

export const attachmentRawResourceUrlWithToken = (
  artifactId: string,
  sessionId: string,
  conversationId: string,
  base = apiBase(),
): string => {
  if (!artifactId.trim() || !sessionId.trim() || !conversationId.trim()) return "";
  const url = safeURL(`${base}/api/attachments/raw`, currentHttpOrigin());
  if (!url) return "";
  url.searchParams.set("artifact_id", artifactId);
  url.searchParams.set("session_id", sessionId);
  url.searchParams.set("conversation_id", conversationId);
  const assetToken = attachmentAssetToken(artifactId, sessionId, conversationId);
  if (assetToken) url.searchParams.set("asset_token", assetToken);
  return url.toString();
};

export const artifactRawResourceUrlWithToken = (
  artifactId: string,
  sessionId: string,
  conversationId: string,
  base = apiBase(),
): string => {
  if (!artifactId.trim() || !sessionId.trim() || !conversationId.trim()) return "";
  const url = safeURL(`${base}/api/artifacts/raw`, currentHttpOrigin());
  if (!url) return "";
  url.searchParams.set("artifact_id", artifactId);
  url.searchParams.set("session_id", sessionId);
  url.searchParams.set("conversation_id", conversationId);
  const assetToken = artifactAssetToken(artifactId, sessionId, conversationId);
  if (assetToken) url.searchParams.set("asset_token", assetToken);
  return url.toString();
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
  return url.toString();
};

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

export const DEFAULT_HTTP_TIMEOUT_MS = 60_000;
export const LONG_HTTP_TIMEOUT_MS = 10 * 60_000;

export type FetchTimeoutOptions = {
  timeoutMs?: number;
  timeoutMessage?: string;
};

/**
 * Fetch with one bounded lifetime while preserving caller cancellation.
 *
 * Desktop operations must always reach a visible terminal state. Native fetch
 * has no timeout, so a dead backend or stalled installer would otherwise leave
 * its button spinning forever.
 */
export const fetchWithTimeout = async (
  input: RequestInfo | URL,
  init: RequestInit = {},
  options: FetchTimeoutOptions = {},
): Promise<Response> => {
  const configuredTimeout = Number(options.timeoutMs ?? DEFAULT_HTTP_TIMEOUT_MS);
  const timeoutMs = Number.isFinite(configuredTimeout) && configuredTimeout > 0
    ? configuredTimeout
    : DEFAULT_HTTP_TIMEOUT_MS;
  const controller = new AbortController();
  const callerSignal = init.signal;
  let timedOut = false;

  const abortFromCaller = () => {
    try {
      controller.abort(callerSignal?.reason);
    } catch {
      controller.abort();
    }
  };

  if (callerSignal?.aborted) {
    abortFromCaller();
  } else {
    callerSignal?.addEventListener("abort", abortFromCaller, { once: true });
  }

  const timeout = globalThis.setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, timeoutMs);

  try {
    return await fetch(input, { ...init, signal: controller.signal });
  } catch (error) {
    if (timedOut) {
      throw new Error(
        options.timeoutMessage
        || `请求超时：${Math.ceil(timeoutMs / 1000)} 秒内没有收到响应`,
      );
    }
    throw error;
  } finally {
    globalThis.clearTimeout(timeout);
    callerSignal?.removeEventListener("abort", abortFromCaller);
  }
};

export const errorMessageFromResponseText = (text: string, fallback: string): string => {
  const trimmed = text.trim();
  if (!trimmed) return fallback;
  const parsed = safeJsonParse<unknown>(trimmed, null);
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
  return trimmed;
};

export const fetchLLMSettings = async (): Promise<unknown> => {
  const res = await fetchWithTimeout(`${apiBase()}/api/llm/settings`, {
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
  conversation_id: string;
  file_name: string;
  doc_id: string;
  artifact_id: string;
  attachment: Record<string, unknown>;
}

export interface AttachmentPreviewResponse {
  artifact_id: string;
  conversation_id: string;
  file_name: string;
  media_type: string;
  kind: string;
  size_bytes: number;
  summary: string;
  parse_error: string;
  content: string;
  content_chars: number;
  truncated: boolean;
  has_native: boolean;
}

export const fetchAttachmentPreview = async (
  sessionId: string,
  conversationId: string,
  artifactId: string,
  signal?: AbortSignal,
): Promise<AttachmentPreviewResponse> => {
  const url = safeURL(`${apiBase()}/api/attachments/preview`, currentHttpOrigin());
  if (!url) throw new ApiError(400, "Invalid attachment preview URL");
  url.searchParams.set("session_id", sessionId);
  url.searchParams.set("conversation_id", conversationId);
  url.searchParams.set("artifact_id", artifactId);
  const res = await fetchWithTimeout(
    url.toString(),
    { headers: authHeaders(), signal },
    { timeoutMessage: "附件预览加载超时，请重试。" },
  );
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new ApiError(res.status, errorMessageFromResponseText(text, res.statusText));
  }
  return (await res.json()) as AttachmentPreviewResponse;
};

export type AttachmentUploadPhase = "uploading" | "processing";

export interface UploadAttachmentOptions {
  signal?: AbortSignal;
  onProgress?: (percent: number, phase: AttachmentUploadPhase) => void;
}

const abortError = (): Error => {
  if (typeof DOMException !== "undefined") {
    return new DOMException("附件上传已取消。", "AbortError");
  }
  const error = new Error("附件上传已取消。");
  error.name = "AbortError";
  return error;
};

const uploadAttachmentWithXhr = (
  url: string,
  form: FormData,
  options: UploadAttachmentOptions,
): Promise<UploadResponse> => new Promise((resolve, reject) => {
  const xhr = new XMLHttpRequest();
  let settled = false;
  const callerSignal = options.signal;

  const cleanup = () => {
    callerSignal?.removeEventListener("abort", abortFromCaller);
  };
  const finish = (callback: () => void) => {
    if (settled) return;
    settled = true;
    cleanup();
    callback();
  };
  const abortFromCaller = () => {
    xhr.abort();
    finish(() => reject(abortError()));
  };

  xhr.open("POST", url, true);
  xhr.timeout = LONG_HTTP_TIMEOUT_MS;
  const headers = new Headers(authHeaders());
  headers.forEach((value, key) => xhr.setRequestHeader(key, value));

  xhr.upload.addEventListener("progress", (event) => {
    if (!event.lengthComputable || event.total <= 0) return;
    const percent = Math.max(0, Math.min(100, Math.round((event.loaded / event.total) * 100)));
    options.onProgress?.(percent, "uploading");
  });
  xhr.upload.addEventListener("load", () => {
    options.onProgress?.(100, "processing");
  });
  xhr.addEventListener("load", () => {
    finish(() => {
      const responseText = String(xhr.responseText || "");
      if (xhr.status < 200 || xhr.status >= 300) {
        reject(new ApiError(
          xhr.status || 500,
          errorMessageFromResponseText(responseText, xhr.statusText || "附件上传失败"),
        ));
        return;
      }
      const parsed = safeJsonParse<UploadResponse | undefined>(responseText, undefined);
      if (parsed === undefined) {
        reject(new ApiError(500, "附件上传响应无效，请重试。"));
        return;
      }
      resolve(parsed);
    });
  });
  xhr.addEventListener("error", () => {
    finish(() => reject(new Error("附件上传失败，请检查连接后重试。")));
  });
  xhr.addEventListener("timeout", () => {
    finish(() => reject(new Error("附件上传超时，请检查连接后重试。")));
  });
  xhr.addEventListener("abort", () => {
    finish(() => reject(abortError()));
  });

  if (callerSignal?.aborted) {
    abortFromCaller();
    return;
  }
  callerSignal?.addEventListener("abort", abortFromCaller, { once: true });
  options.onProgress?.(0, "uploading");
  xhr.send(form);
});

export const uploadAttachment = async (
  sessionId: string,
  conversationId: string,
  file: File,
  options: UploadAttachmentOptions = {},
): Promise<UploadResponse> => {
  const form = new FormData();
  form.append("file", file);
  const url = safeURL(`${apiBase()}/api/uploads`, currentHttpOrigin());
  if (!url) throw new ApiError(400, "Invalid attachment upload URL");
  url.searchParams.set("session_id", sessionId);
  if (conversationId.trim()) url.searchParams.set("conversation_id", conversationId.trim());

  if (typeof XMLHttpRequest !== "undefined") {
    return uploadAttachmentWithXhr(url.toString(), form, options);
  }

  options.onProgress?.(0, "uploading");
  const res = await fetchWithTimeout(
    url.toString(),
    { method: "POST", body: form, headers: authHeaders(), signal: options.signal },
    {
      timeoutMs: LONG_HTTP_TIMEOUT_MS,
      timeoutMessage: "附件上传超时，请检查连接后重试。",
    },
  );
  options.onProgress?.(100, "processing");
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new ApiError(res.status, errorMessageFromResponseText(text, res.statusText));
  }
  return (await res.json()) as UploadResponse;
};
