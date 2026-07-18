const ERROR_PREFIX_RE = /^Error:\s*/i;

/**
 * Model-facing markup tags that wrap tool/sandbox errors. These are
 * instructions for the model, not text for the user to read, so strip them
 * from any displayed tool-error text (mirrors cc's FallbackToolUseErrorMessage
 * tag-stripping). Shared by ErrorCell and ActivityCell's failed-record path.
 */
const ERROR_MARKUP_TAG_RE = /<\/?(?:tool_use_error|error|sandbox_violation)[^>]*>/gi;
const TECHNICAL_ERROR_DETAIL_RE = /\b(?:provider(?:_error_(?:type|code|schema_type))?|request_id|trace_id|call_id)=[^\s,;)}\]]+/gi;
const INTERNAL_CALL_ID_RE = /\bcall_[a-z0-9_-]{8,}\b/gi;
const ELAPSED_ONLY_RE = /\b\d+(?:\.\d+)?s elapsed\b/gi;

function stripTechnicalErrorDetails(text: string): string {
  return text
    .replace(TECHNICAL_ERROR_DETAIL_RE, "")
    .replace(INTERNAL_CALL_ID_RE, "")
    .replace(ELAPSED_ONLY_RE, "")
    .replace(/\(\s*[,;]*\s*\)|\[\s*[,;]*\s*\]/g, "")
    .replace(/\s+([,;:.])/g, "$1")
    .replace(/([,;])\s*([,;])/g, "$1")
    .replace(/\s{2,}/g, " ")
    .trim();
}

export function purifyToolErrorText(text: string | undefined): string {
  if (!text) return text ?? "";
  const stripped = text.replace(ERROR_MARKUP_TAG_RE, "");
  return stripped === text ? text : stripped.trim();
}

type NormalizeAgentErrorMessageOptions = {
  includeProviderDetails?: boolean;
};

const RATE_LIMIT_MESSAGE = "\u6a21\u578b\u6682\u65f6\u7e41\u5fd9\u6216\u8fbe\u5230\u5e76\u53d1\u9650\u5236\uff0c\u8bf7\u7a0d\u540e\u91cd\u8bd5\u6216\u5207\u6362\u6a21\u578b\u3002";
const PROXY_MESSAGE = "\u8054\u7f51\u8bf7\u6c42\u5931\u8d25\uff1a\u4ee3\u7406\u8ba4\u8bc1\u5931\u8d25\uff08407 Proxy Authentication Required\uff09\u3002\u8bf7\u68c0\u67e5 HTTP_PROXY / HTTPS_PROXY \u6216\u4ee3\u7406\u8ba4\u8bc1\u4fe1\u606f\u3002";
const AUTH_MESSAGE = "\u6a21\u578b\u9274\u6743\u5931\u8d25\uff0c\u8bf7\u68c0\u67e5 API Key \u548c\u6a21\u578b\u8bbe\u7f6e\u3002";
const BILLING_MESSAGE = "\u6a21\u578b\u670d\u52a1\u989d\u5ea6\u6216\u8ba1\u8d39\u4e0d\u53ef\u7528\uff0c\u8bf7\u68c0\u67e5\u8d26\u6237\u72b6\u6001\u3002";
const NETWORK_MESSAGE = "\u6a21\u578b\u670d\u52a1\u7f51\u7edc\u8bf7\u6c42\u5931\u8d25\uff0c\u8bf7\u7a0d\u540e\u91cd\u8bd5\u3002";
const GENERIC_MODEL_MESSAGE = "\u6a21\u578b\u8c03\u7528\u5931\u8d25\uff0c\u8bf7\u7a0d\u540e\u91cd\u8bd5\u6216\u5207\u6362\u6a21\u578b\u3002";

function providerDetailSuffix(text: string, includeTechnicalDetails = false): string {
  const parts: string[] = [];
  const provider =
    text.match(/\bprovider_error_type=([A-Za-z0-9._:-]{1,80})/i)?.[1] ||
    text.match(/\bprovider=([A-Za-z0-9._:-]{1,80})/i)?.[1];
  if (includeTechnicalDetails && provider && provider.toLowerCase() !== "unknown") {
    parts.push(`provider=${provider}`);
  }

  const statuses = new Set<string>();
  for (const match of text.matchAll(/\bstatus=(\d{3})\b|\bHTTP\s*(\d{3})\b|\b(40[0-9]|429|50[0-9])\b/gi)) {
    const status = match[1] || match[2] || match[3];
    if (status) statuses.add(status);
  }
  for (const status of statuses) parts.push(`HTTP ${status}`);

  const code = text.match(/\bprovider_error_code=([A-Za-z0-9._:-]{1,80})/i)?.[1];
  if (includeTechnicalDetails && code) parts.push(`code=${code}`);
  const schemaType = text.match(/\bprovider_error_schema_type=([A-Za-z0-9._:-]{1,80})/i)?.[1];
  if (includeTechnicalDetails && schemaType) parts.push(`type=${schemaType}`);

  return parts.length ? `（${parts.join(", ")}）` : "";
}

function modelConfigMessage(text: string, options: NormalizeAgentErrorMessageOptions = {}): string {
  const match = text.match(/\bmodel\s+([A-Za-z0-9._:/-]+)\s+(?:does not exist|not found|is invalid|invalid)/i);
  const model = match?.[1]?.replace(/[),.;:]+$/, "");
  const suffix = model ? ` (${model})` : "";
  return `\u6a21\u578b\u540d\u6216\u6a21\u578b\u914d\u7f6e\u65e0\u6548${suffix}\uff0c\u8bf7\u68c0\u67e5 provider\u3001Base URL \u548c model \u8bbe\u7f6e\u3002${options.includeProviderDetails === false ? "" : providerDetailSuffix(text, options.includeProviderDetails === true)}`;
}

export function normalizeAgentErrorMessage(raw: string, options: NormalizeAgentErrorMessageOptions = {}): string {
  const text = raw.replace(ERROR_PREFIX_RE, "").replace(/\s+/g, " ").trim();
  if (!text) return "Something went wrong.";
  const suffix = options.includeProviderDetails === false
    ? ""
    : providerDetailSuffix(text, options.includeProviderDetails === true);
  if (/concurrency limit exceeded|rate limit|too many requests|retry later|provider_error_type=rate_limit|429/i.test(text)) {
    return RATE_LIMIT_MESSAGE + suffix;
  }
  if (/407|proxy authentication required|proxy auth|provider_error_type=proxy|代理鉴权失败|代理认证失败/i.test(text)) {
    return PROXY_MESSAGE + suffix;
  }
  if (/invalid api key|incorrect api key|unauthorized|authentication|provider_error_type=auth|401/i.test(text)) {
    return AUTH_MESSAGE + suffix;
  }
  if (/insufficient balance|insufficient quota|quota exceeded|billing|payment required|provider_error_type=billing|402/i.test(text)) {
    return BILLING_MESSAGE + suffix;
  }
  if (/your request was blocked|request was blocked|blocked by|waf|cloudflare|provider_error_type=blocked|403/i.test(text)) {
    return "模型请求被服务商或网关拦截，请检查模型、Base URL、网关规则或请求内容。" + suffix;
  }
  if (/provider_error_type=model|model_not_found|invalid_model|model does not exist|model\s+[A-Za-z0-9._:/-]+\s+does not exist|model .*not found|invalid model|unknown model|no such model/i.test(text)) {
    return modelConfigMessage(text, options);
  }
  if (/timeout|timed out|connection reset|connection refused|connection error|bad gateway|service unavailable|gateway timeout|provider_error_type=network|500|503|502|504/i.test(text)) {
    return NETWORK_MESSAGE + suffix;
  }
  if (/Claude API 调用失败|LLM API 调用失败|LLM API request failed|model request failed/i.test(text)) {
    return GENERIC_MODEL_MESSAGE + suffix;
  }
  if (/workspace .*does not exist|invalid project path|workspace does not exist/i.test(text)) {
    return "Workspace folder is missing. Open another folder to continue.";
  }
  if (/Stopped because the model kept attempting the exact same tool call and the system blocked it/i.test(text)) {
    return "Stopped because the model repeated the same blocked tool call. Try rephrasing the request or specify the next step directly.";
  }
  if (/Try rephrasing the request or specify the next step more directly/i.test(text)) {
    return text.replace(/Try rephrasing the request or specify the next step more directly\.?/i, "Try rephrasing the request or specify the next step directly.");
  }
  if (/outside (?:the )?(?:allowed|trusted) workspace|forbidden path/i.test(text)) {
    return "The request tried to access a path outside the current workspace and was blocked. Switch to the correct workspace or use a path inside it.";
  }
  return options.includeProviderDetails === true
    ? text
    : stripTechnicalErrorDetails(text) || "Something went wrong.";
}
