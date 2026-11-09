import { z } from "zod";
export type { SystemFontFamily, SystemFontFace } from "../desktop/runtime";

export const themeFontFaceSchema = z.object({ family: z.string(), fullName: z.string(), postscriptName: z.string() });
export const themeFontsSchema = z.object({
  ui: z.string().nullable(), content: z.string().nullable().optional(), code: z.string().nullable(),
  uiFace: themeFontFaceSchema.optional(), contentFace: themeFontFaceSchema.optional(), codeFace: themeFontFaceSchema.optional(),
});
export type ThemeFontFace = z.infer<typeof themeFontFaceSchema>;
export type ThemeFonts = z.infer<typeof themeFontsSchema>;
export const THEME_FONTS_READY = "minicode:theme-fonts-ready";

export function firstFontFamily(value: string | null | undefined): string | undefined {
  if (!value) return undefined;
  let quote = "", first = "";
  for (let index = 0; index < value.length; index++) {
    const character = value[index];
    if (character === "\\") { first += value[++index] || ""; continue; }
    if (quote) { if (character === quote) quote = ""; else first += character; }
    else if (character === '"' || character === "'") quote = character;
    else if (character === ",") break;
    else first += character;
  }
  return first.trim() || undefined;
}

export const cssFontFamily = (family: string) => `"${family.replaceAll("\\", "\\\\").replaceAll('"', '\\"')}"`;
export const themeFontAlias = (face: ThemeFontFace) => `MiniCode selected ${face.postscriptName}`;
export const faceMatchesFamily = (family: string | null | undefined, face: ThemeFontFace | undefined) =>
  Boolean(face && firstFontFamily(family)?.toLocaleLowerCase() === face.family.toLocaleLowerCase());
export function fontStack(family: string, face?: ThemeFontFace): string {
  const authored = family.includes(",") || /^["']|^var\(/.test(family) || /^(system-ui|(?:ui-)?(?:sans-serif|serif|monospace)|cursive|fantasy|emoji|math|fangsong)$/i.test(family)
    ? family : cssFontFamily(family);
  return faceMatchesFamily(family, face) ? `${cssFontFamily(themeFontAlias(face!))}, ${authored}` : authored;
}

const loading = new WeakMap<FontFaceSet, Map<string, Promise<void>>>();
export async function prepareThemeFonts(fonts?: ThemeFonts): Promise<void> {
  if (!fonts || typeof FontFace === "undefined" || !document.fonts) return;
  const fontSet = document.fonts;
  let faces = loading.get(fontSet);
  if (!faces) { faces = new Map(); loading.set(fontSet, faces); }
  await Promise.all((["ui", "content", "code"] as const).map(key => {
    const face = fonts[`${key}Face`];
    if (!faceMatchesFamily(fonts[key], face)) return;
    const alias = themeFontAlias(face!);
    let current = faces.get(alias);
    if (!current) {
      current = (async () => {
        const loaded = await new FontFace(alias, `local(${cssFontFamily(face!.postscriptName)}), local(${cssFontFamily(face!.fullName)})`).load();
        fontSet.add(loaded);
        window.dispatchEvent(new Event(THEME_FONTS_READY));
      })().catch(error => {
        faces!.delete(alias);
        console.warn(`Local font face is unavailable: ${face!.fullName} (${face!.postscriptName})`, error);
      });
      faces.set(alias, current);
    }
    return current;
  }));
}

/** Canvas consumers need a resolved CSS stack; they cannot interpret var(). */
export function codeFontFamily(): string {
  const style = getComputedStyle(document.documentElement);
  const editor = style.getPropertyValue("--editor-font-family").trim();
  return !editor || editor === "var(--font-mono)" ? style.getPropertyValue("--font-mono").trim() : editor;
}
