const CONTROL_PLANE_NOTICE_PATTERNS = [
  /^Provider (?:updated|saved|ready|switched|set) to\b/i,
  /^Provider (?:saved|ready)\b/i,
  /^Model (?:updated|set|selected|changed|switched) to\b/i,
  /^Using model\b/i,
  /^LLM settings (?:updated|saved|applied)\b/i,
  /^API key (?:saved|updated|cleared|removed)\b/i,
  /^Missing (?:API key|Anthropic API key)\b/i,
  /^(?:OpenAI|Anthropic|DeepSeek|OpenRouter|Gateway|Provider) authentication\b/i,
  /^Reasoning effort set to\b/i,
  /^Reasoning effort applies to OpenAI-compatible providers\.?$/i,
  /^Reasoning effort was not applied because\b/i,
];

export const isControlPlaneNotice = (content: string): boolean => {
  const normalized = content.trim();
  return Boolean(normalized) && CONTROL_PLANE_NOTICE_PATTERNS.some((pattern) => pattern.test(normalized));
};
