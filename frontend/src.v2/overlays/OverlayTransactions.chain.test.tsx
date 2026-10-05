// @vitest-environment jsdom
import { StrictMode } from "react";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAppStore } from "../stores";
import { SchedulerTab } from "./SchedulerTab";
import { AdvancedTab } from "./AdvancedTab";
import { ArchivedTab } from "./ArchivedTab";
import { GeneralTab } from "./GeneralTab";
import { WorkspaceGitTab } from "./WorkspaceGitTab";
import { openAutomations } from "../lib/automations-navigation";

const mocks = vi.hoisted(() => ({ command: vi.fn(), deleteConversation: vi.fn(), confirm: vi.fn(), toast: vi.fn(), detect: vi.fn(),
  getStatus: vi.fn(), onStatus: vi.fn(), status: vi.fn(), worktree: vi.fn(), diff: vi.fn() }));
vi.mock("../protocol/ws-outbox", () => ({ sendClientCommand: vi.fn(), sendClientCommandAwaitResult: mocks.command,
  sendConversationDeleteCommand: mocks.deleteConversation,
  commandResultSucceeded: (event: { level: string }) => event.level !== "error" && event.level !== "failed" }));
vi.mock("./DialogService", () => ({ showConfirm: mocks.confirm, showAlert: vi.fn() }));
vi.mock("./ToastContainer", () => ({ pushToast: mocks.toast }));
vi.mock("./GithubConnection", () => ({ GithubConnection: () => null }));
vi.mock("../desktop/runtime", () => ({ isDesktop: () => true, envDetect: mocks.detect, exportDiagnostics: vi.fn(),
  desktop: () => ({ platformInfo: { isDesktop: true, platform: "linux", arch: "x64" }, updates: { getStatus: mocks.getStatus, onStatus: mocks.onStatus } }) }));
vi.mock("../protocol/workspace", () => ({ fetchWorkspaceGitStatus: mocks.status, fetchWorkspaceGitWorktree: mocks.worktree,
  fetchWorkspaceGitDiff: mocks.diff, removeWorkspaceGitWorktree: vi.fn() }));
const deferred = <T,>() => {
  let resolve!: (value: T) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};
const receipt = (command: string, level = "info", message = "") => ({ type: "command.result", command, level, message, data: {} });
const initial = useAppStore.getState();
const task = { id: "task-a", name: "审计", prompt: "source", schedule: "0 * * * *", permission_mode: "auto", enabled: true };
const fillTask = () => {
  fireEvent.change(screen.getByPlaceholderText("任务名称"), { target: { value: "审计" } });
  fireEvent.change(screen.getByPlaceholderText("要运行的提示词"), { target: { value: "原草稿" } });
};
const fillEnv = () => {
  fireEvent.change(screen.getByPlaceholderText("变量名"), { target: { value: "TOKEN" } });
  fireEvent.change(screen.getByPlaceholderText("变量值"), { target: { value: "old-secret" } });
  fireEvent.change(screen.getByPlaceholderText("说明（可选）"), { target: { value: "old-description" } });
};
beforeEach(() => {
  vi.resetAllMocks();
  window.matchMedia = vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() });
  mocks.command.mockImplementation(async (command: { type: string }) => receipt(command.type));
  mocks.confirm.mockResolvedValue(true);
  mocks.deleteConversation.mockResolvedValue(true);
  mocks.detect.mockResolvedValue({ git: true, python: true, node: true });
  mocks.getStatus.mockResolvedValue({ status: "idle", sequence: 0 });
  mocks.onStatus.mockReturnValue(vi.fn());
  mocks.status.mockResolvedValue({ branch: "main", modified: ["main.ts"], staged: [], untracked: [] });
  mocks.worktree.mockResolvedValue({ current_path: "C:/A", worktrees: [] });
  mocks.diff.mockResolvedValue({ diff: "PATCH" });
  useAppStore.setState({ ...initial, conversationId: "A", workingDirectory: "C:/A", isConnected: true,
    scheduledTasks: [], scheduledTaskRuns: [], envVars: [], conversations: [], workspaceGit: null,
    editorTabs: [], settingsOpen: true, settingsTab: "general" }, true);
});
afterEach(() => cleanup());

describe("overlay transaction chains", () => {
  it("reports a failed scoped schedule read and retries without projecting an empty success", async () => {
    mocks.command.mockResolvedValueOnce(receipt("scheduler.list", "error", "list denied"));
    render(<SchedulerTab />);
    expect(await screen.findByRole("alert")).toHaveProperty("textContent", expect.stringContaining("list denied"));
    expect(screen.queryByText("暂无定时任务")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    expect(await screen.findByText("暂无定时任务")).toBeTruthy();
    expect(mocks.command).toHaveBeenLastCalledWith({ type: "scheduler.list", owner_conversation_id: "A", workspace_root: "C:/A" }, "scheduler.list");
  });
  it("automatically rereads schedules after backend reconnection", async () => {
    useAppStore.setState({ isConnected: false });
    render(<SchedulerTab />);
    expect(mocks.command).not.toHaveBeenCalled();
    act(() => useAppStore.setState({ isConnected: true }));
    expect(await screen.findByText("暂无定时任务")).toBeTruthy();
    expect(mocks.command).toHaveBeenCalledTimes(1);
  });
  it("clears the old project schedules and fences a late catalog read", async () => {
    const first = deferred<ReturnType<typeof receipt>>();
    mocks.command.mockReturnValueOnce(first.promise);
    useAppStore.setState({ scheduledTasks: [task], scheduledTaskRuns: [{ id: "run-a", task_id: task.id, scheduled_at: "now", status: "completed" }] });
    render(<SchedulerTab />);
    act(() => useAppStore.getState().setWorkingDirectory("C:/B"));
    expect(useAppStore.getState().scheduledTasks).toEqual([]);
    expect(useAppStore.getState().scheduledTaskRuns).toEqual([]);
    expect(await screen.findByText("暂无定时任务")).toBeTruthy();
    await act(async () => first.resolve(receipt("scheduler.list", "error", "stale failure")));
    expect(screen.queryByRole("alert")).toBeNull();
    expect(mocks.command).toHaveBeenLastCalledWith({ type: "scheduler.list", owner_conversation_id: "A", workspace_root: "C:/B" }, "scheduler.list");
  });
  it.each(["prompt", "timezone"])("preserves the entire new schedule draft after changing %s while adding", async (field) => {
    const saved = deferred<ReturnType<typeof receipt>>();
    mocks.command.mockImplementation((command: { type: string }) => command.type === "scheduler.add" ? saved.promise : Promise.resolve(receipt(command.type)));
    render(<SchedulerTab />);
    fillTask();
    fireEvent.click(screen.getByRole("button", { name: "添加" }));
    if (field === "prompt") fireEvent.change(screen.getByPlaceholderText("要运行的提示词"), { target: { value: "下一份草稿" } });
    if (field === "timezone") fireEvent.change(screen.getByLabelText("时区"), { target: { value: "America/New_York" } });
    await act(async () => saved.resolve(receipt("scheduler.add")));
    expect((screen.getByPlaceholderText("任务名称") as HTMLInputElement).value).toBe("审计");
    expect((screen.getByPlaceholderText("要运行的提示词") as HTMLTextAreaElement).value).toBe(field === "prompt" ? "下一份草稿" : "原草稿");
  });
  it("keeps each owner's schedule draft when an old owner's add receipt arrives", async () => {
    const saved = deferred<ReturnType<typeof receipt>>();
    mocks.command.mockImplementation((command: { type: string }) => command.type === "scheduler.add" ? saved.promise : Promise.resolve(receipt(command.type)));
    render(<SchedulerTab />);
    fillTask();
    fireEvent.click(screen.getByRole("button", { name: "添加" }));
    act(() => useAppStore.setState({ conversationId: "B" }));
    expect((screen.getByPlaceholderText("任务名称") as HTMLInputElement).value).toBe("");
    fireEvent.change(screen.getByPlaceholderText("任务名称"), { target: { value: "B 的草稿" } });
    await act(async () => saved.resolve(receipt("scheduler.add")));
    expect((screen.getByPlaceholderText("任务名称") as HTMLInputElement).value).toBe("B 的草稿");
    act(() => useAppStore.setState({ conversationId: "A" }));
    expect((screen.getByPlaceholderText("任务名称") as HTMLInputElement).value).toBe("审计");
    expect((screen.getByPlaceholderText("要运行的提示词") as HTMLTextAreaElement).value).toBe("原草稿");
  });
  it.each(["cancel", "switch", "approve"])("keeps schedule actions mutually exclusive through %s confirmation", async (mode) => {
    const confirmed = deferred<boolean>();
    mocks.confirm.mockReturnValue(confirmed.promise);
    useAppStore.setState({ scheduledTasks: [task] });
    render(<SchedulerTab />);
    fireEvent.click(screen.getByRole("button", { name: "删除 审计" }));
    expect((screen.getByRole("button", { name: "立即运行 审计" }) as HTMLButtonElement).disabled).toBe(true);
    if (mode === "switch") act(() => useAppStore.setState({ conversationId: "B" }));
    await act(async () => confirmed.resolve(mode !== "cancel"));
    const removals = mocks.command.mock.calls.filter(([command]) => command.type === "scheduler.remove");
    expect(removals).toHaveLength(mode === "approve" ? 1 : 0);
    expect((screen.getByRole("button", { name: "立即运行 审计" }) as HTMLButtonElement).disabled).toBe(false);
  });
  it("keeps a projectless task's create action disabled", () => {
    useAppStore.setState({ workingDirectory: "" });
    render(<SchedulerTab />);
    fillTask();
    expect((screen.getByRole("button", { name: "添加" }) as HTMLButtonElement).disabled).toBe(true);
    expect(mocks.command).toHaveBeenCalledWith({ type: "scheduler.list", owner_conversation_id: "A", workspace_root: "" }, "scheduler.list");
  });
  it("reports an env read failure and retries the real global read", async () => {
    mocks.command.mockResolvedValueOnce(receipt("env.list", "error", "vault read failed"));
    render(<AdvancedTab />);
    expect(await screen.findByRole("alert")).toHaveProperty("textContent", expect.stringContaining("vault read failed"));
    expect(screen.queryByText("尚未配置环境变量。")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    expect(await screen.findByText("尚未配置环境变量。")).toBeTruthy();
    expect(mocks.command).toHaveBeenLastCalledWith({ type: "env.list" }, "env.list");
  });
  it.each(["变量名", "变量值", "说明（可选）"])("preserves the entire env draft when %s changes while saving", async (field) => {
    const saved = deferred<ReturnType<typeof receipt>>();
    mocks.command.mockImplementation((command: { type: string }) => command.type === "env.set" ? saved.promise : Promise.resolve(receipt(command.type)));
    render(<AdvancedTab />);
    fillEnv();
    fireEvent.click(screen.getByRole("button", { name: "添加" }));
    fireEvent.change(screen.getByPlaceholderText(field), { target: { value: field === "变量名" ? "NEXT_TOKEN" : "next-draft" } });
    await act(async () => saved.resolve(receipt("env.set")));
    expect((screen.getByPlaceholderText("变量名") as HTMLInputElement).value).toBe(field === "变量名" ? "NEXT_TOKEN" : "TOKEN");
    expect((screen.getByPlaceholderText("变量值") as HTMLInputElement).value).toBe(field === "变量值" ? "next-draft" : "old-secret");
    expect((screen.getByPlaceholderText("说明（可选）") as HTMLInputElement).value).toBe(field === "说明（可选）" ? "next-draft" : "old-description");
  });
  it("blocks same-variable delete while saving and releases it on receipt", async () => {
    const saved = deferred<ReturnType<typeof receipt>>();
    mocks.command.mockImplementation((command: { type: string }) => command.type === "env.set" ? saved.promise : Promise.resolve(receipt(command.type)));
    useAppStore.setState({ envVars: [{ name: "TOKEN", description: "", scope: "global" }] });
    render(<AdvancedTab />);
    fillEnv();
    fireEvent.click(screen.getByRole("button", { name: "重新保存" }));
    expect((screen.getByRole("button", { name: "删除环境变量 TOKEN" }) as HTMLButtonElement).disabled).toBe(true);
    await act(async () => saved.resolve(receipt("env.set")));
    expect((screen.getByRole("button", { name: "删除环境变量 TOKEN" }) as HTMLButtonElement).disabled).toBe(false);
  });
  it("blocks same-variable save during delete confirmation and releases a cancellation", async () => {
    const confirmed = deferred<boolean>();
    mocks.confirm.mockReturnValue(confirmed.promise);
    useAppStore.setState({ envVars: [{ name: "TOKEN", description: "", scope: "global" }] });
    render(<AdvancedTab />);
    fillEnv();
    fireEvent.click(screen.getByRole("button", { name: "删除环境变量 TOKEN" }));
    expect((screen.getByRole("button", { name: "重新保存" }) as HTMLButtonElement).disabled).toBe(true);
    await act(async () => confirmed.resolve(false));
    expect((screen.getByRole("button", { name: "重新保存" }) as HTMLButtonElement).disabled).toBe(false);
    expect(mocks.command.mock.calls.some(([command]) => command.type === "env.delete")).toBe(false);
  });
  it("shows missing OS credentials and repairs them through the existing editor without leaking a previous value or changing scope", async () => {
    useAppStore.setState({ envVars: [{ name: "SEARCH_KEY", description: "Search service", scope: "mcp:search", credential_status: "missing" }] });
    render(<AdvancedTab />);
    expect(screen.getByText("未存储／已失效 · 不会注入命令")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "重新保存环境变量 SEARCH_KEY" }));
    expect((screen.getByPlaceholderText("变量名") as HTMLInputElement).value).toBe("SEARCH_KEY");
    expect((screen.getByPlaceholderText("说明（可选）") as HTMLInputElement).value).toBe("Search service");
    expect((screen.getByPlaceholderText("变量值") as HTMLInputElement).value).toBe("");
    fireEvent.change(screen.getByPlaceholderText("变量值"), { target: { value: "replacement-test-value" } });
    fireEvent.click(screen.getByRole("button", { name: "重新保存" }));
    await waitFor(() => expect(mocks.command).toHaveBeenCalledWith({ type: "env.set", name: "SEARCH_KEY", value: "replacement-test-value", description: "Search service", scope: "mcp:search" }, "env.set"));
    act(() => useAppStore.setState({ envVars: [{ name: "SEARCH_KEY", description: "Search service", scope: "mcp:search", credential_status: "stored" }] }));
    expect(screen.getByText("已存储")).toBeTruthy();
    expect(screen.queryByText("未存储／已失效 · 不会注入命令")).toBeNull();
  });
  it("allows removing an indexed variable whose OS value is already missing", async () => {
    useAppStore.setState({ envVars: [{ name: "SEARCH_KEY", description: "", scope: "global", credential_status: "missing" }] });
    render(<AdvancedTab />);
    fireEvent.click(screen.getByRole("button", { name: "删除环境变量 SEARCH_KEY" }));
    await waitFor(() => expect(mocks.command).toHaveBeenCalledWith({ type: "env.delete", name: "SEARCH_KEY" }, "env.delete"));
    expect(mocks.confirm).toHaveBeenCalled();
  });
  it("does not let a superseded StrictMode detector failure erase current environment", async () => {
    const old = deferred<{ git: boolean }>();
    mocks.detect.mockReturnValueOnce(old.promise).mockResolvedValue({ git: true, python: true, node: true });
    render(<StrictMode><AdvancedTab /></StrictMode>);
    await waitFor(() => expect(screen.getAllByText("可用")).toHaveLength(3));
    await act(async () => old.reject(new Error("obsolete detector failure")));
    expect(screen.getAllByText("可用")).toHaveLength(3);
    expect(screen.queryByText(/obsolete detector failure/)).toBeNull();
  });
  it.each([false, true])("blocks restore through archive delete confirmation (%s)", async (approved) => {
    const confirmed = deferred<boolean>();
    const removed = deferred<boolean>();
    mocks.confirm.mockReturnValue(confirmed.promise);
    mocks.deleteConversation.mockReturnValue(removed.promise);
    useAppStore.setState({ conversations: [{ id: "old", title: "归档", archived: true, updatedAt: "now", gitIsolated: true }] });
    render(<ArchivedTab />);
    fireEvent.click(screen.getByRole("button", { name: "删除 归档" }));
    expect((screen.getByRole("button", { name: "恢复 归档" }) as HTMLButtonElement).disabled).toBe(true);
    await act(async () => confirmed.resolve(approved));
    if (approved) {
      expect((screen.getByRole("button", { name: "恢复 归档" }) as HTMLButtonElement).disabled).toBe(true);
      expect(mocks.deleteConversation).toHaveBeenCalledWith({ type: "conversation.delete", conversation_id: "old", cleanup_worktree: true });
      await act(async () => removed.resolve(false));
      expect(mocks.toast).not.toHaveBeenCalled();
    } else expect(mocks.deleteConversation).not.toHaveBeenCalled();
    expect((screen.getByRole("button", { name: "恢复 归档" }) as HTMLButtonElement).disabled).toBe(false);
    expect(screen.getByText("归档")).toBeTruthy();
  });
  it("retains a newer update event when the initial status request later fails", async () => {
    const old = deferred<{ status: string }>();
    mocks.getStatus.mockReturnValue(old.promise);
    render(<GeneralTab remoteImagePolicy="ask" setRemoteImagePolicy={() => {}} />);
    const statusCallback = mocks.onStatus.mock.calls[0][0];
    act(() => statusCallback({ status: "ready", sequence: 4, version: "2.0.0" }));
    await act(async () => old.reject(new Error("old read failed")));
    expect(screen.getByRole("button", { name: "重启并安装" })).toBeTruthy();
    expect(screen.queryByText(/old read failed/)).toBeNull();
  });
  it("opens the Git file through the editor store and leaves settings", async () => {
    const openEditorFile = vi.fn();
    useAppStore.setState({ openEditorFile });
    render(<WorkspaceGitTab />);
    fireEvent.click(await screen.findByRole("button", { name: "已修改 main.ts" }));
    fireEvent.click(screen.getByRole("button", { name: "在编辑器中打开文件" }));
    expect(openEditorFile).toHaveBeenCalledWith("main.ts", "main.ts", { exact: true });
    expect(useAppStore.getState().settingsOpen).toBe(false);
  });
  it("opens automations through the actual settings scheduler destination", () => {
    useAppStore.setState({ settingsOpen: false });
    openAutomations();
    expect(useAppStore.getState()).toMatchObject({ settingsOpen: true, settingsTab: "scheduler" });
    expect("toggleAutomations" in useAppStore.getState()).toBe(false);
  });
});
