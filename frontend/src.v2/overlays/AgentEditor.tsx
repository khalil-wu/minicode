import { useCallback, useEffect, useState } from "react";
import { Bot, Plus, Save, Trash2, X } from "lucide-react";
import { useFocusTrap } from "../hooks/useFocusTrap";
import { useAppStore } from "../stores";
import { apiBase, authHeaders } from "../protocol/api";
import { pushToast } from "./ToastContainer";

// Agent editor — CRUD over user-defined subagent roles (backed by
// /api/agents). Each agent is a markdown file (frontmatter + system-prompt
// body) that becomes a selectable subagent_type in the Task tool.

interface AgentRecord {
  name: string;
  description: string;
  prompt: string;
  model: string;
  tools: string[];
  disallowed_tools: string[];
  source_path: string | null;
}

const EMPTY_DRAFT: AgentRecord = {
  name: "",
  description: "",
  prompt: "",
  model: "",
  tools: [],
  disallowed_tools: [],
  source_path: null,
};

const parseList = (value: string): string[] =>
  value
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);

export const AgentEditor = () => {
  const open = useAppStore((s) => s.agentEditorOpen);
  const toggle = useAppStore((s) => s.toggleAgentEditor);
  const [agents, setAgents] = useState<AgentRecord[]>([]);
  const [draft, setDraft] = useState<AgentRecord>(EMPTY_DRAFT);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const dialogRef = useFocusTrap(open);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const res = await fetch(`${apiBase()}/api/agents`, {
        cache: "no-store",
        headers: authHeaders(),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      setAgents(Array.isArray(data.agents) ? data.agents : []);
    } catch (err) {
      pushToast(`Failed to load agents: ${err instanceof Error ? err.message : err}`, "error");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (open) {
      void load();
      setDraft(EMPTY_DRAFT);
    }
  }, [open, load]);

  if (!open) return null;

  const editExisting = (a: AgentRecord) => setDraft({ ...a });
  const newDraft = () => setDraft(EMPTY_DRAFT);

  const save = async () => {
    const name = draft.name.trim();
    if (!name) {
      pushToast("Agent name is required", "warning");
      return;
    }
    setSaving(true);
    try {
      const res = await fetch(`${apiBase()}/api/agents`, {
        method: "POST",
        headers: { ...authHeaders(), "Content-Type": "application/json" },
        body: JSON.stringify({
          name,
          description: draft.description,
          prompt: draft.prompt,
          model: "",
          tools: draft.tools,
          disallowed_tools: draft.disallowed_tools,
        }),
      });
      if (!res.ok) {
        const detail = await res.json().catch(() => null);
        throw new Error(detail?.detail || `HTTP ${res.status}`);
      }
      pushToast(`Saved agent "${name}"`, "success");
      await load();
    } catch (err) {
      pushToast(`Save failed: ${err instanceof Error ? err.message : err}`, "error");
    } finally {
      setSaving(false);
    }
  };

  const remove = async (name: string) => {
    try {
      const res = await fetch(`${apiBase()}/api/agents/${encodeURIComponent(name)}`, {
        method: "DELETE",
        headers: authHeaders(),
      });
      if (!res.ok) {
        const detail = await res.json().catch(() => null);
        throw new Error(detail?.detail || `HTTP ${res.status}`);
      }
      pushToast(`Deleted "${name}"`, "success");
      if (draft.name === name) setDraft(EMPTY_DRAFT);
      await load();
    } catch (err) {
      pushToast(`Delete failed: ${err instanceof Error ? err.message : err}`, "error");
    }
  };

  const inputStyle: React.CSSProperties = {
    width: "100%",
    padding: "8px 10px",
    borderRadius: 8,
    border: "1px solid var(--border-subtle)",
    background: "var(--surface-base)",
    color: "var(--text-primary)",
    fontSize: "var(--text-chrome)",
    boxSizing: "border-box",
  };
  const labelStyle: React.CSSProperties = {
    fontSize: "var(--text-xxs)",
    color: "var(--text-secondary)",
    marginBottom: 4,
    display: "block",
  };

  return (
    <div
      className="overlay-backdrop"
      onClick={() => toggle()}
      style={{
        position: "fixed",
        inset: 0,
        background: "var(--backdrop-overlay)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        zIndex: "var(--z-modal)",
        pointerEvents: "auto",
      }}
    >
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-label="Agent editor"
        tabIndex={-1}
        className="modal-content agent-editor"
        onClick={(e) => e.stopPropagation()}
        onKeyDown={(e) => {
          if (e.key === "Escape") {
            e.preventDefault();
            e.stopPropagation();
            toggle();
          }
        }}
        style={{
          width: "min(860px, 100%)",
          height: "min(680px, 90vh)",
          background: "var(--surface-raised)",
          border: "1px solid var(--border-subtle)",
          borderRadius: "var(--radius-md, 12px)",
          boxShadow: "var(--shadow-strong, var(--shadow-md))",
          display: "flex",
          flexDirection: "column",
          overflow: "hidden",
        }}
      >
        <div
          className="agent-editor-header"
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            padding: "14px 16px",
            borderBottom: "1px solid var(--border-subtle)",
          }}
        >
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <Bot size={16} />
            <h2 style={{ margin: 0, fontSize: 15, color: "var(--text-primary)" }}>Agent Editor</h2>
          </div>
          <button
            type="button"
            onClick={() => toggle()}
            aria-label="Close agent editor"
            className="mc-icon-button agent-editor-close"
          >
            <X size={16} />
          </button>
        </div>

        <div className="agent-editor-body" style={{ display: "flex", flex: 1, minHeight: 0 }}>
          {/* Left: agent list */}
          <div
            className="agent-editor-sidebar"
            style={{
              width: 240,
              borderRight: "1px solid var(--border-subtle)",
              display: "flex",
              flexDirection: "column",
              minHeight: 0,
            }}
          >
            <button
              type="button"
              onClick={newDraft}
              style={{
                display: "flex",
                alignItems: "center",
                gap: 6,
                margin: "10px",
                padding: "8px 10px",
                borderRadius: 8,
                border: "1px dashed var(--border-subtle)",
                background: "transparent",
                color: "var(--text-primary)",
                cursor: "pointer",
                fontSize: "var(--text-chrome)",
              }}
            >
              <Plus size={14} /> New agent
            </button>
            <div style={{ overflowY: "auto", flex: 1, padding: "0 10px 10px" }}>
              {loading && <div style={{ color: "var(--text-muted)", fontSize: "var(--text-xxs)", padding: 8 }}>Loading…</div>}
              {!loading && agents.length === 0 && (
                <div style={{ color: "var(--text-muted)", fontSize: "var(--text-xxs)", padding: 8 }}>No custom agents</div>
              )}
              {agents.map((a) => (
                <div
                  key={a.name}
                  style={{
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "space-between",
                    gap: 6,
                    padding: "8px 10px",
                    borderRadius: 8,
                    cursor: "pointer",
                    background: draft.name === a.name ? "var(--surface-active)" : "transparent",
                  }}
                  onClick={() => editExisting(a)}
                >
                  <div style={{ minWidth: 0 }}>
                    <div
                      style={{
                        fontSize: "var(--text-chrome)",
                        color: "var(--text-primary)",
                        overflow: "hidden",
                        textOverflow: "ellipsis",
                        whiteSpace: "nowrap",
                      }}
                    >
                      {a.name}
                    </div>
                    {a.description && (
                      <div
                        style={{
                          fontSize: "var(--text-2xs)",
                          color: "var(--text-muted)",
                          overflow: "hidden",
                          textOverflow: "ellipsis",
                          whiteSpace: "nowrap",
                        }}
                      >
                        {a.description}
                      </div>
                    )}
                  </div>
                  <button
                    type="button"
                    onClick={(e) => {
                      e.stopPropagation();
                      void remove(a.name);
                    }}
                    aria-label={`Delete ${a.name}`}
                    className="mc-icon-button mc-icon-button-compact mc-icon-button-danger"
                    style={{ flexShrink: 0 }}
                  >
                    <Trash2 size={14} />
                  </button>
                </div>
              ))}
            </div>
          </div>

          {/* Right: editor form */}
          <div className="agent-editor-form" style={{ flex: 1, overflowY: "auto", padding: "16px", display: "flex", flexDirection: "column", gap: 12 }}>
            <div>
              <label style={labelStyle}>Name</label>
              <input
                style={inputStyle}
                value={draft.name}
                onChange={(e) => setDraft((d) => ({ ...d, name: e.target.value }))}
                placeholder="reviewer"
              />
            </div>
            <div>
              <label style={labelStyle}>Description</label>
              <input
                style={inputStyle}
                value={draft.description}
                onChange={(e) => setDraft((d) => ({ ...d, description: e.target.value }))}
                placeholder="What this agent is best at"
              />
            </div>
            <div style={{ display: "flex", gap: 12 }}>
              <div style={{ flex: 1 }}>
                <label style={labelStyle}>Model</label>
                <input
                  style={{ ...inputStyle, opacity: 0.72, cursor: "not-allowed" }}
                  value="Inherit from session"
                  disabled
                  aria-describedby="agent-model-help"
                />
                <div id="agent-model-help" style={{ color: "var(--text-muted)", fontSize: "var(--text-2xs)", marginTop: 5 }}>
                  Per-agent model overrides are not active yet. This agent uses the current session model.
                </div>
              </div>
            </div>
            <div>
              <label style={labelStyle}>Allowed tools</label>
              <input
                style={inputStyle}
                value={draft.tools.join(", ")}
                onChange={(e) => setDraft((d) => ({ ...d, tools: parseList(e.target.value) }))}
                placeholder="read_file, grep_files, run_command"
              />
            </div>
            <div>
              <label style={labelStyle}>Disallowed tools</label>
              <input
                style={inputStyle}
                value={draft.disallowed_tools.join(", ")}
                onChange={(e) => setDraft((d) => ({ ...d, disallowed_tools: parseList(e.target.value) }))}
                placeholder="write_file"
              />
            </div>
            <div style={{ flex: 1, display: "flex", flexDirection: "column", minHeight: 160 }}>
              <label style={labelStyle}>System prompt</label>
              <textarea
                value={draft.prompt}
                onChange={(e) => setDraft((d) => ({ ...d, prompt: e.target.value }))}
                placeholder="You are a focused code reviewer. ..."
                style={{
                  ...inputStyle,
                  flex: 1,
                  minHeight: 140,
                  resize: "vertical",
                  fontFamily: "var(--font-mono, monospace)",
                  lineHeight: 1.5,
                }}
              />
            </div>
            <div style={{ display: "flex", justifyContent: "flex-end", gap: 8 }}>
              <button
                type="button"
                onClick={save}
                disabled={saving}
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 6,
                  padding: "9px 16px",
                  borderRadius: 8,
                  border: 0,
                  background: "var(--text-primary)",
                  color: "var(--surface-base)",
                  cursor: saving ? "default" : "pointer",
                  fontSize: "var(--text-chrome)",
                  fontWeight: 600,
                  opacity: saving ? 0.6 : 1,
                }}
              >
                <Save size={14} /> {saving ? "Saving…" : "Save"}
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
};
