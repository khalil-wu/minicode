/* @vitest-environment jsdom */
import { afterEach, beforeEach, expect, it, vi } from "vitest";

vi.hoisted(() => Object.defineProperty(globalThis, "matchMedia", { configurable: true, writable: true,
  value: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }) }));

import { useAppStore } from "../stores";
import { captureVoicePageContext } from "./voice-page-context";

beforeEach(() => {
  useAppStore.setState({ conversationId: "current", conversations: [
    { id: "foreign", title: "Another thread", updatedAt: "2026-10-21" },
    { id: "current", title: "Current thread", updatedAt: "2026-10-21" },
  ], settingsOpen: false, projectsOpen: false, skillsMarketplaceOpen: false,
    rightPanelOpen: false, rightStackTab: "files", draft: "Keep the unsent draft" });
  document.body.innerHTML = `<aside id="workbench-right-sidebar" class="mc-sidebar-right" data-embedded="false" data-open="true" aria-hidden="false">
    <div class="mc-sidebar-right-header"><div class="mc-sidebar-right-tabs" role="tablist">
      <button role="tab" data-sidebar-tab="files" aria-selected="false">Files</button>
      <div class="mc-sidebar-browser-tab-host"><div class="mc-browser-tab"><button role="tab" aria-selected="true">Private website title</button></div></div>
      <button role="tab" data-sidebar-tab="diff" aria-selected="false">Changes</button>
    </div></div>
    <div role="tabpanel"><button role="tab" data-sidebar-tab="not-a-sidebar-tab" aria-selected="true">Embedded content</button></div>
  </aside>`;
});
afterEach(() => document.body.replaceChildren());

it("captures the actual thread and rendered sidebar tabs without using the stored panel preference or page contents", () => {
  const before = useAppStore.getState();
  expect(captureVoicePageContext()).toEqual({ page: "thread", thread: { id: "current", title: "Current thread" },
    rightSidebarVisible: true, rightPanelTabs: [
      { type: "files", focused: false }, { type: "browser", focused: true }, { type: "diff", focused: false },
    ] });
  expect(useAppStore.getState()).toBe(before);
  expect(JSON.stringify(captureVoicePageContext())).not.toContain("Private website title");
});

it.each([
  { open: "false", hidden: "false" },
  { open: "true", hidden: "true" },
])("keeps mounted tabs while the actual sidebar is closed or hidden: $open/$hidden", ({ open, hidden }) => {
  useAppStore.setState({ rightPanelOpen: true });
  const sidebar = document.querySelector<HTMLElement>("#workbench-right-sidebar")!;
  sidebar.dataset.open = open; sidebar.setAttribute("aria-hidden", hidden);
  expect(captureVoicePageContext()).toMatchObject({ rightSidebarVisible: false,
    rightPanelTabs: [{ type: "files", focused: false }, { type: "browser", focused: true }, { type: "diff", focused: false }] });
});

it("includes the actually open compact right drawer", () => {
  document.querySelector<HTMLElement>("#workbench-right-sidebar")!.dataset.embedded = "true";
  expect(captureVoicePageContext().rightSidebarVisible).toBe(true);
});

it.each([
  { patch: { conversationId: null }, page: "home" },
  { patch: { settingsOpen: true }, page: "settings" },
  { patch: { projectsOpen: true }, page: "other" },
  { patch: { skillsMarketplaceOpen: true }, page: "other" },
  { patch: { projectsOpen: true, settingsOpen: true }, page: "settings" },
])("reports the actual $page route without borrowing the hidden conversation", ({ patch, page }) => {
  useAppStore.setState(patch);
  const summary = captureVoicePageContext();
  expect(summary.page).toBe(page);
  expect(summary).not.toHaveProperty("thread");
  expect(summary.rightSidebarVisible).toBe(page === "home");
});

it("uses the actual selected id and a nullable title while its metadata is unavailable", () => {
  useAppStore.setState({ conversationId: "not-in-catalog" });
  expect(captureVoicePageContext().thread).toEqual({ id: "not-in-catalog", title: null });
});

it("does not invent a panel or tabs before the real sidebar is mounted", () => {
  document.body.replaceChildren();
  useAppStore.setState({ rightPanelOpen: true, rightStackTab: "browser" });
  expect(captureVoicePageContext()).toMatchObject({ rightSidebarVisible: false, rightPanelTabs: [] });
});
