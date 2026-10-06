/* @vitest-environment jsdom */
import { afterEach, describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import * as Monaco from "monaco-editor/editor/editor.api.js";
import { TokenizationRegistry } from "monaco-editor/editor/common/languages.js";
import { TokenMetadata } from "monaco-editor/editor/common/encodedTokenAttributes.js";
import { StandaloneServices } from "monaco-editor/editor/standalone/browser/standaloneServices.js";
import { IStandaloneThemeService } from "monaco-editor/editor/standalone/common/standaloneTheme.js";
import { defineMiniCodeMonacoTheme, miniCodeMonacoThemeName } from "./monacoTheme";

vi.hoisted(() => {
  Object.defineProperty(document, "queryCommandSupported", { configurable: true, value: () => false });
  vi.stubGlobal("CSS", { escape: (value: string) => value });
  Object.defineProperty(window, "matchMedia", {
    configurable: true,
    value: () => ({ matches: false, media: "", addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} }),
  });
});

await import("monaco-editor/languages/definitions/cpp/register.js");
await import("monaco-editor/languages/definitions/javascript/register.js");
const themeTokens = readFileSync("src.v2/styles/tokens.css", "utf8");
const applyThemeTokens = (theme: "light" | "dark") => {
  const [darkTokens, lightTokens] = themeTokens.split('\n[data-theme="light"] {');
  const source = theme === "light" ? darkTokens + lightTokens : darkTokens;
  for (const [, name, value] of source.matchAll(/(--[\w-]+)\s*:\s*([^;]+);/g)) {
    document.documentElement.style.setProperty(name, value.trim());
  }
};

afterEach(() => document.documentElement.removeAttribute("style"));

describe("MiniCode native code syntax", () => {
  it.each(["dark", "light"] as const)("renders native C++ and JS comments in green with distinct syntax in %s mode", async (theme) => {
    applyThemeTokens(theme);
    expect(getComputedStyle(document.documentElement).getPropertyValue("--editor-foreground").trim()).toBe(theme === "light" ? "#30343b" : "#d4d4d4");
    defineMiniCodeMonacoTheme(Monaco, theme);
    Monaco.editor.setTheme(miniCodeMonacoThemeName(theme));
    for (const [language, code] of [
      ["cpp", '// 中文注释\nconst char* message = "Hello";\n/* 多行\n * 注释\n */'],
      ["javascript", '// 中文注释\nconst message = "Hello";\n/* 多行\n * 注释\n */'],
    ]) {
      const native = await TokenizationRegistry.getOrCreate(language);
      const tokens = Monaco.editor.tokenize(code, language).flat().map((token) => token.type);
      expect(tokens.some((token) => token.startsWith("comment"))).toBe(true);
      expect(tokens.some((token) => token.startsWith("keyword"))).toBe(true);
      expect(tokens.some((token) => token.startsWith("string"))).toBe(true);
      const comment = native!.tokenizeEncoded("// 中文注释", true, native!.getInitialState());
      expect(TokenMetadata.getTokenType(comment.tokens[1])).toBe(1);
      const color = TokenizationRegistry.getColorMap()![TokenMetadata.getForeground(comment.tokens[1])].rgba;
      expect([color.r, color.g, color.b]).toEqual(theme === "light" ? [0, 128, 0] : [106, 153, 85]);
    }
  });

  it.each(["dark", "light"] as const)("keeps focused completion text, icons and matches readable on the neutral selection in %s mode", (mode) => {
    applyThemeTokens(mode);
    defineMiniCodeMonacoTheme(Monaco, mode);
    Monaco.editor.setTheme(miniCodeMonacoThemeName(mode));
    const theme = StandaloneServices.get(IStandaloneThemeService).getColorTheme();
    const selected = theme.getColor("editorSuggestWidget.selectedBackground")!.rgba;
    const background = theme.getColor("editorSuggestWidget.background")!.rgba;
    const luminance = (color: { r: number; g: number; b: number }) => {
      const [r, g, b] = [color.r, color.g, color.b].map(value => {
        const channel = value / 255;
        return channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4;
      });
      return 0.2126 * r + 0.7152 * g + 0.0722 * b;
    };
    for (const [foregroundId, surface] of [
      ["editorSuggestWidget.selectedForeground", selected],
      ["editorSuggestWidget.selectedIconForeground", selected],
      ["editorSuggestWidget.focusHighlightForeground", selected],
      ["editorSuggestWidget.highlightForeground", background],
    ] as const) {
      const foreground = theme.getColor(foregroundId)!.rgba;
      expect(foreground.a).toBe(1);
      const foregroundLight = luminance(foreground);
      const surfaceLight = luminance(surface);
      expect((Math.max(foregroundLight, surfaceLight) + 0.05) / (Math.min(foregroundLight, surfaceLight) + 0.05)).toBeGreaterThanOrEqual(4.5);
      expect(Math.max(foreground.r, foreground.g, foreground.b) - Math.min(foreground.r, foreground.g, foreground.b)).toBeLessThan(16);
    }
  });

  it("changes a plain text scratch model to C++ without changing its source", async () => {
    const source = "// 中文注释\nint answer = 42;";
    const model = Monaco.editor.createModel(source, "plaintext", Monaco.Uri.parse("inmemory://syntax-verification/example.txt"));
    try {
      expect(Monaco.editor.tokenize(source, "plaintext")[0].every((token) => !token.type.startsWith("comment"))).toBe(true);
      Monaco.editor.setModelLanguage(model, "cpp");
      await TokenizationRegistry.getOrCreate("cpp");
      expect(model.getLanguageId()).toBe("cpp");
      expect(Monaco.editor.tokenize(model.getValue(), "cpp")[0][0].type).toMatch(/^comment/);
      expect(model.getValue()).toBe(source);
    } finally {
      model.dispose();
    }
  });
});
