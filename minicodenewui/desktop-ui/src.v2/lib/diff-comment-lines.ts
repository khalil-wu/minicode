import { parseUnifiedDiffLines } from "./unified-diff";
import type { DiffCommentPosition, DiffSide } from "./diff-comments";

export interface DiffLine {
  kind: "context" | "add" | "del" | "hunk" | "meta";
  text: string;
  oldLine?: number;
  newLine?: number;
  lineIndex?: number;
  unifiedIndex?: number;
  oldGap?: number;
  newGap?: number;
  hunkIndex?: number;
}

export interface DiffLineLocation { side: DiffSide; line: number }
export interface DiffLineSelection { start: DiffLineLocation; end: DiffLineLocation }

export function parsePositionedDiff(raw: string): DiffLine[] {
  let oldLine: number | undefined;
  let newLine: number | undefined;
  let lineIndex = 0;
  let hunkIndex = -1;
  return parseUnifiedDiffLines(raw).map(({ kind, text }) => {
    let oldGap: number | undefined;
    let newGap: number | undefined;
    if (kind === "meta") {
      oldLine = undefined;
      newLine = undefined;
    } else if (kind === "hunk") {
      const range = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/.exec(text);
      oldGap = range ? Number(range[1]) - (oldLine ?? 1) : undefined;
      newGap = range ? Number(range[2]) - (newLine ?? 1) : undefined;
      oldLine = range ? Number(range[1]) : undefined;
      newLine = range ? Number(range[2]) : undefined;
      hunkIndex++;
    }
    const line: DiffLine = {
      kind: kind === "marker" ? "meta" : kind,
      text: kind === "add" || kind === "del" || (kind === "context" && text.startsWith(" ")) ? text.slice(1) : text,
      hunkIndex: hunkIndex >= 0 ? hunkIndex : undefined,
    };
    if (kind === "hunk") Object.assign(line, { oldLine, newLine, oldGap, newGap });
    if (kind === "context" || kind === "add" || kind === "del") line.lineIndex = lineIndex++;
    if (kind === "context" || kind === "del") {
      line.oldLine = oldLine;
      if (oldLine !== undefined) oldLine++;
    }
    if (kind === "context" || kind === "add") {
      line.newLine = newLine;
      if (newLine !== undefined) newLine++;
    }
    return line;
  });
}

export function diffCommentPosition(path: string, selection: DiffLineSelection): DiffCommentPosition {
  const { start, end } = selection;
  const sameSide = start.side === end.side;
  const first = sameSide ? Math.min(start.line, end.line) : start.line;
  const last = sameSide ? Math.max(start.line, end.line) : end.line;
  return { path, side: end.side, line: last,
    ...(first !== last || !sameSide ? { start_line: first } : {}),
    ...(!sameSide ? { start_side: start.side } : {}),
  };
}

export function diffLineAtPosition(lines: DiffLine[], location: DiffLineLocation): DiffLine | undefined {
  return lines.find(line => (line.unifiedIndex !== undefined || line.lineIndex !== undefined)
    && (location.side === "left" ? line.oldLine : line.newLine) === location.line);
}

/** A saved patch is partial: keep its original selected hunks without inventing omitted context. */
export function diffCommentHunk(raw: string, position: DiffCommentPosition): string | undefined {
  const lines = parsePositionedDiff(raw);
  const first = diffLineAtPosition(lines, { side: position.start_side ?? position.side, line: position.start_line ?? position.line });
  const last = diffLineAtPosition(lines, position);
  if (first?.hunkIndex === undefined || last?.hunkIndex === undefined) return undefined;
  const start = Math.min(first.hunkIndex, last.hunkIndex), end = Math.max(first.hunkIndex, last.hunkIndex);
  let hunk = -1;
  return parseUnifiedDiffLines(raw).flatMap(line => {
    if (line.kind === "hunk") hunk++;
    if (hunk < start || hunk > end || line.kind === "meta" || line.kind === "marker") return [];
    return [line.text];
  }).join("\r\n");
}

export function diffSelectionContains(lines: DiffLine[], selection: DiffLineSelection | null, location: DiffLineLocation): boolean {
  if (!selection) return false;
  const indexOf = (location: DiffLineLocation) => {
    const line = diffLineAtPosition(lines, location);
    return line?.unifiedIndex ?? line?.lineIndex;
  };
  const first = indexOf(selection.start);
  const last = indexOf(selection.end);
  const current = indexOf(location);
  if (first === undefined || last === undefined || current === undefined) return false;
  if (selection.start.side === selection.end.side) return location.side === selection.end.side && location.line >= Math.min(selection.start.line, selection.end.line) && location.line <= Math.max(selection.start.line, selection.end.line);
  return current >= Math.min(first, last) && current <= Math.max(first, last);
}
