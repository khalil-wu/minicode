import { describe, expect, it } from "vitest";
import { normalizeAgentErrorMessage } from "./errorMessages";

describe("normalizeAgentErrorMessage", () => {
  it("normalizes proxy auth failures into actionable guidance", () => {
    const message = normalizeAgentErrorMessage(
      "Error: hosted web search failed: 407 Proxy Authentication Required",
    );

    expect(message).toContain("\u4ee3\u7406\u8ba4\u8bc1\u5931\u8d25");
    expect(message).toContain("HTTP_PROXY");
    expect(message).toContain("HTTPS_PROXY");
  });

  it("normalizes model configuration failures before generic API fallback", () => {
    const message = normalizeAgentErrorMessage(
      "Error: LLM API request failed: 400 Bad Request: model deepseek-v4 does not exist",
    );

    expect(message).toContain("\u6a21\u578b\u540d");
    expect(message).toContain("deepseek-v4");
  });

  it("reports unsupported image input instead of a generic model failure", () => {
    const message = normalizeAgentErrorMessage(
      "LLM API 调用失败: No endpoints found that support image input (provider_error_type=unsupported_capability status=404)",
    );

    expect(message).toContain("不支持图片输入");
    expect(message).toContain("视觉输入");
    expect(message).not.toContain("稍后重试");
  });

  it("reports provider content filtering with actionable recovery guidance", () => {
    const message = normalizeAgentErrorMessage(
      "LLM API 调用失败: Content Exists Risk (provider_error_type=content_filter status=400)",
    );

    expect(message).toContain("内容安全策略");
    expect(message).toContain("更换来源");
    expect(message).toContain("HTTP 400");
    expect(message).not.toContain("Content Exists Risk");
  });

  it("normalizes auth failures into key guidance", () => {
    const message = normalizeAgentErrorMessage(
      "Error: LLM API request failed: 401 Unauthorized: invalid api key",
    );

    expect(message).toContain("API Key");
  });

  it("normalizes billing failures into account guidance", () => {
    const message = normalizeAgentErrorMessage(
      "Error: LLM API request failed: 402 Payment Required: insufficient balance",
    );

    expect(message).toContain("\u989d\u5ea6");
    expect(message).toContain("\u8d26\u6237");
  });

  it("normalizes network failures into retry guidance", () => {
    const message = normalizeAgentErrorMessage(
      "Error: LLM API request failed: 502 Bad Gateway",
    );

    expect(message).toContain("\u7f51\u7edc");
    expect(message).toContain("\u91cd\u8bd5");
    expect(message).toContain("HTTP 502");
  });

  it("reports an explicit wire-protocol mismatch without suggesting a retry or fallback", () => {
    const message = normalizeAgentErrorMessage(
      "MiniCode Anthropic Messages 请求失败: status=500 provider_error_code=convert_request_failed provider_error_type=protocol",
    );

    expect(message).toContain("API 格式");
    expect(message).toContain("MiniCode 未切换到其他协议");
    expect(message).not.toContain("稍后重试");
  });

  it("keeps protocol details out of ordinary errors unless explicitly requested", () => {
    const raw = "LLM API 调用失败: Your request was blocked. (provider_error_type=blocked status=403 provider_error_code=policy_violation provider_error_schema_type=gateway)";
    const message = normalizeAgentErrorMessage(raw);

    expect(message).toContain("\u62e6\u622a");
    expect(message).toContain("HTTP 403");
    expect(message).not.toContain("provider=");
    expect(message).not.toContain("code=");
    expect(message).not.toContain("type=");

    const technical = normalizeAgentErrorMessage(raw, { includeProviderDetails: true });
    expect(technical).toContain("provider=blocked");
    expect(technical).toContain("code=policy_violation");
    expect(technical).toContain("type=gateway");
  });
});
