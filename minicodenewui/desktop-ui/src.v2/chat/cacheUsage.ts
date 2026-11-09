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

export const promptCacheEffectivePromptTokens = (usage: PromptCacheUsageLike | null | undefined): number => {
  if (!usage) return 0;
  const authoritative = usage.promptCacheTotal;
  if (authoritative !== undefined && authoritative > 0) return authoritative;

  const input = usage.input ?? 0;
  const ordinary = usage.ordinaryInput;
  const cacheRead = usage.cacheRead ?? 0;
  const cacheWrite = usage.cacheWrite ?? 0;
  if (ordinary !== undefined) {
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
  const authoritative = usage.ordinaryInput;
  if (authoritative !== undefined) return authoritative;
  const total = promptCacheEffectivePromptTokens(usage);
  return Math.max(
    0,
    total - (usage.cacheRead ?? 0) - (usage.cacheWrite ?? 0),
  );
};

export const promptCacheHitRate = (usage: PromptCacheUsageLike | null | undefined): number | null => {
  if (!usage) return null;
  const cacheRead = usage.cacheRead ?? 0;
  const authoritative = usage.promptCacheHitRate;
  if (authoritative !== undefined) {
    return Math.round(authoritative * 10) / 10;
  }

  const denominator = promptCacheEffectivePromptTokens(usage);
  if (denominator <= 0 || cacheRead <= 0) return null;
  return Math.round((cacheRead / denominator) * 1000) / 10;
};
