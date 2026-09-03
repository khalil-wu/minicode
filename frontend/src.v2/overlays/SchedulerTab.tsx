import { useMemo, useState } from "react";
import { CalendarClock, History, MessageSquareText, Play, Plus, RotateCcw, Square, Trash2 } from "lucide-react";
import { useAppStore } from "../stores";
import { sendClientCommandAwaitResult } from "../protocol/ws-outbox";
import type { ClientCommand } from "../protocol/events";
import { Section, inputStyle, secondaryActionStyle } from "./settingsShared";
import { pushToast } from "./ToastContainer";
import { showConfirm } from "./DialogService";
import { SelectMenu } from "../components/SelectMenu";
import { reportCommandFailure } from "./commandFeedback";

type SchedulePreset = "hourly" | "daily" | "weekdays" | "custom";

const localTimezone = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
const timezoneOptions = Array.from(new Set([localTimezone, "UTC", "Asia/Shanghai", "Asia/Tokyo", "Europe/London", "America/New_York"]));

const presetSchedule = (preset: SchedulePreset, time: string, custom: string) => {
  if (preset === "hourly") return "0 * * * *";
  if (preset === "custom") return custom.trim();
  const [hour = "9", minute = "0"] = time.split(":");
  return `${Number(minute)} ${Number(hour)} * * ${preset === "weekdays" ? "1-5" : "*"}`;
};

const runStatusLabel = (status: string) => ({
  pending: "等待运行",
  running: "运行中",
  completed: "已完成",
  partial: "部分完成",
  failed: "失败",
  cancelled: "已取消",
}[status] ?? status);

const operationError = (error: unknown): string =>
  error instanceof Error ? error.message : String(error || "未知错误");

export const SchedulerTab = ({
  title = "已安排",
  description = "",
}: {
  title?: string;
  description?: string;
}) => {
  const scheduledTasks = useAppStore((s) => s.scheduledTasks);
  const scheduledTaskRuns = useAppStore((s) => s.scheduledTaskRuns);
  const conversationId = useAppStore((s) => s.conversationId);
  const workingDirectory = useAppStore((s) => s.workingDirectory);
  const requestConversationSwitch = useAppStore((s) => s.requestConversationSwitch);
  const [newTaskName, setNewTaskName] = useState("");
  const [newTaskPrompt, setNewTaskPrompt] = useState("");
  const [newTaskSchedule, setNewTaskSchedule] = useState("0 * * * *");
  const [schedulePreset, setSchedulePreset] = useState<SchedulePreset>("hourly");
  const [scheduleTime, setScheduleTime] = useState("09:00");
  const [timezone, setTimezone] = useState(localTimezone);
  const [isolation, setIsolation] = useState<"worktree" | "workspace">("worktree");
  const [taskMode, setTaskMode] = useState<"standalone" | "heartbeat">("standalone");
  const [addingTask, setAddingTask] = useState(false);
  const [pendingTaskActions, setPendingTaskActions] = useState<Record<string, string>>({});
  const [pendingRunActions, setPendingRunActions] = useState<Record<string, string>>({});
  const effectiveSchedule = useMemo(
    () => presetSchedule(schedulePreset, scheduleTime, newTaskSchedule),
    [newTaskSchedule, schedulePreset, scheduleTime],
  );
  const ownerScope = {
    owner_conversation_id: conversationId ?? undefined,
    workspace_root: workingDirectory || undefined,
  };

  const addTask = async () => {
    const name = newTaskName.trim();
    const prompt = newTaskPrompt.trim();
    const schedule = effectiveSchedule.trim();
    if (!name || !prompt || !schedule || addingTask) return;
    setAddingTask(true);
    try {
      const result = await sendClientCommandAwaitResult({
        type: "scheduler.add",
        name,
        prompt,
        schedule,
        timezone,
        isolation,
        permission_mode: "auto",
        ...ownerScope,
        conversation_id: taskMode === "heartbeat" ? conversationId ?? undefined : undefined,
      }, "scheduler.add");
      if (reportCommandFailure(result, "添加定时任务")) return;
      setNewTaskName((current) => current.trim() === name ? "" : current);
      setNewTaskPrompt((current) => current.trim() === prompt ? "" : current);
      setNewTaskSchedule((current) => current.trim() === schedule ? "0 * * * *" : current);
      pushToast(`已添加定时任务：${name}`, "success");
    } catch (error) {
      pushToast(`添加定时任务失败：${operationError(error)}`, "error");
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
  ) => {
    if (pendingTaskActions[taskId]) return;
    setPendingTaskActions((current) => ({ ...current, [taskId]: expectedCommand }));
    try {
      const result = await sendClientCommandAwaitResult(command, expectedCommand);
      if (!reportCommandFailure(result, action)) pushToast(successMessage, "success");
    } catch (error) {
      pushToast(`${action}失败：${operationError(error)}`, "error");
    } finally {
      setPendingTaskActions((current) => {
        const next = { ...current };
        delete next[taskId];
        return next;
      });
    }
  };

  const removeTask = async (taskId: string, name: string) => {
    const confirmed = await showConfirm({
      title: "删除定时任务",
      message: `确定删除“${name}”？已有运行记录会保留。`,
      confirmLabel: "删除",
      danger: true,
    });
    if (!confirmed) return;
    await runTaskAction(
      taskId,
      { type: "scheduler.remove", task_id: taskId, ...ownerScope },
      "scheduler.remove",
      "删除定时任务",
      `已删除定时任务：${name}`,
    );
  };

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
      pushToast(`${action}失败：${operationError(error)}`, "error");
    } finally {
      setPendingRunActions((current) => {
        const next = { ...current };
        delete next[runId];
        return next;
      });
    }
  };

  return (
    <Section title={title} description={description}>
      {scheduledTasks.length > 0 && (
        <div className="flex flex-col gap-1.5">
          {scheduledTasks.map((t) => (
            <div key={t.id} className="scheduler-task-row flex items-center gap-2 px-2.5 py-1.5 rounded" style={{ background: "var(--surface-soft)" }}>
              <span className="w-2 h-2 rounded-full shrink-0" style={{ background: t.enabled ? "var(--state-success)" : "var(--text-muted)" }} />
              <div className="flex-1 min-w-0">
                <div className="font-medium" style={{ fontSize: "var(--mc-font-body)", color: "var(--text-primary)" }}>{t.name}</div>
                <div className="text-[11px]" style={{ color: "var(--text-muted)", fontSize: "var(--mc-font-secondary)" }}>
                  <span style={{ fontFamily: "var(--font-ui)" }}>{t.schedule}</span>
                  <span> · {t.timezone || "UTC"} · {t.isolation === "workspace" ? "当前项目" : "独立 Worktree"}</span>
                </div>
              </div>
              <div className="hidden md:flex flex-col items-end gap-0.5 text-[10px]" style={{ color: "var(--text-muted)", fontSize: "var(--mc-font-caption)" }}>
                {t.next_run_at && <span>下次运行：{new Date(t.next_run_at).toLocaleString()}</span>}
                {t.last_run_at && <span>上次运行：{new Date(t.last_run_at).toLocaleString()}</span>}
              </div>
              <button
                onClick={() => void runTaskAction(
                  t.id,
                  { type: "scheduler.toggle", task_id: t.id, enabled: !t.enabled, ...ownerScope },
                  "scheduler.toggle",
                  t.enabled ? "停用定时任务" : "启用定时任务",
                  `${t.enabled ? "已停用" : "已启用"}定时任务：${t.name}`,
                )}
                disabled={Boolean(pendingTaskActions[t.id])}
                className="px-1.5 py-0.5 text-[11px]"
                style={secondaryActionStyle}
              >
                {pendingTaskActions[t.id] === "scheduler.toggle" ? "处理中…" : t.enabled ? "停用" : "启用"}
              </button>
              <button
                onClick={() => void runTaskAction(
                  t.id,
                  { type: "scheduler.run_now", task_id: t.id, ...ownerScope },
                  "scheduler.run_now",
                  "立即运行定时任务",
                  `已开始运行：${t.name}`,
                )}
                disabled={Boolean(pendingTaskActions[t.id])}
                className="mc-icon-button mc-icon-button-compact"
                aria-label={`立即运行 ${t.name}`}
                title="立即运行"
              >
                {pendingTaskActions[t.id] === "scheduler.run_now" ? <RotateCcw size={14} className="settings-spin" /> : <Play size={14} />}
              </button>
              <button
                onClick={() => void removeTask(t.id, t.name)}
                disabled={Boolean(pendingTaskActions[t.id])}
                className="mc-icon-button mc-icon-button-compact mc-icon-button-danger"
                aria-label={`删除 ${t.name}`}
                title="删除"
              >
                {pendingTaskActions[t.id] === "scheduler.remove" ? <RotateCcw size={14} className="settings-spin" /> : <Trash2 size={14} />}
              </button>
            </div>
          ))}
        </div>
      )}
      {scheduledTasks.length === 0 && (
        <div className="scheduler-empty">
          <CalendarClock aria-hidden="true" />
          <div><strong>暂无定时任务</strong><span>创建后会在设定时间自动运行。</span></div>
        </div>
      )}
      {scheduledTaskRuns.length > 0 && (
        <div className="flex flex-col gap-1 mt-2">
          <div className="flex items-center gap-1.5" style={{ color: "var(--text-muted)", fontSize: "var(--mc-font-secondary)" }}>
            <History size={14} /> 最近运行
          </div>
          {scheduledTaskRuns.slice(0, 8).map((run) => {
            const task = scheduledTasks.find((item) => item.id === run.task_id);
            const running = run.status === "pending" || run.status === "running";
            return (
              <div key={run.id} className="flex items-center gap-2 px-2.5 py-1.5 rounded" style={{ background: "var(--surface-soft)" }}>
                <span className="w-1.5 h-1.5 rounded-full" style={{ background: running ? "var(--accent-primary)" : run.status === "completed" ? "var(--state-success)" : run.status === "partial" ? "var(--state-warning)" : "var(--state-danger)" }} />
                <div className="flex-1 min-w-0">
                  <div className="truncate" style={{ color: "var(--text-secondary)", fontSize: "var(--mc-font-secondary)" }}>{task?.name ?? "计划任务"}</div>
                  <div className="truncate" title={run.error || run.result_summary || runStatusLabel(run.status)} style={{ color: "var(--text-muted)", fontSize: "var(--mc-font-caption)" }}>{run.error || run.result_summary || runStatusLabel(run.status)}</div>
                </div>
                {run.conversation_id && (
                  <button
                    onClick={() => requestConversationSwitch(run.conversation_id!)}
                    className="mc-icon-button mc-icon-button-compact"
                    title="打开运行对话"
                    aria-label="打开运行对话"
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
                      "取消运行",
                      "运行已取消",
                    )}
                    disabled={Boolean(pendingRunActions[run.id])}
                    className="mc-icon-button mc-icon-button-compact"
                    title="取消运行"
                    aria-label="取消运行"
                  >{pendingRunActions[run.id] ? <RotateCcw size={14} className="settings-spin" /> : <Square size={14} />}</button>
                ) : (
                  <button
                    onClick={() => void runHistoryAction(
                      run.id,
                      { type: "scheduler.retry", run_id: run.id, ...ownerScope },
                      "scheduler.retry",
                      "重试运行",
                      "已开始重试",
                    )}
                    disabled={Boolean(pendingRunActions[run.id])}
                    className="mc-icon-button mc-icon-button-compact"
                    title="重试"
                    aria-label="重试"
                  ><RotateCcw size={14} className={pendingRunActions[run.id] ? "settings-spin" : undefined} /></button>
                )}
              </div>
            );
          })}
        </div>
      )}
      <div className="scheduler-editor">
        <div className="scheduler-editor-heading"><strong>添加定时任务</strong><span>设置提示词、频率和运行工作区。</span></div>
        <input
          placeholder="任务名称"
          value={newTaskName}
          onChange={(e) => setNewTaskName(e.target.value)}
          style={inputStyle}
        />
        <textarea
          placeholder="要运行的提示词"
          value={newTaskPrompt}
          onChange={(e) => setNewTaskPrompt(e.target.value)}
          rows={3}
          className="resize-y text-xs"
          style={inputStyle}
        />
        <div className="scheduler-schedule-row grid grid-cols-2 gap-1.5">
          <SelectMenu value={schedulePreset} onValueChange={(value) => setSchedulePreset(value as SchedulePreset)} ariaLabel="运行频率">
            <option value="hourly">每小时</option>
            <option value="daily">每天</option>
            <option value="weekdays">工作日</option>
            <option value="custom">自定义 Cron</option>
          </SelectMenu>
          {schedulePreset === "daily" || schedulePreset === "weekdays" ? (
            <input type="time" value={scheduleTime} onChange={(event) => setScheduleTime(event.target.value)} style={inputStyle} aria-label="运行时间" />
          ) : (
            <SelectMenu value={timezone} onValueChange={setTimezone} ariaLabel="时区">
              {timezoneOptions.map((item) => <option value={item} key={item}>{item}</option>)}
            </SelectMenu>
          )}
        </div>
        {(schedulePreset === "daily" || schedulePreset === "weekdays") && (
          <SelectMenu value={timezone} onValueChange={setTimezone} ariaLabel="时区">
            {timezoneOptions.map((item) => <option value={item} key={item}>{item}</option>)}
          </SelectMenu>
        )}
        <div className="scheduler-schedule-row grid grid-cols-2 gap-1.5">
          <SelectMenu value={isolation} onValueChange={(value) => setIsolation(value as "worktree" | "workspace")} ariaLabel="运行工作区">
            <option value="worktree">独立 Worktree</option>
            <option value="workspace">当前项目</option>
          </SelectMenu>
          <SelectMenu value={taskMode} onValueChange={(value) => setTaskMode(value as "standalone" | "heartbeat")} ariaLabel="对话模式">
            <option value="standalone">每次新建对话</option>
            <option value="heartbeat" disabled={!conversationId}>继续当前对话</option>
          </SelectMenu>
        </div>
        <div className="scheduler-schedule-row flex gap-1.5 items-center">
          {schedulePreset === "custom" ? (
            <input
              placeholder="Cron 表达式（例如 0 9 * * 1-5）"
              value={newTaskSchedule}
              onChange={(e) => setNewTaskSchedule(e.target.value)}
              className="flex-1 text-xs"
              style={inputStyle}
            />
          ) : <span className="flex-1 text-[11px]" style={{ color: "var(--text-muted)", fontFamily: "var(--font-ui)", fontSize: "var(--mc-font-caption)" }}>{effectiveSchedule}</span>}
          <button
            onClick={() => void addTask()}
            disabled={!newTaskName.trim() || !newTaskPrompt.trim() || !effectiveSchedule.trim() || addingTask}
            style={{ ...secondaryActionStyle, display: "inline-flex", alignItems: "center", gap: 7 }}
          >
            <Plus size={14} /> {addingTask ? "正在添加…" : "添加"}
          </button>
        </div>
      </div>
    </Section>
  );
};
