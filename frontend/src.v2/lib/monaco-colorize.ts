import { useEffect, useState } from "react";
import { useAppStore } from "../stores";
import { defineMiniCodeMonacoTheme, miniCodeMonacoThemeName } from "../panels/monacoTheme";
export { extractFilePathFromDiff } from "./unified-diff";

type Monaco = typeof import("monaco-editor/editor/editor.api.js");

let monacoInstance: Monaco | null = null;
let monacoLoading: Promise<Monaco> | null = null;
const SAFE_STYLE_PROPERTIES = new Set(["color", "background-color", "font-style", "font-weight", "text-decoration"]);

const languageLoaders: Record<string, () => Promise<unknown>> = {
  typescript: () => import("monaco-editor/languages/definitions/typescript/register.js"),
  javascript: () => import("monaco-editor/languages/definitions/javascript/register.js"),
  python: () => import("monaco-editor/languages/definitions/python/register.js"),
  rust: () => import("monaco-editor/languages/definitions/rust/register.js"),
  go: () => import("monaco-editor/languages/definitions/go/register.js"),
  java: () => import("monaco-editor/languages/definitions/java/register.js"),
  ruby: () => import("monaco-editor/languages/definitions/ruby/register.js"),
  css: () => import("monaco-editor/languages/definitions/css/register.js"),
  html: () => import("monaco-editor/languages/definitions/html/register.js"),
  yaml: () => import("monaco-editor/languages/definitions/yaml/register.js"),
  markdown: () => import("monaco-editor/languages/definitions/markdown/register.js"),
  shell: () => import("monaco-editor/languages/definitions/shell/register.js"),
  sql: () => import("monaco-editor/languages/definitions/sql/register.js"),
  c: () => import("monaco-editor/languages/definitions/cpp/register.js"),
  cpp: () => import("monaco-editor/languages/definitions/cpp/register.js"),
  csharp: () => import("monaco-editor/languages/definitions/csharp/register.js"),
  swift: () => import("monaco-editor/languages/definitions/swift/register.js"),
  kotlin: () => import("monaco-editor/languages/definitions/kotlin/register.js"),
};
const loadedLanguages = new Set<string>();

async function getMonaco(language: string): Promise<Monaco> {
  if (!monacoLoading) {
    monacoLoading = import("monaco-editor/editor/editor.api.js").then((monaco) => {
      monacoInstance = monaco;
      return monaco;
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
  const [result, setResult] = useState<{ lines: typeof lines; language: string; theme: string; html: string[] } | null>(null);
  const theme = useAppStore((state) => state.resolvedTheme);

  useEffect(() => {
    if (language === "plaintext" || lines.length === 0) {
      setResult(null);
      return;
    }

    let canceled = false;
    const codeLines = lines
      .filter((l) => l.kind === "add" || l.kind === "del" || l.kind === "context")
      .map((l) => l.text);

    if (codeLines.length === 0) {
      setResult(null);
      return;
    }

    const code = codeLines.join("\n");

    getMonaco(language).then((monaco) => {
      if (canceled) return null;
      defineMiniCodeMonacoTheme(monaco, theme);
      monaco.editor.setTheme(miniCodeMonacoThemeName(theme));
      return monaco.editor.colorize(code, language, { tabSize: 2 });
    }).then((html) => {
        if (canceled) return;
        const htmlLines = html!.split("<br/>");
        const htmlByLine: string[] = [];
        let codeIdx = 0;
        for (const line of lines) {
          if (line.kind === "add" || line.kind === "del" || line.kind === "context") {
            htmlByLine.push(sanitizeColorizedHtml(htmlLines[codeIdx] ?? ""));
            codeIdx++;
          } else {
            htmlByLine.push("");
          }
        }
        setResult({ lines, language, theme, html: htmlByLine });
    }).catch(() => {
      if (!canceled) setResult(null);
    });
    return () => { canceled = true; };
  }, [lines, language, theme]);

  return result?.lines === lines && result.language === language && result.theme === theme ? result.html : null;
}
