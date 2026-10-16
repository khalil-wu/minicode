import { z } from "zod";
import type { AppearanceVariant } from "./appearance-colors";
import { CODE_THEME_CATALOG, isCodeThemeIdForVariant, type CodeThemeId } from "./code-themes";

const sharePrefix = "codex-theme-v1:";
class ThemeShareError extends Error {}
const hexColor = z.string().regex(/^#[0-9a-fA-F]{6}$/).transform(value => value.toLowerCase());
const codeThemeId = z.custom<CodeThemeId>(value => typeof value === "string" && CODE_THEME_CATALOG.some(theme => theme.id === value));

// Codex app-shared kN/$Tt and app-initial d0s/l0s. Only user share text is
// parsed here; the stored/application snapshot deliberately excludes fonts.
export const chromeThemeColorsSchema = z.object({
  accent: hexColor,
  accentSource: z.enum(["chatgpt", "custom"]).optional(),
  contrast: z.number().int().min(0).max(100),
  ink: hexColor,
  opaqueWindows: z.boolean(),
  semanticColors: z.object({ diffAdded: hexColor, diffRemoved: hexColor, skill: hexColor }),
  surface: hexColor,
});
export type ChromeThemeColors = z.infer<typeof chromeThemeColorsSchema>;

export const codeThemeAppearanceSchema = z.object({ codeThemeId, theme: chromeThemeColorsSchema });
export type CodeThemeAppearance = z.infer<typeof codeThemeAppearanceSchema>;

const fontFace = z.object({ family: z.string(), fullName: z.string(), postscriptName: z.string() });
const sharedThemeSchema = z.object({
  codeThemeId,
  theme: chromeThemeColorsSchema.extend({
    fonts: z.object({
      code: z.string().nullable(),
      codeFace: fontFace.optional(),
      content: z.string().nullable().optional(),
      contentFace: fontFace.optional(),
      ui: z.string().nullable(),
      uiFace: fontFace.optional(),
    }),
  }),
  variant: z.enum(["light", "dark"]),
});

/** Source a0s → t0s: raw or URI-encoded JSON, then variant/registration. */
export function parseThemeString(value: string, variant: AppearanceVariant): CodeThemeAppearance {
  const text = value.trim();
  if (!text.startsWith(sharePrefix)) throw new ThemeShareError("Theme share string mismatch");
  const body = text.slice(sharePrefix.length);
  const shared = sharedThemeSchema.parse(JSON.parse(body.startsWith("{") ? body : decodeURIComponent(body)));
  if (shared.variant !== variant) throw new ThemeShareError("Theme variant mismatch");
  if (!isCodeThemeIdForVariant(shared.codeThemeId, variant)) throw new ThemeShareError("Theme code theme mismatch");
  const { fonts: _fonts, ...theme } = shared.theme;
  return { codeThemeId: shared.codeThemeId, theme };
}

/** Source i0s format; typography stays private to the current MiniCode user. */
export function exportThemeString(snapshot: CodeThemeAppearance, variant: AppearanceVariant): string {
  return `${sharePrefix}${JSON.stringify({ codeThemeId: snapshot.codeThemeId, theme: { ...snapshot.theme, fonts: { code: null, ui: null } }, variant })}`;
}

/** Invalid pasted text is normal input state; it disables the submit action. */
export function canImportThemeString(value: string, variant: AppearanceVariant): boolean {
  try {
    parseThemeString(value, variant);
    return true;
  } catch (error) {
    if (error instanceof SyntaxError || error instanceof URIError || error instanceof z.ZodError || error instanceof ThemeShareError) return false;
    throw error;
  }
}
