import { useEffect, useRef, useState } from "react";

type Monaco = typeof import("monaco-editor/esm/vs/editor/editor.api.js");

let monacoInstance: Monaco | null = null;
let monacoLoading: Promise<Monaco> | null = null;
const SAFE_STYLE_PROPERTIES = new Set(["color", "background-color", "font-style", "font-weight", "text-decoration"]);

const languageLoaders: Record<string, () => Promise<unknown>> = {
  typescript: () => import("monaco-editor/esm/vs/basic-languages/typescript/typescript.contribution.js"),
  javascript: () => import("monaco-editor/esm/vs/basic-languages/javascript/javascript.contribution.js"),
  python: () => import("monaco-editor/esm/vs/basic-languages/python/python.contribution.js"),
  rust: () => import("monaco-editor/esm/vs/basic-languages/rust/rust.contribution.js"),
  go: () => import("monaco-editor/esm/vs/basic-languages/go/go.contribution.js"),
  java: () => import("monaco-editor/esm/vs/basic-languages/java/java.contribution.js"),
  ruby: () => import("monaco-editor/esm/vs/basic-languages/ruby/ruby.contribution.js"),
  css: () => import("monaco-editor/esm/vs/basic-languages/css/css.contribution.js"),
  html: () => import("monaco-editor/esm/vs/basic-languages/html/html.contribution.js"),
  yaml: () => import("monaco-editor/esm/vs/basic-languages/yaml/yaml.contribution.js"),
  markdown: () => import("monaco-editor/esm/vs/basic-languages/markdown/markdown.contribution.js"),
  shell: () => import("monaco-editor/esm/vs/basic-languages/shell/shell.contribution.js"),
  sql: () => import("monaco-editor/esm/vs/basic-languages/sql/sql.contribution.js"),
  c: () => import("monaco-editor/esm/vs/basic-languages/cpp/cpp.contribution.js"),
  cpp: () => import("monaco-editor/esm/vs/basic-languages/cpp/cpp.contribution.js"),
  csharp: () => import("monaco-editor/esm/vs/basic-languages/csharp/csharp.contribution.js"),
  swift: () => import("monaco-editor/esm/vs/basic-languages/swift/swift.contribution.js"),
  kotlin: () => import("monaco-editor/esm/vs/basic-languages/kotlin/kotlin.contribution.js"),
};
const loadedLanguages = new Set<string>();

async function getMonaco(language: string): Promise<Monaco> {
  if (!monacoLoading) {
    monacoLoading = import("monaco-editor/esm/vs/editor/editor.api.js").then((m) => {
      monacoInstance = m;
      return m;
    });
  }
  const monaco = monacoInstance ?? await monacoLoading;
  if (!loadedLanguages.has(language)) {
    await languageLoaders[language]?.();
    loadedLanguages.add(language);
  }
  return monaco;
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

export function sanitizeColorizedHtml(rawHtml: string): string {
  if (!rawHtml) return "";
  const template = document.createElement("template");
  template.innerHTML = rawHtml;

  template.content.querySelectorAll("*").forEach((node) => {
    const tag = node.tagName.toLowerCase();
    if (tag !== "span") {
      node.replaceWith(document.createTextNode(node.textContent ?? ""));
      return;
    }

    for (const attr of Array.from(node.attributes)) {
      const name = attr.name.toLowerCase();
      if (name === "class") continue;
      if (name === "style") {
        const safeDeclarations = attr.value
          .split(";")
          .map((part) => part.trim())
          .filter(Boolean)
          .filter((part) => {
            const [rawProperty, ...rawValue] = part.split(":");
            const property = rawProperty?.trim().toLowerCase();
            const value = rawValue.join(":").trim().toLowerCase();
            return (
              SAFE_STYLE_PROPERTIES.has(property) &&
              Boolean(value) &&
              !/(?:url|expression|javascript:|data:|@import)/i.test(value)
            );
          });
        if (safeDeclarations.length > 0) {
          node.setAttribute("style", safeDeclarations.join("; "));
        } else {
          node.removeAttribute(attr.name);
        }
        continue;
      }
      node.removeAttribute(attr.name);
    }
  });

  return template.innerHTML;
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

    getMonaco(language).then((monaco) => {
      if (versionRef.current !== version) return;
      monaco.editor.colorize(code, language, { tabSize: 2 }).then((html) => {
        if (versionRef.current !== version) return;
        const htmlLines = html.split("<br/>");
        const result: string[] = [];
        let codeIdx = 0;
        for (const line of lines) {
          if (line.kind === "add" || line.kind === "del" || line.kind === "context") {
            result.push(sanitizeColorizedHtml(htmlLines[codeIdx] ?? ""));
            codeIdx++;
          } else {
            result.push("");
          }
        }
        setColorized(result);
      }).catch(() => {
        if (versionRef.current === version) setColorized(null);
      });
    }).catch(() => {
      if (versionRef.current === version) setColorized(null);
    });
  }, [lines, language]);

  return colorized;
}
