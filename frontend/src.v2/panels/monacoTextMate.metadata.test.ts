/* @vitest-environment jsdom */
import { readFileSync } from "node:fs";
import { afterEach, expect, it, vi } from "vitest";
import * as Monaco from "monaco-editor/editor/editor.api.js";
import { TokenizationRegistry, type NativeTokenizationSupport } from "monaco-editor/editor/common/languages.js";
import { TokenMetadata } from "monaco-editor/editor/common/encodedTokenAttributes.js";
import { defineMiniCodeMonacoTheme, miniCodeMonacoThemeName } from "./monacoTheme";
import { applyAppearanceColors, emptyAppearanceColors } from "../lib/appearance-colors";
import { defaultThemePreferences } from "../lib/appearance-themes";
import { loadCodeTheme } from "../lib/code-themes";

vi.hoisted(() => {
  Object.defineProperty(document, "queryCommandSupported", { configurable: true, value: () => false });
  vi.stubGlobal("CSS", { escape: (value: string) => value });
  Object.defineProperty(window, "matchMedia", { configurable: true, value: () => ({
    matches: false, media: "", addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {},
  }) });
});
await import("monaco-editor/languages/definitions/javascript/register.js");
await import("monaco-editor/languages/definitions/css/register.js");
await import("monaco-editor/languages/definitions/html/register.js");
const themeTokens = readFileSync("src.v2/styles/tokens.css", "utf8");
const semanticMask = ~(0x00ff8000 | 0x00007800);

function installTokens() {
  const dark = themeTokens.split('\n[data-theme="light"] {')[0];
  for (const [, key, value] of dark.matchAll(/(--[\w-]+)\s*:\s*([^;]+);/g)) document.documentElement.style.setProperty(key, value.trim());
}
function capture(provider: NativeTokenizationSupport, lines: string[]) {
  let state = provider.getInitialState();
  return lines.map(line => {
    const result = provider.tokenizeEncoded(line, true, state);
    state = result.endState;
    return result.tokens;
  });
}
function at(tokens: Uint32Array, offset: number) {
  let index = 0;
  while (index + 2 < tokens.length && tokens[index + 2] <= offset) index += 2;
  return tokens[index + 1];
}
async function select(id: "github" | "dracula") {
  const { chromeSeed } = await loadCodeTheme(id, "dark");
  applyAppearanceColors("dark", { light: null, dark: chromeSeed }, { light: null,
    dark: { codeThemeId: id, ...defaultThemePreferences("dark"), semanticColors: chromeSeed.semanticColors } });
  await defineMiniCodeMonacoTheme(Monaco, "dark");
  Monaco.editor.setTheme(miniCodeMonacoThemeName("dark"));
}
afterEach(async () => {
  applyAppearanceColors("dark", emptyAppearanceColors());
  installTokens();
  await defineMiniCodeMonacoTheme(Monaco, "dark");
  document.documentElement.removeAttribute("style");
});

it("retains native comment/string/regex and independent multiline states on the same dirty JS models", async () => {
  installTokens();
  const native = (await TokenizationRegistry.getOrCreate("javascript"))!;
  const model = Monaco.editor.createModel('// comment\nconst text = "quoted";\nconst re = /a+/;\n/* start\ncontinued\n*/', "javascript");
  const other = Monaco.editor.createModel('const other = "second";', "javascript");
  model.pushEditOperations(null, [{ range: new Monaco.Range(1, 1, 1, 1), text: "// unsaved\n" }], () => null);
  const source = model.getValue(), version = model.getVersionId(), undo = model.canUndo();
  const lines = source.split("\n"), baseline = capture(native, lines);
  try {
    for (const id of ["github", "dracula"] as const) {
      await select(id);
      const support = (await TokenizationRegistry.getOrCreate("javascript"))!;
      const actual = capture(support, lines);
      for (let line = 0; line < lines.length; line++) {
        for (let column = 0; column < Math.max(1, lines[line].length); column++) {
          expect(at(actual[line], column) & semanticMask).toBe(at(baseline[line], column) & semanticMask);
        }
      }
      expect(TokenMetadata.getTokenType(at(actual[1], 3))).toBe(1);
      expect(TokenMetadata.getTokenType(at(actual[2], lines[2].indexOf("quoted")))).toBe(2);
      expect(TokenMetadata.getTokenType(at(actual[3], lines[3].indexOf("a+")))).toBe(3);
      const cleanState = support.getInitialState();
      const inComment = support.tokenizeEncoded("/* open", true, cleanState).endState;
      expect(inComment.equals(inComment.clone())).toBe(true);
      expect(inComment.equals(cleanState)).toBe(false);
      const independent = capture(support, [other.getValue()]);
      expect(TokenMetadata.getTokenType(at(independent[0], 7))).toBe(0);
      expect(model.getValue()).toBe(source);
      expect(model.getVersionId()).toBe(version);
      expect(model.canUndo()).toBe(undo);
      expect(Monaco.editor.tokenize("// classic API", "javascript")[0][0].type).toMatch(/^comment/);
    }
  } finally { model.dispose(); other.dispose(); }
});

it("preserves HTML script/style embedded language IDs and native boundaries while recoloring existing models", async () => {
  installTokens();
  await TokenizationRegistry.getOrCreate("javascript");
  await TokenizationRegistry.getOrCreate("css");
  const native = (await TokenizationRegistry.getOrCreate("html"))!;
  const source = '<style>\n.x { color: red; }\n</style>\n<script>\nconst value = "text"; // comment\n</script>';
  const html = Monaco.editor.createModel(source, "html");
  const js = Monaco.editor.createModel('const existing = "kept";', "javascript");
  const version = html.getVersionId(), lines = source.split("\n");
  const baseline = capture(native, lines);
  expect(TokenMetadata.getLanguageId(at(baseline[1], 2))).toBe(Monaco.languages.getEncodedLanguageId("css"));
  expect(TokenMetadata.getLanguageId(at(baseline[4], 3))).toBe(Monaco.languages.getEncodedLanguageId("javascript"));
  try {
    await select("dracula");
    const support = (await TokenizationRegistry.getOrCreate("html"))!;
    const actual = capture(support, lines);
    for (let line = 0; line < lines.length; line++) {
      for (let column = 0; column < lines[line].length; column++) {
        expect(at(actual[line], column) & semanticMask).toBe(at(baseline[line], column) & semanticMask);
      }
    }
    expect(html.getValue()).toBe(source);
    expect(html.getVersionId()).toBe(version);
    expect(js.getValue()).toBe('const existing = "kept";');
    expect(TokenMetadata.getTokenType(at(actual[4], lines[4].indexOf("text")))).toBe(2);
  } finally { html.dispose(); js.dispose(); }
});
