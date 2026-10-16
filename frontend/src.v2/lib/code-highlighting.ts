import { createHighlighterCore, type HighlighterCore, type ThemedToken } from "@shikijs/core";
import { createJavaScriptRegexEngine } from "@shikijs/engine-javascript";
import { loadCodeTheme, type CodeThemeId } from "./code-themes";
import type { AppearanceVariant } from "./appearance-colors";

// These are the languages offered by MiniCode's editors and code fences. Shiki
// loads their real TextMate grammars (including embedded languages) on demand.
const languages = {
  javascript: () => import("@shikijs/langs/javascript"), jsx: () => import("@shikijs/langs/jsx"),
  typescript: () => import("@shikijs/langs/typescript"), tsx: () => import("@shikijs/langs/tsx"),
  python: () => import("@shikijs/langs/python"), powershell: () => import("@shikijs/langs/powershell"),
  shellscript: () => import("@shikijs/langs/shellscript"), shellsession: () => import("@shikijs/langs/shellsession"),
  json: () => import("@shikijs/langs/json"), jsonc: () => import("@shikijs/langs/jsonc"),
  diff: () => import("@shikijs/langs/diff"), css: () => import("@shikijs/langs/css"),
  scss: () => import("@shikijs/langs/scss"), less: () => import("@shikijs/langs/less"),
  html: () => import("@shikijs/langs/html"), xml: () => import("@shikijs/langs/xml"),
  markdown: () => import("@shikijs/langs/markdown"), yaml: () => import("@shikijs/langs/yaml"),
  toml: () => import("@shikijs/langs/toml"), sql: () => import("@shikijs/langs/sql"),
  c: () => import("@shikijs/langs/c"), cpp: () => import("@shikijs/langs/cpp"),
  csharp: () => import("@shikijs/langs/csharp"), rust: () => import("@shikijs/langs/rust"),
  go: () => import("@shikijs/langs/go"), java: () => import("@shikijs/langs/java"),
  ruby: () => import("@shikijs/langs/ruby"), swift: () => import("@shikijs/langs/swift"),
  kotlin: () => import("@shikijs/langs/kotlin"), vue: () => import("@shikijs/langs/vue"),
  svelte: () => import("@shikijs/langs/svelte"), php: () => import("@shikijs/langs/php"),
  dockerfile: () => import("@shikijs/langs/dockerfile"), makefile: () => import("@shikijs/langs/makefile"),
};
type Language = keyof typeof languages;
const aliases: Record<string, string> = {
  js: "javascript", ts: "typescript", py: "python", ps1: "powershell",
  sh: "shellscript", bash: "shellscript", shell: "shellscript", zsh: "shellscript",
  "shell-session": "shellsession", markup: "html", svg: "xml", md: "markdown",
  yml: "yaml", cs: "csharp", "c++": "cpp", rs: "rust", rb: "ruby", kt: "kotlin",
};
export const codeHighlightLanguage = (language: string): Language | "text" => {
  const normalized = aliases[language.toLowerCase()] ?? language.toLowerCase();
  return Object.hasOwn(languages, normalized) ? normalized as Language : "text";
};

let engine: Promise<HighlighterCore> | undefined;
export const getCodeHighlighter = () => engine ??= createHighlighterCore({
  engine: createJavaScriptRegexEngine(), langs: [], themes: [],
});

export async function prepareCodeHighlighting(id: CodeThemeId, variant: AppearanceVariant, language?: string) {
  const [highlighter, loaded] = await Promise.all([getCodeHighlighter(), loadCodeTheme(id, variant)]);
  await highlighter.loadTheme({ ...loaded.theme, name: loaded.registrationName });
  const lang = codeHighlightLanguage(language ?? "text");
  if (lang !== "text") await highlighter.loadLanguage(languages[lang]);
  return { highlighter, loaded, lang };
}

export async function highlightCode(code: string, language: string, id: CodeThemeId, variant: AppearanceVariant): Promise<ThemedToken[][]> {
  const { highlighter, loaded, lang } = await prepareCodeHighlighting(id, variant, language);
  return highlighter.codeToTokensBase(code, { lang, theme: loaded.registrationName });
}
