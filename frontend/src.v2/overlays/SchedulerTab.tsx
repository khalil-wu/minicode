import { useState } from "react";
import { Plus, Trash2 } from "lucide-react";
import { useAppStore } from "../stores";
import { sendClientCommand } from "../protocol/ws-outbox";
import { Section, inputStyle, secondaryActionStyle } from "./settingsShared";

export const SchedulerTab = ({
  title = "Automations",
  description = "",
}: {
  title?: string;
  description?: string;
}) => {
  const scheduledTasks = useAppStore((s) => s.scheduledTasks);
  const [newTaskName, setNewTaskName] = useState("");
  const [newTaskPrompt, setNewTaskPrompt] = useState("");
  const [newTaskSchedule, setNewTaskSchedule] = useState("0 * * * *");

  return (
    <Section title={title} description={description}>
      {scheduledTasks.length > 0 && (
        <div className="flex flex-col gap-1.5">
          {scheduledTasks.map((t) => (
            <div key={t.id} className="scheduler-task-row flex items-center gap-2 px-2.5 py-1.5 rounded" style={{ background: "var(--bg-secondary)" }}>
              <span className="w-2 h-2 rounded-full shrink-0" style={{ background: t.enabled ? "var(--state-success)" : "var(--text-muted)" }} />
              <div className="flex-1 min-w-0">
                <div className="font-medium" style={{ fontSize: "var(--text-sm)", color: "var(--text-primary)" }}>{t.name}</div>
                <div className="text-[11px]" style={{ color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>{t.schedule}</div>
              </div>
              <div className="hidden md:flex flex-col items-end gap-0.5 text-[10px]" style={{ color: "var(--text-muted)" }}>
                {t.next_run_at && <span>Next: {new Date(t.next_run_at).toLocaleString()}</span>}
                {t.last_run_at && <span>Last: {new Date(t.last_run_at).toLocaleString()}</span>}
              </div>
              <button
                onClick={() => sendClientCommand({ type: "scheduler.toggle", task_id: t.id, enabled: !t.enabled })}
                className="px-1.5 py-0.5 text-[11px]"
                style={secondaryActionStyle}
              >
                {t.enabled ? "Disable" : "Enable"}
              </button>
              <button
                onClick={() => sendClientCommand({ type: "scheduler.remove", task_id: t.id })}
                className="mc-icon-button mc-icon-button-compact mc-icon-button-danger"
                aria-label={`Remove ${t.name}`}
                title="Remove"
              >
                <Trash2 size={14} />
              </button>
            </div>
          ))}
        </div>
      )}
      {scheduledTasks.length === 0 && <div className="text-xs" style={{ color: "var(--text-muted)" }}>No automations configured.</div>}
      <div className="flex flex-col gap-1.5 mt-2.5 p-2.5 rounded-md" style={{ background: "var(--surface-soft)" }}>
        <div className="mb-0.5" style={{ fontSize: "var(--text-xs)", color: "var(--text-muted)" }}>Add Automation</div>
        <input
          placeholder="Task name"
          value={newTaskName}
          onChange={(e) => setNewTaskName(e.target.value)}
          style={inputStyle}
        />
        <textarea
          placeholder="Prompt to run"
          value={newTaskPrompt}
          onChange={(e) => setNewTaskPrompt(e.target.value)}
          rows={3}
          className="resize-y text-xs"
          style={{ ...inputStyle, fontFamily: "var(--font-mono)" }}
        />
        <div className="scheduler-schedule-row flex gap-1.5 items-center">
          <input
            placeholder="Cron (e.g. 0 9 * * 1-5)"
            value={newTaskSchedule}
            onChange={(e) => setNewTaskSchedule(e.target.value)}
            className="flex-1 text-xs"
            style={{ ...inputStyle, fontFamily: "var(--font-mono)" }}
          />
          <button
            onClick={() => {
              if (!newTaskName || !newTaskPrompt || !newTaskSchedule) return;
              sendClientCommand({ type: "scheduler.add", name: newTaskName, prompt: newTaskPrompt, schedule: newTaskSchedule });
              setNewTaskName("");
              setNewTaskPrompt("");
              setNewTaskSchedule("0 * * * *");
            }}
            disabled={!newTaskName || !newTaskPrompt}
            style={{ ...secondaryActionStyle, display: "inline-flex", alignItems: "center", gap: 7 }}
          >
            <Plus size={14} /> Add
          </button>
        </div>
      </div>
    </Section>
  );
};
