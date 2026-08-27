import { lazy, Suspense, useRef, useEffect, useState, useMemo } from "react";
import type React from "react";
import EditorWorker from "monaco-editor/editor/editor.worker?worker";

const MonacoDiffEditor = lazy(async () => {
  const scope = globalThis as typeof globalThis & { MonacoEnvironment?: { getWorker?: () => Worker } };
  if (!scope.MonacoEnvironment?.getWorker) {
    scope.MonacoEnvironment = { ...scope.MonacoEnvironment, getWorker: () => new EditorWorker() };
  }
  const [reactMonaco, monaco] = await Promise.all([
    import("@monaco-editor/react"),
    import("monaco-editor/editor/editor.api.js"),
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
  const lines = patch.split(/\r?\n/);
  let filePath = "";

  // Extract file path from header
  for (const line of lines) {
    if (line.startsWith("+++ b/")) {
      filePath = line.slice(6);
      break;
    }
    if (line.startsWith("+++ ") && line !== "+++ ") {
      filePath = line.slice(4);
      break;
    }
  }

  const origParts: string[] = [];
  const modParts: string[] = [];

  for (const line of lines) {
    if (
      line.startsWith("diff ") ||
      line.startsWith("index ") ||
      line.startsWith("--- ") ||
      line.startsWith("+++ ") ||
      line.startsWith("@@")
    ) {
      continue;
    }
    if (line.startsWith("+")) {
      modParts.push(line.slice(1));
    } else if (line.startsWith("-")) {
      origParts.push(line.slice(1));
    } else if (line.startsWith(" ")) {
      origParts.push(line.slice(1));
      modParts.push(line.slice(1));
    } else if (line === "") {
      // skip empty lines between hunks
    } else {
      origParts.push(line);
      modParts.push(line);
    }
  }

  return {
    original: origParts.join("\n"),
    modified: modParts.join("\n"),
    filePath,
  };
}

interface MonacoDiffViewProps {
  /** Unified diff patch string. If provided, will be parsed automatically. */
  patch?: string;
  /** Original content (left side). Overrides patch parsing. */
  original?: string;
  /** Modified content (right side). Overrides patch parsing. */
  modified?: string;
  /** Programming language for syntax highlighting in the diff editor. */
  language?: string;
  /** File path shown in the header bar. */
  filePath?: string;
  /** Editor height. */
  height?: string | number;
  /** Whether the editor content is read-only. */
  readOnly?: boolean;
  /** Callback for the Accept button. If omitted, the button is hidden. */
  onAccept?: () => void;
  /** Callback for the Reject button. If omitted, the button is hidden. */
  onReject?: () => void;
}

export function MonacoDiffView({
  patch,
  original: originalProp,
  modified: modifiedProp,
  language = "plaintext",
  filePath: filePathProp,
  height = 400,
  readOnly = true,
  onAccept,
  onReject,
}: MonacoDiffViewProps) {
  const editorRef = useRef<import("monaco-editor").editor.IStandaloneDiffEditor | null>(null);
  const [theme, setTheme] = useState<"vs-dark" | "vs">("vs-dark");

  // Sync with app theme
  useEffect(() => {
    const root = document.documentElement;
    const currentTheme = root.getAttribute("data-theme");
    setTheme(currentTheme === "light" ? "vs" : "vs-dark");

    const observer = new MutationObserver(() => {
      const t = root.getAttribute("data-theme");
      setTheme(t === "light" ? "vs" : "vs-dark");
    });
    observer.observe(root, {
      attributes: true,
      attributeFilter: ["data-theme"],
    });
    return () => observer.disconnect();
  }, []);

  const { original, modified, filePath } = useMemo(() => {
    if (originalProp != null && modifiedProp != null) {
      return {
        original: originalProp,
        modified: modifiedProp,
        filePath: filePathProp ?? "",
      };
    }
    if (patch) {
      const parsed = parseUnifiedDiffToOriginalModified(patch);
      return {
        original: parsed.original,
        modified: parsed.modified,
        filePath: filePathProp ?? parsed.filePath,
      };
    }
    return { original: "", modified: "", filePath: filePathProp ?? "" };
  }, [patch, originalProp, modifiedProp, filePathProp]);

  return (
    <div
      style={{
        border: "1px solid var(--border-subtle)",
        borderRadius: "6px",
        overflow: "hidden",
      }}
    >
      {/* Header */}
      {filePath && (
        <div
          style={{
            display: "flex",
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
          {(onAccept || onReject) && (
            <div style={{ display: "flex", gap: "8px" }}>
              {onReject && (
                <button type="button" onClick={onReject} style={rejectBtnStyle}>
                  拒绝
                </button>
              )}
              {onAccept && (
                <button type="button" onClick={onAccept} style={acceptBtnStyle}>
                  接受
                </button>
              )}
            </div>
          )}
        </div>
      )}
      {/* Editor */}
      <Suspense
        fallback={
          <div
            style={{
              height,
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
          height={height}
          language={language}
          original={original}
          modified={modified}
          theme={theme}
          options={{
            readOnly,
            renderSideBySide: true,
            minimap: { enabled: false },
            scrollBeyondLastLine: false,
            fontSize: 15,
            lineHeight: 23,
            lineNumbers: "on",
            wordWrap: "on",
            padding: { top: 8 },
            originalEditable: false,
          }}
          onMount={(editor: import("monaco-editor").editor.IStandaloneDiffEditor) => {
            editorRef.current = editor;
          }}
        />
      </Suspense>
    </div>
  );
}

const acceptBtnStyle: React.CSSProperties = {
  padding: "3px 12px",
  borderRadius: "var(--radius-sm)",
  border: "none",
  cursor: "pointer",
  background: "var(--accent-primary)",
  color: "var(--text-on-accent)",
  fontSize: "var(--text-xxs)",
};

const rejectBtnStyle: React.CSSProperties = {
  padding: "3px 12px",
  borderRadius: "var(--radius-sm)",
  border: "1px solid var(--border-soft)",
  cursor: "pointer",
  background: "transparent",
  color: "var(--text-secondary)",
  fontSize: "var(--text-xxs)",
};
