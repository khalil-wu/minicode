type ProviderReasoningLike = {
  source?: unknown;
  visibility?: unknown;
  provider_reasoning_type?: unknown;
  providerReasoningType?: unknown;
  is_raw_provider_reasoning?: unknown;
  isRawProviderReasoning?: unknown;
};

const PROVIDER_REASONING_SOURCES = new Set(["provider", "reasoning"]);
const HIDDEN_REASONING_VISIBILITIES = new Set([
  "debug",
  "hidden",
  "internal",
  "redacted",
]);

export const PROVIDER_REASONING_SUMMARY_TYPE = "reasoning_summary_text";

export const providerReasoningType = (value: ProviderReasoningLike): string =>
  String(value.providerReasoningType ?? value.provider_reasoning_type ?? "")
    .trim()
    .toLowerCase();

export const isProviderReasoningSummary = (value: ProviderReasoningLike): boolean =>
  providerReasoningType(value) === PROVIDER_REASONING_SUMMARY_TYPE;

export const isProviderReasoning = (value: ProviderReasoningLike): boolean => {
  const source = String(value.source ?? "").trim().toLowerCase();
  return PROVIDER_REASONING_SOURCES.has(source)
    || Boolean(providerReasoningType(value))
    || value.is_raw_provider_reasoning === true
    || value.isRawProviderReasoning === true;
};

export const isTransientProviderReasoning = (value: ProviderReasoningLike): boolean =>
  isProviderReasoning(value) && !isProviderReasoningSummary(value);

export const isHiddenProviderReasoning = (value: ProviderReasoningLike): boolean =>
  HIDDEN_REASONING_VISIBILITIES.has(
    String(value.visibility ?? "").trim().toLowerCase(),
  );
