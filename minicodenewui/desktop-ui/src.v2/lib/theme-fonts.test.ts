/* @vitest-environment jsdom */
import { afterEach, expect, it, vi } from "vitest";
import { appearanceThemeSelectionSchema } from "./appearance-themes";
import { codeFontFamily, cssFontFamily, firstFontFamily, fontStack, prepareThemeFonts, themeFontAlias } from "./theme-fonts";

const documentFontsDescriptor = Object.getOwnPropertyDescriptor(document, "fonts");
afterEach(() => {
  vi.unstubAllGlobals();
  if (documentFontsDescriptor) Object.defineProperty(document, "fonts", documentFontsDescriptor);
  else Reflect.deleteProperty(document, "fonts");
  document.documentElement.style.removeProperty("--editor-font-family");
  document.documentElement.style.removeProperty("--font-mono");
});
it("retains quoted family lists and binds a local face only to its actual family", () => {
  const face = { family: "Fixture, Fancy", fullName: "Fixture, Fancy Medium", postscriptName: "Fixture-Medium" };
  expect(firstFontFamily('"Fixture, Fancy", monospace')).toBe("Fixture, Fancy");
  expect(firstFontFamily('"Quote\\\" Font", serif')).toBe('Quote" Font');
  expect(fontStack('"Fixture, Fancy", serif', face)).toContain(cssFontFamily(themeFontAlias(face)));
  expect(fontStack("Other font", face)).not.toContain("selected");
  expect(fontStack("var(--my-font)")).toBe("var(--my-font)");
});

it("loads the exact native PostScript/full-name face once and persists its complete identity", async () => {
  const face = { family: "Fixture Family", fullName: "Fixture Condensed Medium", postscriptName: "Fixture-CondensedMedium" };
  const calls: Array<{ family: string; source: string }> = [];
  const add = vi.fn();
  Object.defineProperty(document, "fonts", { configurable: true, value: { add } });
  vi.stubGlobal("FontFace", class { constructor(family: string, source: string) { calls.push({ family, source }); } async load() { return this; } });
  const fonts = { ui: '"Fixture Family"', content: null, code: null, uiFace: face };
  await Promise.all([prepareThemeFonts(fonts), prepareThemeFonts(fonts)]);
  expect(calls).toEqual([{ family: themeFontAlias(face), source: 'local("Fixture-CondensedMedium"), local("Fixture Condensed Medium")' }]);
  expect(add).toHaveBeenCalledTimes(1);
  const saved = appearanceThemeSelectionSchema.parse({ codeThemeId: "vscode-plus", contrast: 60, opaqueWindows: false,
    semanticColors: { diffAdded: "#00aa00", diffRemoved: "#aa0000", skill: "#9900ff" }, fonts });
  expect(saved.fonts?.uiFace).toEqual(face);
});

it("passes real font stacks to Canvas consumers rather than unresolved CSS variable syntax", () => {
  document.documentElement.style.setProperty("--editor-font-family", "var(--font-mono)");
  document.documentElement.style.setProperty("--font-mono", '"MiniCode selected Fixture-Medium", "Fixture", monospace');
  expect(codeFontFamily()).toBe('"MiniCode selected Fixture-Medium", "Fixture", monospace');
  document.documentElement.style.setProperty("--editor-font-family", "Authored Editor Font");
  expect(codeFontFamily()).toBe("Authored Editor Font");
});
