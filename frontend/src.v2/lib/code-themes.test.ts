import { describe, expect, it } from "vitest";
import reference from "./code-themes-reference-seeds.json";
import { CODE_THEME_CATALOG, codeThemesForVariant, isCodeThemeIdForVariant, loadCodeTheme } from "./code-themes";

const variants = CODE_THEME_CATALOG.flatMap(theme => theme.variants.map(variant => ({ id: theme.id, variant })));

describe("actual licensed code theme catalog", () => {
  it("offers only complete upstream presets and filters variants before selection or import", () => {
    expect(CODE_THEME_CATALOG).toHaveLength(15);
    expect(variants).toHaveLength(23);
    expect(codeThemesForVariant("light").map(theme => theme.id)).not.toContain("nord");
    expect(isCodeThemeIdForVariant("nord", "light")).toBe(false);
    expect(isCodeThemeIdForVariant("nord", "dark")).toBe(true);
    expect(isCodeThemeIdForVariant("vscode-plus", "light")).toBe(true);
    expect(isCodeThemeIdForVariant("vscode-plus", "dark")).toBe(true);
    expect(isCodeThemeIdForVariant("codex", "dark")).toBe(false);
    expect(isCodeThemeIdForVariant("pretend-theme", "light")).toBe(false);
    expect(isCodeThemeIdForVariant({ id: "nord" }, "dark")).toBe(false);
  });

  it.each(variants)("loads complete $id/$variant data and matches independently extracted Source seed", async ({ id, variant }) => {
    const loaded = await loadCodeTheme(id, variant);
    const expected = reference.cases.find(item => item.registrationName === loaded.registrationName && item.variant === variant)!;
    expect(loaded.theme.type).toBe(variant);
    expect((loaded.theme.tokenColors ?? loaded.theme.settings).length).toBe(expected.tokenRuleCount);
    expect(Object.keys(loaded.theme.semanticTokenColors ?? {})).toHaveLength(expected.semanticTokenCount);
    expect(loaded.chromeSeed).toEqual(expected.seed);
    expect(loaded.chromeSeed).not.toHaveProperty("fonts");
    expect(loaded.chromeSeed).not.toHaveProperty("contrast");
    expect(loaded.chromeSeed).not.toHaveProperty("opaqueWindows");
    expect(loaded.source).toEqual({ package: "@shikijs/themes", version: "3.23.0", license: "MIT" });
  });
});
