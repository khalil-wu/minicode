/* @vitest-environment jsdom */
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { installAppThemeTokens } from "../../test/app-theme-tokens";
import { emptyAppearanceColors } from "../lib/appearance-colors";
import { emptyAppearanceThemes } from "../lib/appearance-themes";
import { APPEARANCE_STORAGE_KEYS as keys, readAppearanceStorage, readAppearanceVariant, writeAppearanceVariant } from "../lib/appearance-storage";
import { useAppStore } from ".";

const highlighting = vi.hoisted(() => ({ prepare: vi.fn(async () => {}) }));
vi.mock("../lib/code-highlighting", () => ({ prepareCodeHighlighting: highlighting.prepare }));
const colors = { accent: "#223344", surface: "#fafafa", ink: "#111111" };
const theme = { codeThemeId: "github" as const, contrast: 75, opaqueWindows: true,
  semanticColors: { diffAdded: "#22aa33", diffRemoved: "#aa3322", skill: "#aabbcc" } };
let removeTokens: () => void;
beforeEach(() => {
  localStorage.clear(); removeTokens = installAppThemeTokens();
  highlighting.prepare.mockReset().mockResolvedValue(undefined);
  useAppStore.setState({ appearanceColors: emptyAppearanceColors(), appearanceThemes: emptyAppearanceThemes() });
  useAppStore.getState().setThemeMode("light");
});
afterEach(() => { localStorage.clear(); removeTokens(); });
const notify = (key: string | null, storageArea = localStorage) =>
  window.dispatchEvent(new StorageEvent("storage", { key, storageArea }));

it("adopts another window's mode and paired theme without touching drafts, attachments or editor models", () => {
  const before = useAppStore.getState();
  writeAppearanceVariant("dark", { colors, theme });
  localStorage.setItem(keys.mode, "dark");
  notify(keys.dark);
  const state = useAppStore.getState();
  expect(state).toMatchObject({ themeMode: "dark", resolvedTheme: "dark", appearanceColors: { dark: colors },
    appearanceThemes: { dark: theme } });
  expect(document.documentElement.style.getPropertyValue("--surface-base")).toBe(colors.surface);
  expect(document.documentElement.dataset.codeTheme).toBe("github");
  for (const key of ["draft", "attachments", "workspaceComposerDrafts", "editorTabs", "conversationWorkbenchStates"] as const) {
    expect(state[key]).toBe(before[key]);
  }
});

it("patches the latest persisted colors and retains the other window's matching theme and opposite variant", () => {
  // The peer write is persisted before its storage event reaches this window.
  writeAppearanceVariant("light", { colors, theme });
  writeAppearanceVariant("dark", { colors: { ...colors, surface: "#101010" }, theme: { ...theme, codeThemeId: "dracula" } });
  useAppStore.getState().setAppearanceColor("light", "accent", "#445566");
  expect(readAppearanceVariant("light")).toEqual({ colors: { ...colors, accent: "#445566" }, theme });
  expect(useAppStore.getState().appearanceThemes.dark?.codeThemeId).toBe("dracula");
  expect(useAppStore.getState().appearanceColors).toEqual(readAppearanceStorage().appearanceColors);
});

it("does not let an old asynchronous theme import overwrite a newer peer edit", async () => {
  let finish!: () => void;
  highlighting.prepare.mockImplementationOnce(() => new Promise<void>(resolve => { finish = resolve; }));
  const importing = useAppStore.getState().importAppearanceTheme("light", { codeThemeId: "dracula",
    theme: { ...colors, ...theme, accentSource: "custom" } });
  await vi.waitFor(() => expect(highlighting.prepare).toHaveBeenCalled());
  writeAppearanceVariant("light", { colors, theme });
  notify(keys.light);
  finish(); await importing;
  expect(readAppearanceVariant("light")).toEqual({ colors, theme });
  expect(useAppStore.getState().appearanceThemes.light).toEqual(theme);
});

it("keeps a later local color edit when an earlier import finishes", async () => {
  let finish!: () => void;
  highlighting.prepare.mockImplementationOnce(() => new Promise<void>(resolve => { finish = resolve; }));
  const importing = useAppStore.getState().importAppearanceTheme("light", { codeThemeId: "dracula",
    theme: { ...colors, ...theme, accentSource: "custom" } });
  await vi.waitFor(() => expect(highlighting.prepare).toHaveBeenCalled());
  useAppStore.getState().setAppearanceColor("light", "accent", "#998877");
  finish(); await importing;
  expect(readAppearanceVariant("light").colors?.accent).toBe("#998877");
  expect(readAppearanceVariant("light").theme).toBeNull();
});

it("uses the latest storage snapshot on clear and ignores other storage areas and unrelated keys", () => {
  writeAppearanceVariant("light", { colors, theme }); notify(keys.light);
  localStorage.removeItem(keys.light);
  notify(keys.light, sessionStorage);
  notify("unrelated");
  expect(useAppStore.getState().appearanceColors.light).toEqual(colors);
  notify(null);
  expect(useAppStore.getState().appearanceColors.light).toBeNull();
  expect(document.documentElement.dataset.codeTheme).toBeUndefined();
});
