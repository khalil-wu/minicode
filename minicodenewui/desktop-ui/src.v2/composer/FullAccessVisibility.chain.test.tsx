/* @vitest-environment jsdom */
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { FooterRow } from "./FooterRow";
import { useAppStore } from "../stores";

const preferences = vi.hoisted(() => ({ showFullAccess: false }));
vi.mock("../hooks/useDesktopPreferences", () => ({ useDesktopPreferences: () => ({ preferences, error: "", update: vi.fn() }) }));
afterEach(cleanup);
beforeEach(() => { preferences.showFullAccess = false; useAppStore.setState({ permissionMode: "bypass", conversationId: "actual-task", workingDirectory: "C:/actual-task",
  runtimeCapabilities: { permission: { available_modes: ["confirm", "bypass"] } } as never,
  pendingConversationSwitchId: null, draft: "Keep this draft", attachments: [{ id: "kept", name: "note.txt", type: "text/plain", size: 4, status: "ready" }] }); });

it("hides full access from the picker while retaining the active task's real permission and authored input", () => {
  render(<FooterRow sendState="idle" onSend={() => {}} />);
  fireEvent.click(screen.getByTitle("更改权限"));
  expect(screen.queryByRole("option", { name: /完全访问/ })).toBeNull();
  expect(screen.getByTitle("更改权限")).toBeTruthy();
  expect(document.querySelector(".composer-footer-integrated")?.getAttribute("data-permission-mode")).toBe("bypass");
  expect(useAppStore.getState()).toMatchObject({ permissionMode: "bypass", draft: "Keep this draft", attachments: [{ id: "kept" }] });
});

it("updates the existing picker when the persisted preference changes without replacing the task", () => {
  const view = render(<FooterRow sendState="idle" onSend={() => {}} />);
  fireEvent.click(screen.getByTitle("更改权限"));
  preferences.showFullAccess = true;
  view.rerender(<FooterRow sendState="idle" onSend={() => {}} />);
  expect(screen.getByRole("option", { name: /完全访问/ })).toBeTruthy();
  preferences.showFullAccess = false;
  view.rerender(<FooterRow sendState="idle" onSend={() => {}} />);
  expect(screen.queryByRole("option", { name: /完全访问/ })).toBeNull();
  expect(useAppStore.getState().conversationId).toBe("actual-task");
});
