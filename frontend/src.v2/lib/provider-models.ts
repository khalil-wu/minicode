export const isKnownProviderModelList = (provider: string): boolean => {
  const normalizedProvider = String(provider || "").trim().toLowerCase();
  return normalizedProvider === "openai" || normalizedProvider === "anthropic";
};

/**
 * Determine which models the composer should show as selectable.
 *
 * - Live discovery, settings/models.json, explicit draft configuration, and extension catalogs
 *   are authoritative for their provider. The backend publishes these exact
 *   source values with the resolved model list.
 * - Otherwise use the explicit built-in provider contract: OpenAI and
 *   Anthropic show their configured list, while custom gateways only expose
 *   the current model when the source is unknown or unavailable. This avoids
 *   treating a runtime fallback list as an authoritative custom catalog.
 */
export const selectableModelsForProvider = (
  models: string[],
  currentModel: string,
  provider: string,
  modelsSource?: string,
): string[] => {
  const current = String(currentModel || "").trim();
  const source = modelsSource === "live" || modelsSource === "settings" || modelsSource === "models_json"
    || modelsSource === "configured" || modelsSource === "extension" || isKnownProviderModelList(provider)
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
