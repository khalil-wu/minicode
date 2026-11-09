/* @vitest-environment jsdom */
import { afterEach, expect, it, vi } from "vitest";

afterEach(() => localStorage.clear());

it.each([[false, true, false], [true, true, true], [true, false, true]])(
  "restores right-open=%s and saved-pin=%s with summary hidden=%s",
  async (rightOpen, pinned, collapsed) => {
    localStorage.setItem("minicode.layout.right-open", rightOpen ? "1" : "0");
    localStorage.setItem("minicode.summary-pinned", pinned ? "1" : "0");
    vi.resetModules();
    const { useAppStore, waitComposerDraftRecovery } = await import(".");
    await waitComposerDraftRecovery();
    expect(useAppStore.getState()).toMatchObject({ rightPanelOpen: rightOpen, compactPanel: null,
      contextCardPinned: pinned, contextCardCollapsed: collapsed });
    expect(localStorage.getItem("minicode.summary-pinned")).toBe(pinned ? "1" : "0");
  },
);
