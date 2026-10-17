import { diffWordsWithSpace, type Change } from "diff";

export type DiffWordSpan = { start: number; end: number };
export type DiffWordLine = { kind: string; text: string };
export type DiffWordOptions = {
  wordDiffsEnabled?: boolean;
  forcePlainText?: boolean;
  /** Zero-based render offset and count, not file coordinates or an end line. */
  startingLine?: number;
  totalLines?: number;
  /** Authoritative file metadata when the patch omits unchanged file bytes. */
  unifiedLineCount?: number;
  splitLineCount?: number;
  changedLineCount?: number;
};

type ChangeBlock = { deletions: number[]; additions: number[]; unifiedStart: number; splitStart: number };
type WordPart = { changed: boolean; value: string };

const isChange = (line: DiffWordLine) => line.kind === "add" || line.kind === "del";
const isNewlineMarker = (line: DiffWordLine) => line.kind === "marker" || line.text.startsWith("\\ No newline at end of file");
const stripEol = (line: string) => line.endsWith("\n") ? line.slice(0, line.endsWith("\r\n") ? -2 : -1) : line;

function appendPart(parts: WordPart[], value: string, neutral: boolean, lastChange: boolean) {
  const previous = parts.at(-1);
  // Source word-alt joins a one-character neutral separator to the preceding
  // changed part. The final change stays separate, including trailing spaces.
  if (!previous || lastChange) parts.push({ changed: !neutral, value });
  else if (neutral === !previous.changed || neutral && value.length === 1 && previous.changed) previous.value += value;
  else parts.push({ changed: !neutral, value });
}

function spansFromParts(parts: WordPart[]): DiffWordSpan[] {
  const spans: DiffWordSpan[] = [];
  let offset = 0;
  for (const part of parts) {
    if (part.changed) spans.push({ start: offset, end: offset + part.value.length });
    offset += part.value.length;
  }
  return spans;
}

function pairedWordSpans(deletion: string, addition: string): [DiffWordSpan[], DiffWordSpan[]] {
  const before = stripEol(deletion), after = stripEol(addition);
  if (before.length > 1000 || after.length > 1000) return [[], []];
  // diff@8.0.4 has the same WordsWithSpace tokenizer and Myers tie handling as
  // the frozen Source caller. diffWords has different whitespace semantics.
  const changes: Change[] = diffWordsWithSpace(before, after);
  const deletions: WordPart[] = [], additions: WordPart[] = [];
  changes.forEach((change, index) => {
    const last = index === changes.length - 1;
    if (!change.added && !change.removed) {
      appendPart(deletions, change.value, true, last);
      appendPart(additions, change.value, true, last);
    } else if (change.removed) appendPart(deletions, change.value, false, last);
    else appendPart(additions, change.value, false, last);
  });
  return [spansFromParts(deletions), spansFromParts(additions)];
}

/** Decorations follow full patch line indices, so unified/split and context
 * folding consume the same pairs without re-diffing a truncated preview. */
export function buildDiffWordDecorations(lines: readonly DiffWordLine[], options: DiffWordOptions = {}): {
  spansByLine: DiffWordSpan[][];
  usePlainText: boolean;
} {
  const spansByLine = lines.map((): DiffWordSpan[] => []);
  const blocks: ChangeBlock[] = [];
  let unifiedCount = 0, splitCount = 0, changedCount = 0;
  for (let index = 0; index < lines.length;) {
    const line = lines[index];
    if (!isChange(line)) {
      if (line.kind === "context") { unifiedCount++; splitCount++; }
      index++;
      continue;
    }
    const block: ChangeBlock = { deletions: [], additions: [], unifiedStart: unifiedCount, splitStart: splitCount };
    while (index < lines.length && (isChange(lines[index]) || isNewlineMarker(lines[index]))) {
      if (lines[index].kind === "del") block.deletions.push(index);
      else if (lines[index].kind === "add") block.additions.push(index);
      index++;
    }
    const changed = block.deletions.length + block.additions.length;
    changedCount += changed;
    unifiedCount += changed;
    splitCount += Math.max(block.deletions.length, block.additions.length);
    blocks.push(block);
  }

  const usePlainText = (options.changedLineCount ?? changedCount) > 2000;
  const start = options.forcePlainText ? options.startingLine ?? 0 : 0;
  const count = options.forcePlainText ? options.totalLines ?? Infinity : Infinity;
  const windowed = start > 0 || count < Infinity;
  const plainBodyTooLarge = options.forcePlainText && !windowed
    && ((options.unifiedLineCount ?? unifiedCount) > 1000 || (options.splitLineCount ?? splitCount) > 1000);
  if (usePlainText || options.wordDiffsEnabled === false || plainBodyTooLarge) return { spansByLine, usePlainText };

  const inWindow = (index: number) => index >= start && index < start + count;
  for (const block of blocks) {
    const pairedCount = Math.min(block.deletions.length, block.additions.length);
    for (let index = 0; index < pairedCount; index++) {
      // Source's highlighter iterates diffStyle "both": a pair can be reached
      // through either unified side or its split row in the requested window.
      if (windowed && !inWindow(block.unifiedStart + index)
        && !inWindow(block.unifiedStart + block.deletions.length + index)
        && !inWindow(block.splitStart + index)) continue;
      const deletionIndex = block.deletions[index], additionIndex = block.additions[index];
      [spansByLine[deletionIndex], spansByLine[additionIndex]] = pairedWordSpans(lines[deletionIndex].text, lines[additionIndex].text);
    }
  }
  return { spansByLine, usePlainText };
}
