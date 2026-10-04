/* @vitest-environment jsdom */

import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAppStore } from "../stores";
import { GitPanel } from "./GitPanel";

const mocks = vi.hoisted(() => ({
  status: vi.fn(),
  worktree: vi.fn(),
  diff: vi.fn(),
  remove: vi.fn(),
  command: vi.fn(),
  confirm: vi.fn(),
  alert: vi.fn(),
  toast: vi.fn(),
}));

vi.mock("../protocol/workspace", () => ({
  fetchWorkspaceGitStatus: mocks.status,
  fetchWorkspaceGitWorktree: mocks.worktree,
  fetchWorkspaceGitDiff: mocks.diff,
  removeWorkspaceGitWorktree: mocks.remove,
}));
vi.mock("../protocol/ws-outbox", () => ({
  sendClientCommandAwaitResult: mocks.command,
  commandResultSucceeded: (event: { level: string }) => event.level === "success",
}));
vi.mock("../overlays/DialogService", () => ({ showConfirm: mocks.confirm, showAlert: mocks.alert }));
vi.mock("../overlays/ToastContainer", () => ({ pushToast: mocks.toast }));

const deferred = <T,>() => {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
};

const root = "C:\\repo-a";
const linked = "C:\\repo-linked";
const status = (files = ["tracked.txt"]) => ({ branch: "main", modified: files, staged: [], untracked: [] });
const worktree = (directory: string) => ({
  current_path: directory,
  worktrees: [
    { path: directory, branch: "main", is_current: true },
    { path: directory === linked ? root : linked, branch: "linked", is_current: false, is_isolated: true, can_remove: true },
  ],
});

describe("GitPanel workspace workflows", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    window.matchMedia = vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() });
    mocks.status.mockResolvedValue(status());
    mocks.worktree.mockImplementation(async (directory: string) => worktree(directory));
    mocks.diff.mockResolvedValue({ diff: "INITIAL_PATCH" });
    mocks.confirm.mockResolvedValue(true);
    useAppStore.setState({
      workingDirectory: root,
      workspaceGit: null,
      activeBottomTab: "git",
      conversationId: "git-conversation",
      conversations: [{ id: "git-conversation", title: "Git task", updatedAt: "2026-09-06T00:00:00Z", workspaceRoot: root }],
      editorTabs: [],
      activeTabPath: null,
      activeEditorPath: null,
      gitReviewRequest: null,
    });
  });

  afterEach(() => cleanup());

  it("retains a hidden overview without fetching and refreshes only after activation", async () => {
    const { rerender } = render(<GitPanel active={false} />);
    expect(mocks.status).not.toHaveBeenCalled();
    expect(mocks.worktree).not.toHaveBeenCalled();
    rerender(<GitPanel active />);
    await screen.findByRole("button", { name: "已修改 tracked.txt" });
    expect(mocks.status).toHaveBeenCalledTimes(1);
    rerender(<GitPanel active={false} />);
    act(() => useAppStore.setState({ activeBottomTab: "budget", conversationId: "another-owner" }));
    expect(mocks.status).toHaveBeenCalledTimes(1);
    rerender(<GitPanel active />);
    await waitFor(() => expect(mocks.status).toHaveBeenCalledTimes(2));
  });

  it("treats ordinary folders as a normal state and detects Git after refresh", async () => {
    mocks.status.mockResolvedValueOnce({ is_git_repo: false, branch: "", modified: [], staged: [], untracked: [] });
    mocks.worktree.mockResolvedValueOnce({ is_git_repo: false, current_path: root, worktrees: [] });
    render(<GitPanel />);
    expect(await screen.findByText("当前文件夹未启用 Git")).toBeTruthy();
    expect(screen.queryByText("工作树干净")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "刷新 Git 状态" }));
    expect(await screen.findByRole("button", { name: "已修改 tracked.txt" })).toBeTruthy();
    expect(screen.queryByText("当前文件夹未启用 Git")).toBeNull();
    expect(mocks.diff).not.toHaveBeenCalled();
  });

  it("routes staged and working changes of the same file into the shared review with their range", async () => {
    mocks.status.mockResolvedValue({ ...status(), staged: ["tracked.txt"] });
    render(<GitPanel />);
    fireEvent.click(await screen.findByRole("button", { name: "已暂存 tracked.txt" }));
    expect(useAppStore.getState().gitReviewRequest).toMatchObject({ path: "tracked.txt", section: "staged", workspaceRoot: root, conversationId: "git-conversation" });
    expect(useAppStore.getState().rightStackTab).toBe("diff");
    const stagedRequest = useAppStore.getState().gitReviewRequest?.id;
    fireEvent.click(screen.getByRole("button", { name: "已修改 tracked.txt" }));
    expect(useAppStore.getState().gitReviewRequest).toMatchObject({ path: "tracked.txt", section: "working", workspaceRoot: root });
    expect(useAppStore.getState().gitReviewRequest?.id).not.toBe(stagedRequest);
    expect(mocks.diff).not.toHaveBeenCalled();
  });

  it("clears the old project's files and ignores its late refresh", async () => {
    const oldStatus = deferred<ReturnType<typeof status>>();
    mocks.status.mockResolvedValueOnce(status(["old-project.txt"])).mockReturnValueOnce(oldStatus.promise).mockResolvedValue(status(["new-project.txt"]));
    render(<GitPanel />);
    await screen.findByRole("button", { name: "已修改 old-project.txt" });
    fireEvent.click(screen.getByRole("button", { name: "刷新 Git 状态" }));
    act(() => useAppStore.getState().setWorkingDirectory("C:\\repo-b"));
    expect(screen.queryByRole("button", { name: "已修改 old-project.txt" })).toBeNull();
    expect(await screen.findByRole("button", { name: "已修改 new-project.txt" })).toBeTruthy();
    await act(async () => { oldStatus.resolve(status(["late-old-project.txt"])); });
    expect(screen.queryByRole("button", { name: "已修改 late-old-project.txt" })).toBeNull();
  });

  it("reports repository request failures and recovers on refresh", async () => {
    mocks.status.mockRejectedValueOnce(new Error("Repository permission denied"));
    render(<GitPanel />);
    expect(await screen.findByText("Repository permission denied")).toBeTruthy();
    expect(screen.queryByText("工作树干净")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "刷新 Git 状态" }));
    expect(await screen.findByRole("button", { name: "已修改 tracked.txt" })).toBeTruthy();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("removes stale repository rows when a later refresh fails", async () => {
    mocks.status.mockResolvedValueOnce(status(["old.txt"])).mockRejectedValueOnce(new Error("Repository went offline"));
    render(<GitPanel />);
    expect(await screen.findByRole("button", { name: "已修改 old.txt" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "刷新 Git 状态" }));
    expect(await screen.findByText("Repository went offline")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "已修改 old.txt" })).toBeNull();
    expect(screen.getByText("Git 状态不可用")).toBeTruthy();
  });

  it("switches through the conversation activation command and waits for acknowledgement", async () => {
    const activation = deferred<{ type: string; command: string; level: string; message: string; data: { workspace_root: string } }>();
    mocks.command.mockReturnValueOnce(activation.promise);
    render(<GitPanel />);
    const buttons = await screen.findAllByRole("button", { name: "切换工作区" });
    fireEvent.click(buttons[1]);

    expect(mocks.command).toHaveBeenCalledWith({ type: "workspace.set", path: linked, permission_mode: expect.any(String) }, "workspace.set");
    expect(useAppStore.getState().workingDirectory).toBe(root);
    expect((screen.getByRole("button", { name: "移除隔离工作区" }) as HTMLButtonElement).disabled).toBe(true);
    await act(async () => {
      useAppStore.setState((state) => ({ conversations: [...state.conversations, { id: "new-worktree", title: "New chat", workspaceRoot: linked, updatedAt: "2026-09-10T00:00:00Z" }] }));
      useAppStore.getState().applyConversationSwitched({ conversationId: "new-worktree" });
      activation.resolve({ type: "command.result", command: "workspace.set", level: "success", message: "Activated", data: { workspace_root: linked } });
    });

    expect(useAppStore.getState().workingDirectory).toBe(linked);
    expect(mocks.status).toHaveBeenLastCalledWith(linked);
  });

  it("leaves the current workspace intact when activation fails", async () => {
    mocks.command.mockResolvedValueOnce({ level: "error", message: "Workspace initialization failed" });
    render(<GitPanel />);
    fireEvent.click((await screen.findAllByRole("button", { name: "切换工作区" }))[1]);

    await waitFor(() => expect(mocks.toast).toHaveBeenCalledWith("Workspace initialization failed", "error", 5000));
    expect(useAppStore.getState().workingDirectory).toBe(root);
    expect((screen.getByRole("button", { name: "移除隔离工作区" }) as HTMLButtonElement).disabled).toBe(false);
  });

  it("shows a rejected removal without losing the worktree", async () => {
    mocks.remove.mockRejectedValueOnce(new Error("Worktree has local changes"));
    render(<GitPanel />);
    fireEvent.click(await screen.findByRole("button", { name: "移除隔离工作区" }));

    await waitFor(() => expect(mocks.alert).toHaveBeenCalledWith({ title: "移除失败", message: "Worktree has local changes" }));
    expect(screen.getByRole("button", { name: "移除隔离工作区" })).toBeTruthy();
    expect(useAppStore.getState().workingDirectory).toBe(root);
  });
});
