import { describe, expect, it } from "vitest";
import { appearanceColorTokens, colorInputDraft, completeColorInput, parseAppearanceColors } from "./appearance-colors";
import { defaultThemePreferences } from "./appearance-themes";

describe("Codex chrome color contracts", () => {
  it("keeps partial hex drafts separate from the six-digit committed value", () => {
    expect(colorInputDraft(" #ab!2c ")).toBe("#AB2C");
    expect(completeColorInput("#AB2C")).toBeNull();
    expect(completeColorInput(colorInputDraft("abcDEF-extra"))).toBe("#abcdef");
    expect(parseAppearanceColors(null)).toEqual({ light: null, dark: null });
    expect(parseAppearanceColors('{"light":{"surface":"#FFEEDD","accent":"#336699","ink":"#112233"},"dark":null}')).toEqual({ light: { surface: "#ffeedd", accent: "#336699", ink: "#112233" }, dark: null });
    expect(() => parseAppearanceColors('{"light":{"surface":"red"},"dark":{}}')).toThrow();
  });

  it("matches Source Pba/Bba/Rba RGB results at dark default contrast 60", () => {
    const tokens = appearanceColorTokens({ accent: "#2468ac", surface: "#101010", ink: "#d0d0d0" }, "dark");
    expect(tokens["--surface-base"]).toBe("#101010");
    expect(tokens["--workbench-sidebar"]).toBe("rgb(13, 13, 13)");
    expect(tokens["--workbench-tool"]).toBe("rgb(33, 33, 33)");
    expect(tokens["--editor-background"]).toBe("rgb(29, 29, 29)");
    expect(tokens["--editor-selection"]).toBe("rgb(11, 30, 50)");
    expect(tokens["--text-secondary"]).toBe("rgba(208, 208, 208, 0.71)");
    expect(tokens["--text-on-accent"]).toBe("rgb(255, 255, 255)");
    expect(tokens["--text-info"]).toBe("rgb(121, 163, 204)");
  });

  it("matches Source Pba/Bba/Iba RGB results at light default contrast 45", () => {
    const tokens = appearanceColorTokens({ accent: "#2468ac", surface: "#e1e2e3", ink: "#202122" }, "light");
    expect(tokens["--surface-base"]).toBe("#e1e2e3");
    expect(tokens["--workbench-sidebar"]).toBe("rgb(217, 218, 219)");
    expect(tokens["--editor-background"]).toBe("rgb(229, 229, 230)");
    expect(tokens["--text-primary"]).toBe("#202122");
    expect(tokens["--accent-primary"]).toBe("#2468ac");
    expect(tokens["--text-info"]).toBe("#2468ac");
    expect(Object.keys(tokens).some(key => /font|--fs-|--fw-|line-height|syntax/.test(key))).toBe(false);
  });

  it.each([
    { variant: "dark", contrast: 85, info: "rgb(157, 187, 218)", sidebar: "rgb(13, 13, 13)", panel: "rgb(31, 31, 31)", control: "rgb(44, 44, 44)", secondary: "rgba(208, 208, 208, 0.818)" },
    { variant: "light", contrast: 30, info: "#2468ac", sidebar: "rgb(20, 20, 20)", panel: "rgb(59, 59, 59)", control: "rgb(39, 39, 39)", secondary: "rgba(208, 208, 208, 0.662)" },
  ] as const)("uses Source ess/tss/nss and custom textAccent at $variant contrast $contrast", ({ variant, contrast, info, sidebar, panel, control, secondary }) => {
    const tokens = appearanceColorTokens({ accent: "#2468ac", surface: "#101010", ink: "#d0d0d0" }, variant,
      { codeThemeId: "codex", ...defaultThemePreferences(variant), contrast });
    expect(tokens["--text-info"]).toBe(info);
    expect(tokens["--surface-sidebar"]).toBe(sidebar);
    expect(tokens["--surface-panel"]).toBe(panel);
    expect(tokens["--workbench-tool"]).toBe(control);
    expect(tokens["--text-secondary"]).toBe(secondary);
  });
});
