import { describe, expect, it } from "vitest";
import {
  capabilityHasDetails,
  capabilityHasInventory,
  capabilityItemNames,
  capabilityToolNames,
  formatAgentToolCounts,
  formatCapabilityPreview,
  formatCapabilitySource,
  formatDeferredCapability,
  formatExposureBreakdown,
  formatInventoryCount,
  formatMcpProxyCount,
  providerCapabilityRows,
  providerCapabilityLimitations,
  formatSkillCapability,
  mergeCapabilities,
  summarizeToolViews,
  withDerivedCapabilitySummary,
} from "./capabilities";

describe("capability payload helpers", () => {
  it("detects inventory details without requiring a summary", () => {
    expect(capabilityHasInventory({ tools: [] })).toBe(true);
    expect(capabilityHasDetails({ tools: [] })).toBe(true);
    expect(capabilityHasDetails({ summary: { tools_total: 1 } })).toBe(true);
    expect(capabilityHasDetails(undefined)).toBe(false);
  });

  it("derives totals from legacy tool lists without inventing direct exposure counts", () => {
    const capabilities = withDerivedCapabilitySummary({
      tools: [
        { function: { name: "read_file" } },
        { function: { name: "list_mcp_resources" } },
      ],
      commands: [{ name: "conversation.list" }],
      skills: [{ name: "debugging" }],
    });

    expect(capabilities?.summary).toEqual({
      tools_total: 2,
      commands: 1,
      skills: 1,
    });
    expect(formatAgentToolCounts(capabilities?.summary)).toBe("2 total");
  });

  it("derives direct, deferred, and hidden counts from tool views", () => {
    const capabilities = withDerivedCapabilitySummary({
      tool_views: [
        { name: "read_file", exposure: "core", direct: true, schema_available: true },
        { name: "tool_call", exposure: "deferred", direct: false, schema_available: true },
        { name: "mcp__secret__danger", exposure: "core", direct: true, schema_available: false },
      ],
    });

    expect(capabilities?.summary).toMatchObject({
      tools_total: 3,
      direct_tools: 1,
      core_tools: 2,
      deferred_tools: 1,
      hidden_tools: 1,
    });
    expect(formatAgentToolCounts(capabilities?.summary)).toBe("3 total / 1 direct");
    expect(formatExposureBreakdown(capabilities?.summary)).toBe("2 core / 1 deferred / 1 hidden");
  });

  it("prefers full tool views over direct schema lists when deriving totals", () => {
    const capabilities = withDerivedCapabilitySummary({
      tools: [{ function: { name: "read_file" } }],
      tool_views: [
        { name: "read_file", exposure: "core", direct: true, schema_available: true },
        { name: "tool_call", exposure: "deferred", direct: false, schema_available: true },
      ],
    });

    expect(capabilities?.summary).toMatchObject({
      tools_total: 2,
      direct_tools: 1,
      deferred_tools: 1,
    });
  });

  it("summarizes schema-unavailable tools as hidden even when direct is true", () => {
    expect(summarizeToolViews([
      { name: "read_file", exposure: "core", direct: true, schema_available: true },
      { name: "tool_call", exposure: "deferred", direct: false, schema_available: true },
      { name: "unsafe_write", exposure: "core", direct: true, schema_available: false },
    ])).toEqual({
      total: 3,
      direct: ["read_file"],
      deferred: ["tool_call"],
      hidden: ["unsafe_write"],
      core: 2,
      hasViews: true,
    });
  });

  it("merges status fallback inventory without overriding doctor data", () => {
    const merged = mergeCapabilities(
      { summary: { tools_total: 7 }, tools: [{ function: { name: "doctor_tool" } }] },
      {
        tools: [{ function: { name: "status_tool" } }],
        tool_views: [{ name: "status_tool", exposure: "core", direct: true, schema_available: true }],
        commands: [{ name: "conversation.list" }],
      },
    );

    expect(capabilityToolNames(merged?.tools)).toEqual(["doctor_tool"]);
    expect(summarizeToolViews(merged?.tool_views).direct).toEqual(["status_tool"]);
    expect(capabilityItemNames(merged?.commands)).toEqual(["conversation.list"]);
    expect(merged?.summary?.tools_total).toBe(7);
  });

  it("formats diagnostic labels for compact UI rows", () => {
    expect(formatCapabilitySource("doctor")).toBe("Doctor");
    expect(formatCapabilitySource("status")).toBe("Status fallback");
    expect(formatCapabilitySource("runtime")).toBe("Runtime");
    expect(formatDeferredCapability({ deferred_bridge: true, deferred_tools: 3 })).toBe("Ready (3)");
    expect(formatSkillCapability({ skills: 2 })).toBe("2 skills");
    expect(formatMcpProxyCount({ mcp_proxy_tools: 1 })).toBe("1 dynamic tool");
    expect(formatInventoryCount([{ name: "a" }], undefined, "command", "commands")).toBe("1 command");
    expect(formatCapabilityPreview(["a", "b", "c"], 2)).toBe("a, b, +1 more");
  });

  it("includes reasoning and media provider capabilities", () => {
    const rows = providerCapabilityRows({
      streaming: true,
      tool_calling: true,
      parallel_tool_calls: false,
      reasoning_effort: true,
      json_mode: false,
      vision: true,
      native_pdf: true,
      image_generation: false,
    });

    expect(rows).toEqual(expect.arrayContaining([
      { label: "Reasoning effort", value: "Ready", supported: true, tone: "ready" },
      { label: "Image generation", value: "Unavailable", supported: false, tone: "unavailable" },
    ]));
    expect(providerCapabilityRows({ streaming: false, tool_calling: false })).toEqual(expect.arrayContaining([
      { label: "Streaming", value: "Missing", supported: false, tone: "missing" },
      { label: "Tool calling", value: "Missing", supported: false, tone: "missing" },
    ]));
    expect(providerCapabilityLimitations({
      limitations: [
        "dedicated_image_model_requires_images_api",
        "unsupported_openai_wire_api:legacy",
      ],
    })).toEqual([
      "GPT Image models use the Images API and cannot be selected as the text/agent model",
      "Unsupported API format: legacy",
    ]);
  });
});
