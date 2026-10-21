import { describe, expect, it } from "vitest";
import { createHash } from "node:crypto";
import reference from "./code-themes-reference-seeds.json";
import { CODE_THEME_CATALOG, codeThemesForVariant, isCodeThemeIdForVariant, loadCodeTheme } from "./code-themes";

const variants = CODE_THEME_CATALOG.flatMap(theme => theme.variants.map(variant => ({ id: theme.id, variant })));
const ordered = (value: unknown): unknown => Array.isArray(value) ? value.map(ordered)
  : value !== null && typeof value === "object"
    ? Object.fromEntries(Object.entries(value).sort(([left], [right]) => left < right ? -1 : left > right ? 1 : 0)
      .map(([key, item]) => [key, ordered(item)]))
    : value;

describe("actual Source code theme catalog", () => {
  it("offers only complete upstream presets and filters variants before selection or import", () => {
    expect(CODE_THEME_CATALOG).toHaveLength(29);
    expect(variants).toHaveLength(44);
    expect(reference.cases.map(item => `${item.id}:${item.variant}`).sort())
      .toEqual(variants.map(item => `${item.id}:${item.variant}`).sort());
    expect(codeThemesForVariant("light").map(theme => theme.id)).not.toContain("nord");
    expect(isCodeThemeIdForVariant("nord", "light")).toBe(false);
    expect(isCodeThemeIdForVariant("nord", "dark")).toBe(true);
    expect(isCodeThemeIdForVariant("vscode-plus", "light")).toBe(true);
    expect(isCodeThemeIdForVariant("vscode-plus", "dark")).toBe(true);
    expect(isCodeThemeIdForVariant("codex", "dark")).toBe(true);
    expect(isCodeThemeIdForVariant("pretend-theme", "light")).toBe(false);
    expect(isCodeThemeIdForVariant({ id: "nord" }, "dark")).toBe(false);
  });

  it.each(variants)("loads complete $id/$variant data and matches independently extracted Source seed", async ({ id, variant }) => {
    const loaded = await loadCodeTheme(id, variant);
    const expected = reference.cases.find(item => item.id === id && item.variant === variant)!;
    expect(loaded.registrationName).toBe(expected.registrationName);
    expect(loaded.theme.type).toBe(variant);
    expect(createHash("sha256").update(JSON.stringify(ordered(loaded.theme))).digest("hex")).toBe(expected.completeThemeSha256);
    expect((loaded.theme.tokenColors ?? loaded.theme.settings).length).toBe(expected.tokenRuleCount);
    expect(Object.keys(loaded.theme.semanticTokenColors ?? {})).toHaveLength(expected.semanticTokenCount);
    expect(loaded.chromeSeed).toEqual(expected.seed);
    expect(loaded.chromeSeed).not.toHaveProperty("fonts");
    expect(loaded.source).toEqual(expected.source);
  });
});
