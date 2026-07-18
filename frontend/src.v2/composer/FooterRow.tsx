import { ArrowUp, Check, ChevronDown, Plus, ShieldCheck, Square } from "lucide-react";
import { memo, useEffect, useId, useRef, useState } from "react";
import type { SendButtonState } from "../lib/send-state";
import { useAppStore } from "../stores";
import { getWebSocket } from "../hooks/useWebSocket";
import { sendClientCommand } from "../protocol/ws-outbox";
import { uploadComposerFiles } from "./uploads";
import type { EffortLevel, PermissionMode } from "../stores/types";
import { formatModelLabel } from "../lib/model-label";
import { UsageRing } from "../shell/UsageRing";
import { openSettings } from "../lib/settings-navigation";
import { selectableModelsForProvider } from "../lib/provider-models";
import { ModelProviderIcon } from "../components/ModelProviderIcon";

interface Props {
  sendState: SendButtonState;
  onSend: () => void | Promise<void>;
  onStop?: () => void;
  compact?: boolean;
  /**
   * Minimal mode keeps the full-size textarea but hides the secondary control
   * row clutter (context ring and token usage).
   * Used by the Cowork landing page composer.
   */
  minimal?: boolean;
}

const PERMISSION_MODES: { id: PermissionMode; label: string }[] = [
  { id: "ask_permissions", label: "Ask" },
  { id: "auto", label: "Auto" },
  { id: "bypass", label: "Full access" },
];

const EFFORT_OPTIONS: { id: EffortLevel; label: string; desc: string }[] = [
  { id: "none", label: "none", desc: "Provider effort: none" },
  { id: "minimal", label: "minimal", desc: "Provider effort: minimal" },
  { id: "low", label: "low", desc: "Provider effort: low" },
  { id: "medium", label: "medium", desc: "Provider effort: medium" },
  { id: "high", label: "high", desc: "Provider effort: high" },
  { id: "xhigh", label: "xhigh", desc: "Provider effort: xhigh" },
  { id: "max", label: "max", desc: "Provider effort: max" },
];

const EFFORT_OPTION_BY_ID = new Map<EffortLevel, { id: EffortLevel; label: string; desc: string }>(
  EFFORT_OPTIONS.map((option) => [option.id, option]),
);

export const FooterRow = memo(({ sendState, onSend, onStop, compact = false, minimal = false }: Props) => {
  const permissionMode = useAppStore((s) => s.permissionMode);
  const effortLevel = useAppStore((s) => s.effortLevel);
  const currentModel = useAppStore((s) => s.currentModel);
  const currentProvider = useAppStore((s) => s.currentProvider);
  const currentProviderBaseUrl = useAppStore((s) => s.currentProviderBaseUrl);
  const availableModels = useAppStore((s) => s.availableModels);
  const modelsSource = useAppStore((s) => s.modelsSource);
  const runtimeCapabilities = useAppStore((s) => s.runtimeCapabilities);
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

  const switchPermissionMode = async (mode: PermissionMode) => {
    // Gate Full access (bypass) behind an explicit accept-responsibility
    // confirm, mirroring cc's BypassPermissionsModeDialog. Other modes switch
    // instantly. The dialog fires on every entry into bypass (no persisted
    // skip-flag) — a deliberate extra confirmation step for a destructive mode.
    if (mode === "bypass" && permissionMode !== "bypass") {
      setPermissionOpen(false);
      const { showConfirm } = await import("../overlays/DialogService");
      const ok = await showConfirm({
        title: "Turn on Full access?",
        message:
          "Full access (bypass) lets the agent run commands, edit files, and use the network without asking. Only enable this in a workspace you trust.",
        confirmLabel: "Turn on Full access",
        cancelLabel: "Cancel",
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
  const selectableModels = selectableModelsForProvider(
    availableModels,
    currentModel,
    currentProvider,
    currentProviderBaseUrl,
    modelsSource,
  );
  const disabledReason = !currentModel.trim() ? "Select a model before sending" : undefined;

  const permLabel = permissionLabel(permissionMode);
  const providerCapabilities = runtimeCapabilities?.provider_capabilities;
  const capabilityEffortLevels = normalizeCapabilityEffortLevels(providerCapabilities?.reasoning_effort_levels);
  const supportsReasoningEffort = capabilityBool(providerCapabilities?.reasoning_effort) && capabilityEffortLevels.length > 0;
  const availableEffortLevels = capabilityEffortLevels;
  const effortOptions = supportsReasoningEffort
    ? availableEffortLevels.map((level) => EFFORT_OPTION_BY_ID.get(level)).filter(Boolean) as typeof EFFORT_OPTIONS
    : [];
  const selectedEffort = effortOptions.find((option) => option.id === effortLevel)
    ?? (effortLevel === "max" ? effortOptions.find((option) => option.id === "xhigh") : undefined)
    ?? effortOptions[0]
    ?? effortOption(effortLevel);
  const effortLabel = selectedEffort.label;
  const effortTitle = `Reasoning effort: ${selectedEffort.desc}`;

  return (
    <div className="flex flex-col gap-0">
      {prMonitor && (
        <div style={prMonitorStyle(prMonitor.ciStatus)}>
          <span className={prMonitor.ciStatus === "running" ? "thinking-pulse-dot" : undefined} style={prDotStyle(prMonitor.ciStatus)} />
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

      <div
        className="composer-footer"
        data-compact={compact ? "true" : "false"}
        data-minimal={minimal ? "true" : "false"}
        style={footerStyle(compact)}
      >
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
        <div className="composer-footer-primary" style={footerLeftStyle}>
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
            icon={<ShieldCheck size={12} />}
          >
            {PERMISSION_MODES.map((mode) => (
              <MenuChoice
                key={mode.id}
                active={permissionMode === mode.id}
                label={mode.label}
                onClick={() => {
                  switchPermissionMode(mode.id);
                }}
              />
            ))}
          </Picker>

        </div>

        <span className="composer-footer-spacer" aria-hidden="true" />

        {!minimal && <ContextUsageRing />}

        {supportsReasoningEffort && (
          <Picker
            refEl={effortRef}
            open={effortOpen}
            className="composer-effort-picker"
            align="right"
            setOpen={(open) => {
              if (open) {
                setModelOpen(false);
                setPermissionOpen(false);
              }
              setEffortOpen(open);
            }}
            label={effortLabel}
            title={effortTitle}
          >
            {effortOptions.map((option) => (
              <MenuChoice
                key={option.id}
                active={effortLevel === option.id}
                label={option.label}
                onClick={() => switchEffort(option.id)}
              />
            ))}
          </Picker>
        )}

        <div ref={dropdownRef} className="composer-model-picker relative">
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
            <ModelProviderIcon model={currentModel} size={15} />
            <span className="max-w-[180px] overflow-hidden text-ellipsis whitespace-nowrap">{modelLabel}</span>
            <ChevronDown size={11} className="opacity-55 ml-0.5 flex-shrink-0" />
          </button>
          {modelOpen && selectableModels.length > 0 && (
            <div style={dropdownStyle("right")}>
              {selectableModels.map((m) => (
                <button key={m} onClick={() => switchModel(m)} style={{ ...dropdownItem, background: m === currentModel ? dropdownActiveBg : "transparent" }}>
                  <ModelProviderIcon model={m} size={16} />
                  <span className="flex-1">{m}</span>
                  {m === currentModel && <Check size={13} style={{ color: "var(--accent-primary)" }} />}
                </button>
              ))}
              <div className="mt-1 pt-1" style={{ borderTop: "1px solid var(--border-subtle)" }}>
                <button
                  onClick={() => {
                    setModelOpen(false);
                    openSettings("provider");
                  }}
                  style={{ ...dropdownItem, color: "var(--accent-primary)" }}
                >
                  Configure...
                </button>
              </div>
            </div>
          )}
        </div>

        {sendState === "queue" && onStop ? (
          <button
            type="button"
            onClick={onStop}
            title="Stop current response"
            aria-label="Stop current response"
            className="composer-stop-current-btn w-7 h-7 border-0 inline-flex items-center justify-center"
          >
            <Square size={11} fill="currentColor" />
          </button>
        ) : null}
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
  align = "left",
  className,
  icon,
  children,
}: {
  refEl: React.RefObject<HTMLDivElement>;
  open: boolean;
  setOpen: (open: boolean) => void;
  label: string;
  title: string;
  align?: "left" | "right";
  className?: string;
  icon?: React.ReactNode;
  children: React.ReactNode;
}) => (
  <div ref={refEl} className={className ? `${className} relative` : "relative"}>
    <button
      type="button"
      onClick={() => {
        setOpen(!open);
      }}
      className="composer-permission-btn"
      aria-expanded={open}
      aria-haspopup="listbox"
      style={{ ...pill, background: open ? "var(--surface-page)" : "var(--surface-soft)", borderColor: open ? "var(--border-subtle)" : "transparent" }}
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
  onClick,
}: {
  active: boolean;
  label: string;
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
      alignItems: "center",
      background: active ? dropdownActiveBg : "transparent",
      padding: "7px 9px",
    }}
  >
    <span style={{ display: "inline-flex", justifyContent: "center", color: active ? "var(--accent-primary)" : "transparent" }}>
      <Check size={13} />
    </span>
    <span style={{ minWidth: 0, color: "var(--text-primary)", fontWeight: active ? 600 : 500 }}>{label}</span>
  </button>
);

const ContextUsageRing = memo(() => {
  const contextUsage = useAppStore((s) => s.contextUsage);
  const budgetBuckets = useAppStore((s) => s.budgetBuckets);
  const totalBudgetPercent = useAppStore((s) => s.totalBudgetPercent);
  const conversationId = useAppStore((s) => s.conversationId);
  const currentModel = useAppStore((s) => s.currentModel);
  const appMode = useAppStore((s) => s.appMode);

  useEffect(() => {
    if (!conversationId) return;
    sendClientCommand({
      type: "session.usage.inspect",
      conversation_id: conversationId,
      source: "usage_ring_auto",
      silent: true,
    });
  }, [appMode, conversationId, currentModel]);

  return (
    <button
      type="button"
      onClick={() => getWebSocket()?.send({ type: "session.usage.inspect", source: "usage_ring" })}
      title="Context usage — click for details (/usage)"
      aria-label="Show context and token usage"
      className="composer-context-usage"
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

const permissionLabel = (mode: PermissionMode): string => {
  if (mode === "ask_permissions") return "Ask";
  if (mode === "plan") return "Plan";
  if (mode === "auto") return "Auto";
  if (mode === "bypass") return "Full access";
  return "Auto";
};

const effortOption = (level: EffortLevel) =>
  EFFORT_OPTIONS.find((option) => option.id === level) ?? EFFORT_OPTIONS[2];

const capabilityBool = (value: unknown): boolean =>
  value === true || (typeof value === "string" && value.trim().toLowerCase() === "true");

const normalizeCapabilityEffortLevels = (value: unknown): EffortLevel[] => {
  if (!Array.isArray(value)) return [];
  return value
    .map((item) => String(item || "").trim().toLowerCase())
    .filter((item): item is EffortLevel =>
      item === "none" ||
      item === "minimal" ||
      item === "low" ||
      item === "medium" ||
      item === "high" ||
      item === "xhigh" ||
      item === "max"
    );
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
});

const footerStyle = (compact: boolean): React.CSSProperties => ({
  marginTop: compact ? 4 : 0,
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
  gap: 7,
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
  gap: 9,
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
  const label = sendState === "stop"
    ? "Stop"
    : sendState === "queue"
      ? "Queue message"
      : sendState === "offline-queue"
        ? "Send when reconnected"
        : "Send";
  return (
    <button onClick={onSend} disabled={disabled} title={disabledReason || label} aria-label={label} className="btn-send composer-send-btn w-7 h-7 border-0 inline-flex items-center justify-center" style={sendIconButtonStyle(sendState, disabled)}>
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
