export type PromptCacheUsageLike = {
  input?: number;
  cacheRead?: number;
  cacheWrite?: number;
  promptCacheTotal?: number;
  promptCacheHitRate?: number;
  provider?: string;
};

const finiteNumber = (value: unknown): number | null => {
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric : null;
};

const nonNegativeNumber = (value: unknown): number => {
  const numeric = finiteNumber(value);
  return numeric == null ? 0 : Math.max(0, numeric);
};

export const promptCacheEffectivePromptTokens = (usage: PromptCacheUsageLike | null | undefined): number => {
  if (!usage) return 0;
  const authoritative = finiteNumber(usage.promptCacheTotal);
  if (authoritative != null && authoritative >= 0) return authoritative;

  const input = nonNegativeNumber(usage.input);
  const cacheRead = nonNegativeNumber(usage.cacheRead);
  const cacheWrite = nonNegativeNumber(usage.cacheWrite);
  const provider = String(usage.provider || "").toLowerCase();
  if (provider.includes("anthropic")) {
    return input + cacheRead + cacheWrite;
  }
  return Math.max(input, cacheRead) + cacheWrite;
};

export const promptCacheHitRate = (usage: PromptCacheUsageLike | null | undefined): number | null => {
  if (!usage) return null;
  const cacheRead = nonNegativeNumber(usage.cacheRead);
  const authoritative = finiteNumber(usage.promptCacheHitRate);
  if (authoritative != null) {
    return Math.max(0, Math.min(100, Math.round(authoritative * 10) / 10));
  }

  const denominator = promptCacheEffectivePromptTokens(usage);
  if (denominator <= 0 || cacheRead <= 0) return null;
  return Math.min(100, Math.round((cacheRead / denominator) * 1000) / 10);
};
