import { describe, expect, it } from "vitest";
import { parseUnifiedDiffToOriginalModified } from "./MonacoDiffView";
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
});
