import { useMemo } from "react";
import { Calendar, FolderOpen, Image, ListChecks, Sparkles, Shuffle } from "lucide-react";
import { useAppStore } from "../stores";
import { Composer } from "../composer/Composer";
import { workspaceDisplayName } from "../lib/workspace-display";

interface SuggestedTask {
  id: string;
  label: string;
  prompt: string;
  icon: React.ReactNode;
}

const SUGGESTED_TASKS: SuggestedTask[] = [
  {
    id: "optimize-week",
    label: "Optimize my week",
    prompt: "Help me plan and optimize my week. Ask me what's on my plate, then propose a focused schedule.",
    icon: <Calendar size={18} />,
  },
  {
    id: "organize-screenshots",
    label: "Organize my screenshots",
    prompt: "Help me organize my screenshots folder: scan it, group related images, and propose a clean naming scheme.",
    icon: <Image size={18} />,
  },
  {
    id: "review-changes",
    label: "Review my changes",
    prompt: "Review the uncommitted changes in my current workspace and summarize what changed, with anything risky flagged.",
    icon: <ListChecks size={18} />,
  },
];

/**
 * Empty-state landing page for Cowork mode, shown when the active conversation
 * has no messages yet. Mirrors the reference Cowork home: a large headline, the
 * full Composer (so `/` skills, model picker and attachments all work), a
 * project chip, and a few one-tap suggested tasks.
 */
export const CoworkHome = () => {
  const workingDirectory = useAppStore((s) => s.workingDirectory);
  const setDraft = useAppStore((s) => s.setDraft);

  const projectLabel = useMemo(
    () => (workingDirectory ? workspaceDisplayName(workingDirectory) : null),
    [workingDirectory],
  );

  const pickRandomTask = () => {
    const task = SUGGESTED_TASKS[Math.floor(Math.random() * SUGGESTED_TASKS.length)];
    setDraft(task.prompt);
  };

  return (
    <div
      className="flex flex-1 min-h-0 flex-col overflow-y-auto"
      style={{
        display: "flex",
        flex: 1,
        minHeight: 0,
        flexDirection: "column",
        overflowY: "auto",
        background: "var(--surface-base)",
      }}
    >
      <div
        style={{
          flex: 1,
          minHeight: 0,
          display: "flex",
          flexDirection: "column",
          justifyContent: "center",
          width: "100%",
          maxWidth: 820,
          margin: "0 auto",
          padding: "48px 24px 24px",
          boxSizing: "border-box",
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 6 }}>
          <Sparkles size={22} style={{ color: "var(--accent-primary)", flexShrink: 0 }} />
          <h1
            style={{
              margin: 0,
              fontSize: "var(--text-2xl, 28px)",
              fontWeight: 500,
              color: "var(--text-primary)",
              lineHeight: 1.2,
            }}
          >
            What can I help you get done?
          </h1>
        </div>
        <p
          style={{
            margin: "0 0 24px 32px",
            fontSize: "var(--text-sm)",
            color: "var(--text-muted)",
            lineHeight: "var(--leading-relaxed)",
          }}
        >
          Type <kbd style={kbdStyle}>/</kbd> to use a skill, or describe the task in plain English.
        </p>

        <Composer minimal />

        {projectLabel && (
          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: 8,
              margin: "10px 0 0",
              fontSize: "var(--text-sm)",
              color: "var(--text-muted)",
            }}
          >
            <FolderOpen size={14} style={{ flexShrink: 0 }} />
            <span>Workspace</span>
            <span
              style={{
                color: "var(--text-secondary)",
                fontFamily: "var(--font-mono)",
                overflow: "hidden",
                textOverflow: "ellipsis",
                whiteSpace: "nowrap",
              }}
              title={workingDirectory}
            >
              {projectLabel}
            </span>
          </div>
        )}

        <div style={{ marginTop: 40 }}>
          <button
            type="button"
            onClick={pickRandomTask}
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: 8,
              marginBottom: 14,
              padding: 0,
              border: 0,
              background: "transparent",
              color: "var(--text-muted)",
              fontSize: "var(--text-sm)",
              cursor: "pointer",
            }}
          >
            <Shuffle size={15} />
            Try a suggested task
          </button>

          <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
            {SUGGESTED_TASKS.map((task) => (
              <button
                key={task.id}
                type="button"
                onClick={() => setDraft(task.prompt)}
                style={suggestedTaskStyle}
                onMouseEnter={(e) => {
                  e.currentTarget.style.background = "var(--surface-soft)";
                }}
                onMouseLeave={(e) => {
                  e.currentTarget.style.background = "transparent";
                }}
              >
                <span style={{ color: "var(--text-muted)", flexShrink: 0 }}>{task.icon}</span>
                <span style={{ color: "var(--text-primary)", fontSize: "var(--text-md)" }}>{task.label}</span>
              </button>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
};

const kbdStyle: React.CSSProperties = {
  display: "inline-block",
  padding: "1px 6px",
  borderRadius: 4,
  border: "1px solid var(--border-subtle)",
  background: "var(--surface-soft)",
  fontFamily: "var(--font-mono)",
  fontSize: "0.85em",
};

const suggestedTaskStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 14,
  width: "100%",
  padding: "12px 14px",
  border: 0,
  borderRadius: "var(--radius-md, 8px)",
  background: "transparent",
  cursor: "pointer",
  textAlign: "left",
  transition: "background 120ms ease",
};
