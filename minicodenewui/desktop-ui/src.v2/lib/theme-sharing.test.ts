import { describe, expect, it } from "vitest";
import { CODE_THEME_CATALOG, codeThemesForVariant } from "./code-themes";
import { canImportThemeString, exportThemeString, parseThemeString, type CodeThemeAppearance } from "./theme-sharing";

const snapshot: CodeThemeAppearance = {
  codeThemeId: codeThemesForVariant("light")[0].id,
  theme: {
    accent: "#236abc", accentSource: "custom", contrast: 45, ink: "#123456", opaqueWindows: false,
    semanticColors: { diffAdded: "#00a240", diffRemoved: "#ba2623", skill: "#924ff7" }, surface: "#f3e8d7",
  },
};

describe("Codex v1 theme share boundary", () => {
  it("uses plain JSON with Source fields and discards imported typography without changing source data", () => {
    const text = exportThemeString(snapshot, "light");
    expect(text.startsWith('codex-theme-v1:{"codeThemeId":')).toBe(true);
    const payload = JSON.parse(text.slice(15));
    expect(payload).toEqual({ ...snapshot, theme: { ...snapshot.theme, fonts: { code: null, ui: null } }, variant: "light" });
    payload.theme.fonts = {
      code: "Uploaded Code Font", content: "Uploaded Content Font", ui: "Uploaded UI Font",
      uiFace: { family: "Uploaded UI Font", fullName: "Uploaded UI Bold", postscriptName: "UploadedUI-Bold" },
    };
    payload.theme.accent = "#ABCDEF";
    payload.theme.sansFontSize = 24;
    payload.theme.fontWeight = 900;
    const imported = parseThemeString(`codex-theme-v1:${JSON.stringify(payload)}`, "light");
    expect(imported).toEqual({ ...snapshot, theme: { ...snapshot.theme, accent: "#abcdef" } });
    expect(payload.theme.fonts.ui).toBe("Uploaded UI Font");
    expect(snapshot.theme.accent).toBe("#236abc");
  });

  it("accepts Source's URI-encoded form and surrounding whitespace", () => {
    const text = exportThemeString(snapshot, "light");
    const encoded = ` \ncodex-theme-v1:${encodeURIComponent(text.slice(15))}\n `;
    expect(parseThemeString(encoded, "light")).toEqual(snapshot);
  });

  it("rejects malformed, incomplete, cross-variant and unavailable registrations", () => {
    const text = exportThemeString(snapshot, "light");
    const payload = JSON.parse(text.slice(15));
    const cases = ["", "codex-theme-v2:{}", "codex-theme-v1:%", "codex-theme-v1:{", text.replace('"contrast":45', '"contrast":45.5'),
      text.replace('"#236abc"', '"#abc"'), text.replace('"opaqueWindows":false,', ""),
      text.replace('"fonts":{"code":null,"ui":null}', '"fonts":{}'),
      `codex-theme-v1:${JSON.stringify({ ...payload, codeThemeId: "uninstalled-theme" })}`,
      `codex-theme-v1:${JSON.stringify({ ...payload, variant: "dark" })}`];
    for (const invalid of cases) {
      expect(canImportThemeString(invalid, "light")).toBe(false);
      expect(() => parseThemeString(invalid, "light")).toThrow();
    }
    const darkOnly = CODE_THEME_CATALOG.find(theme => !theme.variants.includes("light"));
    expect(darkOnly).toBeDefined();
    expect(canImportThemeString(`codex-theme-v1:${JSON.stringify({ ...payload, codeThemeId: darkOnly!.id })}`, "light")).toBe(false);
  });
});
