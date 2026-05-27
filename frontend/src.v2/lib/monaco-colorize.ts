import { useEffect, useRef, useState } from "react";

type Monaco = typeof import("monaco-editor");

let monacoInstance: Monaco | null = null;
let monacoLoading: Promise<Monaco> | null = null;

async function getMonaco(): Promise<Monaco> {
  if (monacoInstance) return monacoInstance;
  if (!monacoLoading) {
    monacoLoading = import("monaco-editor").then((m) => {
      monacoInstance = m;
      return m;
    });
  }
  return monacoLoading;
}

const EXT_TO_LANG: Record<string, string> = {
  ts: "typescript", tsx: "typescript",
  js: "javascript", jsx: "javascript",
  py: "python",
  rs: "rust",
  go: "go",
  java: "java",
  rb: "ruby",
  css: "css", scss: "css",
  html: "html", htm: "html",
  json: "json",
  yaml: "yaml", yml: "yaml",
  toml: "toml",
  md: "markdown",
  sh: "shell", bash: "shell",
  sql: "sql",
  c: "c", h: "c",
  cpp: "cpp", hpp: "cpp", cc: "cpp",
  cs: "csharp",
  swift: "swift",
  kt: "kotlin",
  vue: "html",
  svelte: "html",
};

export function guessLanguageFromPath(path: string): string {
  const ext = path.split(".").pop()?.toLowerCase() ?? "";
  return EXT_TO_LANG[ext] ?? "plaintext";
}

export function extractFilePathFromDiff(lines: { kind: string; text: string }[]): string {
  for (const line of lines) {
    if (line.kind === "meta") {
      const match = line.text.match(/^\+\+\+ b\/(.+)$/) ?? line.text.match(/^--- a\/(.+)$/);
      if (match) return match[1];
    }
  }
  return "";
}

export function useColorizedLines(
  lines: { kind: string; text: string }[],
  language: string,
): string[] | null {
  const [colorized, setColorized] = useState<string[] | null>(null);
  const versionRef = useRef(0);

  useEffect(() => {
    if (language === "plaintext" || lines.length === 0) {
      setColorized(null);
      return;
    }

    const version = ++versionRef.current;
    const codeLines = lines
      .filter((l) => l.kind === "add" || l.kind === "del" || l.kind === "context")
      .map((l) => l.text);

    if (codeLines.length === 0) {
      setColorized(null);
      return;
    }

    const code = codeLines.join("\n");

    getMonaco().then((monaco) => {
      if (versionRef.current !== version) return;
      monaco.editor.colorize(code, language, { tabSize: 2 }).then((html) => {
        if (versionRef.current !== version) return;
        const htmlLines = html.split("<br/>");
        const result: string[] = [];
        let codeIdx = 0;
        for (const line of lines) {
          if (line.kind === "add" || line.kind === "del" || line.kind === "context") {
            result.push(htmlLines[codeIdx] ?? "");
            codeIdx++;
          } else {
            result.push("");
          }
        }
        setColorized(result);
      });
    });
  }, [lines, language]);

  return colorized;
}
