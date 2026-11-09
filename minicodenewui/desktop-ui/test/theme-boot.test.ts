/* @vitest-environment jsdom */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { runInNewContext } from "node:vm";
import { afterEach, expect, it, vi } from "vitest";
import { applyTheme, initialTheme, LS } from "../src.v2/stores/shared-helpers";

const source = readFileSync(resolve("public/theme-boot.js"), "utf8");
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); localStorage.clear(); });

it.each([
  ["dark", true], ["light", false], ["system", true], ["system", false],
  [null, true], [null, false], ["unsupported", true],
])("matches the actual store theme before React starts (preference=%s, light=%s)", (preference, light) => {
  localStorage.clear();
  if (preference !== null) localStorage.setItem(LS.theme, preference);
  vi.stubGlobal("matchMedia", () => ({ matches: light }));
  runInNewContext(source, { document, localStorage, matchMedia });
  const bootTheme = document.documentElement.getAttribute("data-theme");
  expect(applyTheme(initialTheme())).toBe(bootTheme);
});

it("uses the same system theme as the store when browser storage is unavailable", () => {
  vi.stubGlobal("matchMedia", () => ({ matches: true }));
  vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new DOMException("Storage unavailable", "SecurityError"); });
  runInNewContext(source, { document, localStorage, matchMedia });
  expect(document.documentElement.getAttribute("data-theme")).toBe("light");
  expect(applyTheme(initialTheme())).toBe("light");
});
