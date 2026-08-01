export const isKnownProviderModelList = (provider: string): boolean => {
  const normalizedProvider = String(provider || "").trim().toLowerCase();
  return normalizedProvider === "openai" || normalizedProvider === "anthropic";
};

/**
 * Determine which models the composer should show as selectable.
 *
 * - If ``modelsSource`` is ``"live"``, the full list is trusted regardless of
 *   provider (the user explicitly ran Discover and it succeeded).
 * - Otherwise use the explicit built-in provider contract: OpenAI and
 *   Anthropic show their configured list, while custom gateways only expose
 *   the current model unless discovery marked the list as live.
 */
export const selectableModelsForProvider = (
  models: string[],
  currentModel: string,
  provider: string,
  modelsSource?: string,
): string[] => {
  const current = String(currentModel || "").trim();
  const source = modelsSource === "live" || isKnownProviderModelList(provider)
    ? models
    : current
      ? [current]
      : [];
  const merged = source
    .map((model) => String(model || "").trim())
    .filter(Boolean);
  if (current && !merged.includes(current)) {
    merged.unshift(current);
  }
  return Array.from(new Set(merged));
};
