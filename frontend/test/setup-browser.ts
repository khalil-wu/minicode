import { afterEach, beforeEach, vi } from "vitest";
import "fake-indexeddb/auto";
import { resetEditorDraftStorageForTests } from "../src.v2/stores/editor-drafts";
import { flushEditorDrafts } from "../src.v2/stores/editor-drafts";
import { flushComposerDrafts } from "../src.v2/stores/composer-drafts";

// JSDOM has no layout observer. Geometry/lifetime specs install their own
// callbacks; other UI specs still need the browser API at shared primitives.
// This shared setup also runs for Node-only store and projection specs.
if (typeof Element !== "undefined") Element.prototype.scrollIntoView = vi.fn();
beforeEach(async () => {
  vi.stubGlobal("matchMedia", vi.fn((query: string) => ({ matches: query.includes("prefers-reduced-motion"), media: query, addEventListener() {}, removeEventListener() {} })));
  // Only a real renderer store installs this handle. Partial store mocks and
  // pure Node specs must not import a second store merely to run setup.
  if (typeof window !== "undefined" && (window as Window & { __zustandStore?: unknown }).__zustandStore) {
    const { waitComposerDraftRecovery, useAppStore } = await import("../src.v2/stores");
    await waitComposerDraftRecovery();
    const preference = useAppStore.getState().reducedMotionPreference;
    const reducedMotion = preference !== "off";
    document.documentElement.setAttribute("data-reduced-motion", String(reducedMotion));
    useAppStore.setState({ reducedMotion });
    useAppStore.setState({ activeConversationDraftId: null, conversationDrafts: {}, homeConversationDraftIds: {} });
    const { resetComposerDraftStorageForTests } = await import("../src.v2/stores/composer-drafts");
    await resetComposerDraftStorageForTests();
  }
  await resetEditorDraftStorageForTests();
  vi.stubGlobal("ResizeObserver", class {
    observe() {}
    unobserve() {}
    disconnect() {}
  });
});
afterEach(async () => {
  if (typeof window !== "undefined" && (window as Window & { __zustandStore?: unknown }).__zustandStore) {
    const { waitComposerDraftRecovery, waitEditorDraftRecovery } = await import("../src.v2/stores");
    await Promise.all([waitComposerDraftRecovery(), waitEditorDraftRecovery()]);
    await Promise.all([flushComposerDrafts(), flushEditorDrafts()]);
  }
});
