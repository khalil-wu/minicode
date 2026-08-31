import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import * as fc from "fast-check";
import { oklch, parse, wcagContrast, formatHex, type Color } from "culori";

const tokensCss = readFileSync(
  resolve(process.cwd(), "src.v2/styles/tokens.css"),
  "utf8",
);
const tailwindConfig = readFileSync(resolve(process.cwd(), "tailwind.config.js"), "utf8");
const designTokensCss = readFileSync(
  resolve(process.cwd(), "src.v2/styles/design-tokens.css"),
  "utf8",
);
interface TokenMap {
  [name: string]: string;
}

const extractScope = (source: string, scope: string): TokenMap => {
  const escaped = scope.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const re = new RegExp(`${escaped}\\s*\\{([^}]*)\\}`, "s");
  const m = source.match(re);
  if (!m) throw new Error(`scope ${scope} not found in tokens.css`);
  const body = m[1];
  const tokens: TokenMap = {};
  const decl = /(--[a-z0-9-]+)\s*:\s*([^;]+);/gi;
  let match: RegExpExecArray | null;
  while ((match = decl.exec(body)) !== null) {
    tokens[match[1].trim()] = match[2].trim();
  }
  return tokens;
};

const dark = extractScope(tokensCss, ":root");
const light = extractScope(tokensCss, '[data-theme="light"]');

const resolveValue = (theme: TokenMap, raw: string): string => {
  let value = raw;
  let depth = 0;
  while (value.includes("var(") && depth < 10) {
    value = value.replace(/var\(\s*(--[a-z0-9-]+)(?:\s*,\s*([^)]*))?\s*\)/gi, (_m, name, fallback) =>
      theme[name] ?? fallback ?? "transparent",
    );
    depth += 1;
  }
  return value;
};

const tryParseColor = (raw: string): Color | undefined => {
  try {
    const c = parse(raw);
    return c;
  } catch {
    return undefined;
  }
};

describe("Phase A — design token property tests", () => {
  it("P1.1 token coverage: dark ≥ 32 distinct tokens across 5 prefixes", () => {
    const prefixes = ["--surface-", "--text-", "--border-", "--accent-", "--state-"];
    const counts: Record<string, number> = {};
    let total = 0;
    for (const p of prefixes) {
      counts[p] = Object.keys(dark).filter((n) => n.startsWith(p)).length;
      total += counts[p];
    }
    expect(counts["--surface-"]).toBeGreaterThanOrEqual(8);
    expect(counts["--text-"]).toBeGreaterThanOrEqual(4);
    expect(counts["--border-"]).toBeGreaterThanOrEqual(4);
    expect(counts["--accent-"]).toBeGreaterThanOrEqual(7);
    expect(counts["--state-"]).toBeGreaterThanOrEqual(8);
    expect(total).toBeGreaterThanOrEqual(32);
  });

  it("P1.7 light parity: every theme-bound color token in dark exists in light", () => {
    const textColorNames = new Set([
      "--text-primary",
      "--text-secondary",
      "--text-muted",
      "--text-disabled",
    ]);
    const isThemeBound = (name: string) =>
      name.startsWith("--surface-") ||
      name.startsWith("--border-") ||
      name.startsWith("--accent-") ||
      name.startsWith("--state-") ||
      textColorNames.has(name);
    const darkColorNames = Object.keys(dark).filter(isThemeBound);
    const missingInLight = darkColorNames.filter((n) => !(n in light));
    expect(missingInLight).toEqual([]);
  });

  it("P1.2 surface hierarchy: dark ramps upward while light keeps white canvases", () => {
    const darkOrder = [
      "--surface-base",
      "--surface-sidebar",
      "--surface-page",
      "--surface-soft",
      "--surface-panel",
      "--surface-raised",
      "--surface-hover",
      "--surface-active",
    ];

    const luminance = (theme: TokenMap, name: string) =>
      oklch(parse(theme[name]) as Color)?.l ?? Number.NaN;
    const darkLs = darkOrder.map((name) => luminance(dark, name));
    for (let i = 1; i < darkLs.length; i++) {
      const delta = darkLs[i] - darkLs[i - 1];
      expect(delta).toBeGreaterThanOrEqual(0.005);
      expect(delta).toBeLessThanOrEqual(0.08);
    }

    // Content canvases stay pure white in light theme — text sits on paper.
    for (const name of ["--surface-base", "--surface-page", "--surface-raised"]) {
      expect(luminance(light, name), name).toBeGreaterThanOrEqual(0.99);
    }

    // Chrome (sidebar) is deliberately NOT a canvas: it reads as a neutral grey
    // frame the white content floats above (macOS/Linear sidebar model), so
    // hierarchy is carried by planes rather than by 1px hairlines. It must stay
    // light, but must be distinguishably darker than the content it frames.
    const sidebarL = luminance(light, "--surface-sidebar");
    expect(sidebarL).toBeGreaterThanOrEqual(0.93);
    expect(sidebarL).toBeLessThan(luminance(light, "--surface-base"));

    const lightInteractionOrder = ["--surface-soft", "--surface-panel", "--surface-hover", "--surface-active"];
    const lightLs = lightInteractionOrder.map((name) => luminance(light, name));
    for (let i = 1; i < lightLs.length; i++) {
      const delta = lightLs[i - 1] - lightLs[i];
      expect(delta).toBeGreaterThanOrEqual(0.004);
      expect(delta).toBeLessThanOrEqual(0.04);
    }
  });

  it("P1.3 WCAG: text-primary ≥ 4.5:1 vs every surface (both themes)", () => {
    for (const theme of [dark, light] as const) {
      const fg = formatHex(parse(theme["--text-primary"]) as Color);
      const surfaces = Object.keys(theme).filter((n) => n.startsWith("--surface-"));
      for (const s of surfaces) {
        const bg = formatHex(parse(theme[s]) as Color);
        if (!fg || !bg) continue;
        const ratio = wcagContrast(fg, bg);
        expect(ratio).toBeGreaterThanOrEqual(4.5);
      }
    }
  });

  it("P1.5 accent-primary uses a cool blue hue with visible chroma", () => {
    for (const theme of [dark, light] as const) {
      const c = oklch(parse(theme["--accent-primary"]) as Color);
      expect(c).toBeDefined();
      if (!c) continue;
      const h = c.h ?? 0;
      const ch = c.c ?? 0;
      expect(h).toBeGreaterThanOrEqual(230);
      expect(h).toBeLessThanOrEqual(270);
      expect(ch).toBeGreaterThanOrEqual(0.03);
      expect(ch).toBeLessThanOrEqual(0.20);
    }
  });

  it("P2.6 font-size ratio: adjacent --text-* steps r ∈ [1.05, 1.45]", () => {
    const sizeNames = ["--text-xs", "--text-sm", "--text-base", "--text-md", "--text-lg", "--text-xl", "--text-2xl", "--text-3xl"];
    const px = sizeNames.map((n) => {
      const v = dark[n];
      const m = v.match(/(\d+(?:\.\d+)?)\s*px/);
      return m ? parseFloat(m[1]) : Number.NaN;
    });
    for (let i = 1; i < px.length; i++) {
      const r = px[i] / px[i - 1];
      expect(r).toBeGreaterThanOrEqual(1.05);
      expect(r).toBeLessThanOrEqual(1.45);
    }
  });

  it("P1.6 no raw hex literals leak into surface/text/accent definitions (dark scope)", () => {
    const hexLiteral = /#[0-9a-fA-F]{3,8}\b/;
    for (const [name, raw] of Object.entries(dark)) {
      if (
        name.startsWith("--surface-") ||
        name.startsWith("--text-") ||
        name.startsWith("--accent-")
      ) {
        const resolved = resolveValue(dark, raw);
        expect(resolved, `${name} = ${raw}`).not.toMatch(hexLiteral);
      }
    }
  });

  it("PBT: every surface token in both themes is parsable as a color", () => {
    fc.assert(
      fc.property(
        fc.constantFrom(...Object.keys(dark).filter((n) => n.startsWith("--surface-"))),
        fc.constantFrom("dark", "light"),
        (name, theme) => {
          const map = theme === "dark" ? dark : light;
          const raw = resolveValue(map, map[name] ?? "");
          if (!raw) return true;
          const c = tryParseColor(raw);
          return c !== undefined;
        },
      ),
      { numRuns: 50 },
    );
  });

  it("keeps Tailwind and polish layers from redefining canonical color tokens", () => {
    expect(tailwindConfig).not.toMatch(/#[0-9a-fA-F]{3,8}\b/);

    const canonicalDeclaration =
      /(?:^|\n)\s*--(?:accent|surface|text|border|state)-(?:primary|secondary|strong|hover|active|soft|sunken|base|sidebar|page|panel|raised|muted|subtle|success|danger|warning|info)\s*:/;
    expect(designTokensCss).not.toMatch(canonicalDeclaration);

    const bannedWarmAccent = /#(?:A89278|C4A882|8C7A64|B9986F)\b/i;
    expect(tailwindConfig).not.toMatch(bannedWarmAccent);
    expect(designTokensCss).not.toMatch(bannedWarmAccent);
  });
});
