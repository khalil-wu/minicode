import { describe, expect, it } from "vitest";
import {
  isHiddenProviderReasoning,
  isProviderReasoningSummary,
  isTransientProviderReasoning,
} from "./provider-reasoning";

describe("provider reasoning lifecycle", () => {
  it("keeps summaries durable and treats raw or untyped provider reasoning as transient", () => {
    expect(isProviderReasoningSummary({
      source: "provider",
      providerReasoningType: "reasoning_summary_text",
    })).toBe(true);
    expect(isTransientProviderReasoning({
      source: "provider",
      providerReasoningType: "reasoning_summary_text",
    })).toBe(false);
    expect(isTransientProviderReasoning({
      source: "provider",
      providerReasoningType: "reasoning_content",
    })).toBe(true);
    expect(isTransientProviderReasoning({ source: "provider" })).toBe(true);
  });

  it("keeps debug reasoning off the public surface", () => {
    expect(isHiddenProviderReasoning({ visibility: "debug" })).toBe(true);
    expect(isHiddenProviderReasoning({ visibility: "timeline" })).toBe(false);
  });
});
