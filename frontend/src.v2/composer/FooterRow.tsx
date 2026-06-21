import { ArrowUp, Check, ChevronDown, FolderOpen, Plus, ShieldCheck, Square } from "lucide-react";
import { memo, useEffect, useId, useRef, useState } from "react";
import type { SendButtonState } from "../lib/send-state";
import { useAppStore } from "../stores";
import { getWebSocket } from "../hooks/useWebSocket";
import { sendClientCommand } from "../protocol/ws-outbox";
import { uploadComposerFiles } from "./uploads";
import type { EffortLevel, PermissionMode } from "../stores/types";
import { formatModelLabel } from "../lib/model-label";
import { UsageRing } from "../shell/UsageRing";
import { branchDisplayName, workspaceDisplayName } from "../lib/workspace-display";

interface Props {
  sendState: SendButtonState;
  onSend: () => void | Promise<void>;
  compact?: boolean;
  /**
   * Minimal mode keeps the full-size textarea but hides the secondary control
   * row clutter (context ring and token usage).
   * Used by the Cowork landing page composer.
   */
  minimal?: boolean;
}

// Codex-style picker: three choices only. Plan mode is still supported by the
// backend and surfaced via the current-mode label when the agent enters it
// (EnterPlanMode tool), but it is not a user-selectable picker option.
const PERMISSION_MODES: { id: PermissionMode; label: string; desc: string }[] = [
  { id: "ask_permissions", label: "Ask", desc: "Ask before file and network actions" },
  { id: "auto", label: "Auto", desc: "Auto read, search, and edit workspace files" },
  { id: "bypass", label: "Full access", desc: "Use files, network, edits, and commands without prompts" },
];

const EFFORT_OPTIONS: { id: EffortLevel; label: string; desc: string }[] = [
  { id: "low", label: "Low", desc: "Fastest pass" },
  { id: "medium", label: "Med", desc: "Balanced reasoning" },
  { id: "high", label: "High", desc: "Default coding depth" },
  { id: "max", label: "Max", desc: "Deepest reasoning" },
];


export const FooterRow = memo(({ sendState, onSend, compact = false, minimal = false }: Props) => {
  const permissionMode = useAppStore((s) => s.permissionMode);
  const effortLevel = useAppStore((s) => s.effortLevel);
  const currentModel = useAppStore((s) => s.currentModel);
  const availableModels = useAppStore((s) => s.availableModels);
  const workingDirectory = useAppStore((s) => s.workingDirectory);
  const workspaceGit = useAppStore((s) => s.workspaceGit);
  const prMonitor = useAppStore((s) => s.prMonitor);
  const setPermissionMode = useAppStore((s) => s.setPermissionMode);
  const setEffortLevel = useAppStore((s) => s.setEffortLevel);
  const fileInputId = useId();
  const fileRef = useRef<HTMLInputElement>(null);
  const [modelOpen, setModelOpen] = useState(false);
  const [permissionOpen, setPermissionOpen] = useState(false);
  const [effortOpen, setEffortOpen] = useState(false);
  const dropdownRef = useRef<HTMLDivElement>(null);
  const permissionRef = useRef<HTMLDivElement>(null);
  const effortRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const close = (e: MouseEvent) => {
      if (modelOpen && dropdownRef.current && !dropdownRef.current.contains(e.target as Node)) setModelOpen(false);
      if (permissionOpen && permissionRef.current && !permissionRef.current.contains(e.target as Node)) setPermissionOpen(false);
      if (effortOpen && effortRef.current && !effortRef.current.contains(e.target as Node)) setEffortOpen(false);
    };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, [modelOpen, permissionOpen, effortOpen]);

  useEffect(() => {
    const openModelMenu = () => {
      setPermissionOpen(false);
      setEffortOpen(false);
      setModelOpen(true);
    };
    const openPermissionMenu = () => {
      setModelOpen(false);
      setEffortOpen(false);
      setPermissionOpen(true);
    };
    document.addEventListener("open-model-menu", openModelMenu);
    document.addEventListener("open-permission-menu", openPermissionMenu);
    return () => {
      document.removeEventListener("open-model-menu", openModelMenu);
      document.removeEventListener("open-permission-menu", openPermissionMenu);
    };
  }, []);

  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = e.target.files;
    if (!files) return;
    uploadComposerFiles(Array.from(files));
    e.target.value = "";
  };

  const openFilePicker = (event?: React.MouseEvent | React.KeyboardEvent) => {
    event?.preventDefault();
    event?.stopPropagation();
    fileRef.current?.click();
  };

  const switchModel = (model: string) => {
    sendClientCommand({ type: "llm.model.set", model });
    setModelOpen(false);
  };

  const switchEffort = (level: EffortLevel) => {
    setEffortLevel(level);
    setEffortOpen(false);
  };

  const switchPermissionMode = (mode: PermissionMode) => {
    setPermissionMode(mode);
    setPermissionOpen(false);
  };

  const togglePRAutomation = async (key: "autoFix" | "autoMerge") => {
    if (!prMonitor) return;
    const next = !prMonitor[key];
    if (next) {
      const label = key === "autoFix" ? "Auto-fix" : "Auto-merge";
      const { showConfirm } = await import("../overlays/DialogService");
      const ok = await showConfirm({
        title: `Enable ${label}`,
        message: `${label} can trigger repository changes automatically. Enable it?`,
        confirmLabel: "Enable",
      });
      if (!ok) return;
    }
    useAppStore.getState().setPRMonitor({ ...prMonitor, [key]: next });
  };

  const modelLabel = formatModelLabel(currentModel, "Select model");
  const disabledReason = !currentModel.trim() ? "Select a model before sending" : undefined;

  const permLabel = permissionLabel(permissionMode);
  const effortLabel = effortOption(effortLevel).label;
  const workspaceLabel = workspaceDisplayName(workingDirectory, "Work in a project");
  const branchLabel = branchDisplayName(workspaceGit?.branch);
  const workspaceTitle = [workingDirectory || workspaceLabel, branchLabel ? `branch: ${branchLabel}` : ""]
    .filter(Boolean)
    .join("\n");

  return (
    <div className="flex flex-col gap-0">
      {prMonitor && (
        <div style={prMonitorStyle(prMonitor.ciStatus)}>
          <span style={prDotStyle(prMonitor.ciStatus)} />
          <span className="font-semibold" style={{ color: "var(--text-primary)" }}>PR #{prMonitor.prNumber}</span>
          <span style={{ color: "var(--text-muted)" }}>CI: {prMonitor.ciStatus}</span>
          {prMonitor.failedChecks && prMonitor.failedChecks.length > 0 && (
            <span style={{ color: "var(--state-danger)" }}>{prMonitor.failedChecks.length} failed</span>
          )}
          <span className="flex-1" />
          <ToggleChip
            label="Auto-fix"
            active={prMonitor.autoFix}
            onClick={() => togglePRAutomation("autoFix")}
          />
          <ToggleChip
            label="Auto-merge"
            active={prMonitor.autoMerge}
            onClick={() => togglePRAutomation("autoMerge")}
          />
        </div>
      )}

      <div className="composer-footer" style={footerStyle(compact, minimal)}>
        <input
          id={fileInputId}
          ref={fileRef}
          type="file"
          multiple
          className="absolute w-px h-px p-0 -m-px overflow-hidden whitespace-nowrap border-0"
          style={{ clip: "rect(0 0 0 0)" }}
          tabIndex={-1}
          aria-hidden="true"
          onChange={handleFileChange}
        />
        <div style={footerLeftStyle}>
          <button
            type="button"
            title="Attach file"
            className="composer-attach-btn"
            style={iconBtn(compact)}
            aria-label="Attach file"
            onClick={openFilePicker}
            onKeyDown={(event) => {
              if (event.key !== "Enter" && event.key !== " ") return;
              openFilePicker(event);
            }}
          >
            <Plus size={compact ? 16 : 18} strokeWidth={1.8} />
          </button>

          <Picker
            refEl={permissionRef}
            open={permissionOpen}
            setOpen={(open) => {
              if (open) {
                setModelOpen(false);
                setEffortOpen(false);
              }
              setPermissionOpen(open);
            }}
            label={permLabel}
            title={`Permission: ${permLabel}`}
            tone={permissionTone(permissionMode)}
            icon={<ShieldCheck size={12} />}
          >
            {PERMISSION_MODES.map((mode) => (
              <MenuChoice
                key={mode.id}
                active={permissionMode === mode.id}
                label={mode.label}
                desc={mode.desc}
                onClick={() => {
                  switchPermissionMode(mode.id);
                }}
              />
            ))}
          </Picker>

          {!minimal && (
            <span style={workspacePillStyle} title={workspaceTitle}>
              <FolderOpen size={13} />
              <span style={workspaceNameStyle}>{workspaceLabel}</span>
              {branchLabel && <span style={branchLabelStyle}>{branchLabel}</span>}
            </span>
          )}
        </div>

        <span className="flex-1 min-w-0" />

        {!minimal && <ContextUsageRing />}

        <Picker
          refEl={effortRef}
          open={effortOpen}
          align="right"
          setOpen={(open) => {
            if (open) {
              setModelOpen(false);
              setPermissionOpen(false);
            }
            setEffortOpen(open);
          }}
          label={effortLabel}
          title={`Reasoning effort: ${effortOption(effortLevel).desc}`}
          tone={effortLevel === "max" ? "warning" : "normal"}
        >
          {EFFORT_OPTIONS.map((option) => (
            <MenuChoice
              key={option.id}
              active={effortLevel === option.id}
              label={option.label}
              desc={option.desc}
              onClick={() => switchEffort(option.id)}
            />
          ))}
        </Picker>

        <div ref={dropdownRef} className="relative">
          <button
            onClick={() => {
              setPermissionOpen(false);
              setEffortOpen(false);
              setModelOpen(!modelOpen);
            }}
            className="composer-model-select"
            aria-expanded={modelOpen}
            aria-haspopup="listbox"
            style={{ ...pill, background: modelOpen ? "var(--surface-page)" : "var(--surface-soft)", borderColor: modelOpen ? "var(--border-subtle)" : "transparent" }}
            title={currentModel || "Select model"}
          >
            <span className="max-w-[180px] overflow-hidden text-ellipsis whitespace-nowrap">{modelLabel}</span>
            <ChevronDown size={11} className="opacity-55 ml-0.5 flex-shrink-0" />
          </button>
          {modelOpen && availableModels.length > 0 && (
            <div style={dropdownStyle("right")}>
              {availableModels.map((m) => (
                <button key={m} onClick={() => switchModel(m)} style={{ ...dropdownItem, background: m === currentModel ? dropdownActiveBg : "transparent" }}>
                  {m === currentModel && <Check size={13} className="mr-1.5" style={{ color: "var(--accent-primary)" }} />}
                  <span className="flex-1">{m}</span>
                </button>
              ))}
              <div className="mt-1 pt-1" style={{ borderTop: "1px solid var(--border-subtle)" }}>
                <button
                  onClick={() => {
                    setModelOpen(false);
                    useAppStore.getState().toggleSettings();
                  }}
                  style={{ ...dropdownItem, color: "var(--accent-primary)" }}
                >
                  Configure...
                </button>
              </div>
            </div>
          )}
        </div>

        <SendIconBtn sendState={sendState} onSend={onSend} disabledReason={disabledReason} />
      </div>
    </div>
  );
});
FooterRow.displayName = "FooterRow";

const Picker = ({
  refEl,
  open,
  setOpen,
  label,
  title,
  tone = "normal",
  align = "left",
  icon,
  children,
}: {
  refEl: React.RefObject<HTMLDivElement>;
  open: boolean;
  setOpen: (open: boolean) => void;
  label: string;
  title: string;
  tone?: "normal" | "danger" | "info" | "warning";
  align?: "left" | "right";
  icon?: React.ReactNode;
  children: React.ReactNode;
}) => (
  <div ref={refEl} className="relative">
    <button
      type="button"
      onClick={() => {
        setOpen(!open);
      }}
      className="composer-permission-btn"
      aria-expanded={open}
      aria-haspopup="listbox"
      style={{ ...pill, background: open ? "var(--surface-page)" : "var(--surface-soft)", borderColor: open ? "var(--border-subtle)" : "transparent", color: toneColor(tone) }}
      title={title}
    >
      {icon}
      {label}
      <ChevronDown size={11} className="opacity-55 ml-0.5" />
    </button>
    {open && <div style={dropdownStyle(align)}>{children}</div>}
  </div>
);

const MenuChoice = ({
  active,
  label,
  desc,
  onClick,
}: {
  active: boolean;
  label: string;
  desc: string;
  onClick: () => void;
}) => (
  <button
    onClick={onClick}
    className="composer-menu-choice"
    style={{
      ...dropdownItem,
      display: "grid",
      gridTemplateColumns: "18px 1fr",
      columnGap: 8,
      alignItems: "start",
      background: active ? dropdownActiveBg : "transparent",
      padding: "8px 9px",
    }}
  >
    <span style={{ display: "inline-flex", justifyContent: "center", paddingTop: 2, color: active ? "var(--accent-primary)" : "transparent" }}>
      <Check size={13} />
    </span>
    <span style={{ display: "grid", gap: 2, minWidth: 0 }}>
      <span style={{ color: "var(--text-primary)", fontWeight: active ? 600 : 500 }}>{label}</span>
      <span style={{ color: "var(--text-muted)", fontSize: "var(--text-xs)", lineHeight: 1.35 }}>{desc}</span>
    </span>
  </button>
);

const ContextUsageRing = memo(() => {
  const contextUsage = useAppStore((s) => s.contextUsage);
  const budgetBuckets = useAppStore((s) => s.budgetBuckets);
  const totalBudgetPercent = useAppStore((s) => s.totalBudgetPercent);
  // Nothing measured yet (fresh session, no turn): stay out of the way.
  if (!contextUsage && totalBudgetPercent <= 0) return null;
  return (
    <button
      type="button"
      onClick={() => getWebSocket()?.send({ type: "session.usage.inspect", source: "usage_ring" })}
      title="Context usage — click for details (/usage)"
      aria-label="Show context and token usage"
      style={{ background: "transparent", border: 0, padding: 0, cursor: "pointer" }}
    >
      <UsageRing buckets={budgetBuckets} contextUsage={contextUsage} totalBudgetPercent={totalBudgetPercent} />
    </button>
  );
});
ContextUsageRing.displayName = "ContextUsageRing";

const ToggleChip = ({ label, active, onClick }: { label: string; active: boolean; onClick: () => void }) => (
  <button
    onClick={onClick}
    className="px-2 py-0.5 border cursor-pointer"
    style={{
      borderRadius: "var(--radius-sm, 4px)",
      borderColor: "var(--border-subtle)",
      background: active ? "var(--accent-soft)" : "var(--surface-soft)",
      color: active ? "var(--accent-primary)" : "var(--text-muted)",
      fontSize: "var(--text-xs)",
    }}
  >
    {label}
  </button>
);

const toneColor = (tone: "normal" | "danger" | "info" | "warning") => {
  if (tone === "danger") return "var(--state-danger)";
  if (tone === "info") return "var(--state-info)";
  if (tone === "warning") return "var(--state-warning)";
  return "var(--text-secondary)";
};

const permissionTone = (mode: PermissionMode): "normal" | "danger" | "info" | "warning" =>
  mode === "auto" ? "info" : mode === "plan" ? "warning" : "normal";

const permissionLabel = (mode: PermissionMode): string => {
  if (mode === "ask_permissions") return "Ask";
  if (mode === "plan") return "Plan";
  if (mode === "auto") return "Auto";
  if (mode === "bypass") return "Full access";
  return "Auto";
};

const effortOption = (level: EffortLevel) =>
  EFFORT_OPTIONS.find((option) => option.id === level) ?? EFFORT_OPTIONS[2];

const prMonitorStyle = (status: string): React.CSSProperties => ({
  display: "flex",
  alignItems: "center",
  gap: 8,
  padding: "6px 8px",
  marginBottom: 4,
  background: status === "failed" ? "var(--state-danger-soft)" : status === "passed" ? "var(--state-success-soft)" : "var(--state-info-soft)",
  borderRadius: "var(--radius-sm, 4px)",
  fontSize: "var(--text-xs)",
});

const prDotStyle = (status: string): React.CSSProperties => ({
  width: 6,
  height: 6,
  borderRadius: "50%",
  background: status === "failed" ? "var(--state-danger)" : status === "passed" ? "var(--state-success)" : status === "running" ? "var(--state-info)" : "var(--text-muted)",
  animation: status === "running" ? "thinking-pulse 1.5s ease-in-out infinite" : "none",
});

const footerStyle = (compact: boolean, minimal: boolean): React.CSSProperties => ({
  display: "flex",
  alignItems: "center",
  gap: 7,
  marginTop: compact ? 4 : 0,
  padding: compact ? "0 12px 8px" : minimal ? "4px 6px 0" : "2px 2px 0",
  borderTop: 0,
  fontSize: "var(--text-xs)",
});

const footerLeftStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 7,
  minWidth: 0,
};

const iconBtn = (compact: boolean): React.CSSProperties => ({
  background: "transparent",
  color: "var(--text-secondary)",
  border: "1px solid transparent",
  cursor: "pointer",
  width: compact ? 28 : 34,
  height: compact ? 28 : 34,
  padding: 0,
  display: "inline-flex",
  alignItems: "center",
  justifyContent: "center",
  borderRadius: "var(--radius-full)",
});

const pill: React.CSSProperties = {
  background: "transparent",
  color: "var(--text-secondary)",
  border: "1px solid transparent",
  borderRadius: "var(--radius-full)",
  padding: "5px 8px",
  cursor: "pointer",
  fontSize: "var(--text-xs)",
  display: "inline-flex",
  alignItems: "center",
};

const workspacePillStyle: React.CSSProperties = {
  display: "inline-flex",
  alignItems: "center",
  gap: 6,
  minWidth: 0,
  maxWidth: 260,
  height: 28,
  padding: "0 8px",
  border: "1px solid transparent",
  borderRadius: "var(--radius-full)",
  color: "var(--text-secondary)",
  background: "transparent",
};

const workspaceNameStyle: React.CSSProperties = {
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
  fontSize: "var(--text-xs)",
};

const branchLabelStyle: React.CSSProperties = {
  overflow: "hidden",
  maxWidth: 88,
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
  color: "var(--text-muted)",
  fontFamily: "var(--font-mono)",
  fontSize: 10,
};

const dropdownStyle = (align: "left" | "right" = "left"): React.CSSProperties => ({
  position: "absolute",
  bottom: "calc(100% + 6px)",
  left: align === "left" ? 0 : "auto",
  right: align === "right" ? 0 : "auto",
  minWidth: 244,
  background: "var(--surface-page)",
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-md, 8px)",
  boxShadow: "var(--shadow-soft)",
  padding: 6,
  zIndex: 10,
  maxHeight: 260,
  overflowY: "auto",
});

const dropdownActiveBg = "color-mix(in oklch, var(--accent-primary) 7%, var(--surface-page))";

const dropdownItem: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  width: "100%",
  textAlign: "left",
  padding: "7px 9px",
  border: 0,
  cursor: "pointer",
  color: "var(--text-primary)",
  fontSize: "var(--text-sm)",
  fontFamily: "var(--font-ui)",
  borderRadius: "var(--radius-sm, 6px)",
};

const SendIconBtn = memo(({ sendState, onSend, disabledReason }: { sendState: SendButtonState; onSend: () => void; disabledReason?: string }) => {
  const disabled = sendState === "disabled";
  return (
    <button onClick={onSend} disabled={disabled} title={disabledReason || (sendState === "stop" ? "Stop" : "Send")} aria-label={sendState === "stop" ? "Stop" : "Send"} className="btn-send composer-send-btn w-7 h-7 border-0 inline-flex items-center justify-center" style={sendIconButtonStyle(sendState, disabled)}>
      {sendState === "stop" ? <Square size={12} fill="currentColor" /> : <ArrowUp size={16} strokeWidth={2.3} />}
    </button>
  );
});
SendIconBtn.displayName = "SendIconBtn";

const sendIconButtonStyle = (sendState: SendButtonState, disabled: boolean): React.CSSProperties => ({
  borderRadius: "var(--radius-full)",
  background: sendState === "stop" ? "var(--text-primary)" : disabled ? "var(--surface-soft)" : "var(--text-primary)",
  color: disabled ? "var(--text-muted)" : "var(--text-on-accent)",
  cursor: disabled ? "not-allowed" : "pointer",
});
