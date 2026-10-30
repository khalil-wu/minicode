/* @vitest-environment jsdom */
import { loadConversationMenu } from "./ConversationMenu";

// These action suites use a ready interaction module; cold ownership is covered separately.
beforeAll(async () => { await loadConversationMenu(); });
import { act, cleanup, fireEvent, render, renderHook, screen, waitFor, within } from "@testing-library/react";
import { beforeAll, afterEach, beforeEach, expect, it, vi } from "vitest";
import { useAppStore } from "../stores";
import { ConversationsTab } from "./ConversationsTab";
import { SIDEBAR_SECTIONS_EVENT, useSidebarSections, type SidebarSection } from "../protocol/thread-metadata";
import { handleControlPlaneProjectionEvent } from "../chat/controlPlaneEvents";

const mocks = vi.hoisted(() => ({ fetch: vi.fn(), prompt: vi.fn(), confirm: vi.fn(), command: vi.fn(), archive: vi.fn(),
  sessionId: "original-window-session", sections: [] as SidebarSection[] }));
vi.mock("../hooks/useWebSocket", () => ({ getWebSocket: () => ({ sessionId: mocks.sessionId }) }));
vi.mock("../desktop/runtime", () => ({ isDesktop: () => false, desktop: () => undefined, revealPath: vi.fn(), openExternal: vi.fn() }));
vi.mock("../overlays/DialogService", () => ({ showConfirm: mocks.confirm, showPrompt: mocks.prompt }));
vi.mock("../overlays/ToastContainer", () => ({ pushToast: vi.fn() }));
vi.mock("../chat/archiveConversation", () => ({ setConversationArchived: mocks.archive }));
vi.mock("../protocol/api", async importOriginal => ({ ...await importOriginal<object>(),
  apiBase: () => "http://owning-host", fetchWithTimeout: mocks.fetch, authHeaders: (headers: object) => headers }));
vi.mock("../protocol/ws-outbox", () => ({
  LONG_COMMAND_RESULT_TIMEOUT_MS: 600_000, sendClientCommand: vi.fn(() => true),
  sendClientCommandAwaitResult: mocks.command, sendConversationDeleteCommand: vi.fn(async () => true),
  commandResultSucceeded: (result: { level: string }) => result.level === "success",
}));

const response = (payload: unknown) => new Response(JSON.stringify(payload), { headers: { "Content-Type": "application/json" } });
const section = (keys = ["thread:direct", "project:c:/project"]): SidebarSection => ({
  id: "source-section", name: "Source section", appearance: null, item_keys: keys,
});
const projectChats = Array.from({ length: 7 }, (_, index) => ({ id: `project-${index}`, title: `Project task ${index}`,
  updatedAt: `2026-10-${String(20 - index).padStart(2, "0")}`, workspaceRoot: "C:/project" }));

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.removeItem("minicode.sidebar.conversations.state");
  mocks.sessionId = "original-window-session";
  mocks.sections = [section()];
  mocks.confirm.mockResolvedValue(true);
  mocks.prompt.mockResolvedValue(null);
  mocks.command.mockImplementation(async (command: { type: string }) => ({ type: "command.result", command: command.type, level: "success", message: "", data: {} }));
  mocks.archive.mockResolvedValue({ type: "command.result", command: "conversation.archive", level: "success", message: "", data: {} });
  mocks.fetch.mockImplementation(async (url: URL, options?: RequestInit) => {
    if (url.pathname === "/api/thread-metadata/sections" && !options?.method) return response({ sections: mocks.sections });
    if (url.pathname === "/api/thread-metadata/sections" && options?.method === "POST") return response({ section: { ...section([]), id: "created-section" } });
    if (url.pathname.startsWith("/api/thread-metadata/sections/") && options?.method) return response({});
    throw new Error(`Unexpected metadata request ${url}`);
  });
  useAppStore.setState({ appMode: "cowork", conversationId: "project-0", pendingConversationSwitchId: null,
    conversations: [{ id: "direct", title: "Direct source task", updatedAt: "2026-10-21" }, ...projectChats],
    recentWorkspaces: [{ path: "C:/project", name: "Project", projectType: "node", lastOpened: 1, pinned: true, named: true }],
    conversationMessages: {}, conversationStreaming: {}, conversationHydration: {}, messages: [],
    draft: "Keep the active draft", attachments: [], selectedMentions: [], selectedSkills: [], quotedMessage: null,
    workingDirectory: "C:/project", workspaceGit: null, isConnected: true, isStreaming: false,
    pendingApproval: null, approvalQueue: [], pendingDiffReview: null, diffReviewQueue: [], pendingAskUser: null, askUserQueue: [],
    runtimeSession: null, sidebarOrder: null });
});
afterEach(() => cleanup());

it("keeps a grouped project's real expansion, unique selected row and section-aware numbered navigation", async () => {
  const request = vi.fn();
  const original = useAppStore.getState().requestConversationSwitch;
  useAppStore.setState({ requestConversationSwitch: request });
  try {
    const view = render(<ConversationsTab conversationId="project-0" onSetConfirmDialog={vi.fn()} />);
    const group = await screen.findByRole("region", { name: "Source section" });
    const project = within(group).getByRole("region", { name: "工作区 Project" });
    expect(within(project).getAllByRole("button", { name: /^Project task/ })).toHaveLength(5);
    expect(view.container.querySelectorAll('[data-session-row][data-active="true"]')).toHaveLength(1);
    expect(within(project).getByRole("button", { name: "Project task 0", exact: true }).getAttribute("aria-current")).toBe("page");
    fireEvent.click(within(project).getByRole("button", { name: "展开显示" }));
    expect(within(project).getAllByRole("button", { name: /^Project task/ })).toHaveLength(7);
    act(() => window.dispatchEvent(new CustomEvent("select-numbered-chat", { detail: { index: 0, conversationId: "project-0" } })));
    expect(request).toHaveBeenLastCalledWith("direct");
    act(() => window.dispatchEvent(new CustomEvent("select-numbered-chat", { detail: { index: 1, conversationId: "project-0" } })));
    expect(request).toHaveBeenLastCalledWith("project-0");
    expect(useAppStore.getState().draft).toBe("Keep the active draft");
  } finally { useAppStore.setState({ requestConversationSwitch: original }); }
});

it("marks and archives every real chat in the section and its project, including collapsed project rows", async () => {
  render(<ConversationsTab conversationId="project-0" onSetConfirmDialog={vi.fn()} />);
  const group = await screen.findByRole("region", { name: "Source section" });
  fireEvent.click(within(group).getByRole("button", { name: "分组选项：Source section" }));
  fireEvent.click(screen.getByRole("menuitem", { name: "全部标记为已读" }));
  await waitFor(() => expect(mocks.command.mock.calls.filter(([command]) => command.type === "conversation.read_state.set")).toHaveLength(8));
  const ids = mocks.command.mock.calls.filter(([command]) => command.type === "conversation.read_state.set").map(([command]) => command.conversation_id);
  expect(new Set(ids)).toEqual(new Set(["direct", ...projectChats.map(chat => chat.id)]));
  fireEvent.click(within(group).getByRole("button", { name: "分组选项：Source section" }));
  fireEvent.click(screen.getByRole("menuitem", { name: "归档聊天" }));
  await waitFor(() => expect(mocks.archive).toHaveBeenCalledTimes(8));
  expect(mocks.archive.mock.calls.map(([id]) => id)).toEqual(ids);
  expect(useAppStore.getState().draft).toBe("Keep the active draft");
});

it("keeps the project's chats in its section when the committed project inventory unpins it, including after remount", async () => {
  const view = render(<ConversationsTab conversationId="project-0" onSetConfirmDialog={vi.fn()} />);
  await screen.findByRole("region", { name: "Source section" });
  act(() => { handleControlPlaneProjectionEvent({ type: "workspace.recent.list", projects: [{
    path: "C:/project", name: "Project", project_type: "node", last_opened: 1, pinned: false, named: true,
  }] }); });
  let group = screen.getByRole("region", { name: "Source section" });
  expect(within(group).getByRole("region", { name: "工作区 Project" })).toBeTruthy();
  expect(within(group).getByRole("button", { name: "Project task 0", exact: true })).toBeTruthy();
  expect(screen.getAllByRole("region", { name: "工作区 Project" })).toHaveLength(1);
  view.unmount();
  render(<ConversationsTab conversationId="project-0" onSetConfirmDialog={vi.fn()} />);
  group = await screen.findByRole("region", { name: "Source section" });
  expect(within(group).getByRole("button", { name: "Project task 0", exact: true })).toBeTruthy();
  expect(useAppStore.getState()).toMatchObject({ conversationId: "project-0", workingDirectory: "C:/project", draft: "Keep the active draft" });
});

it("uses Source's empty create name and original session without adding a directory or appearance control", async () => {
  mocks.prompt.mockResolvedValue("   ");
  render(<ConversationsTab conversationId="project-0" onSetConfirmDialog={vi.fn()} />);
  const group = await screen.findByRole("region", { name: "Source section" });
  fireEvent.click(within(group).getByRole("button", { name: "分组选项：Source section" }));
  fireEvent.click(screen.getByRole("menuitem", { name: "新建分组" }));
  await waitFor(() => expect(mocks.fetch.mock.calls.some(([_url, options]) => options?.method === "POST")).toBe(true));
  const [url, options] = mocks.fetch.mock.calls.find(([_url, current]) => current?.method === "POST")!;
  expect(url.searchParams.get("session_id")).toBe("original-window-session");
  expect(JSON.parse(options.body)).toEqual({ name: "新建分组" });
  expect(mocks.prompt).toHaveBeenCalledWith(expect.objectContaining({ title: "新建分组", placeholder: "分组名称", allowEmpty: true }));
  expect(screen.queryByRole("button", { name: /外观|appearance|folder picker/i })).toBeNull();
});

it("does not overwrite a committed websocket section projection with an older initial HTTP response", async () => {
  let complete!: (value: Response) => void;
  mocks.fetch.mockImplementationOnce(() => new Promise<Response>(resolve => { complete = resolve; }));
  const hook = renderHook(() => useSidebarSections());
  const mutate = hook.result.current.mutate;
  await waitFor(() => expect(mocks.fetch).toHaveBeenCalledOnce());
  const latest = { ...section(), name: "Committed Source name" };
  act(() => window.dispatchEvent(new CustomEvent(SIDEBAR_SECTIONS_EVENT, { detail: [latest] })));
  await act(async () => complete(response({ sections: [section()] })));
  expect(hook.result.current.sections).toEqual([latest]);
  hook.rerender();
  expect(hook.result.current.mutate).toBe(mutate);
});

it("creates a row's new section after its current projected section", async () => {
  render(<ConversationsTab conversationId="direct" onSetConfirmDialog={vi.fn()} />);
  await screen.findByRole("region", { name: "Source section" });
  const current = { ...section(["thread:direct"]), id: "current-section", name: "Current section" };
  act(() => window.dispatchEvent(new CustomEvent(SIDEBAR_SECTIONS_EVENT, { detail: [current] })));
  const group = await screen.findByRole("region", { name: "Current section" });
  mocks.prompt.mockResolvedValue("Fresh group");
  fireEvent.click(within(group).getByRole("button", { name: "会话操作" }));
  fireEvent.click(screen.getByRole("menuitem", { name: "分组", exact: true }));
  fireEvent.click(screen.getByRole("menuitem", { name: "新建分组…" }));
  await waitFor(() => expect(mocks.fetch.mock.calls.some(([url, options]) => url.pathname === "/api/thread-metadata/sections/move" && options?.method === "POST")).toBe(true));
  const [createUrl, createOptions] = mocks.fetch.mock.calls.find(([url, options]) => url.pathname === "/api/thread-metadata/sections" && options?.method === "POST")!;
  expect(createUrl.searchParams.get("session_id")).toBe("original-window-session");
  expect(JSON.parse(createOptions.body)).toEqual({ name: "Fresh group", after_section_id: "current-section" });
  const [moveUrl, moveOptions] = mocks.fetch.mock.calls.find(([url, options]) => url.pathname === "/api/thread-metadata/sections/move" && options?.method === "POST")!;
  expect(moveUrl.searchParams.get("session_id")).toBe("original-window-session");
  expect(JSON.parse(moveOptions.body)).toEqual({ kind: "thread", id: "direct", section_id: "created-section" });
  expect(useAppStore.getState().draft).toBe("Keep the active draft");
});
