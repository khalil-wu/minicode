import { z } from "zod";
import { appearanceColorValuesSchema, parseAppearanceColors, type AppearanceVariant } from "./appearance-colors";
import { appearanceThemeSelectionSchema, parseAppearanceThemes } from "./appearance-themes";

export const APPEARANCE_STORAGE_KEYS = {
  mode: "minicode.theme",
  light: "minicode.appearance.light",
  dark: "minicode.appearance.dark",
  legacyColors: "minicode.appearance.colors",
  legacyThemes: "minicode.appearance.themes",
} as const;
const snapshotSchema = z.object({
  colors: appearanceColorValuesSchema.nullable(),
  theme: appearanceThemeSelectionSchema.nullable(),
});
export type SavedAppearanceVariant = z.infer<typeof snapshotSchema>;
const variants = ["light", "dark"] as const;

/** Preserve authored pre-variant settings before retiring the two old maps. */
function migrateAppearanceStorage() {
  const oldColors = localStorage.getItem(APPEARANCE_STORAGE_KEYS.legacyColors);
  const oldThemes = localStorage.getItem(APPEARANCE_STORAGE_KEYS.legacyThemes);
  if (oldColors === null && oldThemes === null) return;
  const colors = parseAppearanceColors(oldColors);
  const themes = parseAppearanceThemes(oldThemes);
  for (const variant of variants) {
    if (localStorage.getItem(APPEARANCE_STORAGE_KEYS[variant]) === null) {
      localStorage.setItem(APPEARANCE_STORAGE_KEYS[variant], JSON.stringify({ colors: colors[variant], theme: themes[variant] }));
    }
  }
  localStorage.removeItem(APPEARANCE_STORAGE_KEYS.legacyColors);
  localStorage.removeItem(APPEARANCE_STORAGE_KEYS.legacyThemes);
}

function readVariant(variant: AppearanceVariant): SavedAppearanceVariant {
  const raw = localStorage.getItem(APPEARANCE_STORAGE_KEYS[variant]);
  return raw === null ? { colors: null, theme: null } : snapshotSchema.parse(JSON.parse(raw));
}

export function readAppearanceStorage() {
  migrateAppearanceStorage();
  const light = readVariant("light"), dark = readVariant("dark");
  return {
    appearanceColors: { light: light.colors, dark: dark.colors },
    appearanceThemes: { light: light.theme, dark: dark.theme },
  };
}

export function readAppearanceVariant(variant: AppearanceVariant) {
  migrateAppearanceStorage();
  return readVariant(variant);
}

/** One atomic KV write owns one variant; another window cannot erase its peer. */
export function writeAppearanceVariant(variant: AppearanceVariant, snapshot: SavedAppearanceVariant) {
  migrateAppearanceStorage();
  localStorage.setItem(APPEARANCE_STORAGE_KEYS[variant], JSON.stringify(snapshot));
  return readAppearanceStorage();
}

export const isAppearanceStorageKey = (key: string | null) =>
  key === null || Object.values<string>(APPEARANCE_STORAGE_KEYS).includes(key);
