import { ArrowUp, BrainCircuit, Check, ChevronDown, Plus, ShieldCheck, Square } from "lucide-react";
import { memo, useEffect, useId, useRef, useState } from "react";
import type { SendButtonState } from "../lib/send-state";
import { useAppStore } from "../stores";
import { sendClientCommand } from "../protocol/ws-outbox";
import { uploadComposerFiles } from "./uploads";
import type { EffortLevel, PermissionMode } from "../stores/types";
import { formatModelLabel } from "../lib/model-label";
import { UsageRing } from "../shell/UsageRing";
import { openSettings } from "../lib/settings-navigation";
import { selectableModelsForProvider } from "../lib/provider-models";
import { ModelBrandIcon } from "../components/ModelBrandIcon";

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
  { id: "confirm", label: "询问" },
  { id: "plan", label: "规划" },
  { id: "auto", label: "自动" },
  { id: "bypass", label: "完全访问" },
];

type EffortOption = {
  id: EffortLevel;
  label: string;
  description: string;
};

const KNOWN_EFFORT_OPTIONS: Record<string, Omit<EffortOption, "id">> = {
  none: { label: "关闭", description: "关闭模型推理" },
  minimal: { label: "最低", description: "最低推理强度" },
  low: { label: "低", description: "低推理强度" },
  medium: { label: "中", description: "中等推理强度" },
  high: { label: "高", description: "高推理强度" },
  xhigh: { label: "极高", description: "极高推理强度" },
  max: { label: "最大", description: "最大推理强度" },
  ultra: { label: "Ultra", description: "Ultra 推理强度" },
};

const STANDARD_EFFORT_LEVELS: EffortLevel[] = ["low", "medium", "high"];
const EXTREME_EFFORT_LEVELS: EffortLevel[] = ["xhigh", "max", "ultra"];

export const FooterRow = memo(({ sendState, onSend, onStop, compact = false, minimal = false }: Props) => {
  const permissionMode = useAppStore((s) => s.permissionMode);
  const effortLevel = useAppStore((s) => s.effortLevel);
  const currentModel = useAppStore((s) => s.currentModel);
  const currentProvider = useAppStore((s) => s.currentProvider);
  const availableModels = useAppStore((s) => s.availableModels);
  const availableModelLabels = useAppStore((s) => s.availableModelLabels);
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
    const root = modelOpen
      ? dropdownRef.current
      : permissionOpen
        ? permissionRef.current
        : effortOpen
          ? effortRef.current
          : null;
    if (!root) return;
    const trigger = root.querySelector<HTMLButtonElement>(':scope > button[aria-expanded]');
    const options = () => Array.from(root.querySelectorAll<HTMLButtonElement>('[role="option"]:not(:disabled)'));
    queueMicrotask(() => {
      const items = options();
      (items.find((item) => item.getAttribute("aria-selected") === "true") ?? items[0])?.focus();
    });
    const handleMenuKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        setModelOpen(false);
        setPermissionOpen(false);
        setEffortOpen(false);
        queueMicrotask(() => trigger?.focus());
        return;
      }
      const items = options();
      if (!items.length) return;
      const current = items.indexOf(document.activeElement as HTMLButtonElement);
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        const next = event.key === "ArrowDown"
          ? (current + 1 + items.length) % items.length
          : (current - 1 + items.length) % items.length;
        items[next].focus();
      } else if (event.key === "Home" || event.key === "End") {
        event.preventDefault();
        items[event.key === "Home" ? 0 : items.length - 1].focus();
      }
    };
    root.addEventListener("keydown", handleMenuKeyDown);
    return () => root.removeEventListener("keydown", handleMenuKeyDown);
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
    queueMicrotask(() => dropdownRef.current?.querySelector<HTMLButtonElement>(':scope > button')?.focus());
  };

  const switchEffort = (level: EffortLevel) => {
    setEffortLevel(level);
    setEffortOpen(false);
    queueMicrotask(() => effortRef.current?.querySelector<HTMLButtonElement>(':scope > button')?.focus());
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
        title: "开启完全访问？",
        message:
          "完全访问允许智能体无需询问即可运行命令、编辑文件和使用网络。请仅在可信工作区中启用。",
        confirmLabel: "开启完全访问",
        cancelLabel: "取消",
        danger: true,
      });
      if (!ok) return;
    }
    setPermissionMode(mode);
    setPermissionOpen(false);
    queueMicrotask(() => permissionRef.current?.querySelector<HTMLButtonElement>(':scope > button')?.focus());
  };

  const togglePRAutomation = async (key: "autoFix" | "autoMerge") => {
    if (!prMonitor) return;
    const next = !prMonitor[key];
    if (next) {
      const label = key === "autoFix" ? "自动修复" : "自动合并";
      const { showConfirm } = await import("../overlays/DialogService");
      const ok = await showConfirm({
        title: `开启${label}？`,
        message: `${label}会在当前项目中持续生效；代码写入仍遵循当前权限策略。`,
        confirmLabel: "开启",
      });
      if (!ok) return;
    }
    const automationKey = key === "autoFix" ? "auto_fix" : "auto_merge";
    sendClientCommand({ type: "git.pr_automation.set", [automationKey]: next });
    useAppStore.getState().setPRMonitor({ ...prMonitor, [key]: next });
  };

  const modelLabel = availableModelLabels[currentModel] || formatModelLabel(currentModel, "选择模型");
  const selectableModels = selectableModelsForProvider(
    availableModels,
    currentModel,
    currentProvider,
    modelsSource,
  );
  const disabledReason = !currentModel.trim() ? "请先选择模型再发送" : undefined;

  const permLabel = permissionLabel(permissionMode);
  const providerCapabilities = runtimeCapabilities?.provider_capabilities;
  const capabilityEffortLevels = normalizeCapabilityEffortLevels(
    providerCapabilities?.reasoning_effort_levels,
  );
  const supportsReasoningEffort = capabilityBool(
    providerCapabilities?.reasoning_effort_supported ?? providerCapabilities?.reasoning_effort,
  ) && capabilityEffortLevels.length > 0;
  const composerEffortLevels = standardComposerEffortLevels(capabilityEffortLevels, effortLevel);
  const effortOptions = supportsReasoningEffort
    ? composerEffortLevels.map(effortOption)
    : [];
  // The pill must show the level the user is actually on. Substituting a
  // declared default (the old `?? medium` fallback) reported 中 for a session
  // configured at `minimal`, put the checkmark on the wrong row, and left the
  // real level unreachable from the menu.
  const effortIsDeclared = capabilityEffortLevels.includes(effortLevel);
  const selectedEffort = effortOptions.find((option) => option.id === effortLevel)
    ?? effortOption(effortLevel);
  const effortTitle = supportsReasoningEffort && !effortIsDeclared
    ? `模型推理强度：${selectedEffort.description}。当前 Provider 未声明支持该强度，请改选下方受支持的档位。`
    : `模型推理强度：${selectedEffort.description}。仅在当前 Provider/模型支持时生效，不改变工具迭代预算。`;
  const effortLabel = supportsReasoningEffort && !effortIsDeclared
    ? `${selectedEffort.label}（不支持）`
    : selectedEffort.label;

  return (
    <div className="flex flex-col gap-0">
      {prMonitor && (
        <div style={prMonitorStyle(prMonitor.ciStatus)}>
          <span className={prMonitor.ciStatus === "running" ? "thinking-pulse-dot" : undefined} style={prDotStyle(prMonitor.ciStatus)} />
          <span className="font-semibold" style={{ color: "var(--text-primary)" }}>PR #{prMonitor.prNumber}</span>
          <span style={{ color: "var(--text-muted)" }}>检查：{ciStatusLabel(prMonitor.ciStatus)}</span>
          {prMonitor.failedChecks && prMonitor.failedChecks.length > 0 && (
            <span style={{ color: "var(--state-danger)" }}>{prMonitor.failedChecks.length} 项失败</span>
          )}
          <span className="flex-1" />
          <ToggleChip
            label="自动修复"
            active={prMonitor.autoFix}
            onClick={() => togglePRAutomation("autoFix")}
          />
          <ToggleChip
            label="自动合并"
            active={prMonitor.autoMerge}
            onClick={() => togglePRAutomation("autoMerge")}
          />
        </div>
      )}

      <div
        className="composer-footer composer-footer-integrated"
        data-compact={compact ? "true" : "false"}
        data-minimal={minimal ? "true" : "false"}
        data-permission-mode={permissionMode}
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
            title="添加附件"
            className="composer-attach-btn"
            style={iconBtn(compact)}
            aria-label="添加附件"
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
            title={`权限：${permLabel}`}
            icon={<ShieldCheck size={14} />}
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
            icon={<BrainCircuit size={14} />}
          >
            {effortOptions.map((option) => (
              <MenuChoice
                key={option.id}
                active={selectedEffort.id === option.id}
                label={option.label}
                onClick={() => switchEffort(option.id)}
              />
            ))}
          </Picker>
        )}

        <div ref={dropdownRef} className="composer-model-picker relative">
          <button
            type="button"
            onClick={() => {
              setPermissionOpen(false);
              setEffortOpen(false);
              setModelOpen(!modelOpen);
            }}
            className="composer-model-select"
            aria-expanded={modelOpen}
            aria-haspopup="listbox"
            style={{ ...pill, background: modelOpen ? "var(--surface-page)" : "var(--surface-soft)", borderColor: modelOpen ? "var(--border-subtle)" : "transparent" }}
            title={currentModel || "选择模型"}
          >
            <ModelBrandIcon model={currentModel} size={15} />
            <span className="max-w-[180px] overflow-hidden text-ellipsis whitespace-nowrap">{modelLabel}</span>
            <ChevronDown size={14} className="opacity-55 ml-0.5 flex-shrink-0" />
          </button>
          {modelOpen && selectableModels.length > 0 && (
            <div className="mc-dropdown-menu composer-picker-menu" role="listbox" aria-label="选择模型" style={dropdownStyle("right")}>
              {selectableModels.map((m) => (
                  <button key={m} type="button" role="option" aria-selected={m === currentModel} onClick={() => switchModel(m)} style={{ ...dropdownItem, background: m === currentModel ? dropdownActiveBg : "transparent" }}>
                  <ModelBrandIcon model={m} size={16} />
                  <span className="flex-1">{availableModelLabels[m] || m}</span>
                  {m === currentModel && <Check size={14} style={{ color: "var(--accent-primary)" }} />}
                </button>
              ))}
              <div className="mt-1 pt-1" style={{ borderTop: "1px solid var(--border-subtle)" }}>
                <button
                  type="button"
                  role="option"
                  aria-selected="false"
                  onClick={() => {
                    setModelOpen(false);
                    openSettings("provider");
                  }}
                  style={{ ...dropdownItem, color: "var(--accent-primary)" }}
                >
                  配置模型…
                </button>
              </div>
            </div>
          )}
        </div>

        {sendState === "queue" && onStop ? (
          <button
            type="button"
            onClick={onStop}
             title="停止当前回复"
             aria-label="停止当前回复"
            className="composer-stop-current-btn w-7 h-7 border-0 inline-flex items-center justify-center"
          >
            <Square size={14} fill="currentColor" className="anim-icon-swap" />
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
      <ChevronDown size={14} className="opacity-55 ml-0.5" />
    </button>
    {open && <div className="mc-dropdown-menu composer-picker-menu" role="listbox" aria-label={title} style={dropdownStyle(align)}>{children}</div>}
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
    type="button"
    role="option"
    aria-selected={active}
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
      <Check size={14} />
    </span>
    <span style={{ minWidth: 0, color: "var(--text-primary)", fontWeight: active ? 600 : 500 }}>{label}</span>
  </button>
);

const effortOption = (level: EffortLevel): EffortOption => {
  const normalized = String(level || "").trim().toLowerCase() as EffortLevel;
  const known = KNOWN_EFFORT_OPTIONS[normalized];
  if (known) return { id: normalized, ...known };
  return {
    id: normalized,
    label: normalized,
    description: `Provider 声明的推理强度：${normalized}`,
  };
};

const capabilityBool = (value: unknown): boolean =>
  value === true || (typeof value === "string" && value.trim().toLowerCase() === "true");

const normalizeCapabilityEffortLevels = (value: unknown): EffortLevel[] => {
  if (!Array.isArray(value)) return [];
  return Array.from(new Set(
    value
      .map((item) => String(item || "").trim().toLowerCase())
      .filter(Boolean),
  )) as EffortLevel[];
};

const standardComposerEffortLevels = (
  levels: EffortLevel[],
  current: EffortLevel,
): EffortLevel[] => {
  const declared = new Set(levels);
  if (!STANDARD_EFFORT_LEVELS.every((level) => declared.has(level))) return levels;
  const keep = new Set<EffortLevel>(STANDARD_EFFORT_LEVELS);
  const extreme = EXTREME_EFFORT_LEVELS.find((level) => declared.has(level));
  if (extreme) keep.add(extreme);
  // Narrowing the menu must never hide the level the session is actually using:
  // that is what made a `minimal` configuration unreselectable while the pill
  // claimed 中. Provider order is preserved so the menu reads low → high.
  if (declared.has(current)) keep.add(current);
  return levels.filter((level) => keep.has(level));
};

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
      onClick={() => sendClientCommand({
        type: "session.usage.inspect",
        conversation_id: conversationId || undefined,
        source: "usage_ring",
      })}
       title="上下文用量，点击查看详情（/usage）"
       aria-label="显示上下文和令牌用量"
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
      background: active ? "var(--surface-active)" : "var(--surface-soft)",
      color: active ? "var(--text-primary)" : "var(--text-muted)",
      fontSize: "var(--mc-font-secondary)",
    }}
  >
    {label}
  </button>
);

const permissionLabel = (mode: PermissionMode): string => {
  if (mode === "confirm") return "询问";
  if (mode === "plan") return "规划";
  if (mode === "auto") return "自动";
  if (mode === "bypass") return "完全访问";
  return "自动";
};

const ciStatusLabel = (status: string): string => ({
  running: "运行中",
  passed: "已通过",
  failed: "失败",
}[status] ?? status);

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
  zIndex: "var(--z-sticky)",
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
    ? "停止当前回复"
    : sendState === "queue"
      ? "将消息加入队列"
      : sendState === "offline-queue"
         ? "连接恢复后发送"
         : "发送";
  return (
    <button onClick={onSend} disabled={disabled} title={disabledReason || label} aria-label={label} className="btn-send composer-send-btn w-7 h-7 border-0 inline-flex items-center justify-center" style={sendIconButtonStyle(sendState, disabled)}>
      {sendState === "stop" ? <Square size={14} fill="currentColor" className="anim-icon-swap" /> : <ArrowUp size={16} strokeWidth={1.8} className="anim-icon-swap" />}
    </button>
  );
});
SendIconBtn.displayName = "SendIconBtn";

const sendIconButtonStyle = (sendState: SendButtonState, disabled: boolean): React.CSSProperties => ({
  borderRadius: "var(--radius-full)",
  background: sendState === "stop" ? "var(--text-primary)" : disabled ? "var(--surface-active)" : "var(--accent-primary)",
  color: disabled ? "var(--text-muted)" : "var(--text-on-accent)",
  cursor: disabled ? "not-allowed" : "pointer",
});
