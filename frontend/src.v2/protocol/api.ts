import { safeURL } from "../lib/safe-parse";

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

const rightRotate = (value: number, bits: number): number => (value >>> bits) | (value << (32 - bits));

const SHA256_K = new Uint32Array([
  0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
  0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
  0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
  0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
  0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
  0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
  0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
  0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
]);

const sha256Bytes = (input: Uint8Array): Uint8Array => {
  const words = new Uint32Array(64);
  const paddedLength = (((input.length + 9 + 63) >> 6) << 6);
  const padded = new Uint8Array(paddedLength);
  padded.set(input);
  padded[input.length] = 0x80;
  const bitLength = input.length * 8;
  const view = new DataView(padded.buffer);
  view.setUint32(padded.length - 4, bitLength >>> 0, false);
  view.setUint32(padded.length - 8, Math.floor(bitLength / 0x100000000), false);

  let h0 = 0x6a09e667;
  let h1 = 0xbb67ae85;
  let h2 = 0x3c6ef372;
  let h3 = 0xa54ff53a;
  let h4 = 0x510e527f;
  let h5 = 0x9b05688c;
  let h6 = 0x1f83d9ab;
  let h7 = 0x5be0cd19;

  for (let offset = 0; offset < padded.length; offset += 64) {
    for (let index = 0; index < 16; index += 1) {
      words[index] = view.getUint32(offset + (index * 4), false);
    }
    for (let index = 16; index < 64; index += 1) {
      const s0 = rightRotate(words[index - 15]!, 7) ^ rightRotate(words[index - 15]!, 18) ^ (words[index - 15]! >>> 3);
      const s1 = rightRotate(words[index - 2]!, 17) ^ rightRotate(words[index - 2]!, 19) ^ (words[index - 2]! >>> 10);
      words[index] = (words[index - 16]! + s0 + words[index - 7]! + s1) >>> 0;
    }

    let a = h0;
    let b = h1;
    let c = h2;
    let d = h3;
    let e = h4;
    let f = h5;
    let g = h6;
    let h = h7;

    for (let index = 0; index < 64; index += 1) {
      const s1 = rightRotate(e, 6) ^ rightRotate(e, 11) ^ rightRotate(e, 25);
      const ch = (e & f) ^ (~e & g);
      const temp1 = (h + s1 + ch + SHA256_K[index]! + words[index]!) >>> 0;
      const s0 = rightRotate(a, 2) ^ rightRotate(a, 13) ^ rightRotate(a, 22);
      const maj = (a & b) ^ (a & c) ^ (b & c);
      const temp2 = (s0 + maj) >>> 0;

      h = g;
      g = f;
      f = e;
      e = (d + temp1) >>> 0;
      d = c;
      c = b;
      b = a;
      a = (temp1 + temp2) >>> 0;
    }

    h0 = (h0 + a) >>> 0;
    h1 = (h1 + b) >>> 0;
    h2 = (h2 + c) >>> 0;
    h3 = (h3 + d) >>> 0;
    h4 = (h4 + e) >>> 0;
    h5 = (h5 + f) >>> 0;
    h6 = (h6 + g) >>> 0;
    h7 = (h7 + h) >>> 0;
  }

  const digest = new Uint8Array(32);
  const digestView = new DataView(digest.buffer);
  [h0, h1, h2, h3, h4, h5, h6, h7].forEach((value, index) => {
    digestView.setUint32(index * 4, value >>> 0, false);
  });
  return digest;
};

const hmacSha256Bytes = (key: Uint8Array, message: Uint8Array): Uint8Array => {
  const blockSize = 64;
  const normalizedKey = key.length > blockSize ? sha256Bytes(key) : key;
  const keyBlock = new Uint8Array(blockSize);
  keyBlock.set(normalizedKey);

  const innerPad = new Uint8Array(blockSize);
  const outerPad = new Uint8Array(blockSize);
  for (let index = 0; index < blockSize; index += 1) {
    const value = keyBlock[index] ?? 0;
    innerPad[index] = value ^ 0x36;
    outerPad[index] = value ^ 0x5c;
  }

  const innerInput = new Uint8Array(blockSize + message.length);
  innerInput.set(innerPad);
  innerInput.set(message, blockSize);
  const innerDigest = sha256Bytes(innerInput);

  const outerInput = new Uint8Array(blockSize + innerDigest.length);
  outerInput.set(outerPad);
  outerInput.set(innerDigest, blockSize);
  return sha256Bytes(outerInput);
};

const workspaceRawToken = (path: string): string => {
  const token = runtimeToken();
  if (!token) return "";
  const expiresAt = Math.floor(Date.now() / 1000) + 300;
  const payload = `workspace_raw:v1:${path}:${expiresAt}`;
  const signature = bytesToBase64Url(hmacSha256Bytes(toUtf8Bytes(token), toUtf8Bytes(payload)));
  return `${expiresAt}.${signature}`;
};

export const withRuntimeToken = (url: string): string => url;

export const workspaceRawResourceUrlWithToken = (path: string, base = apiBase()): string => {
  const url = safeURL(`${base}/api/workspace/raw`, currentHttpOrigin());
  if (!url) {
    return `${base}/api/workspace/raw?path=${encodeURIComponent(path)}`;
  }
  url.searchParams.set("path", path);
  const rawToken = workspaceRawToken(path);
  if (rawToken) url.searchParams.set("raw_token", rawToken);
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

export const errorMessageFromResponseText = (text: string, fallback: string): string => {
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
  signal?: AbortSignal,
): Promise<UploadResponse> => {
  const form = new FormData();
  form.append("file", file);
  const res = await fetch(
    `${apiBase()}/api/uploads?session_id=${encodeURIComponent(sessionId)}`,
    { method: "POST", body: form, headers: authHeaders(), signal },
  );
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new ApiError(res.status, errorMessageFromResponseText(text, res.statusText));
  }
  return (await res.json()) as UploadResponse;
};
