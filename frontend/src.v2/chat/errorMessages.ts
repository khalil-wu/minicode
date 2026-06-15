const ERROR_PREFIX_RE = /^Error:\s*/i;

const RATE_LIMIT_MESSAGE = "\u6a21\u578b\u6682\u65f6\u7e41\u5fd9\u6216\u8fbe\u5230\u5e76\u53d1\u9650\u5236\uff0c\u8bf7\u7a0d\u540e\u91cd\u8bd5\u6216\u5207\u6362\u6a21\u578b\u3002";
const PROXY_MESSAGE = "\u8054\u7f51\u8bf7\u6c42\u5931\u8d25\uff1a\u4ee3\u7406\u8ba4\u8bc1\u5931\u8d25\uff08407 Proxy Authentication Required\uff09\u3002\u8bf7\u68c0\u67e5 HTTP_PROXY / HTTPS_PROXY \u6216\u4ee3\u7406\u8ba4\u8bc1\u4fe1\u606f\u3002";
const AUTH_MESSAGE = "\u6a21\u578b\u9274\u6743\u5931\u8d25\uff0c\u8bf7\u68c0\u67e5 API Key \u548c\u6a21\u578b\u8bbe\u7f6e\u3002";
const BILLING_MESSAGE = "\u6a21\u578b\u670d\u52a1\u989d\u5ea6\u6216\u8ba1\u8d39\u4e0d\u53ef\u7528\uff0c\u8bf7\u68c0\u67e5\u8d26\u6237\u72b6\u6001\u3002";
const NETWORK_MESSAGE = "\u6a21\u578b\u670d\u52a1\u7f51\u7edc\u8bf7\u6c42\u5931\u8d25\uff0c\u8bf7\u7a0d\u540e\u91cd\u8bd5\u3002";
const GENERIC_MODEL_MESSAGE = "\u6a21\u578b\u8c03\u7528\u5931\u8d25\uff0c\u8bf7\u7a0d\u540e\u91cd\u8bd5\u6216\u5207\u6362\u6a21\u578b\u3002";

function modelConfigMessage(text: string): string {
  const match = text.match(/\bmodel\s+([A-Za-z0-9._:/-]+)\s+(?:does not exist|not found|is invalid|invalid)/i);
  const model = match?.[1]?.replace(/[),.;:]+$/, "");
  const suffix = model ? ` (${model})` : "";
  return `\u6a21\u578b\u540d\u6216\u6a21\u578b\u914d\u7f6e\u65e0\u6548${suffix}\uff0c\u8bf7\u68c0\u67e5 provider\u3001Base URL \u548c model \u8bbe\u7f6e\u3002`;
}

export function normalizeAgentErrorMessage(raw: string): string {
  const text = raw.replace(ERROR_PREFIX_RE, "").replace(/\s+/g, " ").trim();
  if (!text) return "Something went wrong.";
  if (/concurrency limit exceeded|rate limit|too many requests|retry later|provider_error_type=rate_limit|429/i.test(text)) {
    return RATE_LIMIT_MESSAGE;
  }
  if (/407|proxy authentication required|proxy auth|provider_error_type=proxy|代理鉴权失败|代理认证失败/i.test(text)) {
    return PROXY_MESSAGE;
  }
  if (/invalid api key|incorrect api key|unauthorized|authentication|provider_error_type=auth|401/i.test(text)) {
    return AUTH_MESSAGE;
  }
  if (/insufficient balance|insufficient quota|quota exceeded|billing|payment required|provider_error_type=billing|402/i.test(text)) {
    return BILLING_MESSAGE;
  }
  if (/provider_error_type=model|model_not_found|invalid_model|model does not exist|model\s+[A-Za-z0-9._:/-]+\s+does not exist|model .*not found|invalid model|unknown model|no such model/i.test(text)) {
    return modelConfigMessage(text);
  }
  if (/timeout|timed out|connection reset|connection refused|connection error|bad gateway|service unavailable|gateway timeout|provider_error_type=network|500|503|502|504/i.test(text)) {
    return NETWORK_MESSAGE;
  }
  if (/Claude API 调用失败|LLM API 调用失败|LLM API request failed|model request failed/i.test(text)) {
    return GENERIC_MODEL_MESSAGE;
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
  return text;
}
