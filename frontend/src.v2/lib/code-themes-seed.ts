import type { ThemeRegistrationRaw } from "@shikijs/core";
import type { CodeThemeVariant } from "./code-themes";

export type CodeThemeChromeSeed = {
  contrast?: number;
  opaqueWindows?: boolean;
  accent: string;
  accentSource: "custom";
  ink: string;
  surface: string;
  semanticColors: { diffAdded: string; diffRemoved: string; skill: string };
};

type RGB = { red: number; green: number; blue: number; alpha: number };
type HueRange = { min: number; max: number };

// Source app-initial Mya/Pya/Iya/Rya and WJ/Bya: theme colors are parsed at
// this data boundary, then ranked by the actual palette rather than by name.
const defaults = {
  dark: { surface: "#181818", ink: "#ffffff", accent: "#339cff",
    semanticColors: { diffAdded: "#40c977", diffRemoved: "#fa423e", skill: "#ad7bf9" } },
  light: { surface: "#ffffff", ink: "#1a1c1f", accent: "#339cff",
    semanticColors: { diffAdded: "#00a240", diffRemoved: "#ba2623", skill: "#924ff7" } },
};

const surfaceKeys = ["editor.background", "sideBar.background", "editorGroupHeader.tabsBackground", "panel.background", "activityBar.background"];
const inkKeys = ["editor.foreground", "sideBarTitle.foreground", "sideBar.foreground", "foreground"];
const accentKeys = ["activityBarBadge.background", "textLink.foreground", "editorCursor.foreground", "focusBorder", "button.background", "activityBar.activeBorder"];
const addedKeys = ["gitDecoration.addedResourceForeground", "gitDecoration.untrackedResourceForeground", "terminal.ansiGreen", "terminal.ansiBrightGreen"];
const removedKeys = ["gitDecoration.deletedResourceForeground", "terminal.ansiRed", "terminal.ansiBrightRed"];
const skillKeys = ["charts.purple", "terminal.ansiMagenta", "terminal.ansiBrightMagenta"];

function rgb(value: string | undefined): RGB | undefined {
  if (value == null) return;
  const hex = value.trim();
  if (!/^#[0-9a-fA-F]{6}([0-9a-fA-F]{2})?$/.test(hex)) return;
  return { red: parseInt(hex.slice(1, 3), 16), green: parseInt(hex.slice(3, 5), 16),
    blue: parseInt(hex.slice(5, 7), 16), alpha: hex.length === 9 ? parseInt(hex.slice(7, 9), 16) / 255 : 1 };
}
const range = ({ red, green, blue }: RGB) => Math.max(red, green, blue) - Math.min(red, green, blue);
const hexColor = ({ red, green, blue }: RGB) => `#${[red, green, blue].map(channel => channel.toString(16).padStart(2, "0")).join("")}`;
function candidate(value: string | undefined, minimumAlpha = .98, minimumRange = 0): string | undefined {
  const color = rgb(value);
  if (color && color.alpha >= minimumAlpha && range(color) >= minimumRange) return hexColor(color);
}
const distance = (a: RGB, b: RGB) => Math.sqrt((a.red - b.red) ** 2 + (a.green - b.green) ** 2 + (a.blue - b.blue) ** 2);
const same = (a: string, b: string) => {
  const first = rgb(a), second = rgb(b);
  return first && second ? distance(first, second) < 42 : false;
};
function score(color: string, surface: string, ink: string): number {
  const value = rgb(color), background = rgb(surface), foreground = rgb(ink);
  return value && background && foreground ? range(value) + distance(value, background) / 4 + distance(value, foreground) / 4 : 0;
}
function firstColor(colors: Record<string, string> | undefined, keys: string[]): string | undefined {
  if (colors) for (const key of keys) {
    const color = candidate(colors[key]);
    if (color) return color;
  }
}
const rules = (theme: ThemeRegistrationRaw) => [...theme.tokenColors ?? [], ...theme.settings ?? []];
function accent(theme: ThemeRegistrationRaw, surface: string, ink: string): string | undefined {
  for (const key of accentKeys) {
    const color = candidate(theme.colors?.[key], .45, 24);
    if (color && !same(color, surface) && !same(color, ink)) return color;
  }
  let best: string | undefined, bestScore = -1;
  for (const rule of rules(theme)) {
    const color = candidate(rule.settings?.foreground, .45, 24);
    if (!color || same(color, surface) || same(color, ink)) continue;
    const rank = score(color, surface, ink);
    if (rank > bestScore) { best = color; bestScore = rank; }
  }
  return best;
}
function hue({ red, green, blue }: RGB): number | null {
  const r = red / 255, g = green / 255, b = blue / 255;
  const max = Math.max(r, g, b), delta = max - Math.min(r, g, b);
  if (delta === 0) return null;
  const value = max === r ? (g - b) / delta % 6 * 60 : max === g ? ((b - r) / delta + 2) * 60 : ((r - g) / delta + 4) * 60;
  return (value + 360) % 360;
}
const inHueRange = (value: number, bounds: HueRange) => bounds.min <= bounds.max
  ? value >= bounds.min && value <= bounds.max : value >= bounds.min || value <= bounds.max;
const hueDistance = (a: number, b: number) => Math.min(Math.abs(a - b), 360 - Math.abs(a - b));
function semanticCandidate(theme: ThemeRegistrationRaw, surface: string, ink: string, bounds: HueRange, preferredHue: number): string | undefined {
  const palette = new Set([...Object.values(theme.colors ?? {}), ...rules(theme).map(rule => rule.settings?.foreground)]
    .map(value => candidate(value, .45, 24)).filter((value): value is string => value !== undefined));
  let best: string | undefined, bestScore = -1;
  for (const color of palette) {
    if (same(color, surface) || same(color, ink)) continue;
    const value = hue(rgb(color)!);
    if (value === null || !inHueRange(value, bounds)) continue;
    const rank = score(color, surface, ink) - hueDistance(value, preferredHue) * 2;
    if (rank > bestScore) { best = color; bestScore = rank; }
  }
  return best;
}

export function codeThemeChromeSeed(theme: ThemeRegistrationRaw, variant: CodeThemeVariant): CodeThemeChromeSeed {
  const original = defaults[variant];
  const chrome = (theme as ThemeRegistrationRaw & { chromeTheme?: Partial<CodeThemeChromeSeed> }).chromeTheme;
  const surface = chrome?.surface ?? firstColor(theme.colors, surfaceKeys) ?? original.surface;
  const ink = chrome?.ink ?? firstColor(theme.colors, inkKeys) ?? original.ink;
  const primary = chrome?.accent ?? accent(theme, surface, ink) ?? original.accent;
  return { ...(chrome?.contrast === undefined ? {} : { contrast: chrome.contrast }),
    ...(chrome?.opaqueWindows === undefined ? {} : { opaqueWindows: chrome.opaqueWindows }),
    accent: primary, accentSource: "custom", surface, ink, semanticColors: {
    diffAdded: chrome?.semanticColors?.diffAdded ?? firstColor(theme.colors, addedKeys) ?? semanticCandidate(theme, surface, ink, { min: 80, max: 170 }, 125) ?? original.semanticColors.diffAdded,
    diffRemoved: chrome?.semanticColors?.diffRemoved ?? firstColor(theme.colors, removedKeys) ?? semanticCandidate(theme, surface, ink, { min: 345, max: 15 }, 0) ?? original.semanticColors.diffRemoved,
    skill: chrome?.semanticColors?.skill ?? firstColor(theme.colors, skillKeys) ?? semanticCandidate(theme, surface, ink, { min: 210, max: 320 }, 265)
      ?? (!same(primary, surface) && !same(primary, ink) ? primary : original.semanticColors.skill),
  } };
}
