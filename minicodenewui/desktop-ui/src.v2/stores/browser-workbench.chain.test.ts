/* @vitest-environment jsdom */
import { beforeEach, expect, it, vi } from "vitest";
import { browserOwnerId, currentBrowserOwnerId, draftBrowserOwnerId, projectBrowserWorkbenchEvent, readBrowserWorkbench,
  transferDraftBrowserWorkbench, writeBrowserWorkbench } from "./browser-workbench";
import { useAppStore } from "./index";
import { __resetOpenWebInBrowserForTests, acknowledgeBrowserRequest, openNewBrowserTab, subscribeBrowserRequests,
  transferDraftBrowserRequests } from "../chat/openWebInBrowser";
import type { ConversationDraftContext } from "./types";

const native = vi.hoisted(() => ({ transfer: vi.fn() }));
vi.mock("../desktop/runtime", async load => ({ ...await load<typeof import("../desktop/runtime")>(),
  desktop: () => ({ embeddedBrowser: { transferOwner: native.transfer }, preferences: { get: async () => ({ defaultTaskDirectory: "", defaultPermissionMode: "confirm" }) } }) }));
const context = { id: "local", workspaceRoot: "", adopted: false } as ConversationDraftContext;
const state = { conversationId: null, activeConversationDraftId: "local", conversationDrafts: { local: context } };

beforeEach(() => {
  localStorage.clear(); __resetOpenWebInBrowserForTests(); native.transfer.mockReset().mockResolvedValue(1);
  window.__MINICODE_RUNTIME__ = { windowOwnerId: "browser-test-window" };
});

it("separates browser ownership from backend identity and includes its native window", () => {
  expect(browserOwnerId(state)).toBe("client-draft:browser-test-window:local");
  expect(browserOwnerId({ ...state, conversationId: "server" })).toBe("server");
  expect(browserOwnerId({ ...state, conversationDrafts: { local: { ...context, adopted: true, conversationId: "real" } } })).toBe("real");
  window.__MINICODE_RUNTIME__ = { windowOwnerId: "another-window" };
  expect(browserOwnerId(state)).toBe("client-draft:another-window:local");
});

it("waits for native ownership transfer before moving persisted tabs and selected address", async () => {
  const source = draftBrowserOwnerId("local");
  writeBrowserWorkbench(source, { tabs: [{ id: "page", title: "Actual", url: "https://example.com/", draftUrl: "unsent" }], activeId: "page",
    layout: { rightPanelOpen: true, rightPanelExpanded: false, rightStackTab: "browser", rightStackTabLocked: false } });
  let finish!: (n: number) => void;
  native.transfer.mockReturnValueOnce(new Promise<number>(done => { finish = done; }));
  const moving = transferDraftBrowserWorkbench("local", "real");
  expect(readBrowserWorkbench("real")).toBeNull();
  expect(readBrowserWorkbench(source)?.activeId).toBe("page");
  finish(1); await moving;
  expect(native.transfer).toHaveBeenCalledWith({ fromOwner: source, toOwner: "real" });
  expect(readBrowserWorkbench(source)).toBeNull();
  expect(readBrowserWorkbench("real")).toMatchObject({ activeId: "page", tabs: [{ id: "page", draftUrl: "unsent" }], layout: { rightStackTab: "browser" } });
  expect(currentBrowserOwnerId(source)).toBe("real");
});

it("keeps the source snapshot when the real native transfer fails", async () => {
  writeBrowserWorkbench(draftBrowserOwnerId("failed"), { tabs: [], activeId: "" });
  native.transfer.mockRejectedValueOnce(new Error("actual transfer error"));
  await expect(transferDraftBrowserWorkbench("failed", "real")).rejects.toThrow("actual transfer error");
  expect(readBrowserWorkbench(draftBrowserOwnerId("failed"))).toEqual({ tabs: [], activeId: "" });
  expect(readBrowserWorkbench("real")).toBeNull();
});

it("merges native updates arriving during transfer without replacing source address edits, order or selection", async () => {
  const source = draftBrowserOwnerId("moving");
  writeBrowserWorkbench(source, { tabs: [
    { id: "blank", title: "New tab", url: "", draftUrl: "" },
    { id: "page", title: "Old page", url: "https://example.com/old", draftUrl: "unfinished address" },
  ], activeId: "blank" });
  let finish!: (n: number) => void;
  native.transfer.mockReturnValueOnce(new Promise<number>(done => { finish = done; }));
  const moving = transferDraftBrowserWorkbench("moving", "real");
  projectBrowserWorkbenchEvent({ id: "page", conversationId: "real", type: "updated", title: "Newest native title",
    url: "https://example.com/new", loading: false, canGoBack: true, canGoForward: false });
  projectBrowserWorkbenchEvent({ id: "native-child", conversationId: "real", type: "page", title: "Native child",
    url: "https://example.com/child", active: false, loading: false, canGoBack: false, canGoForward: false });
  finish(1); await moving;
  const result = readBrowserWorkbench("real")!;
  expect(result.activeId).toBe("blank");
  expect(result.tabs.map(tab => tab.id)).toEqual(["blank", "page", "native-child"]);
  expect(result.tabs[1]).toMatchObject({ url: "https://example.com/new", title: "Newest native title", draftUrl: "unfinished address" });
});

it("does not resurrect a source page closed after native transfer but before the IPC response", async () => {
  const source = draftBrowserOwnerId("closed-during-move");
  writeBrowserWorkbench(source, { tabs: [{ id: "closed-page", title: "Page", url: "https://example.com/", draftUrl: "unsent" }], activeId: "closed-page" });
  let finish!: (n: number) => void;
  native.transfer.mockReturnValueOnce(new Promise<number>(done => { finish = done; }));
  const moving = transferDraftBrowserWorkbench("closed-during-move", "real");
  projectBrowserWorkbenchEvent({ id: "closed-page", conversationId: "real", type: "closed", title: "Page", url: "https://example.com/",
    loading: false, canGoBack: false, canGoForward: false });
  finish(1); await moving;
  expect(readBrowserWorkbench("real")).toEqual({ tabs: [], activeId: "" });
});

it("does not synchronously rewrite storage for loading events with unchanged persisted fields", () => {
  writeBrowserWorkbench("loading", { tabs: [{ id: "page", title: "Page", url: "https://example.com/", draftUrl: "https://example.com/" }], activeId: "page" });
  const writes = vi.spyOn(Storage.prototype, "setItem");
  projectBrowserWorkbenchEvent({ id: "page", conversationId: "loading", type: "loading", title: "Page", url: "https://example.com/",
    loading: true, canGoBack: false, canGoForward: false });
  expect(writes).not.toHaveBeenCalled();
  writes.mockRestore();
});

it("does not add a native round trip to first send when the draft never opened the browser", async () => {
  await transferDraftBrowserWorkbench("unused", "real");
  expect(native.transfer).not.toHaveBeenCalled();
});

it("projects background native updates and close to their owner without changing the selected chat", () => {
  useAppStore.setState({ conversationId: "selected", draft: "Keep selected draft" });
  writeBrowserWorkbench("background", { tabs: [{ id: "bg", title: "Old", url: "https://example.com/", draftUrl: "edited address" }], activeId: "bg" });
  const event = { id: "bg", conversationId: "background", type: "updated" as const, title: "Loaded", url: "https://example.com/",
    loading: false, canGoBack: false, canGoForward: false };
  projectBrowserWorkbenchEvent(event);
  expect(readBrowserWorkbench("background")?.tabs[0]).toMatchObject({ title: "Loaded", draftUrl: "edited address" });
  projectBrowserWorkbenchEvent({ ...event, type: "closed" });
  expect(readBrowserWorkbench("background")).toEqual({ tabs: [], activeId: "" });
  expect(useAppStore.getState()).toMatchObject({ conversationId: "selected", draft: "Keep selected draft" });
});

it("keeps every new-tab click isolated by owner and migrates unconsumed draft requests", () => {
  useAppStore.setState(state);
  openNewBrowserTab(); openNewBrowserTab();
  useAppStore.setState({ conversationId: "different" }); openNewBrowserTab();
  transferDraftBrowserRequests(draftBrowserOwnerId("local"), "real");
  const seen: { id: number; conversationId: string }[] = [];
  const stop = subscribeBrowserRequests(request => { seen.push(request); acknowledgeBrowserRequest(request.id); }); stop();
  expect(seen.map(request => request.conversationId)).toEqual(["real", "real", "different"]);
  expect(new Set(seen.map(request => request.id)).size).toBe(3);
});
