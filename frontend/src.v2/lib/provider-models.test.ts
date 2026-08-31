import { describe, expect, it } from "vitest";
import { selectableModelsForProvider } from "./provider-models";

describe("selectableModelsForProvider", () => {
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
