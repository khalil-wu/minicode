import { useI18n } from "../../i18n";
import { useMemo } from "react";
import { ChevronRight } from "../../lib/icons";
import { guessLanguageFromPath, useColorizedLines } from "../../lib/monaco-colorize";
import { extractFilePathFromDiff, parseUnifiedDiffLines, type UnifiedDiffLine } from "../../lib/unified-diff";
import { useTranscriptSearch } from "../TranscriptSearchContext";
import { useTranscriptReadingPreference } from "../transcriptReadingState";
import "./InlineDiff.css";

type DiffLine = {
  text: string;
  oldLine?: number;
  newLine?: number;
  kind: UnifiedDiffLine["kind"];
  index: number;
  omittedContext?: number;
};

type DiffFile = { path: string; lines: DiffLine[] };
type DiffPart = { kind: "line"; line: DiffLine } | { kind: "context"; lines: DiffLine[] };

function parseFiles(patch: string, filePath: string | undefined): DiffFile[] {
  const files: UnifiedDiffLine[][] = [];
  for (const line of parseUnifiedDiffLines(patch)) {
    if (files.length === 0 || (line.kind === "meta" && /^(?:diff --git |Index: )/.test(line.text))) files.push([]);
    files.at(-1)!.push(line);
  }
  return files.map((source, fileIndex) => {
    let oldLine: number | undefined;
    let newLine: number | undefined;
    const lines = source.map(({ text, kind }, index): DiffLine => {
      const line: DiffLine = {
        text: kind === "add" || kind === "del" || (kind === "context" && text.startsWith(" ")) ? text.slice(1) : text,
        kind,
        index,
      };
      if (kind === "hunk") {
        const hunk = text.match(/^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/)!;
        const nextOld = Number(hunk[1]);
        const nextNew = Number(hunk[2]);
        const oldGap = nextOld - (oldLine ?? 1);
        const newGap = nextNew - (newLine ?? 1);
        // A hunk can prove a coordinate gap, but it does not supply its bytes.
        // Only carried context below gets an expansion control.
        if (oldGap > 0 && oldGap === newGap) line.omittedContext = oldGap;
        oldLine = nextOld;
        newLine = nextNew;
      }
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
    return { path: extractFilePathFromDiff(source) || (files.length === 1 && fileIndex === 0 ? filePath || "" : ""), lines };
  });
}

function foldContext(lines: DiffLine[], contextLines: number | undefined): DiffPart[] {
  if (contextLines == null || contextLines < 0 || !lines.some(line => line.kind === "add" || line.kind === "del")) {
    return lines.map(line => ({ kind: "line", line }));
  }
  const parts: DiffPart[] = [];
  for (let index = 0; index < lines.length;) {
    const line = lines[index];
    if (line.kind !== "context") {
      parts.push({ kind: "line", line });
      index++;
      continue;
    }
    const start = index;
    while (index < lines.length && lines[index].kind === "context") index++;
    const run = lines.slice(start, index);
    const previous = lines[start - 1];
    const next = lines[index];
    const visibleStart = previous && (previous.kind === "add" || previous.kind === "del") ? contextLines : 0;
    const visibleEnd = next && (next.kind === "add" || next.kind === "del") ? contextLines : 0;
    if (run.length <= visibleStart + visibleEnd) {
      parts.push(...run.map((line): DiffPart => ({ kind: "line", line })));
    } else {
      parts.push(...run.slice(0, visibleStart).map((line): DiffPart => ({ kind: "line", line })));
      parts.push({ kind: "context", lines: run.slice(visibleStart, run.length - visibleEnd) });
      parts.push(...run.slice(run.length - visibleEnd).map((line): DiffPart => ({ kind: "line", line })));
    }
  }
  return parts;
}

function DiffRow({ line, html }: { line: DiffLine; html?: string }) {
  const kind = line.kind === "add" ? "added" : line.kind === "del" ? "removed" : line.kind;
  return <div className={`inline-diff-line inline-diff-line-${kind}`}>
    <span className="inline-diff-number">{line.kind === "del" ? line.oldLine ?? "" : line.newLine ?? line.oldLine ?? ""}</span>
    <span className="inline-diff-marker" aria-hidden="true">{line.kind === "add" ? "+" : line.kind === "del" ? "-" : " "}</span>
    {html ? <span className="inline-diff-text" dangerouslySetInnerHTML={{ __html: html }} /> : <span className="inline-diff-text">{line.text}</span>}
  </div>;
}

function ContextSection({ lines, colorized, readingKey }: { lines: DiffLine[]; colorized: string[] | null; readingKey: string }) {
  const { t: tr } = useI18n();
  const [preference, setExpanded, userToggled] = useTranscriptReadingPreference(readingKey, false);
  const expanded = useTranscriptSearch() || preference;
  const count = lines.length;
  return <div className="inline-diff-context-section">
    <button type="button" className="inline-diff-line-marker inline-diff-context-toggle" aria-expanded={expanded}
      aria-label={tr("{v0} {v1} 行未修改内容", { v0: expanded ? tr("收起") : tr("展开"), v1: count })} onClick={() => {
        userToggled.current = true;
        setExpanded(value => !value);
      }}>
      <ChevronRight size={14} className="mc-process-disclosure" data-expanded={expanded} aria-hidden="true" />
      <span>{count.toLocaleString()} {tr("行未修改")}</span>
    </button>
    {expanded && lines.map(line => <DiffRow key={line.index} line={line} html={colorized?.[line.index]} />)}
  </div>;
}

function FileDiff({ file, contextLines, readingKey }: { file: DiffFile; contextLines?: number; readingKey: string }) {
  const { t: tr } = useI18n();
  const colorized = useColorizedLines(file.lines, guessLanguageFromPath(file.path));
  const parts = useMemo(() => foldContext(file.lines, contextLines), [file.lines, contextLines]);
  return <div className="inline-diff-file" data-file-path={file.path}>
    {parts.map(part => {
      if (part.kind === "context") {
        const first = part.lines[0];
        return <ContextSection key={`context:${first.index}`} lines={part.lines} colorized={colorized}
          readingKey={`${readingKey}:context:${first.oldLine ?? first.index}:${first.newLine ?? first.index}:${part.lines.length}`} />;
      }
      const line = part.line;
      if (line.kind === "hunk") return line.omittedContext ? <div key={line.index} className="inline-diff-context-gap">{line.omittedContext.toLocaleString()} {tr("行未修改")}</div> : null;
      if (line.kind === "meta") return null;
      return <DiffRow key={line.index} line={line} html={colorized?.[line.index]} />;
    })}
  </div>;
}

export function InlineDiff({ patch, contextLines, filePath, readingKey = "inline-diff" }: { patch: string; contextLines?: number; filePath?: string; readingKey?: string }) {
  const { t: tr } = useI18n();
  const files = useMemo(() => parseFiles(patch, filePath), [patch, filePath]);
  return <div className="inline-diff" role="region" aria-label={tr("文件修改差异")}>
    {files.map((file, index) => <div key={`${index}:${file.path}`}>
      {files.length > 1 && file.path && <div className="inline-diff-file-title">{tr("文件：")}{file.path}</div>}
      <FileDiff file={file} contextLines={contextLines} readingKey={`${readingKey}:file:${index}:${file.path}`} />
    </div>)}
  </div>;
}
