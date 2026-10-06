import { lazy, Suspense, useRef, useLayoutEffect, useId, useMemo } from "react";
import type * as Monaco from "monaco-editor/editor/editor.api.js";
import EditorWorker from "monaco-editor/editor/editor.worker?worker";
import { extractFilePathFromDiff, parseUnifiedDiffLines, type UnifiedDiffLine } from "../lib/unified-diff";
import { useAppStore } from "../stores";
import { defineMiniCodeMonacoTheme, miniCodeMonacoThemeName } from "../panels/monacoTheme";
import { loadMiniCodeEditorFeatures } from "../panels/monacoEditorFeatures";
import { useOwnedDiffModelCleanup } from "./useOwnedDiffModelCleanup";

const MonacoDiffEditor = lazy(async () => {
  const scope = globalThis as typeof globalThis & { MonacoEnvironment?: { getWorker?: () => Worker } };
  if (!scope.MonacoEnvironment?.getWorker) {
    scope.MonacoEnvironment = { ...scope.MonacoEnvironment, getWorker: () => new EditorWorker() };
  }
  const [reactMonaco, monaco] = await Promise.all([
    import("@monaco-editor/react"),
    import("monaco-editor/editor/editor.api.js"),
    loadMiniCodeEditorFeatures(),
    import("monaco-editor/languages/definitions/typescript/register.js"),
    import("monaco-editor/languages/definitions/javascript/register.js"),
    import("monaco-editor/languages/definitions/css/register.js"),
    import("monaco-editor/languages/definitions/html/register.js"),
    import("monaco-editor/languages/definitions/markdown/register.js"),
    import("monaco-editor/languages/definitions/python/register.js"),
  ]);
  reactMonaco.loader?.config?.({ monaco });
  return { default: reactMonaco.DiffEditor };
});

/**
 * Parse a unified diff patch string into original and modified content strings
 * suitable for Monaco's side-by-side diff editor.
 */
export function parseUnifiedDiffToOriginalModified(
  patch: string,
): { original: string; modified: string; filePath: string } {
  const { original, modified, filePath } = parseUnifiedDiffExcerpt(patch);
  return { original, modified, filePath };
}

export function parseUnifiedDiffExcerpt(patch: string): { original: string; modified: string; filePath: string; originalLines: (number | null)[]; modifiedLines: (number | null)[] } {
  const lines = parseUnifiedDiffLines(patch);
  const filePath = extractFilePathFromDiff(lines);
  const rawLines = patch.split("\n");

  const origParts: string[] = [];
  const modParts: string[] = [];
  const originalLines: (number | null)[] = [];
  const modifiedLines: (number | null)[] = [];
  let oldLine: number | undefined;
  let newLine: number | undefined;

  let previousBodyKind: UnifiedDiffLine["kind"] | undefined;
  for (const [index, { text: line, kind }] of lines.entries()) {
    if (kind === "hunk") {
      const range = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/.exec(line);
      if (range) {
        const oldStart = Number(range[1]);
        const newStart = Number(range[2]);
        const omitted = oldLine === undefined || newLine === undefined ? 0 : Math.max(oldStart - oldLine, newStart - newLine);
        if (omitted > 0) {
          const gap = `⋯ 中间省略 ${omitted} 行 ⋯\n`;
          origParts.push(gap);
          modParts.push(gap);
          originalLines.push(null);
          modifiedLines.push(null);
        }
        oldLine = oldStart;
        newLine = newStart;
      }
      previousBodyKind = undefined;
      continue;
    }
    if (kind === "marker") {
      if (previousBodyKind === "del" || previousBodyKind === "context") {
        origParts[origParts.length - 1] = origParts[origParts.length - 1].replace(/\n$/, "");
      }
      if (previousBodyKind === "add" || previousBodyKind === "context") {
        modParts[modParts.length - 1] = modParts[modParts.length - 1].replace(/\n$/, "");
      }
      continue;
    }
    if (kind === "add") {
      modParts.push(line.slice(1) + (rawLines[index].endsWith("\r") ? "\r\n" : "\n"));
      modifiedLines.push(newLine ?? null);
      if (newLine !== undefined) newLine++;
    } else if (kind === "del") {
      origParts.push(line.slice(1) + (rawLines[index].endsWith("\r") ? "\r\n" : "\n"));
      originalLines.push(oldLine ?? null);
      if (oldLine !== undefined) oldLine++;
    } else if (kind === "context" && line.startsWith(" ")) {
      const context = line.slice(1) + (rawLines[index].endsWith("\r") ? "\r\n" : "\n");
      origParts.push(context);
      modParts.push(context);
      originalLines.push(oldLine ?? null);
      modifiedLines.push(newLine ?? null);
      if (oldLine !== undefined) oldLine++;
      if (newLine !== undefined) newLine++;
    } else {
      if (line.startsWith("diff --git ") || line.startsWith("Index: ")) { oldLine = undefined; newLine = undefined; }
      previousBodyKind = undefined;
      continue;
    }
    previousBodyKind = kind;
  }

  return {
    original: origParts.join(""),
    modified: modParts.join(""),
    filePath,
    originalLines,
    modifiedLines,
  };
}

interface MonacoDiffViewProps {
  /** The selected file's unified patch; this view displays its excerpts. */
  patch: string;
  /** Programming language for syntax highlighting in the diff editor. */
  language?: string;
  /** File path shown in the header bar. */
  filePath?: string;
  /** Editor height. */
  height?: string | number;
}

export function MonacoDiffView({
  patch,
  language = "plaintext",
  filePath: filePathProp,
  height = 400,
}: MonacoDiffViewProps) {
  const monacoRef = useRef<typeof Monaco | null>(null);
  const diffEditorRef = useRef<Monaco.editor.IStandaloneDiffEditor | null>(null);
  const instanceId = useId();
  const theme = useAppStore((state) => state.resolvedTheme);
  const codeTextScale = useAppStore((state) => state.codeTextScale);
  useLayoutEffect(() => {
    if (monacoRef.current) defineMiniCodeMonacoTheme(monacoRef.current, theme);
  }, [theme]);

  const { original, modified, filePath, originalLines, modifiedLines } = useMemo(() => {
    const parsed = parseUnifiedDiffExcerpt(patch);
    return { ...parsed, filePath: filePathProp ?? parsed.filePath };
  }, [patch, filePathProp]);
  const applySourceLineNumbers = (editor: Monaco.editor.IStandaloneDiffEditor) => {
    editor.getOriginalEditor().updateOptions({ lineNumbers: (line) => originalLines[line - 1]?.toString() ?? "⋯" });
    editor.getModifiedEditor().updateOptions({ lineNumbers: (line) => modifiedLines[line - 1]?.toString() ?? "⋯" });
  };
  useLayoutEffect(() => {
    if (diffEditorRef.current) applySourceLineNumbers(diffEditorRef.current);
  }, [originalLines, modifiedLines]);
  const viewPath = `minicode-diff://preview/${encodeURIComponent(instanceId)}/${encodeURIComponent(filePath || (language === "typescript" ? "preview.ts" : language === "javascript" ? "preview.js" : "preview"))}`;
  const onOwnedMount = useOwnedDiffModelCleanup(viewPath);
  const scriptExtension = /(?:\.d)?\.[cm]?[jt]sx?$/i.exec(filePath)?.[0] ?? (language === "typescript" ? ".ts" : language === "javascript" ? ".js" : "");
  const onlyEolChanged = original.includes("\r\n") !== modified.includes("\r\n")
    && original.replace(/\r\n/g, "\n") === modified.replace(/\r\n/g, "\n");

  return (
    <div
      style={{
        height,
        minHeight: 0,
        display: "flex",
        flexDirection: "column",
        border: "1px solid var(--border-subtle)",
        borderRadius: "6px",
        overflow: "hidden",
      }}
    >
      {/* Header */}
      {(filePath || patch || onlyEolChanged) && (
        <div
          style={{
            display: "flex",
            flexShrink: 0,
            alignItems: "center",
            justifyContent: "space-between",
            padding: "6px 12px",
            background: "var(--surface-soft)",
            borderBottom: "1px solid var(--border-subtle)",
          }}
        >
          <span
            style={{
              fontSize: "var(--text-xxs)",
              color: "var(--text-secondary)",
              fontFamily: "var(--font-mono)",
            }}
          >
            {filePath}
          </span>
          <span style={{ fontSize: "var(--text-xxs)", color: "var(--text-muted)" }}>{onlyEolChanged ? `换行符从 ${original.includes("\r\n") ? "CRLF" : "LF"} 改为 ${modified.includes("\r\n") ? "CRLF" : "LF"}` : patch ? "仅显示差异片段" : ""}</span>
        </div>
      )}
      {/* Editor */}
      <div style={{ flex: 1, minHeight: 0, background: "var(--editor-background)", color: "var(--editor-foreground)" }}><Suspense
        fallback={
          <div
            style={{
              height: "100%",
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              color: "var(--text-muted)",
            }}
          >
            正在加载差异编辑器…
          </div>
        }
      >
        <MonacoDiffEditor
          key={viewPath}
          height="100%"
          language={language}
          original={original}
          modified={modified}
          originalModelPath={`${viewPath}/original${scriptExtension}`}
          modifiedModelPath={`${viewPath}/modified${scriptExtension}`}
          theme={miniCodeMonacoThemeName(theme)}
          options={{
            readOnly: true,
            renderSideBySide: true,
            minimap: { enabled: false },
            scrollBeyondLastLine: false,
            fontFamily: getComputedStyle(document.documentElement).getPropertyValue("--editor-font-family").trim(),
            fontSize: Math.round(14 * codeTextScale),
            lineHeight: Math.round(22 * codeTextScale),
            fontLigatures: false,
            lineNumbers: "on",
            wordWrap: "on",
            padding: { top: 8 },
            originalEditable: false,
          }}
          beforeMount={(monaco: typeof Monaco) => {
            monacoRef.current = monaco;
            defineMiniCodeMonacoTheme(monaco, useAppStore.getState().resolvedTheme);
          }}
          onMount={(editor: Monaco.editor.IStandaloneDiffEditor) => { onOwnedMount(editor); diffEditorRef.current = editor; applySourceLineNumbers(editor); }}
        />
      </Suspense></div>
    </div>
  );
}
