import { describe, expect, it } from "vitest";
import { diffCommentHunk, diffCommentPosition, parsePositionedDiff } from "./diff-comment-lines";

describe("Diff comment file coordinates", () => {
  it("retains both real counters through no-newline markers between a deletion and addition", () => {
    const lines = parsePositionedDiff("@@ -100 +200 @@\n-old\n\\ No newline at end of file\n+new\n\\ No newline at end of file");
    expect(lines.find(line => line.kind === "del")?.oldLine).toBe(100);
    expect(lines.find(line => line.kind === "add")?.newLine).toBe(200);
  });
  it("keeps same-side reverse selections sorted and cross-side endpoints in their authored direction", () => {
    expect(diffCommentPosition("a.ts", { start: { side: "right", line: 205 }, end: { side: "right", line: 200 } }))
      .toEqual({ path: "a.ts", side: "right", line: 205, start_line: 200 });
    expect(diffCommentPosition("a.ts", { start: { side: "right", line: 200 }, end: { side: "left", line: 100 } }))
      .toEqual({ path: "a.ts", side: "left", line: 100, start_side: "right", start_line: 200 });
  });
  it("sends selected actual partial hunks with Source CRLF and no invented gap text", () => {
    const patch = "diff --git a/a.ts b/a.ts\n@@ -100 +200 @@ first\n-old\n+new\n@@ -500 +600 @@ second\n-later old\n+later new";
    expect(diffCommentHunk(patch, { path: "a.ts", side: "right", line: 600 }))
      .toBe("@@ -500 +600 @@ second\r\n-later old\r\n+later new");
    expect(diffCommentHunk(patch, { path: "a.ts", side: "right", line: 600, start_side: "left", start_line: 100 }))
      .toBe("@@ -100 +200 @@ first\r\n-old\r\n+new\r\n@@ -500 +600 @@ second\r\n-later old\r\n+later new");
  });
  it("does not manufacture a location or hunk when a stored preview has no source coordinates", () => {
    expect(parsePositionedDiff("-old\n+new").filter(line => line.kind === "add").map(line => line.newLine)).toEqual([undefined]);
    expect(diffCommentHunk("-old\n+new", { path: "a.ts", side: "right", line: 1 })).toBeUndefined();
  });
});
