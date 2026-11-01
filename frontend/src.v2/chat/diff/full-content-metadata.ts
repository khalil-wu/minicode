import { parsePatch } from "diff";
import { parsePositionedDiff, type DiffLine } from "../../lib/diff-comment-lines";

/** Patch indices remain unchanged; complete-file reading order is separate. */
export interface FullDiffLine extends DiffLine { unifiedIndex?: number }

export interface FullDiffHunk {
  index: number;
  header: FullDiffLine;
  before: FullDiffLine[];
  body: FullDiffLine[];
  oldStart: number;
  oldCount: number;
  newStart: number;
  newCount: number;
  collapsedBefore: number;
  splitLineStart: number;
  unifiedLineStart: number;
  splitLineCount: number;
  unifiedLineCount: number;
}

export interface FullDiffContext {
  hunks: FullDiffHunk[];
  trailing: FullDiffLine[];
  lines: FullDiffLine[];
  oldLines: string[];
  newLines: string[];
  isPartial: boolean;
  fileType: "changed" | "new" | "deleted";
  splitLineCount: number;
  unifiedLineCount: number;
}

type PatchContentLine = { kind: "context" | "add" | "del"; value: string };
const sameLine = (left: string, right: string, ignoreWhitespace: boolean) => ignoreWhitespace
  ? left.replace(/[ \t\r\n\f\v]/g, "") === right.replace(/[ \t\r\n\f\v]/g, "")
  : left === right;

/** Source full-content-metadata compares carried EOL bytes; display text omits EOL. */
export const diffLineText = (line: string) => line.replace(/\r?\n$/, "");

function carriedLines(lines: string[], terminalNewline: boolean): PatchContentLine[] {
  const content: PatchContentLine[] = [];
  for (let index = 0; index < lines.length; index++) {
    const line = lines[index];
    if (line.startsWith("\\")) {
      const previous = content[content.length - 1];
      previous.value = previous.value.replace(/\r?\n$/, "");
    } else {
      const value = line.slice(1), carriedEol = terminalNewline || index < lines.length - 1 || value === "";
      content.push({ kind: line.startsWith("+") ? "add" : line.startsWith("-") ? "del" : "context", value: value + (carriedEol ? "\n" : "") });
    }
  }
  return content;
}

function contextLines(oldLines: string[], newLines: string[], oldStart: number, newStart: number,
  count: number, unifiedStart: number, ignoreWhitespace: boolean): FullDiffLine[] | null {
  const lines: FullDiffLine[] = [];
  for (let offset = 0; offset < count; offset++) {
    const oldText = oldLines[oldStart + offset], newText = newLines[newStart + offset];
    if (oldText == null || newText == null || !sameLine(oldText, newText, ignoreWhitespace)) return null;
    lines.push({ kind: "context", text: diffLineText(newText), oldLine: oldStart + offset + 1,
      newLine: newStart + offset + 1, unifiedIndex: unifiedStart + offset });
  }
  return lines;
}

/** Count the existing change blocks, without computing another diff. */
export function splitHunkLineCount(lines: readonly DiffLine[]): number {
  let total = 0, additions = 0, deletions = 0;
  for (const line of lines) {
    if (line.kind === "context") { total += Math.max(additions, deletions) + 1; additions = 0; deletions = 0; }
    else if (line.kind === "add") additions++;
    else if (line.kind === "del") deletions++;
  }
  return total + Math.max(additions, deletions);
}

/** Build full-file hunk context from the patch plus both file contents; no new diff or disk read. */
export function buildFullDiffContext(patch: string, oldLines: string[], newLines: string[], ignoreWhitespace = false): FullDiffContext | null {
  const [parsed] = parsePatch(patch);
  if (!parsed) return null;
  const positioned = parsePositionedDiff(patch);
  const headers = positioned.filter(line => line.kind === "hunk");
  const bodies = parsed.hunks.map(() => [] as DiffLine[]);
  for (const line of positioned) if (line.hunkIndex !== undefined && line.lineIndex !== undefined) bodies[line.hunkIndex].push(line);
  const hunks: FullDiffHunk[] = [];
  let oldEnd = 0, newEnd = 0, splitLineCount = 0, unifiedLineCount = 0;
  for (let index = 0; index < parsed.hunks.length; index++) {
    const hunk = parsed.hunks[index], oldLineStart = headers[index].oldLine!, newLineStart = headers[index].newLine!;
    // A hunk header may carry a zero-count start that jsdiff normalizes away;
    // use the original coordinates for the exact range checks below.
    const oldStart = Math.max(oldLineStart - 1, 0), newStart = Math.max(newLineStart - 1, 0);
    const gap = newStart - newEnd, oldGap = oldStart - oldEnd;
    if (gap < 0 || oldGap < 0 || gap !== oldGap || newStart + hunk.newLines > newLines.length || oldStart + hunk.oldLines > oldLines.length) return null;
    const before = contextLines(oldLines, newLines, oldEnd, newEnd, gap, unifiedLineCount, ignoreWhitespace);
    if (before === null) return null;
    const carried = carriedLines(hunk.lines, index < parsed.hunks.length - 1 || patch.endsWith("\n")), body: FullDiffLine[] = [];
    let oldIndex = oldStart, newIndex = newStart;
    for (let offset = 0; offset < carried.length; offset++) {
      const line = carried[offset];
      if (line.kind !== "add" && (oldLines[oldIndex] == null || !sameLine(line.value, oldLines[oldIndex], ignoreWhitespace))) return null;
      if (line.kind !== "del" && (newLines[newIndex] == null || !sameLine(line.value, newLines[newIndex], ignoreWhitespace))) return null;
      body.push({ ...bodies[index][offset], kind: line.kind,
        text: diffLineText(line.kind === "del" ? oldLines[oldIndex] : newLines[newIndex]),
        oldLine: line.kind === "add" ? undefined : oldIndex + 1, newLine: line.kind === "del" ? undefined : newIndex + 1,
        unifiedIndex: unifiedLineCount + gap + offset });
      if (line.kind !== "add") oldIndex++;
      if (line.kind !== "del") newIndex++;
    }
    const splitCount = splitHunkLineCount(body);
    hunks.push({ index, header: { ...headers[index], oldGap: gap, newGap: gap }, before, body,
      oldStart: oldLineStart, oldCount: hunk.oldLines, newStart: newLineStart, newCount: hunk.newLines,
      collapsedBefore: gap, splitLineStart: splitLineCount + gap, unifiedLineStart: unifiedLineCount + gap,
      splitLineCount: splitCount, unifiedLineCount: body.length });
    splitLineCount += gap + splitCount;
    unifiedLineCount += gap + body.length;
    oldEnd = oldStart + hunk.oldLines;
    newEnd = newStart + hunk.newLines;
  }
  let trailing: FullDiffLine[] = [];
  if (hunks.length > 0) {
    const newCount = newLines.length - newEnd, oldCount = oldLines.length - oldEnd;
    if (newCount < 0 || oldCount < 0 || newCount !== oldCount) return null;
    const lines = contextLines(oldLines, newLines, oldEnd, newEnd, newCount, unifiedLineCount, ignoreWhitespace);
    if (lines === null) return null;
    trailing = lines;
    splitLineCount += newCount;
    unifiedLineCount += newCount;
  }
  return { hunks, trailing, lines: [...hunks.flatMap(hunk => [...hunk.before, hunk.header, ...hunk.body]), ...trailing],
    oldLines, newLines, isPartial: false, fileType: parsed.oldFileName === "/dev/null" ? "new" : parsed.newFileName === "/dev/null" ? "deleted" : "changed",
    splitLineCount, unifiedLineCount };
}
