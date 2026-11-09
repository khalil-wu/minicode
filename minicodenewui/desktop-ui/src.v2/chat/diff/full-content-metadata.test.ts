import { describe, expect, it } from "vitest";
import { buildFullDiffContext } from "./full-content-metadata";

describe("Source full-content metadata reconstruction", () => {
  it("uses actual prefix/body/trailing lines without changing original patch indices", () => {
    const patch = "@@ -2,2 +2,2 @@\n same\n-old\n+new\n";
    const oldLines = ["prefix\n", "same\n", "old\n", "tail\n"], newLines = ["prefix\n", "same\n", "new\n", "tail\n"];
    const result = buildFullDiffContext(patch, oldLines, newLines)!;
    expect(result.hunks[0].before).toEqual([{ kind: "context", text: "prefix", oldLine: 1, newLine: 1, unifiedIndex: 0 }]);
    expect(result.hunks[0].body.map(line => [line.kind, line.lineIndex, line.unifiedIndex, line.oldLine, line.newLine])).toEqual([
      ["context", 0, 1, 2, 2], ["del", 1, 2, 3, undefined], ["add", 2, 3, undefined, 3],
    ]);
    expect(result.trailing[0]).toMatchObject({ text: "tail", oldLine: 4, newLine: 4, unifiedIndex: 4 });
    expect(result).toMatchObject({ isPartial: false, splitLineCount: 4, unifiedLineCount: 5 });
    expect(result.hunks[0]).toMatchObject({ collapsedBefore: 1, splitLineStart: 1, unifiedLineStart: 1, splitLineCount: 2, unifiedLineCount: 3 });
    expect(oldLines).toEqual(["prefix\n", "same\n", "old\n", "tail\n"]);
  });

  it.each([
    [["wrong\n", "old\n", "tail\n"], ["prefix\n", "new\n", "tail\n"]],
    [["prefix\n", "different\n", "tail\n"], ["prefix\n", "new\n", "tail\n"]],
    [["prefix\n", "old\n", "old tail\n"], ["prefix\n", "new\n", "new tail\n"]],
    [["prefix\n", "old\n", "tail\n"], ["prefix\n", "new\n"]],
  ])("rejects mismatched carried or collapsed bytes %j", (oldLines, newLines) => {
    expect(buildFullDiffContext("@@ -2 +2 @@\n-old\n+new\n", oldLines, newLines)).toBeNull();
  });

  it("rejects unequal leading gaps and keeps Source's zero-count header behavior", () => {
    expect(buildFullDiffContext("@@ -2 +3 @@\n-old\n+new\n", ["p\n", "old\n"], ["p\n", "extra\n", "new\n"])).toBeNull();
    expect(buildFullDiffContext("@@ -2,0 +3 @@\n+new\n", ["a\n", "b\n"], ["a\n", "b\n", "new\n"])).toBeNull();
    const beginning = buildFullDiffContext("@@ -0,0 +1 @@\n+new\n", ["old\n"], ["new\n", "old\n"])!;
    expect(beginning.hunks[0]).toMatchObject({ oldStart: 0, oldCount: 0, newStart: 1, newCount: 1 });
    expect(beginning.trailing[0]).toMatchObject({ oldLine: 1, newLine: 2 });
  });

  it("compares Source's ASCII whitespace set only when requested and renders real new bytes", () => {
    const patch = "@@ -2 +2 @@\n-old value\n+new value\n";
    const oldLines = ["  prefix\n", "old\tvalue\n", "tail\n"], newLines = ["prefix\n", " new value \n", "tail \n"];
    expect(buildFullDiffContext(patch, oldLines, newLines)).toBeNull();
    const result = buildFullDiffContext(patch, oldLines, newLines, true)!;
    expect(result.hunks[0].body.map(line => line.text)).toEqual(["old\tvalue", " new value "]);
    expect(result.hunks[0].before[0].text).toBe("prefix");
    expect(result.trailing[0].text).toBe("tail ");
  });

  it("preserves CRLF and no-final-newline semantics instead of normalizing the snapshot", () => {
    const crlf = "@@ -1 +1 @@\n-old\r\n+new\r\n";
    expect(buildFullDiffContext(crlf, ["old\r\n"], ["new\r\n"])!.hunks[0].body.map(line => line.text)).toEqual(["old", "new"]);
    expect(buildFullDiffContext(crlf, ["old\n"], ["new\n"])).toBeNull();
    const noEol = "@@ -1 +1 @@\n-old\n\\ No newline at end of file\n+new\n\\ No newline at end of file\n";
    expect(buildFullDiffContext(noEol, ["old"], ["new"])!.hunks[0].body.map(line => line.text)).toEqual(["old", "new"]);
    expect(buildFullDiffContext(noEol, ["old\n"], ["new"])).toBeNull();
    const noCrLf = "@@ -1 +1 @@\n-old\r\n\\ No newline at end of file\n+new\r\n\\ No newline at end of file\n";
    expect(buildFullDiffContext(noCrLf, ["old"], ["new"])!.hunks[0].body.map(line => line.text)).toEqual(["old", "new"]);
    expect(buildFullDiffContext(noCrLf, ["old\r"], ["new\r"])).toBeNull();
    expect(buildFullDiffContext("@@ -1 +1 @@\n-old\n+new", ["old\n"], ["new"])!.hunks[0].body[1].text).toBe("new");
  });

  it("keeps each real hunk and separates its carried context from omitted full-file context", () => {
    const patch = "@@ -2 +2 @@\n-b\n+B\n@@ -5 +5 @@\n-e\n+E\n";
    const result = buildFullDiffContext(patch, ["a\n", "b\n", "c\n", "d\n", "e\n", "f\n"], ["a\n", "B\n", "c\n", "d\n", "E\n", "f\n"])!;
    expect(result.hunks.map(hunk => hunk.before.map(line => line.text))).toEqual([["a"], ["c", "d"]]);
    expect(result.hunks[1].body.map(line => [line.lineIndex, line.unifiedIndex])).toEqual([[2, 5], [3, 6]]);
    expect(result.trailing.map(line => line.text)).toEqual(["f"]);
    expect(result).toMatchObject({ splitLineCount: 6, unifiedLineCount: 8 });
  });
});
