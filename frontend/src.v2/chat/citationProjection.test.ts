import { describe, expect, it } from "vitest";
import {
  citationUrl,
  extractInlineCitationIndexes,
  providerCitationsToBase,
  resolveCitations,
} from "./citationProjection";

describe("citationProjection explicit citation contract", () => {
  it("accepts only HTTP citation URLs", () => {
    expect(citationUrl({ source: "", url: "https://example.com", range: [0, 0] })).toBe("https://example.com");
    expect(citationUrl({ source: "not-a-url", range: [0, 0] })).toBe("");
  });

  it("extracts unique positive inline indexes", () => {
    expect([...extractInlineCitationIndexes("See [2], [1], [2], and [0].")]).toEqual([2, 1]);
  });

  it("normalizes provider-native citations", () => {
    expect(providerCitationsToBase([
      { url: "https://www.example.com/a", title: "Source A", range: [1, 4] },
    ])).toEqual([
      {
        source: "https://www.example.com/a",
        url: "https://www.example.com/a",
        title: "Source A",
        providerNative: true,
        label: "example.com",
        range: [1, 4],
      },
    ]);
  });

  it("keeps provider-native document locations without fabricating web URLs", () => {
    expect(providerCitationsToBase([
      {
        source: "anthropic:document:abc123",
        title: "Architecture notes",
        label: "Pages 2–3",
        location_type: "page_location",
        range: [2, 3],
      },
    ])).toEqual([
      {
        source: "anthropic:document:abc123",
        title: "Architecture notes",
        label: "Pages 2–3",
        locationType: "page_location",
        providerNative: true,
        range: [2, 3],
      },
    ]);
  });

  it("binds only backend or provider citations to model-authored indexes", () => {
    const citations = resolveCitations(
      [{ source: "https://backend.example/one", range: [0, 0] }],
      [],
      "Answer [1] [2]",
      [
        { url: "https://provider.example/one", title: "One" },
        { url: "https://provider.example/two", title: "Two" },
      ],
    );

    expect(citations).toEqual([
      expect.objectContaining({ url: "https://provider.example/one" }),
      expect.objectContaining({ url: "https://provider.example/two" }),
    ]);
  });

  it("does not parse tool output to fabricate citation ownership", () => {
    const citations = resolveCitations(undefined, [{
      type: "tool_call",
      record: {
        id: "search-1",
        name: "web_search",
        args: {},
        status: "success",
        summary: "[1] Result\nURL: https://tool-output.example",
        startedAt: 1,
      },
    }], "Answer [1]");

    expect(citations).toEqual([]);
  });

  it("drops citations when the answer has no inline markers", () => {
    expect(resolveCitations(
      [{ source: "https://example.com", range: [0, 0] }],
      [],
      "No markers",
    )).toEqual([]);
  });

  it("keeps provider-native sources when the provider emits no inline markers", () => {
    expect(resolveCitations(
      undefined,
      [],
      "Provider-authored answer without numeric markers.",
      [{ url: "https://provider.example/source", title: "Provider source" }],
    )).toEqual([
      expect.objectContaining({
        url: "https://provider.example/source",
        providerNative: true,
      }),
    ]);
  });
});
