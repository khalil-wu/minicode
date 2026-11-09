// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { useAppStore } from "../stores";
import { handleSessionEvent } from "../chat/sessionEvents";
import type { ServerEvent } from "../protocol/events";
import { ProjectContextMenu } from "./ProjectContextMenu";

const mocks = vi.hoisted(() => ({ send: vi.fn(), fetch: vi.fn(), confirm: vi.fn(), toast: vi.fn(), native: false, open: vi.fn() }));
vi.hoisted(() => Object.defineProperty(globalThis, "matchMedia", { configurable: true, writable: true,
  value: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }),
}));
vi.mock("../protocol/ws-outbox", async load => ({ ...await load<typeof import("../protocol/ws-outbox")>(), sendClientCommandAwaitResult: mocks.send }));
vi.mock("../protocol/api", async load => ({ ...await load<typeof import("../protocol/api")>(), apiBase: () => "http://project-owner", fetchWithTimeout: mocks.fetch, authHeaders: () => ({}) }));
vi.mock("../desktop/runtime", () => ({ isDesktop: () => mocks.native, desktop: () => ({ openPreferredPath: mocks.open }) }));
vi.mock("../overlays/DialogService", () => ({ showConfirm: mocks.confirm, showPrompt: vi.fn() }));
vi.mock("../overlays/ToastContainer", () => ({ pushToast: mocks.toast }));

const project = { path: "C:/A", name: "Project A", project_type: "project", last_opened: 1, source_folders: ["C:/A"], pinned: true, named: true };
const menu = () => render(<ProjectContextMenu path="C:/A" position={{ x: 100, y: 100 }} onClose={vi.fn()} />);
beforeEach(() => {
  vi.clearAllMocks(); mocks.native = false;
  vi.stubGlobal("ResizeObserver", class { observe() {} disconnect() {} });
  mocks.send.mockImplementation(async command => ({ type: "command.result", command: command.type, level: "success", message: "", data: {} }));
  mocks.confirm.mockResolvedValue(true); mocks.open.mockResolvedValue({});
  useAppStore.setState({ conversationId: "reader", workingDirectory: "C:/B", draft: "Keep this reader draft", isConnected: false,
    conversations: [], conversationMessages: {}, messages: [], pendingConversationSwitchId: null, isStreaming: false,
    conversationInventoryInstanceId: null, conversationInventoryRevision: 0,
    recentWorkspaces: [{ path: project.path, name: project.name, projectType: project.project_type, lastOpened: project.last_opened,
      sourceFolders: project.source_folders, named: true, pinned: true }] });
  act(() => { handleSessionEvent({ type: "conversation.list", conversation_id: "reader", active_conversation_id: "reader",
    inventory_instance_id: "a".repeat(32), inventory_revision: 1, conversations: [
      { id: "reader", title: "Current reader", updated_at: "today", project_id: "C:/B", workspace_root: "C:/B" },
      { id: "owned", title: "Moved into A", updated_at: "today", project_id: "C:/A", workspace_root: "C:/elsewhere", has_unread_turn: true },
      { id: "legacy", title: "Existing workspace chat", updated_at: "today", project_id: null, workspace_root: "C:/A", has_unread_turn: true },
      { id: "moved", title: "Moved to B", updated_at: "today", project_id: "C:/B", workspace_root: "C:/A", has_unread_turn: true },
      { id: "removed", title: "Removed from project", updated_at: "today", project_id: "", workspace_root: "C:/A", has_unread_turn: true },
      { id: "archived", title: "Archived A", updated_at: "today", project_id: "C:/A", workspace_root: "C:/A", archived: true, has_unread_turn: true },
    ] } as ServerEvent, { textStreamBuffer: { push: vi.fn(), flush: vi.fn(), destroy: vi.fn() }, thinkingStreamBuffer: { push: vi.fn(), flush: vi.fn(), destroy: vi.fn() } }); });
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it.each(["全部标记为已读", "归档聊天"])("%s uses the public project membership while preserving old workspace chats and the current reader", async label => {
  expect(useAppStore.getState().conversations.find(chat => chat.id === "legacy")!.projectId).toBeNull();
  expect(useAppStore.getState().conversations.find(chat => chat.id === "removed")!.projectId).toBe("");
  menu(); fireEvent.click(screen.getByRole("menuitem", { name: label, exact: true }));
  await waitFor(() => expect(mocks.send).toHaveBeenCalledTimes(2));
  expect(mocks.send.mock.calls.map(([command]) => command.conversation_id)).toEqual(["owned", "legacy"]);
  expect(mocks.send.mock.calls.every(([command]) => command.type === (label === "归档聊天" ? "conversation.archive" : "conversation.read_state.set"))).toBe(true);
  expect(useAppStore.getState().conversations.find(chat => chat.id === "owned")!.hasUnreadTurn).toBe(true);
  expect(useAppStore.getState()).toMatchObject({ conversationId: "reader", workingDirectory: "C:/B", draft: "Keep this reader draft" });
});

it("persists the project pin action and consumes its actual API receipt without changing the current workspace", async () => {
  let finish!: (response: Response) => void;
  mocks.fetch.mockReturnValueOnce(new Promise<Response>(resolve => { finish = resolve; }));
  menu(); fireEvent.click(screen.getByRole("menuitem", { name: "取消固定", exact: true }));
  const [url, options] = mocks.fetch.mock.calls[0];
  expect(url.pathname).toBe("/api/projects");
  expect(options.method).toBe("PATCH");
  expect(JSON.parse(options.body)).toEqual({ path: "C:/A", pinned: false });
  expect(useAppStore.getState().recentWorkspaces[0].pinned).toBe(true);
  await act(async () => finish(new Response(JSON.stringify({ project: { ...project, pinned: false }, projects: [{ ...project, pinned: false }] }))));
  await waitFor(() => expect(useAppStore.getState().recentWorkspaces[0].pinned).toBe(false));
  expect(useAppStore.getState()).toMatchObject({ conversationId: "reader", workingDirectory: "C:/B", draft: "Keep this reader draft" });
});

it("opens a single source in the native file manager without rewriting the preferred target", () => {
  mocks.native = true;
  menu(); fireEvent.click(screen.getByRole("menuitem", { name: "在资源管理器中打开", exact: true }));
  expect(mocks.open).toHaveBeenCalledExactlyOnceWith("C:/A", undefined, { targetId: "fileManager", workspaceRoot: "C:/A" });
  expect(mocks.fetch).not.toHaveBeenCalled();
});

it("omits the file manager action for a multi-source project", () => {
  mocks.native = true;
  useAppStore.setState({ recentWorkspaces: [{ ...useAppStore.getState().recentWorkspaces[0], sourceFolders: ["C:/A", "C:/secondary"] }] });
  menu();
  expect(screen.queryByRole("menuitem", { name: "在资源管理器中打开", exact: true })).toBeNull();
});
