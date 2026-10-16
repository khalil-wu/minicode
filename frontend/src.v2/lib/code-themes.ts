import type { ThemeRegistrationRaw } from "@shikijs/core";
import { codeThemeChromeSeed, type CodeThemeChromeSeed } from "./code-themes-seed";

export type CodeThemeVariant = "light" | "dark";

// Source dba/HJ registrations, using the same MIT-licensed Shiki 3.23.0 data.
// The 14 Codex-specific presets remain audit-only until redistribution terms
// are established. Every exposed entry below loads its complete actual theme.
const registrations = {
  ayu: { label: "Ayu", variants: { dark: () => import("@shikijs/themes/ayu-dark") } },
  catppuccin: { label: "Catppuccin", variants: {
    dark: () => import("@shikijs/themes/catppuccin-mocha"), light: () => import("@shikijs/themes/catppuccin-latte"),
  } },
  dracula: { label: "Dracula", variants: { dark: () => import("@shikijs/themes/dracula") } },
  everforest: { label: "Everforest", variants: {
    dark: () => import("@shikijs/themes/everforest-dark"), light: () => import("@shikijs/themes/everforest-light"),
  } },
  github: { label: "GitHub", variants: {
    dark: () => import("@shikijs/themes/github-dark-default"), light: () => import("@shikijs/themes/github-light-default"),
  } },
  gruvbox: { label: "Gruvbox", variants: {
    dark: () => import("@shikijs/themes/gruvbox-dark-medium"), light: () => import("@shikijs/themes/gruvbox-light-medium"),
  } },
  material: { label: "Material", variants: { dark: () => import("@shikijs/themes/material-theme-darker") } },
  monokai: { label: "Monokai", variants: { dark: () => import("@shikijs/themes/monokai") } },
  "night-owl": { label: "Night Owl", variants: { dark: () => import("@shikijs/themes/night-owl") } },
  nord: { label: "Nord", variants: { dark: () => import("@shikijs/themes/nord") } },
  one: { label: "One", variants: {
    dark: () => import("@shikijs/themes/one-dark-pro"), light: () => import("@shikijs/themes/one-light"),
  } },
  "rose-pine": { label: "Rose Pine", variants: {
    dark: () => import("@shikijs/themes/rose-pine-moon"), light: () => import("@shikijs/themes/rose-pine-dawn"),
  } },
  solarized: { label: "Solarized", variants: {
    dark: () => import("@shikijs/themes/solarized-dark"), light: () => import("@shikijs/themes/solarized-light"),
  } },
  "tokyo-night": { label: "Tokyo Night", variants: { dark: () => import("@shikijs/themes/tokyo-night") } },
  "vscode-plus": { label: "VS Code Plus", variants: {
    dark: () => import("@shikijs/themes/dark-plus"), light: () => import("@shikijs/themes/light-plus"),
  } },
} as const;

export type CodeThemeId = keyof typeof registrations;
export type CodeThemeDescriptor = {
  readonly id: CodeThemeId;
  readonly label: string;
  readonly variants: readonly CodeThemeVariant[];
};

export const CODE_THEME_CATALOG: readonly CodeThemeDescriptor[] = Object.entries(registrations).map(([id, entry]) => ({
  id: id as CodeThemeId, label: entry.label, variants: Object.keys(entry.variants) as CodeThemeVariant[],
}));

const labelOrder = new Intl.Collator(undefined, { sensitivity: "base" });
export const codeThemesForVariant = (variant: CodeThemeVariant): readonly CodeThemeDescriptor[] =>
  CODE_THEME_CATALOG.filter(theme => theme.variants.includes(variant)).sort((a, b) => labelOrder.compare(a.label, b.label));

export const isCodeThemeIdForVariant = (value: unknown, variant: CodeThemeVariant): value is CodeThemeId =>
  CODE_THEME_CATALOG.some(theme => theme.id === value && theme.variants.includes(variant));

export type LoadedCodeTheme = {
  id: CodeThemeId;
  variant: CodeThemeVariant;
  registrationName: string;
  theme: ThemeRegistrationRaw;
  chromeSeed: CodeThemeChromeSeed;
  source: { package: "@shikijs/themes"; version: "3.23.0"; license: "MIT" };
};

type ThemeLoader = () => Promise<{ default: ThemeRegistrationRaw }>;
const loadedThemes = new Map<string, Promise<LoadedCodeTheme>>();

export function loadCodeTheme(id: CodeThemeId, variant: CodeThemeVariant): Promise<LoadedCodeTheme> {
  const key = `${id}:${variant}`;
  if (!loadedThemes.has(key)) {
    const load = (registrations[id].variants as Partial<Record<CodeThemeVariant, ThemeLoader>>)[variant]!;
    loadedThemes.set(key, load().then(({ default: theme }): LoadedCodeTheme => ({
      id, variant, registrationName: theme.name!, theme,
      chromeSeed: codeThemeChromeSeed(theme, variant),
      source: { package: "@shikijs/themes", version: "3.23.0", license: "MIT" },
    })));
  }
  return loadedThemes.get(key)!;
}

export const loadCodeThemeSeed = async (id: CodeThemeId, variant: CodeThemeVariant): Promise<CodeThemeChromeSeed> =>
  (await loadCodeTheme(id, variant)).chromeSeed;

export type { CodeThemeChromeSeed } from "./code-themes-seed";
