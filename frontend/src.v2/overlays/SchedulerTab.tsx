import { useMemo, useState } from "react";
import { History, MessageSquareText, Play, Plus, RotateCcw, Square, Trash2 } from "lucide-react";
import { useAppStore } from "../stores";
import { sendClientCommand } from "../protocol/ws-outbox";
import { Section, inputStyle, secondaryActionStyle } from "./settingsShared";

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
  const requestConversationSwitch = useAppStore((s) => s.requestConversationSwitch);
  const [newTaskName, setNewTaskName] = useState("");
  const [newTaskPrompt, setNewTaskPrompt] = useState("");
  const [newTaskSchedule, setNewTaskSchedule] = useState("0 * * * *");
  const [schedulePreset, setSchedulePreset] = useState<SchedulePreset>("hourly");
  const [scheduleTime, setScheduleTime] = useState("09:00");
  const [timezone, setTimezone] = useState(localTimezone);
  const [isolation, setIsolation] = useState<"worktree" | "workspace">("worktree");
  const [taskMode, setTaskMode] = useState<"standalone" | "heartbeat">("standalone");
  const effectiveSchedule = useMemo(
    () => presetSchedule(schedulePreset, scheduleTime, newTaskSchedule),
    [newTaskSchedule, schedulePreset, scheduleTime],
  );

  return (
    <Section title={title} description={description}>
      {scheduledTasks.length > 0 && (
        <div className="flex flex-col gap-1.5">
          {scheduledTasks.map((t) => (
            <div key={t.id} className="scheduler-task-row flex items-center gap-2 px-2.5 py-1.5 rounded" style={{ background: "var(--bg-secondary)" }}>
              <span className="w-2 h-2 rounded-full shrink-0" style={{ background: t.enabled ? "var(--state-success)" : "var(--text-muted)" }} />
              <div className="flex-1 min-w-0">
                <div className="font-medium" style={{ fontSize: "var(--text-sm)", color: "var(--text-primary)" }}>{t.name}</div>
                <div className="text-[11px]" style={{ color: "var(--text-muted)" }}>
                  <span style={{ fontFamily: "var(--font-mono)" }}>{t.schedule}</span>
                  <span> · {t.timezone || "UTC"} · {t.isolation === "workspace" ? "当前项目" : "独立 Worktree"}</span>
                </div>
              </div>
              <div className="hidden md:flex flex-col items-end gap-0.5 text-[10px]" style={{ color: "var(--text-muted)" }}>
                {t.next_run_at && <span>下次运行：{new Date(t.next_run_at).toLocaleString()}</span>}
                {t.last_run_at && <span>上次运行：{new Date(t.last_run_at).toLocaleString()}</span>}
              </div>
              <button
                onClick={() => sendClientCommand({ type: "scheduler.toggle", task_id: t.id, enabled: !t.enabled })}
                className="px-1.5 py-0.5 text-[11px]"
                style={secondaryActionStyle}
              >
                {t.enabled ? "停用" : "启用"}
              </button>
              <button
                onClick={() => sendClientCommand({ type: "scheduler.run_now", task_id: t.id })}
                className="mc-icon-button mc-icon-button-compact"
                aria-label={`立即运行 ${t.name}`}
                title="立即运行"
              >
                <Play size={14} />
              </button>
              <button
                onClick={() => sendClientCommand({ type: "scheduler.remove", task_id: t.id })}
                className="mc-icon-button mc-icon-button-compact mc-icon-button-danger"
                aria-label={`删除 ${t.name}`}
                title="删除"
              >
                <Trash2 size={14} />
              </button>
            </div>
          ))}
        </div>
      )}
      {scheduledTasks.length === 0 && <div className="text-xs" style={{ color: "var(--text-muted)" }}>尚未配置定时任务。</div>}
      {scheduledTaskRuns.length > 0 && (
        <div className="flex flex-col gap-1 mt-2">
          <div className="flex items-center gap-1.5" style={{ color: "var(--text-muted)", fontSize: "var(--text-xs)" }}>
            <History size={14} /> 最近运行
          </div>
          {scheduledTaskRuns.slice(0, 8).map((run) => {
            const task = scheduledTasks.find((item) => item.id === run.task_id);
            const running = run.status === "pending" || run.status === "running";
            return (
              <div key={run.id} className="flex items-center gap-2 px-2.5 py-1.5 rounded" style={{ background: "var(--surface-soft)" }}>
                <span className="w-1.5 h-1.5 rounded-full" style={{ background: running ? "var(--accent-primary)" : run.status === "completed" ? "var(--state-success)" : run.status === "partial" ? "var(--state-warning)" : "var(--state-danger)" }} />
                <div className="flex-1 min-w-0">
                  <div className="truncate" style={{ color: "var(--text-secondary)", fontSize: "var(--text-xs)" }}>{task?.name ?? "计划任务"}</div>
                  <div className="truncate" title={run.error || run.result_summary || runStatusLabel(run.status)} style={{ color: "var(--text-muted)", fontSize: "var(--text-3xs)" }}>{run.error || run.result_summary || runStatusLabel(run.status)}</div>
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
                  <button onClick={() => sendClientCommand({ type: "scheduler.cancel", run_id: run.id })} className="mc-icon-button mc-icon-button-compact" title="取消运行" aria-label="取消运行"><Square size={14} /></button>
                ) : (
                  <button onClick={() => sendClientCommand({ type: "scheduler.retry", run_id: run.id })} className="mc-icon-button mc-icon-button-compact" title="重试" aria-label="重试"><RotateCcw size={14} /></button>
                )}
              </div>
            );
          })}
        </div>
      )}
      <div className="flex flex-col gap-1.5 mt-2.5 p-2.5 rounded-md" style={{ background: "var(--surface-soft)" }}>
        <div className="mb-0.5" style={{ fontSize: "var(--text-xs)", color: "var(--text-muted)" }}>添加定时任务</div>
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
          style={{ ...inputStyle, fontFamily: "var(--font-mono)" }}
        />
        <div className="scheduler-schedule-row grid grid-cols-2 gap-1.5">
          <select value={schedulePreset} onChange={(event) => setSchedulePreset(event.target.value as SchedulePreset)} style={inputStyle} aria-label="运行频率">
            <option value="hourly">每小时</option>
            <option value="daily">每天</option>
            <option value="weekdays">工作日</option>
            <option value="custom">自定义 Cron</option>
          </select>
          {schedulePreset === "daily" || schedulePreset === "weekdays" ? (
            <input type="time" value={scheduleTime} onChange={(event) => setScheduleTime(event.target.value)} style={inputStyle} aria-label="运行时间" />
          ) : (
            <select value={timezone} onChange={(event) => setTimezone(event.target.value)} style={inputStyle} aria-label="时区">
              {timezoneOptions.map((item) => <option value={item} key={item}>{item}</option>)}
            </select>
          )}
        </div>
        {(schedulePreset === "daily" || schedulePreset === "weekdays") && (
          <select value={timezone} onChange={(event) => setTimezone(event.target.value)} style={inputStyle} aria-label="时区">
            {timezoneOptions.map((item) => <option value={item} key={item}>{item}</option>)}
          </select>
        )}
        <div className="scheduler-schedule-row grid grid-cols-2 gap-1.5">
          <select value={isolation} onChange={(event) => setIsolation(event.target.value as "worktree" | "workspace")} style={inputStyle} aria-label="运行工作区">
            <option value="worktree">独立 Worktree</option>
            <option value="workspace">当前项目</option>
          </select>
          <select value={taskMode} onChange={(event) => setTaskMode(event.target.value as "standalone" | "heartbeat")} style={inputStyle} aria-label="对话模式">
            <option value="standalone">每次新建对话</option>
            <option value="heartbeat" disabled={!conversationId}>继续当前对话</option>
          </select>
        </div>
        <div className="scheduler-schedule-row flex gap-1.5 items-center">
          {schedulePreset === "custom" ? (
            <input
              placeholder="Cron 表达式（例如 0 9 * * 1-5）"
              value={newTaskSchedule}
              onChange={(e) => setNewTaskSchedule(e.target.value)}
              className="flex-1 text-xs"
              style={{ ...inputStyle, fontFamily: "var(--font-mono)" }}
            />
          ) : <span className="flex-1 text-[11px]" style={{ color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>{effectiveSchedule}</span>}
          <button
            onClick={() => {
              if (!newTaskName || !newTaskPrompt || !effectiveSchedule) return;
              sendClientCommand({
                type: "scheduler.add",
                name: newTaskName,
                prompt: newTaskPrompt,
                schedule: effectiveSchedule,
                timezone,
                isolation,
                conversation_id: taskMode === "heartbeat" ? conversationId ?? undefined : undefined,
              });
              setNewTaskName("");
              setNewTaskPrompt("");
              setNewTaskSchedule("0 * * * *");
            }}
            disabled={!newTaskName || !newTaskPrompt || !effectiveSchedule}
            style={{ ...secondaryActionStyle, display: "inline-flex", alignItems: "center", gap: 7 }}
          >
            <Plus size={14} /> 添加
          </button>
        </div>
      </div>
    </Section>
  );
};
