import { z } from "zod";
import type { AppearanceVariant } from "./appearance-colors";
import { CODE_THEME_CATALOG, type CodeThemeId } from "./code-themes";

const hexColor = z.string().regex(/^#[0-9a-fA-F]{6}$/);
export const semanticThemeColorsSchema = z.object({
  diffAdded: hexColor,
  diffRemoved: hexColor,
  skill: hexColor,
});
const selectionSchema = z.object({
  codeThemeId: z.custom<CodeThemeId>(value => CODE_THEME_CATALOG.some(theme => theme.id === value)),
  contrast: z.number().int().min(0).max(100),
  opaqueWindows: z.boolean(),
  semanticColors: semanticThemeColorsSchema,
});
export type AppearanceThemeSelection = z.infer<typeof selectionSchema>;
export type AppearanceThemes = Record<AppearanceVariant, AppearanceThemeSelection | null>;
export const emptyAppearanceThemes = (): AppearanceThemes => ({ light: null, dark: null });
const savedThemes = z.object({ light: selectionSchema.nullable(), dark: selectionSchema.nullable() });
export const parseAppearanceThemes = (raw: string | null): AppearanceThemes =>
  raw === null ? emptyAppearanceThemes() : savedThemes.parse(JSON.parse(raw));

// Reference VJ defaults. Colors are captured separately so enabling the new
// picker does not replace an existing MiniCode palette on startup.
export const defaultThemePreferences = (variant: AppearanceVariant) => ({
  contrast: variant === "light" ? 45 : 60,
  opaqueWindows: false,
  semanticColors: variant === "light"
    ? { diffAdded: "#00a240", diffRemoved: "#ba2623", skill: "#924ff7" }
    : { diffAdded: "#40c977", diffRemoved: "#fa423e", skill: "#ad7bf9" },
});
