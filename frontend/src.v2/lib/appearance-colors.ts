import { formatHex } from "culori";
import { z } from "zod";
import type { AppearanceThemeSelection, AppearanceThemes } from "./appearance-themes";
import { fontStack, prepareThemeFonts } from "./theme-fonts";

export type AppearanceVariant = "light" | "dark";
export type AppearanceColorRole = "accent" | "surface" | "ink";
export type AppearanceColorValues = Record<AppearanceColorRole, string>;
export type AppearanceColors = Record<AppearanceVariant, AppearanceColorValues | null>;

const hexColor = z.string().regex(/^#[0-9a-fA-F]{6}$/).transform(value => value.toLowerCase());
export const appearanceColorValuesSchema = z.object({ accent: hexColor, surface: hexColor, ink: hexColor });
const colorValues = appearanceColorValuesSchema;
const savedColors = z.object({ light: colorValues.nullable(), dark: colorValues.nullable() });
export const emptyAppearanceColors = (): AppearanceColors => ({ light: null, dark: null });
// Inactive variants cannot read the currently painted variant's computed style.
// These are the authored base values in styles/tokens.css, not a theme preset.
export const defaultAppearanceColors: Record<AppearanceVariant, AppearanceColorValues> = {
  light: { accent: formatHex("oklch(0.38 0 0)")!, surface: "#ffffff", ink: "#1a1c1f" },
  dark: { accent: formatHex("oklch(0.82 0 0)")!, surface: formatHex("oklch(0.205 0.002 250)")!, ink: formatHex("oklch(0.930 0.002 250)")! },
};
export const parseAppearanceColors = (raw: string | null): AppearanceColors =>
  raw === null ? emptyAppearanceColors() : savedColors.parse(JSON.parse(raw));

// Codex settings Ta/ka/Aa: keep incomplete input local, commit six hex digits.
export const colorInputDraft = (value: string) => `#${value.toUpperCase().replace(/[^0-9A-F]/g, "").slice(0, 6)}`;
export const completeColorInput = (value: string) => /^#[0-9A-F]{6}$/.test(value) ? value.toLowerCase() : null;

type RGB = [number, number, number];
const black: RGB = [0, 0, 0];
const white: RGB = [255, 255, 255];
const rgb = (color: string): RGB => [1, 3, 5].map(index => parseInt(color.slice(index, index + 2), 16)) as RGB;
const mix = (from: RGB, to: RGB, amount: number): RGB =>
  from.map((channel, index) => Math.round(channel + (to[index] - channel) * Math.min(1, Math.max(0, amount)))) as RGB;
const solid = (color: RGB) => `rgb(${color.join(", ")})`;
const alpha = (color: RGB, opacity: number) => `rgba(${color.join(", ")}, ${Math.min(1, Math.max(0, opacity))})`;
const luminance = (color: RGB) => color.map(channel => {
  const value = channel / 255;
  return value <= .04045 ? value / 12.92 : ((value + .055) / 1.055) ** 2.4;
}).reduce((sum, value, index) => sum + value * [.2126, .7152, .0722][index], 0);
const onAccent = (color: RGB) => {
  const [red, green, blue] = color;
  if (blue > red && blue > green) {
    const range = blue - Math.min(red, green);
    const hue = ((red - green) / range + 4) * 60;
    if (range / blue >= .8 && hue >= 205 && hue <= 212
      || range / blue >= .6 && hue >= 218 && hue <= 233 && luminance(color) <= .21) return solid(white);
  }
  return solid(luminance(color) > .179 ? black : white);
};

/** Source Pba/Bba/Iba/Rba/Vba/Fba at the reference app-initial theme cluster.
 * Theme selection/import also supplies contrast and semantic colors. Typography
 * and TextMate syntax stay owned by their existing consumers.
 */
export function appearanceColorTokens(colors: AppearanceColorValues, variant: AppearanceVariant, theme?: AppearanceThemeSelection | null): Record<string, string> {
  const light = variant === "light";
  const contrast = theme ? theme.contrast / 100 : light ? .45 : .6;
  const surface = rgb(colors.surface), ink = rgb(colors.ink), accent = rgb(colors.accent);
  const under = solid(mix(surface, light ? ink : black, light ? .04 : .16));
  const control = solid(mix(surface, light ? white : ink, light ? .09 + contrast * .04 : .06 + contrast * .05));
  const elevated = solid(mix(surface, light ? white : ink, light ? .16 + contrast * .12 : .08 + contrast * .08));
  const secondary = solid(mix(surface, light ? white : ink, light ? .08 + contrast * .08 : .04 + contrast * .05));
  const hover = alpha(ink, light ? .08 + contrast * .04 : .05 + contrast * .03);
  const active = alpha(ink, light ? .16 + contrast * .08 : .07 + contrast * .05);
  const accentText = light ? colors.accent : solid(mix(accent, white, .3 + contrast * .15));
  const accentSoft = solid(mix(light ? surface : black, accent, light ? .11 + contrast * .04 : .2 + contrast * .08));
  const accentSelected = solid(mix(light ? surface : black, accent, light ? .13 + contrast * .05 : .22 + contrast * .12));
  const border = alpha(ink, .06 + contrast * .04);
  const borderHeavy = alpha(ink, light ? .09 + contrast * .06 : .12 + contrast * .06);
  const borderLight = alpha(ink, light ? .04 + contrast * .02 : .03 + contrast * .02);
  const focus = light ? colors.accent : alpha(mix(accent, white, .3 + contrast * .15), .7 + contrast * .1);
  const muted = alpha(ink, light ? .45 + contrast * .1 : .42 + contrast * .13);
  const semantic = theme?.semanticColors;
  return {
    "--desktop-shell-background": under,
    "--desktop-frame-border": border,
    "--surface-sunken": under,
    "--surface-base": colors.surface,
    "--surface-sidebar": under,
    "--surface-page": colors.surface,
    "--surface-soft": secondary,
    "--surface-panel": solid(mix(surface, light ? white : ink, light ? .18 + contrast * .008 : .03 + contrast * .03)),
    "--surface-raised": elevated,
    "--surface-hover": hover,
    "--surface-active": active,
    "--workbench-sidebar": under,
    "--workbench-tool": control,
    "--workbench-hover": hover,
    "--workbench-active": active,
    "--text-primary": colors.ink,
    "--text-secondary": alpha(ink, .65 + contrast * .1),
    "--text-muted": muted,
    "--text-disabled": alpha(ink, light ? .45 + contrast * .1 : .42 + contrast * .13),
    "--text-on-accent": onAccent(accent),
    "--border-subtle": border,
    "--border-soft": borderLight,
    "--border-strong": borderHeavy,
    "--border-focus": focus,
    "--accent-primary": colors.accent,
    "--accent-secondary": accentText,
    "--accent-strong": colors.accent,
    "--accent-hover": colors.accent,
    "--accent-active": colors.accent,
    "--accent-soft": accentSoft,
    "--state-info": accentText,
    "--state-info-soft": accentSoft,
    "--command-accent": colors.accent,
    "--command-accent-strong": accentText,
    "--command-border": focus,
    "--editor-background": solid(mix(surface, light ? white : ink, light ? .12 : .07)),
    "--editor-foreground": colors.ink,
    "--editor-line-number": muted,
    "--editor-line-number-active": colors.ink,
    "--editor-line-highlight": control,
    "--editor-selection": accentSelected,
    "--editor-selection-inactive": accentSoft,
    "--editor-indent-guide": border,
    "--editor-indent-guide-active": borderHeavy,
    "--editor-widget": elevated,
    "--editor-ghost-foreground": muted,
    "--editor-border": borderHeavy,
    "--editor-widget-selection": hover,
    "--editor-find-match": accentSelected,
    "--editor-find-range": accentSoft,
    ...(semantic ? {
      "--diff-added-foreground": semantic.diffAdded,
      "--diff-removed-foreground": semantic.diffRemoved,
      "--diff-added-background": alpha(rgb(semantic.diffAdded), light ? .15 : .23),
      "--diff-removed-background": alpha(rgb(semantic.diffRemoved), light ? .15 : .23),
      "--diff-added-word-background": alpha(rgb(semantic.diffAdded), light ? .15 : .23),
      "--diff-removed-word-background": alpha(rgb(semantic.diffRemoved), light ? .15 : .23),
      "--diff-add-text": semantic.diffAdded,
      "--diff-del-text": semantic.diffRemoved,
      "--diff-add-bg": alpha(rgb(semantic.diffAdded), light ? .15 : .23),
      "--diff-del-bg": alpha(rgb(semantic.diffRemoved), light ? .15 : .23),
      "--app-color-accent-purple": semantic.skill,
    } : {}),
  };
}

const appliedRoots = new WeakSet<HTMLElement>();
const ownedTokens = Object.keys(appearanceColorTokens({ accent: "#000000", surface: "#000000", ink: "#ffffff" }, "dark", {
  codeThemeId: "vscode-plus", contrast: 60, opaqueWindows: false,
  semanticColors: { diffAdded: "#000000", diffRemoved: "#000000", skill: "#000000" },
}));
const baseTokens: Record<AppearanceColorRole, string> = { accent: "--accent-primary", surface: "--surface-base", ink: "--text-primary" };

export const effectiveAppearanceColors = (): AppearanceColorValues => {
  const style = getComputedStyle(document.documentElement);
  return Object.fromEntries(Object.entries(baseTokens).map(([role, token]) => [role, formatHex(style.getPropertyValue(token).trim())!])) as AppearanceColorValues;
};

export function applyAppearanceColors(variant: AppearanceVariant, saved: AppearanceColors, themes?: AppearanceThemes) {
  const root = document.documentElement;
  const fonts = themes?.[variant]?.fonts;
  for (const [property, key, base] of [
    ["--font-ui", "ui", "var(--font-app)"], ["--font-prose", "content", "var(--font-app)"],
    ["--font-mono", "code", '"SFMono-Regular", Menlo, Consolas, monospace'],
  ] as const) {
    const family = fonts?.[key];
    if (family) root.style.setProperty(property, `${fontStack(family, fonts?.[`${key}Face`])}, ${base}`);
    else root.style.removeProperty(property!);
  }
  void prepareThemeFonts(fonts);
  if (!fonts?.content && fonts?.ui) root.style.setProperty("--font-prose", "var(--font-ui)");
  if (themes?.[variant]) root.dataset.codeTheme = themes[variant].codeThemeId;
  else delete root.dataset.codeTheme;
  if (appliedRoots.has(root)) {
    for (const token of ownedTokens) root.style.removeProperty(token);
    appliedRoots.delete(root);
  }
  const colors = saved[variant];
  if (colors === null) return;
  for (const [token, value] of Object.entries(appearanceColorTokens(colors, variant, themes?.[variant]))) root.style.setProperty(token, value);
  appliedRoots.add(root);
}
