import { describe, expect, it } from "vitest";
import { selectableModelsForProvider } from "./provider-models";

describe("selectableModelsForProvider", () => {
  it.each(["configured", "extension"])("keeps the backend's actual %s catalog selectable", (source) => {
    expect(selectableModelsForProvider(["audit-model-a", "audit-model-b"], "audit-model-a", "custom", source))
      .toEqual(["audit-model-a", "audit-model-b"]);
  });

  it.each(["fallback", "unavailable", undefined])("keeps an unverified custom catalog scoped to the selected model: %s", (source) => {
    expect(selectableModelsForProvider(["stale-model", "gateway-model"], "gateway-model", "custom", source)).toEqual(["gateway-model"]);
  });

  it("keeps a discovered custom-provider model list selectable", () => {
    expect(selectableModelsForProvider(
      ["freebbe-fast", "freebbe-reasoning"],
      "freebbe-fast",
      "custom",
      "live",
    )).toEqual(["freebbe-fast", "freebbe-reasoning"]);
  });

  it("falls back to the current model when no model list is available", () => {
    expect(selectableModelsForProvider([], "gateway-model", "custom", "")).toEqual(["gateway-model"]);
  });

  it("deduplicates a live list and preserves the current model", () => {
    expect(selectableModelsForProvider(["a", "a", "b"], "missing", "custom", "live")).toEqual(["missing", "a", "b"]);
  });
});
