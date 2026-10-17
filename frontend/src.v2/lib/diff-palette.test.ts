import { describe, expect, it } from "vitest";
import { createDiffPalette, diffPaletteStyle, monacoDiffPaletteColors } from "./diff-palette";

// Independent Chrome computed CSS results from the installed Source caller.
// output/diff-theme-caller-20261017/computed-css.json, chromeChanged states.
const references = {
  light: { fg: "#1f2328", bg: "#ffffff", line: ["#ebf9ee", "#ffebe6"], gutter: ["#f0fbf2", "#fff0ec"], word: ["#40c97726", "#fa423e26"] },
  dark: { fg: "#e6edf3", bg: "#0d1117", line: ["#1c3129", "#3a1f1f"], gutter: ["#132017", "#28130e"], word: ["#40c97733", "#fa423e33"] },
};
const chrome = { surface: "#345678", ink: "#abcdef", accent: "#2468ac" };
const semantic = { diffAdded: "#40c977", diffRemoved: "#fa423e" };

describe("actual app Diff palette", () => {
  it.each(["light", "dark"] as const)("matches independent Source computed line, gutter and word colors in %s", variant => {
    const reference = references[variant];
    const palette = createDiffPalette(variant, reference, chrome, semantic);
    expect(palette).toEqual({
      background: chrome.surface, foreground: reference.fg, addition: semantic.diffAdded, deletion: semantic.diffRemoved,
      additionLine: reference.line[0], deletionLine: reference.line[1],
      additionGutter: reference.gutter[0], deletionGutter: reference.gutter[1],
      additionWord: reference.word[0], deletionWord: reference.word[1],
    });
    const recoloredChrome = createDiffPalette(variant, reference, { ...chrome, surface: "#102030", ink: "#fedcba" }, semantic);
    expect(recoloredChrome).toEqual({ ...palette, background: "#102030" });
    const monaco = monacoDiffPaletteColors(palette);
    expect(monaco["diffEditor.insertedLineBackground"]).toBe(reference.line[0]);
    expect(monaco["diffEditor.insertedTextBackground"]).toBe(reference.word[0]);
    expect(monaco["diffEditorGutter.insertedLineBackground"]).toBe(reference.gutter[0]);
  });

  it.each(["light", "dark"] as const)("keeps the native default raw color source while painting the chrome surface in %s", variant => {
    const style = diffPaletteStyle(null, variant);
    expect(style["--diff-view-background"]).toBe("var(--surface-base)");
    expect(style["--diff-view-foreground"]).toBe("var(--editor-foreground)");
    expect(style["--diff-added-background"]).toBe(`color-mix(in lab, var(--editor-background) ${variant === "light" ? 88 : 80}%, var(--diff-added-foreground))`);
    expect(Object.keys(style).some(key => /font|weight/.test(key))).toBe(false);
  });
});
