import { Check, ChevronDown, Paperclip, Send, ShieldCheck, StopCircle } from "lucide-react";
import { memo, useEffect, useId, useRef, useState } from "react";
import type { SendButtonState } from "../lib/send-state";
import { useAppStore } from "../stores";
import { getWebSocket } from "../hooks/useWebSocket";
import { uploadComposerFiles } from "./uploads";
import type { PermissionMode } from "../stores/types";
import { formatModelLabel } from "../lib/model-label";
import { UsageRing } from "../shell/UsageRing";

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

const formatTokens = (n: number): string => {
  if (n >= 1000) return `${(n / 1000).toFixed(1)}k`;
  return String(n);
};

const PERMISSION_MODES: { id: PermissionMode; label: string; desc: string }[] = [
  { id: "ask_permissions", label: "Ask", desc: "Ask before file and network actions" },
  { id: "auto", label: "Auto", desc: "Auto read, search, and edit workspace files" },
  { id: "bypass", label: "Full access", desc: "Use files, network, edits, and commands without prompts" },
];


export const FooterRow = memo(({ sendState, onSend, compact = false, minimal = false }: Props) => {
  const permissionMode = useAppStore((s) => s.permissionMode);
  const currentModel = useAppStore((s) => s.currentModel);
  const availableModels = useAppStore((s) => s.availableModels);
  const prMonitor = useAppStore((s) => s.prMonitor);
  const setPermissionMode = useAppStore((s) => s.setPermissionMode);
  const fileInputId = useId();
  const fileRef = useRef<HTMLInputElement>(null);
  const [modelOpen, setModelOpen] = useState(false);
  const [permissionOpen, setPermissionOpen] = useState(false);
  const dropdownRef = useRef<HTMLDivElement>(null);
  const permissionRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const close = (e: MouseEvent) => {
      if (modelOpen && dropdownRef.current && !dropdownRef.current.contains(e.target as Node)) setModelOpen(false);
      if (permissionOpen && permissionRef.current && !permissionRef.current.contains(e.target as Node)) setPermissionOpen(false);
    };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, [modelOpen, permissionOpen]);

  useEffect(() => {
    const openModelMenu = () => {
      setPermissionOpen(false);
      setModelOpen(true);
    };
    const openPermissionMenu = () => {
      setModelOpen(false);
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
    getWebSocket()?.send({ type: "llm.model.set", model });
    setModelOpen(false);
  };

  const switchPermissionMode = async (mode: PermissionMode) => {
    if (mode === "bypass" && permissionMode !== "bypass") {
      const { showConfirm } = await import("../overlays/DialogService");
      const ok = await showConfirm({
        title: "Enable Full access",
        message: "Full access can read files outside the workspace and run edits or commands without approval prompts. Continue?",
        confirmLabel: "Enable",
        danger: true,
      });
      if (!ok) return;
    }
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

      <div style={footerStyle(compact, minimal)}>
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
        <button
          type="button"
          title="Attach file"
          style={iconBtn(compact)}
          aria-label="Attach file"
          onClick={openFilePicker}
          onKeyDown={(event) => {
            if (event.key !== "Enter" && event.key !== " ") return;
            openFilePicker(event);
          }}
        >
          <Paperclip size={14} />
        </button>

        <span className="flex-1 min-w-0" />

        <div ref={dropdownRef} className="relative">
          <button
            onClick={() => {
              setPermissionOpen(false);
              setModelOpen(!modelOpen);
            }}
            style={{ ...pill, background: modelOpen ? "var(--surface-page)" : "var(--surface-soft)", borderColor: modelOpen ? "var(--border-subtle)" : "transparent" }}
            title={currentModel || "Select model"}
          >
            <span className="max-w-[180px] overflow-hidden text-ellipsis whitespace-nowrap">{modelLabel}</span>
            <ChevronDown size={11} className="opacity-55 ml-0.5 flex-shrink-0" />
          </button>
          {modelOpen && availableModels.length > 0 && (
            <div style={dropdownStyle}>
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

        <Picker
          refEl={permissionRef}
          open={permissionOpen}
          setOpen={(open) => {
            if (open) setModelOpen(false);
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

        {!minimal && <ContextUsageRing />}

        {compact ? (
          <>
            <span className="flex-1" />
          <SendIconBtn sendState={sendState} onSend={onSend} disabledReason={disabledReason} />
          </>
        ) : (
          <SendBtn sendState={sendState} onSend={onSend} disabledReason={disabledReason} />
        )}
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
  icon,
  children,
}: {
  refEl: React.RefObject<HTMLDivElement>;
  open: boolean;
  setOpen: (open: boolean) => void;
  label: string;
  title: string;
  tone?: "normal" | "danger" | "info" | "warning";
  icon?: React.ReactNode;
  children: React.ReactNode;
}) => (
  <div ref={refEl} className="relative">
    <button
      type="button"
      onClick={() => {
        setOpen(!open);
      }}
      style={{ ...pill, background: open ? "var(--surface-page)" : "var(--surface-soft)", borderColor: open ? "var(--border-subtle)" : "transparent", color: toneColor(tone) }}
      title={title}
    >
      {icon}
      {label}
      <ChevronDown size={11} className="opacity-55 ml-0.5" />
    </button>
    {open && <div style={dropdownStyle}>{children}</div>}
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

const TokenUsage = memo(() => {
  const lastUsage = useAppStore((s) => s.lastUsage);
  const isStreaming = useAppStore((s) => s.isStreaming);
  if (!lastUsage) return null;
  const total = lastUsage.input + lastUsage.output + lastUsage.cacheRead + lastUsage.cacheWrite;
  if (total === 0) return null;
  return (
    <span style={{ ...pathStyle, opacity: isStreaming ? 0.55 : 1 }} title={`In: ${lastUsage.input} Out: ${lastUsage.output} Cache: ${lastUsage.cacheRead}`}>
      {formatTokens(lastUsage.input + lastUsage.output)} tok
      {isStreaming && <span style={{ opacity: 0.65 }}> (Previous Turn)</span>}
    </span>
  );
});
TokenUsage.displayName = "TokenUsage";

const SendBtn = memo(({ sendState, onSend, disabledReason }: { sendState: SendButtonState; onSend: () => void; disabledReason?: string }) => {
  const disabled = sendState === "disabled";
  const label = sendState === "stop" ? "Stop" : sendState === "sending" ? "..." : "Send";
  return (
    <button onClick={onSend} disabled={disabled} title={disabledReason} className="btn-send px-3.5 border-0 font-semibold min-w-[42px] h-[42px] inline-flex items-center justify-center gap-1.5" style={sendButtonStyle(sendState, disabled)}>
      {sendState === "stop" ? <StopCircle size={14} /> : <Send size={14} />}
      <span>{label}</span>
    </button>
  );
});
SendBtn.displayName = "SendBtn";

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
  mode === "bypass" ? "danger" : mode === "auto" ? "info" : "normal";

const permissionLabel = (mode: PermissionMode): string => {
  if (mode === "ask_permissions") return "Ask";
  if (mode === "auto") return "Auto";
  if (mode === "bypass") return "Full access";
  return "Auto";
};

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

const iconBtn = (compact: boolean): React.CSSProperties => ({
  background: "transparent",
  color: "var(--text-secondary)",
  border: 0,
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


const pathStyle: React.CSSProperties = {
  color: "var(--text-muted)",
  fontFamily: "var(--font-mono)",
  fontSize: "var(--text-xs)",
  maxWidth: 170,
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
};

const dropdownStyle: React.CSSProperties = {
  position: "absolute",
  bottom: "calc(100% + 6px)",
  left: 0,
  minWidth: 244,
  background: "var(--surface-page)",
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-md, 8px)",
  boxShadow: "var(--shadow-soft)",
  padding: 6,
  zIndex: 10,
  maxHeight: 260,
  overflowY: "auto",
};

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

const sendButtonStyle = (sendState: SendButtonState, disabled: boolean): React.CSSProperties => ({
  background: sendState === "stop" ? "var(--state-danger)" : disabled ? "var(--surface-soft)" : "var(--accent-primary)",
  color: disabled ? "var(--text-muted)" : "var(--text-on-accent)",
  borderRadius: "var(--radius-md, 12px)",
  cursor: disabled ? "not-allowed" : "pointer",
});

const SendIconBtn = memo(({ sendState, onSend, disabledReason }: { sendState: SendButtonState; onSend: () => void; disabledReason?: string }) => {
  const disabled = sendState === "disabled";
  return (
    <button onClick={onSend} disabled={disabled} title={disabledReason || (sendState === "stop" ? "Stop" : "Send")} aria-label={sendState === "stop" ? "Stop" : "Send"} className="btn-send w-7 h-7 border-0 inline-flex items-center justify-center" style={sendIconButtonStyle(sendState, disabled)}>
      {sendState === "stop" ? <StopCircle size={15} /> : <Send size={15} />}
    </button>
  );
});
SendIconBtn.displayName = "SendIconBtn";

const sendIconButtonStyle = (sendState: SendButtonState, disabled: boolean): React.CSSProperties => ({
  borderRadius: "var(--radius-sm, 7px)",
  background: sendState === "stop" ? "var(--state-danger)" : disabled ? "transparent" : "var(--accent-primary)",
  color: disabled ? "var(--text-muted)" : "var(--text-on-accent)",
  cursor: disabled ? "not-allowed" : "pointer",
});
