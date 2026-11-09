/* @vitest-environment node */
import { readFileSync, existsSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { expect, it } from "vitest";

const tokens = readFileSync(new URL("../styles/tokens.css", import.meta.url), "utf8");
const faces = readFileSync(new URL("../styles/fonts.css", import.meta.url), "utf8");

it("pairs bundled Anthropic Latin text with PingFang-first CJK fallbacks", () => {
  expect(tokens).toMatch(/--font-app:\s*"Anthropic Sans",\s*var\(--font-cjk\)/);
  expect(tokens).toMatch(/--font-cjk:\s*"PingFang SC",[^;]*"Noto Sans SC"/);
  expect(tokens).toMatch(/--font-ui:\s*var\(--font-app\)/);
  expect(tokens).toMatch(/--font-prose:\s*var\(--font-app\)/);
  expect(tokens).toMatch(/--font-mono:[^;]*Consolas[^;]*monospace/);
});

it("ships the default faces locally with non-blocking loading", () => {
  for (const name of ["anthropic-sans-roman.woff2", "anthropic-sans-italic.woff2", "noto-sans-sc-variable.ttf"]) {
    expect(faces).toContain(`/fonts/${name}`);
    expect(existsSync(fileURLToPath(new URL(`../../public/fonts/${name}`, import.meta.url)))).toBe(true);
  }
  expect(faces).toContain("font-display: swap");
  expect(faces).not.toMatch(/url\(["']?https?:/);
});
