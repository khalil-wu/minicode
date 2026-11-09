// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { initializeWindowNavigation } from "./windowNavigation";
import { conversationIdForSessionRestore } from "../hooks/useWebSocket";
import { LS, writeLS } from "../stores/shared-helpers";

const consume = vi.fn(() => true);
const driver = { windowControls: { consumeInitialNavigation: consume } } as never;
beforeEach(() => { localStorage.clear(); consume.mockClear(); });
afterEach(() => { delete window.__MINICODE_RUNTIME__; });

it("keeps primary and secondary active owners independent while applying the requested native route", () => {
  localStorage.setItem("minicode.conversation.active-id", "primary-current");
  window.__MINICODE_RUNTIME__ = { windowOwnerId: "secondary-1", initialNavigation: { conversationId: "existing-task" }, desktop: driver };
  localStorage.setItem(LS.conversation.activeId, "secondary-old");
  initializeWindowNavigation();
  expect(conversationIdForSessionRestore({})).toBe("existing-task");
  expect(localStorage.getItem("minicode.conversation.active-id")).toBe("primary-current");
  writeLS(LS.conversation.activeId, "secondary-next");
  expect(conversationIdForSessionRestore({})).toBe("secondary-next");
  window.__MINICODE_RUNTIME__ = { windowOwnerId: "secondary-1" };
  initializeWindowNavigation();
  expect(conversationIdForSessionRestore({})).toBe("secondary-next");
});

it("does not resurrect the initial thread after home or clear even while the renderer retains its initial runtime snapshot", () => {
  window.__MINICODE_RUNTIME__ = { windowOwnerId: "secondary-1", initialNavigation: { conversationId: "initial-task" }, desktop: driver };
  initializeWindowNavigation();
  expect(conversationIdForSessionRestore({})).toBe("initial-task");
  writeLS(LS.conversation.activeId, "");
  expect(window.__MINICODE_RUNTIME__.initialNavigation?.conversationId).toBe("initial-task");
  expect(consume).toHaveBeenCalledTimes(1);
  expect(conversationIdForSessionRestore({ conversationId: null })).toBe("");
  expect(conversationIdForSessionRestore({ conversationId: "created-later" })).toBe("created-later");
});

it("leaves an empty new window empty without borrowing the primary navigation", () => {
  localStorage.setItem("minicode.conversation.active-id", "primary-current");
  window.__MINICODE_RUNTIME__ = { windowOwnerId: "secondary-2" };
  initializeWindowNavigation();
  expect(conversationIdForSessionRestore({})).toBe("");
  expect(localStorage.getItem(LS.conversation.activeId)).toBeNull();
});

it("applies an explicit fresh home route when the native secondary owner slot is reused without clearing its authored storage", () => {
  window.__MINICODE_RUNTIME__ = { windowOwnerId: "secondary-1", initialNavigation: { conversationId: null }, desktop: driver };
  writeLS(LS.conversation.activeId, "old-secondary-task");
  localStorage.setItem("authored-buffer-secondary-1", "unsaved draft kept");
  initializeWindowNavigation();
  expect(conversationIdForSessionRestore({})).toBe("");
  expect(localStorage.getItem("authored-buffer-secondary-1")).toBe("unsaved draft kept");
});

it("retains the last primary owner during actual application startup rather than applying a fresh-window route", () => {
  window.__MINICODE_RUNTIME__ = { windowOwnerId: "primary" };
  writeLS(LS.conversation.activeId, "primary-last-task");
  initializeWindowNavigation();
  expect(conversationIdForSessionRestore({})).toBe("primary-last-task");
});

it("reopens the stable Popout owner without replacing its saved conversation or another window's draft", () => {
  localStorage.setItem("minicode.conversation.active-id", "primary-current");
  window.__MINICODE_RUNTIME__ = { windowOwnerId: "popout", initialNavigation: { conversationId: null, popout: true,
    workspaceRoot: "C:/Project", restoreLastConversation: true }, desktop: driver };
  writeLS(LS.conversation.activeId, "popout-current");
  localStorage.setItem("authored-popout-draft", "Unsent text");
  initializeWindowNavigation();
  expect(conversationIdForSessionRestore({})).toBe("popout-current");
  expect(localStorage.getItem("minicode.conversation.active-id")).toBe("primary-current");
  expect(localStorage.getItem("authored-popout-draft")).toBe("Unsent text");
  expect(consume).toHaveBeenCalledOnce();
});
