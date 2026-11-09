/* @vitest-environment jsdom */
import { describe, expect, it } from "vitest";
import { parseUnifiedDiffExcerpt, parseUnifiedDiffToOriginalModified } from "./MonacoDiffView";
import { countUnifiedDiffLines, parseUnifiedDiffLines } from "../lib/unified-diff";
import { summarizeTurnDiff } from "../lib/turn-diff";

const patch = [
  "diff --git a/sample.txt b/sample.txt",
  "--- a/sample.txt",
  "+++ b/sample.txt",
  "@@ -1 +1 @@",
  "--- before",
  "\\ No newline at end of file",
  "+++ after",
  "",
].join("\n");

describe("shared diff interpretation", () => {
  it("keeps real source offsets and a visible gap between distant excerpts", () => {
    const patch = "--- a/file.ts\n+++ b/file.ts\n@@ -100,2 +120,2 @@\n keep\n-old\n+new\n@@ -500 +520 @@\n-later old\n+later new\n";
    const excerpt = parseUnifiedDiffExcerpt(patch);
    expect(excerpt.originalLines).toEqual([100, 101, null, 500]);
    expect(excerpt.modifiedLines).toEqual([120, 121, null, 520]);
    expect(excerpt.original).toContain("⋯ 中间省略 398 行 ⋯");
    expect(excerpt.modified).toContain("⋯ 中间省略 398 行 ⋯");
  });
  it("retains header-shaped content and the final-newline difference in Monaco", () => {
    expect(parseUnifiedDiffToOriginalModified(patch)).toEqual({
      filePath: "sample.txt", original: "-- before", modified: "++ after\n",
    });
    expect(countUnifiedDiffLines(patch)).toEqual({ plus: 1, minus: 1 });
    expect(parseUnifiedDiffLines(patch).filter((line) => line.kind === "add")).toEqual([
      { kind: "add", text: "+++ after" },
    ]);
  });

  it("keeps turn totals in agreement with the displayed changes", () => {
    const summary = summarizeTurnDiff({ threadId: "thread-audit", turnId: "turn-audit", diff: patch, updatedAt: 0 });
    expect(summary?.additions).toBe(1);
    expect(summary?.deletions).toBe(1);
  });

  it("does not put file metadata or newline markers into Monaco content", () => {
    const added = [
      "diff --git a/new.txt b/new.txt", "new file mode 100644", "index 0000000..1234567",
      "--- /dev/null", "+++ b/new.txt", "@@ -0,0 +1 @@", "+value", "\\ No newline at end of file", "",
    ].join("\n");
    expect(parseUnifiedDiffToOriginalModified(added)).toEqual({
      filePath: "new.txt", original: "", modified: "value",
    });
  });

  it("resets hunk classification at each subsequent file header", () => {
    const second = patch.replaceAll("sample.txt", "second.txt");
    expect(countUnifiedDiffLines(patch + second)).toEqual({ plus: 2, minus: 2 });
  });
  it("uses the destination identity for renames and the removed identity for deletions", () => {
    const rename = "diff --git a/old.ts b/new.ts\nrename from old.ts\nrename to new.ts\n--- a/old.ts\n+++ b/new.ts\n@@ -1 +1 @@\n-old\n+new\n";
    const deleted = "diff --git a/deleted.ts b/deleted.ts\n--- a/deleted.ts\n+++ /dev/null\n@@ -1 +0,0 @@\n-old\n";
    expect(parseUnifiedDiffToOriginalModified(rename).filePath).toBe("new.ts");
    expect(parseUnifiedDiffToOriginalModified(deleted).filePath).toBe("deleted.ts");
    expect(summarizeTurnDiff({ threadId: "A", turnId: "T", updatedAt: 0, diff: rename })?.files[0]).toMatchObject({ path: "new.ts", oldPath: "old.ts" });
    expect(summarizeTurnDiff({ threadId: "A", turnId: "T", updatedAt: 0, diff: deleted })?.files[0]).not.toHaveProperty("oldPath");
  });
  it("preserves CRLF and final-newline markers in the displayed source sides", () => {
    const patch = "--- a/file.ts\n+++ b/file.ts\n@@ -1,2 +1,2 @@\n keep\r\n-old\r\n+new\n\\ No newline at end of file\n";
    expect(parseUnifiedDiffToOriginalModified(patch)).toEqual({ filePath: "file.ts", original: "keep\r\nold\r\n", modified: "keep\r\nnew" });
  });
});
