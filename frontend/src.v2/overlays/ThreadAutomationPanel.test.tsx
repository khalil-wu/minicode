/* @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAppStore, waitComposerDraftRecovery, waitEditorDraftRecovery } from "../stores";
import { flushComposerDrafts } from "../stores/composer-drafts";
import { flushEditorDrafts } from "../stores/editor-drafts";
import type { ScheduledTaskEntry } from "../stores/types";
import { ThreadAutomationPanel } from "./ThreadAutomationPanel";
import { SchedulerTab } from "./SchedulerTab";
import { ScheduledTasksPage } from "./ScheduledTasksPage";
import { openAutomations, openThreadAutomationEditor, openThreadAutomationSettings, removeThreadAutomationEditor, useThreadAutomationEditors } from "../lib/automations-navigation";

const mocks = vi.hoisted(() => ({ send: vi.fn(), toast: vi.fn(), preferences: vi.fn() }));
vi.mock("../protocol/ws-outbox", () => ({ sendClientCommandAwaitResult: mocks.send, commandResultSucceeded: (event: { level?: string }) => event.level !== "error" && event.level !== "failed" }));
vi.mock("./ToastContainer", () => ({ pushToast: mocks.toast }));
vi.mock("../desktop/runtime", () => ({ getDesktopPreferences: mocks.preferences }));

const owner = { id: "pane-A", title: "Chat A", workspaceRoot: "C:/A", permissionMode: "auto" as const };
const rule = "RRULE:FREQ=MINUTELY;INTERVAL=30";
const makeTask = (overrides: Partial<ScheduledTaskEntry> = {}): ScheduledTaskEntry => ({ id: "pane-task", name: "Morning check", prompt: "Original prompt", schedule: rule,
  kind: "heartbeat", conversation_id: owner.id, workspace_root: owner.workspaceRoot, timezone: "Asia/Shanghai", isolation: "workspace", permission_mode: "auto", enabled: true, ...overrides });
const editor = (task?: ScheduledTaskEntry) => ({ id: task?.id ?? "draft:pane-A", owner, task, open: true });
const ack = (command: string, data: Record<string, unknown>) => ({ type: "command.result", command, level: "success", message: "", data });
const mutations = () => mocks.send.mock.calls.filter(([command]) => command.type !== "scheduler.list");
const change = (label: string, value: string) => fireEvent.change(screen.getByLabelText(label), { target: { value } });
async function targetPicker() { await act(async () => { fireEvent.click(screen.getByRole("button", { name: "定时任务目标" })); }); return screen.getByRole("dialog", { name: "搜索聊天" }); }
function Inventory() { const editors = useThreadAutomationEditors(); return <output data-testid="automation-inventory">{editors.some(item => item.task?.id === "pane-task") ? "present" : "absent"}</output>; }

describe("ThreadAutomationPanel", () => {
  beforeEach(() => {
    vi.clearAllMocks(); openAutomations();
    vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => window.setTimeout(() => callback(0), 0));
    vi.stubGlobal("cancelAnimationFrame", (handle: number) => window.clearTimeout(handle));
    vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
    mocks.preferences.mockResolvedValue({ defaultTaskDirectory: "D:/Real task outputs" });
    useAppStore.setState({ conversationId: owner.id, workingDirectory: owner.workspaceRoot, isConnected: true,
      draft: "Keep the main draft", scheduledTasks: [], scheduledTaskRuns: [],
      conversations: [{ id: owner.id, title: owner.title, workspaceRoot: owner.workspaceRoot, updatedAt: "today", pinned: true },
        { id: "pane-B", title: "Chat B", workspaceRoot: "C:/B", updatedAt: "today" },
        { id: "pane-C", title: "Occupied chat", workspaceRoot: "C:/B", updatedAt: "today" },
        { id: "child", title: "Child work", workspaceRoot: "C:/B", parentConversationId: owner.id, updatedAt: "today" },
        { id: "remote", title: "Remote chat", workspaceRoot: "C:/remote", environment: "remote", updatedAt: "today" }],
      recentWorkspaces: [{ path: "C:/A", name: "Project A", projectType: "node", lastOpened: 1 }, { path: "project-B", name: "Project B", projectType: "project", lastOpened: 2, sourceFolders: ["C:/B"] }],
      currentProvider: "custom", currentModel: "model-one", availableModels: ["model-one", "model-two"], modelsSource: "configured", availableModelLabels: { "model-two": "Model Two" },
      availableModelOptions: [{ model: "model-one", reasoningEffortLevels: ["low", "high"], defaultReasoningEffort: "low" }, { model: "model-two", reasoningEffortLevels: ["medium", "xhigh"], defaultReasoningEffort: "medium" }],
    });
    mocks.send.mockImplementation(async (command: Record<string, unknown>) => command.type === "scheduler.list"
      ? ack("scheduler.list", { tasks: command.scheduler_scope === "global" || command.workspace_root === "C:/B" ? [makeTask({ id: "other-active", conversation_id: "pane-C", workspace_root: "C:/B" }), makeTask({ id: "other-paused", conversation_id: "pane-B", workspace_root: "C:/B", enabled: false })] : [], runs: [] })
      : ack(String(command.type), { task: makeTask({ ...command as Partial<ScheduledTaskEntry>, workspace_root: String(command.task_workspace_root ?? command.workspace_root ?? "C:/A") }) }));
  });
  afterEach(async () => {
    cleanup();
    // Unmount first so draining IndexedDB work cannot fire a pane autosave.
    if (vi.isFakeTimers()) await vi.runAllTimersAsync();
    vi.useRealTimers();
    await Promise.all([waitComposerDraftRecovery(), waitEditorDraftRecovery()]);
    await Promise.all([flushComposerDrafts(), flushEditorDrafts()]);
    vi.unstubAllGlobals();
    removeThreadAutomationEditor("pane-task"); removeThreadAutomationEditor("draft:pane-A"); openAutomations();
  });

  it("keeps the content scroll and bottom footer independent and exposes Source heartbeat controls", () => {
    const view = render(<ThreadAutomationPanel editor={editor()} />);
    const pane = view.container.querySelector(".thread-automation-panel")!;
    expect(pane.querySelector(".thread-automation-footer")?.parentElement).toBe(pane);
    expect(pane.querySelector(".thread-automation-scroll")?.contains(pane.querySelector(".thread-automation-footer"))).toBe(false);
    expect(screen.getByLabelText("任务名称")).toHaveProperty("value", owner.title);
    expect(screen.queryByLabelText("定时任务项目")).toBeNull();
    expect(screen.queryByLabelText("任务权限模式")).toBeNull();
    expect(screen.queryByLabelText("定时任务权限详情")).toBeNull();
    expect(screen.queryByText("查看 Cron 表达式")).toBeNull();
    expect(screen.getByLabelText("通知")).toHaveProperty("value", "");
    expect(mocks.send).not.toHaveBeenCalled();
  });

  it("uses real local targets and scoped occupancy, then saves standalone project/model/reasoning through the captured owner", async () => {
    render(<ThreadAutomationPanel editor={editor()} />);
    let picker = await targetPicker();
    expect(mocks.send).toHaveBeenCalledWith({ type: "scheduler.list", scheduler_scope: "global" }, "scheduler.list");
    expect(within(picker).queryByRole("button", { name: "Child work" })).toBeNull();
    expect(within(picker).queryByRole("button", { name: "Remote chat" })).toBeNull();
    expect(within(picker).getByRole("button", { name: "Occupied chat" })).toHaveProperty("disabled", true);
    expect(within(picker).getByRole("button", { name: "Chat B" })).toHaveProperty("disabled", false);
    fireEvent.click(within(picker).getByRole("button", { name: "每次新建聊天" }));
    change("定时任务项目", "project-B");
    fireEvent.click(screen.getByRole("button", { name: "定时任务模型，当前：选择模型" }));
    const models = screen.getByRole("listbox", { name: "定时任务模型" });
    expect(within(models).getAllByRole("option").map(option => option.textContent?.trim())).toEqual(["model-one", "Model Two"]);
    expect(within(models).queryByRole("option", { name: "默认" })).toBeNull();
    fireEvent.click(within(models).getByRole("option", { name: "Model Two" }));
    change("定时任务思考强度", "xhigh");
    expect(screen.getByLabelText("定时任务权限详情")).toBeTruthy();
    expect(Array.from((screen.getByLabelText("定时任务思考强度") as HTMLSelectElement).options).map(option => option.value)).toEqual(["", "medium", "xhigh"]);
    change("通知", "failed_runs_only"); change("提示词", "Run in project B");
    act(() => useAppStore.setState({ conversationId: "pane-B", workingDirectory: "C:/B", draft: "Keep B draft" }));
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "创建定时任务" })); });
    expect(mutations()).toHaveLength(1);
    expect(mocks.send).toHaveBeenCalledWith(expect.objectContaining({ type: "scheduler.add", kind: "cron", conversation_id: "", execution_workspace_root: "C:/B", provider: "custom", model: "model-two", reasoning_effort: "xhigh",
      notification_policy: "failed_runs_only", owner_conversation_id: owner.id, workspace_root: owner.workspaceRoot }), "scheduler.add");
    expect(useAppStore.getState()).toMatchObject({ conversationId: "pane-B", workingDirectory: "C:/B", draft: "Keep B draft" });
    expect(mocks.preferences).not.toHaveBeenCalled();
    vi.useFakeTimers();
    picker = await targetPicker(); fireEvent.click(within(picker).getByRole("button", { name: "Chat B" }));
    expect(screen.queryByLabelText("定时任务模型")).toBeNull();
    await act(async () => { await vi.advanceTimersByTimeAsync(600); });
    expect(mocks.send).toHaveBeenLastCalledWith(expect.objectContaining({ type: "scheduler.update", task_id: "pane-task", task_workspace_root: "C:/A", kind: "heartbeat", conversation_id: "pane-B", execution_workspace_root: null,
      owner_conversation_id: owner.id, workspace_root: "C:/A", provider: null, model: null, reasoning_effort: null }), "scheduler.update");
  });

  it("resolves None through the real desktop output base and keeps an empty execution root on the wire", async () => {
    render(<ThreadAutomationPanel editor={editor()} />);
    const picker = await targetPicker(); fireEvent.click(within(picker).getByRole("button", { name: "每次新建聊天" }));
    change("定时任务项目", "__none__"); change("提示词", "Use a generated work folder");
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "创建定时任务" })); });
    expect(mocks.preferences).toHaveBeenCalledTimes(1);
    expect(mocks.send).toHaveBeenLastCalledWith(expect.objectContaining({ type: "scheduler.add", execution_workspace_root: "", projectless_base: "D:/Real task outputs", workspace_root: "C:/A", owner_conversation_id: owner.id }), "scheduler.add");
  });

  it("discards dialog edits on Cancel, rejects invalid RFC rules, and commits the exact complete raw rule with default notification reset", async () => {
    vi.useFakeTimers();
    const original = "DTSTART:20270101T000000Z\nRRULE:FREQ=YEARLY;BYMONTH=1;BYMONTHDAY=1;BYHOUR=0;BYMINUTE=0;BYSECOND=0";
    render(<ThreadAutomationPanel editor={editor(makeTask({ schedule: original, notification_policy: "failed_runs_only" }))} />);
    expect(screen.getByLabelText("重复计划")).toHaveProperty("value", "custom");
    expect(screen.queryByRole("dialog", { name: "编辑计划规则" })).toBeNull();
    expect(screen.getByRole("heading", { name: "频率" }).closest(".automation-section-heading")!.contains(screen.getByRole("button", { name: "编辑计划规则" }))).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "编辑计划规则" }));
    let dialog = screen.getByRole("dialog", { name: "编辑计划规则" });
    expect(within(dialog).getByText("使用 RRULE 定义自定义计划")).toBeTruthy();
    expect(within(dialog).getByText("RRULE", { selector: "label" }).getAttribute("for")).toBe(within(dialog).getByLabelText("RRULE").id);
    expect(within(dialog).getByRole("button", { name: "关闭" })).toBeTruthy();
    expect(within(dialog).getByLabelText("RRULE")).toHaveProperty("value", original);
    fireEvent.change(within(dialog).getByLabelText("RRULE"), { target: { value: "RRULE:FREQ=UNKNOWN" } });
    expect(within(dialog).getByRole("button", { name: "保存" })).toHaveProperty("disabled", true);
    fireEvent.click(within(dialog).getByRole("button", { name: "取消" }));
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    expect(mutations()).toHaveLength(0);
    fireEvent.click(screen.getByRole("button", { name: "编辑计划规则" }));
    dialog = screen.getByRole("dialog", { name: "编辑计划规则" });
    expect(within(dialog).getByLabelText("RRULE")).toHaveProperty("value", original);
    const raw = "DTSTART;TZID=America/New_York:20261101T013000\nRRULE:FREQ=DAILY;COUNT=3\nEXDATE;TZID=America/New_York:20261102T013000";
    fireEvent.change(within(dialog).getByLabelText("RRULE"), { target: { value: raw } });
    fireEvent.click(within(dialog).getByRole("button", { name: "保存" })); change("通知", "");
    await act(async () => { await vi.advanceTimersByTimeAsync(600); });
    expect(mutations()).toHaveLength(1);
    expect(mocks.send).toHaveBeenLastCalledWith(expect.objectContaining({ type: "scheduler.update", task_id: "pane-task", schedule: raw, notification_policy: null, timezone: "Asia/Shanghai" }), "scheduler.update");
    expect(mocks.toast).not.toHaveBeenCalled();
  });

  it("updates pause state only from ACK and locks deletion until its ACK removes the editor and any queued autosave", async () => {
    vi.useFakeTimers();
    const task = makeTask();
    let finishToggle!: (value: unknown) => void;
    let finishDelete!: (value: unknown) => void;
    mocks.send.mockImplementation((command: { type: string }) => command.type === "scheduler.toggle" ? new Promise(resolve => { finishToggle = resolve; })
      : new Promise(resolve => { finishDelete = resolve; }));
    const onClose = vi.fn();
    act(() => openThreadAutomationEditor(owner, task));
    render(<><ThreadAutomationPanel editor={editor(task)} onClose={onClose} /><Inventory /></>);
    fireEvent.click(screen.getByRole("button", { name: "定时任务操作" })); fireEvent.click(screen.getByRole("menuitem", { name: "暂停" }));
    expect(screen.getByRole("button", { name: "定时任务操作" })).toHaveProperty("disabled", true);
    await act(async () => { finishToggle(ack("scheduler.toggle", { task: { ...task, enabled: false } })); });
    fireEvent.click(screen.getByRole("button", { name: "定时任务操作" }));
    expect(screen.getByRole("menuitem", { name: "恢复" })).toBeTruthy(); fireEvent.click(screen.getByRole("menuitem", { name: "删除" }));
    let dialog = screen.getByRole("dialog", { name: "删除 Morning check？" });
    expect(within(dialog).getByRole("button", { name: "关闭" })).toBeTruthy();
    fireEvent.click(within(dialog).getByRole("button", { name: "取消" }));
    expect(screen.queryByRole("dialog")).toBeNull(); expect(screen.getByTestId("automation-inventory").textContent).toBe("present");
    change("提示词", "An unsaved patch before deletion");
    fireEvent.click(screen.getByRole("button", { name: "定时任务操作" })); fireEvent.click(screen.getByRole("menuitem", { name: "删除" }));
    dialog = screen.getByRole("dialog", { name: "删除 Morning check？" }); fireEvent.click(within(dialog).getByRole("button", { name: "删除定时任务" }));
    expect(within(dialog).getByRole("button", { name: "取消" })).toHaveProperty("disabled", true);
    expect(within(dialog).queryByRole("button", { name: "关闭" })).toBeNull();
    fireEvent.keyDown(dialog, { key: "Escape" }); expect(screen.getByRole("dialog")).toBeTruthy();
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    expect(mocks.send.mock.calls.map(([command]) => command.type)).toEqual(["scheduler.toggle", "scheduler.remove"]);
    expect(mocks.send).toHaveBeenLastCalledWith(expect.objectContaining({ type: "scheduler.remove", task_workspace_root: "C:/A", workspace_root: "C:/A", owner_conversation_id: owner.id }), "scheduler.remove");
    await act(async () => { finishDelete(ack("scheduler.remove", { task_id: task.id })); });
    expect(onClose).toHaveBeenCalledTimes(1); expect(screen.getByTestId("automation-inventory").textContent).toBe("absent");
    await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
    expect(mutations()).toHaveLength(2);
  });

  it("opens the same RRULE task on Scheduled through its captured list ACK without exposing Cron fields or touching the selected main chat", async () => {
    const task = makeTask({ schedule: "DTSTART:20270101T000000Z\nRRULE:FREQ=YEARLY;BYMONTH=1;BYMONTHDAY=1;BYHOUR=0;BYMINUTE=0;BYSECOND=0" });
    mocks.send.mockResolvedValue(ack("scheduler.list", { tasks: [task], runs: [] }));
    act(() => { useAppStore.setState({ conversationId: "pane-B", workingDirectory: "C:/B", draft: "Unsent B", scheduledTasks: [makeTask({ id: "B-only", name: "B task" })] }); openThreadAutomationSettings(owner, task); });
    render(<ScheduledTasksPage />);
    await act(async () => {});
    expect(mocks.send).toHaveBeenCalledWith({ type: "scheduler.list", scheduler_scope: "global" }, "scheduler.list");
    expect(screen.getByLabelText("重复计划")).toHaveProperty("value", "custom");
    expect(screen.queryByText("查看 Cron 表达式")).toBeNull(); expect(screen.queryByLabelText("任务权限模式")).toBeNull();
    expect(useAppStore.getState()).toMatchObject({ conversationId: "pane-B", workingDirectory: "C:/B", draft: "Unsent B" });
    expect(mutations()).toHaveLength(0);
  });

  it("keeps an acknowledged management root when a bound task is opened through target B", async () => {
    vi.useFakeTimers();
    const task = makeTask({ conversation_id: "pane-B" });
    const targetOwner = { ...owner, id: "pane-B", title: "Chat B", workspaceRoot: "C:/B" };
    render(<ThreadAutomationPanel editor={{ ...editor(task), owner: targetOwner }} />);
    change("提示词", "Revised from target B");
    await act(async () => { await vi.advanceTimersByTimeAsync(600); });
    expect(mocks.send).toHaveBeenLastCalledWith(expect.objectContaining({ type: "scheduler.update", task_id: task.id, task_workspace_root: "C:/A",
      owner_conversation_id: "pane-B", workspace_root: "C:/B", conversation_id: "pane-B" }), "scheduler.update");
    const picker = await targetPicker(); fireEvent.click(within(picker).getByRole("button", { name: "每次新建聊天" }));
    expect(screen.getByLabelText("定时任务项目")).toHaveProperty("value", "C:/A");
    await act(async () => { await vi.advanceTimersByTimeAsync(600); });
    expect(mocks.send).toHaveBeenLastCalledWith(expect.objectContaining({ type: "scheduler.update", task_workspace_root: "C:/A", kind: "cron", execution_workspace_root: null, workspace_root: "C:/B" }), "scheduler.update");
  });

  it("adopts an acknowledged Settings edit in a clean pane and preserves an existing unsaved local draft", async () => {
    vi.useFakeTimers();
    const original = makeTask();
    const view = render(<ThreadAutomationPanel editor={editor(original)} />);
    const fromSettings = { ...original, name: "Updated in Settings", prompt: "Saved Settings prompt" };
    view.rerender(<ThreadAutomationPanel editor={editor(fromSettings)} />);
    expect(screen.getByLabelText("任务名称")).toHaveProperty("value", "Updated in Settings");
    expect(screen.getByLabelText("提示词")).toHaveProperty("value", "Saved Settings prompt");
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    expect(mutations()).toHaveLength(0);
    change("提示词", "Keep my unsaved sidebar draft");
    view.rerender(<ThreadAutomationPanel editor={editor({ ...fromSettings, prompt: "A later Settings edit" })} />);
    expect(screen.getByLabelText("提示词")).toHaveProperty("value", "Keep my unsaved sidebar draft");
  });

  it("receives its owner's late model metadata without adopting another selected chat's catalog while hidden", () => {
    vi.useFakeTimers();
    useAppStore.setState({ currentModel: "", availableModels: [], availableModelOptions: [] });
    render(<ThreadAutomationPanel editor={editor(makeTask({ kind: "cron", conversation_id: "" }))} />);
    const options = () => Array.from((screen.getByLabelText("定时任务模型") as HTMLSelectElement).options).map(option => option.value);
    expect(options()).toEqual([""]);
    act(() => useAppStore.setState({ currentModel: "late-model", availableModels: ["late-model"], availableModelLabels: { "late-model": "Late model" },
      availableModelOptions: [{ model: "late-model", reasoningEffortLevels: ["low", "xhigh"], defaultReasoningEffort: "low" }] }));
    expect(options()).toEqual(["", "late-model"]);
    change("定时任务模型", "late-model");
    expect(Array.from((screen.getByLabelText("定时任务思考强度") as HTMLSelectElement).options).map(option => option.value)).toEqual(["", "low", "xhigh"]);
    act(() => useAppStore.setState({ conversationId: "pane-B", workingDirectory: "C:/B", currentModel: "B-model", availableModels: ["B-model"],
      availableModelOptions: [{ model: "B-model", reasoningEffortLevels: ["medium"], defaultReasoningEffort: "medium" }] }));
    expect(options()).toEqual(["late-model"]);
    expect((screen.getByLabelText("定时任务模型") as HTMLSelectElement).value).toBe("late-model");
  });
});
