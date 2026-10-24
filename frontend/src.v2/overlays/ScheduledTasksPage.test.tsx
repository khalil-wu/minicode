/* @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAppStore } from "../stores";
import type { ScheduledTaskEntry, ScheduledTaskRunEntry } from "../stores/types";
import { closeAutomations, getAutomationsPageState, openAutomations, toggleAutomationsSidebar } from "../lib/automations-navigation";
import { ScheduledTasksPage } from "./ScheduledTasksPage";

const mocks = vi.hoisted(() => ({ send: vi.fn(), toast: vi.fn(), confirm: vi.fn(), preferences: vi.fn() }));
vi.mock("../protocol/ws-outbox", () => ({ sendClientCommandAwaitResult: mocks.send,
  commandResultSucceeded: (event: { level?: string }) => event.level !== "error" && event.level !== "failed" }));
vi.mock("./ToastContainer", () => ({ pushToast: mocks.toast }));
vi.mock("./DialogService", () => ({ showConfirm: mocks.confirm }));
vi.mock("../desktop/runtime", () => ({ getDesktopPreferences: mocks.preferences }));

const task = (id: string, root: string, conversationId: string, enabled = true): ScheduledTaskEntry => ({ id, name: `${id} task`, prompt: `Inspect ${id}`,
  schedule: "RRULE:FREQ=MINUTELY;INTERVAL=2", kind: "heartbeat", conversation_id: conversationId, workspace_root: root,
  timezone: "Asia/Shanghai", permission_mode: "auto", isolation: "workspace", enabled });
const ack = (command: string, data: Record<string, unknown>) => ({ type: "command.result", command, level: "success", message: "", data });
let tasksA: ScheduledTaskEntry[];
let tasksB: ScheduledTaskEntry[];
const runA: ScheduledTaskRunEntry = { id: "run-A", task_id: "A", status: "completed", scheduled_at: "2026-10-24T00:00:00Z", conversation_id: "run-chat-A", workspace_root: "C:/A", result_summary: "A completed result" };
const change = (element: HTMLElement, value: string) => fireEvent.change(element, { target: { value } });
const createDialog = () => screen.getByRole("dialog", { name: "设置定时任务" });
const openCreate = () => {
  fireEvent.click(within(screen.getByRole("main", { name: "定时任务" })).getByRole("button", { name: "新建任务" }));
  return createDialog();
};
async function renderPage() { let view!: ReturnType<typeof render>; await act(async () => { view = render(<ScheduledTasksPage />); }); return view; }

describe("ScheduledTasksPage local navigation and editor chain", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    Object.defineProperty(window, "matchMedia", { configurable: true, value: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }) });
    vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
    vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => window.setTimeout(() => callback(0), 0));
    vi.stubGlobal("cancelAnimationFrame", (handle: number) => window.clearTimeout(handle));
    if (!getAutomationsPageState().sidebarOpen) toggleAutomationsSidebar();
    closeAutomations();
    tasksA = [task("A", "C:/A", "chat-A")]; tasksB = [task("B", "C:/B", "chat-B", false)];
    useAppStore.setState({ conversationId: "chat-A", pendingConversationSwitchId: null, workingDirectory: "C:/A", permissionMode: "confirm", isConnected: true,
      settingsOpen: false, skillsMarketplaceOpen: false, projectsOpen: false, draft: "A main draft", attachments: [{ id: "A-image", name: "draft.png", type: "image/png", size: 1 }],
      scheduledTasks: tasksA, scheduledTaskRuns: [runA],
      conversations: [{ id: "chat-A", title: "Chat A", workspaceRoot: "C:/A", updatedAt: "today" },
        { id: "chat-B", title: "Chat B", workspaceRoot: "C:/B", updatedAt: "today" },
        { id: "remote-chat", title: "Remote", workspaceRoot: "C:/remote", environment: "remote", updatedAt: "today" }],
      recentWorkspaces: [{ path: "C:/A", name: "Project A", projectType: "node", lastOpened: 1 }, { path: "C:/B", name: "Project B", projectType: "node", lastOpened: 2 }],
      currentProvider: "custom", currentModel: "model-one", modelsSource: "configured", availableModels: ["model-one", "model-two"], availableModelLabels: {},
      availableModelOptions: [{ model: "model-one", reasoningEffortLevels: ["low", "high"], defaultReasoningEffort: "low" }, { model: "model-two", reasoningEffortLevels: ["medium", "high"], defaultReasoningEffort: "medium" }],
    });
    mocks.confirm.mockResolvedValue(true);
    mocks.preferences.mockResolvedValue({ defaultTaskDirectory: "D:/Tasks" });
    mocks.send.mockImplementation(async (command: Record<string, unknown>) => {
      if (command.type === "scheduler.list") return ack("scheduler.list", { tasks: [...tasksA, ...tasksB], runs: [runA] });
      if (command.type === "conversation.create") return ack("conversation.create", { conversation_id: command.conversation_id, created: true, activated: false });
      return ack(String(command.type), { task: { ...command, id: "created-task", enabled: true, workspace_root: command.workspace_root } });
    });
    openAutomations();
  });
  afterEach(async () => {
    cleanup();
    if (vi.isFakeTimers()) await vi.runAllTimersAsync();
    vi.useRealTimers(); vi.unstubAllGlobals(); closeAutomations();
  });

  it("opens an independent Scheduled page with a single local aggregate and filters its sidebar without navigating the reader", async () => {
    await renderPage();
    expect(getAutomationsPageState().open).toBe(true);
    expect(useAppStore.getState()).toMatchObject({ settingsOpen: false, conversationId: "chat-A", workingDirectory: "C:/A", draft: "A main draft" });
    expect(screen.getByRole("navigation", { name: "已安排任务文件夹" })).toBeTruthy();
    expect(screen.getAllByRole("button", { name: "新建任务" })).toHaveLength(2);
    expect(mocks.send).toHaveBeenCalledExactlyOnceWith({ type: "scheduler.list", scheduler_scope: "global" }, "scheduler.list");
    expect(within(screen.getByRole("navigation", { name: "已安排任务文件夹" })).getByRole("button", { name: "新建任务", exact: true }).closest(".scheduled-sidebar-heading")).toBeNull();
    expect(document.querySelector('.scheduled-task-group-heading [data-sidebar-source-icon="sort"]')).toBeTruthy();
    expect(screen.getByRole("button", { name: /A task/ })).toBeTruthy(); expect(screen.getByRole("button", { name: /B task/ })).toBeTruthy();
    expect(screen.queryByText("自定义 Cron")).toBeNull(); expect(screen.queryByLabelText("任务权限模式")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "搜索定时任务" })); change(screen.getByRole("textbox", { name: "搜索定时任务" }), "B task");
    expect(screen.queryByRole("button", { name: /A task/ })).toBeNull(); expect(screen.getByRole("button", { name: /B task/ })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "筛选任务" })); fireEvent.click(screen.getByRole("menuitemradio", { name: "进行中" }));
    expect(screen.queryByRole("button", { name: /B task/ })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "筛选任务" })); fireEvent.click(screen.getByRole("menuitemradio", { name: "已暂停" }));
    expect(screen.getByRole("button", { name: /B task/ })).toBeTruthy();
    expect(useAppStore.getState().draft).toBe("A main draft");
  });

  it("reuses local cron saving from the observed create modal after its reader switches to another project", async () => {
    await renderPage(); const dialog = openCreate();
    const description = within(dialog).getByRole("textbox", { name: "说明…" });
    expect(within(dialog).queryByLabelText("任务名称")).toBeNull();
    expect(within(dialog).getByRole("button", { name: "创建" })).toHaveProperty("disabled", true);
    expect(Array.from((within(dialog).getByLabelText("重复计划") as HTMLSelectElement).options).map(option => option.value)).toEqual(["daily", "weekdays", "weekly", "custom"]);
    change(description, "Inspect local task\nRetain all of these instructions"); change(within(dialog).getByLabelText("重复计划"), "custom");
    expect(within(dialog).getByLabelText("自定义重复单位")).toHaveProperty("value", "weekly");
    expect(within(dialog).getByLabelText("自定义重复间隔")).toHaveProperty("value", "1");
    expect(within(dialog).getByRole("group", { name: "重复日期" })).toBeTruthy();
    expect(within(dialog).queryByRole("button", { name: "编辑计划规则" })).toBeNull();
    change(within(dialog).getByLabelText("自定义重复间隔"), "2");
    fireEvent.click(within(dialog).getByRole("button", { name: "高级" }));
    change(within(dialog).getByLabelText("通知"), "failed_runs_only");
    fireEvent.click(within(dialog).getByRole("switch", { name: "每次运行时都开启新聊天" }));
    change(within(dialog).getByLabelText("定时任务模型"), "model-two"); change(within(dialog).getByLabelText("定时任务思考强度"), "high");
    act(() => useAppStore.setState({ conversationId: "chat-B", workingDirectory: "C:/B", draft: "B hidden workspace draft", attachments: [{ id: "B-image", name: "B.png", type: "image/png", size: 2 }], scheduledTasks: tasksB, scheduledTaskRuns: [] }));
    await act(async () => { fireEvent.click(within(dialog).getByRole("button", { name: "创建" })); });
    const [request] = mocks.send.mock.calls.find(([command]) => command.type === "scheduler.add")!;
    expect(request).toMatchObject({ name: "Inspect local task", prompt: "Inspect local task\nRetain all of these instructions", kind: "cron", conversation_id: "",
      owner_conversation_id: "chat-A", workspace_root: "C:/A", provider: "custom", model: "model-two", reasoning_effort: "high", notification_policy: "failed_runs_only" });
    expect(request.schedule).toContain("FREQ=WEEKLY"); expect(request.schedule).toContain("INTERVAL=2");
    expect(useAppStore.getState()).toMatchObject({ conversationId: "chat-B", workingDirectory: "C:/B", draft: "B hidden workspace draft", attachments: [{ id: "B-image", name: "B.png" }] });
    expect(screen.queryByRole("dialog", { name: "设置定时任务" })).toBeNull();
    expect(screen.getByRole("textbox", { name: "提示词" })).toHaveProperty("value", request.prompt);
    expect(mocks.preferences).not.toHaveBeenCalled();
  });

  it("keeps separate A and B creation drafts when cancelled or hidden and never modifies either main composer", async () => {
    const view = await renderPage();
    let dialog = openCreate(); const inputA = within(dialog).getByRole("textbox", { name: "说明…" }); change(inputA, "Unsubmitted A task");
    fireEvent.click(within(dialog).getByRole("button", { name: "取消" }));
    act(() => useAppStore.setState({ conversationId: "chat-B", workingDirectory: "C:/B", draft: "B main draft", scheduledTasks: tasksB, scheduledTaskRuns: [] }));
    dialog = openCreate(); change(within(dialog).getByRole("textbox", { name: "说明…" }), "Unsubmitted B task");
    fireEvent.click(within(dialog).getByRole("button", { name: "关闭对话框" }));
    act(() => useAppStore.setState({ conversationId: "chat-A", workingDirectory: "C:/A", draft: "A main draft", scheduledTasks: tasksA, scheduledTaskRuns: [runA] }));
    dialog = openCreate();
    expect(within(dialog).getByRole("textbox", { name: "说明…" })).toBe(inputA); expect(inputA).toHaveProperty("value", "Unsubmitted A task");
    view.rerender(<ScheduledTasksPage active={false} />); expect(screen.queryByRole("dialog")).toBeNull(); expect(inputA.isConnected).toBe(true);
    view.rerender(<ScheduledTasksPage active />); expect(within(createDialog()).getByRole("textbox", { name: "说明…" })).toBe(inputA);
    expect(useAppStore.getState().draft).toBe("A main draft");
    expect(mocks.send.mock.calls.filter(([command]) => command.type === "scheduler.add")).toHaveLength(0);
  });

  it("retains a rejected creation with its exact error and issues no retry until the user submits again", async () => {
    vi.useFakeTimers(); await renderPage(); const dialog = openCreate();
    change(within(dialog).getByRole("textbox", { name: "说明…" }), "Keep rejected task instructions");
    mocks.send.mockImplementation(async (command: { type: string; conversation_id?: string }) => command.type === "scheduler.add"
      ? { type: "command.result", command: command.type, level: "error", message: "Original scheduler write failure" }
      : command.type === "conversation.create" ? ack("conversation.create", { conversation_id: command.conversation_id, created: true, activated: false }) : ack("scheduler.list", { tasks: [], runs: [] }));
    await act(async () => { fireEvent.click(within(dialog).getByRole("button", { name: "创建" })); });
    await act(async () => { await vi.advanceTimersByTimeAsync(3000); });
    expect(within(createDialog()).getByRole("textbox", { name: "说明…" })).toHaveProperty("value", "Keep rejected task instructions");
    expect(mocks.send.mock.calls.filter(([command]) => command.type === "scheduler.add")).toHaveLength(1);
    expect(mocks.toast).toHaveBeenCalledWith(expect.stringContaining("Original scheduler write failure"), "error");
    await act(async () => { fireEvent.click(within(dialog).getByRole("button", { name: "创建" })); });
    expect(mocks.send.mock.calls.filter(([command]) => command.type === "scheduler.add")).toHaveLength(2);
    expect(mocks.send.mock.calls.filter(([command]) => command.type === "conversation.create")).toHaveLength(1);
    const [created] = mocks.send.mock.calls.find(([command]) => command.type === "conversation.create")!;
    expect(created).toMatchObject({ title: "Keep rejected task instructions", workspace_root: "C:/A", activate: false });
    expect(mocks.send.mock.calls.filter(([command]) => command.type === "scheduler.add").every(([command]) => command.conversation_id === created.conversation_id && command.conversation_id !== "chat-A")).toBe(true);
    expect(useAppStore.getState()).toMatchObject({ conversationId: "chat-A", draft: "A main draft" });
  });

  it("updates a different project's paused task only after its scoped toggle acknowledgement", async () => {
    await renderPage();
    let finish!: (value: unknown) => void;
    mocks.send.mockImplementation((command: { type: string }) => command.type === "scheduler.toggle" ? new Promise(resolve => { finish = resolve; })
      : Promise.resolve(ack("scheduler.list", { tasks: [...tasksA, ...tasksB], runs: [] })));
    const row = screen.getByRole("button", { name: /B task/ }).closest("[role=listitem]")!;
    fireEvent.click(within(row as HTMLElement).getByRole("button", { name: "已安排任务操作" })); fireEvent.click(screen.getByRole("menuitem", { name: "恢复" }));
    expect(within(row as HTMLElement).getByText("已暂停")).toBeTruthy();
    expect(mocks.send).toHaveBeenCalledWith({ type: "scheduler.toggle", task_id: "B", task_workspace_root: "C:/B", scheduler_scope: "global", enabled: true }, "scheduler.toggle");
    tasksB = [{ ...tasksB[0], enabled: true }];
    await act(async () => { finish(ack("scheduler.toggle", { task: tasksB[0] })); });
    expect(within(screen.getByRole("button", { name: /B task/ })).queryByText("已暂停")).toBeNull();
    expect(useAppStore.getState()).toMatchObject({ conversationId: "chat-A", draft: "A main draft", workingDirectory: "C:/A" });
  });

  it("keeps genuine task evidence when the global read fails and does not report an empty successful inventory", async () => {
    const view = await renderPage();
    await act(async () => { view.rerender(<ScheduledTasksPage active={false} />); });
    mocks.send.mockResolvedValue({ level: "error", message: "Global inventory denied" });
    await act(async () => { view.rerender(<ScheduledTasksPage active />); });
    expect(screen.getByRole("alert").textContent).toContain("Global inventory denied");
    expect(screen.getByRole("button", { name: /B task/ })).toBeTruthy();
    expect(screen.getByRole("button", { name: /A task/ })).toBeTruthy();
  });

  it("loads the selected task's older run history through its captured owner before opening the actual result chat", async () => {
    const requestConversationSwitch = vi.fn(); useAppStore.setState({ requestConversationSwitch });
    await renderPage(); fireEvent.click(screen.getByRole("button", { name: /B task/ }));
    mocks.send.mockResolvedValue(ack("scheduler.history", { runs: [{ ...runA, id: "old-B-run", task_id: "B", workspace_root: "C:/B", conversation_id: "old-B-result", result_summary: "An older B result" }], next_offset: 1, has_more: false }));
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "显示更多运行记录" })); });
    expect(mocks.send).toHaveBeenCalledWith({ type: "scheduler.history", task_id: "B", task_workspace_root: "C:/B", scheduler_scope: "global", offset: 0, limit: 50 }, "scheduler.history");
    fireEvent.click(screen.getByRole("button", { name: /An older B result/ }));
    expect(requestConversationSwitch).toHaveBeenCalledWith("old-B-result"); expect(getAutomationsPageState().open).toBe(false);
    expect(useAppStore.getState().draft).toBe("A main draft");
  });

  it("lists an orphan cron even with thousands of loaded chats and does not refetch for chat pagination", async () => {
    let orphan = { ...task("orphan", "C:/orphan-storage", ""), kind: "cron" as const };
    useAppStore.setState({ conversations: [...useAppStore.getState().conversations, ...Array.from({ length: 1000 }, (_, index) => ({ id: `history-${index}`, title: `Chat ${index}`, workspaceRoot: "C:/history", updatedAt: "today" }))] });
    mocks.send.mockImplementation(async (command: { type: string }) => {
      if (command.type === "scheduler.toggle") { orphan = { ...orphan, enabled: false }; return ack(command.type, { task: orphan }); }
      return ack("scheduler.list", { tasks: [orphan], runs: [] });
    });
    await renderPage();
    expect(screen.getByRole("button", { name: /orphan task/ })).toBeTruthy();
    expect(mocks.send).toHaveBeenCalledExactlyOnceWith({ type: "scheduler.list", scheduler_scope: "global" }, "scheduler.list");
    await act(async () => { useAppStore.setState({ conversations: [...useAppStore.getState().conversations, { id: "next-page", title: "Next page", workspaceRoot: "C:/next", updatedAt: "today" }] }); });
    expect(mocks.send).toHaveBeenCalledTimes(1);
    const row = screen.getByRole("button", { name: /orphan task/ }).closest("[role=listitem]")!;
    fireEvent.click(within(row as HTMLElement).getByRole("button", { name: "已安排任务操作" }));
    await act(async () => { fireEvent.click(screen.getByRole("menuitem", { name: "暂停" })); });
    expect(mocks.send).toHaveBeenCalledWith({ type: "scheduler.toggle", task_id: orphan.id, task_workspace_root: "C:/orphan-storage", scheduler_scope: "global", enabled: false }, "scheduler.toggle");
    expect(useAppStore.getState()).toMatchObject({ conversationId: "chat-A", workingDirectory: "C:/A", draft: "A main draft" });
  });
});
