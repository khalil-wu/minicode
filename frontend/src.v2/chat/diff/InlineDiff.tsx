import { useMemo } from "react";

type DiffLine = {
  text: string;
  oldLine?: number;
  newLine?: number;
  kind: "context" | "added" | "removed" | "header";
};

type ParsedDiff = {
  lines: DiffLine[];
  contextCollapsed: boolean;
};

function parseLines(patch: string, contextLines: number | undefined): ParsedDiff {
  let oldLine = 0;
  let newLine = 0;
  const sourceLines = patch.split(/\r?\n/);
  const lines: DiffLine[] = sourceLines.map((text): DiffLine => {
    const hunk = text.match(/^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/);
    if (hunk) {
      const rawOldLine = Number(hunk[1]);
      const rawNewLine = Number(hunk[2]);
      // A turn may contain several tool snapshots concatenated together. A
      // later snapshot can repeat the same hunk start even though the visible
      // rows already advanced. Keep the single displayed line-number column
      // monotonic while still honoring genuinely later hunk starts.
      oldLine = Math.max(rawOldLine, oldLine);
      newLine = Math.max(rawNewLine, newLine);
      return { text, kind: "header" };
    }
    if (/^(?:diff --git|index |new file mode|deleted file mode|rename (?:from|to)|--- |\+\+\+ )/.test(text)) {
      return { text, kind: "header" };
    }
    if (text.startsWith("+") && !text.startsWith("+++")) {
      const line = newLine++;
      return { text, newLine: line, kind: "added" };
    }
    if (text.startsWith("-") && !text.startsWith("---")) {
      const line = oldLine++;
      return { text, oldLine: line, kind: "removed" };
    }
    const old = oldLine++;
    const next = newLine++;
    const line = { text, oldLine: old, newLine: next, kind: "context" as const };
    return line;
  });
  if (contextLines == null || contextLines < 0) return { lines, contextCollapsed: false };
  const changed = lines
    .map((line, index) => line.kind === "added" || line.kind === "removed" ? index : -1)
    .filter((index) => index >= 0);
  if (changed.length === 0) return { lines, contextCollapsed: false };
  const filtered = lines.filter((line, index) => (
    line.kind !== "context"
    || changed.some((changeIndex) => Math.abs(changeIndex - index) <= contextLines)
  ));
  return { lines: filtered, contextCollapsed: filtered.length < lines.length };
}

export function InlineDiff({ patch, contextLines }: { patch: string; contextLines?: number }) {
  const parsed = useMemo(() => parseLines(patch, contextLines), [patch, contextLines]);
  return (
    <div className="inline-diff" role="region" aria-label="文件修改差异">
      {parsed.lines.filter((line) => line.kind !== "header").map((line, index) => (
        <div key={`${index}-${line.text}`} className={`inline-diff-line inline-diff-line-${line.kind}`}>
          <span className="inline-diff-number">{line.kind === "removed" ? line.oldLine ?? "" : line.newLine ?? line.oldLine ?? ""}</span>
          <span className="inline-diff-marker">{line.kind === "added" ? "+" : line.kind === "removed" ? "-" : " "}</span>
          <span className="inline-diff-text">{line.text.slice(line.kind === "header" || line.kind === "context" ? 0 : 1)}</span>
        </div>
      ))}
    </div>
  );
}
