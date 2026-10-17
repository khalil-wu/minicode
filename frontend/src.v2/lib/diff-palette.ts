import { converter, formatHex, formatHex8, interpolate } from "culori";
import type { CSSProperties } from "react";
import type { AppearanceColorValues, AppearanceVariant } from "./appearance-colors";
import type { CodeThemeId } from "./code-themes";

type SemanticDiffColors = { diffAdded: string; diffRemoved: string };
type DiffCodeColors = { fg: string; bg: string };
export type DiffPalette = {
  background: string;
  foreground: string;
  addition: string;
  deletion: string;
  additionLine: string;
  deletionLine: string;
  additionGutter: string;
  deletionGutter: string;
  additionWord: string;
  deletionWord: string;
};
type DiffPaletteStyle = CSSProperties & { [key: `--${string}`]: string | number | undefined };
const rgb = converter("rgb");
const mixLab = (background: string, foreground: string, backgroundShare: number) =>
  formatHex(interpolate([background, foreground], "lab")(1 - backgroundShare))!;
const withAlpha = (color: string, alpha: number) => formatHex8({ ...rgb(color)!, alpha })!;

/** Actual Source single-theme FileDiff caller, including the app unsafe CSS.
 * Chrome surface paints the pre; host-computed line colors retain raw theme bg.
 * The caller does not enable hover, and line selection remains a separate UI.
 */
export function createDiffPalette(variant: AppearanceVariant, code: DiffCodeColors, chrome: AppearanceColorValues, semantic: SemanticDiffColors): DiffPalette {
  const light = variant === "light";
  const lineShare = light ? .88 : .8, gutterShare = light ? .91 : .85;
  return {
    background: chrome.surface, foreground: code.fg,
    addition: semantic.diffAdded, deletion: semantic.diffRemoved,
    additionLine: mixLab(code.bg, semantic.diffAdded, lineShare),
    deletionLine: mixLab(code.bg, semantic.diffRemoved, lineShare),
    additionGutter: mixLab(light ? "#ffffff" : "#000000", semantic.diffAdded, gutterShare),
    deletionGutter: mixLab(light ? "#ffffff" : "#000000", semantic.diffRemoved, gutterShare),
    additionWord: withAlpha(semantic.diffAdded, light ? .15 : .2),
    deletionWord: withAlpha(semantic.diffRemoved, light ? .15 : .2),
  };
}

export async function loadDiffCodeColors(id: CodeThemeId, variant: AppearanceVariant): Promise<DiffCodeColors> {
  const { prepareCodeHighlighting } = await import("./code-highlighting");
  const { highlighter, loaded } = await prepareCodeHighlighting(id, variant);
  const { fg, bg } = highlighter.getTheme(loaded.registrationName);
  return { fg, bg };
}

/** Scoped to a Diff view; the app's editor/proposal color tokens keep ownership. */
export function diffPaletteStyle(palette: DiffPalette | null, variant: AppearanceVariant): DiffPaletteStyle {
  const light = variant === "light";
  return palette ? {
    "--diff-view-background": palette.background,
    "--diff-view-foreground": palette.foreground,
    "--diff-added-foreground": palette.addition,
    "--diff-removed-foreground": palette.deletion,
    "--diff-added-background": palette.additionLine,
    "--diff-removed-background": palette.deletionLine,
    "--diff-added-gutter-background": palette.additionGutter,
    "--diff-removed-gutter-background": palette.deletionGutter,
    "--diff-added-word-background": palette.additionWord,
    "--diff-removed-word-background": palette.deletionWord,
  } : {
    "--diff-view-background": "var(--surface-base)",
    "--diff-view-foreground": "var(--editor-foreground)",
    "--diff-added-background": `color-mix(in lab, var(--editor-background) ${light ? 88 : 80}%, var(--diff-added-foreground))`,
    "--diff-removed-background": `color-mix(in lab, var(--editor-background) ${light ? 88 : 80}%, var(--diff-removed-foreground))`,
    "--diff-added-gutter-background": `color-mix(in lab, ${light ? "#ffffff 91%" : "#000000 85%"}, var(--diff-added-foreground))`,
    "--diff-removed-gutter-background": `color-mix(in lab, ${light ? "#ffffff 91%" : "#000000 85%"}, var(--diff-removed-foreground))`,
    "--diff-added-word-background": `rgb(from var(--diff-added-foreground) r g b / ${light ? .15 : .2})`,
    "--diff-removed-word-background": `rgb(from var(--diff-removed-foreground) r g b / ${light ? .15 : .2})`,
  };
}

export const monacoDiffPaletteColors = (palette: DiffPalette) => ({
  "diffEditor.insertedLineBackground": palette.additionLine,
  "diffEditor.removedLineBackground": palette.deletionLine,
  "diffEditor.insertedTextBackground": palette.additionWord,
  "diffEditor.removedTextBackground": palette.deletionWord,
  "diffEditorGutter.insertedLineBackground": palette.additionGutter,
  "diffEditorGutter.removedLineBackground": palette.deletionGutter,
});
