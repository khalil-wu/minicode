// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
const mocks = vi.hoisted(() => ({ pick: vi.fn(), fetch: vi.fn(), activate: vi.fn(), createChat: vi.fn(), send: vi.fn() }));
vi.mock("../protocol/api", async load => ({ ...await load<typeof import("../protocol/api")>(), apiBase: () => "http://projects.test", authHeaders: () => ({}), fetchWithTimeout: mocks.fetch }));
vi.mock("../desktop/runtime", async load => ({ ...await load<typeof import("../desktop/runtime")>(), isDesktop: () => true, pickWorkspaceDirectory: mocks.pick }));
vi.mock("../workspace/openWorkspaceFolder", () => ({ activateWorkspaceFolder: mocks.activate }));
vi.mock("../protocol/ws-outbox", async load => ({ ...await load<typeof import("../protocol/ws-outbox")>(), sendClientCommand: vi.fn(), sendClientCommandAwaitResult: mocks.send }));
vi.mock("../overlays/ToastContainer", () => ({ pushToast: vi.fn() }));
import { useAppStore } from "../stores";
import { ProjectsPage } from "./ProjectsPage";
import { ConversationsTab } from "./ConversationsTab";
import { ProjectDialogHost } from "../overlays/ProjectDialog";

let projects: Array<{ path: string; name: string; project_type: string; last_opened: number; source_folders: string[]; pinned: boolean; named: boolean; appearance?: unknown }>;
beforeEach(() => {
  vi.clearAllMocks();
  vi.stubGlobal("ResizeObserver", class { observe() {} disconnect() {} });
  projects = [];
  mocks.createChat.mockResolvedValue(true); mocks.activate.mockResolvedValue("C:/A");
  mocks.fetch.mockImplementation(async (_url, init) => {
    const values = JSON.parse(init.body);
    if (init.method === "POST") projects.push({ path: values.source_folders[0], project_type: "project", last_opened: 1, pinned: true, named: true, ...values });
    else projects = projects.map(project => project.path === values.path ? { ...project, ...values } : project);
    return new Response(JSON.stringify({ projects, project: projects.find(project => project.path === (values.path || values.source_folders[0])) }), { status: 200 });
  });
  useAppStore.setState({ projectsOpen: true, projectEditTarget: null, recentWorkspaces: [], conversations: [], conversationId: null, workingDirectory: "C:/original", draft: "keep unsaved", attachments: [],
    isConnected: false, isStreaming: false, pendingConversationSwitchId: null, createConversation: mocks.createChat, messages: [], fileTreeVersion: 0 });
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("opens the Source Create modal before choosing folders and creates a named multi-folder project", async () => {
  mocks.pick.mockResolvedValueOnce("C:/A").mockResolvedValueOnce("C:/B");
  render(<><ProjectsPage /><ProjectDialogHost /></>);
  fireEvent.click(screen.getByRole("button", { name: "创建" }));
  const dialog = screen.getByRole("dialog", { name: "创建项目" });
  expect(mocks.pick).not.toHaveBeenCalled();
  fireEvent.change(within(dialog).getByRole("textbox", { name: "项目名称" }), { target: { value: "Research folders" } });
  for (const folder of ["C:/A", "C:/B"]) {
    fireEvent.click(within(dialog).getByRole("button", { name: "添加此电脑上的文件夹" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "添加此电脑上的文件夹" }));
    await within(dialog).findByText(folder);
  }
  fireEvent.click(within(dialog).getByRole("button", { name: "创建", exact: true }));
  await waitFor(() => expect(screen.queryByRole("dialog", { name: "创建项目" })).toBeNull());
  expect(JSON.parse(mocks.fetch.mock.calls[0][1].body)).toEqual({ name: "Research folders", source_folders: ["C:/A", "C:/B"], appearance: null });
  expect(useAppStore.getState().recentWorkspaces).toMatchObject([{ name: "Research folders", sourceFolders: ["C:/A", "C:/B"] }]);
  expect(mocks.createChat).not.toHaveBeenCalled();
  expect(useAppStore.getState()).toMatchObject({ workingDirectory: "C:/original", draft: "keep unsaved" });
});

it("opens project creation over the current chat when sidebar navigation closes the catalog", () => {
  useAppStore.setState({ projectsOpen: false, projectEditTarget: null, conversationId: "current-chat", draft: "keep unsaved",
    editorTabs: [{ id: "editing", path: "draft.ts", content: "unsaved editor", original: "disk", loading: false }] });
  render(<><ConversationsTab conversationId="current-chat" onNavigate={() => useAppStore.setState({ projectsOpen: false })} onSetConfirmDialog={vi.fn()} /><ProjectDialogHost /></>);
  fireEvent.click(screen.getByRole("button", { name: "添加项目" }));
  const dialog = screen.getByRole("dialog", { name: "创建项目" });
  expect(useAppStore.getState()).toMatchObject({ projectsOpen: false, projectEditTarget: "create", conversationId: "current-chat", workingDirectory: "C:/original", draft: "keep unsaved" });
  expect(useAppStore.getState().editorTabs[0].content).toBe("unsaved editor");
  fireEvent.click(within(dialog).getByRole("button", { name: "取消" }));
  expect(useAppStore.getState().projectEditTarget).toBeNull();
  expect(mocks.fetch).not.toHaveBeenCalled();
  expect(useAppStore.getState().draft).toBe("keep unsaved");
});

it("persists project pin and marker changes and the sidebar consumes the named metadata", async () => {
  projects = [{ path: "C:/A", name: "My named project", project_type: "project", last_opened: 1, source_folders: ["C:/A", "C:/B"], pinned: true, named: true }];
  useAppStore.setState({ recentWorkspaces: [{ path: "C:/A", name: "My named project", projectType: "project", lastOpened: 1, sourceFolders: ["C:/A", "C:/B"], pinned: true, named: true }] });
  const page = render(<><ProjectsPage /><ProjectDialogHost /></>);
  fireEvent.click(screen.getByRole("button", { name: "更改 My named project 的标记" }));
  fireEvent.click(screen.getByRole("button", { name: "book", exact: true }));
  await waitFor(() => expect(useAppStore.getState().recentWorkspaces[0].appearance).toEqual({ color: "black", marker: { kind: "icon", icon: "book" } }));
  page.unmount();
  const sidebar = render(<ConversationsTab conversationId="" onSetConfirmDialog={vi.fn()} />);
  expect(screen.getByRole("button", { name: "My named project", exact: true })).toBeTruthy();
  expect(sidebar.container.querySelector(".lucide-book")).toBeTruthy();
  sidebar.unmount(); render(<><ProjectsPage /><ProjectDialogHost /></>);
  fireEvent.click(screen.getByRole("button", { name: "取消置顶项目" }));
  await waitFor(() => expect(useAppStore.getState().recentWorkspaces[0].pinned).toBe(false));
  cleanup(); render(<ConversationsTab conversationId="" onSetConfirmDialog={vi.fn()} />);
  expect(screen.queryByRole("button", { name: "My named project", exact: true })).toBeNull();
});

it("keeps the modal and entered folders when persistence is rejected", async () => {
  useAppStore.setState({ projectEditTarget: "create" });
  mocks.pick.mockResolvedValue("C:/A");
  mocks.fetch.mockResolvedValueOnce(new Response("Source folder is not trusted", { status: 403 }));
  render(<><ProjectsPage /><ProjectDialogHost /></>);
  const dialog = await screen.findByRole("dialog", { name: "创建项目" });
  fireEvent.change(within(dialog).getByRole("textbox", { name: "项目名称" }), { target: { value: "Unfinished project" } });
  fireEvent.click(within(dialog).getByRole("button", { name: "添加此电脑上的文件夹" }));
  fireEvent.click(screen.getByRole("menuitem", { name: "添加此电脑上的文件夹" }));
  await within(dialog).findByText("C:/A");
  fireEvent.click(within(dialog).getByRole("button", { name: "创建", exact: true }));
  await within(dialog).findByRole("alert");
  expect(within(dialog).getByRole("textbox", { name: "项目名称" })).toHaveProperty("value", "Unfinished project");
  expect(within(dialog).getByText("C:/A")).toBeTruthy();
  expect(useAppStore.getState().recentWorkspaces).toEqual([]);
});

it("selects a real emoji from the Source catalogue and saves it as the project marker", async () => {
  projects = [{ path: "C:/A", name: "Emoji project", project_type: "project", last_opened: 1, source_folders: ["C:/A"], pinned: true, named: true }];
  useAppStore.setState({ recentWorkspaces: [{ path: "C:/A", name: "Emoji project", projectType: "project", lastOpened: 1, sourceFolders: ["C:/A"], named: true }] });
  render(<><ProjectsPage /><ProjectDialogHost /></>);
  fireEvent.click(screen.getByRole("button", { name: "更改 Emoji project 的标记" }));
  const marker = screen.getByRole("dialog", { name: "项目标记" });
  fireEvent.click(within(marker).getByRole("button", { name: "表情符号" }));
  fireEvent.change(within(marker).getByRole("textbox", { name: "搜索标记" }), { target: { value: "grinning face" } });
  fireEvent.click(await within(marker).findByRole("button", { name: "face grin grinning face", exact: true }));
  await waitFor(() => expect(useAppStore.getState().recentWorkspaces[0].appearance).toEqual({ color: "black", marker: { kind: "emoji", emoji: "😀" } }));
  expect(useAppStore.getState().draft).toBe("keep unsaved");
});
