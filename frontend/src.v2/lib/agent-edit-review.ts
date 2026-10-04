/**
 * Review model for agent edits shown inline in the editor.
 *
 * The turn diff is the authority for what the agent changed. A block is one
 * contiguous run of removed/added lines inside a hunk; it is reviewable only
 * while its added text is still present in the buffer, so a block the user has
 * already undone (or edited over) disappears instead of being reverted twice.
 */

export interface AgentEditBlock {
  /** Content identity; stable while the block's text is unchanged. */
  key: string;
  /** 1-based first added line; for a pure deletion, the line that followed the removed text. */
  line: number;
  added: string[];
  removed: string[];
  /** When a diff EOF marker establishes the removed side's newline state. */
  removedFinalNewline?: boolean;
  removedEol?: "\n" | "\r\n";
}

export interface AgentEditReview {
  blocks: AgentEditBlock[];
  /** Hunks whose post-change text is no longer present in the buffer. */
  unmatchedHunks: number;
}

interface PatchLine {
  kind: "context" | "add" | "del";
  text: string;
  noNewline?: boolean;
  eol?: "\n" | "\r\n";
}

interface PatchHunk {
  oldStart: number;
  newStart: number;
  lines: PatchLine[];
}

const HUNK_HEADER = /^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@/;

const stripCr = (line: string): string => (line.endsWith("\r") ? line.slice(0, -1) : line);

/** Parse the hunks of a single-file unified patch. */
export function parsePatchHunks(patch: string): PatchHunk[] {
  const hunks: PatchHunk[] = [];
  let current: PatchHunk | null = null;
  let oldRemaining = 0;
  let newRemaining = 0;
  for (const raw of patch.split("\n")) {
    const line = stripCr(raw);
    if (line === "\\ No newline at end of file" && current?.lines.length) {
      current.lines[current.lines.length - 1].noNewline = true;
      delete current.lines[current.lines.length - 1].eol;
      continue;
    }
    if (current && (oldRemaining > 0 || newRemaining > 0)) {
      // Counted hunk bodies: content lines such as "---x" stay content.
      if (line.startsWith("\\")) continue;
      const marker = line[0];
      const text = line.slice(1);
      if (marker === "+") {
        current.lines.push({ kind: "add", text });
        newRemaining -= 1;
      } else if (marker === "-") {
        current.lines.push({ kind: "del", text, eol: raw.endsWith("\r") ? "\r\n" : "\n" });
        oldRemaining -= 1;
      } else if (marker === " " || line === "") {
        current.lines.push({ kind: "context", text });
        oldRemaining -= 1;
        newRemaining -= 1;
      } else {
        current = null;
      }
      continue;
    }
    const header = HUNK_HEADER.exec(line);
    if (!header) continue;
    oldRemaining = header[2] === undefined ? 1 : Number(header[2]);
    newRemaining = header[4] === undefined ? 1 : Number(header[4]);
    current = { oldStart: Number(header[1]), newStart: Number(header[3]), lines: [] };
    hunks.push(current);
  }
  return hunks;
}

const bufferLines = (content: string): string[] => {
  if (!content) return [];
  const lines = content.split("\n").map(stripCr);
  if (content.endsWith("\n")) lines.pop();
  return lines;
};

const matchesAt = (lines: string[], index: number, expected: string[]): boolean => {
  if (index < 0 || index + expected.length > lines.length) return false;
  for (let offset = 0; offset < expected.length; offset += 1) {
    if (lines[index + offset] !== expected[offset]) return false;
  }
  return true;
};

/** Nearest index to `expected` where `sequence` occurs in `lines`, or -1. */
const locate = (lines: string[], sequence: string[], expected: number): number => {
  if (sequence.length === 0) {
    return Math.max(0, Math.min(expected, lines.length));
  }
  if (matchesAt(lines, expected, sequence)) return expected;
  let best = -1;
  for (let index = 0; index + sequence.length <= lines.length; index += 1) {
    if (!matchesAt(lines, index, sequence)) continue;
    if (best < 0 || Math.abs(index - expected) < Math.abs(best - expected)) best = index;
  }
  return best;
};

const hashText = (value: string): string => {
  // FNV-1a: a compact, deterministic identity for block bookkeeping.
  let hash = 0x811c9dc5;
  for (let index = 0; index < value.length; index += 1) {
    hash ^= value.charCodeAt(index);
    hash = Math.imul(hash, 0x01000193);
  }
  return (hash >>> 0).toString(36);
};

/** Locate the reviewable blocks of `patch` inside the current buffer text. */
export function reviewAgentEdits(patch: string, content: string): AgentEditReview {
  const lines = bufferLines(content);
  const blocks: AgentEditBlock[] = [];
  let unmatchedHunks = 0;
  let drift = 0;
  for (const hunk of parsePatchHunks(patch)) {
    if (!hunk.lines.some((line) => line.kind !== "context")) continue;
    const newSide = hunk.lines.filter((line) => line.kind !== "del").map((line) => line.text);
    // An empty new side ("+N,0") is positioned after new line N, not at it.
    const anchor = Math.max(0, newSide.length === 0 ? hunk.newStart : hunk.newStart - 1);
    const found = newSide.length === 0 && hunk.newStart === 0 && lines.length > 0
      ? -1 : locate(lines, newSide, anchor + drift);
    if (found < 0) {
      unmatchedHunks += 1;
      continue;
    }
    drift = found - anchor;
    let cursor = found;
    let oldLine = hunk.oldStart;
    let pending: { oldLine: number; line: number; added: string[]; removed: string[]; removedFinalNewline?: boolean; removedEol?: "\n" | "\r\n" } | null = null;
    const flush = () => {
      if (!pending) return;
      if (newSide.length === 0 && hunk.newStart === 0 && pending.removedFinalNewline === undefined) {
        pending.removedFinalNewline = true;
      }
      const identity = hashText(`${pending.removed.join("\n")}\u0000${pending.added.join("\n")}\u0000${pending.removedFinalNewline ?? ""}\u0000${pending.removedEol ?? ""}`);
      const { oldLine, ...block } = pending;
      blocks.push({ key: `${identity}.${oldLine}`, ...block });
      pending = null;
    };
    for (const line of hunk.lines) {
      if (line.kind === "context") {
        flush();
        cursor += 1;
        oldLine += 1;
        continue;
      }
      pending ??= { oldLine, line: cursor + 1, added: [], removed: [] };
      if (line.kind === "del") {
        pending.removed.push(line.text);
        oldLine += 1;
        if (line.eol) pending.removedEol = line.eol;
        if (line.noNewline) pending.removedFinalNewline = false;
      } else {
        pending.added.push(line.text);
        if (line.noNewline && pending.removed.length > 0 && pending.removedFinalNewline === undefined) {
          pending.removedFinalNewline = true;
        }
        cursor += 1;
      }
    }
    flush();
  }
  return { blocks, unmatchedHunks };
}

/** Raw line segments, each carrying its own terminator ("\r\n", "\n" or ""). */
const segmentsOf = (content: string): string[] => content.match(/[^\n]*\n|[^\n]+$/g) ?? [];

const terminatorOf = (segment: string | undefined): string => {
  if (!segment) return "";
  if (segment.endsWith("\r\n")) return "\r\n";
  return segment.endsWith("\n") ? "\n" : "";
};

const bodyOf = (segment: string): string => segment.slice(0, segment.length - terminatorOf(segment).length);

/**
 * Restore the removed text of `block` in `content`. Untouched lines keep their
 * exact bytes; returns null when the added text is no longer where the block
 * says it is.
 */
export function revertAgentEditBlock(content: string, block: AgentEditBlock): string | null {
  const segments = segmentsOf(content);
  const start = block.line - 1;
  if (start < 0 || start + block.added.length > segments.length) return null;
  for (let offset = 0; offset < block.added.length; offset += 1) {
    if (stripCr(bodyOf(segments[start + offset])) !== block.added[offset]) return null;
  }
  const eol = block.removedEol ?? (content.includes("\r\n") ? "\r\n" : "\n");
  const replaced = segments.slice(start, start + block.added.length);
  const endsFile = start + block.added.length === segments.length;
  const currentTerminator = replaced.length > 0 ? terminatorOf(replaced.at(-1)) : terminatorOf(segments.at(-1));
  // The file's final-newline state belongs to whatever ends up last.
  const finalTerminator = endsFile
    ? (block.removedFinalNewline === undefined
      ? (currentTerminator ? block.removedEol ?? currentTerminator : "")
      : block.removedFinalNewline ? eol : "")
    : eol;
  const restored = block.removed.map((text, index) => {
    const own = terminatorOf(replaced[index]);
    const last = index === block.removed.length - 1;
    return text + (last && endsFile ? finalTerminator : block.removedEol || own || eol);
  });
  const before = segments.slice(0, start);
  if (endsFile && replaced.length === 0 && restored.length > 0 && before.length > 0) {
    // Inserting after an unterminated final line needs a separator first.
    const tail = before[before.length - 1];
    if (!terminatorOf(tail)) before[before.length - 1] = tail + eol;
  }
  return [...before, ...restored, ...segments.slice(start + block.added.length)].join("");
}

/** Revert every block, last first so earlier line numbers stay valid. */
export function revertAgentEditBlocks(content: string, blocks: AgentEditBlock[]): string {
  return [...blocks]
    .sort((left, right) => right.line - left.line)
    .reduce((current, block) => revertAgentEditBlock(current, block) ?? current, content);
}

export interface AgentEditTextEdit {
  range: { startLineNumber: number; startColumn: number; endLineNumber: number; endColumn: number };
  text: string;
}

/**
 * The minimal editor edit restoring `block` in an editor model of `lineCount`
 * lines (a trailing newline yields a final empty model line). `lineMaxColumn`
 * returns the column after the last character of a model line.
 */
export function agentEditUndoEdit(
  block: AgentEditBlock,
  lineCount: number,
  lineMaxColumn: (lineNumber: number) => number,
  eol: string,
): AgentEditTextEdit {
  eol = block.removedEol ?? eol;
  const after = block.line + block.added.length;
  if (after <= lineCount) {
    return {
      range: { startLineNumber: block.line, startColumn: 1, endLineNumber: after, endColumn: 1 },
      text: block.removed.join(eol) + (block.removed.length && block.removedFinalNewline !== false ? eol : ""),
    };
  }
  const lastColumn = lineMaxColumn(lineCount);
  if (block.added.length === 0) {
    // Removed lines that used to follow an unterminated final line.
    return {
      range: { startLineNumber: lineCount, startColumn: lastColumn, endLineNumber: lineCount, endColumn: lastColumn },
      text: eol + block.removed.join(eol) + (block.removedFinalNewline === true ? eol : ""),
    };
  }
  if (block.removed.length === 0 && block.line > 1) {
    // Dropping the unterminated final lines also drops the separator before them.
    return {
      range: { startLineNumber: block.line - 1, startColumn: lineMaxColumn(block.line - 1), endLineNumber: lineCount, endColumn: lastColumn },
      text: "",
    };
  }
  return {
    range: { startLineNumber: block.line, startColumn: 1, endLineNumber: lineCount, endColumn: lastColumn },
    text: block.removed.join(eol) + (block.removedFinalNewline === true ? eol : ""),
  };
}
