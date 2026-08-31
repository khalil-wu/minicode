import { describe, expect, it } from "vitest";

import { fuzzyFilter, fuzzyScore } from "./fuzzy-match";

describe("fuzzyScore whitespace handling", () => {
  it("treats whitespace-only query as empty query (score 0)", () => {
    expect(fuzzyScore("   ", "anything")).toBe(0);
    expect(fuzzyScore("\t\n", "anything")).toBe(0);
  });

  it("matches the same as the trimmed query with surrounding whitespace", () => {
    expect(fuzzyScore("  run  ", "runner")).not.toBeNull();
    expect(fuzzyScore("  run  ", "runner")).toBe(fuzzyScore("run", "runner"));
  });

  it("does not mutate target semantics: internal whitespace still must match", () => {
    // Whitespace inside the query is preserved, only leading/trailing trimmed.
    expect(fuzzyScore(" a b ", "a b")).not.toBeNull();
  });
});

describe("fuzzyFilter whitespace handling", () => {
  const items = ["open file", "run command", "open terminal"];

  it("returns all items unchanged for whitespace-only query", () => {
    expect(fuzzyFilter(items, "   ", (s) => s)).toEqual(items);
  });

  it("keeps matches when query has leading/trailing whitespace", () => {
    expect(fuzzyFilter(items, "  open  ", (s) => s)).toEqual([
      "open file",
      "open terminal",
    ]);
  });

  it("does not modify the original items array or item text", () => {
    const original = ["  open file  ", "run"];
    const snapshot = [...original];
    fuzzyFilter(original, " open ", (s) => s);
    expect(original).toEqual(snapshot);
  });

  it("preserves original order for equal scores", () => {
    const result = fuzzyFilter(["ab", "ab"], "  ab  ", (s) => s);
    expect(result).toEqual(["ab", "ab"]);
  });
});
