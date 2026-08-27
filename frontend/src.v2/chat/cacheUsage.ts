export type PromptCacheUsageLike = {
  input?: number;
  ordinaryInput?: number;
  inputIncludesCacheRead?: boolean;
  inputIncludesCacheWrite?: boolean;
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
  if (authoritative != null && authoritative > 0) return authoritative;

  const input = nonNegativeNumber(usage.input);
  const ordinary = finiteNumber(usage.ordinaryInput);
  const cacheRead = nonNegativeNumber(usage.cacheRead);
  const cacheWrite = nonNegativeNumber(usage.cacheWrite);
  if (ordinary != null && ordinary >= 0) {
    return ordinary + cacheRead + cacheWrite;
  }
  let normalizedOrdinary = input;
  if (usage.inputIncludesCacheRead !== false) {
    normalizedOrdinary -= Math.min(cacheRead, normalizedOrdinary);
  }
  if (usage.inputIncludesCacheWrite !== false) {
    normalizedOrdinary -= Math.min(cacheWrite, Math.max(0, normalizedOrdinary));
  }
  return Math.max(0, normalizedOrdinary) + cacheRead + cacheWrite;
};

export const promptCacheOrdinaryInputTokens = (usage: PromptCacheUsageLike | null | undefined): number => {
  if (!usage) return 0;
  const authoritative = finiteNumber(usage.ordinaryInput);
  if (authoritative != null && authoritative >= 0) return authoritative;
  const total = promptCacheEffectivePromptTokens(usage);
  return Math.max(
    0,
    total - nonNegativeNumber(usage.cacheRead) - nonNegativeNumber(usage.cacheWrite),
  );
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
