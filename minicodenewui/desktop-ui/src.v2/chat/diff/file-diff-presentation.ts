import type { Parser, SyntaxNode } from "@lezer/common";
import type { FullDiffContext, FullDiffHunk, FullDiffLine } from "./full-content-metadata";

export type DiffPresentationLanguage = "javascript" | "typescript" | "tsx" | "python" | "rust" | "go" | "java";
/** Annotations use file lines (1 based), never a rendered lineIndex. */
export interface DiffPresentationAnnotation { side: "additions" | "deletions"; lineNumber: number }

export function diffPresentationLanguage(path: string): DiffPresentationLanguage | undefined {
  switch (path.split(".").at(-1)?.toLowerCase()) {
    case "ts": case "mts": case "cts": return "typescript";
    case "tsx": return "tsx";
    case "js": case "jsx": case "mjs": case "cjs": return "javascript";
    case "py": case "pyi": return "python";
    case "rs": return "rust";
    case "go": return "go";
    case "java": return "java";
  }
}

function importNode(language: DiffPresentationLanguage, node: SyntaxNode): boolean {
  switch (language) {
    case "javascript": case "typescript": case "tsx": case "java": return node.name === "ImportDeclaration";
    case "python": return node.name === "ImportStatement";
    case "rust": return node.getChild("Vis") == null && (node.name === "UseDeclaration" || node.name === "ExternCrateDeclaration");
    case "go": return node.name === "ImportDecl";
  }
}

function upperBound(values: number[], value: number): number {
  let low = 0, high = values.length;
  while (low < high) {
    const middle = (low + high) >>> 1;
    if (values[middle] <= value) low = middle + 1;
    else high = middle;
  }
  return low;
}

/** Parsed top-level, whole-line imports, including multiline nodes. */
function importLines(lines: string[], parser: Parser, language: DiffPresentationLanguage): Set<number> {
  const result = new Set<number>(), offsets = [0];
  for (const line of lines) offsets.push(offsets[offsets.length - 1] + line.length);
  if (offsets[offsets.length - 1] > 1e6) return result;
  parser.parse(lines.join("")).iterate({ enter({ node }) {
    if (node.parent?.parent != null || !importNode(language, node)) return;
    const first = upperBound(offsets, node.from) - 1, last = upperBound(offsets, node.to - 1) - 1;
    let end = node.to;
    for (let sibling = node.nextSibling; sibling != null
      && sibling.from < offsets[last + 1] && sibling.to <= offsets[last + 1]
      && ["LineComment", "BlockComment", "Comment"].includes(sibling.name)
      && lines[last].slice(end - offsets[last], sibling.from - offsets[last]).trim() === ""; sibling = sibling.nextSibling) end = sibling.to;
    if (lines[first].slice(0, node.from - offsets[first]).trim() !== "" || lines[last].slice(end - offsets[last]).trim() !== "") return false;
    let invalid = false;
    node.toTree().iterate({ enter({ type }) { if (type.isError) invalid = true; } });
    if (invalid) return false;
    for (let index = first; index <= last; index++) result.add(index);
    return false;
  } });
  return result;
}

function importOnlyHunk(context: FullDiffContext, hunk: FullDiffHunk, additions: Set<number>, deletions: Set<number>): boolean {
  let foundImport = false;
  for (const line of hunk.body) {
    if (line.kind === "add") {
      const index = line.newLine! - 1;
      if (additions.has(index)) foundImport = true;
      else if (context.newLines[index]?.trim() !== "") return false;
    } else if (line.kind === "del") {
      const index = line.oldLine! - 1;
      if (deletions.has(index)) foundImport = true;
      else if (context.oldLines[index]?.trim() !== "") return false;
    }
  }
  return foundImport;
}

/** Retain mixed hunks and hunks containing any annotation. */
export function filterImportOnlyDiffHunks(context: FullDiffContext, path: string, parser: Parser | null | undefined,
  lineAnnotations: readonly DiffPresentationAnnotation[] = []): FullDiffContext {
  const language = diffPresentationLanguage(path);
  if (parser == null || language == null || context.isPartial || context.fileType === "new" || context.fileType === "deleted") return context;
  const additions = importLines(context.newLines, parser, language), deletions = importLines(context.oldLines, parser, language);
  const kept = context.hunks.filter(hunk => lineAnnotations.some(annotation => {
    const start = annotation.side === "additions" ? hunk.newStart : hunk.oldStart;
    const count = annotation.side === "additions" ? hunk.newCount : hunk.oldCount;
    return annotation.lineNumber >= start && annotation.lineNumber < start + count;
  }) || !importOnlyHunk(context, hunk, additions, deletions));
  if (kept.length === context.hunks.length) return context;
  const oldLines: string[] = [], newLines: string[] = [], hunks: FullDiffHunk[] = [];
  let additionEnd = 0, splitLineCount = 0, unifiedLineCount = 0;
  for (const hunk of kept) {
    const start = hunk.newStart - (hunk.newCount === 0 ? 0 : 1), gap = Math.max(start - additionEnd, 0);
    const body = hunk.body.map((line, offset): FullDiffLine => {
      if (line.kind !== "add") oldLines.push(context.oldLines[line.oldLine! - 1]);
      if (line.kind !== "del") newLines.push(context.newLines[line.newLine! - 1]);
      return { ...line, unifiedIndex: unifiedLineCount + gap + offset };
    });
    hunks.push({ ...hunk, before: [], body, collapsedBefore: gap,
      header: { ...hunk.header, oldGap: gap, newGap: gap }, splitLineStart: splitLineCount + gap, unifiedLineStart: unifiedLineCount + gap });
    additionEnd = start + hunk.newCount;
    splitLineCount += gap + hunk.splitLineCount;
    unifiedLineCount += gap + hunk.unifiedLineCount;
  }
  return { ...context, hunks, trailing: [], lines: hunks.flatMap(hunk => [hunk.header, ...hunk.body]), oldLines, newLines,
    isPartial: true, splitLineCount, unifiedLineCount };
}
