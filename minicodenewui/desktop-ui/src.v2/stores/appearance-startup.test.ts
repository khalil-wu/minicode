/* @vitest-environment jsdom */
import { expect, it, vi } from "vitest";

it("applies saved colors during the original renderer-store startup without opening Appearance", async () => {
  expect(getComputedStyle(document.documentElement).getPropertyValue("--surface-base")).toBe("");
  localStorage.setItem("minicode.theme", "light");
  localStorage.setItem("minicode.appearance.colors", JSON.stringify({ light: { surface: "#e8edf3", accent: "#267c51", ink: "#1a1c1f" }, dark: null }));
  localStorage.setItem("minicode.textScale", "1.125");
  const originalMatchMedia = window.matchMedia;
  window.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {} }) as MediaQueryList;
  try {
    vi.resetModules();
    const { useAppStore } = await import(".");
    expect(useAppStore.getState().settingsOpen).toBe(false);
    expect(useAppStore.getState().resolvedTheme).toBe("light");
    expect(document.documentElement.style.getPropertyValue("--surface-base")).toBe("#e8edf3");
    expect(document.documentElement.style.getPropertyValue("--accent-primary")).toBe("#267c51");
    expect(document.documentElement.style.getPropertyValue("--app-text-scale")).toBe("1.125");
    expect(localStorage.getItem("minicode.textScale")).toBe("1.125");
  } finally { localStorage.clear(); window.matchMedia = originalMatchMedia; }
});
