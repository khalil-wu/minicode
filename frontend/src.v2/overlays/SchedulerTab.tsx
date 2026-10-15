import { useI18n, type Translate } from "../i18n";
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { CalendarClock, History, MessageSquareText, Play, Plus, RotateCcw, Square, Trash2 } from "lucide-react";
import { useAppStore } from "../stores";
import { commandResultSucceeded, sendClientCommandAwaitResult } from "../protocol/ws-outbox";
import type { ClientCommand } from "../protocol/events";
import { Section, inputStyle, secondaryActionStyle } from "./settingsShared";
import { pushToast } from "./ToastContainer";
import { showConfirm } from "./DialogService";
import { SelectMenu } from "../components/SelectMenu";
import { reportCommandFailure } from "./commandFeedback";
import { workspaceRootsEqual } from "../lib/workspace-path";

type SchedulePreset = "hourly" | "daily" | "weekdays" | "custom";
const scheduleParts = (schedule: string): { preset: SchedulePreset; time: string } => {
  if (schedule === "0 * * * *") return { preset: "hourly", time: "09:00" };
  const match = /^(\d+) (\d+) \* \* (\*|1-5)$/.exec(schedule);
  if (!match) return { preset: "custom", time: "09:00" };
  return { preset: match[3] === "*" ? "daily" : "weekdays", time: `${match[2].padStart(2, "0")}:${match[1].padStart(2, "0")}` };
};
const scheduleLabel = (tr: Translate, schedule: string) => {
  const { preset, time } = scheduleParts(schedule);
  return preset === "hourly" ? tr("每小时整点") : preset === "custom" ? tr("自定义计划") : `${preset === "daily" ? tr("每天") : tr("工作日")} ${time}`;
};

const runDate = (date: string, timezone: string) => new Date(date).toLocaleString(undefined, { timeZone: timezone, month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });

const localTimezone = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
const timezoneOptions = Array.from(new Set([localTimezone, "UTC", "Asia/Shanghai", "Asia/Tokyo", "Europe/London", "America/New_York"]));

const presetSchedule = (preset: SchedulePreset, time: string, custom: string) => {
  if (preset === "hourly") return "0 * * * *";
  if (preset === "custom") return custom.trim();
  const [hour = "9", minute = "0"] = time.split(":");
  return `${Number(minute)} ${Number(hour)} * * ${preset === "weekdays" ? "1-5" : "*"}`;
};

const runStatusLabel = (tr: Translate, status: string) => ({
  pending: tr("等待运行"),
  running: tr("运行中"),
  completed: tr("已完成"),
  partial: tr("部分完成"),
  failed: tr("失败"),
  cancelled: tr("已取消"),
}[status] ?? status);

const operationError = (tr: Translate, error: unknown): string =>
  error instanceof Error ? error.message : String(error || tr("未知错误"));

export const SchedulerTab = ({
  title,
  description = "",
  active = true,
}: {
  title?: string;
  description?: string;
  active?: boolean;
}) => {
  const { t: tr } = useI18n();
  const scheduledTasks = useAppStore((s) => s.scheduledTasks);
  const scheduledTaskRuns = useAppStore((s) => s.scheduledTaskRuns);
  const conversationId = useAppStore((s) => s.conversationId);
  const workingDirectory = useAppStore((s) => s.workingDirectory);
  const isConnected = useAppStore((s) => s.isConnected);
  const requestConversationSwitch = useAppStore((s) => s.requestConversationSwitch);
  const [newTaskName, setNewTaskName] = useState("");
  const [newTaskPrompt, setNewTaskPrompt] = useState("");
  const [newTaskSchedule, setNewTaskSchedule] = useState("0 * * * *");
  const [schedulePreset, setSchedulePreset] = useState<SchedulePreset>("hourly");
  const [scheduleTime, setScheduleTime] = useState("09:00");
  const [timezone, setTimezone] = useState(localTimezone);
  const currentTimezoneOptions = Array.from(new Set([...timezoneOptions, timezone]));
  const [isolation, setIsolation] = useState<"worktree" | "workspace">("worktree");
  const [taskMode, setTaskMode] = useState<"standalone" | "heartbeat">("standalone");
  const [editingTaskId, setEditingTaskId] = useState<string | null>(null);
  const [taskConversationId, setTaskConversationId] = useState("");
  const [taskPermissionMode, setTaskPermissionMode] = useState("auto");
  const [addingTask, setAddingTask] = useState(false);
  const [pendingTaskActions, setPendingTaskActions] = useState<Record<string, string>>({});
  const [pendingRunActions, setPendingRunActions] = useState<Record<string, string>>({});
  const [listState, setListState] = useState<"idle" | "loading" | "ready" | "error">("idle");
  const [listError, setListError] = useState("");
  const [refreshVersion, setRefreshVersion] = useState(0);
  const editorRef = useRef<HTMLDivElement>(null);
  const [historyCount, setHistoryCount] = useState(8);
  const [historyPage, setHistoryPage] = useState<{ runs: typeof scheduledTaskRuns; has_more: boolean; next_offset: number } | null>(null);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [historyError, setHistoryError] = useState("");
  const effectiveSchedule = useMemo(
    () => presetSchedule(schedulePreset, scheduleTime, newTaskSchedule),
    [newTaskSchedule, schedulePreset, scheduleTime],
  );
  const ownerScope = {
    owner_conversation_id: conversationId ?? undefined,
    workspace_root: workingDirectory,
  };
  const formDraft = { newTaskName, newTaskPrompt, newTaskSchedule, schedulePreset, scheduleTime, timezone, isolation, taskMode, editingTaskId, taskConversationId, taskPermissionMode };
  const draftRef = useRef(formDraft);
  draftRef.current = formDraft;
  const scopeKey = JSON.stringify([conversationId, workingDirectory]);
  const draftScope = useRef(scopeKey);
  const scopedDrafts = useRef(new Map<string, { selected: string; drafts: Map<string, typeof formDraft> }>());
  if (draftScope.current === scopeKey) {
    const scope = scopedDrafts.current.get(scopeKey) || { selected: "new", drafts: new Map<string, typeof formDraft>() };
    scope.selected = editingTaskId || "new";
    scope.drafts.set(scope.selected, formDraft);
    scopedDrafts.current.set(scopeKey, scope);
  }
  const applyDraft = (draft: typeof formDraft) => {
    setNewTaskName(draft.newTaskName); setNewTaskPrompt(draft.newTaskPrompt); setNewTaskSchedule(draft.newTaskSchedule);
    setSchedulePreset(draft.schedulePreset); setScheduleTime(draft.scheduleTime); setTimezone(draft.timezone);
    setIsolation(draft.isolation); setTaskMode(draft.taskMode); setEditingTaskId(draft.editingTaskId); setTaskConversationId(draft.taskConversationId);
    setTaskPermissionMode(draft.taskPermissionMode);
  };
  const emptyDraft: typeof formDraft = { newTaskName: "", newTaskPrompt: "", newTaskSchedule: "0 * * * *", schedulePreset: "hourly", scheduleTime: "09:00", timezone: localTimezone, isolation: "worktree", taskMode: "standalone", editingTaskId: null, taskConversationId: "", taskPermissionMode: "auto" };
  useLayoutEffect(() => {
    draftScope.current = scopeKey;
    const scope = scopedDrafts.current.get(scopeKey);
    applyDraft(scope?.drafts.get(scope.selected) || emptyDraft);
    setHistoryPage(null); setHistoryCount(8); setHistoryError("");
  }, [scopeKey]);
  const editTask = (task: typeof scheduledTasks[number]) => {
    const cached = scopedDrafts.current.get(scopeKey)?.drafts.get(task.id);
    const { preset, time } = scheduleParts(task.schedule);
    applyDraft(cached || { newTaskName: task.name, newTaskPrompt: task.prompt, newTaskSchedule: task.schedule,
      schedulePreset: preset, scheduleTime: time, timezone: task.timezone || localTimezone, isolation: task.isolation || "workspace",
      taskMode: task.conversation_id ? "heartbeat" : "standalone", taskConversationId: task.conversation_id || "", editingTaskId: task.id, taskPermissionMode: task.permission_mode });
    setHistoryPage(null); setHistoryCount(8); setHistoryError("");
    editorRef.current?.scrollIntoView?.({ block: "nearest" });
  };
  const createTask = () => {
    applyDraft(scopedDrafts.current.get(scopeKey)?.drafts.get("new") || emptyDraft);
    setHistoryPage(null); setHistoryCount(8); setHistoryError("");
  };
  const localRuns = scheduledTaskRuns.filter((run) => !editingTaskId || run.task_id === editingTaskId);
  const historyRuns = historyPage ? [...localRuns, ...historyPage.runs].filter((run, index, rows) => rows.findIndex((row) => row.id === run.id) === index)
    .sort((a, b) => (b.started_at || b.scheduled_at).localeCompare(a.started_at || a.scheduled_at)) : localRuns;
  const loadMoreHistory = async () => {
    if (!historyPage && historyCount < localRuns.length) { setHistoryCount((count) => count + 20); return; }
    setHistoryLoading(true); setHistoryError("");
    const requestedTask = editingTaskId;
    try {
      const result = await sendClientCommandAwaitResult({ type: "scheduler.history", task_id: editingTaskId || undefined,
        offset: historyPage?.next_offset ?? localRuns.length, limit: 50, ...ownerScope }, "scheduler.history");
      if (reportCommandFailure(result, tr("读取运行历史"))) return;
      if (draftScope.current !== scopeKey || draftRef.current.editingTaskId !== requestedTask) return;
      const page = result.data as unknown as { runs: typeof scheduledTaskRuns; has_more: boolean; next_offset: number };
      setHistoryPage((previous) => ({ ...page, runs: [...(previous?.runs || []), ...page.runs] }));
      setHistoryCount((count) => count + 50);
    } catch (error) { setHistoryError(operationError(tr, error)); }
    finally { setHistoryLoading(false); }
  };
  const scopeIsCurrent = () => {
    const current = useAppStore.getState();
    return current.conversationId === conversationId && workspaceRootsEqual(current.workingDirectory, workingDirectory);
  };
  useEffect(() => {
    if (!active) return;
    let cancelled = false;
    if (!conversationId) {
      setListState("idle");
      setListError(tr("请先打开项目任务。"));
      return;
    }
    if (!isConnected) {
      setListState("error");
      setListError(tr("后端连接尚未就绪，恢复连接后将重新加载。"));
      return;
    }
    setListState("loading");
    setListError("");
    void sendClientCommandAwaitResult({ type: "scheduler.list",
      owner_conversation_id: conversationId, workspace_root: workingDirectory }, "scheduler.list").then((result) => {
      if (cancelled) return;
      if (!commandResultSucceeded(result)) throw new Error(result.message || tr("无法读取定时任务。"));
      setListState("ready");
    }).catch((error) => {
      if (cancelled) return;
      setListState("error");
      setListError(operationError(tr, error));
    });
    return () => { cancelled = true; };
  }, [active, conversationId, workingDirectory, isConnected, refreshVersion]);

  const addTask = async () => {
    const name = newTaskName.trim();
    const prompt = newTaskPrompt.trim();
    const schedule = effectiveSchedule.trim();
    if (!name || !prompt || !schedule || addingTask || !conversationId || !workingDirectory) return;
    const submittedDraft = draftRef.current;
    setAddingTask(true);
    try {
      const configuration = {
        name,
        prompt,
        schedule,
        timezone,
        isolation,
        permission_mode: taskPermissionMode,
        ...ownerScope,
        conversation_id: taskMode === "heartbeat" ? taskConversationId || conversationId || undefined : editingTaskId ? "" : undefined,
      };
      const result = await sendClientCommandAwaitResult(editingTaskId
        ? { type: "scheduler.update", task_id: editingTaskId, ...configuration }
        : { type: "scheduler.add", ...configuration },
      editingTaskId ? "scheduler.update" : "scheduler.add");
      if (reportCommandFailure(result, editingTaskId ? tr("保存定时任务") : tr("添加定时任务"))) return;
      if (draftScope.current === scopeKey && (Object.keys(submittedDraft) as Array<keyof typeof submittedDraft>).every((key) =>
        draftRef.current[key] === submittedDraft[key])) {
        if (!editingTaskId) { setNewTaskName(""); setNewTaskPrompt(""); }
      }
      pushToast(tr("{v0}定时任务：{v1}", { v0: editingTaskId ? tr("已保存") : tr("已添加"), v1: name }), "success");
    } catch (error) {
      pushToast(tr("{v0}定时任务失败：{v1}", { v0: editingTaskId ? tr("保存") : tr("添加"), v1: operationError(tr, error) }), "error");
    } finally {
      setAddingTask(false);
    }
  };

  const runTaskAction = async (
    taskId: string,
    command: ClientCommand,
    expectedCommand: string,
    action: string,
    successMessage: string,
    confirmation?: Parameters<typeof showConfirm>[0],
  ) => {
    if (pendingTaskActions[taskId]) return;
    setPendingTaskActions((current) => ({ ...current, [taskId]: expectedCommand }));
    try {
      if (confirmation && !await showConfirm(confirmation)) return;
      if (!scopeIsCurrent()) return;
      const result = await sendClientCommandAwaitResult(command, expectedCommand);
      if (!reportCommandFailure(result, action)) pushToast(successMessage, "success");
    } catch (error) {
      pushToast(tr("{v0}失败：{v1}", { v0: action, v1: operationError(tr, error) }), "error");
    } finally {
      setPendingTaskActions((current) => {
        const next = { ...current };
        delete next[taskId];
        return next;
      });
    }
  };

  const removeTask = (taskId: string, name: string) => runTaskAction(
    taskId,
    { type: "scheduler.remove", task_id: taskId, ...ownerScope },
    "scheduler.remove",
    tr("删除定时任务"),
    tr("已删除定时任务：{v0}", { v0: name }),
    { title: tr("删除定时任务"), message: tr("确定删除“{v0}”？已有运行记录会保留。", { v0: name }), confirmLabel: tr("删除"), danger: true },
  );

  const runHistoryAction = async (
    runId: string,
    command: ClientCommand,
    expectedCommand: string,
    action: string,
    successMessage: string,
  ) => {
    if (pendingRunActions[runId]) return;
    setPendingRunActions((current) => ({ ...current, [runId]: expectedCommand }));
    try {
      const result = await sendClientCommandAwaitResult(command, expectedCommand);
      if (!reportCommandFailure(result, action)) pushToast(successMessage, "success");
    } catch (error) {
      pushToast(tr("{v0}失败：{v1}", { v0: action, v1: operationError(tr, error) }), "error");
    } finally {
      setPendingRunActions((current) => {
        const next = { ...current };
        delete next[runId];
        return next;
      });
    }
  };

  return (
    <Section title={title ?? tr("已安排")} description={description}>
      {listState === "loading" && <p role="status">{tr("正在读取定时任务…")}</p>}
      {listError && <div role="alert" className="settings-page-note">
        {listState === "error" ? tr("读取定时任务失败：") : ""}{listError}
        {listState === "error" && <button type="button" style={secondaryActionStyle}
          onClick={() => setRefreshVersion((version) => version + 1)}>{tr("重试")}</button>}
      </div>}
      {scheduledTasks.length > 0 && (
        <div className="flex flex-col gap-1.5">
          {scheduledTasks.map((t) => (
            <div key={t.id} className="scheduler-task-row flex items-center gap-2 px-2.5 py-1.5 rounded" style={{ background: "var(--surface-soft)" }}>
              <span className="w-2 h-2 rounded-full shrink-0" style={{ background: t.enabled ? "var(--state-success)" : "var(--text-muted)" }} />
              <div className="flex-1 min-w-0">
                <button type="button" className="scheduler-task-title" onClick={() => editTask(t)} aria-label={tr("编辑 {v0}", { v0: t.name })}>{t.name}</button>
                <div className="text-[11px]" style={{ color: "var(--text-muted)", fontSize: "var(--mc-font-secondary)" }}>
                  <span>{scheduleLabel(tr, t.schedule)}</span>
                  <span> · {t.timezone || localTimezone} · {t.isolation === "workspace" ? tr("当前项目") : tr("独立 Worktree")}</span>
                </div>
                <div className="scheduler-task-next">{t.next_run_at ? tr("下次运行：{v0} · {v1}", { v0: runDate(t.next_run_at, t.timezone || localTimezone), v1: t.timezone || localTimezone }) : t.enabled ? tr("暂无下次运行时间") : tr("已暂停")}</div>
              </div>
              <div className="scheduler-task-last" style={{ color: "var(--text-muted)", fontSize: "var(--mc-font-caption)" }}>
                {t.last_run_at && <span>{tr("上次运行：")}{runDate(t.last_run_at, t.timezone || localTimezone)} · {t.timezone || localTimezone}</span>}
              </div>
              <button
                onClick={() => void runTaskAction(
                  t.id,
                  { type: "scheduler.toggle", task_id: t.id, enabled: !t.enabled, ...ownerScope },
                  "scheduler.toggle",
                  t.enabled ? tr("停用定时任务") : tr("启用定时任务"),
                  tr("{v0}定时任务：{v1}", { v0: t.enabled ? tr("已停用") : tr("已启用"), v1: t.name }),
                )}
                disabled={Boolean(pendingTaskActions[t.id])}
                className="px-1.5 py-0.5 text-[11px]"
                style={secondaryActionStyle}
              >
                {pendingTaskActions[t.id] === "scheduler.toggle" ? tr("处理中…") : t.enabled ? tr("停用") : tr("启用")}
              </button>
              <button
                onClick={() => void runTaskAction(
                  t.id,
                  { type: "scheduler.run_now", task_id: t.id, ...ownerScope },
                  "scheduler.run_now",
                  tr("立即运行定时任务"),
                  tr("已开始运行：{v0}", { v0: t.name }),
                )}
                disabled={Boolean(pendingTaskActions[t.id])}
                className="mc-icon-button mc-icon-button-compact"
                aria-label={tr("立即运行 {v0}", { v0: t.name })}
                title={tr("立即运行")}
              >
                {pendingTaskActions[t.id] === "scheduler.run_now" ? <RotateCcw size={14} className="settings-spin" /> : <Play size={14} />}
              </button>
              <button
                onClick={() => void removeTask(t.id, t.name)}
                disabled={Boolean(pendingTaskActions[t.id])}
                className="mc-icon-button mc-icon-button-compact mc-icon-button-danger"
                aria-label={tr("删除 {v0}", { v0: t.name })}
                title={tr("删除")}
              >
                {pendingTaskActions[t.id] === "scheduler.remove" ? <RotateCcw size={14} className="settings-spin" /> : <Trash2 size={14} />}
              </button>
            </div>
          ))}
        </div>
      )}
      {scheduledTasks.length === 0 && listState === "ready" && (
        <div className="scheduler-empty">
          <CalendarClock aria-hidden="true" />
          <div><strong>{tr("暂无定时任务")}</strong><span>{tr("创建后会在设定时间自动运行。")}</span></div>
        </div>
      )}
      {(historyRuns.length > 0 || editingTaskId) && (
        <div className="flex flex-col gap-1 mt-2">
          <div className="flex items-center gap-1.5" style={{ color: "var(--text-muted)", fontSize: "var(--mc-font-secondary)" }}>
            <History size={14} /> {editingTaskId ? tr("{v0}的运行历史", { v0: newTaskName || tr("此任务") }) : tr("最近运行")}
          </div>
          {historyRuns.slice(0, historyCount).map((run) => {
            const task = scheduledTasks.find((item) => item.id === run.task_id);
            const running = run.status === "pending" || run.status === "running";
            return (
              <div key={run.id} className="flex items-center gap-2 px-2.5 py-1.5 rounded" style={{ background: "var(--surface-soft)" }}>
                <span className="w-1.5 h-1.5 rounded-full" style={{ background: running ? "var(--accent-primary)" : run.status === "completed" ? "var(--state-success)" : run.status === "partial" ? "var(--state-warning)" : "var(--state-danger)" }} />
                <div className="flex-1 min-w-0">
                  <div className="truncate" style={{ color: "var(--text-secondary)", fontSize: "var(--mc-font-secondary)" }}>{task?.name ?? tr("计划任务")}</div>
                  <div className="truncate" title={run.error || run.result_summary || runStatusLabel(tr, run.status)} style={{ color: "var(--text-muted)", fontSize: "var(--mc-font-caption)" }}>{run.error || run.result_summary || runStatusLabel(tr, run.status)}</div>
                  <time dateTime={run.started_at || run.scheduled_at} className="scheduler-run-date">{runDate(run.started_at || run.scheduled_at, task?.timezone || localTimezone)} · {task?.timezone || localTimezone} · {runStatusLabel(tr, run.status)}</time>
                </div>
                {run.conversation_id && (
                  <button
                    onClick={() => requestConversationSwitch(run.conversation_id!)}
                    className="mc-icon-button mc-icon-button-compact"
                    title={tr("打开运行对话")}
                    aria-label={tr("打开运行对话")}
                  >
                    <MessageSquareText size={14} />
                  </button>
                )}
                {running ? (
                  <button
                    onClick={() => void runHistoryAction(
                      run.id,
                      { type: "scheduler.cancel", run_id: run.id, ...ownerScope },
                      "scheduler.cancel",
                      tr("取消运行"),
                      tr("运行已取消"),
                    )}
                    disabled={Boolean(pendingRunActions[run.id])}
                    className="mc-icon-button mc-icon-button-compact"
                    title={tr("取消运行")}
                    aria-label={tr("取消运行")}
                  >{pendingRunActions[run.id] ? <RotateCcw size={14} className="settings-spin" /> : <Square size={14} />}</button>
                ) : (
                  <button
                    onClick={() => void runHistoryAction(
                      run.id,
                      { type: "scheduler.retry", run_id: run.id, ...ownerScope },
                      "scheduler.retry",
                      tr("重试运行"),
                      tr("已开始重试"),
                    )}
                    disabled={Boolean(pendingRunActions[run.id])}
                    className="mc-icon-button mc-icon-button-compact"
                    title={tr("重试")}
                    aria-label={tr("重试")}
                  ><RotateCcw size={14} className={pendingRunActions[run.id] ? "settings-spin" : undefined} /></button>
                )}
              </div>
            );
          })}
          {(historyCount < historyRuns.length || !historyPage || historyPage.has_more) && <button type="button" className="settings-action-button"
            disabled={historyLoading} onClick={() => void loadMoreHistory()}>{historyLoading ? tr("正在读取…") : tr("显示更多运行记录")}</button>}
          {historyError && <p role="alert">{historyError}</p>}
        </div>
      )}
      <div className="scheduler-editor" ref={editorRef}>
        <div className="scheduler-editor-heading"><strong>{editingTaskId ? tr("编辑定时任务") : tr("添加定时任务")}</strong><span>{tr("设置提示词、频率和运行工作区。")}{(newTaskName || newTaskPrompt) && <span className="settings-unsaved">{tr("草稿会保留")}</span>}</span>
          {editingTaskId && <button type="button" className="settings-action-button" onClick={createTask}>{tr("返回新建任务")}</button>}
        </div>
        <input
          placeholder={tr("任务名称")}
          aria-label={tr("任务名称")}
          value={newTaskName}
          onChange={(e) => setNewTaskName(e.target.value)}
          style={inputStyle}
        />
        <textarea
          placeholder={tr("要运行的提示词")}
          value={newTaskPrompt}
          onChange={(e) => setNewTaskPrompt(e.target.value)}
          rows={3}
          className="resize-y text-xs"
          style={inputStyle}
        />
        <div className="scheduler-schedule-row grid grid-cols-2 gap-1.5">
          <SelectMenu className="settings-select settings-select-regular" align="end" value={schedulePreset} onValueChange={(value) => setSchedulePreset(value as SchedulePreset)} ariaLabel={tr("运行频率")}>
            <option value="hourly">{tr("每小时")}</option>
            <option value="daily">{tr("每天")}</option>
            <option value="weekdays">{tr("工作日")}</option>
            <option value="custom">{tr("自定义 Cron")}</option>
          </SelectMenu>
          {schedulePreset === "daily" || schedulePreset === "weekdays" ? (
            <input type="time" value={scheduleTime} onChange={(event) => setScheduleTime(event.target.value)} style={inputStyle} aria-label={tr("运行时间")} />
          ) : (
            <SelectMenu className="settings-select settings-select-regular" align="end" value={timezone} onValueChange={setTimezone} ariaLabel={tr("时区")}>
              {currentTimezoneOptions.map((item) => <option value={item} key={item}>{item}</option>)}
            </SelectMenu>
          )}
        </div>
        {(schedulePreset === "daily" || schedulePreset === "weekdays") && (
          <SelectMenu className="settings-select settings-select-regular" align="end" value={timezone} onValueChange={setTimezone} ariaLabel={tr("时区")}>
            {currentTimezoneOptions.map((item) => <option value={item} key={item}>{item}</option>)}
          </SelectMenu>
        )}
        <div className="scheduler-schedule-row grid grid-cols-2 gap-1.5">
          <SelectMenu className="settings-select settings-select-regular" align="end" value={isolation} onValueChange={(value) => setIsolation(value as "worktree" | "workspace")} ariaLabel={tr("运行工作区")}>
            <option value="worktree">{tr("独立 Worktree")}</option>
            <option value="workspace">{tr("当前项目")}</option>
          </SelectMenu>
          <SelectMenu className="settings-select settings-select-regular" align="end" value={taskMode} onValueChange={(value) => { setTaskMode(value as "standalone" | "heartbeat"); if (value === "heartbeat" && !taskConversationId) setTaskConversationId(conversationId || ""); }} ariaLabel={tr("对话模式")}>
            <option value="standalone">{tr("每次新建对话")}</option>
            <option value="heartbeat" disabled={!conversationId}>{editingTaskId && taskConversationId && taskConversationId !== conversationId ? tr("继续原任务对话") : tr("继续当前对话")}</option>
          </SelectMenu>
        </div>
        <div className="scheduler-schedule-row flex gap-1.5 items-center">
          {schedulePreset === "custom" ? (
            <input
              placeholder={tr("Cron 表达式（例如 0 9 * * 1-5）")}
              value={newTaskSchedule}
              onChange={(e) => setNewTaskSchedule(e.target.value)}
              className="flex-1 text-xs"
              style={inputStyle}
            />
          ) : <span className="flex-1 text-[11px]" style={{ color: "var(--text-muted)", fontSize: "var(--mc-font-caption)" }}>{scheduleLabel(tr, effectiveSchedule)} · {timezone}</span>}
          <button
            onClick={() => void addTask()}
            disabled={!newTaskName.trim() || !newTaskPrompt.trim() || !effectiveSchedule.trim() || addingTask || !conversationId || !workingDirectory}
            style={{ ...secondaryActionStyle, display: "inline-flex", alignItems: "center", gap: 7 }}
          >
            <Plus size={14} /> {addingTask ? tr("正在保存…") : editingTaskId ? tr("保存修改") : tr("添加")}
          </button>
        </div>
        <SelectMenu className="settings-select settings-select-regular" align="end" value={taskPermissionMode} onValueChange={setTaskPermissionMode} ariaLabel={tr("任务权限模式")}><option value="auto">{tr("正常自动权限")}</option><option value="confirm">{tr("需要确认")}</option></SelectMenu>
        <details className="scheduler-cron-detail"><summary>{tr("查看 Cron 表达式")}</summary><code>{effectiveSchedule}</code></details>
        {editingTaskId && <p className="settings-page-note">{tr("修改将用于下一次运行，已开始的运行继续使用启动时的配置。")}</p>}
      </div>
    </Section>
  );
};
