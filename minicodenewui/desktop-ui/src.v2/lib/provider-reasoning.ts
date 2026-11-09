export type ProviderReasoningLike = {
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

const reasoningFields = (value: object): ProviderReasoningLike => value as ProviderReasoningLike;

export const providerReasoningType = (value: object): string => {
  const fields = reasoningFields(value);
  return String(fields.providerReasoningType ?? fields.provider_reasoning_type ?? "")
    .trim()
    .toLowerCase();
};

export const isProviderReasoningSummary = (value: object): boolean =>
  providerReasoningType(value) === PROVIDER_REASONING_SUMMARY_TYPE;

export const isProviderReasoning = (value: object): boolean => {
  const fields = reasoningFields(value);
  const source = String(fields.source ?? "").trim().toLowerCase();
  return PROVIDER_REASONING_SOURCES.has(source)
    || Boolean(providerReasoningType(value))
    || fields.is_raw_provider_reasoning === true
    || fields.isRawProviderReasoning === true;
};

export const isTransientProviderReasoning = (value: object): boolean =>
  isProviderReasoning(value) && !isProviderReasoningSummary(value);

export const isHiddenProviderReasoning = (value: object): boolean =>
  HIDDEN_REASONING_VISIBILITIES.has(
    String(reasoningFields(value).visibility ?? "").trim().toLowerCase(),
  );
