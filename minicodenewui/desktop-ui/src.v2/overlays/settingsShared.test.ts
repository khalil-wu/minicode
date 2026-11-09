import { describe, expect, it } from "vitest";
import {
  canChooseApiFormat,
  defaultPromptCacheRetention,
  promptCacheRetentionAfterWireChange,
  defaultSectionForProvider,
  effectiveCustomWireApi,
  historyForUiProvider,
  providerDisplayName,
  savedOrHistorySectionForUiProvider,
  toUiProvider,
} from "./settingsShared";

describe("settingsShared explicit provider contracts", () => {
  it("uses the selected API format without endpoint inference", () => {
    expect(effectiveCustomWireApi("custom", "https://api.deepseek.com/v1", "responses")).toBe("responses");
    expect(effectiveCustomWireApi("custom", "https://gateway.example/v1", "anthropic")).toBe("anthropic");
    expect(effectiveCustomWireApi("openai", "https://gateway.example/v1", "anthropic")).toBe("chat");
    expect(effectiveCustomWireApi("anthropic", "https://gateway.example/v1", "responses")).toBe("anthropic");
  });

  it("uses the explicit backend provider without hostname classification", () => {
    expect(toUiProvider({
      provider: "openai",
      openai: { base_url: "https://api.deepseek.com/v1" },
    })).toBe("openai");
    expect(toUiProvider({ provider: "custom" })).toBe("custom");
    expect(toUiProvider({ provider: "anthropic" })).toBe("anthropic");
  });

  it("does not invent default model ids", () => {
    expect(defaultSectionForProvider("openai").model).toBe("");
    expect(defaultSectionForProvider("anthropic").model).toBe("");
    expect(defaultSectionForProvider("custom").model).toBe("");
    expect(defaultSectionForProvider("openai").wire_api).toBe("responses");
    expect(defaultSectionForProvider("anthropic").wire_api).toBe("anthropic");
    expect(defaultSectionForProvider("custom").wire_api).toBe("chat");
    expect(defaultSectionForProvider("openai").proxy_mode).toBe("inherit");
    expect(defaultSectionForProvider("anthropic").proxy_mode).toBe("inherit");
    expect(defaultSectionForProvider("custom").proxy_mode).toBe("inherit");
  });

  it("keeps Anthropic fixed to Messages and other formats explicit", () => {
    expect(canChooseApiFormat("anthropic", "")).toBe(false);
    expect(canChooseApiFormat("openai", "https://api.openai.com/v1")).toBe(true);
    expect(canChooseApiFormat("custom", "https://gateway.example/v1")).toBe(true);
  });

  it("keeps optional Responses cache retention off by default", () => {
    expect(defaultPromptCacheRetention("responses")).toBe("");
    expect(defaultPromptCacheRetention("chat")).toBe("");
  });

  it("does not erase an explicit cache preference when the wire API changes", () => {
    expect(promptCacheRetentionAfterWireChange("24h", "chat")).toBe("24h");
    expect(promptCacheRetentionAfterWireChange("in_memory", "responses")).toBe("in_memory");
  });

  it("maps provider history by its explicit provider contract", () => {
    const payload = {
      provider: "openai",
      provider_history: [
        {
          provider: "custom",
          provider_id: "custom",
          display_name: "Team gateway",
          base_url: "https://openrouter.ai/api/v1",
          model: "vendor-model",
          wire_api: "chat",
          proxy_mode: "direct" as const,
        },
        {
          provider: "openai",
          provider_id: "openai",
          display_name: "OpenAI",
          model: "configured-openai-model",
          wire_api: "responses",
          prompt_cache_retention: "24h",
        },
      ],
    };

    expect(historyForUiProvider(payload, "custom")).toHaveLength(1);
    expect(savedOrHistorySectionForUiProvider(payload, "custom")?.model).toBe("vendor-model");
    expect(providerDisplayName(savedOrHistorySectionForUiProvider(payload, "custom"))).toBe("Team gateway");
    expect(savedOrHistorySectionForUiProvider(payload, "custom")?.proxy_mode).toBe("direct");
    expect(savedOrHistorySectionForUiProvider(payload, "openai")?.prompt_cache_retention).toBe("24h");
  });
});
