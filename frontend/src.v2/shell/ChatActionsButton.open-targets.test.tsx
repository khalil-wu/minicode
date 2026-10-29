// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { useAppStore } from "../stores";
import { ChatActionsButton } from "./ChatActionsButton";

const mocks = vi.hoisted(() => ({ preferences: vi.fn(), open: vi.fn(), git: vi.fn() }));
vi.mock("../protocol/workspace", async load => ({ ...await load<typeof import("../protocol/workspace")>(), fetchWorkspaceGitStatus: mocks.git }));
vi.mock("../lib/automations-navigation", async load => ({ ...await load<typeof import("../lib/automations-navigation")>(), loadThreadAutomations: async () => [] }));
vi.hoisted(() => Object.defineProperty(globalThis, "matchMedia", { configurable: true,
  value: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }),
}));
vi.mock("../desktop/runtime", async load => ({ ...await load<typeof import("../desktop/runtime")>(),
  isDesktop: () => true, getDesktopPreferenceTargets: mocks.preferences, desktop: () => ({ openPreferredPath: mocks.open }),
}));

const targets = [
  { id: "minicode", label: "MiniCode", kind: "editor", hidden: true },
  { id: "system", label: "Default app", kind: "systemDefault", hidden: true },
  { id: "vscode", label: "VS Code", kind: "editor", icon: "data:image/png;base64,editor" },
  { id: "terminal", label: "Terminal", kind: "terminal", icon: "apps/microsoft-terminal.png" },
  { id: "fileManager", label: "File Explorer", kind: "fileManager", icon: "apps/file-explorer.png" },
];

const openSubmenu = (item: HTMLElement) => { item.focus(); fireEvent.keyDown(item, { key: "ArrowRight" }); };

beforeEach(() => {
  vi.resetAllMocks();
  vi.stubGlobal("ResizeObserver", class { observe() {} disconnect() {} });
  mocks.preferences.mockResolvedValue({ fileOpenTargets: targets });
  mocks.git.mockResolvedValue({ is_git_repo: true });
  mocks.open.mockResolvedValue({ openInEditor: false });
  useAppStore.setState({ conversationId: "A", pendingConversationSwitchId: null, appMode: "code", workingDirectory: "C:/project", sideChatOpen: false,
    conversations: [{ id: "A", title: "A", workspaceRoot: "C:/project", executionCwd: "C:/worktrees/A", gitBranch: "main", updatedAt: "today" }],
    messages: [{ id: "message-A", role: "user", content: "Preserve my draft and workspace", timestamp: 1 }],
  });
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

async function openTargets() {
  fireEvent.click(screen.getByRole("button", { name: "聊天操作" }));
  openSubmenu(await screen.findByRole("menuitem", { name: "打开方式" }));
  await screen.findByRole("menuitem", { name: "File Explorer" });
}

it("produces source Open in targets with their real icons and opens the execution worktree with workspace persistence", async () => {
  const messages = useAppStore.getState().messages;
  render(<ChatActionsButton />);
  await openTargets();
  expect(screen.getByRole("menuitem", { name: "VS Code" }).querySelector("img")?.getAttribute("src")).toBe("data:image/png;base64,editor");
  expect(screen.getByRole("menuitem", { name: "Terminal" })).toBeTruthy();
  expect(screen.queryByRole("menuitem", { name: "MiniCode" })).toBeNull();
  expect(screen.queryByRole("menuitem", { name: "Default app" })).toBeNull();
  fireEvent.click(screen.getByRole("menuitem", { name: "VS Code" }));
  await waitFor(() => expect(mocks.open).toHaveBeenCalledWith("C:/worktrees/A", undefined, {
    targetId: "vscode", workspaceRoot: "C:/worktrees/A", persistPreferredTargetPath: "C:/worktrees/A",
  }));
  expect(screen.queryByRole("menu")).toBeNull();
  expect(useAppStore.getState().messages).toBe(messages);
  expect(useAppStore.getState().workingDirectory).toBe("C:/project");
});

it("matches the source Work-mode filter for editor and terminal targets", async () => {
  useAppStore.setState({ appMode: "cowork" });
  render(<ChatActionsButton />);
  await openTargets();
  expect(screen.queryByRole("menuitem", { name: "VS Code" })).toBeNull();
  expect(screen.queryByRole("menuitem", { name: "Terminal" })).toBeNull();
  expect(screen.getByRole("menuitem", { name: "File Explorer" })).toBeTruthy();
});

it("does not attach late application discovery to a different conversation", async () => {
  let release!: (value: { fileOpenTargets: typeof targets }) => void;
  mocks.preferences.mockImplementation(() => new Promise(resolve => { release = resolve; }));
  render(<ChatActionsButton />);
  fireEvent.click(screen.getByRole("button", { name: "聊天操作" }));
  openSubmenu(await screen.findByRole("menuitem", { name: "打开方式" }));
  expect(screen.getByRole("menuitem", { name: "正在加载可用应用…" })).toBeTruthy();
  act(() => useAppStore.setState({ conversationId: "B", conversations: [{ id: "B", title: "B", workspaceRoot: "C:/B", updatedAt: "today" }] }));
  await act(async () => release({ fileOpenTargets: targets }));
  expect(screen.queryByRole("menu")).toBeNull();
  expect(mocks.open).not.toHaveBeenCalled();
});

it("shows discovery failure using the source disabled menu message", async () => {
  mocks.preferences.mockRejectedValue(new Error("Native target discovery failed"));
  render(<ChatActionsButton />);
  fireEvent.click(screen.getByRole("button", { name: "聊天操作" }));
  openSubmenu(await screen.findByRole("menuitem", { name: "打开方式" }));
  const row = await screen.findByRole("menuitem", { name: "无法加载应用。请重新打开菜单以重试" });
  expect((row as HTMLButtonElement).disabled).toBe(true);
  expect(screen.queryByRole("menuitem", { name: "VS Code" })).toBeNull();
  expect(mocks.open).not.toHaveBeenCalled();
});
