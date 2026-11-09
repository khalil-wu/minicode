import { describe, expect, it } from "vitest";
import { buildFullDiffContext } from "./full-content-metadata";
import { displayFullDiffContext, expandDiffContext } from "./full-context-display";

const prefix = Array.from({ length: 81 }, (_, index) => `before ${index + 1}\n`);
const tail = Array.from({ length: 45 }, (_, index) => `tail ${index + 1}\n`);
const context = () => buildFullDiffContext("@@ -82 +82 @@\n-old\n+new\n", [...prefix, "old\n", ...tail], [...prefix, "new\n", ...tail])!;

describe("Source complete context expansion", () => {
  it("keeps omitted text collapsed without changing patch comment coordinates", () => {
    const shown = displayFullDiffContext(context(), {});
    expect(shown.lines.filter(line => line.kind === "context")).toEqual([]);
    expect([...shown.gaps.values()]).toEqual([
      { key: "before:0", remaining: 81, allowUp: false, allowDown: true },
      { key: "trailing", remaining: 45, allowUp: true, allowDown: false },
    ]);
    expect(shown.lines.find(line => line.kind === "add")).toMatchObject({ lineIndex: 1, newLine: 82, unifiedIndex: 82 });
  });

  it("opens twenty from the selected edge, both edges on text click, and all for Shift", () => {
    const first = expandDiffContext({}, "before:0", "down");
    const shown = displayFullDiffContext(context(), first);
    expect(shown.lines.filter(line => line.kind === "context").map(line => line.text)).toEqual(prefix.slice(-20).map(line => line.trimEnd()));
    expect([...shown.gaps.values()][0].remaining).toBe(61);
    const both = displayFullDiffContext(context(), expandDiffContext(first, "before:0", "both"));
    expect([...both.gaps.values()][0].remaining).toBe(21);
    expect(both.lines.filter(line => line.kind === "context")).toHaveLength(60);
    const all = displayFullDiffContext(context(), expandDiffContext(first, "before:0", "all"));
    expect([...all.gaps.values()][0].remaining).toBe(0);
    expect(all.lines.filter(line => line.kind === "context")).toHaveLength(81);
    expect(all.lines.filter(line => line.kind !== "hunk").map(line => line.unifiedIndex)).toEqual(Array.from({ length: 83 }, (_, index) => index));
  });

  it("expands trailing lines toward the end without exposing filtered imports as context", () => {
    const full = context();
    const shown = displayFullDiffContext(full, expandDiffContext({}, "trailing", "up"));
    expect(shown.lines.filter(line => line.kind === "context").map(line => line.text)).toEqual(tail.slice(0, 20).map(line => line.trimEnd()));
    const partial = { ...full, isPartial: true, lines: full.hunks.flatMap(hunk => [hunk.header, ...hunk.body]) };
    expect(displayFullDiffContext(partial, expandDiffContext({}, "before:0", "all"))).toEqual({ lines: partial.lines, gaps: new Map() });
  });
});
