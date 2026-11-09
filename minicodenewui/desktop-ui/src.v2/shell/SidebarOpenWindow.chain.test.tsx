// @vitest-environment jsdom
import { loadConversationMenu } from "./ConversationMenu";

// These action suites use a ready interaction module; cold ownership is covered separately.
beforeAll(async () => { await loadConversationMenu(); });
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeAll, afterEach, beforeEach, expect, it, vi } from "vitest";
import { ConversationMenu } from "./sidebarComponents";
import { SessionRow } from "./SessionRow";
import { useAppStore } from "../stores";

const native = vi.hoisted(() => ({ preferences: vi.fn(), openPath: vi.fn(), openWindow: vi.fn(), git: vi.fn(), platform: "win32" }));
vi.mock("../protocol/workspace", async load => ({ ...await load<typeof import("../protocol/workspace")>(), fetchWorkspaceGitStatus: native.git }));
vi.mock("../desktop/runtime", async load => ({ ...await load<typeof import("../desktop/runtime")>(),
  isDesktop: () => true, getDesktopPreferenceTargets: native.preferences,
  desktop: () => ({ openPreferredPath: native.openPath, windowControls: { openConversation: native.openWindow }, platformInfo: { platform: native.platform } }),
}));
const targets = [{ id: "vscode", label: "VS Code", kind: "editor", icon: "data:image/png;base64,native" },
  { id: "terminal", label: "Terminal", kind: "terminal" }, { id: "fileManager", label: "File Explorer", kind: "fileManager" },
  { id: "minicode", label: "MiniCode", kind: "editor", hidden: true }];
let anchor: HTMLButtonElement;
const menuProps = () => ({ anchor, menuId: "sidebar-source", conversationId: "background-original", workspaceRoot: "C:/execution/worktree",
  archived: false, isIsolated: true, canCopy: true, canHandoff: true, onRename: vi.fn(), onCopy: vi.fn(), onCopyLink: vi.fn(),
  onCopyMarkdown: vi.fn(), onHandoff: vi.fn(), onArchive: vi.fn(), onClose: vi.fn(), onPin: vi.fn(), onReadState: vi.fn(), onProjectChange: vi.fn() });
const openSubmenu = (item: HTMLElement) => { item.focus(); fireEvent.keyDown(item, { key: "ArrowRight" }); };

beforeEach(() => {
  vi.clearAllMocks();
  native.platform = "win32"; native.git.mockResolvedValue({ is_git_repo: true });
  vi.stubGlobal("ResizeObserver", class { observe() {} disconnect() {} });
  native.preferences.mockResolvedValue({ fileOpenTargets: targets }); native.openPath.mockResolvedValue({ openInEditor: false });
  native.openWindow.mockResolvedValue({ windowOwnerId: "secondary-1", windowInstanceId: "native-created" });
  anchor = document.createElement("button"); document.body.append(anchor);
  useAppStore.setState({ appMode: "code", conversationId: "front-reader", workingDirectory: "C:/front", draft: "reader-unsent", attachments: [], messages: [] });
});
afterEach(() => { cleanup(); anchor.remove(); vi.unstubAllGlobals(); });

it("opens the clicked background execution root through the shared real target consumer without moving the reader", async () => {
  render(<ConversationMenu {...menuProps()} />);
  openSubmenu(await screen.findByRole("menuitem", { name: "打开方式" }));
  await screen.findByRole("menuitem", { name: "VS Code" });
  expect(screen.queryByRole("menuitem", { name: "MiniCode" })).toBeNull();
  fireEvent.click(screen.getByRole("menuitem", { name: "VS Code" }));
  await waitFor(() => expect(native.openPath).toHaveBeenCalledWith("C:/execution/worktree", undefined, {
    workspaceRoot: "C:/execution/worktree", targetId: "vscode", persistPreferredTargetPath: "C:/execution/worktree",
  }));
  expect(useAppStore.getState()).toMatchObject({ conversationId: "front-reader", workingDirectory: "C:/front", draft: "reader-unsent" });
});

it("uses the same Source Work-mode target filter in the sidebar", async () => {
  useAppStore.setState({ appMode: "cowork" }); render(<ConversationMenu {...menuProps()} />);
  openSubmenu(await screen.findByRole("menuitem", { name: "打开方式" }));
  await screen.findByRole("menuitem", { name: "File Explorer" });
  expect(screen.queryByRole("menuitem", { name: "VS Code" })).toBeNull(); expect(screen.queryByRole("menuitem", { name: "Terminal" })).toBeNull();
});

it("requests a new native window for the background owner without create, switch or send actions", async () => {
  const state = useAppStore.getState(); render(<ConversationMenu {...menuProps()} />);
  fireEvent.click(screen.getByRole("menuitem", { name: "在新窗口中打开" }));
  await waitFor(() => expect(native.openWindow).toHaveBeenCalledWith("background-original"));
  expect(useAppStore.getState()).toMatchObject({ conversationId: state.conversationId, workingDirectory: state.workingDirectory, draft: state.draft });
  expect(useAppStore.getState().attachments).toBe(state.attachments);
  expect(useAppStore.getState().messages).toBe(state.messages);
});

it("does not expose native workspace Open in for a remote owner", () => {
  render(<ConversationMenu {...menuProps()} environment="ssh" />);
  expect(screen.queryByRole("menuitem", { name: "打开方式" })).toBeNull(); expect(native.preferences).not.toHaveBeenCalled();
});

it("uses actual repository origin even when an ordinary local thread has no worktree or branch metadata", async () => {
  render(<ConversationMenu {...menuProps()} canHandoff={false} />);
  expect(await screen.findByRole("menuitem", { name: "打开方式" })).toBeTruthy();
  expect(native.git).toHaveBeenCalledWith("C:/execution/worktree");
});

it("does not treat worktree-shaped thread metadata as a repository when the actual origin query says false", async () => {
  native.git.mockResolvedValue({ is_git_repo: false }); render(<ConversationMenu {...menuProps()} />);
  await waitFor(() => expect(native.git).toHaveBeenCalled());
  expect(screen.queryByRole("menuitem", { name: "打开方式" })).toBeNull(); expect(native.preferences).not.toHaveBeenCalled();
});

it.each([{ archived: true }, { conversationType: "side_chat" as const }, { environment: "remote" as const }])("does not expose an unsupported new-window owner %j", props => {
  render(<ConversationMenu {...menuProps()} {...props} />); expect(screen.queryByRole("menuitem", { name: "在新窗口中打开" })).toBeNull();
});

it.each(["win32", "darwin"])("routes only the Source platform modifier on %s from the actual row to a native window", platform => {
  native.platform = platform;
  const onSwitch = vi.fn(); const callbacks = { onSwitch, onSetMenuFor: vi.fn(), onStartRename: vi.fn(), onCommitRename: vi.fn(),
    onCancelRename: vi.fn(), onSetRenameValue: vi.fn(), onArchive: vi.fn(), onCopyLink: vi.fn(), onCopyMarkdown: vi.fn(), onHandoff: vi.fn(),
    onCopy: vi.fn(), onPin: vi.fn(), onReadState: vi.fn(), onProjectChange: vi.fn(), currentProject: "", projectOptions: [] };
  render(<SessionRow conversation={{ id: "row-task", title: "Actual row", updatedAt: "today" }} sessionStatus="running" active={false}
    menuOpen={false} renaming={false} renameValue="" waitingLabel={null} {...callbacks} />);
  const row = screen.getByRole("button", { name: "Actual row" });
  fireEvent.click(row, { ctrlKey: true }); fireEvent.click(row, { metaKey: true }); fireEvent.click(row);
  expect(native.openWindow.mock.calls).toEqual([["row-task"]]); expect(onSwitch.mock.calls).toEqual([["row-task", "row-task"], ["row-task", "row-task"]]);
});
