import { describe, expect, it } from "vitest";
import {
  agentEditUndoEdit,
  parsePatchHunks,
  reviewAgentEdits,
  revertAgentEditBlock,
  revertAgentEditBlocks,
  type AgentEditTextEdit,
} from "./agent-edit-review";

// Patches below are verbatim output of backend/diff/unified.py (difflib, 3 context lines).
const header = "diff --git a/src/app.py b/src/app.py\n--- a/src/app.py\n+++ b/src/app.py\n";
const lines = (count: number, edit: (text: string, index: number) => string | string[] = (text) => text) =>
  Array.from({ length: count }, (_, index) => edit(`line${index + 1}`, index + 1)).flat().map((text) => `${text}\n`).join("");

const MULTI_OLD = lines(20);
const MULTI_NEW = lines(20, (text, index) => (
  index === 3 ? "LINE3" : index === 15 ? [text, "inserted"] : index === 18 ? [] : text
));
const MULTI_PATCH = `${header}@@ -1,6 +1,6 @@\n line1\n line2\n-line3\n+LINE3\n line4\n line5\n line6\n`
  + "@@ -13,8 +13,8 @@\n line13\n line14\n line15\n+inserted\n line16\n line17\n-line18\n line19\n line20\n";

describe("agent edit review", () => {
  it("splits hunks into contiguous change blocks positioned in the current buffer", () => {
    const review = reviewAgentEdits(MULTI_PATCH, MULTI_NEW);
    expect(review.unmatchedHunks).toBe(0);
    expect(review.blocks.map(({ line, added, removed }) => ({ line, added, removed }))).toEqual([
      { line: 3, added: ["LINE3"], removed: ["line3"] },
      { line: 16, added: ["inserted"], removed: [] },
      { line: 19, added: [], removed: ["line18"] },
    ]);
    expect(new Set(review.blocks.map((block) => block.key)).size).toBe(3);
  });

  it("reverts every block back to the agent's baseline", () => {
    const { blocks } = reviewAgentEdits(MULTI_PATCH, MULTI_NEW);
    expect(revertAgentEditBlocks(MULTI_NEW, blocks)).toBe(MULTI_OLD);
  });

  it("reverts one block and leaves the others reviewable", () => {
    const { blocks } = reviewAgentEdits(MULTI_PATCH, MULTI_NEW);
    const reverted = revertAgentEditBlock(MULTI_NEW, blocks[1]);
    expect(reverted).toBe(MULTI_NEW.replace("inserted\n", ""));
    const remaining = reviewAgentEdits(MULTI_PATCH, reverted!);
    expect(remaining.blocks.map((block) => block.added)).toEqual([["LINE3"]]);
    expect(remaining.unmatchedHunks).toBe(1);
  });

  it("follows the agent's text when the user shifts lines above it", () => {
    const shifted = `# header\n# more\n${MULTI_NEW}`;
    const { blocks, unmatchedHunks } = reviewAgentEdits(MULTI_PATCH, shifted);
    expect(unmatchedHunks).toBe(0);
    expect(blocks.map((block) => block.line)).toEqual([5, 18, 21]);
    expect(revertAgentEditBlocks(shifted, blocks)).toBe(`# header\n# more\n${MULTI_OLD}`);
  });

  it("drops a hunk whose post-change text the user already edited", () => {
    const edited = MULTI_NEW.replace("LINE3\n", "user text\n");
    const review = reviewAgentEdits(MULTI_PATCH, edited);
    expect(review.unmatchedHunks).toBe(1);
    expect(review.blocks.map((block) => block.line)).toEqual([16, 19]);
  });

  it("shows nothing once the buffer is back at the baseline", () => {
    expect(reviewAgentEdits(MULTI_PATCH, MULTI_OLD).blocks).toEqual([]);
  });

  it("preserves CRLF bytes on revert", () => {
    const patch = `${header}@@ -1,4 +1,4 @@\n a\r\n-b\r\n+B\r\n c\r\n d\r\n`;
    const current = "a\r\nB\r\nc\r\nd\r\n";
    const { blocks } = reviewAgentEdits(patch, current);
    expect(blocks).toHaveLength(1);
    expect(blocks[0]).toMatchObject({ line: 2, added: ["B"], removed: ["b"] });
    expect(revertAgentEditBlock(current, blocks[0])).toBe("a\r\nb\r\nc\r\nd\r\n");
  });

  it("reverts a new file to empty and a pure deletion to its removed lines", () => {
    const created = reviewAgentEdits(`${header}@@ -0,0 +1,2 @@\n+x\n+y\n`, "x\ny\n");
    expect(created.blocks).toEqual([expect.objectContaining({ line: 1, added: ["x", "y"], removed: [] })]);
    expect(revertAgentEditBlock("x\ny\n", created.blocks[0])).toBe("");

    const deletion = reviewAgentEdits(
      `${header}@@ -1,5 +1,3 @@\n keep1\n keep2\n-drop1\n-drop2\n keep3\n`,
      "keep1\nkeep2\nkeep3\n",
    );
    expect(deletion.blocks).toEqual([expect.objectContaining({ line: 3, added: [], removed: ["drop1", "drop2"] })]);
    expect(revertAgentEditBlock("keep1\nkeep2\nkeep3\n", deletion.blocks[0])).toBe("keep1\nkeep2\ndrop1\ndrop2\nkeep3\n");
  });

  it("keeps the final-newline state of the file", () => {
    const noEol = `${header}@@ -1,2 +1,2 @@\n a\n-b\n\\ No newline at end of file\n+c\n\\ No newline at end of file\n`;
    const review = reviewAgentEdits(noEol, "a\nc");
    expect(review.blocks).toEqual([expect.objectContaining({ line: 2, added: ["c"], removed: ["b"] })]);
    expect(revertAgentEditBlock("a\nc", review.blocks[0])).toBe("a\nb");

    const appended = reviewAgentEdits(`${header}@@ -1,2 +1,3 @@\n a\n b\n+c\n`, "a\nb\nc\n");
    expect(revertAgentEditBlock("a\nb\nc\n", appended.blocks[0])).toBe("a\nb\n");
  });

  it("refuses to revert text that no longer matches", () => {
    const { blocks } = reviewAgentEdits(MULTI_PATCH, MULTI_NEW);
    expect(revertAgentEditBlock(MULTI_NEW.replace("LINE3", "LINE-3"), blocks[0])).toBeNull();
  });

  it("treats header-like content lines inside a counted hunk as content", () => {
    const patch = `${header}@@ -1,2 +1,2 @@\n----x\n+++-y\n keep\n`;
    const [hunk] = parsePatchHunks(patch);
    expect(hunk.lines).toEqual([
      { kind: "del", text: "---x" },
      { kind: "add", text: "++-y" },
      { kind: "context", text: "keep" },
    ]);
  });
});

// Applies an edit the way an editor model does: lines split on "\n", a trailing
// newline producing a final empty line.
const applyModelEdit = (content: string, edit: AgentEditTextEdit): string => {
  const modelLines = content.split("\n");
  const offset = (lineNumber: number, column: number) =>
    modelLines.slice(0, lineNumber - 1).reduce((sum, line) => sum + line.length + 1, 0) + column - 1;
  const { range } = edit;
  return content.slice(0, offset(range.startLineNumber, range.startColumn))
    + edit.text
    + content.slice(offset(range.endLineNumber, range.endColumn));
};

describe("agent edit undo as an editor edit", () => {
  const cases: Array<[string, string, string]> = [
    ["multi", MULTI_PATCH, MULTI_NEW],
    ["new file", `${header}@@ -0,0 +1,2 @@\n+x\n+y\n`, "x\ny\n"],
    ["deletion", `${header}@@ -1,5 +1,3 @@\n keep1\n keep2\n-drop1\n-drop2\n keep3\n`, "keep1\nkeep2\nkeep3\n"],
    ["no final newline", `${header}@@ -1,2 +1,2 @@\n a\n-b\n\\ No newline at end of file\n+c\n\\ No newline at end of file\n`, "a\nc"],
    ["append", `${header}@@ -1,2 +1,3 @@\n a\n b\n+c\n`, "a\nb\nc\n"],
    ["append without final newline", `${header}@@ -1,2 +1,3 @@\n a\n-b\n\\ No newline at end of file\n+b\n+c\n\\ No newline at end of file\n`, "a\nb\nc"],
    ["deleted tail of an unterminated file", `${header}@@ -1,3 +1 @@\n-keep\n-x\n-y\n\\ No newline at end of file\n+keep\n\\ No newline at end of file\n`, "keep"],
    ["deleted tail", `${header}@@ -1,3 +1 @@\n keep\n-x\n-y\n`, "keep\n"],
  ];

  it.each(cases)("matches the string revert for %s", (_name, patch, content) => {
    const { blocks, unmatchedHunks } = reviewAgentEdits(patch, content);
    expect(unmatchedHunks).toBe(0);
    expect(blocks.length).toBeGreaterThan(0);
    const modelLines = content.split("\n");
    for (const block of blocks) {
      const edit = agentEditUndoEdit(block, modelLines.length, (lineNumber) => modelLines[lineNumber - 1].length + 1, "\n");
      expect(applyModelEdit(content, edit)).toBe(revertAgentEditBlock(content, block));
    }
  });
});
