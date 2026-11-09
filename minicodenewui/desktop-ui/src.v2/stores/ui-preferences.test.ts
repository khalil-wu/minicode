/* @vitest-environment jsdom */

import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { syncSystemTheme, useAppStore } from ".";
import { initialViewMode, LS } from "./shared-helpers";

describe("UI preference persistence", () => {
  beforeEach(() => {
    localStorage.clear();
    useAppStore.setState({ viewMode: "normal" });
  });

  afterEach(() => {
    localStorage.clear();
    useAppStore.setState({ viewMode: "normal" });
  });

  it("restores only supported process detail modes", () => {
    expect(initialViewMode()).toBe("normal");
    localStorage.setItem(LS.viewMode, "summary");
    expect(initialViewMode()).toBe("summary");
    localStorage.setItem(LS.viewMode, "verbose");
    expect(initialViewMode()).toBe("verbose");
    localStorage.setItem(LS.viewMode, "unknown");
    expect(initialViewMode()).toBe("normal");
  });

  it("persists process detail changes from the app store", () => {
    useAppStore.getState().setViewMode("summary");

    expect(useAppStore.getState().viewMode).toBe("summary");
    expect(localStorage.getItem(LS.viewMode)).toBe("summary");
  });

  it("projects system color-scheme changes into reactive theme state", () => {
    let prefersLight = true;
    const originalMatchMedia = window.matchMedia;
    Object.defineProperty(window, "matchMedia", {
      configurable: true,
      value: () => ({
        matches: prefersLight,
        media: "(prefers-color-scheme: light)",
        onchange: null,
        addListener: () => {},
        removeListener: () => {},
        addEventListener: () => {},
        removeEventListener: () => {},
        dispatchEvent: () => false,
      }),
    });
    try {
      useAppStore.getState().setThemeMode("system");
      expect(useAppStore.getState().resolvedTheme).toBe("light");
      expect(document.documentElement.getAttribute("data-theme")).toBe("light");

      prefersLight = false;
      syncSystemTheme();
      expect(useAppStore.getState().resolvedTheme).toBe("dark");
      expect(document.documentElement.getAttribute("data-theme")).toBe("dark");
    } finally {
      Object.defineProperty(window, "matchMedia", {
        configurable: true,
        value: originalMatchMedia,
      });
      useAppStore.getState().setThemeMode("system");
    }
  });
});
