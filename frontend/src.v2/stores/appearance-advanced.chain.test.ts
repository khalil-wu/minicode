/* @vitest-environment jsdom */
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { installAppThemeTokens } from "../../test/app-theme-tokens";
import { emptyAppearanceColors } from "../lib/appearance-colors";
import { emptyAppearanceThemes, defaultThemePreferences } from "../lib/appearance-themes";
import { exportThemeString, parseThemeString } from "../lib/theme-sharing";
import { readAppearanceVariant } from "../lib/appearance-storage";
import { prepareThemeFonts } from "../lib/theme-fonts";
import { applyCodeTextScale, initialCodeFontBase, initialCodeTextScale, LS, readAppearancePreferences } from "./shared-helpers";
import { useAppStore } from ".";

const media = vi.hoisted(() => ({ matches: false, callbacks: new Set<() => void>() }));
const mediaQuery = vi.hoisted(() => (query: string) => ({
  get matches() { return query === "(prefers-reduced-motion: reduce)" && media.matches; },
  addEventListener: (_name: string, callback: () => void) => { if (query === "(prefers-reduced-motion: reduce)") media.callbacks.add(callback); },
  removeEventListener: (_name: string, callback: () => void) => media.callbacks.delete(callback),
}));
vi.hoisted(() => Object.defineProperty(globalThis, "matchMedia", { writable: true, value: mediaQuery }));
vi.mock("../lib/code-highlighting", () => ({ prepareCodeHighlighting: vi.fn(async () => {}) }));
let removeTokens: () => void;
const documentFontsDescriptor = Object.getOwnPropertyDescriptor(document, "fonts");
beforeEach(() => {
  vi.stubGlobal("matchMedia", mediaQuery);
  localStorage.clear(); media.matches = false; removeTokens = installAppThemeTokens();
  useAppStore.setState({ ...readAppearancePreferences(), appearanceColors: emptyAppearanceColors(), appearanceThemes: emptyAppearanceThemes() });
  useAppStore.getState().setThemeMode("dark");
});
afterEach(() => {
  removeTokens(); localStorage.clear(); vi.restoreAllMocks(); vi.unstubAllGlobals();
  if (documentFontsDescriptor) Object.defineProperty(document, "fonts", documentFontsDescriptor);
  else Reflect.deleteProperty(document, "fonts");
});
const notify = (key: string) => window.dispatchEvent(new StorageEvent("storage", { storageArea: localStorage, key }));

it("preserves a legacy explicit code scale's absolute code/editor sizes across restart and peer updates", () => {
  localStorage.setItem(LS.codeTextScale, "1.25");
  expect(initialCodeFontBase()).toBe(14); expect(initialCodeTextScale()).toBe(1.25);
  useAppStore.setState(readAppearancePreferences());
  useAppStore.getState().setCodeTextScale(1.25);
  expect(localStorage.getItem(LS.codeFontBase)).toBe("14");
  expect(readAppearancePreferences()).toMatchObject({ codeFontBase: 14, codeTextScale: 1.25 });
  expect(document.documentElement.style.getPropertyValue("--code-font-size")).toBe("17.5px");
  expect(14 * useAppStore.getState().codeTextScale).toBe(17.5);
  localStorage.setItem(LS.codeFontBase, "14"); localStorage.setItem(LS.codeTextScale, "1.125"); notify(LS.codeTextScale);
  expect(useAppStore.getState()).toMatchObject({ codeFontBase: 14, codeTextScale: 1.125 });
  expect(document.documentElement.style.getPropertyValue("--code-font-size")).toBe("15.75px");
});

it("uses Source 12 for new code defaults and does not reinterpret new authored scales as legacy", () => {
  expect(readAppearancePreferences()).toMatchObject({ codeFontBase: 12, codeTextScale: 1, textScale: 1 });
  applyCodeTextScale(1, 12); expect(document.documentElement.style.getPropertyValue("--code-font-size")).toBe("12px");
  useAppStore.getState().setCodeTextScale(1.5);
  expect(readAppearancePreferences()).toMatchObject({ codeFontBase: 12, codeTextScale: 1.5 });
  expect(document.documentElement.style.getPropertyValue("--code-font-size")).toBe("18px");
});

it("applies all three motion preferences and continues following real system changes in System mode", () => {
  const state = useAppStore.getState(); media.matches = true;
  state.setReducedMotionPreference("off"); expect(useAppStore.getState().reducedMotion).toBe(false);
  expect(document.documentElement.dataset.reducedMotion).toBe("false");
  state.setReducedMotionPreference("system"); expect(useAppStore.getState().reducedMotion).toBe(true);
  media.matches = false; media.callbacks.forEach(callback => callback());
  expect(useAppStore.getState().reducedMotion).toBe(false);
  state.setReducedMotionPreference("on"); expect(useAppStore.getState().reducedMotion).toBe(true);
  localStorage.setItem(LS.reducedMotion, "off"); notify(LS.reducedMotion);
  expect(useAppStore.getState()).toMatchObject({ reducedMotionPreference: "off", reducedMotion: false });
});

it("preserves local family and face choices through color-theme import/export and peer restore", async () => {
  const fonts = { ui: '"Authored UI"', content: null, code: '"Authored Code"',
    uiFace: { family: "Authored UI", fullName: "Authored UI Condensed", postscriptName: "AuthoredUI-Condensed" } };
  await useAppStore.getState().patchAppearanceTheme("dark", { fonts });
  expect(readAppearanceVariant("dark").theme).toMatchObject({ codeThemeId: "codex", fonts });
  expect(useAppStore.getState().appearanceColors.dark).toBeNull();
  const snapshot = { codeThemeId: "github" as const, theme: { ...defaultThemePreferences("dark"), accent: "#2468ac", surface: "#101010", ink: "#eeeeee" } };
  const exported = JSON.parse(exportThemeString({ ...snapshot, theme: { ...snapshot.theme, fonts } }, "dark").slice(15));
  expect(exported.theme.fonts).toEqual({ ui: null, code: null });
  await useAppStore.getState().importAppearanceTheme("dark", parseThemeString(exportThemeString(snapshot, "dark"), "dark"));
  expect(useAppStore.getState().appearanceThemes.dark?.fonts).toEqual(fonts);
  expect(document.documentElement.style.getPropertyValue("--font-ui")).toContain("MiniCode selected AuthoredUI-Condensed");
  notify(LS.appearanceDark); expect(useAppStore.getState().appearanceThemes.dark?.fonts).toEqual(fonts);
});

it("still saves theme colors and contrast when an already saved local face can no longer load", async () => {
  const fonts = { ui: '"Saved Special Font"', content: null, code: null,
    uiFace: { family: "Saved Special Font", fullName: "Saved Special Font Medium", postscriptName: "SavedSpecial-Medium" } };
  await useAppStore.getState().patchAppearanceTheme("dark", { fonts });
  const unavailable = new Error("The saved local font file was removed");
  const warning = vi.spyOn(console, "warn").mockImplementation(() => {});
  const add = vi.fn();
  Object.defineProperty(document, "fonts", { configurable: true, value: { add } });
  vi.stubGlobal("FontFace", class { async load() { throw unavailable; } });
  const snapshot = { codeThemeId: "github" as const,
    theme: { ...defaultThemePreferences("dark"), accent: "#2468ac", surface: "#101010", ink: "#eeeeee" } };
  await useAppStore.getState().importAppearanceTheme("dark", parseThemeString(exportThemeString(snapshot, "dark"), "dark"));
  await useAppStore.getState().patchAppearanceTheme("dark", { contrast: 82 });
  useAppStore.getState().setAppearanceColor("dark", "accent", "#ddee66");
  await prepareThemeFonts(fonts);
  expect(useAppStore.getState().appearanceThemes.dark).toMatchObject({ codeThemeId: "github", contrast: 82, fonts });
  expect(useAppStore.getState().appearanceColors.dark).toEqual({ accent: "#ddee66", surface: "#101010", ink: "#eeeeee" });
  expect(readAppearanceVariant("dark").theme?.fonts).toEqual(fonts);
  expect(document.documentElement.style.getPropertyValue("--font-ui")).toContain('"Saved Special Font"');
  expect(warning).toHaveBeenCalledWith(expect.stringContaining("SavedSpecial-Medium"), unavailable);
  expect(add).not.toHaveBeenCalled();
});

it("resets only Source Advanced preferences while preserving visual UI family, palette, prose and authored work", async () => {
  const state = useAppStore.getState();
  await state.patchAppearanceTheme("dark", { contrast: 88, opaqueWindows: true, fonts: { ui: "Visual Family", code: "Custom Mono", content: "Reading Family",
    uiFace: { family: "Visual Family", fullName: "Visual Family Heavy", postscriptName: "Visual-Heavy" } } });
  state.setAppearanceColor("dark", "accent", "#2468ac"); state.setReducedMotionPreference("on"); state.setPointerCursors(true); state.setDiffMarkerStyle("symbols");
  useAppStore.setState({ codeFontBase: 14, codeTextScale: 1.25, textScale: 1.25 });
  state.setWorkbenchPreferences({ proseSize: 21 });
  const before = useAppStore.getState();
  state.resetAdvancedAppearance(false);
  expect(useAppStore.getState()).toMatchObject({ codeFontBase: 12, codeTextScale: 1, textScale: 1, reducedMotionPreference: "system", pointerCursors: false, diffMarkerStyle: "color" });
  expect(useAppStore.getState().appearanceThemes.dark).toMatchObject({ contrast: 60, opaqueWindows: true, fonts: { ui: "Visual Family", content: null, code: null } });
  expect(useAppStore.getState().appearanceThemes.dark?.fonts?.uiFace).toBeUndefined();
  expect(useAppStore.getState().appearanceColors.dark?.accent).toBe("#2468ac");
  expect(useAppStore.getState().workbenchPreferences.proseSize).toBe(21);
  for (const key of ["draft", "attachments", "editorTabs", "workspaceComposerDrafts"] as const) expect(useAppStore.getState()[key]).toBe(before[key]);
  state.resetAdvancedAppearance(true); expect(useAppStore.getState().appearanceThemes.dark?.opaqueWindows).toBe(false);
});
