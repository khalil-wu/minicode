/* @vitest-environment jsdom */
import { afterEach, beforeEach, expect, it } from "vitest";
import { installAppThemeTokens } from "../../test/app-theme-tokens";
import { applyAppearanceColors, emptyAppearanceColors } from "../lib/appearance-colors";
import { emptyAppearanceThemes } from "../lib/appearance-themes";
import { loadCodeTheme } from "../lib/code-themes";
import { readAppearanceStorage } from "../lib/appearance-storage";
import { exportThemeString, parseThemeString } from "../lib/theme-sharing";
import { LS, initialAppearanceColors, initialAppearanceThemes, applyTheme } from "./shared-helpers";
import { useAppStore } from ".";

let removeTokens: () => void;
beforeEach(() => {
  localStorage.clear(); removeTokens = installAppThemeTokens();
  useAppStore.setState({ appearanceColors: emptyAppearanceColors(), appearanceThemes: emptyAppearanceThemes() });
  useAppStore.getState().setThemeMode("dark");
});
afterEach(() => {
  useAppStore.setState({ appearanceColors: emptyAppearanceColors(), appearanceThemes: emptyAppearanceThemes() });
  applyAppearanceColors("dark", emptyAppearanceColors());
  localStorage.clear(); removeTokens();
});

it("commits the real theme seed together, retains both variants and leaves drafts and typography untouched", async () => {
  const state = useAppStore.getState();
  const draft = state.draft, tabs = state.editorTabs, hidden = state.workspaceComposerDrafts, attachments = state.attachments;
  document.documentElement.style.setProperty("--app-text-scale", "1.125");
  document.documentElement.style.setProperty("--editor-font-family", "Authored font");
  await state.setCodeThemeId("dark", "github");
  const seed = (await loadCodeTheme("github", "dark")).chromeSeed;
  expect(initialAppearanceColors().dark).toEqual({ accent: seed.accent, surface: seed.surface, ink: seed.ink });
  expect(initialAppearanceThemes().dark).toMatchObject({ codeThemeId: "github", contrast: 60, semanticColors: seed.semanticColors });
  expect(document.documentElement.dataset.codeTheme).toBe("github");
  expect(document.documentElement.style.getPropertyValue("--diff-added-foreground")).toBe(seed.semanticColors.diffAdded);
  await state.setCodeThemeId("light", "one");
  expect(document.documentElement.dataset.codeTheme).toBe("github");
  state.setThemeMode("light");
  expect(document.documentElement.dataset.codeTheme).toBe("one");
  state.setThemeMode("dark");
  expect(document.documentElement.style.getPropertyValue("--surface-base")).toBe(seed.surface);
  expect(useAppStore.getState()).toMatchObject({ draft, editorTabs: tabs, workspaceComposerDrafts: hidden, attachments });
  expect(useAppStore.getState().editorTabs).toBe(tabs);
  expect(document.documentElement.style.getPropertyValue("--app-text-scale")).toBe("1.125");
  expect(document.documentElement.style.getPropertyValue("--editor-font-family")).toBe("Authored font");
});

it("imports contrast and semantic colors, restores them on startup and never writes font preferences", async () => {
  const { chromeSeed } = await loadCodeTheme("github", "dark");
  const snapshot = { codeThemeId: "github" as const, theme: { ...chromeSeed, contrast: 85, opaqueWindows: true } };
  const share = JSON.parse(exportThemeString(snapshot, "dark").slice(15));
  share.theme.fonts = { code: "Other code font", ui: "Other UI font" };
  await useAppStore.getState().importAppearanceTheme("dark", parseThemeString(`codex-theme-v1:${JSON.stringify(share)}`, "dark"));
  expect(initialAppearanceThemes().dark).toMatchObject({ contrast: 85, opaqueWindows: true });
  expect(localStorage.getItem(LS.textScale)).toBeNull();
  expect(localStorage.getItem(LS.codeTextScale)).toBeNull();
  expect(localStorage.getItem(LS.appearanceDark)).not.toContain("Other");
  const selectedBackground = document.documentElement.style.getPropertyValue("--surface-base");
  applyAppearanceColors("dark", emptyAppearanceColors());
  expect(document.documentElement.dataset.codeTheme).toBeUndefined();
  applyTheme("dark", readAppearanceStorage());
  expect(document.documentElement.dataset.codeTheme).toBe("github");
  expect(document.documentElement.style.getPropertyValue("--surface-base")).toBe(selectedBackground);
});
