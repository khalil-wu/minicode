import type { ThemeRegistrationRaw } from "@shikijs/core";
import { codeThemeChromeSeed, type CodeThemeChromeSeed } from "./code-themes-seed";

export type CodeThemeVariant = "light" | "dark";

// Source dba/HJ registrations. Desktop presets retain their own provenance.
const registrations = {
  "absolutely": { label: "Absolutely", variants: { dark: () => import("./codex-themes/absolutely-dark.json"), light: () => import("./codex-themes/absolutely-light.json") } },
  "codex": { label: "ChatGPT", variants: { dark: () => import("./codex-themes/codex-dark.json"), light: () => import("./codex-themes/codex-light.json") } },
  "linear": { label: "Linear", variants: { dark: () => import("./codex-themes/linear-dark.json"), light: () => import("./codex-themes/linear-light.json") } },
  "lobster": { label: "Lobster", variants: { dark: () => import("./codex-themes/lobster-dark.json") } },
  "matrix": { label: "Matrix", variants: { dark: () => import("./codex-themes/matrix-dark.json") } },
  "notion": { label: "Notion", variants: { dark: () => import("./codex-themes/notion-dark.json"), light: () => import("./codex-themes/notion-light.json") } },
  "og": { label: "OG", variants: { dark: () => import("./codex-themes/og-dark.json") } },
  "oscurange": { label: "Oscurange", variants: { dark: () => import("./codex-themes/oscurange-dark.json") } },
  "proof": { label: "Proof", variants: { light: () => import("./codex-themes/proof-light.json") } },
  "raycast": { label: "Raycast", variants: { dark: () => import("./codex-themes/raycast-dark.json"), light: () => import("./codex-themes/raycast-light.json") } },
  "sentry": { label: "Sentry", variants: { dark: () => import("./codex-themes/sentry-dark.json") } },
  "temple": { label: "Temple", variants: { dark: () => import("./codex-themes/temple-dark.json") } },
  "vercel": { label: "Vercel", variants: { dark: () => import("./codex-themes/vercel-dark.json"), light: () => import("./codex-themes/vercel-light.json") } },
  "xcode": { label: "Xcode", variants: { dark: () => import("./codex-themes/xcode-dark.json"), light: () => import("./codex-themes/xcode-light.json") } },
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
  source: { package: string; version: string; license?: string };
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
      source: ["absolutely", "codex", "linear", "lobster", "matrix", "notion", "og", "oscurange", "proof", "raycast", "sentry", "temple", "vercel", "xcode"].includes(id)
        ? { package: "Codex desktop reference", version: "app-initial-b8e1c8014bb4" }
        : { package: "@shikijs/themes", version: "3.23.0", license: "MIT" },
    })));
  }
  return loadedThemes.get(key)!;
}

export const loadCodeThemeSeed = async (id: CodeThemeId, variant: CodeThemeVariant): Promise<CodeThemeChromeSeed> =>
  (await loadCodeTheme(id, variant)).chromeSeed;

export type { CodeThemeChromeSeed } from "./code-themes-seed";
