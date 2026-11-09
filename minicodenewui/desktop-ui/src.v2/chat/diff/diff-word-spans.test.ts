import { describe, expect, it } from "vitest";
import { buildDiffWordDecorations, type DiffWordLine } from "./diff-word-spans";

const pair = (before: string, after: string): DiffWordLine[] => [{ kind: "del", text: before }, { kind: "add", text: after }];
const spans = (before: string, after: string) => buildDiffWordDecorations(pair(before, after)).spansByLine;

describe("Source word-alt decorations", () => {
  it("keeps changed words separate from an unchanged trailing phrase", () => {
    expect(spans("hello old world", "hello new world")).toEqual([[{ start: 6, end: 9 }], [{ start: 6, end: 9 }]]);
  });

  it("joins single-character neutral separators but keeps the final change separate", () => {
    expect(spans("a.b c", "x.y z")).toEqual([[{ start: 0, end: 5 }], [{ start: 0, end: 4 }, { start: 4, end: 5 }]]);
    expect(spans("a  b tail", "x  y tail")).toEqual([
      [{ start: 0, end: 1 }, { start: 3, end: 4 }],
      [{ start: 0, end: 1 }, { start: 3, end: 4 }],
    ]);
  });

  it("uses the frozen tokenizer's multiplication, division and soft-hyphen boundaries", () => {
    expect(spans("a×b", "a×c")).toEqual([[{ start: 2, end: 3 }], [{ start: 2, end: 3 }]]);
    expect(spans("a÷b", "a÷c")).toEqual([[{ start: 2, end: 3 }], [{ start: 2, end: 3 }]]);
    expect(spans("co\u00adoperate", "co\u00adordinate")).toEqual([[{ start: 0, end: 10 }], [{ start: 0, end: 11 }]]);
  });

  it("preserves UTF-16 positions for astral symbols, CJK and extended Latin words", () => {
    expect(spans("😀 old", "😀 new")).toEqual([[{ start: 3, end: 6 }], [{ start: 3, end: 6 }]]);
    expect(spans("旧中文", "新中文")).toEqual([[{ start: 0, end: 1 }], [{ start: 0, end: 1 }]]);
    expect(spans("café", "cafè")).toEqual([[{ start: 0, end: 4 }], [{ start: 0, end: 4 }]]);
  });

  it("does not ignore case or a changed whitespace token", () => {
    expect(spans("name tail", "NAME tail")).toEqual([[{ start: 0, end: 4 }], [{ start: 0, end: 4 }]]);
    expect(spans("a\tb", "a  b")).toEqual([[{ start: 1, end: 2 }], [{ start: 1, end: 3 }]]);
  });

  it("removes EOL before the strict 1000-code-unit limit", () => {
    expect(spans("a".repeat(1000) + "\r\n", "b".repeat(1000) + "\n")).toEqual([[{ start: 0, end: 1000 }], [{ start: 0, end: 1000 }]]);
    expect(spans("a".repeat(1001), "b")).toEqual([[], []]);
    expect(spans("a", "b".repeat(1001))).toEqual([[], []]);
    expect(spans("a".repeat(1000) + "\r", "b")).toEqual([[], []]);
  });

  it("pairs only corresponding rows in the same change block and never invents pure-change pairs", () => {
    const lines = [
      { kind: "hunk", text: "@@ -100,2 +200,3 @@" },
      { kind: "del", text: "before" }, { kind: "add", text: "after" }, { kind: "add", text: "extra" },
      { kind: "context", text: "stable" }, { kind: "del", text: "removed" },
      { kind: "hunk", text: "@@ -300,0 +400 @@" }, { kind: "add", text: "new" },
    ];
    const result = buildDiffWordDecorations(lines).spansByLine;
    expect(result).toEqual([[], [{ start: 0, end: 6 }], [{ start: 0, end: 5 }], [], [], [], [], []]);
    expect(buildDiffWordDecorations([{ kind: "add", text: "only addition" }]).spansByLine).toEqual([[]]);
    expect(buildDiffWordDecorations([{ kind: "del", text: "only deletion" }]).spansByLine).toEqual([[]]);
  });

  it("keeps newline markers out of pair indices and preserves a real empty paired line", () => {
    expect(buildDiffWordDecorations([
      { kind: "del", text: "old" }, { kind: "meta", text: "\\ No newline at end of file" },
      { kind: "add", text: "new" }, { kind: "marker", text: "\\ No newline at end of file" },
    ]).spansByLine).toEqual([[{ start: 0, end: 3 }], [], [{ start: 0, end: 3 }], []]);
    expect(spans("", "new")).toEqual([[], [{ start: 0, end: 3 }]]);
  });

  it("allows exactly 2000 changed rows, then disables words and syntax for 2001", () => {
    const lines = [...Array.from({ length: 1000 }, () => ({ kind: "del", text: "old" })), ...Array.from({ length: 1000 }, () => ({ kind: "add", text: "new" }))];
    const exact = buildDiffWordDecorations(lines);
    expect(exact.usePlainText).toBe(false);
    expect(exact.spansByLine.filter(line => line.length > 0)).toHaveLength(2000);
    const tooLarge = buildDiffWordDecorations([...lines, { kind: "add", text: "extra" }]);
    expect(tooLarge.usePlainText).toBe(true);
    expect(tooLarge.spansByLine.every(line => line.length === 0)).toBe(true);
  });

  it("uses authoritative full-file counts and disables words for a false preference", () => {
    expect(buildDiffWordDecorations(pair("old", "new"), { changedLineCount: 2001 }).usePlainText).toBe(true);
    expect(buildDiffWordDecorations(pair("old", "new"), { wordDiffsEnabled: false })).toEqual({ spansByLine: [[], []], usePlainText: false });
  });

  it("limits unwindowed forced-plain bodies only above 1000 unified or split rows", () => {
    for (const counts of [{ unifiedLineCount: 1001 }, { splitLineCount: 1001 }]) {
      expect(buildDiffWordDecorations(pair("old", "new"), { ...counts, forcePlainText: true }).spansByLine).toEqual([[], []]);
      expect(buildDiffWordDecorations(pair("old", "new"), counts).spansByLine[0]).toEqual([{ start: 0, end: 3 }]);
    }
    expect(buildDiffWordDecorations(pair("old", "new"), { forcePlainText: true, unifiedLineCount: 1000, splitLineCount: 1000 }).spansByLine[0]).toEqual([{ start: 0, end: 3 }]);
    const largeContext = [...Array.from({ length: 1000 }, () => ({ kind: "context", text: "stable" })), ...pair("old", "new")];
    expect(buildDiffWordDecorations(largeContext, { forcePlainText: true }).spansByLine.every(line => line.length === 0)).toBe(true);
  });

  it("treats startingLine as an offset and totalLines as a count across both views", () => {
    const lines = [{ kind: "del", text: "old0" }, { kind: "del", text: "old1" }, { kind: "add", text: "new0" }, { kind: "add", text: "new1" }];
    const result = buildDiffWordDecorations(lines, { forcePlainText: true, startingLine: 2, totalLines: 1, unifiedLineCount: 2000 });
    expect(result.spansByLine).toEqual([[{ start: 0, end: 4 }], [], [{ start: 0, end: 4 }], []]);
    expect(buildDiffWordDecorations(lines, { forcePlainText: true, totalLines: 0 }).spansByLine).toEqual([[], [], [], []]);
    expect(buildDiffWordDecorations(lines, { startingLine: 100, totalLines: 0 }).spansByLine.every(line => line.length > 0)).toBe(true);
  });
});
