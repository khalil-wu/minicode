import { useMemo } from "react";
import { extractFilePathFromDiff, parseUnifiedDiffLines } from "../../lib/unified-diff";

type DiffLine = {
  text: string;
  oldLine?: number;
  newLine?: number;
  kind: "context" | "added" | "removed" | "header" | "marker";
};

type ParsedDiff = {
  lines: DiffLine[];
  contextCollapsed: boolean;
};

function parseLines(patch: string, contextLines: number | undefined): ParsedDiff {
  let oldLine: number | undefined;
  let newLine: number | undefined;
  const sourceLines = parseUnifiedDiffLines(patch);
  const fileStarts = sourceLines.flatMap((line, index) => line.kind === "meta" && line.text.startsWith("diff --git ") ? [index] : []);
  let fileIndex = 0;
  const lines: DiffLine[] = sourceLines.map(({ text, kind }): DiffLine => {
    const hunk = text.match(/^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/);
    if (hunk) {
      const rawOldLine = Number(hunk[1]);
      const rawNewLine = Number(hunk[2]);
      oldLine = rawOldLine;
      newLine = rawNewLine;
      return { text, kind: "header" };
    }
    if (kind === "meta" || kind === "hunk") {
      if (text.startsWith("diff --git ") || text.startsWith("Index: ")) oldLine = newLine = undefined;
      if (fileStarts.length > 1 && text.startsWith("diff --git ")) {
        const path = extractFilePathFromDiff(sourceLines.slice(fileStarts[fileIndex], fileStarts[fileIndex + 1] ?? sourceLines.length));
        fileIndex++;
        return { text: path ? `文件：${path}` : text, kind: "marker" };
      }
      return { text, kind: "header" };
    }
    if (kind === "marker") return { text, kind: "marker" };
    if (kind === "add") {
      const line = newLine;
      if (newLine !== undefined) newLine++;
      return { text, newLine: line, kind: "added" };
    }
    if (kind === "del") {
      const line = oldLine;
      if (oldLine !== undefined) oldLine++;
      return { text, oldLine: line, kind: "removed" };
    }
    const old = oldLine;
    const next = newLine;
    if (oldLine !== undefined) oldLine++;
    if (newLine !== undefined) newLine++;
    const line = { text, oldLine: old, newLine: next, kind: "context" as const };
    return line;
  });
  if (contextLines == null || contextLines < 0) return { lines, contextCollapsed: false };
  const changed = lines
    .map((line, index) => line.kind === "added" || line.kind === "removed" ? index : -1)
    .filter((index) => index >= 0);
  if (changed.length === 0) return { lines, contextCollapsed: false };
  const filtered: DiffLine[] = [];
  let changeIndex = 0;
  let skipped = false;
  let contextCollapsed = false;
  lines.forEach((line, index) => {
    while (changed[changeIndex] !== undefined && changed[changeIndex] < index - contextLines) changeIndex++;
    if (line.kind === "context" && !(changed[changeIndex] !== undefined && changed[changeIndex] <= index + contextLines)) {
      skipped = contextCollapsed = true;
      return;
    }
    if (skipped) filtered.push({ text: "…", kind: "marker" });
    skipped = false;
    filtered.push(line);
  });
  if (skipped) filtered.push({ text: "…", kind: "marker" });
  return { lines: filtered, contextCollapsed };
}

export function InlineDiff({ patch, contextLines }: { patch: string; contextLines?: number }) {
  const parsed = useMemo(() => parseLines(patch, contextLines), [patch, contextLines]);
  return (
    <div className="inline-diff" role="region" aria-label="文件修改差异">
      {parsed.lines.filter((line) => line.kind !== "header").map((line, index) => (
        <div key={`${index}-${line.text}`} className={`inline-diff-line inline-diff-line-${line.kind}`}>
          <span className="inline-diff-number">{line.kind === "removed" ? line.oldLine ?? "" : line.newLine ?? line.oldLine ?? ""}</span>
          <span className="inline-diff-marker">{line.kind === "added" ? "+" : line.kind === "removed" ? "-" : " "}</span>
          <span className="inline-diff-text">{line.kind === "added" || line.kind === "removed" || (line.kind === "context" && line.text.startsWith(" ")) ? line.text.slice(1) : line.text}</span>
        </div>
      ))}
    </div>
  );
}
