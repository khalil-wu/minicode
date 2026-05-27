import { Check, ChevronDown, Paperclip, Send, ShieldCheck, StopCircle } from "lucide-react";
import { memo, useEffect, useId, useRef, useState } from "react";
import type { SendButtonState } from "../lib/send-state";
import { useAppStore } from "../stores";
import { getWebSocket } from "../hooks/useWebSocket";
import { uploadComposerFiles } from "./uploads";
import type { PermissionMode } from "../stores/types";
import type { EffortLevel } from "../stores/types";

interface Props {
  sendState: SendButtonState;
  onSend: () => void | Promise<void>;
  compact?: boolean;
}

const formatTokens = (n: number): string => {
  if (n >= 1000) return `${(n / 1000).toFixed(1)}k`;
  return String(n);
};

const EFFORT_LEVELS: { id: EffortLevel; label: string; desc: string }[] = [
  { id: "low", label: "Low", desc: "Fast, minimal reasoning" },
  { id: "medium", label: "Med", desc: "Balanced reasoning" },
  { id: "high", label: "High", desc: "Default depth" },
  { id: "max", label: "Max", desc: "Deepest reasoning" },
];

const PERMISSION_MODES: { id: PermissionMode; label: string; desc: string }[] = [
  { id: "ask_permissions", label: "Ask", desc: "Ask before tool actions" },
  { id: "auto", label: "Auto", desc: "Read/search freely, ask before risky actions" },
  { id: "acceptEdits", label: "Accept", desc: "Auto-accept file edits, ask for commands" },
  { id: "plan", label: "Plan", desc: "Read/search only, block edits and commands" },
  { id: "bypass", label: "Bypass", desc: "Run tools without approval prompts" },
];


export const FooterRow = memo(({ sendState, onSend, compact = false }: Props) => {
  const permissionMode = useAppStore((s) => s.permissionMode);
  const currentModel = useAppStore((s) => s.currentModel);
  const currentProvider = useAppStore((s) => s.currentProvider);
  const availableModels = useAppStore((s) => s.availableModels);
  const effortLevel = useAppStore((s) => s.effortLevel);
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
    useAppStore.getState().setCurrentModel(model);
    setModelOpen(false);
  };

  const switchPermissionMode = async (mode: PermissionMode) => {
    if (mode === "bypass" && permissionMode !== "bypass") {
      const { showConfirm } = await import("../overlays/DialogService");
      const ok = await showConfirm({
        title: "Enable Bypass mode",
        message: "Bypass mode will run edits and commands without approval prompts. Continue?",
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

  const shortModel = currentModel
    ? currentModel.replace(/^(claude-|gpt-|gemini-)/, "").split("-").slice(0, 2).join("-")
    : "--";

  const permLabel = permissionLabel(permissionMode);
  const thinkingCapability = getThinkingCapability(currentProvider, currentModel);

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 0 }}>
      {prMonitor && (
        <div style={prMonitorStyle(prMonitor.ciStatus)}>
          <span style={prDotStyle(prMonitor.ciStatus)} />
          <span style={{ fontWeight: 600, color: "var(--text-primary)" }}>PR #{prMonitor.prNumber}</span>
          <span style={{ color: "var(--text-muted)" }}>CI: {prMonitor.ciStatus}</span>
          {prMonitor.failedChecks && prMonitor.failedChecks.length > 0 && (
            <span style={{ color: "var(--state-danger)" }}>{prMonitor.failedChecks.length} failed</span>
          )}
          <span style={{ flex: 1 }} />
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

      <div style={footerStyle(compact)}>
        <input
          id={fileInputId}
          ref={fileRef}
          type="file"
          multiple
          style={visuallyHiddenInputStyle}
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

        {!compact && <div ref={dropdownRef} style={{ position: "relative" }}>
          <button onClick={() => setModelOpen(!modelOpen)} style={{ ...pill, background: modelOpen ? "var(--surface-active)" : "var(--surface-soft)" }} title={currentModel || "Select model"}>
            {shortModel} <ChevronDown size={11} style={{ opacity: 0.55, marginLeft: 2 }} />
          </button>
          {modelOpen && availableModels.length > 0 && (
            <div style={dropdownStyle}>
              {availableModels.map((m) => (
                <button key={m} onClick={() => switchModel(m)} style={{ ...dropdownItem, background: m === currentModel ? "var(--surface-active)" : "transparent" }}>
                  {m === currentModel && <Check size={13} style={{ color: "var(--accent-primary)", marginRight: 6 }} />}
                  <span style={{ flex: 1 }}>{m}</span>
                </button>
              ))}
              <div style={{ borderTop: "1px solid var(--border-subtle)", marginTop: 4, paddingTop: 4 }}>
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
        </div>}

        {!compact && <Picker
          refEl={permissionRef}
          open={permissionOpen}
          setOpen={setPermissionOpen}
          label={permLabel}
          title={`Permission: ${permLabel}`}
          tone={permissionTone(permissionMode)}
          icon={<ShieldCheck size={12} />}
        >
          {PERMISSION_MODES.map((mode) => (
            <MenuChoice
              key={mode.id}
              index={PERMISSION_MODES.findIndex((item) => item.id === mode.id) + 1}
              active={permissionMode === mode.id}
              label={mode.label}
              desc={mode.desc}
              onClick={() => {
                switchPermissionMode(mode.id);
              }}
            />
          ))}
        </Picker>}

        {!compact && <ContextRing />}

        {!compact && <ThinkingControl
          refEl={effortRef}
          open={effortOpen}
          setOpen={setEffortOpen}
          capability={thinkingCapability}
          effortLevel={effortLevel}
          onEffortChange={setEffortLevel}
        />}

        {!compact && <TokenUsage />}

        <span style={{ flex: 1, minWidth: 0 }} />

        {compact ? (
          <>
            <span style={compactStatusStyle}>{permLabel}</span>
            <span style={{ flex: 1 }} />
            <span style={compactModelStyle}>{shortModel}</span>
            <SendIconBtn sendState={sendState} onSend={onSend} />
          </>
        ) : (
          <SendBtn sendState={sendState} onSend={onSend} />
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
  <div ref={refEl} style={{ position: "relative" }}>
    <button
      onMouseDown={(event) => {
        event.preventDefault();
        event.stopPropagation();
        setOpen(!open);
      }}
      onClick={(event) => event.preventDefault()}
      style={{ ...pill, background: open ? "var(--surface-active)" : "var(--surface-soft)", color: toneColor(tone) }}
      title={title}
    >
      {icon}
      {label}
      <ChevronDown size={11} style={{ opacity: 0.55, marginLeft: 2 }} />
    </button>
    {open && <div style={dropdownStyle}>{children}</div>}
  </div>
);

const MenuChoice = ({
  index,
  active,
  label,
  desc,
  onClick,
}: {
  index: number;
  active: boolean;
  label: string;
  desc: string;
  onClick: () => void;
}) => (
  <button
    onClick={onClick}
    style={{
      ...dropdownItem,
      background: active ? "var(--surface-active)" : "transparent",
      flexDirection: "column",
      alignItems: "flex-start",
      gap: 2,
    }}
  >
    <span style={{ display: "flex", alignItems: "center", gap: 6, color: active ? "var(--accent-primary)" : "var(--text-primary)", fontWeight: active ? 600 : 400 }}>
      <span style={{ fontFamily: "var(--font-mono)", color: "var(--text-muted)", width: 14 }}>{index}</span>
      {active && <Check size={13} />}
      {label}
    </span>
    <span style={{ fontSize: 11, color: "var(--text-muted)", paddingLeft: 20 }}>{desc}</span>
  </button>
);

const TokenUsage = memo(() => {
  const lastUsage = useAppStore((s) => s.lastUsage);
  const isStreaming = useAppStore((s) => s.isStreaming);
  if (!lastUsage || isStreaming) return null;
  const total = lastUsage.input + lastUsage.output + lastUsage.cacheRead + lastUsage.cacheWrite;
  if (total === 0) return null;
  return (
    <span style={pathStyle} title={`In: ${lastUsage.input} Out: ${lastUsage.output} Cache: ${lastUsage.cacheRead}`}>
      {formatTokens(lastUsage.input + lastUsage.output)} tok
    </span>
  );
});
TokenUsage.displayName = "TokenUsage";

const SendBtn = memo(({ sendState, onSend }: { sendState: SendButtonState; onSend: () => void }) => {
  const disabled = sendState === "disabled";
  const label = sendState === "stop" ? "Stop" : sendState === "sending" ? "..." : "Send";
  return (
    <button onClick={onSend} disabled={disabled} style={sendButtonStyle(sendState, disabled)}>
      {sendState === "stop" ? <StopCircle size={14} /> : <Send size={14} />}
      <span>{label}</span>
    </button>
  );
});
SendBtn.displayName = "SendBtn";

const ContextRing = memo(() => {
  const contextUsage = useAppStore((s) => s.contextUsage);
  if (!contextUsage) return null;
  const pct = Math.min(contextUsage.used / contextUsage.limit, 1);
  const r = 8;
  const circ = 2 * Math.PI * r;
  const offset = circ * (1 - pct);
  const color = pct > 0.9 ? "var(--state-danger)" : pct > 0.7 ? "var(--state-warning)" : "var(--accent-primary)";
  return (
    <span title={`Context: ${Math.round(pct * 100)}% (${formatTokens(contextUsage.used)}/${formatTokens(contextUsage.limit)})`} style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
      <svg width={20} height={20} style={{ transform: "rotate(-90deg)" }}>
        <circle cx={10} cy={10} r={r} fill="none" stroke="var(--border-subtle)" strokeWidth={2.5} />
        <circle cx={10} cy={10} r={r} fill="none" stroke={color} strokeWidth={2.5} strokeDasharray={circ} strokeDashoffset={offset} strokeLinecap="round" />
      </svg>
      <span style={{ fontSize: "var(--text-xs)", color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>{Math.round(pct * 100)}%</span>
    </span>
  );
});
ContextRing.displayName = "ContextRing";

const ThinkingControl = ({
  refEl,
  open,
  setOpen,
  capability,
  effortLevel,
  onEffortChange,
}: {
  refEl: React.RefObject<HTMLDivElement>;
  open: boolean;
  setOpen: (open: boolean) => void;
  capability: ReturnType<typeof getThinkingCapability>;
  effortLevel: EffortLevel;
  onEffortChange: (level: EffortLevel) => void;
}) => {
  if (capability.kind === "none") {
    return (
      <span title={capability.title} style={disabledThinkingPillStyle}>
        Think n/a
      </span>
    );
  }
  if (capability.kind === "anthropic") {
    return (
      <button
        type="button"
        title={capability.title}
        onClick={() => {
          window.dispatchEvent(new CustomEvent("minicode:settings-tab", { detail: "provider" }));
          useAppStore.getState().toggleSettings();
        }}
        style={pill}
      >
        Think {capability.label}
      </button>
    );
  }
  return (
    <Picker
      refEl={refEl}
      open={open}
      setOpen={setOpen}
      label={`Think ${effortLabel(effortLevel)}`}
      title={capability.title}
    >
      {EFFORT_LEVELS.map((level) => (
        <MenuChoice
          key={level.id}
          index={EFFORT_LEVELS.findIndex((item) => item.id === level.id) + 1}
          active={effortLevel === level.id}
          label={level.label}
          desc={level.desc}
          onClick={() => {
            onEffortChange(level.id);
            setOpen(false);
          }}
        />
      ))}
    </Picker>
  );
};

const getThinkingCapability = (provider: string, model: string) => {
  const p = provider.toLowerCase();
  const m = model.toLowerCase();
  if (p === "anthropic" || m.includes("claude")) {
    return { kind: "anthropic" as const, label: "budget", title: "Claude thinking is controlled by the provider Thinking Budget in Settings." };
  }
  const isReasoningModel =
    p === "openai" ||
    m.includes("gpt-5") ||
    /\bo[134]\b/.test(m) ||
    m.includes("o1") ||
    m.includes("o3") ||
    m.includes("o4") ||
    m.includes("reasoning");
  if (isReasoningModel) {
    return { kind: "effort" as const, label: "effort", title: "Reasoning effort for OpenAI-compatible reasoning models." };
  }
  return { kind: "none" as const, label: "none", title: "The selected model does not expose a reasoning effort control." };
};

const effortLabel = (level: EffortLevel): string =>
  level === "medium" ? "Med" : level[0].toUpperCase() + level.slice(1);

const ToggleChip = ({ label, active, onClick }: { label: string; active: boolean; onClick: () => void }) => (
  <button
    onClick={onClick}
    style={{
      padding: "2px 8px",
      borderRadius: "var(--radius-sm, 4px)",
      border: "1px solid var(--border-subtle)",
      background: active ? "var(--accent-soft)" : "var(--surface-soft)",
      color: active ? "var(--accent-primary)" : "var(--text-muted)",
      fontSize: "var(--text-xs)",
      cursor: "pointer",
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
  mode === "bypass" ? "danger" : mode === "auto" ? "info" : mode === "plan" ? "warning" : "normal";

const permissionLabel = (mode: PermissionMode): string => {
  if (mode === "ask_permissions") return "Ask";
  if (mode === "auto") return "Auto";
  if (mode === "acceptEdits") return "Accept";
  if (mode === "plan") return "Plan";
  if (mode === "bypass") return "Bypass";
  return "Ask";
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

const footerStyle = (compact: boolean): React.CSSProperties => ({
  display: "flex",
  alignItems: "center",
  gap: 7,
  marginTop: 4,
  padding: compact ? "0 12px 8px" : "6px 6px 0",
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

const visuallyHiddenInputStyle: React.CSSProperties = {
  position: "absolute",
  width: 1,
  height: 1,
  padding: 0,
  margin: -1,
  overflow: "hidden",
  clip: "rect(0 0 0 0)",
  whiteSpace: "nowrap",
  border: 0,
};

const pill: React.CSSProperties = {
  background: "transparent",
  color: "var(--text-secondary)",
  border: "1px solid transparent",
  borderRadius: "var(--radius-full)",
  padding: "6px 9px",
  cursor: "pointer",
  fontSize: "var(--text-sm)",
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

const disabledThinkingPillStyle: React.CSSProperties = {
  ...pill,
  cursor: "default",
  color: "var(--text-muted)",
  opacity: 0.72,
};

const dropdownStyle: React.CSSProperties = {
  position: "absolute",
  bottom: "calc(100% + 6px)",
  left: 0,
  minWidth: 250,
  background: "var(--surface-raised)",
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-sm, 6px)",
  boxShadow: "var(--shadow-strong)",
  padding: 6,
  zIndex: 10,
  maxHeight: 280,
  overflowY: "auto",
};

const dropdownItem: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  width: "100%",
  textAlign: "left",
  padding: "8px 10px",
  border: 0,
  cursor: "pointer",
  color: "var(--text-primary)",
  fontSize: "var(--text-xs)",
  fontFamily: "var(--font-ui)",
  borderRadius: "var(--radius-sm, 4px)",
};

const sendButtonStyle = (sendState: SendButtonState, disabled: boolean): React.CSSProperties => ({
  background: sendState === "stop" ? "var(--state-danger)" : disabled ? "var(--surface-soft)" : "var(--accent-primary)",
  color: disabled ? "var(--text-muted)" : "var(--text-on-accent)",
  border: 0,
  padding: "0 13px",
  borderRadius: "var(--radius-sm, 10px)",
  fontWeight: 600,
  cursor: disabled ? "not-allowed" : "pointer",
  minWidth: 42,
  height: 42,
  display: "inline-flex",
  alignItems: "center",
  justifyContent: "center",
  gap: 6,
});

const SendIconBtn = memo(({ sendState, onSend }: { sendState: SendButtonState; onSend: () => void }) => {
  const disabled = sendState === "disabled";
  return (
    <button onClick={onSend} disabled={disabled} title={sendState === "stop" ? "Stop" : "Send"} aria-label={sendState === "stop" ? "Stop" : "Send"} style={sendIconButtonStyle(sendState, disabled)}>
      {sendState === "stop" ? <StopCircle size={15} /> : <Send size={15} />}
    </button>
  );
});
SendIconBtn.displayName = "SendIconBtn";

const sendIconButtonStyle = (sendState: SendButtonState, disabled: boolean): React.CSSProperties => ({
  width: 28,
  height: 28,
  border: 0,
  borderRadius: "var(--radius-sm, 7px)",
  background: sendState === "stop" ? "var(--state-danger)" : disabled ? "transparent" : "var(--accent-primary)",
  color: disabled ? "var(--text-muted)" : "var(--text-on-accent)",
  cursor: disabled ? "not-allowed" : "pointer",
  display: "inline-flex",
  alignItems: "center",
  justifyContent: "center",
});

const compactStatusStyle: React.CSSProperties = {
  color: "var(--text-secondary)",
  fontSize: "var(--text-sm)",
};

const compactModelStyle: React.CSSProperties = {
  color: "var(--text-secondary)",
  fontSize: "var(--text-sm)",
};
