/* @vitest-environment jsdom */
import { afterEach, expect, it, vi } from "vitest";
import { APPEARANCE_STORAGE_KEYS as keys, readAppearanceStorage, writeAppearanceVariant } from "./appearance-storage";

const light = { colors: { accent: "#123456", surface: "#ffffff", ink: "#101010" }, theme: null };
const dark = { colors: { accent: "#abcdef", surface: "#101010", ink: "#eeeeee" }, theme: {
  codeThemeId: "github" as const, contrast: 85, opaqueWindows: true,
  semanticColors: { diffAdded: "#00ff00", diffRemoved: "#ff0000", skill: "#aabbcc" },
} };
afterEach(() => { vi.restoreAllMocks(); localStorage.clear(); });

function saveLegacy() {
  localStorage.setItem(keys.legacyColors, JSON.stringify({ light: light.colors, dark: dark.colors }));
  localStorage.setItem(keys.legacyThemes, JSON.stringify({ light: light.theme, dark: dark.theme }));
}

it("migrates both paired variants without changing authored colors, theme or unrelated data", () => {
  saveLegacy();
  localStorage.setItem("minicode.theme", "system");
  localStorage.setItem("minicode.composer.authored", "draft with attachments");
  expect(readAppearanceStorage()).toEqual({ appearanceColors: { light: light.colors, dark: dark.colors },
    appearanceThemes: { light: light.theme, dark: dark.theme } });
  expect(JSON.parse(localStorage.getItem(keys.light)!)).toEqual(light);
  expect(JSON.parse(localStorage.getItem(keys.dark)!)).toEqual(dark);
  expect(localStorage.getItem(keys.legacyColors)).toBeNull();
  expect(localStorage.getItem(keys.legacyThemes)).toBeNull();
  expect(localStorage.getItem("minicode.theme")).toBe("system");
  expect(localStorage.getItem("minicode.composer.authored")).toBe("draft with attachments");
});

it("retains an already migrated variant and removes legacy maps only after both saves succeed", () => {
  saveLegacy();
  const authored = { ...light, colors: { ...light.colors, accent: "#998877" } };
  localStorage.setItem(keys.light, JSON.stringify(authored));
  const save = Storage.prototype.setItem;
  vi.spyOn(Storage.prototype, "setItem").mockImplementation(function(this: Storage, key, value) {
    if (key === keys.dark) throw new DOMException("Storage full", "QuotaExceededError");
    save.call(this, key, value);
  });
  expect(() => readAppearanceStorage()).toThrow("Storage full");
  expect(localStorage.getItem(keys.legacyColors)).not.toBeNull();
  expect(localStorage.getItem(keys.legacyThemes)).not.toBeNull();
  vi.restoreAllMocks();
  expect(readAppearanceStorage().appearanceColors.light).toEqual(authored.colors);
  expect(JSON.parse(localStorage.getItem(keys.dark)!)).toEqual(dark);
});

it("writing one window's variant never writes the other variant or separates code theme from colors", () => {
  writeAppearanceVariant("light", light);
  const save = vi.spyOn(Storage.prototype, "setItem");
  const current = writeAppearanceVariant("dark", dark);
  expect(save.mock.calls.map(([key]) => key)).toEqual([keys.dark]);
  expect(current).toEqual({ appearanceColors: { light: light.colors, dark: dark.colors },
    appearanceThemes: { light: light.theme, dark: dark.theme } });
});
