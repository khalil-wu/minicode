export const isKnownProviderModelList = (provider: string, baseUrl: string): boolean => {
  const normalizedProvider = String(provider || "").trim().toLowerCase();
  if (normalizedProvider === "openai" || normalizedProvider === "anthropic" || normalizedProvider === "deepseek" || normalizedProvider === "openrouter") {
    return true;
  }
  const host = String(baseUrl || "").trim().toLowerCase();
  return host.includes("api.openai.com") || host.includes("api.deepseek.com") || host.includes("openrouter.ai");
};

/**
 * Determine which models the composer should show as selectable.
 *
 * - If ``modelsSource`` is ``"live"``, the full list is trusted regardless of
 *   provider (the user explicitly ran Discover and it succeeded).
 * - Otherwise fall back to the ``isKnownProviderModelList`` heuristic: known
 *   providers (OpenAI, Anthropic, DeepSeek, OpenRouter) show the full list,
 *   while custom/unknown gateways only expose the current model to avoid
 *   surfacing stale or fallback entries.
 */
export const selectableModelsForProvider = (
  models: string[],
  currentModel: string,
  provider: string,
  baseUrl: string,
  modelsSource?: string,
): string[] => {
  const current = String(currentModel || "").trim();
  const source = modelsSource === "live" || isKnownProviderModelList(provider, baseUrl)
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
