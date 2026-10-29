import { useI18n, type Translate, type TranslationKey } from "../i18n";
import { ArrowUp, Check, ChevronRight, Folder, Globe, Hand, ListChecks, RotateCcw, ShieldAlert, Square, TerminalSquare, X } from "../lib/icons";
import { memo, useEffect, useId, useRef, useState } from "react";
import type { SendButtonState } from "../lib/send-state";
import { useAppStore } from "../stores";
import { RealtimeComposerActions } from "./RealtimeComposerActions";
import { sendClientCommand } from "../protocol/ws-outbox";
import { uploadComposerFiles, uploadComposerRemoteFiles } from "./uploads";
import type { EffortLevel, PermissionMode } from "../stores/types";
import { formatModelLabel } from "../lib/model-label";
import { UsageRing } from "../shell/UsageRing";
import { openSettings } from "../lib/settings-navigation";
import { selectableModelsForProvider } from "../lib/provider-models";
import { workspaceRootsEqual } from "../lib/workspace-path";
import { pushToast } from "../overlays/ToastContainer";
import { Button } from "../components/Button";
import { ComposerAddMenu } from "./ComposerAddMenu";
import { PromptSourceIcon } from "../lib/prompt-source-icons";
import { isDesktop, openExternal } from "../desktop/runtime";
import { useDesktopPreferences } from "../hooks/useDesktopPreferences";
import { capabilityFeatureEnabled } from "../protocol/capabilities";
import { Target } from "../lib/icons";
import { enterComposerGoalMode, updateThreadGoal } from "./goal-state";
import { isConversationDraftMaterializing } from "../stores/conversation-drafts";
import { isConversationMessageAwaitingReceipt } from "../chat/sendChatMessage";
import "./goal.css";

interface Props {
  sendState: SendButtonState;
  isStopping?: boolean;
  submitting?: boolean;
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

const PERMISSION_MODES = [
  { id: "auto", label: "请求批准", icon: Hand, description: "编辑外部文件和使用互联网时始终询问" },
  { id: "bypass", label: "完全访问", icon: ShieldAlert, description: "可不受限制地访问互联网和你电脑上的任何文件" },
] as const;
const PERMISSIONS_HELP_URL = "https://developers.openai.com/codex/concepts/sandboxing#how-you-control-it";
const openPermissionsHelp = () => isDesktop() ? openExternal(PERMISSIONS_HELP_URL) : window.open(PERMISSIONS_HELP_URL, "_blank", "noopener,noreferrer");

type EffortOption = {
  id: EffortLevel;
  label: string;
  description: string;
};

const KNOWN_EFFORT_OPTIONS: Record<string, { label: TranslationKey | "Ultra"; description: TranslationKey }> = {
  none: { label: "关闭", description: "关闭模型推理" },
  minimal: { label: "最低", description: "最低推理强度" },
  low: { label: "低", description: "低推理强度" },
  medium: { label: "中", description: "中等推理强度" },
  high: { label: "高", description: "高推理强度" },
  xhigh: { label: "极高", description: "极高推理强度" },
  max: { label: "最大", description: "最大推理强度" },
  ultra: { label: "Ultra", description: "Ultra 推理强度" },
};

export const FooterRow = memo(({ sendState, isStopping = false, submitting = false, onSend, onStop, compact = false, minimal = false }: Props) => {
  const { t: tr } = useI18n();
  const { preferences } = useDesktopPreferences();
  const permissionMode = useAppStore((s) => s.permissionMode);
  const agentMode = useAppStore((s) => s.agentMode);
  const goalMode = useAppStore((s) => s.goalMode && capabilityFeatureEnabled(s.runtimeCapabilities, "goals"));
  const setAgentMode = useAppStore((s) => s.setAgentMode);
  const effortLevel = useAppStore((s) => s.effortLevel);
  const conversationId = useAppStore((s) => s.conversationId);
  const activeDraftId = useAppStore(s => s.activeConversationDraftId);
  const composerDraftsReady = useAppStore(s => s.composerDraftsReady);
  const draftContext = useAppStore(s => s.activeConversationDraftId ? s.conversationDrafts[s.activeConversationDraftId] : undefined);
  const materializing = useAppStore(isConversationDraftMaterializing);
  const draftControlsBusy = !composerDraftsReady || Boolean(activeDraftId && !draftContext
    || draftContext?.selectionLoading || draftContext?.preferencesLoading || draftContext?.materializing);
  const pendingConversationSwitchId = useAppStore((s) => s.pendingConversationSwitchId);
  const awaitingInputReceipt = useAppStore(s => Boolean(s.conversationId && isConversationMessageAwaitingReceipt(s.conversationId)));
  const modeControlsBusy = draftControlsBusy || materializing || submitting || awaitingInputReceipt || Boolean(pendingConversationSwitchId);
  const workingDirectory = useAppStore((s) => s.workingDirectory);
  const currentModel = useAppStore((s) => s.currentModel);
  const projectedDefaultModel = useAppStore((s) => s.defaultModelSelection);
  const modelSelectionMode = useAppStore((s) => s.modelSelectionMode);
  const availableModelOptions = useAppStore((s) => s.availableModelOptions);
  const defaultModelSelection = projectedDefaultModel && projectedDefaultModel.conversationId === (conversationId || "")
    && workspaceRootsEqual(projectedDefaultModel.workingDirectory, workingDirectory) ? projectedDefaultModel : null;
  const currentProvider = useAppStore((s) => s.currentProvider);
  const availableModels = useAppStore((s) => s.availableModels);
  const availableModelLabels = useAppStore((s) => s.availableModelLabels);
  const modelsSource = useAppStore((s) => s.modelsSource);
  const runtimeCapabilities = useAppStore((s) => s.runtimeCapabilities);
  const prMonitor = useAppStore((s) => s.prMonitor);
  const prStatusIssue = useAppStore((s) => s.prStatusIssue);
  const setPermissionMode = useAppStore((s) => s.setPermissionMode);
  const setEffortLevel = useAppStore((s) => s.setEffortLevel);
  const setModelSelection = useAppStore((s) => s.setModelSelection);
  const fileInputId = useId();
  const fileRef = useRef<HTMLInputElement>(null);
  const filePickerActive = useRef(false);
  useEffect(() => useAppStore.subscribe((state, previous) => {
    if (state.conversationId !== previous.conversationId || state.activeConversationDraftId !== previous.activeConversationDraftId || state.workingDirectory !== previous.workingDirectory
      || state.pendingConversationSwitchId !== previous.pendingConversationSwitchId
      || state.pendingConversationCreateId !== previous.pendingConversationCreateId) {
      filePickerActive.current = false;
      remotePickerController.current?.abort();
    }
  }), []);
  const remotePickerController = useRef<AbortController | null>(null);
  useEffect(() => () => { filePickerActive.current = false; remotePickerController.current?.abort(); }, []);
  const remoteHost = window.__MINICODE_RUNTIME__?.remoteHost;
  const [modelOpen, setModelOpen] = useState(false);
  const [permissionOpen, setPermissionOpen] = useState(false);
  const [effortOpen, setEffortOpen] = useState(false);
  const [addOpen, setAddOpen] = useState(false);
  const footerRef = useRef<HTMLDivElement>(null);
  useEffect(() => { setAddOpen(false); setModelOpen(false); setEffortOpen(false); setPermissionOpen(false); }, [conversationId, activeDraftId, workingDirectory, pendingConversationSwitchId]);
  useEffect(() => {
    const openAddMenu = () => { window.dispatchEvent(new Event("composer:focus")); setAddOpen(true); setModelOpen(false); setEffortOpen(false); setPermissionOpen(false); };
    window.addEventListener("composer:open-add-menu", openAddMenu);
    return () => window.removeEventListener("composer:open-add-menu", openAddMenu);
  }, []);
  const [effortDraft, setEffortDraft] = useState<{ level: EffortLevel; model: string; owner: string | null | undefined; accepted: boolean } | null>(null);
  const effortRequest = useRef(0);
  const composerOwnerId = conversationId || activeDraftId;
  const displayedEffort = effortDraft?.model === currentModel && effortDraft.owner === composerOwnerId
    ? effortDraft.level : effortLevel;
  useEffect(() => {
    if (effortDraft && (effortDraft.model !== currentModel || effortDraft.owner !== composerOwnerId
      || (effortDraft.accepted && effortDraft.level === effortLevel))) setEffortDraft(null);
  }, [currentModel, composerOwnerId, effortLevel, effortDraft]);
  const dropdownRef = useRef<HTMLDivElement>(null);
  const permissionRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const close = (e: MouseEvent) => {
      if (addOpen && !footerRef.current!.contains(e.target as Node)) setAddOpen(false);
      if ((modelOpen || effortOpen) && dropdownRef.current && !dropdownRef.current.contains(e.target as Node)) {
        setModelOpen(false);
        setEffortOpen(false);
      }
      if (permissionOpen && permissionRef.current && !permissionRef.current.contains(e.target as Node)) setPermissionOpen(false);
    };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, [modelOpen, permissionOpen, effortOpen, addOpen]);

  useEffect(() => {
    const root = modelOpen || effortOpen
      ? dropdownRef.current
      : permissionOpen
        ? permissionRef.current
        : null;
    if (!root) return;
    const trigger = root.querySelector<HTMLButtonElement>(':scope > button[aria-expanded]');
    const options = () => Array.from(root.querySelectorAll<HTMLElement>('[role="option"]:not(:disabled), .composer-effort-model-toggle:not(:disabled), .composer-effort-reset:not(:disabled), input[type="range"]'));
    queueMicrotask(() => {
      const items = options();
      const selected = root.querySelector<HTMLInputElement>('input[type="range"]')
        ?? items.find((item) => item.getAttribute("aria-selected") === "true") ?? items[0];
      selected?.focus({ preventScroll: true });
      if (modelOpen) selected?.scrollIntoView({ block: "nearest" });
    });
    const handleMenuKeyDown = (event: KeyboardEvent) => {
      if (event.isComposing || event.keyCode === 229) return;
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        setModelOpen(false);
        setPermissionOpen(false);
        setEffortOpen(false);
        queueMicrotask(() => trigger?.focus({ preventScroll: true }));
        return;
      }
      const items = options();
      if (!items.length) return;
      const current = items.indexOf(document.activeElement as HTMLElement);
      const slider = event.target instanceof HTMLInputElement && event.target.type === "range";
      if (event.key === "Enter" && slider) {
        event.preventDefault(); event.stopPropagation();
        setEffortOpen(false);
        queueMicrotask(() => trigger?.focus({ preventScroll: true }));
        return;
      }
      if (event.key === "Tab") {
        event.preventDefault(); event.stopPropagation();
        if (effortOpen) items[(current + (event.shiftKey ? -1 : 1) + items.length) % items.length].focus();
        return;
      }
      if (slider && !["ArrowDown", "ArrowUp"].includes(event.key)) return;
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        event.stopPropagation();
        const next = effortOpen ? (current + (event.key === "ArrowDown" ? 1 : -1) + items.length) % items.length
          : current < 0 ? event.key === "ArrowDown" ? 0 : items.length - 1
          : Math.max(0, Math.min(items.length - 1, current + (event.key === "ArrowDown" ? 1 : -1)));
        items[next].focus();
      } else if (event.key === "Home" || event.key === "End") {
        event.preventDefault();
        event.stopPropagation();
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
    const selected = Array.from(files);
    const accepted = filePickerActive.current;
    filePickerActive.current = false;
    e.target.value = "";
    if (accepted) uploadComposerFiles(selected);
  };

  const openFilePicker = (event?: React.MouseEvent | React.KeyboardEvent) => {
    event?.preventDefault();
    event?.stopPropagation();
    remotePickerController.current?.abort();
    remotePickerController.current = null;
    filePickerActive.current = true;
    fileRef.current?.click();
  };

  const openRemoteFilePicker = async () => {
    remotePickerController.current?.abort();
    const controller = new AbortController();
    remotePickerController.current = controller;
    filePickerActive.current = true;
    try {
      const [{ showRemoteFilePicker }, { remoteDirectoryEntries }] = await Promise.all([
        import("../overlays/RemoteFilePicker"), import("../desktop/runtime"),
      ]);
      if (controller.signal.aborted) return;
      const paths = await showRemoteFilePicker({ initialDirectoryPath: workingDirectory || remoteHost!.home,
        platform: remoteHost!.platform, listDirectory: path => remoteDirectoryEntries(path, false), signal: controller.signal });
      if (!controller.signal.aborted && filePickerActive.current && paths?.length) uploadComposerRemoteFiles(paths);
    } catch (error) {
      if (!controller.signal.aborted) pushToast(error instanceof Error ? error.message : String(error), "error");
    } finally {
      if (remotePickerController.current === controller) { remotePickerController.current = null; filePickerActive.current = false; }
    }
  };

  const editGoal = () => {
    enterComposerGoalMode();
    window.dispatchEvent(new Event("composer:focus"));
  };

  const togglePlanMode = () => {
    const owner = useAppStore.getState();
    if (modeControlsBusy || isConversationDraftMaterializing(owner)
      || (owner.conversationId && isConversationMessageAwaitingReceipt(owner.conversationId))) return;
    if (owner.agentMode === "plan") { void owner.setAgentMode("build"); return; }
    useAppStore.setState({ goalMode: false });
    if (owner.activeGoal && owner.conversationId && capabilityFeatureEnabled(owner.runtimeCapabilities, "goals")) {
      void updateThreadGoal(owner.conversationId, "clear")
        .catch(error => pushToast(error instanceof Error ? error.message : tr("目标操作失败"), "error"));
    }
    void owner.setAgentMode("plan");
    window.dispatchEvent(new Event("composer:focus"));
  };

  const clearGoalMode = () => {
    const owner = useAppStore.getState();
    if (modeControlsBusy || isConversationDraftMaterializing(owner)
      || (owner.conversationId && isConversationMessageAwaitingReceipt(owner.conversationId))) return;
    useAppStore.setState({ goalMode: false });
    if (owner.activeGoal && owner.conversationId) void updateThreadGoal(owner.conversationId, "clear")
      .catch(error => pushToast(error instanceof Error ? error.message : tr("目标操作失败"), "error"));
    window.dispatchEvent(new Event("composer:focus"));
  };

  const confirmUltraWithFullAccess = async (level: EffortLevel | undefined, owner: ReturnType<typeof useAppStore.getState>) => {
    if (level !== "ultra" || owner.effortLevel === "ultra" || owner.permissionMode !== "bypass") return true;
    const fallback = owner.runtimeCapabilities?.permission?.available_modes?.find(mode => mode !== "bypass");
    const { showChoice } = await import("../overlays/DialogService");
    const choice = await showChoice({ title: tr("要将 Ultra 与完全访问搭配使用吗？"), titleIcon: <ShieldAlert size={17} />,
      message: fallback ? tr("开启 Ultra 和完全访问后，MiniCode 可在未经询问的前提下运行命令、联网检索、编辑电脑内任意路径文件，同时启用深度推理。你可切换至管控更严格的权限模式，或使用完全访问。")
        : tr("开启 Ultra 和完全访问后，MiniCode 可在未经询问的前提下运行命令、联网检索、编辑电脑内任意路径文件，同时启用深度推理。目前没有可用的更严格权限模式。"),
      confirmLabel: tr("使用完全访问"), cancelLabel: tr("继续"), cancelDisabled: !fallback, danger: true });
    const current = useAppStore.getState();
    if (choice === null || current.conversationId !== owner.conversationId || current.activeConversationDraftId !== owner.activeConversationDraftId || !workspaceRootsEqual(current.workingDirectory, owner.workingDirectory)) return false;
    if (choice === false && (!fallback || !await setPermissionMode(fallback))) return false;
    const after = useAppStore.getState();
    return after.conversationId === owner.conversationId && after.activeConversationDraftId === owner.activeConversationDraftId && workspaceRootsEqual(after.workingDirectory, owner.workingDirectory);
  };

  const switchModel = async (selection: { mode: "model"; model: string } | { mode: "default" }) => {
    const owner = useAppStore.getState();
    const option = selection.mode === "model" ? availableModelOptions.find(option => option.model === selection.model) : null;
    const nextEffort = selection.mode === "default" ? defaultModelSelection?.reasoningEffort
      : option?.reasoningEffortLevels.includes(owner.effortLevel) ? owner.effortLevel : option?.defaultReasoningEffort;
    if (!await confirmUltraWithFullAccess(nextEffort, owner)) return;
    const admitted = useAppStore.getState();
    if (admitted.conversationId !== owner.conversationId || admitted.activeConversationDraftId !== owner.activeConversationDraftId || !workspaceRootsEqual(admitted.workingDirectory, owner.workingDirectory)) return;
    if (!await setModelSelection(selection)) return;
    const current = useAppStore.getState();
    if (current.conversationId !== owner.conversationId || current.activeConversationDraftId !== owner.activeConversationDraftId || !workspaceRootsEqual(current.workingDirectory, owner.workingDirectory)) return;
    setModelOpen(false);
    const capabilities = current.runtimeCapabilities?.provider_capabilities;
    const selectedOption = current.availableModelOptions.find(option => option.model === current.currentModel);
    const selectedLevels = selectedOption
      ? normalizeCapabilityEffortLevels(selectedOption.reasoningEffortLevels)
      : normalizeCapabilityEffortLevels(capabilities?.reasoning_effort_levels);
    const visibleLevels = selectedLevels.filter(level => level !== "max" && level !== "ultra"
      || preferences?.enabledReasoningEfforts === undefined || preferences.enabledReasoningEfforts.includes(level));
    const hasEffort = (capabilities?.model === undefined || capabilities.model === current.currentModel)
      && capabilityBool(capabilities?.reasoning_effort_supported ?? capabilities?.reasoning_effort)
      && visibleLevels.length > 1 && selectedLevels.includes(current.effortLevel);
    setEffortOpen(hasEffort);
    if (!hasEffort) queueMicrotask(() => dropdownRef.current?.querySelector<HTMLButtonElement>(':scope > button')?.focus());
  };

  const switchEffort = async (level: EffortLevel) => {
    if (level === displayedEffort) return;
    const owner = useAppStore.getState();
    const request = ++effortRequest.current;
    const draft = { level, model: currentModel, owner: composerOwnerId, accepted: false };
    setEffortDraft(draft);
    if (!await confirmUltraWithFullAccess(level, owner)) {
      if (request === effortRequest.current) setEffortDraft(null);
      return;
    }
    const admitted = useAppStore.getState();
    if (admitted.conversationId !== owner.conversationId || admitted.activeConversationDraftId !== owner.activeConversationDraftId || !workspaceRootsEqual(admitted.workingDirectory, owner.workingDirectory)) {
      if (request === effortRequest.current) setEffortDraft(null);
      return;
    }
    const accepted = await setEffortLevel(level);
    if (request === effortRequest.current) setEffortDraft(accepted ? { ...draft, accepted: true } : null);
  };

  const switchPermissionMode = async (mode: PermissionMode) => {
    const owner = useAppStore.getState();
    if (mode === "bypass") {
      setPermissionOpen(false);
      const { showConfirm } = await import("../overlays/DialogService");
      const ok = await showConfirm({
        title: tr("要开启完全访问吗？"),
        titleIcon: <ShieldAlert size={17} />,
        message: <FullAccessDetails tr={tr} />,
        confirmLabel: tr("确认"),
        cancelLabel: tr("取消"),
        danger: true,
      });
      if (!ok) return;
    }
    const current = useAppStore.getState();
    if (current.conversationId !== owner.conversationId
      || current.activeConversationDraftId !== owner.activeConversationDraftId
      || !workspaceRootsEqual(current.workingDirectory, owner.workingDirectory)) {
      pushToast(tr("会话或工作区已切换，未应用原完全访问确认；请重新选择权限。"), "warning");
      return;
    }
    await setPermissionMode(mode);
    setPermissionOpen(false);
    queueMicrotask(() => permissionRef.current?.querySelector<HTMLButtonElement>(':scope > button')?.focus());
  };

  const togglePRAutomation = async (key: "autoFix" | "autoMerge") => {
    if (!prMonitor) return;
    const owner = useAppStore.getState();
    const next = !prMonitor[key];
    if (next) {
      const label = key === "autoFix" ? tr("自动修复") : tr("自动合并");
      const { showConfirm } = await import("../overlays/DialogService");
      const ok = await showConfirm({
        title: tr("开启{label}？", { label }),
        message: tr("{label}会在当前项目中持续生效；代码写入仍遵循当前权限策略。", { label }),
        confirmLabel: tr("开启"),
      });
      if (!ok) return;
    }
    const current = useAppStore.getState();
    if (current.conversationId !== owner.conversationId
      || !workspaceRootsEqual(current.workingDirectory, owner.workingDirectory)
      || current.prMonitor?.prNumber !== prMonitor.prNumber
      || current.prMonitor?.prUrl !== prMonitor.prUrl) {
      pushToast(tr("会话、工作区或 PR 已变化，原自动化确认已取消；请重新选择。"), "warning");
      return;
    }
    const automationKey = key === "autoFix" ? "auto_fix" : "auto_merge";
    const scope = {
      ...(owner.conversationId ? { conversation_id: owner.conversationId } : {}),
      workspace_root: owner.workingDirectory,
    };
    // git.pr_status is the semantic projection; admission alone does not enable automation.
    sendClientCommand({ type: "git.pr_automation.set", ...scope, [automationKey]: next,
      ...(key === "autoMerge" ? { expected_pr_number: prMonitor.prNumber, expected_branch: owner.workspaceGit?.branch || "" } : {}),
    });
  };

  const modelLabel = formatModelLabel(availableModelLabels[currentModel] || currentModel, tr("选择模型"));
  const selectableModels = selectableModelsForProvider(
    availableModels,
    currentModel,
    currentProvider,
    modelsSource,
  );
  const disabledReason = sendState === "disabled" && !currentModel.trim() ? tr("请先选择模型再发送") : undefined;

  const permLabel = permissionLabel(permissionMode, tr);
  const PermissionIcon = permissionMode === "bypass" ? ShieldAlert : Hand;
  const availablePermissionModes = runtimeCapabilities?.permission?.available_modes;
  const defaultPermissionMode = permissionMode !== "bypass" && availablePermissionModes?.includes(permissionMode)
    ? permissionMode : availablePermissionModes?.find(mode => mode !== "bypass");
  const providerCapabilities = runtimeCapabilities?.provider_capabilities;
  const capabilityEffortLevels = normalizeCapabilityEffortLevels(
    providerCapabilities?.reasoning_effort_levels,
  );
  const selectedModelOption = availableModelOptions.find(option => option.model === currentModel);
  // The provider capability ladder can describe the adapter in general. The
  // model catalog is the authoritative per-model contract; when it declares
  // no effort levels, keep the composer focused on model selection instead of
  // presenting a retained level that this model cannot use.
  const modelEffortLevels = selectedModelOption
    ? normalizeCapabilityEffortLevels(selectedModelOption.reasoningEffortLevels)
    : capabilityEffortLevels;
  const supportsReasoningEffort = (providerCapabilities?.model === undefined || providerCapabilities.model === currentModel)
    && capabilityBool(
    providerCapabilities?.reasoning_effort_supported ?? providerCapabilities?.reasoning_effort,
  ) && modelEffortLevels.length > 0;
  const visibleReasoningEfforts = preferences?.enabledReasoningEfforts;
  const composerEffortLevels = modelEffortLevels.filter(level => level !== "max" && level !== "ultra"
    || visibleReasoningEfforts === undefined || visibleReasoningEfforts.includes(level));
  const effortOptions = supportsReasoningEffort
    ? composerEffortLevels.map((level) => {
      const option = effortOption(level, tr);
      const wireMap = providerCapabilities?.reasoning_effort_wire_map as Record<string, string> | undefined;
      return level === "ultra" && wireMap?.ultra
        ? { ...option, description: tr("主动多智能体协作；模型请求使用{effort}推理", { effort: effortOption(wireMap.ultra, tr).label }) }
        : option;
    })
    : [];
  // The pill must show the level the user is actually on. Substituting a
  // declared default (the old `?? medium` fallback) reported 中 for a session
  // configured at `minimal`, put the checkmark on the wrong row, and left the
  // real level unreachable from the menu.
  const effortIsDeclared = modelEffortLevels.includes(displayedEffort);
  const showReasoningEffort = supportsReasoningEffort && effortIsDeclared && composerEffortLevels.length > 1;
  const selectedEffort = effortOptions.find((option) => option.id === displayedEffort)
    ?? effortOption(displayedEffort, tr);
  const effortTitle = tr("选择模型");
  const effortLabel = selectedEffort.label;

  return (
    <div ref={footerRef} className="flex flex-col gap-0">
      {addOpen && <ComposerAddMenu workspaceRoot={workingDirectory} planMode={agentMode === "plan"} planAvailable={!modeControlsBusy}
        showGoal={capabilityFeatureEnabled(runtimeCapabilities, "goals")} goalAvailable={!modeControlsBusy}
        onFiles={openFilePicker}
        onRemoteFiles={remoteHost ? () => void openRemoteFilePicker() : undefined}
        onGoal={() => void editGoal()} onPlan={togglePlanMode}
        onPlugin={(id, name) => useAppStore.getState().addSelectedMention({ kind: "plugin", name, configName: id, path: `plugin://${id}` })}
        onClose={restoreFocus => { setAddOpen(false); if (restoreFocus) window.dispatchEvent(new Event("composer:focus")); }} />}
      {prMonitor && (
        <div style={prMonitorStyle(prMonitor.ciStatus)}>
          <span className={prMonitor.ciStatus === "running" ? "thinking-pulse-dot" : undefined} style={prDotStyle(prMonitor.ciStatus)} />
          <span className="font-semibold" style={{ color: "var(--text-primary)" }}>PR #{prMonitor.prNumber}</span>
          <span style={{ color: "var(--text-muted)" }}>{tr("检查：")}{ciStatusLabel(prMonitor.ciStatus, tr)}</span>
          {prMonitor.failedChecks && prMonitor.failedChecks.length > 0 && (
            <span style={{ color: "var(--state-danger)" }}>{prMonitor.failedChecks.length} {tr("项失败")}</span>
          )}
          <span className="flex-1" />
          <ToggleChip
            label={tr("自动修复")}
            active={prMonitor.autoFix}
            disabled={Boolean(prStatusIssue)}
            onClick={() => togglePRAutomation("autoFix")}
          />
          <ToggleChip
            label={tr("自动合并")}
            active={prMonitor.autoMerge}
            disabled={Boolean(prStatusIssue)}
            onClick={() => togglePRAutomation("autoMerge")}
          />
        </div>
      )}

      {prStatusIssue && <div role="status" style={{ display: "flex", alignItems: "center", gap: 8, padding: "6px 8px", fontSize: "var(--text-xs)", color: "var(--text-muted)" }}>
        <ShieldAlert size={13} aria-hidden="true" />
        <span style={{ flex: 1 }}>
          {prStatusIssue.code === "auth_required" ? tr("GitHub 尚未连接。")
            : prStatusIssue.code === "gh_unavailable" ? tr("GitHub 连接组件未就绪。")
            : prStatusIssue.code === "permission_denied" ? tr("GitHub 无权访问当前仓库。")
            : tr("PR 状态读取失败：{message}", { message: prStatusIssue.message })}
          {prMonitor && " 当前显示上次成功读取的结果。"}
        </span>
        <Button variant="ghost" size="sm" onClick={() => openSettings("workspaceGit")}>{prStatusIssue.code === "auth_required" ? tr("连接 GitHub") : tr("查看连接设置")}</Button>
        <Button variant="ghost" size="sm" onClick={() => {
          const owner = useAppStore.getState();
          sendClientCommand({ type: "git.pr_status", conversation_id: owner.conversationId!, workspace_root: owner.workingDirectory });
        }}>{tr("重试")}</Button>
      </div>}

      <div
        className="composer-footer composer-footer-integrated"
        data-compact={compact ? "true" : "false"}
        data-minimal={minimal ? "true" : "false"}
        data-permission-mode={permissionMode}
        style={footerStyle}
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
            title={tr("添加文件等内容")}
            className="composer-attach-btn"
            style={iconBtn}
            aria-label={tr("添加文件等内容")}
            aria-haspopup="menu"
            aria-expanded={addOpen}
            disabled={Boolean(pendingConversationSwitchId)}
            onMouseDown={event => event.preventDefault()}
            onClick={() => { window.dispatchEvent(new Event("composer:focus")); setAddOpen(value => !value); setModelOpen(false); setEffortOpen(false); setPermissionOpen(false); }}
          >
            <PromptSourceIcon icon="plus" />
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
            title={tr("更改权限")}
            icon={<PermissionIcon size={15} />}
            disabled={availablePermissionModes === undefined || Boolean(pendingConversationSwitchId) || draftControlsBusy}
            warning={permissionMode === "bypass"}
          >
            <div className="composer-permission-heading"><span>{tr("应如何批准操作？")}</span><button type="button" onClick={openPermissionsHelp}>{tr("了解更多")}</button></div>
            {PERMISSION_MODES.filter(mode => mode.id !== "bypass" || preferences?.showFullAccess !== false).map((mode) => (
              <MenuChoice
                key={mode.id}
                active={mode.id === "bypass" ? permissionMode === "bypass" : permissionMode !== "bypass"}
                label={tr(mode.label)}
                icon={<mode.icon size={17} />}
                description={mode.id === "bypass" && !availablePermissionModes?.includes("bypass") ? tr("被 requirements.toml 禁用") : tr(mode.description)}
                disabled={mode.id === "bypass" ? !availablePermissionModes?.includes("bypass") : !defaultPermissionMode}
                warning={mode.id === "bypass"}
                onClick={() => {
                  void switchPermissionMode(mode.id === "bypass" ? "bypass" : defaultPermissionMode!);
                }}
              />
            ))}
          </Picker>

          {agentMode === "plan" && <button type="button" className="composer-plan-chip" disabled={modeControlsBusy} aria-label={tr("关闭计划模式")} title={tr("计划模式")} onClick={() => setAgentMode("build")}><ListChecks size={15} /><span>{tr("计划")}</span><X size={13} /></button>}
          {goalMode && <button type="button" className="composer-goal-mode" disabled={modeControlsBusy} aria-label={tr("清除目标")} title={tr("清除目标")}
            onClick={clearGoalMode}><Target className="goal-mode-target" size={14} /><X className="goal-mode-clear" size={14} />{tr("目标")}</button>}

        </div>

        <span className="composer-footer-spacer" aria-hidden="true" />

        {!minimal && (!isDesktop() || preferences?.showContextWindowUsage) && <ContextUsageRing />}

        <div className="composer-model-controls" role="group" aria-label={tr("模型与推理强度")}>
        <div ref={dropdownRef} className="composer-model-picker relative" onBlur={(event) => {
          if (!event.currentTarget.contains(event.relatedTarget)) { setModelOpen(false); setEffortOpen(false); }
        }}>
          <button
            type="button"
            onClick={() => {
              setPermissionOpen(false);
              const open = modelOpen || effortOpen;
              setModelOpen(!open && !showReasoningEffort);
              setEffortOpen(!open && showReasoningEffort);
            }}
            className="composer-model-select"
            data-reasoning-supported={showReasoningEffort}
            aria-label={showReasoningEffort ? tr("模型与推理强度：{model}，{effort}", { model: modelLabel, effort: effortLabel }) : undefined}
            aria-expanded={modelOpen || effortOpen}
            disabled={Boolean(pendingConversationSwitchId) || draftControlsBusy}
            aria-haspopup={showReasoningEffort ? "dialog" : "listbox"}
            style={pill}
            title={showReasoningEffort ? effortTitle : currentModel || tr("选择模型")}
          >
            <span className="composer-model-trigger-label">{modelLabel}</span>
            {showReasoningEffort && <span className="composer-model-trigger-effort" data-effort={displayedEffort}>{effortLabel}</span>}
            <PromptSourceIcon icon="model-chevron-down" />
          </button>
          {modelOpen && (
            <div className="mc-dropdown-menu composer-picker-menu composer-model-menu" role="listbox" aria-label={tr("选择模型")} style={{ ...dropdownStyle("right"), overflow: "hidden" }}>
              <div className="composer-menu-heading">{tr("选择模型")}</div>
              <div className="composer-model-options">
              {selectableModels.length === 0 && <div className="composer-menu-empty">{tr("尚未配置模型")}</div>}
               {defaultModelSelection && <button type="button" role="option" aria-selected={modelSelectionMode === "default"} className="composer-model-option composer-model-default" onClick={() => { void switchModel({ mode: "default" }); }} style={dropdownItem}>
                 <span><span>{tr("默认")}</span><small>{tr("项目配置的模型与强度")}</small></span><Check size={14} style={{ visibility: modelSelectionMode === "default" ? "visible" : "hidden" }} aria-hidden="true" />
               </button>}
               {selectableModels.map((m) => (
                   <button key={m} type="button" role="option" aria-selected={m === currentModel && modelSelectionMode !== "default"} className="composer-model-option" onClick={() => { void switchModel({ mode: "model", model: m }); }} style={{ ...dropdownItem, display: "grid", gridTemplateColumns: "minmax(0, 1fr) 16px" }}>
                  <span className="composer-model-option-label" title={m}>{formatModelLabel(availableModelLabels[m] || m)}</span>
                   <Check size={14} aria-hidden="true" style={{ visibility: m === currentModel && modelSelectionMode !== "default" ? "visible" : "hidden" }} />
                </button>
              ))}
              </div>
              {selectableModels.length === 0 && <div className="composer-model-menu-footer">
                <button
                  type="button"
                  role="option"
                  aria-selected="false"
                  onClick={() => {
                    setModelOpen(false);
                    openSettings("provider");
                  }}
                  style={dropdownItem}
                >
                  {tr("配置模型…")}</button>
              </div>}
            </div>
          )}
        {showReasoningEffort && effortOpen && (
          <div className="mc-dropdown-menu composer-picker-menu composer-reasoning-menu" role="dialog" aria-label={tr("模型与推理强度")} style={dropdownStyle("right")}>
            <EffortControl
              key={`${currentModel}:${composerOwnerId}:${composerEffortLevels.join(",")}`}
              options={effortOptions}
              selected={selectedEffort}
              modelLabel={modelLabel}
              onSelect={switchEffort}
              onReset={defaultModelSelection && modelSelectionMode === "model" ? () => { void switchModel({ mode: "default" }); } : undefined}
              onChooseModel={() => {
                setEffortOpen(false);
                setModelOpen(true);
              }}
            />
          </div>
        )}
        </div>
        </div>

        {sendState === "queue" && onStop ? (
          <Button
            type="button"
            variant="ghost"
            loading={isStopping}
            aria-disabled={isStopping || undefined}
            onClick={onStop}
             title={tr("停止当前回复")}
             aria-label={tr("停止当前回复")}
            className="composer-stop-current-btn w-7 h-7 border-0 inline-flex items-center justify-center"
            style={{ padding: 0 }}
            icon={<Square size={14} fill="currentColor" className="anim-icon-swap" />}
          />
        ) : null}
         <RealtimeComposerActions sendButton={<SendIconBtn sendState={sendState} isStopping={isStopping} onSend={onSend} disabledReason={disabledReason} />} />
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
  menuRole = "listbox",
  disabled = false,
  warning = false,
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
  menuRole?: "listbox" | "dialog";
  disabled?: boolean;
  warning?: boolean;
}) => (
  <div ref={refEl} className={`${className ?? "composer-permission-picker"} relative`} onBlur={(event) => {
    if (!event.currentTarget.contains(event.relatedTarget)) setOpen(false);
  }}>
    <button
      type="button"
      onClick={() => {
        setOpen(!open);
      }}
      className="composer-permission-btn"
      disabled={disabled}
      data-warning={warning || undefined}
      aria-expanded={open}
      aria-haspopup={menuRole}
      aria-label={title}
      style={{ ...pill, color: warning ? "var(--state-warning)" : pill.color, background: open ? "var(--surface-page)" : "var(--surface-soft)", borderColor: open ? "var(--border-subtle)" : "transparent" }}
      title={title}
    >
      {icon}
      <span className="composer-control-label">{label}</span>
    </button>
    {open && <div className="mc-dropdown-menu composer-picker-menu" role={menuRole} aria-label={title} style={dropdownStyle(align)}>{children}</div>}
  </div>
);

function EffortControl({ options, selected, modelLabel, onSelect, onChooseModel, onReset }: {
  options: EffortOption[];
  selected: EffortOption;
  modelLabel: string;
  onSelect: (level: EffortLevel) => void;
  onChooseModel: () => void;
  onReset?: () => void;
}) {
  const { t: tr } = useI18n();
  const selectedIndex = options.findIndex((option) => option.id === selected.id);
  const [dragIndex, setDragIndex] = useState<number | null>(null);
  const index = dragIndex ?? Math.max(0, selectedIndex);
  const [dragging, setDragging] = useState(false);
  const canSlide = selectedIndex >= 0 && options.length > 1;
  const current = canSlide ? options[index] : selected;
  const commit = () => {
    if (options[index].id !== selected.id) onSelect(options[index].id);
    setDragIndex(null);
    setDragging(false);
  };
  return <div className="composer-effort-control">
    <div className="composer-effort-heading">
      <div className="composer-effort-selection">
        <span className="composer-effort-value">{current.label}</span>
        <button type="button" className="composer-effort-model-toggle" aria-label={tr("选择模型")} title={modelLabel} aria-haspopup="listbox" onClick={onChooseModel}>
          <span className="composer-effort-model">{modelLabel}</span>
          <ChevronRight size={14} aria-hidden="true" />
        </button>
      </div>
      {onReset && <button type="button" className="composer-effort-reset" title={tr("重置为默认")} aria-label={tr("重置为默认")} onClick={onReset}><RotateCcw size={16} /></button>}
    </div>
    {canSlide && <div
      className="composer-effort-slider"
      data-dragging={dragging}
      data-effort={current.id}
      style={{ "--effort-progress": `${index / (options.length - 1) * 100}%` } as React.CSSProperties}
    >
      <div className="composer-effort-track" aria-hidden="true">
        <div className="composer-effort-rail"><span className="composer-effort-fill" /></div>
      </div>
      <div className="composer-effort-rail" aria-hidden="true">
        {options.map((option, step) => <span key={option.id} className="composer-effort-dot" data-active={step <= index} style={{ left: `${step / (options.length - 1) * 100}%` }} />)}
        <span className="composer-effort-thumb" />
      </div>
      <input
        className="composer-effort-input"
        type="range" min={0} max={options.length - 1} step={1} value={index}
        aria-label={tr("推理强度")} aria-valuetext={current.label}
        onChange={(event) => setDragIndex(Number(event.target.value))}
        onPointerDown={(event) => event.currentTarget.setPointerCapture(event.pointerId)}
        onPointerMove={(event) => { if (event.buttons === 1) setDragging(true); }}
        onPointerUp={commit}
        onPointerCancel={() => { setDragIndex(null); setDragging(false); }}
        onBlur={commit}
        onKeyUp={(event) => {
          if (event.nativeEvent.isComposing || event.keyCode === 229) return;
          if (["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home", "End", "PageUp", "PageDown"].includes(event.key)) commit();
        }}
      />
    </div>}
    {!canSlide && <div role="listbox" aria-label={tr("推理档位")} className="composer-effort-options">
      {options.map((option) => <MenuChoice key={option.id} label={option.label} active={selected.id === option.id} onClick={() => onSelect(option.id)} />)}
    </div>}
  </div>;
}

const MenuChoice = ({
  active,
  label,
  onClick,
  icon,
  description,
  warning = false,
  disabled = false,
}: {
  active: boolean;
  label: string;
  onClick: () => void;
  icon?: React.ReactNode;
  description?: string;
  warning?: boolean;
  disabled?: boolean;
}) => (
  <button
    type="button"
    role="option"
    aria-selected={active}
    aria-label={label}
    onClick={onClick}
    disabled={disabled}
    className="composer-menu-choice"
    data-warning={warning ? "true" : undefined}
    style={{
      ...dropdownItem,
      color: warning ? "var(--state-warning)" : dropdownItem.color,
      display: "grid",
      gridTemplateColumns: icon ? "20px minmax(0, 1fr) 16px" : "18px minmax(0, 1fr)",
      columnGap: 8,
      alignItems: "center",
      background: active ? dropdownActiveBg : "transparent",
      padding: "7px 9px",
    }}
  >
    <span className="composer-menu-choice-icon" aria-hidden="true" style={{ visibility: icon || active ? "visible" : "hidden" }}>
      {icon || <Check size={14} />}
    </span>
    <span className="composer-menu-choice-copy"><span>{label}</span>{description && <small>{description}</small>}</span>
    {icon && <Check size={14} aria-hidden="true" style={{ visibility: active ? "visible" : "hidden" }} />}
  </button>
);

const effortOption = (level: EffortLevel, tr: Translate): EffortOption => {
  const normalized = String(level || "").trim().toLowerCase() as EffortLevel;
  const known = KNOWN_EFFORT_OPTIONS[normalized];
  if (known) return { id: normalized, label: known.label === "Ultra" ? "Ultra" : tr(known.label), description: tr(known.description) };
  return {
    id: normalized,
    label: normalized,
    description: tr("Provider 声明的推理强度：{effort}", { effort: normalized }),
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

const ContextUsageRing = memo(() => {
  const { t: tr } = useI18n();
  const contextUsage = useAppStore((s) => s.contextUsage);
  const budgetBuckets = useAppStore((s) => s.budgetBuckets);
  const totalBudgetPercent = useAppStore((s) => s.totalBudgetPercent);
  const conversationId = useAppStore((s) => s.conversationId);
  const currentModel = useAppStore((s) => s.currentModel);
  const appMode = useAppStore((s) => s.appMode);
  const isConnected = useAppStore((s) => s.isConnected);
  const pendingConversationSwitchId = useAppStore((s) => s.pendingConversationSwitchId);
  const hydration = useAppStore((s) => conversationId ? s.conversationHydration[conversationId] : undefined);
  const ownerReady = isConnected && !pendingConversationSwitchId && !hydration?.isHydrating;

  useEffect(() => {
    if (!conversationId || !ownerReady) return;
    sendClientCommand({
      type: "session.usage.inspect",
      conversation_id: conversationId,
      source: "usage_ring_auto",
      silent: true,
    });
  }, [appMode, conversationId, currentModel, ownerReady, hydration?.updatedAt]);

  if (!contextUsage && budgetBuckets.length === 0 && totalBudgetPercent <= 0) return null;

  return (
    <button
      type="button"
      onClick={() => sendClientCommand({
        type: "session.usage.inspect",
        conversation_id: conversationId || undefined,
        source: "usage_ring",
      })}
       aria-label={tr("查看会话用量详情")}
      className="composer-context-usage"
      style={{ background: "transparent", border: 0, padding: 0, cursor: "pointer" }}
    >
      <UsageRing buckets={budgetBuckets} contextUsage={contextUsage} totalBudgetPercent={totalBudgetPercent} />
    </button>
  );
});
ContextUsageRing.displayName = "ContextUsageRing";

const ToggleChip = ({ label, active, onClick, disabled = false }: { label: string; active: boolean; onClick: () => void; disabled?: boolean }) => (
  <button
    onClick={onClick}
    disabled={disabled}
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

const permissionLabel = (mode: PermissionMode, tr: Translate): string => {
  if (mode === "bypass") return tr("完全访问");
  return tr("请求批准");
};

const FullAccessDetails = ({ tr }: { tr: Translate }) => <div className="composer-full-access-details">
  <p>{tr("MiniCode 将能够在未经您许可的情况下，在这台计算机上的任何位置运行命令、使用互联网，以及创建和编辑文件。这包括但不限于：")}</p>
  <div className="composer-full-access-capabilities">
    {[
      { Icon: Folder, title: "文件和文件夹", description: "读取、创建、修改、上传或删除此计算机上任意位置的文件" },
      { Icon: TerminalSquare, title: "终端命令", description: "运行命令、安装软件和更改系统设置" },
      { Icon: Globe, title: "互联网和已连接的应用", description: "访问网站、发送数据并使用已启用的插件" },
    ].map(({ Icon, title, description }) => <div key={title}><Icon size={24} /><span><strong>{tr(title as TranslationKey)}</strong><small>{tr(description as TranslationKey)}</small></span></div>)}
  </div>
  <p>{tr("这会带来敏感数据丢失或泄露、提示注入等风险。你可以将其关闭。")}{" "}<button type="button" onClick={openPermissionsHelp}>{tr("了解更多")}</button></p>
</div>;

const ciStatusLabel = (status: string, tr: Translate): string => ({
  running: tr("运行中"),
  passed: tr("已通过"),
  failed: tr("失败"),
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

const footerStyle: React.CSSProperties = {
  fontSize: "var(--text-xs)",
};

const footerLeftStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 4,
  minWidth: 0,
};

const iconBtn: React.CSSProperties = {
  background: "transparent",
  color: "var(--text-secondary)",
  border: "1px solid transparent",
  cursor: "pointer",
  width: 28,
  height: 28,
  padding: 0,
  display: "inline-flex",
  alignItems: "center",
  justifyContent: "center",
  borderRadius: "var(--radius-full)",
};

const pill: React.CSSProperties = {
  background: "transparent",
  color: "var(--text-secondary)",
  border: "1px solid transparent",
  borderRadius: "var(--radius-full)",
  padding: "0 8px",
  cursor: "pointer",
  fontSize: "var(--text-xs)",
  lineHeight: 1,
};

const dropdownStyle = (align: "left" | "right" = "left"): React.CSSProperties => ({
  position: "absolute",
  bottom: "calc(100% + 6px)",
  left: `var(--composer-menu-left, ${align === "left" ? "0px" : "auto"})`,
  right: `var(--composer-menu-right, ${align === "right" ? "0px" : "auto"})`,
  minWidth: "var(--composer-menu-min-width, var(--composer-menu-width, 280px))",
  background: "var(--surface-page)",
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-md, 8px)",
  boxShadow: "var(--shadow-soft)",
  padding: 6,
  zIndex: "var(--z-sticky)",
  maxHeight: "min(var(--composer-menu-height, 260px), 55dvh)",
  overflowY: "auto",
});

const dropdownActiveBg = "var(--surface-soft)";

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

const SendIconBtn = memo(({ sendState, isStopping, onSend, disabledReason }: { sendState: SendButtonState; isStopping: boolean; onSend: () => void; disabledReason?: string }) => {
  const { t: tr } = useI18n();
  const disabled = sendState === "disabled";
  const loading = sendState === "sending" || sendState === "stop" && isStopping;
  const label = sendState === "stop"
    ? tr("停止当前回复")
    : sendState === "queue"
      ? tr("将消息加入队列")
      : sendState === "offline-queue"
         ? tr("连接恢复后发送")
         : tr("发送");
  return (
    <Button variant="ghost" onClick={onSend} disabled={disabled} loading={loading} aria-disabled={disabled || loading || undefined}
      title={disabledReason || label} aria-label={label} className="btn-send composer-send-btn w-7 h-7 border-0 inline-flex items-center justify-center" style={sendIconButtonStyle(sendState, disabled)}
      icon={sendState === "stop" ? <Square size={14} fill="currentColor" className="anim-icon-swap" /> : <ArrowUp size={16} strokeWidth={1.8} className="anim-icon-swap" />} />
  );
});
SendIconBtn.displayName = "SendIconBtn";

const sendIconButtonStyle = (sendState: SendButtonState, disabled: boolean): React.CSSProperties => ({
  padding: 0,
  borderRadius: "var(--radius-full)",
  background: sendState === "stop" ? "var(--text-primary)" : disabled ? "var(--surface-active)" : "var(--accent-primary)",
  color: disabled ? "var(--text-muted)" : "var(--text-on-accent)",
  cursor: disabled ? "not-allowed" : "pointer",
});
