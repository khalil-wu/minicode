type RetryOptions = {
  attempts?: number;
  delaysMs?: number[];
  cacheKey?: string;
};

const DEFAULT_STARTUP_RETRY_DELAYS_MS = [250, 700, 1400];
const inFlightLoads = new Map<string, Promise<unknown>>();

const delay = (ms: number): Promise<void> =>
  new Promise((resolve) => window.setTimeout(resolve, ms));

export const formatSettingsLoadError = (error: unknown): string => {
  const message = error instanceof Error ? error.message : String(error);
  if (/failed to fetch|networkerror|load failed/i.test(message)) {
    return "暂时无法连接 MiniCode 后端，请等待桌面后端启动完成后重试。";
  }
  return message || "请求失败。";
};

const isRetryableLoadError = (error: unknown): boolean => {
  const message = error instanceof Error ? error.message : String(error);
  return /failed to fetch|networkerror|load failed/i.test(message);
};

export const fetchJsonWithStartupRetry = async <T,>(
  url: string,
  init: RequestInit,
  options: RetryOptions = {},
): Promise<T> => {
  if (options.cacheKey) {
    const existing = inFlightLoads.get(options.cacheKey);
    if (existing) return await existing as T;
  }

  const loadPromise = fetchJsonWithStartupRetryInner<T>(url, init, options);
  if (options.cacheKey) {
    inFlightLoads.set(options.cacheKey, loadPromise);
    const clear = () => {
      if (inFlightLoads.get(options.cacheKey!) === loadPromise) {
        inFlightLoads.delete(options.cacheKey!);
      }
    };
    void loadPromise.then(clear, clear);
  }
  return await loadPromise;
};

const fetchJsonWithStartupRetryInner = async <T,>(
  url: string,
  init: RequestInit,
  options: RetryOptions,
): Promise<T> => {
  const delaysMs = options.delaysMs ?? DEFAULT_STARTUP_RETRY_DELAYS_MS;
  const attempts = Math.max(1, options.attempts ?? delaysMs.length + 1);
  let lastError: unknown = null;

  for (let attempt = 0; attempt < attempts; attempt += 1) {
    try {
      const res = await fetch(url, init);
      if (!res.ok) throw new Error(await res.text().catch(() => res.statusText));
      return await res.json() as T;
    } catch (error) {
      lastError = error;
      const canRetry = attempt < attempts - 1 && isRetryableLoadError(error);
      if (!canRetry) break;
      await delay(delaysMs[Math.min(attempt, delaysMs.length - 1)] ?? 500);
    }
  }

  throw lastError;
};
